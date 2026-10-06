#!/usr/bin/env python3
"""Copy a synthetic plan into a new external data directory; never load user data."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path


EXAMPLES = Path(__file__).resolve().parent
PROJECT = EXAMPLES.parent


def initialize(destination: Path) -> dict:
    destination = destination.expanduser().resolve()
    if destination == PROJECT or PROJECT in destination.parents:
        raise ValueError('示例数据目录须在源码目录之外')
    if destination.exists():
        raise ValueError('目标目录已存在；请选择新的目录，不覆盖现有设置或记录')
    plan = json.loads((EXAMPLES / 'growth-plan.sample.json').read_text())
    if plan.get('synthetic') is not True:
        raise ValueError('示例缺少 synthetic 标识')
    for source in plan['source_register']:
        file = (EXAMPLES / source['path']).resolve()
        if EXAMPLES not in file.parents or not file.is_file() or file.is_symlink():
            raise ValueError('示例来源路径异常')
        if hashlib.sha256(file.read_bytes()).hexdigest() != source['sha256']:
            raise ValueError('示例来源已修改，请先同步登记哈希')
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    shutil.copytree(EXAMPLES / 'sources', destination / 'sources')
    shutil.copy2(EXAMPLES / 'growth-plan.sample.json', destination / 'growth-plan.json')
    settings = {
        'growth_plan': str(destination / 'growth-plan.json'),
        'private_root': str(destination),
    }
    settings_path = destination / 'settings.json'
    with settings_path.open('x', encoding='utf-8') as file:
        json.dump(settings, file, ensure_ascii=False, indent=2)
        file.write('\n')
    settings_path.chmod(0o600)
    return {
        'data_dir': str(destination),
        'synthetic': True,
        'plan': 'growth-plan.json',
        'sources': len(plan['source_register']),
        'domains': len(plan['domains']),
        'goals_created': 0,
        'network_calls': 0,
        'next': '用此目录运行 tools/setup_knowledge.py，然后启动 app.py。',
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', required=True, type=Path)
    args = parser.parse_args()
    try:
        result = initialize(args.data_dir)
    except (ValueError, OSError) as error:
        parser.exit(1, str(error) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
