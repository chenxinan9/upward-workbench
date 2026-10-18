"""Unified growth views over shared Markdown and dated local records."""
import datetime as dt
import json
import re
from pathlib import Path
from urllib.parse import quote
from phase import TZ, local_date, moment_key

PURPOSES = [
 {'id':'daily','label':'安排今天'}, {'id':'panorama','label':'人生方向'},
 {'id':'goals','label':'目标与计划'}, {'id':'ideas','label':'推敲想法'},
 {'id':'review','label':'阶段复盘'}, {'id':'learn','label':'顾问学习'},
 {'id':'general','label':'成长讨论'}, {'id':'local','label':'本机协作'}]

def metadata(text):
    match=re.match(r'\A---\s*\n(.*?)\n---\s*(?:\n|$)',text,re.S)
    if not match:return {},text
    meta={}
    for line in match[1].splitlines():
        if ':' not in line:continue
        key,value=line.split(':',1)
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',key):continue
        try:meta[key]=json.loads(value.strip())
        except ValueError:meta[key]=value.strip().strip('"\'')
    return meta,text[match.end():]

def goal_body(body, base):
    result=dict(base)
    result['due']=''
    labels={'进行中':'active','已采用':'active','候选':'draft','尚未采纳':'draft','暂停':'paused','已完成':'done','已归档':'archived'}
    found=re.search(r'(?m)^状态[：:]\s*\**([^\n*]+)',body)
    if found:
        value=found[1].strip()
        status=next((k for k in ('draft','active','paused','done','archived') if value==k),None)
        if status is None:status=next((v for k,v in labels.items() if k in value),None)
        if status:result['status']=status
    found=re.search(r'(?m)^截止日期[：:]\s*(\d{4}-\d{2}-\d{2})',body)
    if found:result['due']=found[1]
    steps=[]
    for line in body.splitlines():
        item=re.match(r'^[-*] \[([ xX])\] (.+)',line)
        if item:
            text=re.sub(r'\s*[（(]原行动.*$','',item[2])
            steps.append(dict(text=text,done=item[1].lower()=='x',status='draft' if re.search(r'候选|尚未采纳|未采用',text) else 'active',due='',criterion='',outcome=''))
        elif steps:
            field=re.match(r'^\s+[-*] (日期|完成标准|实际结果)[：:]\s*(.*)',line)
            if field:steps[-1][{'日期':'due','完成标准':'criterion','实际结果':'outcome'}[field[1]]]=field[2]
    # Markdown body is authoritative, including deletion of all checklist rows.
    result['steps']=steps
    result['body']=body
    return result

class GrowthWorkspaceMixin:
    def growth_root_ids(self):
        roots=self.vault.roots()
        explicit=self.settings.get('growth_root_id')
        if explicit and any(r['id']==explicit for r in roots):return [explicit]
        if any(r['id']=='upward' for r in roots):return ['upward']
        return [r['id'] for r in roots if r.get('writable') and not r['id'].startswith('advisor-')][:1]

    def _growth_documents(self):
        """Bounded scan of the explicitly configured main workspace only."""
        roots=self.growth_root_ids()
        if not roots:return [],[]
        stack=[(rid,'') for rid in roots];docs=[];warnings=[];seen=0
        while stack and seen<400:
            rid,folder=stack.pop()
            try:entries=self.vault.tree(rid,folder)['entries']
            except (ValueError,OSError) as error:
                warnings.append('部分成长目录暂不可读：'+str(error));continue
            for entry in entries:
                if entry['kind']=='directory':
                    if entry['path'].count('/')<4:stack.append((rid,entry['path']))
                    else:warnings.append('深层成长目录未全部读取；请用知识库检索具体内容')
                    continue
                if not entry['filename'].endswith('.md'):continue
                seen+=1
                if seen>400:break
                try:
                    doc=self.vault.read(entry['file_id'])
                    meta,body=metadata(doc['text'])
                    docs.append(dict(entry,meta=meta,body=body,sha=doc['sha256'] if 'sha256' in doc else doc.get('sha')))
                except (ValueError,OSError):warnings.append('部分成长文档暂不可读')
        if stack or seen>=400:warnings.append('本次最多读取主知识空间400篇文档；更多内容请检索')
        return docs,warnings

    def growth_snapshot(self,days=7):
        if isinstance(days,bool) or str(days) not in ('7','30'):raise ValueError('阶段范围仅支持7或30天')
        days=int(days);snapshot=self.phase_snapshot(days);period=snapshot['period']
        docs,warnings=self._growth_documents()
        original_goals={g['id']:g for g in snapshot['goals']}
        migrated={d['meta'].get('legacy_id'):d for d in docs if d['meta'].get('legacy_id')}
        # This is a derived identity index, never a second business record.
        # A deleted/unreadable canonical file must not resurrect the old database copy.
        with self.store.conn() as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS growth_document_links(legacy_id TEXT PRIMARY KEY,file_id TEXT NOT NULL)')
            for legacy_id,doc in migrated.items():
                connection.execute('INSERT OR REPLACE INTO growth_document_links VALUES (?,?)',(legacy_id,doc['file_id']))
            known_links={row['legacy_id']:row['file_id'] for row in connection.execute('SELECT legacy_id,file_id FROM growth_document_links')}
        source_times={}
        if migrated:
            with self.store.conn() as connection:
                rows=connection.execute("SELECT json_extract(entry.value,'$.id') AS note_id,json_extract(c.data,'$.payload.source_time') AS source_time FROM objects c,json_each(c.data,'$.saved_items') entry WHERE c.kind='capture' AND json_extract(entry.value,'$.kind')='note' ORDER BY c.rowid DESC").fetchall()
            for row in rows:
                if row['note_id'] in migrated and row['source_time'] and row['note_id'] not in source_times:source_times[row['note_id']]=row['source_time']
        goals=[];ideas=[];reviews=[];learning=[]
        clock=dt.datetime.now(TZ)
        def within(at):
            return bool(local_date(at) and period['start']<=local_date(at)<=period['end'] and moment_key(at)<=clock.timestamp())
        for g in original_goals.values():
            doc=migrated.get(g['id'])
            if doc:
                meta=doc['meta'];base=dict(g,**meta.get('legacy_goal_details',{}))
                if isinstance(meta.get('legacy_steps'),list):base['steps']=meta['legacy_steps']
                goal=goal_body(doc['body'],base)
                goal.update(title=doc['title'],file_id=doc['file_id'],source='共享 Markdown · 当前版本')
            elif g['id'] in known_links or warnings:continue
            else:goal=dict(g,file_id=None,source='原工作台记录 · 尚未关联共享文档')
            if goal.get('status')!='archived':goals.append(goal)
        for doc in docs:
            meta=doc['meta'];kind=meta.get('record_kind') or meta.get('legacy_kind') or meta.get('kind')
            at=meta.get('occurred_at') or meta.get('source_time') or source_times.get(meta.get('legacy_id')) or meta.get('created_at') or meta.get('date')
            folder=doc['path'].split('/')[0]
            if meta.get('archived') is True or meta.get('legacy_archived') is True:continue
            if kind=='goal' or folder=='目标与行动':
                if meta.get('legacy_id') in original_goals:continue
                base={'id':doc['file_id'],'status':meta.get('status',meta.get('legacy_status','draft')),'steps':meta.get('steps',meta.get('legacy_steps',[])),'due':meta.get('due','')}
                row=goal_body(doc['body'],base)
                row.update(title=doc['title'],file_id=doc['file_id'],source='共享 Markdown · 当前版本')
                if row.get('status')!='archived':goals.append(row)
                continue
            if kind=='learning' or folder=='顾问学习':
                learn_meta=meta.get('legacy_learning',{})
                at=meta.get('read_at') or learn_meta.get('read_at') or at
                if within(at):learning.append({'id':meta.get('legacy_id') or doc['file_id'],'title':doc['title'],'body':doc['body'],'created_at':at,'status':meta.get('status',meta.get('legacy_status','recorded')),'source':'共享 Markdown · 学习及实践留底','file_id':doc['file_id']})
                continue
            if not within(at):continue
            if kind in ('review','idea','note','conversation') or folder in ('想法与笔记','阶段复盘','每日想法'):
                row={'id':meta.get('legacy_id') or doc['file_id'],'title':doc['title'],'body':doc['body'],
                     'created_at':at,'source':'共享 Markdown · 已保存记录','file_id':doc['file_id'],'ref':doc['file_id']}
                if meta.get('review_period'):row['review_period']=meta['review_period']
                (reviews if kind=='review' or folder=='阶段复盘' else ideas).append(row)
        mapped=set(known_links)
        if warnings:mapped.update(n['id'] for n in snapshot['ideas']+snapshot['reviews'])
        # Do not replace edited or undated Markdown with stale copies from SQLite.
        for key,target in [('ideas',ideas),('reviews',reviews)]:
            target.extend(dict(row,file_id=None) for row in snapshot[key] if row['id'] not in mapped)
        for row in self.store.all('learning'):
            at=row.get('read_at') or row.get('created_at')
            if row['id'] not in mapped and within(at):learning.append({'id':row['id'],'title':row.get('advisor','顾问学习'),'body':row.get('insight',''),'created_at':at,'status':row.get('status','recorded'),'source':'原学习与实践记录','file_id':None})
        existing={r.get('body','').strip() for r in ideas}
        with self.store.conn() as connection:
            rows=connection.execute("SELECT id,data FROM objects WHERE kind='workspace-message' AND json_extract(data,'$.role')='user' ORDER BY rowid DESC LIMIT 300").fetchall()
        for row in rows:
            data=json.loads(row['data']);body=data.get('text','').strip();at=data.get('created_at')
            if not body or body in existing or not within(at):continue
            existing.add(body);ideas.append({'id':row['id'],'title':body[:60],'body':body,'created_at':at,
                'source':'本人近期对话 · 未当作已完成或正式计划','file_id':None,'ref':'workspace-message:'+row['id']})
        today=clock.date().isoformat();actions=[];overdue=[];unscheduled=[]
        focus=dict(snapshot['focus'])
        focus.update(applies_today=focus.get('date')==today and focus.get('status')=='confirmed',
                     date_note=('今日已明确更新' if focus.get('status')=='confirmed' else '今日重点尚未确认') if focus.get('date')==today else '历史或未注明适用日期的重点，不计为今日已确认')
        for goal in goals:
            if goal.get('status')!='active':continue
            for step in goal.get('steps',[]):
                if step.get('done') or step.get('status')=='draft':continue
                action=dict(step,goal_id=goal['id'],goal_title=goal.get('title',''),file_id=goal.get('file_id'))
                due=step.get('due','')
                if due==today:actions.append(action)
                elif due and re.fullmatch(r'\d{4}-\d{2}-\d{2}',due) and due<today:overdue.append(action)
                elif not due:unscheduled.append(action)
        qpath=Path(__file__).parent/'quotes.json'
        quotes=json.loads(qpath.read_text());quotes=quotes.get('quotes',[]) if isinstance(quotes,dict) else quotes
        daily=dict(quotes[clock.date().toordinal()%len(quotes)]) if quotes else {}
        daily['author']=daily.get('advisor','')
        sources=[]
        for source in self.source_items():
            sources.append({'id':source['id'],'title':source.get('title',''),'date':source.get('date',''),
                'status':source.get('evidence_type','参考资料'),'detail':source.get('read_scope',source.get('note',''))})
        for source in self.store.all('growth-plan-statement'):
            if source.get('status')=='verified':sources.append({'id':'growth-update:'+source['id'],'title':'本人当轮方向更新原话','date':source.get('at',''),'status':'current_user_instruction','detail':'当轮要求和实际写回，可回读'})
        sources+=snapshot['sources']
        for warning in warnings:sources.append({'id':'scan-warning','title':'知识空间读取范围','status':'partial','detail':warning})
        method=dict(snapshot['method'],mastery=snapshot['vision'].get('mastery',{}))
        return {'period':period,'generated_at':clock.isoformat(),
                'today':{'date':today,'quote':daily,'focus':focus,'actions':actions,'overdue':overdue,'unscheduled':unscheduled},
                'panorama':snapshot['vision'],'goals':goals,'goal_proposals':[{k:d[k] for k in ('id','title','summary','status','plan_draft','actions','source_ids') if k in d} for d in self.plan().get('domains',[])],
                'ideas':sorted(ideas,key=lambda x:moment_key(x['created_at']),reverse=True)[:100],
                'reviews':sorted(reviews,key=lambda x:moment_key(x['created_at']),reverse=True)[:50],
                'learning':sorted(learning,key=lambda x:moment_key(x['created_at']),reverse=True)[:80],'events':snapshot['events'],'creator':snapshot['creator'],'method':method,'sources':sources,
                'freshness':self.source_freshness(),
                'advisors':self._workspace_advisors,'local_access':self.local_capabilities(),'presets':PURPOSES,
                'scope_warnings':list(dict.fromkeys(warnings)),'scope_note':'共享 Markdown 为当前文档事实；本人原话、历史资料、候选计划与实际成果分别保留。'}

    def growth_context(self,purpose='general',days=7):
        if purpose not in {p['id'] for p in PURPOSES}:raise ValueError('未知成长用途')
        snapshot=self.growth_snapshot(days)
        context={'purpose':purpose,'period':snapshot['period'],'generated_at':snapshot['generated_at'],
                 'scope_note':snapshot['scope_note'],'focus':snapshot['today']['focus'], 'freshness':snapshot['freshness']}
        fields={'daily':['today','creator'],'panorama':['panorama'],'goals':['goals','goal_proposals'],
                'ideas':['ideas'],'review':['goals','ideas','reviews','learning','events','creator','method'],
                'learn':['method','learning'],'local':[],'general':[]}
        for field in fields[purpose]:context[field]=snapshot[field]
        context['sources']=[{'id':s['id'],'title':s['title'],'status':s.get('status')} for s in snapshot['sources']]
        def bound(value):
            if isinstance(value,str):return value[:2200]
            if isinstance(value,list):return [bound(x) for x in value[:30]]
            if isinstance(value,dict):return {k:bound(v) for k,v in value.items()}
            return value
        context=bound(context)
        context['excerpt_note']='按用途有界摘录；需要原文时使用知识工具，不把未读材料说成已核验。'
        while len(json.dumps(context,ensure_ascii=False))>26000:
            longest=max((k for k,v in context.items() if isinstance(v,list) and v),key=lambda k:len(json.dumps(context[k],ensure_ascii=False)),default=None)
            if longest:context[longest].pop()
            else:
                context={k:v for k,v in context.items() if k not in fields[purpose]}
                context['excerpt_note']='资料较长，本轮仅发送方向和目录，请按需检索具体文档。'
                break
        return context
