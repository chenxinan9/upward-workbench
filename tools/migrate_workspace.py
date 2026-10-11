#!/usr/bin/env python3
"""Copy selected legacy records into readable Markdown. Never update existing files.

SQLite is opened read-only. No model, network, prompt/run/job export or deletion.
"""
from __future__ import annotations
import argparse
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from urllib.parse import quote

FOLDERS = {'note':'想法与笔记','goal':'目标与行动','learning':'顾问学习','planning':'人生方案'}


def sha(value): return hashlib.sha256(value).hexdigest()
def scalar(value): return json.dumps(value,ensure_ascii=False,sort_keys=True)
def slug(value):
    return re.sub(r'[^\w\u4e00-\u9fff-]+','-',str(value),flags=re.UNICODE).strip('-_')[:48] or '未命名'
def safe_id(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',value):
        raise ValueError('来源标识格式错误')
    return value

def frontmatter(meta):
    return '---\n'+'\n'.join(k+': '+scalar(v) for k,v in meta.items())+'\n---\n\n'

def _plain(value):
    if value is None:return ''
    return value if isinstance(value,str) else scalar(value)

def _metadata(kind, ident, data, rev):
    fields={'migration_source':'legacy-workbench','legacy_kind':kind,'legacy_id':ident,'legacy_revision':rev,
            'title':data.get('title') or ('顾问学习 · '+str(data.get('advisor','未命名'))),
            'created_at':data.get('created_at',''),'updated_at':data.get('updated_at','')}
    for key in ('kind','status','archived','goal_id','origin_id','domain_id','parent_id','source','file_id','advisor'):
        if key in data:fields['legacy_'+key]=data[key]
    snapshot=data.get('source_snapshot')
    if isinstance(snapshot,dict):
        fields['source_evidence']={k:snapshot[k] for k in ('source','source_id','source_time','source_quote','explicit','adopted') if k in snapshot}
    return fields


def render(kind, ident, data, rev):
    meta=_metadata(kind,ident,data,rev)
    meta['migration_format']=2
    # Original state and adoption evidence stay available in document properties.
    labels={'active':'进行中','paused':'暂停','draft':'候选，尚未采纳','done':'已完成',
            'completed':'已完成','archived':'已归档','reading':'阅读中','planned':'计划中'}
    def status_label(value):return labels.get(str(value),str(value))
    lines=['# '+str(meta['title']),'']
    if kind=='note':
        lines.extend([_plain(data.get('body','')),''])
        if data.get('review_period'):lines+=['复盘期间：'+scalar(data['review_period']),'']
    elif kind=='goal':
        status=data.get('status','未记录')
        steps=data.get('steps',[])
        if not isinstance(steps,list) or any(not isinstance(step,dict) for step in steps):
            raise ValueError('目标行动格式错误')
        meta['legacy_steps']=steps
        meta['legacy_goal_details']={k:data[k] for k in ('why','domain','due','priority','evidence') if k in data}
        lines+=['状态：**'+status_label(status)+'**','']
        if data.get('why'):lines+=['## 为什么做','',_plain(data['why']),'']
        for key,label in [('domain','领域'),('due','截止日期'),('priority','优先级')]:
            if data.get(key) not in ('',None):lines+=[label+'：'+_plain(data[key])]
        lines+=['','## 行动','']
        for step in steps:
            state=step.get('status','未记录');done=step.get('done') is True
            checked=done and state!='draft' and status!='draft'
            suffix='（候选，尚未采纳）' if state=='draft' or status=='draft' else ''
            lines+=['- ['+('x' if checked else ' ')+'] '+_plain(step.get('text',''))+suffix]
            for key,label in [('due','日期'),('criterion','完成标准'),('outcome','实际结果')]:
                if step.get(key):lines+=['  - '+label+'：'+_plain(step[key])]
        if not steps:lines+=['还没有记录行动。可以在右侧与 Codex 讨论下一步。']
        if data.get('evidence'):lines+=['','## 结果与证据','',_plain(data['evidence']),'']
    elif kind=='learning':
        meta['legacy_learning']={k:data[k] for k in ('position','read_at','insight') if k in data}
        lines+=['顾问：'+_plain(data.get('advisor','')),'状态：'+status_label(data.get('status','未记录'))]
        for key,label in [('position','阅读进度'),('read_at','阅读日期')]:
            if data.get(key) not in ('',None):lines+=[label+'：'+_plain(data[key])]
        lines+=['','## 学习心得与实践','',_plain(data.get('insight','')),'']
    else:raise ValueError('不支持的迁移类型')
    return frontmatter(meta)+'\n'.join(lines)


def _reject_links(path):
    for part in [path]+list(path.parents):
        if part.is_symlink():raise ValueError('迁移路径不能包含符号链接')


def _existing_path(target, folder, identifier, title):
    directory=target/folder
    _reject_links(directory)
    candidates=list(directory.glob('*--'+identifier+'.md')) if directory.is_dir() else []
    if len(candidates)>1:raise ValueError('同一来源标识存在多个迁移文件，未选择覆盖')
    return candidates[0] if candidates else directory/(slug(title)+'--'+identifier+'.md')


@contextmanager
def _parent_fd(path, create=False):
    """Anchor each path component so a symlink swap cannot redirect migration."""
    flags=os.O_RDONLY|os.O_DIRECTORY|getattr(os,'O_NOFOLLOW',0)
    directory=os.open(path.anchor,flags)
    try:
        for part in path.parent.parts[1:]:
            try:child=os.open(part,flags,dir_fd=directory)
            except FileNotFoundError:
                if not create:raise
                try:os.mkdir(part,0o700,dir_fd=directory)
                except FileExistsError:pass
                child=os.open(part,flags,dir_fd=directory)
            os.close(directory);directory=child
        yield directory
    finally:os.close(directory)


def _matches_at(directory,name,raw):
    try:fd=os.open(name,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0),dir_fd=directory)
    except FileNotFoundError:return None
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size!=len(raw):return False
        return stream.read(len(raw)+1)==raw


def _write_new(target,path,raw,dry_run):
    _reject_links(path)
    if target not in path.parents:raise ValueError('目标文件越界')
    try:
        with _parent_fd(path,create=not dry_run) as directory:
            same=_matches_at(directory,path.name,raw)
            if same is not None:return 'skipped' if same else 'conflicts'
            if dry_run:return 'created'
            try:
                fd=os.open(path.name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600,dir_fd=directory)
            except FileExistsError:
                return 'skipped' if _matches_at(directory,path.name,raw) else 'conflicts'
            with os.fdopen(fd,'wb') as stream:
                stream.write(raw);stream.flush();os.fsync(stream.fileno())
            if _matches_at(directory,path.name,raw) is not True:raise OSError('迁移文件回读不一致')
            return 'created'
    except FileNotFoundError:
        if dry_run:return 'created'
        raise


def migrate(data_dir,target_dir,dry_run=False,*,include_sessions=False):
    source=Path(data_dir).expanduser().absolute();target=Path(target_dir).expanduser().absolute()
    _reject_links(source);_reject_links(target)
    source=source.resolve();target=target.resolve()
    if not source.is_dir():raise ValueError('旧数据目录不存在')
    if target==source or source/'planning'==target or source/'planning' in target.parents:
        raise ValueError('Vault 目录必须与旧数据目录及旧方案目录分开')
    receipt={'dry_run':bool(dry_run),'created':[],'skipped':[],'conflicts':[],'errors':[],
             'source':'legacy-workbench','excluded':['job','runs','model prompts','raw responses','full session messages']}
    links={name:[] for name in FOLDERS.values()}
    def save(path,text,source_key,title,folder=None):
        raw=text.encode('utf-8')
        status=_write_new(target,path,raw,dry_run)
        receipt[status].append({'source_id':source_key,'path':str(path.relative_to(target)),
                                'expected_sha256':sha(raw),'title':title})
        if folder and status!='conflicts':links[folder].append((title,path.relative_to(target).as_posix()))
        return status
    db=source/'desk.sqlite'
    if db.exists():
        _reject_links(db)
        with closing(sqlite3.connect(db.as_uri()+'?mode=ro',uri=True)) as conn:
            conn.execute('BEGIN')
            rows=conn.execute("SELECT id,kind,data,rev FROM objects WHERE kind IN ('note','goal','learning') ORDER BY kind,id").fetchall()
            for ident,kind,raw,rev in rows:
                try:
                    ident=safe_id(ident);data=json.loads(raw)
                    if not isinstance(data,dict):raise ValueError('记录正文不是对象')
                    title=data.get('title') or ('顾问学习 · '+str(data.get('advisor','未命名')))
                    path=_existing_path(target,FOLDERS[kind],ident,title)
                    save(path,render(kind,ident,data,rev),kind+':'+ident,title,FOLDERS[kind])
                except (ValueError,OSError,TypeError) as error:
                    receipt['errors'].append({'source_id':str(kind)+':'+str(ident),'error':str(error)[:300]})
            if include_sessions:
                # Never SELECT message.data or any job payload. Only counts/timestamps are exported.
                sessions=conn.execute("SELECT id,data FROM objects WHERE kind='session' ORDER BY id").fetchall()
                lines=['# 旧会话索引','','这里只登记旧会话标题、日期和消息数，没有迁移消息正文。','']
                for sid,raw in sessions:
                    data=json.loads(raw);count=conn.execute("SELECT count(*) FROM objects WHERE kind='message' AND json_extract(data,'$.session_id')=?",(sid,)).fetchone()[0]
                    lines+=['- '+str(data.get('title','未命名会话'))+'；日期 '+str(data.get('created_at','未记录'))+'；消息数 '+str(count)+'；原会话标识 `'+str(sid)+'`']
                save(target/'旧会话索引.md','\n'.join(lines)+'\n','sessions:index','旧会话索引')
    planning=source/'planning'
    if planning.exists():
        _reject_links(planning)
        for path in sorted(planning.rglob('*.md')):
            relative=path.relative_to(planning).as_posix()
            try:
                _reject_links(path)
                if not path.is_file() or path.stat().st_size>8_000_000:raise ValueError('方案不是可读取的 Markdown 或超过 8 MB')
                raw=path.read_bytes();text=raw.decode('utf-8-sig')
                ident=sha(relative.encode())[:24]
                title=next((line[2:].strip() for line in text.splitlines() if line.startswith('# ')),path.stem)
                destination=_existing_path(target,FOLDERS['planning'],ident,title)
                wrapper=frontmatter({'migration_source':'legacy-planning','legacy_path':relative,'source_sha256':sha(raw),'adoption':'unchanged; migration does not adopt proposals'})
                save(destination,wrapper+text,'planning:'+relative,title,FOLDERS['planning'])
            except (ValueError,OSError,UnicodeError) as error:receipt['errors'].append({'source_id':'planning:'+relative,'error':str(error)[:300]})
    # Home contains only successful paths, no private execution or model logs.
    home=['# 向上 · 我的知识库','','从一个真实问题开始。打开下面的目标或笔记，在右侧与 Codex 一起推敲。',
          '想保存讨论结果，可以直接说：把这次讨论整理到当前笔记。','',
          '左侧顾问团收录顾问方法与参考资料；专题资料保留人生指南与历史经验。原来的候选目标与已采纳计划保持各自状态。','']
    for folder,entries in links.items():
        if not entries:continue
        home+=['## '+folder,'']
        for title,relative in sorted(entries):
            label=str(title).replace('[','（').replace(']','）').replace('\n',' ')
            home+=['- ['+label+']('+quote(relative,safe='/')+')']
        if not entries:home+=['暂无本次可链接资料；已有文件如有冲突会保留，请查迁移回执。']
        home+=['']
    save(target/'首页.md','\n'.join(home)+'\n','migration:home','向上 · 我的知识库')
    receipt['counts']={key:len(receipt[key]) for key in ('created','skipped','conflicts','errors')}
    return receipt


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',required=True);parser.add_argument('--target-dir',required=True)
    parser.add_argument('--dry-run',action='store_true');parser.add_argument('--include-sessions',action='store_true')
    args=parser.parse_args()
    result=migrate(args.data_dir,args.target_dir,args.dry_run,include_sessions=args.include_sessions)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 1 if result['errors'] or result['conflicts'] else 0

if __name__=='__main__':raise SystemExit(main())
