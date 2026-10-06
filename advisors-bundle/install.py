#!/usr/bin/env python3
"""Copy explicitly selected reviewed skills; no network and no overwrite."""
import argparse, hashlib, json
from pathlib import Path
ROOT = Path(__file__).resolve().parent

def plan(destination, requested):
    entries = json.loads((ROOT / 'manifest.json').read_text())['entries']
    by_name = {e['name']: e for e in entries}
    unknown = set(requested) - set(by_name)
    if unknown:
        raise ValueError('Unknown skills: ' + ', '.join(sorted(unknown)))
    names = requested or list(by_name)
    if len(names) != len(set(names)):
        raise ValueError('Duplicate skill selection')
    dst = Path(destination).expanduser().resolve()
    if dst == ROOT or ROOT in dst.parents:
        raise ValueError('Destination must be outside this bundle')
    result = []
    for name in names:
        e = by_name[name]
        if e['path'] != name or Path(name).name != name:
            raise ValueError('Unexpected manifest path')
        src = ROOT / name
        if src.is_symlink() or src.resolve().parent != ROOT:
            raise ValueError('Unsafe source directory: ' + name)
        target = dst / name
        if target.exists() or target.is_symlink():
            raise ValueError('Existing skill is preserved; choose a different destination: ' + name)
        paths = sorted(p.relative_to(src).as_posix() for p in src.rglob('*') if p.is_file())
        if paths != ['LICENSE', 'SKILL.md', 'provenance.json'] or any(p.is_symlink() for p in src.rglob('*')):
            raise ValueError('Unexpected files in source: ' + name)
        provenance = json.loads((src / 'provenance.json').read_text())
        if {f['path'] for f in provenance['files']} != {'SKILL.md', 'LICENSE'}:
            raise ValueError('Unexpected provenance entries: ' + name)
        for f in provenance['files']:
            b = (src / f['path']).read_bytes()
            if len(b) != f['bytes'] or hashlib.sha256(b).hexdigest() != f['sha256']:
                raise ValueError('Source checksum mismatch: ' + name)
        result.append((src, target))
    return dst, result

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--destination', required=True, help='Your Codex skills directory or an isolated test directory')
    p.add_argument('--name', action='append', default=[], help='Repeat to select skills; omitted means all 51')
    p.add_argument('--apply', action='store_true', help='Copy files; without this flag print the plan only')
    args = p.parse_args()
    try:
        dst, pairs = plan(args.destination, args.name)
        if args.apply:
            dst.mkdir(parents=True, exist_ok=True)
            copied = []
            for src, target in pairs:
                # Exclusive creation keeps an existing package intact even if it appeared after planning.
                target.mkdir(exist_ok=False)
                copied.append(target.name)
                for name in ['SKILL.md', 'LICENSE', 'provenance.json']:
                    with (target / name).open('xb') as f:
                        f.write((src / name).read_bytes())
            print(json.dumps({'copied': copied, 'status': 'copied; host discovery must be refreshed'}, ensure_ascii=False))
        else:
            print(json.dumps({'planned': [t.name for _, t in pairs], 'writes': False}, ensure_ascii=False))
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as e:
        p.exit(1, str(e) + '\n')

if __name__ == '__main__':
    main()
