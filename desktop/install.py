#!/usr/bin/env python3
"""Install a self-contained public runtime; private data stays in its own directory."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import uuid

SOURCE = Path(__file__).resolve().parents[1]
APP_ID = 'local.growth.advisor.desk'


def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _safe_destination(path):
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError('安装路径不可包含符号链接，请使用真实目录')


def _json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.upward-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def deploy_runtime(source, support, data, *, upgrade=False):
    """Testable runtime deployment, without compiling or starting an application.

    Replaces the runtime as a directory, so removed modules do not survive an
    upgrade. A private rollback snapshot preserves the previous program/config.
    """
    source = Path(source).resolve(); support = Path(support).expanduser().absolute()
    data = Path(data).expanduser().absolute()
    _safe_destination(support); _safe_destination(data)
    data = data.resolve()
    if data == source or source in data.parents:
        raise ValueError('私人数据必须位于源码目录之外')
    if data == support or support in data.parents:
        raise ValueError('私人数据必须与桌面运行程序分开')
    support.mkdir(parents=True, exist_ok=True); support.chmod(0o700)
    runtime = support / '程序'
    config_file = support / 'desktop.json'
    for path in [runtime, config_file, support/'start_service.py', support/'启动服务.command', support/'deployment.json']:
        _safe_destination(path)
    if runtime.exists() and not upgrade:
        raise ValueError('运行副本已存在；停止服务后用 --upgrade 保留备份并升级')
    if config_file.exists():
        previous = json.loads(config_file.read_text())
        if Path(previous.get('data', '')).expanduser().resolve() != data:
            raise ValueError('已有桌面入口指向另一数据目录，未自动切换')
        if Path(previous.get('source', '')).resolve() != runtime.resolve():
            raise ValueError('已有桌面运行目录不匹配，未替换')
    tools = source / 'tools'
    if str(tools) not in sys.path: sys.path.insert(0, str(tools))
    builder = _module('upward_install_release', tools/'build_release.py')
    paths = builder.release_files()
    scan = builder.check(source, paths)
    if not scan['passed']:
        raise ValueError('待安装公开文件未通过隐私与完整性检查：' + json.dumps(scan['findings'], ensure_ascii=False))
    builder.validate_dependencies(paths)
    reviewed_resources = builder.resource_files()
    if not set(reviewed_resources) <= set(paths):
        raise ValueError('资料清单在安装期间发生变化')
    old_settings = data/'settings.json'
    if old_settings.is_symlink(): raise ValueError('拒绝符号链接设置')
    settings_before = old_settings.read_bytes() if old_settings.exists() else None
    stage = Path(tempfile.mkdtemp(prefix='.程序待安装-', dir=support))
    backup = support / '安装备份' / uuid.uuid4().hex
    saved = []
    switched = False
    try:
        for rel in paths:
            origin = source/rel
            if origin.is_symlink(): raise ValueError('安装源包含符号链接')
            target = stage/rel; target.parent.mkdir(parents=True, exist_ok=True)
            if rel in reviewed_resources:
                target.write_bytes(reviewed_resources[rel])
            else:
                shutil.copy2(origin, target)
        # A compile-only check never executes application code or models.
        for relative in ['app.py','workspace_api.py','vault.py','codex_workspace.py']:
            compile((stage/relative).read_text(), relative, 'exec')
        backup.mkdir(parents=True, exist_ok=False); backup.chmod(0o700)
        for name in ['程序','desktop.json','deployment.json','start_service.py','启动服务.command']:
            target = support/name
            if target.exists():
                os.replace(target, backup/name)
                saved.append(name)
        os.replace(stage, runtime); switched = True
        setup = _module('upward_installed_setup', runtime/'tools/setup_knowledge.py')
        result = setup.setup(data, initialize_legacy=False, preserve_existing=True)
        shutil.copy2(runtime/'desktop/start_service.py', support/'start_service.py')
        launcher = support/'启动服务.command'
        launcher.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' '+shlex.quote(str(support/'start_service.py'))+'\n')
        launcher.chmod(0o700)
        _json(config_file, {'python':sys.executable, 'source':str(runtime), 'data':str(data),
                           'launcher':str(launcher), 'data_id':hashlib.sha256(str(data).encode()).hexdigest()})
        receipt = {'source':str(source), 'runtime':str(runtime), 'setup':result,
                   'files':{rel:hashlib.sha256((runtime/rel).read_bytes()).hexdigest() for rel in paths}}
        _json(support/'deployment.json', receipt)
        return {'runtime':str(runtime), 'backup':str(backup), 'files':len(paths), 'setup':result}
    except Exception:
        if switched:
            failed = backup/'未完成的新程序'
            if runtime.exists(): os.replace(runtime, failed)
            for name in ['desktop.json','deployment.json','start_service.py','启动服务.command']:
                (support/name).unlink(missing_ok=True)
        for name in reversed(saved):
            if (backup/name).exists(): os.replace(backup/name, support/name)
        if settings_before is not None:
            old_settings.parent.mkdir(parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(prefix='.settings-restore-', dir=old_settings.parent)
            with os.fdopen(fd, 'wb') as handle: handle.write(settings_before)
            os.chmod(temp, 0o600); os.replace(temp, old_settings)
        elif old_settings.exists(): old_settings.unlink()
        raise
    finally:
        if stage.exists(): shutil.rmtree(stage)


def build_app(source, destination):
    contents = destination/'Contents'
    (contents/'MacOS').mkdir(parents=True); (contents/'Resources').mkdir()
    subprocess.run(['xcrun','swiftc','-O',str(source/'desktop/GrowthDesk.swift'),'-o',str(contents/'MacOS/GrowthDesk'),'-framework','AppKit','-framework','WebKit','-framework','Speech','-framework','AVFoundation'],check=True)
    with tempfile.TemporaryDirectory() as temp:
        temp=Path(temp); png=temp/'icon.png'; icons=temp/'AppIcon.iconset'; icons.mkdir()
        designed=source/'desktop/assets/upward-icon.png'
        if designed.is_file(): shutil.copy2(designed,png)
        else: subprocess.run(['xcrun','swift',str(source/'desktop/icon.swift'),str(png)],check=True)
        for size in [16,32,128,256,512]:
            for scale in [1,2]:
                name=f'icon_{size}x{size}'+('@2x' if scale==2 else '')+'.png'
                subprocess.run(['sips','-z',str(size*scale),str(size*scale),str(png),'--out',str(icons/name)],check=True,stdout=subprocess.DEVNULL)
        subprocess.run(['iconutil','-c','icns',str(icons),'-o',str(contents/'Resources/AppIcon.icns')],check=True)
    info={'CFBundleIconFile':'AppIcon.icns','CFBundleDisplayName':'睿顾台','CFBundleName':'睿顾台',
          'CFBundleIdentifier':APP_ID,'CFBundleExecutable':'GrowthDesk','CFBundlePackageType':'APPL',
          'CFBundleShortVersionString':'0.4.7','CFBundleVersion':'12',
          'NSMicrophoneUsageDescription':'点击语音输入时使用麦克风，把你的话转为可编辑的文字。',
          'NSSpeechRecognitionUsageDescription':'将你主动录入的语音转为文字；优先使用本机识别，必要时使用 Apple 语音服务。','NSHighResolutionCapable':True,
          'NSAppTransportSecurity':{'NSAllowsLocalNetworking':True},'NSHumanReadableCopyright':'睿顾台'}
    (contents/'Info.plist').write_bytes(plistlib.dumps(info))
    subprocess.run(['codesign','--force','--deep','--sign','-',str(destination)],check=True)


def install(data, destination, *, upgrade=False, source=SOURCE):
    source=Path(source).resolve(); data=Path(data).expanduser().absolute()
    destination=Path(destination).expanduser().absolute(); _safe_destination(destination); _safe_destination(data)
    support=Path.home()/'Library/Application Support/向上成长顾问'
    previous_destination = destination
    legacy = Path.home()/'Desktop/向上工作台.app'
    if upgrade and destination == Path.home()/'Desktop/睿顾台.app' and not destination.exists() and legacy.exists():
        _safe_destination(legacy); previous_destination = legacy
    if previous_destination.exists():
        if not upgrade: raise ValueError('目标已存在，拒绝覆盖；升级请用 --upgrade')
        info=previous_destination/'Contents/Info.plist'
        if not info.is_file() or plistlib.loads(info.read_bytes()).get('CFBundleIdentifier') != APP_ID:
            raise ValueError('目标不是睿顾台或兼容的向上工作台，拒绝替换')
    try:
        with urllib.request.urlopen('http://127.0.0.1:60128/api/identity',timeout=1) as response:
            running=json.load(response)
        if running.get('application')=='growth-desk':
            raise ValueError('请先退出睿顾台（原向上）窗口并停止后台服务，再安装或升级；未改动当前运行副本')
    except (OSError, json.JSONDecodeError):
        pass
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.向上安装-',dir=destination.parent) as temp:
        candidate=Path(temp)/'睿顾台.app'
        build_app(source,candidate)
        settings_path=data/'settings.json'
        settings_before=settings_path.read_bytes() if settings_path.exists() else None
        receipt=deploy_runtime(source,support,data,upgrade=upgrade)
        backup=Path(receipt['backup']); previous_app=backup/previous_destination.name
        app_moved=False
        try:
            if previous_destination.exists():
                shutil.move(str(previous_destination),str(previous_app)); app_moved=True
            shutil.move(str(candidate),str(destination))
        except Exception:
            # Restore both halves of an upgrade if the final desktop swap fails.
            # Keep failed artifacts for diagnosis; never remove private documents.
            if app_moved:
                if destination.exists(): shutil.move(str(destination),str(backup/'未完成的新入口.app'))
                shutil.move(str(previous_app),str(previous_destination))
            elif not candidate.exists() and destination.exists():
                shutil.move(str(destination),str(backup/'未完成的新入口.app'))
            for name in ['程序','desktop.json','deployment.json','start_service.py','启动服务.command']:
                current=support/name; previous=backup/name
                if current.exists(): os.replace(current,backup/('未完成-'+name))
                if previous.exists(): os.replace(previous,current)
            if settings_before is None:
                settings_path.unlink(missing_ok=True)
            else:
                fd, restored=tempfile.mkstemp(prefix='.settings-restore-',dir=data)
                with os.fdopen(fd,'wb') as handle: handle.write(settings_before)
                os.chmod(restored,0o600); os.replace(restored,settings_path)
            raise
    return dict(receipt,application=str(destination))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',required=True)
    parser.add_argument('--destination',default=str(Path.home()/'Desktop/睿顾台.app'))
    parser.add_argument('--upgrade',action='store_true',help='停止服务后备份旧运行副本并升级，保留私人数据')
    args=parser.parse_args()
    try: print(json.dumps(install(args.data_dir,args.destination,upgrade=args.upgrade),ensure_ascii=False,indent=2))
    except (ValueError,OSError,subprocess.CalledProcessError) as error: parser.exit(1,str(error)+'\n')

if __name__=='__main__':main()
