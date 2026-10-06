#!/usr/bin/env python3
"""Opt-in local connection. No download, model call or upload."""
import argparse,json,os,shutil,sys,datetime,subprocess
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--data-dir',required=True);p.add_argument('--advisor-config',required=True);p.add_argument('--advisor-script',required=True);p.add_argument('--catalog',required=True);a=p.parse_args()
root=Path(__file__).resolve().parents[1];data=Path(a.data_dir).expanduser().resolve()
if root==data or root in data.parents:raise SystemExit('数据必须放在项目目录外')
config=Path(a.advisor_config).expanduser().resolve();script=Path(a.advisor_script).expanduser().resolve();catalog=Path(a.catalog).expanduser().resolve()
for f in [config,script,catalog]:
 if not f.is_file():raise SystemExit('需要存在的本地文件：'+f.name)
c=json.loads(config.read_text());data.mkdir(parents=True,exist_ok=True);data.chmod(0o700);(data/'knowledge').mkdir(exist_ok=True)
# Stable collection id, explicit rebind refusal prevents replacing another user's store.
entry={'id':'growth-desk-notes','path':str(data/'knowledge'),'evidence':'user_written_or_saved_conversation','status':'dated_record_not_automatically_verified_fact'}
old=next((x for x in c['collections'] if x['id']==entry['id']),None)
if old and Path(old['path']).resolve()!=data/'knowledge':raise SystemExit('已有工作台集合指向另一目录；先核对配置，不自动覆盖')
backup=data/('advisor-config-before-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S')+'.json');shutil.copy2(config,backup);backup.chmod(0o600)
if not old:c['collections'].append(entry)
temp=config.with_suffix('.growth-tmp');temp.write_text(json.dumps(c,ensure_ascii=False,indent=2));temp.chmod(0o600);temp.replace(config)
settings=data/'settings.json';s=json.loads(settings.read_text()) if settings.exists() else {};s.update(advisor_config=str(config),advisor_script=str(script),catalog=str(catalog));settings.write_text(json.dumps(s,ensure_ascii=False,indent=2));settings.chmod(0o600)
r=subprocess.run([sys.executable,str(script),'--config',str(config),'build'],capture_output=True,text=True)
(data/'connection-index-receipt.json').write_text(r.stdout);(data/'connection-index-receipt.json').chmod(0o600)
result=json.loads(r.stdout)
if r.returncode or result.get('errors'):raise SystemExit('连接已配置，索引未成功；请核对本机回执，原索引由检索器保留')
print('本地目录已连接并建立索引；未调用模型或上传资料。')
