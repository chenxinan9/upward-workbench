#!/usr/bin/env python3
"""Connect only this workbench's notes to the bundled local indexer."""
import argparse, json, os, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def atomic_json(path, value):
    fd, name = tempfile.mkstemp(prefix='.upward-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write('\n')
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)

def setup(data):
    data = Path(data).expanduser().resolve()
    if data == ROOT or ROOT in data.parents:
        raise ValueError('数据目录必须位于项目源码之外')
    data.mkdir(parents=True, exist_ok=True)
    data.chmod(0o700)
    settings_file = data / 'settings.json'
    if settings_file.is_symlink():
        raise ValueError('拒绝符号链接配置')
    settings = json.loads(settings_file.read_text()) if settings_file.exists() else {}
    config_file = data / 'advisor-config.json'
    script = ROOT / 'skills/upward-advisor/scripts/advisor.py'
    notes = data / 'knowledge'
    database = data / 'knowledge-index.sqlite'
    for path in [config_file, notes, database]:
        if path.is_symlink():
            raise ValueError('拒绝符号链接数据路径')
    desired = {'database': str(database), 'collections': [{
        'id': 'growth-desk-notes', 'path': str(notes),
        'evidence': 'user_written_or_saved_conversation',
        'status': 'dated_record_not_automatically_verified_fact'}]}
    if settings.get('advisor_config') and Path(settings['advisor_config']).expanduser().resolve() != config_file:
        raise ValueError('已有其他检索器连接，保留原配置；请使用现有连接')
    if config_file.exists() and json.loads(config_file.read_text()) != desired:
        raise ValueError('已有自定义索引配置，保留原配置，不自动重绑')
    notes.mkdir(exist_ok=True)
    notes.chmod(0o700)
    if not config_file.exists():
        atomic_json(config_file, desired)
    result = subprocess.run([sys.executable, str(script), '--config', str(config_file), 'build'], capture_output=True, text=True)
    try:
        report = json.loads(result.stdout)
    except ValueError:
        raise ValueError('索引器未返回有效结果；检查 Python 与 SQLite FTS5 环境') from None
    if result.returncode or report.get('errors'):
        raise ValueError('索引未建立，连接未修改；检查本机知识目录与 SQLite FTS5 支持')
    settings.update(advisor_config=str(config_file), advisor_script=str(script))
    if settings_file.exists() and json.loads(settings_file.read_text()) != settings:
        backup = data / 'settings.before-knowledge.json'
        if backup.exists():
            raise ValueError('原配置备份已存在，请先核对；不覆盖备份')
        atomic_json(backup, json.loads(settings_file.read_text()))
    atomic_json(settings_file, settings)
    return {'connected': True, 'scope': 'growth-desk-notes only', 'index': report, 'restart_required': True}

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(setup(args.data_dir), ensure_ascii=False, indent=2))
    except (ValueError, OSError) as error:
        parser.exit(1, str(error) + '\n')
