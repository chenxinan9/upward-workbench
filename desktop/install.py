#!/usr/bin/env python3
"""Build a native macOS window; local paths live in private user config, not source."""
import argparse,hashlib,json,os,plistlib,shlex,shutil,subprocess,sys,tempfile
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--data-dir',required=True);p.add_argument('--destination',default=str(Path.home()/'Desktop/向上工作台.app'));a=p.parse_args()
source=Path(__file__).resolve().parents[1];dest=Path(a.destination).expanduser().resolve();data=Path(a.data_dir).expanduser().resolve()
if dest.exists():raise SystemExit('目标已存在，拒绝覆盖。')
if source==data or source in data.parents:raise SystemExit('私人数据必须位于源码目录之外。')
contents=dest/'Contents';(contents/'MacOS').mkdir(parents=True);(contents/'Resources').mkdir()
subprocess.run(['xcrun','swiftc','-O',str(source/'desktop/GrowthDesk.swift'),'-o',str(contents/'MacOS/GrowthDesk'),'-framework','AppKit','-framework','WebKit'],check=True)
with tempfile.TemporaryDirectory() as temp:
 temp=Path(temp);png=temp/'icon.png';icons=temp/'AppIcon.iconset';icons.mkdir()
 designed=source/'desktop/assets/upward-icon.png'
 if designed.is_file():shutil.copy2(designed,png)
 else:subprocess.run(['xcrun','swift',str(source/'desktop/icon.swift'),str(png)],check=True)
 for size in [16,32,128,256,512]:
  for scale in [1,2]:
   name=f'icon_{size}x{size}'+('@2x' if scale==2 else '')+'.png'
   subprocess.run(['sips','-z',str(size*scale),str(size*scale),str(png),'--out',str(icons/name)],check=True,stdout=subprocess.DEVNULL)
 subprocess.run(['iconutil','-c','icns',str(icons),'-o',str(contents/'Resources/AppIcon.icns')],check=True)
info={'CFBundleIconFile':'AppIcon.icns','CFBundleDisplayName':'向上工作台','CFBundleName':'向上工作台','CFBundleIdentifier':'local.growth.advisor.desk','CFBundleExecutable':'GrowthDesk','CFBundlePackageType':'APPL','CFBundleShortVersionString':'0.2.1','CFBundleVersion':'4','NSHighResolutionCapable':True,'NSAppTransportSecurity':{'NSAllowsLocalNetworking':True},'NSHumanReadableCopyright':'个人成长顾问工作台'}
(contents/'Info.plist').write_bytes(plistlib.dumps(info))
config=Path.home()/'Library/Application Support/向上成长顾问';config.mkdir(parents=True,exist_ok=True);config.chmod(0o700)
runtime=config/'程序';runtime.mkdir(exist_ok=True)
for name in ['app.py','growth.py','methods.json','quotes.json','public-advisors.json']:shutil.copy2(source/name,runtime/name)
shutil.copytree(source/'web',runtime/'web',dirs_exist_ok=True)
shutil.copytree(source/'advisors-bundle',runtime/'advisors-bundle',dirs_exist_ok=True)
shutil.copy2(source/'desktop/start_service.py',config/'start_service.py')
launcher=config/'启动服务.command';launcher.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' '+shlex.quote(str(config/'start_service.py'))+'\n');launcher.chmod(0o700)
f=config/'desktop.json';f.write_text(json.dumps({'python':sys.executable,'source':str(runtime),'data':str(data),'launcher':str(launcher),'data_id':hashlib.sha256(str(data).encode()).hexdigest()},ensure_ascii=False,indent=2));f.chmod(0o600)
receipt={'source':str(source),'files':{str(f.relative_to(runtime)):hashlib.sha256(f.read_bytes()).hexdigest() for f in runtime.rglob('*') if f.is_file() and '__pycache__' not in str(f)}}
(config/'deployment.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2));(config/'deployment.json').chmod(0o600)
subprocess.run(['codesign','--force','--deep','--sign','-',str(dest)],check=True)
print(dest)
