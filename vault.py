"""Markdown-first workspace over explicitly registered local vaults.

No implicit home-directory discovery, no executable files, and no generated
copy of note bodies: the file on disk is always the source of truth.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import time
import threading
import unicodedata
from urllib.parse import quote, unquote
import uuid

_LOCK = threading.RLock()
_EXTENSIONS = {'.md', '.markdown', '.txt'}
_WRITE_EXTENSIONS = {'.md', '.markdown'}
_SKIP_DIRS = {'node_modules', '__pycache__', 'runtime', 'runs', 'logs', 'auth',
              'credentials', 'secrets', 'cache', 'caches', 'vault-history'}
_SKIP_NAMES = {'auth.md', 'credentials.md', 'secrets.md', 'token.md', 'tokens.md',
               'passwords.md', 'private-key.md'}
_MAX_BYTES = 8 * 1024 * 1024


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _normal(value):
    return unicodedata.normalize('NFKC', value).casefold()


class VaultWorkspace:
    def __init__(self, settings, data_dir):
        self.data_dir = Path(data_dir).expanduser().absolute()
        self._roots = {}
        self._cache = {}
        self._cache_bytes = 0
        self._indexed = {}
        self._reverse_keys = {}
        self._file_keys = {}
        self._root_scan_status = {}
        self._scan_local = threading.local()
        self._search_states = {}
        self.scan_status = {'complete': False, 'scanned_files': 0, 'scope': [],
                            'reason': '尚未扫描知识库'}
        budget = settings.get('vault_scan_budget', {})
        self._max_files = max(1, min(int(budget.get('files', 5000)), 50000))
        self._max_scan_bytes = max(1024, min(int(budget.get('bytes', 67108864)), 268435456))
        self._max_scan_seconds = max(.01, min(float(budget.get('seconds', 2)), 30))
        for item in settings.get('vault_roots', []):
            if not isinstance(item, dict):
                raise ValueError('知识库配置必须为对象')
            rid = item.get('id', '')
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', rid) or rid in self._roots:
                raise ValueError('知识库标识无效或重复')
            raw = item.get('path')
            if not isinstance(raw, str) or not raw.strip() or not Path(raw).expanduser().is_absolute():
                raise ValueError('知识库必须配置明确的绝对路径')
            root = Path(raw).expanduser().absolute()
            exclude = item.get('exclude', [])
            if not isinstance(exclude, list) or any(not isinstance(pattern, str) for pattern in exclude):
                raise ValueError('知识库 exclude 必须是 glob 字符串列表')
            self._roots[rid] = {'id': rid, 'label': str(item.get('label') or rid),
                                'path': root, 'writable': item.get('writable') is True,
                                'exclude': exclude}

    @property
    def scan_status(self):
        # HTTP requests and model tool calls may search concurrently; do not
        # accidentally report another request's scope/completeness.
        return getattr(self._scan_local, 'status', self._last_scan_status)

    @scan_status.setter
    def scan_status(self, value):
        self._scan_local.status = value
        self._last_scan_status = value

    @staticmethod
    def _excluded(root, relative, directory=False):
        parts = PurePosixPath(relative).parts
        candidates = [relative]
        if directory:
            candidates.append(relative.rstrip('/') + '/')
        for count in range(1, len(parts)):
            parent = '/'.join(parts[:count])
            candidates.extend([parent, parent + '/'])
        return any(fnmatch.fnmatchcase(candidate, pattern)
                   for candidate in candidates for pattern in root.get('exclude', []))

    @staticmethod
    def _no_symlinks(path):
        # Check every component, including an explicitly configured root itself.
        for p in reversed([path, *path.parents]):
            if p.is_symlink():
                raise ValueError('知识库不读取或写入符号链接')

    def _root(self, root_id, write=False):
        root = self._roots.get(root_id)
        if not root:
            raise ValueError('未注册的知识库')
        self._no_symlinks(root['path'])
        if not root['path'].is_dir():
            raise FileNotFoundError('知识库目录暂不可用')
        if write and not root['writable']:
            raise ValueError('这个知识库为只读')
        return root

    def roots(self):
        result = []
        for rid, root in self._roots.items():
            try:
                self._root(rid)
                available = True
            except (ValueError, OSError):
                available = False
            result.append({'id': rid, 'label': root['label'],
                           'writable': root['writable'], 'available': available})
        return result

    @staticmethod
    def _relative(value, allow_empty=False):
        if not isinstance(value, str) or '\\' in value or '\x00' in value:
            raise ValueError('知识库相对路径无效')
        if value == '' and allow_empty:
            return PurePosixPath('.')
        parts = value.split('/')
        if not value or any(p in ('', '.', '..') for p in parts) or value.startswith('/'):
            raise ValueError('知识库路径不能越界')
        if any(p.startswith('.') or p.casefold() in _SKIP_DIRS for p in parts):
            raise ValueError('此目录不进入知识库')
        if parts[-1].casefold() in _SKIP_NAMES or (Path(parts[-1]).suffix.casefold() == '.txt' and Path(parts[-1]).stem.casefold() + '.md' in _SKIP_NAMES):
            raise ValueError('认证和密钥文件不进入知识库')
        return PurePosixPath(value)

    def _path(self, root_id, relative, *, directory=False, write=False):
        root = self._root(root_id, write)
        rel = self._relative(relative, allow_empty=directory)
        if relative and self._excluded(root, relative, directory):
            raise ValueError('该路径已被知识库排除规则禁止访问')
        path = root['path'].joinpath(*rel.parts)
        self._no_symlinks(path)
        if not path.is_relative_to(root['path']):
            raise ValueError('知识库路径不能越界')
        if not directory and path.suffix.casefold() not in _EXTENSIONS:
            raise ValueError('知识库仅支持 Markdown 与 TXT 文本')
        if write and not directory and path.suffix.casefold() not in _WRITE_EXTENSIONS:
            raise ValueError('TXT 原始资料为只读；请将笔记保存为 Markdown')
        return root, path

    @staticmethod
    def file_id(root_id, relative):
        return root_id + ':' + quote(relative, safe='/')

    @staticmethod
    def _split(file_id):
        if not isinstance(file_id, str) or ':' not in file_id:
            raise ValueError('文档标识无效')
        rid, encoded = file_id.split(':', 1)
        return rid, unquote(encoded)

    @staticmethod
    @contextmanager
    def _directory_fd(path, create=False):
        """Anchor each parent inode, never following a replaced parent symlink."""
        absolute = Path(path).absolute()
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open('/', flags)
        try:
            for name in absolute.parts[1:]:
                if name in ('', '.', '..'):
                    raise ValueError('目录路径无效')
                if create:
                    try:
                        os.mkdir(name, mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                try:
                    next_fd = os.open(name, flags, dir_fd=fd)
                except OSError as exc:
                    raise ValueError('目录不可用或含符号链接，操作已停止') from exc
                os.close(fd)
                fd = next_fd
            yield fd
        finally:
            os.close(fd)

    @staticmethod
    def _bytes_at(parent_fd, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        with os.fdopen(fd, 'rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('不是普通文本文件')
            if info.st_size > _MAX_BYTES:
                raise ValueError('文档超过 8 MB，请先拆分后阅读')
            data = handle.read(_MAX_BYTES + 1)
        if len(data) > _MAX_BYTES:
            raise ValueError('文档超过 8 MB，请先拆分后阅读')
        return data

    @classmethod
    def _bytes(cls, path):
        with cls._directory_fd(path.parent) as parent_fd:
            return cls._bytes_at(parent_fd, path.name)

    @staticmethod
    def _text(data):
        try:
            text = data.decode('utf-8-sig')
        except UnicodeDecodeError as exc:
            raise ValueError('Markdown 需要 UTF-8 编码') from exc
        if '\x00' in text:
            raise ValueError('文件含二进制内容，未作为 Markdown 读取')
        return text

    @staticmethod
    def _display_title(prefix, fallback):
        """A bounded plain-text title reader, deliberately not a YAML executor."""
        prefix = prefix.lstrip('\ufeff')
        frontmatter = re.match(r'\A---[ \t]*\r?\n(.*?)^---[ \t]*\r?$', prefix, re.M | re.S)
        if frontmatter:
            title = re.search(r'^title:[ \t]*(.+?)\s*$', frontmatter.group(1), re.M | re.I)
            if title:
                value = title.group(1).strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                if value and value not in ('|', '>', '|-', '>-'):
                    return value[:200]
        heading = re.search(r'^#[ \t]+([^\r\n]+?)\s*#*[ \t]*$', prefix, re.M)
        return heading.group(1)[:200] if heading else fallback

    def _tree_title(self, folder_fd, name):
        fallback = Path(name).stem
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder_fd)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    return fallback
                prefix = os.read(fd, 8192)
            finally:
                os.close(fd)
            if b'\x00' in prefix:
                return fallback
            return self._display_title(prefix.decode('utf-8-sig', errors='replace'), fallback)
        except (ValueError, OSError):
            return fallback

    def _document(self, rid, relative):
        root, path = self._path(rid, relative)
        data = self._bytes(path)
        text = self._text(data)
        doc = {'file_id': self.file_id(rid, relative), 'root_id': rid,
               'path': relative, 'title': self._display_title(text[:8192], path.stem),
               'text': text, 'sha': _sha(data),
               'writable': root['writable'] and path.suffix.casefold() in _WRITE_EXTENSIONS}
        self._index_document(doc)
        return doc

    def tree(self, root_id=None, path=''):
        """One directory level. Directories can be expanded on demand."""
        if root_id is None:
            return {'roots': self.roots(), 'entries': []}
        root, folder = self._path(root_id, path, directory=True)
        if not folder.is_dir():
            raise FileNotFoundError('知识库目录不存在')
        entries = []
        with self._directory_fd(folder) as folder_fd, os.scandir(folder_fd) as listing:
            for entry in listing:
                relative = (PurePosixPath(path) / entry.name).as_posix()
                try:
                    self._relative(relative)
                except ValueError:
                    continue
                is_dir = entry.is_dir(follow_symlinks=False)
                if entry.is_symlink() or self._excluded(root, relative, is_dir):
                    continue
                suffix = Path(entry.name).suffix.casefold()
                if is_dir:
                    kind = 'directory'
                elif entry.is_file(follow_symlinks=False) and suffix in _EXTENSIONS:
                    kind = 'file'
                else:
                    continue
                writable = root['writable'] and (kind == 'directory' or suffix in _WRITE_EXTENSIONS)
                entries.append({'file_id': self.file_id(root_id, relative), 'root_id': root_id,
                                'path': relative, 'filename': entry.name,
                                'title': entry.name if kind == 'directory' else self._tree_title(folder_fd, entry.name),
                                'kind': kind, 'writable': writable, 'read_only': not writable})
        entries.sort(key=lambda x: (x['kind'] != 'directory', _normal(x['title'])))
        return {'roots': self.roots(), 'entries': entries}

    def _root_search_files(self, rid, state):
        try:
            spec = self._root(rid)
            root = spec['path']
        except (ValueError, OSError):
            state['errors'].add('部分集合暂不可用')
            return
        def on_error(_error):
            state['errors'].add('部分目录无法读取')
        for folder, dirs, files in os.walk(root, followlinks=False, onerror=on_error):
            base = Path(folder)
            dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d.casefold() not in _SKIP_DIRS
                             and not (base / d).is_symlink()
                             and not self._excluded(spec, (base / d).relative_to(root).as_posix(), True))
            for name in sorted(files):
                path = base / name
                if path.suffix.casefold() not in _EXTENSIONS or path.is_symlink():
                    continue
                relative = path.relative_to(root).as_posix()
                try:
                    self._relative(relative)
                except ValueError:
                    continue
                if not self._excluded(spec, relative):
                    yield rid, relative, path

    def _search_files(self, scope, state):
        # One file per live collection, then move on. The deque itself remains
        # suspended across budget boundaries, so later roots cannot be starved
        # by a large first collection, even with a one-file batch budget.
        queues = deque(self._root_search_files(rid, state) for rid in scope)
        while queues:
            iterator = queues.popleft()
            try:
                value = next(iterator)
            except StopIteration:
                continue
            queues.append(iterator)
            yield value

    def _cached_document(self, rid, relative, path):
        self._no_symlinks(path)
        info = path.stat()
        stamp = (info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_ino)
        fid = self.file_id(rid, relative)
        old = self._cache.get(fid)
        doc = old[1] if old and old[0] == stamp else self._document(rid, relative)
        self._drop_cached(fid)
        self._cache[fid] = (stamp, doc)
        self._cache_bytes += info.st_size
        while self._cache and self._cache_bytes > self._max_scan_bytes:
            self._drop_cached(next(iter(self._cache)))
        return doc, info.st_size

    @staticmethod
    def _match_document(doc, terms):
        title = _normal(doc['title'] + ' ' + doc['path'])
        full = _normal(doc['text'])
        if not all(term in full or term in title for term in terms):
            return None
        lines = doc['text'].splitlines() or ['']
        hits, index = max(((sum(term in line for term in terms), i)
                           for i, line in enumerate(map(_normal, lines))), key=lambda x: (x[0], -x[1]))
        line = lines[index]
        positions = [line.casefold().find(term) for term in terms if term in line.casefold()]
        start = max(0, min(positions) - 80) if positions else 0
        excerpt = line[start:start + 340] or '\n'.join(lines[max(0, index-1):index+2])[:340]
        score = sum(8 for term in terms if term in title) + hits * 2
        return score, {'file_id': doc['file_id'], 'root_id': doc['root_id'],
                       'path': doc['path'], 'title': doc['title'],
                       'excerpt': excerpt, 'line': index + 1, 'sha': doc['sha']}

    def search(self, query, limit=40, root_id=None):
        """Repeat the same query/scope to continue a bounded incomplete scan.

        Only top matches and a directory iterator survive between batches, not a
        second unbounded copy of the entire vault. A completed query starts a new
        fresh pass when next requested; every returned match is revalidated.
        """
        query = str(query).strip()
        if not query:
            self.scan_status = {'complete': True, 'has_more': False, 'scanned_files': 0,
                                'scope': [], 'reason': '搜索词为空'}
            return []
        if len(query) > 500:
            raise ValueError('搜索词过长')
        if root_id is not None and root_id not in self._roots:
            raise ValueError('未注册的知识库')
        scope = [root_id] if root_id is not None else list(self._roots)
        terms = list(dict.fromkeys(_normal(query).split()))
        limit = max(1, min(int(limit), 100))
        key = (query, tuple(scope), limit)
        with _LOCK:
            state = self._search_states.get(key)
            if not state or state['finished']:
                if len(self._search_states) >= 4:
                    self._search_states.pop(next(iter(self._search_states)))
                state = {'finished': False, 'matches': {}, 'errors': set(),
                         'scanned': 0, 'bytes': 0, 'skipped': 0, 'seen_ids': set(), 'started_at': time.time()}
                state['iterator'] = self._search_files(scope, state)
                self._search_states[key] = state
            started = time.monotonic()
            count = 0
            batch_bytes = 0
            while count < self._max_files and (not count or time.monotonic()-started < self._max_scan_seconds):
                try:
                    rid, relative, path = next(state['iterator'])
                except StopIteration:
                    state['finished'] = True
                    break
                count += 1
                state['scanned'] += 1
                state['seen_ids'].add(self.file_id(rid, relative))
                try:
                    doc, size = self._cached_document(rid, relative, path)
                    batch_bytes += size
                    state['bytes'] += size
                    matched = self._match_document(doc, terms)
                    if matched:
                        state['matches'][doc['file_id']] = matched
                except (OSError, ValueError) as exc:
                    state['skipped'] += 1
                    state['errors'].add('部分文本超过 8 MB、编码异常或读取失败')
                    continue
                if batch_bytes >= self._max_scan_bytes:
                    break
            # Bound accumulated results, while continuation keeps scanning later files.
            ranked = sorted(state['matches'].values(), key=lambda x: (-x[0], _normal(x[1]['path'])))[:limit]
            state['matches'] = {item[1]['file_id']: item for item in ranked}
            fresh = []
            for _, previous in ranked:
                try:
                    doc, _ = self._cached_document(previous['root_id'], previous['path'],
                                                    self._roots[previous['root_id']]['path'] / previous['path'])
                    matched = self._match_document(doc, terms)
                    if matched:
                        fresh.append(matched)
                except (OSError, ValueError):
                    continue
            state['matches'] = {item[1]['file_id']: item for item in fresh}
            complete = state['finished'] and not state['errors']
            reasons = sorted(state['errors'])
            if not state['finished']:
                reasons.append('扫描尚未完成；再次发送相同搜索和集合即可继续后续文件')
            self.scan_status = {'complete': complete, 'has_more': not state['finished'],
                                'continuation': 'repeat_same_query' if not state['finished'] else None,
                                'scope': scope, 'scanned_files': state['scanned'],
                                'batch_scanned_files': count, 'skipped_files': state['skipped'],
                                'scanned_bytes': state['bytes'], 'matched_files': len(fresh),
                                'duration_ms': round((time.monotonic()-started)*1000),
                                'as_of': state['started_at'],
                                'reason': '；'.join(reasons),
                                'limits': {'files': self._max_files, 'bytes': self._max_scan_bytes,
                                           'seconds': self._max_scan_seconds}}
            for rid in scope:
                self._root_scan_status[rid] = dict(self.scan_status)
            if state['finished']:
                for fid, metadata in list(self._indexed.items()):
                    if metadata['root_id'] in scope and fid not in state['seen_ids']:
                        self._forget_document(fid)
            fresh.sort(key=lambda x: (-x[0], _normal(x[1]['path'])))
            return [dict(item[1], scan_complete=complete) for item in fresh]

    @staticmethod
    def _raw_links(text):
        # Code examples should not create document relationships.
        text = re.sub(r'```.*?```|~~~.*?~~~', '', text, flags=re.S)
        links = []
        for match in re.finditer(r'!?\[\[([^\]\n]+)\]\]|(?<!!)\[[^\]\n]*\]\(([^)\n]+)\)', text):
            target = match.group(1) or match.group(2)
            wiki = match.group(1) is not None
            if wiki:
                target = target.split('|', 1)[0]
            else:
                target = target.strip().strip('<>').split(' "', 1)[0]
            target = unquote(target.split('#', 1)[0].strip())
            if not target or re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', target) or target.startswith('/'):
                continue
            links.append((target, wiki))
        return list(dict.fromkeys(links))

    def _resolved_links(self, doc, docs):
        result = []
        for target, wiki in self._raw_links(doc['text']):
            if '\\' in target:
                continue
            candidates = []
            # Resolve relative links within the same registered root only.
            relative = os.path.normpath(str(PurePosixPath(doc['path']).parent / target))
            possible = [relative, target] if wiki else [relative]
            for value in possible:
                if not PurePosixPath(value).suffix:
                    value += '.md'
                try:
                    self._relative(value)
                except ValueError:
                    continue
                fid = self.file_id(doc['root_id'], value)
                if fid in docs:
                    candidates.append(fid)
            if wiki and not candidates and '/' not in target:
                candidates = [fid for fid, d in docs.items() if d['root_id'] == doc['root_id']
                              and (PurePosixPath(d['path']).stem == target or PurePosixPath(d['path']).name == target)]
            candidates = list(dict.fromkeys(candidates))
            found = docs[candidates[0]] if len(candidates) == 1 else None
            result.append({'target': target, 'file_id': found['file_id'] if found else None,
                           'title': found['title'] if found else target,
                           'resolved': found is not None, 'ambiguous': len(candidates) > 1})
        return result

    def _drop_cached(self, fid):
        previous = self._cache.pop(fid, None)
        if previous is not None:
            self._cache_bytes -= previous[0][2]

    def _forget_document(self, fid):
        self._indexed.pop(fid, None)
        self._drop_cached(fid)
        for key in self._file_keys.pop(fid, set()):
            refs = self._reverse_keys.get(key, set())
            refs.discard(fid)
            if not refs:
                self._reverse_keys.pop(key, None)

    def _index_document(self, doc):
        fid = doc['file_id']
        old = self._indexed.get(fid)
        if old and old.get('sha') == doc['sha']:
            return
        self._forget_document(fid)
        self._indexed[fid] = {key: doc[key] for key in ('file_id', 'root_id', 'path', 'title', 'sha', 'writable')}
        keys = set()
        for target, wiki in self._raw_links(doc['text']):
            if '\\' in target:
                continue
            possible = [os.path.normpath(str(PurePosixPath(doc['path']).parent / target))]
            if wiki:
                possible.append(target)
                if '/' not in target:
                    keys.add(doc['root_id'] + '|wiki:' + target)
            for value in possible:
                if not PurePosixPath(value).suffix:
                    value += '.md'
                try:
                    self._relative(value)
                except ValueError:
                    continue
                keys.add(self.file_id(doc['root_id'], value))
        self._file_keys[fid] = keys
        for key in keys:
            self._reverse_keys.setdefault(key, set()).add(fid)

    def _register_direct_link_targets(self, doc):
        # Resolve explicit relative links with metadata only, without walking or
        # reading any target body. Basename wiki links use the known index.
        for target, wiki in self._raw_links(doc['text']):
            possible = [os.path.normpath(str(PurePosixPath(doc['path']).parent / target))]
            if wiki:
                possible.append(target)
            for relative in possible:
                if not PurePosixPath(relative).suffix:
                    relative += '.md'
                fid = self.file_id(doc['root_id'], relative)
                if fid in self._indexed:
                    continue
                try:
                    root, path = self._path(doc['root_id'], relative)
                    with self._directory_fd(path.parent) as parent_fd:
                        info = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
                    if stat.S_ISREG(info.st_mode):
                        self._indexed[fid] = {'file_id': fid, 'root_id': doc['root_id'], 'path': relative,
                                              'title': path.stem, 'sha': None,
                                              'writable': root['writable'] and path.suffix.casefold() in _WRITE_EXTENSIONS}
                except (ValueError, OSError):
                    continue

    def stat(self, file_id):
        """Lightweight disk-version poll; no directory walk or link scan."""
        rid, relative = self._split(file_id)
        with _LOCK:
            _, path = self._path(rid, relative)
            doc, size = self._cached_document(rid, relative, path)
            return {**{key: doc[key] for key in ('file_id', 'root_id', 'path', 'title', 'sha', 'writable')},
                    'size': size}

    def read(self, file_id):
        """Read one body, resolve known links. Never launch a vault scan."""
        rid, relative = self._split(file_id)
        with _LOCK:
            doc = self._document(rid, relative)
            self._register_direct_link_targets(doc)
            lookup_keys = {doc['file_id'], rid + '|wiki:' + PurePosixPath(relative).stem,
                           rid + '|wiki:' + PurePosixPath(relative).name}
            candidates = set()
            for key in lookup_keys:
                candidates.update(self._reverse_keys.get(key, set()))
            candidates.discard(doc['file_id'])
            backlinks = []
            # Verify known inbound sources only, with a strict page limit.
            # A search builds this graph incrementally; read cannot sweep the vault.
            for fid in sorted(candidates)[:50]:
                previous = self._indexed.get(fid)
                if not previous:
                    continue
                try:
                    source, _ = self._cached_document(previous['root_id'], previous['path'],
                                                     self._roots[previous['root_id']]['path'] / previous['path'])
                    if any(link['file_id'] == doc['file_id'] for link in self._resolved_links(source, self._indexed)):
                        backlinks.append({key: source[key] for key in ('file_id', 'path', 'title')})
                except (ValueError, OSError):
                    self._forget_document(fid)
            scan = dict(self._root_scan_status.get(rid, {'complete': False, 'scope': [rid],
                        'scanned_files': 0, 'reason': '尚未建立此集合的完整链接索引；搜索会逐步补充'}))
            scan.update(index_is_live=False, current_body_fresh=True)
            if scan.get('complete'):
                scan['reason'] = '反链来自最近索引快照；新建或新增引用需通过搜索更新'
            if len(candidates) > 50:
                scan.update(complete=False, reason='当前仅显示前 50 条已知反链')
            return dict(doc, links=self._resolved_links(doc, self._indexed), backlinks=backlinks,
                        scan=scan)

    @staticmethod
    def _encode(text):
        if not isinstance(text, str) or '\x00' in text:
            raise ValueError('正文必须是 Markdown 文本')
        data = text.encode('utf-8')
        if len(data) > _MAX_BYTES:
            raise ValueError('文档超过 8 MB，请先拆分')
        return data

    def _history(self):
        folder = self.data_dir / 'vault-history'
        with self._directory_fd(folder, create=True) as fd:
            os.fchmod(fd, 0o700)
        return folder

    @staticmethod
    def _atomic_at(parent_fd, name, data, mode=0o600, replace=True):
        temp = '.upward-' + uuid.uuid4().hex
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     mode, dir_fd=parent_fd)
        try:
            with os.fdopen(fd, 'wb') as handle:
                os.fchmod(handle.fileno(), mode)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if replace:
                os.replace(temp, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            else:
                os.link(temp, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                        follow_symlinks=False)
            os.fsync(parent_fd)
        finally:
            try:
                os.unlink(temp, dir_fd=parent_fd)
            except FileNotFoundError:
                pass

    @classmethod
    def _atomic(cls, path, data, mode=0o600):
        with cls._directory_fd(path.parent) as parent_fd:
            cls._atomic_at(parent_fd, path.name, data, mode)

    def save(self, file_id, text, expected_sha):
        return self._save_bytes(file_id, self._encode(text), expected_sha)

    def _save_bytes(self, file_id, data, expected_sha):
        rid, relative = self._split(file_id)
        with _LOCK:
            _, path = self._path(rid, relative, write=True)
            with self._directory_fd(path.parent) as parent_fd:
                original = self._bytes_at(parent_fd, path.name)
                if not isinstance(expected_sha, str) or _sha(original) != expected_sha:
                    raise ValueError('文档已被外部修改，请重新读取并合并；未覆盖原文')
                if data == original:
                    return dict(self.read(file_id), undo_id=None, changed=False)
                undo_id = uuid.uuid4().hex
                history = self._history()
                self._atomic(history / (undo_id + '.md'), original)
                receipt = {'file_id': file_id, 'before_sha': _sha(original), 'after_sha': _sha(data)}
                self._atomic(history / (undo_id + '.json'), json.dumps(receipt).encode())
                # Both reads and commit use the SAME anchored parent descriptor.
                if _sha(self._bytes_at(parent_fd, path.name)) != expected_sha:
                    raise ValueError('保存期间文档发生变化，未覆盖原文')
                info = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
                self._atomic_at(parent_fd, path.name, data, stat.S_IMODE(info.st_mode))
            return dict(self.read(file_id), undo_id=undo_id, changed=True)

    def create(self, root_id, relative_path, text):
        data = self._encode(text)
        with _LOCK:
            _, path = self._path(root_id, relative_path, write=True)
            fid = self.file_id(root_id, relative_path)
            with self._directory_fd(path.parent, create=True) as parent_fd:
                try:
                    existing = self._bytes_at(parent_fd, path.name)
                except FileNotFoundError:
                    existing = None
                if existing is not None:
                    if existing == data:
                        return dict(self.read(fid), created=False)
                    raise ValueError('同路径文档已存在且内容不同，未覆盖；请读取后编辑')
                try:
                    self._atomic_at(parent_fd, path.name, data, replace=False)
                except FileExistsError as exc:
                    raise ValueError('同路径文档刚刚被创建，请重新读取') from exc
            return dict(self.read(fid), created=True)

    def undo(self, undo_id, expected_sha):
        if not isinstance(undo_id, str) or not re.fullmatch(r'[a-f0-9]{32}', undo_id):
            raise ValueError('撤销记录无效')
        with _LOCK:
            history = self._history()
            metadata = history / (undo_id + '.json')
            backup = history / (undo_id + '.md')
            self._no_symlinks(metadata); self._no_symlinks(backup)
            receipt = json.loads(self._bytes(metadata))
            original = self._bytes(backup)
            if _sha(original) != receipt['before_sha']:
                raise ValueError('备份校验失败，未执行撤销')
            if expected_sha != receipt['after_sha']:
                raise ValueError('文档版本与撤销记录不一致，请重新核对')
            # save's disk hash check refuses rollback over a newer external edit.
            return self._save_bytes(receipt['file_id'], original, expected_sha)
