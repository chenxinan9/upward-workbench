#!/usr/bin/env python3
"""Copy explicitly selected reviewed skills; no network and no overwrite."""
import argparse, json, os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
from bundle_resources import advisor_resources, directory_fd, no_symlinks, read_resource, relative_path

def plan(destination, requested):
    entries = json.loads(read_resource(ROOT, 'manifest.json'))['entries']
    if len({e['name'] for e in entries}) != len(entries):
        raise ValueError('Duplicate manifest skill')
    by_name = {e['name']: e for e in entries}
    unknown = set(requested) - set(by_name)
    if unknown:
        raise ValueError('Unknown skills: ' + ', '.join(sorted(unknown)))
    names = requested or list(by_name)
    if len(names) != len(set(names)):
        raise ValueError('Duplicate skill selection')
    dst = no_symlinks(destination).resolve()
    if dst == ROOT or ROOT in dst.parents:
        raise ValueError('Destination must be outside this bundle')
    result = []
    for name in names:
        e = by_name[name]
        if e['path'] != name or relative_path(name).as_posix() != name or '/' in name:
            raise ValueError('Unexpected manifest path')
        src = ROOT / name
        if src.is_symlink() or src.resolve().parent != ROOT:
            raise ValueError('Unsafe source directory: ' + name)
        target = dst / name
        if target.exists() or target.is_symlink():
            raise ValueError('Existing skill is preserved; choose a different destination: ' + name)
        advisor_resources(src)
        result.append((src, target))
    return dst, result


def copy_selected(dst, pairs):
    # Snapshot checked bytes before any write. No source reread after checksum verification.
    snapshots = [(target, advisor_resources(src)) for src, target in pairs]
    base = directory_fd(dst, create=True)
    copied = []
    try:
        for target, files in snapshots:
            if target.parent != dst or '/' in target.name:
                raise ValueError('Unsafe installation target')
            os.mkdir(target.name, mode=0o700, dir_fd=base)
            package = os.open(target.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=base)
            try:
                for relative, data in files.items():
                    parts = relative_path(relative).parts
                    directory = os.dup(package)
                    try:
                        for part in parts[:-1]:
                            try: os.mkdir(part, mode=0o700, dir_fd=directory)
                            except FileExistsError: pass
                            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                            os.close(directory); directory = child
                        fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                        with os.fdopen(fd, 'wb') as stream: stream.write(data)
                    finally: os.close(directory)
            finally: os.close(package)
            copied.append(target.name)
    finally: os.close(base)
    return copied

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--destination', required=True, help='Your Codex skills directory or an isolated test directory')
    p.add_argument('--name', action='append', default=[], help='Repeat to select skills; omitted means all registered skills')
    p.add_argument('--apply', action='store_true', help='Copy files; without this flag print the plan only')
    args = p.parse_args()
    try:
        dst, pairs = plan(args.destination, args.name)
        if args.apply:
            copied = copy_selected(dst, pairs)
            print(json.dumps({'copied': copied, 'status': 'copied; host discovery must be refreshed'}, ensure_ascii=False))
        else:
            print(json.dumps({'planned': [t.name for _, t in pairs], 'writes': False}, ensure_ascii=False))
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as e:
        p.exit(1, str(e) + '\n')

if __name__ == '__main__':
    main()
