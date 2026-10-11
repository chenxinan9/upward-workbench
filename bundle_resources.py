"""Validate explicit, reviewed resource manifests; never execute bundled content."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat


def relative_path(value):
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise ValueError('Invalid resource path')
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(p in ('', '.', '..') for p in path.parts):
        raise ValueError('Unsafe resource path')
    return path


def no_symlinks(path):
    path = Path(path).expanduser().absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Resource paths must not contain symlinks')
    return path


def directory_fd(path, *, create=False):
    """Open/create every directory component without following a swapped symlink."""
    path = no_symlinks(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            if part in ('', '.', '..'):
                raise ValueError('Unsafe directory component')
            if create:
                try: os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError: pass
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd); fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_resource(root, relative):
    """Use descriptor traversal so a swapped parent cannot redirect a read."""
    path = no_symlinks(Path(root) / relative_path(relative))
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0)
    directory = os.open(path.anchor, flags | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, flags | os.O_DIRECTORY, dir_fd=directory)
            os.close(directory); directory = child
        fd = os.open(path.name, flags, dir_fd=directory)
    finally:
        os.close(directory)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4_000_000:
            raise ValueError('Resource is not a bounded regular file')
        data = stream.read(4_000_001)
        if len(data) > 4_000_000:
            raise ValueError('Resource exceeds size limit')
        data.decode('utf-8')
        return data


def verified_files(root, entries, *, metadata=(), required=()):
    """The manifest selects files. Traversal only rejects unregistered additions."""
    root = no_symlinks(root)
    if not isinstance(entries, list) or len(entries) > 2000:
        raise ValueError('Invalid resource inventory')
    content = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError('Invalid resource entry')
        rel = relative_path(entry.get('path')).as_posix()
        if rel in content or rel in metadata:
            raise ValueError('Duplicate resource inventory entry')
        if not isinstance(entry.get('bytes'), int) or isinstance(entry['bytes'], bool) or entry['bytes'] < 0:
            raise ValueError('Invalid resource byte count')
        if not isinstance(entry.get('sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', entry['sha256']):
            raise ValueError('Invalid resource checksum')
        data = read_resource(root, rel)
        if len(data) != entry['bytes'] or hashlib.sha256(data).hexdigest() != entry['sha256']:
            raise ValueError('Resource checksum mismatch: ' + rel)
        content[rel] = data
    if not set(required) <= content.keys():
        raise ValueError('Required resource is missing')
    actual = set()
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            if (Path(directory) / name).is_symlink():
                raise ValueError('Resource directory contains symlink')
        actual.update((Path(directory) / name).relative_to(root).as_posix() for name in files)
    if actual != set(content) | set(metadata):
        raise ValueError('Unregistered or missing resource file')
    return content


def advisor_resources(root):
    provenance_bytes = read_resource(root, 'provenance.json')
    provenance = json.loads(provenance_bytes)
    entries = provenance.get('files')
    if not isinstance(entries, list):
        raise ValueError('Missing advisor provenance inventory')
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError('Invalid advisor inventory entry')
        rel = relative_path(entry.get('path'))
        if rel.as_posix() not in ('SKILL.md', 'LICENSE') and not (
            len(rel.parts) > 1 and rel.parts[0] in ('references', 'examples') and rel.suffix.lower() in ('.md', '.txt', '.json')
        ):
            raise ValueError('Unreviewed advisor resource type')
    content = verified_files(root, entries, metadata=('provenance.json',), required=('SKILL.md', 'LICENSE'))
    content['provenance.json'] = provenance_bytes
    return content


def knowledge_resources(root):
    root = Path(root)
    if not root.exists() and not root.is_symlink():
        return [], {}
    manifest_bytes = read_resource(root, 'manifest.json')
    manifest = json.loads(manifest_bytes)
    if manifest.get('schema_version') != 1 or not isinstance(manifest.get('collections'), list):
        raise ValueError('Invalid knowledge bundle manifest')
    collections = manifest['collections']; ids = set(); paths = []
    for entry in collections:
        if not isinstance(entry, dict):
            raise ValueError('Invalid knowledge collection')
        identifier = entry.get('id')
        if not isinstance(identifier, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', identifier) or identifier in ids:
            raise ValueError('Invalid or duplicate knowledge collection ID')
        ids.add(identifier)
        path = relative_path(entry.get('path'))
        if any(path == old or path in old.parents or old in path.parents for old in paths):
            raise ValueError('Overlapping knowledge collection paths')
        paths.append(path)
        if any(not isinstance(entry.get(key), str) or not entry[key].strip() for key in ('title', 'license', 'source_url')):
            raise ValueError('Knowledge collection needs attribution and license')
    files = manifest.get('files')
    if not isinstance(files, list):
        raise ValueError('Missing knowledge file inventory')
    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError('Invalid knowledge inventory entry')
        path = relative_path(entry.get('path'))
        if sum(parent in path.parents for parent in paths) != 1 or (path.suffix.lower() not in ('.md', '.txt', '.json') and path.name != 'LICENSE'):
            raise ValueError('Knowledge file must belong to exactly one collection')
    content = verified_files(root, files, metadata=('manifest.json',))
    for path in paths:
        if not any(path in PurePosixPath(rel).parents for rel in content):
            raise ValueError('Empty knowledge collection')
    content['manifest.json'] = manifest_bytes
    return collections, content


def knowledge_roots(program_root):
    root = Path(program_root) / 'knowledge-bundle'
    collections, _ = knowledge_resources(root)
    return [{'id': 'public-knowledge-' + entry['id'], 'label': entry['title'],
             'path': str(root / entry['path']), 'writable': False, 'group': '公开资料库'} for entry in collections]
