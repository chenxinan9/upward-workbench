#!/usr/bin/env python3
"""Local private backup; never include output in a release. Restore into a NEW folder."""
import argparse,json,sqlite3,zipfile,os,tempfile,shutil
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('action',choices=['backup','restore']);p.add_argument('--data-dir',required=True);p.add_argument('--archive',required=True);a=p.parse_args();data=Path(a.data_dir).expanduser().resolve();archive=Path(a.archive).expanduser().resolve();root=Path(__file__).resolve().parents[1]
for v in [data,archive]:
 if root==v or root in v.parents:raise SystemExit('私人备份和数据必须放在项目外')
if a.action=='backup':
 if archive.exists():raise SystemExit('拒绝覆盖现有备份')
 archive.parent.mkdir(parents=True,exist_ok=True)
 with tempfile.TemporaryDirectory() as tmp:
  db=Path(tmp)/'desk.sqlite'
  with sqlite3.connect(data/'desk.sqlite') as src,sqlite3.connect(db) as dst:src.backup(dst)
  with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
   z.write(db,'desk.sqlite')
   for pth in data.rglob('*'):
    if pth.is_symlink():raise SystemExit('私人目录含符号链接，需人工核对')
    if pth.is_file() and pth.name not in ['desk.sqlite','desk.sqlite-wal','desk.sqlite-shm'] and pth!=archive:z.write(pth,pth.relative_to(data))
 archive.chmod(0o600);print('私人备份已生成。跨文件一致性请在服务停止后备份。')
else:
 if data.exists():raise SystemExit('恢复目录必须尚不存在，避免覆盖现有资料')
 with zipfile.ZipFile(archive) as z:
  if sum(i.file_size for i in z.infolist())>512*1024*1024:raise SystemExit('备份超过大小限制')
  for i in z.infolist():
   target=(data/i.filename).resolve()
   if data not in target.parents or (i.external_attr>>16)&0o170000==0o120000:raise SystemExit('备份包含不安全路径')
  if 'desk.sqlite' not in z.namelist():raise SystemExit('缺少数据库')
  data.mkdir(parents=True,mode=0o700);z.extractall(data)
  for f in data.rglob('*'):f.chmod(0o700 if f.is_dir() else 0o600)
 print('已恢复到新目录。若迁移设备，重新执行 connect_local.py；旧目录的绝对路径不能直接沿用。')
