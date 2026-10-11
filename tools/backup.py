#!/usr/bin/env python3
"""Private local backups with explicit shared-vault inclusion; never publish archives."""
from __future__ import annotations
import argparse
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 512 * 1024 * 1024


def _path(value):
    p = Path(value).expanduser().absolute()
    for parent in [p, *p.parents]:
        if parent.is_symlink():
            raise ValueError('备份路径不能包含符号链接')
    p = p.resolve()
    if p == ROOT or ROOT in p.parents:
        raise ValueError('私人备份和数据必须放在源码目录之外')
    return p


@contextmanager
def _read_regular(path):
    flags=os.O_RDONLY|os.O_DIRECTORY|getattr(os,'O_NOFOLLOW',0)
    directory=os.open(path.anchor,flags)
    try:
        for part in path.parent.parts[1:]:
            child=os.open(part,flags,dir_fd=directory);os.close(directory);directory=child
        fd=os.open(path.name,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0),dir_fd=directory)
        with os.fdopen(fd,'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):raise ValueError('只能备份普通文件')
            yield stream
    finally:os.close(directory)


def backup(data_dir, archive, vault_ids=(), max_bytes=MAX_BYTES):
    data, output = _path(data_dir), _path(archive)
    if not (data / 'desk.sqlite').is_file():
        raise ValueError('没有可备份的工作台数据库')
    if output.exists():
        raise ValueError('拒绝覆盖现有备份')
    settings = json.loads((data / 'settings.json').read_text()) if (data / 'settings.json').exists() else {}
    roots = {r['id']: r for r in settings.get('vault_roots', [])}
    selected = list(dict.fromkeys(vault_ids))
    if any(r not in roots for r in selected):
        raise ValueError('只能选择已登记的知识库标识')
    for rid in selected:
        if not rid or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in rid):
            raise ValueError('知识库标识无效')
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = {'version': 2, 'source_data': str(data), 'included_vaults': [],
                'external_vaults_not_included': [r for r in roots if r not in selected and not Path(roots[r]['path']).expanduser().absolute().is_relative_to(data)],
                'consistency': '跨文件一致性需要停止服务，并暂停其他应用编辑共享文档'}
    with tempfile.TemporaryDirectory(prefix='.upward-backup-', dir=output.parent) as folder:
        temporary = Path(folder)
        db = temporary / 'desk.sqlite'
        with closing(sqlite3.connect((data / 'desk.sqlite').as_uri() + '?mode=ro', uri=True)) as src, closing(sqlite3.connect(db)) as dst:
            src.backup(dst)
        entries = [('desk.sqlite', db)]
        for p in data.rglob('*'):
            if p.is_symlink():
                raise ValueError('数据目录含符号链接，未生成备份')
            if p.is_file() and p.name not in ('desk.sqlite', 'desk.sqlite-wal', 'desk.sqlite-shm', 'backup-manifest.json'):
                if p == output or temporary == p or temporary in p.parents:
                    continue
                entries.append((p.relative_to(data).as_posix(), p))
        for rid in selected:
            root = _path(roots[rid]['path'])
            if not root.is_dir():
                raise ValueError('选择的知识库暂不可用')
            if root == output or root in output.parents:
                raise ValueError('备份输出不能放进选择的知识库内')
            if root == data or data in root.parents or root in data.parents:
                raise ValueError('选择的知识库与数据目录重叠，请单独备份')
            # Respect the same text, exclusion and no-symlink boundary as the workspace.
            import sys
            if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
            from vault import VaultWorkspace
            vault = VaultWorkspace({'vault_roots': [roots[rid]]}, temporary / 'validation')
            count = 0
            for p in root.rglob('*'):
                if p.is_symlink():
                    raise ValueError('共享知识库含符号链接，未生成备份')
                if not p.is_file():
                    continue
                rel = p.relative_to(root).as_posix()
                try: vault._path(rid, rel)
                except ValueError: continue
                entries.append(('shared-vaults/' + rid + '/' + rel, p)); count += 1
            manifest['included_vaults'].append({'id': rid, 'label': roots[rid].get('label', rid),
                'original_path': str(root), 'writable': roots[rid].get('writable') is True,
                'files': count, 'exclude': list(roots[rid].get('exclude', [])), 'scope': '工作台可读文本；不包含Obsidian插件与二进制附件'})
        names = [name for name, _ in entries]
        if len(names) != len(set(names)) or 'backup-manifest.json' in names:
            raise ValueError('备份文件名冲突，请检查数据目录')
        if len(entries) > 50000 or sum(p.stat().st_size for _, p in entries) > max_bytes:
            raise ValueError('备份超过大小或文件数限制，请缩小范围或提高明确的大小上限')
        draft = temporary / 'archive.zip'
        total = 0
        with zipfile.ZipFile(draft, 'x', zipfile.ZIP_DEFLATED) as z:
            for name, path in entries:
                with _read_regular(path) as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode): raise ValueError('只能备份普通文件')
                    raw = stream.read(max_bytes - total + 1)
                total += len(raw)
                if total > max_bytes: raise ValueError('备份超过大小限制')
                z.writestr(name, raw)
            z.writestr('backup-manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        draft.chmod(0o600)
        os.link(draft, output)  # Atomic no-overwrite publication on the same filesystem.
    return manifest


def restore(data_dir, archive, max_bytes=MAX_BYTES):
    data, source = _path(data_dir), _path(archive)
    if data.exists(): raise ValueError('恢复目录必须尚不存在，避免覆盖现有资料')
    data.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source) as z:
        infos = z.infolist(); names = [i.filename for i in infos]
        if len(names) != len(set(names)) or len(infos) > 50000:
            raise ValueError('备份含重复路径或过多文件')
        if sum(i.file_size for i in infos) > max_bytes: raise ValueError('备份超过大小限制')
        for i in infos:
            rel = PurePosixPath(i.filename)
            mode = (i.external_attr >> 16) & 0o170000
            if (rel.is_absolute() or not rel.parts or any(p in ('.', '..') or ':' in p for p in rel.parts)
                    or '\\' in i.filename or '\x00' in i.filename or mode not in (0, stat.S_IFREG, stat.S_IFDIR)):
                raise ValueError('备份包含不安全路径或特殊文件')
        if 'desk.sqlite' not in names: raise ValueError('缺少数据库')
        with tempfile.TemporaryDirectory(prefix='.upward-restore-', dir=data.parent) as folder:
            tmp = Path(folder) / 'data'; tmp.mkdir(mode=0o700)
            z.extractall(tmp)
            with closing(sqlite3.connect((tmp/'desk.sqlite').as_uri()+'?mode=ro', uri=True)) as conn:
                if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('备份数据库完整性检查失败')
                conn.execute('SELECT id,kind,data,rev FROM objects LIMIT 1')
            manifest = json.loads((tmp/'backup-manifest.json').read_text()) if (tmp/'backup-manifest.json').exists() else {}
            config = tmp/'settings.json'; restored = []
            if config.exists():
                settings = json.loads(config.read_text()); restored = []
                original_data = Path(manifest['source_data']) if manifest.get('source_data') else None
                if original_data and (not original_data.is_absolute() or '..' in original_data.parts):
                    raise ValueError('备份来源目录无效')
                for root in settings.get('vault_roots', []):
                    old = Path(root.get('path', ''))
                    if '..' in old.parts:
                        raise ValueError('备份知识库根包含越界路径')
                    if original_data and old.is_absolute() and old.is_relative_to(original_data):
                        rel = old.relative_to(original_data)
                        if not (tmp/rel).resolve().is_relative_to(tmp.resolve()) or not (data/rel).resolve().is_relative_to(data.resolve()):
                            raise ValueError('恢复知识库路径越界')
                        (tmp/rel).mkdir(parents=True, exist_ok=True)
                        restored.append(dict(root, path=str(data/rel)))
                for item in manifest.get('included_vaults', []):
                    rid = item['id']
                    if not isinstance(rid, str) or not rid or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in rid):
                        raise ValueError('备份知识库标识无效')
                    exclude = item.get('exclude', [])
                    if not isinstance(exclude, list) or any(not isinstance(p, str) for p in exclude):
                        raise ValueError('备份知识库排除规则无效')
                    (tmp/'shared-vaults'/rid).mkdir(parents=True, exist_ok=True)
                    restored.append({'id': rid, 'label': item.get('label', rid), 'path': str(data/'shared-vaults'/rid), 'writable': item.get('writable') is True, 'exclude': exclude})
                settings['vault_roots'] = restored
                settings['local_access'] = {'enabled': False, 'notes_enabled': False, 'read_roots': [], 'write_roots': []}
                for key in ('advisor_config','advisor_script','catalog','growth_plan','private_root','scene'):
                    settings.pop(key, None)
                config.write_text(json.dumps(settings, ensure_ascii=False, indent=2))
            for p in tmp.rglob('*'): p.chmod(0o700 if p.is_dir() else 0o600)
            if data.exists(): raise ValueError('恢复目标已出现，未覆盖')
            tmp.rename(data)
    return {'restored': True, 'shared_vaults': sum(r['path'].startswith(str(data/'shared-vaults')+os.sep) for r in restored),
            'notice': '恢复到新目录；已包含知识库指向恢复副本，未包含的外部路径与本机访问需重新连接'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['backup','restore']);parser.add_argument('--data-dir',required=True);parser.add_argument('--archive',required=True)
    parser.add_argument('--vault-root',action='append',default=[],help='备份时额外包含的已登记文本知识库ID，可重复')
    parser.add_argument('--max-bytes',type=int,default=MAX_BYTES)
    args=parser.parse_args()
    if args.max_bytes < 1: parser.error('大小上限必须大于0')
    try:
        result=backup(args.data_dir,args.archive,args.vault_root,args.max_bytes) if args.action=='backup' else restore(args.data_dir,args.archive,args.max_bytes)
    except (ValueError,OSError,sqlite3.Error,zipfile.BadZipFile) as exc: parser.exit(1,str(exc)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__ == '__main__': main()
