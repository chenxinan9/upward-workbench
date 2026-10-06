"""Source-backed planning, reference reading and visible local retrieval. No external calls."""
from pathlib import Path
import datetime as dt, hashlib, json, re, zipfile
import xml.etree.ElementTree as ET

THEMES={
 'career':['职业','求职','简历','面试','岗位','薪资','职场'],
 'business':['事业','创业','现金流','副业','收入','一人公司','获客','交付','商业'],
 'ai':['AI','ai','人工智能','编程','工程','自动化','FDE','fde','项目'],
 'english':['英语','英文','口语','阅读','english'],
 'health':['健康','身体','运动','健身','睡眠','饮食','体重'],
 'creation':['创作','自媒体','视频','写作','表达','内容','公众号'],
 'freedom':['长期','自由','旅居','财富','财务','预算','资产'],
 'relationships':['家庭','亲子','孩子','母亲','关系','照护','陪伴']}
ROUTES=[(['拖延','迟迟','不开始','行动力','自律','习惯','坚持','计划很多','开始不了'],['think-woop','clear-perspective','wangyangming-perspective']),(['成功','意义','迷茫','人生','内耗'],['naval-perspective','frankl-perspective','aurelius-perspective']),(['哲学','实践论','认知','实事求是'],['mao-zedong-perspective','marx-perspective','feynman-perspective']),(['创作','网红','表达','视频','写作'],['steve-jobs-perspective','dan-koe-skill','mrbeast-perspective'])]

def plain(text,limit=5000):
    if not isinstance(text,str) or len(text)>limit:raise ValueError('文字类型或长度不正确')
    return text.strip()

class GrowthMixin:
    def plan(self):
        path=self.settings.get('growth_plan')
        if path and Path(path).is_file():
            result=json.loads(Path(path).read_text())
            result['connected']=True
        else:result={'schema_version':1,'connected':False,'domains':[],'source_register':[]}
        return result
    def commitments(self):
        config=self.settings.get('advisor_config')
        if not config or not Path(config).is_file():return {'connected':False,'goal_statuses':[],'next_actions':[]}
        path=json.loads(Path(config).read_text()).get('canonical_commitments')
        if not path or not Path(path).is_file():return {'connected':False,'goal_statuses':[],'next_actions':[]}
        data=json.loads(Path(path).read_text());periods=data.get('periods',{});period=max(periods) if periods else ''
        return dict(periods.get(period,{}),connected=True,period=period,updated_at=data.get('updated_at'),goal_versions_count=len(data.get('goal_versions',[])))
    def commitment_source(self):
        config=json.loads(Path(self.settings['advisor_config']).read_text());path=Path(config['canonical_commitments'])
        return {'id':'source:canonical-commitments','title':'现有目标与承诺台账','text':path.read_text()[:60000],'source':'本人现行周复盘台账 · 只读','kind':'历史目标、已知结果与未确认建议，须保留原状态'}
    def source_items(self):
        sources=self.plan().get('source_register',[])
        return [dict(v,id=k) for k,v in sources.items()] if isinstance(sources,dict) else sources
    def source(self,sid):
        if sid=='canonical-commitments':return self.commitment_source()
        source=next((s for s in self.source_items() if s.get('id',s.get('source_id'))==sid),None)
        if not source:raise ValueError('资料来源不存在')
        if source.get('external_key')=='canonical_commitments':return self.commitment_source()
        if source.get('root')=='web':
            return {'id':'source:'+sid,'title':source['title'],'text':source.get('read_scope','')+'\n\n原始出处：'+source['url']+'\n核对日期：'+source.get('checked_at','未注明'),'source':source['url'],'kind':'公开资料出处说明；本页不是网站原文'}
        root=Path(self.settings.get('private_root','')).resolve()
        rel=source.get('path',source.get('relative_path',''))
        if not self.settings.get('private_root') or not rel:raise ValueError('未配置资料根目录')
        f=(root/rel).resolve()
        if root not in f.parents or f.suffix.lower() not in ['.md','.txt','.json','.docx'] or not f.is_file():raise ValueError('来源不在可读文本范围；请按摘录与原文件核对')
        if f.stat().st_size>2*1024*1024:raise ValueError('原文过长，请通过知识库检索具体片段')
        if f.suffix.lower()=='.docx':
            with zipfile.ZipFile(f) as z:
                if z.getinfo('word/document.xml').file_size>4*1024*1024:raise ValueError('文档展开内容过长')
                xml=z.read('word/document.xml')
                if b'<!DOCTYPE' in xml or b'<!ENTITY' in xml:raise ValueError('不支持含实体声明的文档')
                ns={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
                text='\n'.join(''.join(n.itertext()) for n in ET.fromstring(xml).findall('.//w:p',ns))
        else:text=f.read_text()
        actual_hash=hashlib.sha256(f.read_bytes()).hexdigest();changed=bool(source.get('sha256') and source['sha256']!=actual_hash)
        warning='【来源文件已变化】以下为当前原文，与方案研究时登记的版本不同；请重新核对后更新判断。\n\n' if changed else ''
        return {'id':'source:'+sid,'title':source.get('title',f.stem),'text':warning+text[:60000],'source':source.get('date',''),'kind':'个人来源 · 保留原文历史状态；文档正文抽取不含图片','source_changed':changed,'registered_sha256':source.get('sha256'),'current_sha256':actual_hash}
    def domain(self,id):
        domain=next((x for x in self.plan().get('domains',[]) if x['id']==id),None)
        if not domain:raise ValueError('成长领域不存在')
        return domain
    def domain_read(self,id):
        d=self.domain(id)
        return {'id':'plan:'+id,'title':d['title']+' · 已整理方案','text':json.dumps(d,ensure_ascii=False,indent=2),'source':'个人资料研究 · 待确认方案','kind':'带日期的历史依据与当前建议，未经确认不是本人承诺'}
    def adopt(self,d):
        domain=self.domain(plain(d.get('domain_id',''),100))
        with self.store.lock:
            existing=next((g for g in self.store.all('goal') if g.get('origin_id')=='domain:'+domain['id'] and g['status']!='archived'),None)
            if existing:return existing
            title=plain(d.get('title',''),180)
            if not title:raise ValueError('请核对要采用的目标标题')
            plan=self.plan();steps=d.get('steps',[]);actions={a['id']:a for a in domain.get('actions',[])}
            for step in steps:
                aid=step.get('plan_action_id')
                if aid and aid not in actions:raise ValueError('行动不属于当前方案')
                if aid:step['canonical_action_ref']=actions[aid].get('canonical_action_ref','')
            snapshot={'plan_generated_at':plan.get('generated_at'),'plan_sha256':hashlib.sha256(json.dumps(plan,ensure_ascii=False,sort_keys=True).encode()).hexdigest(),'domain':domain,'sources':[s for s in self.source_items() if s.get('id') in domain.get('source_ids',[])],'canonical_goal_id':domain.get('canonical_goal_id',''),'canonical_action_refs':list(dict.fromkeys(a.get('canonical_action_ref') for a in domain.get('actions',[]) if a.get('canonical_action_ref'))),'ledger_status':'仅引用原台账；工作台采用状态独立保存，未写回原台账'}
            result=self.goal({'title':title,'why':plain(d.get('why',''),1500),'domain':domain['title'],'domain_id':domain['id'],'origin_id':'domain:'+domain['id'],'status':'active','priority':d.get('priority',2),'steps':steps,'due':d.get('due',''),'source_snapshot':snapshot})
            return result
    def advisor_entries(self):
        p=self.settings.get('catalog')
        if p and Path(p).is_file():return [x for x in json.loads(Path(p).read_text()).get('entries',[]) if x.get('status')=='installed_verified']
        root=Path(__file__).resolve().parent/'advisors-bundle';manifest=root/'manifest.json'
        entries=[]
        if manifest.is_file():
            for e in json.loads(manifest.read_text()).get('entries',[]):
                folder=(root/e['path']).resolve()
                if root.resolve() not in folder.parents or not (folder/'SKILL.md').is_file():continue
                entries.append(dict(e,installed_path=str(folder),status='installed_verified',review_note='随包提供的审阅缩编方法；不是名人本人或完整著作'))
        return entries
    def advisor_entry(self,name):
        entry=next((x for x in self.advisor_entries() if x.get('name')==name),None)
        if not entry:raise ValueError('顾问不存在或未启用')
        return entry
    def method_registry(self):
        root=Path(__file__).resolve().parent;path=root/'advisors-bundle/methods.json'
        data=json.loads(path.read_text()) if path.is_file() else []
        return data.get('methods',[]) if isinstance(data,dict) else data
    def messages(self,sid):
        self.store.get(sid,'session');result=[]
        for m in reversed(self.store.all('message')):
            if m['session_id']!=sid:continue
            if m.get('role')=='assistant' and m.get('job_id'):
                try:
                    job=self.store.get(m['job_id'],'job');preview=self.store.get(job['preview_id'],'preview')
                    m['reply_context']={'preview_id':preview['id'],'context_hash':preview['hash'],'contexts':preview['contexts'],'history_count':preview['history_count'],'history':preview['history']}
                except ValueError:m['context_warning']='该条历史回复的发送快照不可用，不能用当前资料代替。'
            result.append(m)
        return result
    def advisor_files(self,name):
        entry=self.advisor_entry(name);root=Path(entry['installed_path']).resolve();files=[]
        candidates=[root/'SKILL.md']+sorted((root/'references').rglob('*')) if (root/'references').exists() else [root/'SKILL.md']
        for p in candidates:
            if p.is_symlink() or not p.is_file() or p.suffix.lower() not in ['.md','.txt'] or root not in p.resolve().parents or p.stat().st_size>1_000_000:continue
            rel=p.relative_to(root).as_posix();text=p.read_text(errors='replace');match=re.search(r'^#\s+(.+)',text,re.M)
            title=(match.group(1).strip() if match else p.stem.replace('-',' '))[:130]
            files.append({'id':'advisor-file:'+name+':'+rel,'path':rel,'title':'方法入口' if rel=='SKILL.md' else title,'characters':len(text),'type':'方法说明' if rel=='SKILL.md' else '参考材料'})
        return {'name':name,'title':entry['title'],'category':entry['category'],'url':entry['url'],'review_note':entry.get('review_note','社区方法需要结合当下问题判断'),'files':files,'learning':[x for x in self.store.all('learning') if x['advisor']==name]}
    def advisor_file_read(self,id):
        parts=id.split(':',2)
        if len(parts)!=3:raise ValueError('资料标识错误')
        name,rel=parts[1:];listing=self.advisor_files(name);item=next((x for x in listing['files'] if x['path']==rel),None)
        if not item:raise ValueError('不允许读取该参考文件')
        root=Path(self.advisor_entry(name)['installed_path']).resolve();f=(root/rel).resolve()
        if root not in f.parents:raise ValueError('参考路径越界')
        return {'id':id,'name':name,'title':listing['title']+' · '+item['title'],'text':f.read_text(errors='replace')[:60000],'source':listing['url'],'kind':'顾问'+item['type']+' · 社区材料需核对','characters':item['characters']}
    def learn(self,d):
        name=plain(d.get('advisor',''),100);listing=self.advisor_files(name)
        file_id=d.get('file_id','advisor-file:'+name+':SKILL.md')
        if not any(f['id']==file_id for f in listing['files']):raise ValueError('学习来源不在当前顾问文件内')
        status=d.get('status','reading')
        if status not in ['reading','read','practiced']:raise ValueError('学习状态不正确')
        insight=plain(d.get('insight',''),5000);goal_id=d.get('goal_id','')
        if goal_id:self.store.get(goal_id,'goal')
        if status=='practiced' and not insight:raise ValueError('请留下实际运用的结果再标记实践')
        with self.store.lock:
            old=next((x for x in self.store.all('learning') if x.get('file_id')==file_id),None)
            if status=='reading' and old and old['status'] in ['read','practiced']:status=old['status']
            value={'advisor':name,'file_id':file_id,'status':status,'insight':insight if 'insight' in d else (old or {}).get('insight',''),'goal_id':goal_id if 'goal_id' in d else (old or {}).get('goal_id',''),'position':max(0,min(int(d.get('position',(old or {}).get('position',0))),100)),'read_at':dt.datetime.now(dt.timezone.utc).isoformat()}
            if status=='practiced' and not value['insight']:raise ValueError('实践记录必须保留实际运用的结果')
            return self.store.put('learning',value,old['id'] if old else None,old['rev'] if old else None)
    def context_candidates(self,d):
        question=plain(d.get('question',''),6000)
        if not question:raise ValueError('先写下你要讨论的问题')
        candidates=[];warnings=[];domains=self.plan().get('domains',[])
        scored=[]
        for domain in domains:
            keys=domain.get('keywords',THEMES.get(domain['id'],[]))+[domain['title']]
            score=sum(len(k) for k in keys if k.lower() in question.lower())
            if d.get('domain_id')==domain['id']:score+=100
            if score:scored.append((score,domain))
        scored.sort(key=lambda x:x[0],reverse=True)
        chosen=scored[0][1] if scored else None
        if chosen:candidates.append({'id':'plan:'+chosen['id'],'title':chosen['title']+' · 个人依据与方案','reason':'匹配当前领域；含资料日期与待确认项'})
        advisors=self.catalog();preferred=(chosen or {}).get('advisor_slugs',[])
        ranked=[]
        for a in advisors:
            if not a['installed']:continue
            score=10 if a['name'] in preferred else 0
            score+=sum(20-names.index(a['name']) for words,names in ROUTES if a['name'] in names and any(w.lower() in question.lower() for w in words))
            score+=sum(4 for k in [a['title'],a['category'],a['name']] if k.lower() in question.lower())
            for key,words in THEMES.items():
                if any(w.lower() in question.lower() for w in words) and any(w.lower() in (a['title']+a['category']).lower() for w in words):score+=2
            if score:ranked.append((score,a))
        ranked.sort(key=lambda x:x[0],reverse=True)
        for score,a in ranked[:2]:
            candidates.append({'id':'advisor:'+a['name'],'title':a['title']+' · 方法','reason':'按领域与关键词匹配，发送前可移除'})
            files=self.advisor_files(a['name'])['files'][1:]
            if files and len(candidates)<4:
                querywords=[w for words in THEMES.values() for w in words if w.lower() in question.lower()]
                top=sorted(files,key=lambda f:sum(w.lower() in f['title'].lower() for w in querywords),reverse=True)[0]
                candidates.append({'id':top['id'],'title':top['title'],'reason':'该顾问的参考材料，可先回读核对'})
        terms=[w for words in THEMES.values() for w in words if w.lower() in question.lower()]
        for word in list(dict.fromkeys(terms))[:2]:
            try:
                for r in self.search(word)[:2]:
                    if not any(x['id']==r['id'] for x in candidates):candidates.append({'id':r['id'],'title':r['title'],'reason':'本机知识库关键词：'+word})
            except Exception:
                if not warnings:warnings.append('知识库检索失败，当前只列出方案和顾问匹配；请检查索引连接后重试。')
        return {'method':'本地领域与关键词匹配；不是全库语义理解','candidates':candidates[:8],'suggested_ids':[x['id'] for x in candidates[:4]],'domain_id':chosen['id'] if chosen else '','warnings':warnings}
    def goal_from_record(self,d):
        source=plain(d.get('source_id',''),150)
        if source.startswith('message:'):
            record=self.store.get(source[8:],'message')
            if record.get('role')!='assistant' or record.get('status')!='completed':raise ValueError('只能使用已完成的顾问回复')
        elif source.startswith('note:'):record=self.store.get(source[5:],'note')
        else:raise ValueError('需要可回看的记录来源')
        with self.store.lock:
            old=next((g for g in self.store.all('goal') if g.get('origin_id')==source and g['status']!='archived'),None)
            if old:return old
            return self.goal(dict(d,origin_id=source,status='active',id=None,rev=None))
    def add_record_action(self,d):
        source=plain(d.get('source_id',''),150)
        if source.startswith('message:'):
            record=self.store.get(source[8:],'message')
            if record.get('role')!='assistant' or record.get('status')!='completed':raise ValueError('只能使用已完成的顾问回复')
        elif source.startswith('note:'):self.store.get(source[5:],'note')
        else:raise ValueError('行动需要可回看的来源')
        with self.store.lock:
            goal=self.store.get(d['goal_id'],'goal')
            if any(s.get('source_id')==source and s.get('text')==plain(d.get('text',''),300) for s in goal['steps']):return goal
            goal['steps'].append({'text':d.get('text',''),'done':False,'source_id':source,'due':d.get('due',''),'criterion':d.get('criterion',''),'outcome':''})
            return self.goal(goal)
