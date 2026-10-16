#!/usr/bin/env python3
"""Initialize an explicit local Markdown workspace without scanning the computer."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def atomic_json(path, value):
    if path.is_symlink():
        raise ValueError('拒绝符号链接配置')
    fd, name = tempfile.mkstemp(prefix='.upward-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write('\n')
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _register_root(roots, path, label, preferred_id, writable=False):
    path = Path(path).expanduser()
    if path.is_symlink() or not path.is_absolute() or not path.is_dir():
        raise ValueError('登记的 Vault 必须是存在的真实绝对目录')
    path = path.resolve()
    for root in roots:
        if Path(root.get('path', '')).expanduser().resolve() == path:
            # Preserve explicit user permissions and exclusion rules.
            return root, False
    used = {r.get('id') for r in roots}
    identifier = preferred_id
    serial = 2
    while identifier in used:
        identifier = preferred_id + '-' + str(serial)
        serial += 1
    value = {'id': identifier, 'label': label, 'path': str(path), 'writable': bool(writable)}
    roots.append(value)
    return value, True


def setup(data, *, vaults=(), initialize_legacy=True, preserve_existing=False):
    """Keep the old Python-call default for the legacy notes index tests.

    The CLI and desktop installer use initialize_legacy=False: the Markdown
    workspace has no FTS dependency. Legacy indexing is an explicit option.
    """
    original_data = Path(data).expanduser()
    if original_data.is_symlink():
        raise ValueError('拒绝符号链接数据目录')
    data = original_data.resolve()
    if data == ROOT or ROOT in data.parents:
        raise ValueError('数据目录必须位于项目源码之外')
    data.mkdir(parents=True, exist_ok=True)
    data.chmod(0o700)
    settings_file = data / 'settings.json'
    if settings_file.is_symlink():
        raise ValueError('拒绝符号链接配置')
    original = settings_file.read_bytes() if settings_file.exists() else None
    settings = json.loads(original) if original is not None else {}
    if not isinstance(settings, dict):
        raise ValueError('工作台设置必须是 JSON 对象')
    config_file = data / 'advisor-config.json'
    script = ROOT / 'skills/upward-advisor/scripts/advisor.py'
    notes = data / 'knowledge'
    database = data / 'knowledge-index.sqlite'
    for path in [config_file, notes, database]:
        if path.is_symlink():
            raise ValueError('拒绝符号链接数据路径')
    existing_config = settings.get('advisor_config')
    foreign_index = bool(existing_config and Path(existing_config).expanduser().resolve() != config_file)
    if foreign_index and initialize_legacy and not preserve_existing:
        raise ValueError('已有其他检索器连接，保留原配置；请使用现有连接')
    roots = settings.get('vault_roots', [])
    if not isinstance(roots, list) or any(not isinstance(r, dict) for r in roots):
        raise ValueError('vault_roots 必须是集合列表')
    roots = [dict(r) for r in roots]
    # Validate external inputs before creating or rewriting any note.
    requested = []
    for value in vaults:
        path = Path(value).expanduser()
        if path.is_symlink() or not path.is_absolute() or not path.is_dir():
            raise ValueError('登记的 Vault 必须是存在的真实绝对目录')
        requested.append(path.resolve())
    # An upgrade should keep an established writable Vault as the user's home.
    # Do not manufacture an empty second notebook beside a shared Obsidian root.
    needs_default_notes = not (preserve_existing and any(root.get('writable') is True for root in roots))
    registered = []
    if needs_default_notes:
        notes.mkdir(exist_ok=True)
        notes.chmod(0o700)
        note_root, added = _register_root(roots, notes, '我的笔记', 'upward', True)
        if added:
            registered.append(note_root['id'])
    for index, path in enumerate(requested):
        root, changed = _register_root(roots, path, path.name, 'obsidian' if index == 0 else 'vault', False)
        if changed:
            registered.append(root['id'])
    settings['vault_roots'] = roots
    # Validate exactly the configured collections; this does not scan their contents.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from vault import VaultWorkspace
    VaultWorkspace(settings, data)
    report = None
    desired = {'database': str(database), 'collections': [{
        'id': 'growth-desk-notes', 'path': str(notes),
        'evidence': 'user_written_or_saved_conversation',
        'status': 'dated_record_not_automatically_verified_fact'}]}
    if initialize_legacy and not foreign_index:
        # Explicit legacy indexing may still need its existing notes directory.
        notes.mkdir(exist_ok=True)
        if config_file.exists() and json.loads(config_file.read_text()) != desired:
            raise ValueError('已有自定义索引配置，保留原配置，不自动重绑')
        if not config_file.exists():
            atomic_json(config_file, desired)
        result = subprocess.run([sys.executable, str(script), '--config', str(config_file), 'build'], capture_output=True, text=True)
        try:
            report = json.loads(result.stdout)
        except ValueError:
            raise ValueError('索引器未返回有效结果；检查 Python 与 SQLite FTS5 环境') from None
        if result.returncode or report.get('errors'):
            raise ValueError('旧版索引未建立，设置未修改；新知识工作区不要求 FTS5')
        settings.update(advisor_config=str(config_file), advisor_script=str(script))
    elif existing_config and not foreign_index:
        # A desktop upgrade must not retain a script path into a deleted ZIP extraction.
        settings['advisor_script'] = str(script)
    if original is not None and json.loads(original) != settings:
        backup = data / ('settings.before-workspace-' + hashlib.sha256(original).hexdigest()[:12] + '.json')
        if backup.exists() and (backup.is_symlink() or backup.read_bytes() != original):
            raise ValueError('设置备份同名但内容不同，未覆盖')
        if not backup.exists():
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as handle:
                handle.write(original)
    if (settings_file.read_bytes() if settings_file.exists() else None) != original:
        raise ValueError('设置在初始化期间发生变化，未覆盖')
    if original is None or json.loads(original) != settings:
        atomic_json(settings_file, settings)
    welcome = notes / '开始使用.md'
    if needs_default_notes and not welcome.exists():
        text = ('# 睿顾台 · 我的知识库\n\n左边阅读和整理内容，右边与 Codex 持续讨论。\n\n'
                '可以从一条近期想法、一个正在做的项目或一个具体问题开始。'
                '顾问团提供方法参考，个人目标与决定以你自己的记录为准。\n\n'
                '本目录是普通 Markdown 文件夹，可以在 Obsidian 中作为 Vault 打开。'
                '保存后可直接搜索；外部修改会在睿顾台重新读取，不需要再点“入库”。\n')
        fd = os.open(welcome, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(text)
    manifest = ROOT / 'advisors-bundle/manifest.json'
    entries = json.loads(manifest.read_text()).get('entries', [])
    bundled = sum((ROOT / 'advisors-bundle' / entry['path'] / 'SKILL.md').is_file() for entry in entries)
    return {'connected': True, 'scope': 'explicit-vault-roots', 'registered_roots': registered,
            'roots': len(roots), 'bundled_skills_available': bundled,
            'index': report, 'legacy_index_preserved': foreign_index,
            'restart_required': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--vault', action='append', default=[], help='显式登记现有 Vault；默认只读，可重复')
    parser.add_argument('--legacy-index', action='store_true', help='同时初始化旧版 FTS5 笔记索引')
    args = parser.parse_args()
    try:
        print(json.dumps(setup(args.data_dir, vaults=args.vault, initialize_legacy=args.legacy_index,
                               preserve_existing=True), ensure_ascii=False, indent=2))
    except (ValueError, OSError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
