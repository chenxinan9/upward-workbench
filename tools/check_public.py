#!/usr/bin/env python3
"""Static distribution checks; findings require review, not a safety guarantee."""
import argparse, json, re, subprocess
from pathlib import Path

RULES = {
    'private-key': r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
    'provider-token': r'\b(?:sk-[A-Za-z0-9_-]{30,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b',
    'user-path': r'/(?:Users|home)/[A-Za-z0-9_.-]+/',
    'windows-user-path': r'[A-Za-z]:\\Users\\[^\\\s]+\\',
}
PRIVATE_NAMES = {'.env', 'settings.json', 'desktop.json', 'deployment.json', '.DS_Store'}
PRIVATE_PARTS = {'private', '私有资料', '工作记录', '宣传素材', '创作参考', 'runs', '__pycache__'}
PRIVATE_EXTENSIONS = {'.sqlite', '.sqlite3', '.db', '.pyc', '.mp4', '.mov', '.docx', '.pdf', '.zip'}

def check(root, paths, terms=()):
    root = Path(root).resolve()
    findings = []
    count = 0
    for rel in paths:
        rel = Path(rel)
        path = root / rel
        count += 1
        if rel.is_absolute() or '..' in rel.parts or path.is_symlink() or root not in path.resolve().parents:
            findings.append({'file': rel.as_posix(), 'rule': 'unsafe-path'}); continue
        if not path.is_file():
            findings.append({'file': rel.as_posix(), 'rule': 'missing-file'}); continue
        if rel.name in PRIVATE_NAMES or rel.name.startswith('.env.') or PRIVATE_PARTS.intersection(rel.parts) or rel.suffix.lower() in PRIVATE_EXTENSIONS:
            findings.append({'file': rel.as_posix(), 'rule': 'private-artifact'})
        data = path.read_bytes()
        if rel.as_posix() == 'desktop/assets/upward-icon.png':
            if not data.startswith(b'\x89PNG\r\n\x1a\n'):
                findings.append({'file': rel.as_posix(), 'rule': 'invalid-icon'})
            continue
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            findings.append({'file': rel.as_posix(), 'rule': 'unexpected-binary'}); continue
        for label, pattern in RULES.items():
            if re.search(pattern, text):
                findings.append({'file': rel.as_posix(), 'rule': label})
        # The optional term file lives outside the public tree. Do not echo values.
        if any(term and (term in text or term in rel.as_posix()) for term in terms):
            findings.append({'file': rel.as_posix(), 'rule': 'private-lexicon-match'})
    return {'checked_files': count, 'findings': findings, 'passed': not findings,
            'scope': 'static patterns and optional private lexicon; independent content and license review still required'}

if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', default=str(Path(__file__).resolve().parents[1]))
    p.add_argument('--tracked', action='store_true', help='Check git tracked files only (CI).')
    p.add_argument('--terms', help='Private UTF-8 JSON array outside the repository; values never printed.')
    a = p.parse_args(); root = Path(a.root).resolve()
    if a.tracked:
        raw = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z'])
        paths = [x.decode() for x in raw.split(b'\0') if x]
    else:
        paths = [x.relative_to(root).as_posix() for x in root.rglob('*') if '.git' not in x.relative_to(root).parts and (x.is_file() or x.is_symlink())]
    terms = []
    if a.terms:
        f = Path(a.terms).resolve()
        if root == f or root in f.parents:
            p.error('Private term file must be outside the public tree')
        terms = json.loads(f.read_text())
        if not isinstance(terms, list) or not all(isinstance(x, str) for x in terms):
            p.error('Expected JSON array of strings')
    result = check(root, paths, terms)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['passed'] and paths else 1)
