#!/usr/bin/env bash
# One-command browser demo. The only downloaded program is the checksum-pinned RC2.
set -euo pipefail
if ! command -v python3 >/dev/null 2>&1; then
  echo '需要 Python 3.11+。请先安装 Python，再运行此命令；安装器不会使用 sudo。' >&2
  exit 1
fi
python3 - "$@" <<'UPWARD_PYTHON'
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import webbrowser
import zipfile

VERSION = 'v0.2.1-rc2'
ARCHIVE_URL = 'https://github.com/chenxinan9/upward-workbench/releases/download/v0.2.1-rc2/upward-workbench-v0.2.1-rc2.zip'
ARCHIVE_SHA256 = 'd7feb1bd16dac189fbd897b0f3b97d56813b94423834e6b969ab93daa5cb9eae'
MAX_DOWNLOAD = 10 * 1024 * 1024


def environment_check():
    if sys.version_info < (3, 11):
        raise ValueError('需要 Python 3.11+；本次未安装任何内容。')
    if sys.platform not in ('darwin', 'linux'):
        raise ValueError('一键体验支持 macOS / Linux；其他系统请按 README 手动启动。')
    db = sqlite3.connect(':memory:')
    try:
        db.execute("CREATE VIRTUAL TABLE probe USING fts5(body, tokenize='trigram')")
    except sqlite3.Error as exc:
        raise ValueError('当前 Python 的 SQLite 缺少 FTS5 trigram，请换用支持它的 Python 3.11+。') from exc
    finally:
        db.close()


def new_paths(install, data):
    raw = [Path(install).expanduser(), Path(data).expanduser()]
    for path in raw:
        if path.exists() or path.is_symlink():
            raise ValueError(f'保留已有目录，不覆盖：{path}。请选择新的 --install-dir 和 --data-dir；已有服务可使用上次输出的地址。')
    install, data = [p.resolve() for p in raw]
    if install == data or install in data.parents or data in install.parents:
        raise ValueError('程序目录和资料目录必须互相独立，不能包含对方。')
    return install, data


def download_verified(target):
    request = urllib.request.Request(ARCHIVE_URL, headers={'User-Agent': 'Upward-Workbench-Quickstart/1.0'})
    digest = hashlib.sha256()
    size = 0
    with urllib.request.urlopen(request, timeout=45) as response, target.open('xb') as output:
        if not response.url.startswith('https://'):
            raise ValueError('拒绝非 HTTPS 下载。')
        while chunk := response.read(65536):
            size += len(chunk)
            if size > MAX_DOWNLOAD:
                raise ValueError('下载超过候选包大小限制，已停止。')
            digest.update(chunk)
            output.write(chunk)
    if digest.hexdigest() != ARCHIVE_SHA256:
        raise ValueError('下载校验失败，拒绝解包或运行。')
    return size


def extract_verified(archive, staging):
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        if len(names) != len(set(names)) or sum(i.file_size for i in bundle.infolist()) > 30 * 1024 * 1024:
            raise ValueError('候选包结构异常。')
        for info in bundle.infolist():
            path = PurePosixPath(info.filename)
            if path.is_absolute() or '..' in path.parts or '\\' in info.filename or not path.parts or path.parts[0] != 'growth-desk' or stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError('拒绝候选包中的异常路径。')
        bundle.extractall(staging)
    source = staging / 'growth-desk'
    manifest = json.loads((source / 'release-manifest.json').read_text())
    listed = set()
    for item in manifest['files']:
        rel = PurePosixPath(item['path'])
        if rel.is_absolute() or '..' in rel.parts or item['path'] in listed:
            raise ValueError('文件清单路径异常。')
        listed.add(item['path'])
        content = (source / item['path']).read_bytes()
        if len(content) != item['bytes'] or hashlib.sha256(content).hexdigest() != item['sha256']:
            raise ValueError('文件清单校验失败。')
    actual = {p.relative_to(source).as_posix() for p in source.rglob('*') if p.is_file()}
    if actual != listed | {'release-manifest.json'}:
        raise ValueError('候选包存在未登记文件。')
    return source


def run_setup(install, data):
    for script in ['examples/init_demo.py', 'tools/setup_knowledge.py']:
        result = subprocess.run([sys.executable, str(install / script), '--data-dir', str(data)],
                                capture_output=True, text=True, timeout=90)
        if result.returncode:
            raise ValueError(f'{script} 未完成：{result.stderr.strip() or result.stdout.strip()}。保留本次文件供检查，未启动服务。')
    return json.loads((data / 'settings.json').read_text())


def start_service(install, data, port, open_browser):
    log_path = data / 'quickstart-service.log'
    command = [sys.executable, str(install / 'app.py'), '--data-dir', str(data), '--port', str(port)]
    with log_path.open('x') as log:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   cwd=install, start_new_session=True)
    log_path.chmod(0o600)
    expected = hashlib.sha256(str(data).encode()).hexdigest()
    url = None
    try:
        for _ in range(150):
            if process.poll() is not None:
                raise ValueError(f'服务未启动，请查看 {log_path}。本次资料已保留；原有服务未被停止。')
            match = re.search(r'http://127\.0\.0\.1:(\d+)', log_path.read_text())
            if match:
                candidate = match.group(0)
                try:
                    with urllib.request.urlopen(candidate + '/api/identity', timeout=1) as response:
                        identity = json.load(response)
                    if identity.get('application') != 'growth-desk' or identity.get('data_id') != expected:
                        raise ValueError('端口身份不匹配，未打开浏览器。')
                    url = candidate
                    break
                except (OSError, json.JSONDecodeError):
                    pass
            time.sleep(0.1)
        if not url:
            raise ValueError(f'服务就绪超时，请查看 {log_path}。')
    except BaseException:
        process.terminate()
        process.wait(timeout=5)
        raise
    receipt = {'version': VERSION, 'archive_sha256': ARCHIVE_SHA256, 'url': url,
               'pid': process.pid, 'install_dir': str(install), 'data_dir': str(data),
               'scope': 'synthetic browser demo; no global skills installed; no model call'}
    receipt_path = data / 'quickstart.json'
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
    receipt_path.chmod(0o600)
    print(f'\n向上工作台已启动：{url}', flush=True)
    print('内含 51 份顾问缩编书房；摄影与英语资料均为合成示例。未安装全局技能，也未调用 AI 模型。')
    print(f'程序：{install}\n资料：{data}\n启动记录：{receipt_path}')
    print('关闭网页不会停止服务。停止本次服务：kill ' + str(process.pid))
    print('停止后再次启动：' + shlex.join(command[:-1] + [url.rsplit(':', 1)[1]]))
    if open_browser:
        try:
            opened = webbrowser.open(url)
        except Exception:
            opened = False
        if not opened:
            print('未能自动打开浏览器，请手动打开上方地址。')
    return receipt


def main(argv=None):
    root = Path.home() / '.local/share/upward-workbench'
    parser = argparse.ArgumentParser(description='校验并安装 RC2 的浏览器演示，不覆盖已有程序或数据，不安装全局顾问技能。')
    parser.add_argument('--install-dir', default=os.environ.get('UPWARD_INSTALL_DIR', str(root / ('app-' + VERSION))))
    parser.add_argument('--data-dir', default=os.environ.get('UPWARD_DATA_DIR', str(root / 'demo-data')))
    parser.add_argument('--port', type=int, default=int(os.environ.get('UPWARD_PORT', '0')), help='0 自动选择空闲端口')
    parser.add_argument('--no-browser', action='store_true', default=os.environ.get('UPWARD_NO_BROWSER') == '1')
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error('端口范围应为 0..65535')
    environment_check()
    install, data = new_paths(args.install_dir, args.data_dir)
    print('下载已审计 RC2 并校验 SHA-256；不会修改现有资料。', flush=True)
    with tempfile.TemporaryDirectory(prefix='upward-download-') as temporary:
        staging = Path(temporary)
        archive = staging / 'release.zip'
        download_verified(archive)
        source = extract_verified(archive, staging)
        install.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, install)  # exclusive: an existing target is never merged
        install.chmod(0o700)
    run_setup(install, data)
    return start_service(install, data, args.port, not args.no_browser)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
        print('安装未完成：' + str(error), file=sys.stderr)
        sys.exit(1)
UPWARD_PYTHON
