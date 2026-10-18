"""Controlled edits and dated metadata for the explicitly registered JSON plan."""
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid
from vault import VaultWorkspace, _LOCK


def _plan_clauses(message, section):
    from codex_workspace import explicit_write_intent
    if not isinstance(message, str) or section not in ('daily', 'panorama'):
        return []
    text = re.sub(r'```[\s\S]*?```|“[^”]*”|「[^」]*」|『[^』]*』|‘[^’]*’|"[^"\n]*"|\'[^\'\n]*\'', '', message)
    if re.search(r'如果|假如|假设|举例|示例|例如|比如|解释|转述|引用|考虑|也许|可能|先别|暂不|只(?:是)?讨论|仅讨论|别保存|不要保存|不保存|不执行|取消|如何|怎么|能否|是否|可否|能不能|可不可以|该不该|要不要|[？?]', text):
        return []
    if re.search(r'(?:他|她|他们|她们|朋友|别人|有人|你)(?:[^。！!;；\n]{0,12})(?:说|建议|要求)|(?:昨天|之前|上次|以前|曾经|刚才|上周|过去)[^。！!;；\n]{0,15}(?:说|要求|让你|叫你)|(?:^|[。！!;；\n])\s*(?:不要|别|勿)\s*(?:修改|更新|改写|保存)\s*[。！!;；\n]*$',text):
        return []
    target = r'今日重点|今天的?重点|今日焦点|今天的?焦点|当前重点|当前主线' if section == 'daily' else r'长期方向|人生方向|长期愿景|北极星|人生全景|全景|支柱|维度|长期路线|路线'
    found=[]
    for clause in re.split(r'[，,。！!;；\n]', text):
        if not re.search(target, clause):
            continue
        normalized = re.sub(r'改成|改为|设为|定为|调整为|调整成|修订', '更新为', clause.strip()).replace('补充','更新')
        normalized = re.sub(r'^只(?=把|将|更新|修改)', '', normalized)
        if re.match(r'^(?:我的)?(?:' + target + r')', normalized):
            normalized = '请把' + normalized
        if explicit_write_intent(normalized):
            found.append(clause)
    return found


def plan_intent(message, section):
    return bool(_plan_clauses(message, section))


class GrowthPlanMixin:
    def _plan_path(self):
        raw = self.settings.get('growth_plan')
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError('尚未配置个人方案，不会自动创建空方案')
        path = Path(raw).expanduser().absolute()
        source = Path(__file__).resolve().parent
        if not Path(raw).expanduser().is_absolute() or path.suffix.lower() != '.json' or path.is_relative_to(source):
            raise ValueError('个人方案必须是源码目录之外明确登记的 JSON 文件')
        VaultWorkspace._no_symlinks(path)
        return path

    def _plan_read(self):
        path = self._plan_path()
        raw = VaultWorkspace._bytes(path)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError('个人方案超过读取限制')
        data = json.loads(raw.decode('utf-8-sig'))
        if not isinstance(data, dict):
            raise ValueError('个人方案不是 JSON 对象')
        if any(key in data and not isinstance(data[key],dict) for key in ('life_panorama','current_focus_draft')):
            raise ValueError('个人方案的全景或重点结构无效')
        return path, raw, data

    @staticmethod
    def _plan_projection(data, sha):
        version = data.get('context_revision', 0)
        if type(version) is not int or version < 0:
            raise ValueError('个人方案修订版本无效，请先核对原文件')
        projection = {key:copy.deepcopy(data.get(key, {})) for key in ('life_panorama', 'current_focus_draft')}
        if any(not isinstance(value, dict) for value in projection.values()):
            raise ValueError('个人方案的全景或重点结构无效')
        result = dict(projection, document_id='growth-plan', format='json', version=version,
                      sha256=sha, generated_at=data.get('generated_at'),
                      updated_at=data.get('updated_at'), notice='登记的 JSON 方案；只读回可修订部分，未返回私人路径。')
        if len(json.dumps(result, ensure_ascii=False)) > 120000:
            raise ValueError('个人方案内容过长，未交付不完整的可编辑版本')
        return result

    def read_growth_plan(self):
        _,raw,data=self._plan_read()
        result=self._plan_projection(data,hashlib.sha256(raw).hexdigest())
        sid=data.get('life_panorama',{}).get('north_star',{}).get('source_id')
        if isinstance(sid,str) and sid.startswith('growth-update:'):
            try:result['update_statement']=self.source(sid)
            except ValueError:result['update_statement']={'id':sid,'status':'unverifiable','text':'这项当轮陈述尚未完成独立回读核验'}
        return result

    @staticmethod
    def _plan_patch(current, patch, section, message):
        if not isinstance(patch, dict) or not patch:
            raise ValueError('方案更新必须是非空 JSON 对象')
        allowed = {'title', 'reason', 'selection_note'} if section == 'daily' else {'title', 'subtitle', 'north_star', 'pillars', 'route'}
        if set(patch) - allowed:
            raise ValueError('只能更新明确支持的方向或重点字段')
        result = copy.deepcopy(current)
        clauses=_plan_clauses(message,section)
        def merge(target, changes, keys):
            if not isinstance(target, dict) or not isinstance(changes, dict) or not changes or set(changes) - keys:
                raise ValueError('方案字段或原结构无效')
            for key, value in changes.items():
                if not isinstance(value, str) or not value.strip() or len(value) > 4000:
                    raise ValueError('方案内容必须是 1 至 4000 字符的文字')
                target[key] = value.strip()
        for key, value in patch.items():
            if key == 'north_star':
                if not any(re.search(r'长期方向|人生方向|长期愿景|北极星|(?:人生)?全景\s*(?:更新|改|调整|设|定)',clause) for clause in clauses):
                    raise PermissionError('本轮未明确指定长期方向字段')
                if re.search(r'(?:不|别|勿|不要)(?:修改|改|更新|调整)[^，,。;；\n]{0,8}(?:长期方向|人生方向|长期愿景|北极星)',message):
                    raise PermissionError('本轮明确要求保留长期方向')
                merge(result.setdefault(key, {}), value, {'quote', 'interpretation'})
            elif key in ('pillars', 'route'):
                if not isinstance(value, list) or not value or len(value) > 30 or not isinstance(result.get(key), list):
                    raise ValueError('只能修订已有维度或路线')
                names=r'支柱|维度' if key=='pillars' else r'路线'
                if not any(re.search(names,clause) for clause in clauses):
                    raise PermissionError('本轮未明确要求修改维度或路线')
                if re.search(r'(?:不要|不|别|勿|禁止)(?:再)?(?:修改|改|更新|调整)[^，,。;；\n]{0,12}(?:'+names+r')',message):
                    raise PermissionError('本轮明确要求保留这些维度或路线')
                seen = set()
                keys = {'title', 'desired_life', 'meaning'} if key == 'pillars' else {'title', 'horizon', 'description', 'evidence_needed'}
                for item in value:
                    if not isinstance(item, dict):raise ValueError('维度或路线修改无效')
                    changes = dict(item); index = changes.pop('index', None); ident = changes.pop('id', None)
                    if ident is not None:
                        matches = [i for i, row in enumerate(result[key]) if isinstance(row, dict) and row.get('id') == ident]
                        if len(matches) != 1 or index is not None:raise ValueError('维度标识不唯一或定位重复')
                        index = matches[0]
                    if type(index) is not int or not 0 <= index < len(result[key]) or index in seen:
                        raise ValueError('维度或路线序号无效或重复')
                    row=result[key][index]
                    named=[str(v) for v in (row.get('id'),row.get('title')) if isinstance(v,str) and len(v)>=2]
                    title_matches=[i for i,v in enumerate(result[key]) if isinstance(v,dict) and v.get('title')==row.get('title')]
                    ordinal=str(index+1)+(('|'+('一','二','三','四','五','六','七','八','九','十')[index]) if index<10 else '')
                    numbered=r'(?:第\s*(?:'+ordinal+r')\s*(?:个|项|条)?\s*(?:'+names+r')|(?:'+names+r')\s*第\s*(?:'+ordinal+r')\s*(?:个|项|条)?)'
                    targets=[re.split(r'改成|改为|修改为|更新为|设为|定为|调整为|补充',clause,maxsplit=1)[0] for clause in clauses]
                    identified=any((any(name in clause for name in named) and len(title_matches)==1) or re.search(numbered,clause) for clause in targets)
                    if not identified:raise PermissionError('请在本轮点名唯一的已有维度、路线标题或序号，不能修改未指定项')
                    seen.add(index); merge(result[key][index], changes, keys)
            else:
                if section=='panorama' and not any(re.search(r'标题|名称|副标题',clause) for clause in clauses):
                    raise PermissionError('本轮未明确指定全景标题字段')
                if section=='daily':
                    aliases={'title':r'标题|名称','reason':r'理由|原因|依据','selection_note':r'备注|选择说明|说明'}
                    alias=aliases[key]
                    denied=(r'(?:不要|不|别|勿|禁止|无需)(?:再)?(?:修改|改|更新|调整|替换|覆盖)[^，,。;；\n]{0,20}(?:'+alias+r')|(?:'+alias+r')[^，,。;；\n]{0,4}(?:不改|保留不变)')
                    if re.search(denied,message):raise PermissionError('本轮明确要求保留今日重点的这个字段')
                    named_field=any(re.search(alias,clause) or re.search(re.escape(clause)+r'\s*[，,]\s*(?:'+alias+r')(?:是|为|[：:])',message) for clause in clauses)
                    if key!='title' and not named_field:
                        raise PermissionError('本轮没有指定修改今日重点的这个字段')
                    if key=='title' and not any(not re.search(r'理由|原因|依据|备注|选择说明',clause) or re.search(r'标题|名称',clause) for clause in clauses):
                        raise PermissionError('本轮只要求修改说明，未授权替换今日重点标题')
                merge(result, {key:value}, allowed)
        return result

    def update_growth_plan(self, section, content, expected_sha256, *, message, job_id, stop):
        if not plan_intent(message, section):
            raise PermissionError('本轮未明确要求更新这项人生方向或今日重点')
        if not isinstance(content, str) or len(content) > 30000:
            raise ValueError('方案修改内容无效或过长')
        patch = json.loads(content)
        field = 'life_panorama' if section == 'panorama' else 'current_focus_draft'
        title = '人生全景' if section == 'panorama' else '今日重点'
        with _LOCK:
            path, original, data = self._plan_read()
            before = hashlib.sha256(original).hexdigest()
            if before != expected_sha256:
                raise ValueError('个人方案已有外部修改，请重新读取；未覆盖原文')
            current = data.get(field, {})
            if not isinstance(current, dict):raise ValueError('个人方案原结构无效')
            updated = self._plan_patch(current, patch, section, message)
            today=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date().isoformat()
            if updated == current and (section!='daily' or current.get('date')==today and current.get('status')=='confirmed'):
                return dict(self.read_growth_plan(), saved=True, verified=True, read_back=True, changed=False, section=section)
            stamp = dt.datetime.now(dt.timezone.utc).isoformat()
            version = data.get('context_revision', 0)
            if type(version) is not int or version < 0:raise ValueError('个人方案修订版本无效')
            updated['updated_at'] = stamp
            updated['update_basis'] = '本人本轮明确更新；旧来源仍保留为历史参考'
            statement=None
            if section=='panorama' and updated.get('north_star',{}).get('quote')!=current.get('north_star',{}).get('quote'):
                previous=current.get('north_star',{});history=updated.get('history',[])
                if not isinstance(history,list):raise ValueError('全景历史结构无效')
                updated['history']=history+[dict(previous,title='更新前的长期方向',description=previous.get('quote',''))]
                statement={'id':uuid.uuid4().hex,'status':'prepared','job_id':job_id,'section':section,
                    'user_text':message,'value':updated['north_star']['quote'],'at':stamp}
                updated['north_star'].update(source_id='growth-update:'+statement['id'],date=today,status='confirmed')
            if section == 'daily':
                updated['date'] = today
                if 'title' in patch or current.get('status')=='confirmed':updated['status'] = 'confirmed'
            revisions = data.get('revisions', [])
            if not isinstance(revisions, list):raise ValueError('个人方案修订历史无效')
            data.update({field:updated, 'updated_at':stamp, 'context_revision':version+1,
                         'revisions':revisions+[{'at':stamp, 'change':'本轮明确更新'+title, 'section':section,
                            'job_id':job_id, 'before_sha256':before, 'version':version+1}]})
            saved = (json.dumps(data, ensure_ascii=False, indent=2)+'\n').encode()
            expected = hashlib.sha256(saved).hexdigest()
            if len(saved)>2*1024*1024:raise ValueError('修改后方案超过读取限制；未写入')
            self._plan_projection(data,expected)
            if stop.is_set():raise ValueError('本轮已停止，未执行方案写入')
            if statement:
                ident=statement.pop('id');statement=self.store.put('growth-plan-statement',statement,id=ident)
            undo_id = uuid.uuid4().hex
            folder = self.store.path/'growth-plan-history'
            with VaultWorkspace._directory_fd(folder, create=True) as fd:os.fchmod(fd, 0o700)
            VaultWorkspace._atomic(folder/(undo_id+'.json'), original)
            metadata = {'before_sha':before, 'after_sha':expected, 'plan_identity':hashlib.sha256(str(path).encode()).hexdigest(), 'section':section}
            VaultWorkspace._atomic(folder/(undo_id+'.meta.json'), json.dumps(metadata).encode())
            journal = self.store.put('workspace-write', {'job_id':job_id, 'document_id':'growth-plan', 'title':title,
                'operation':'plan_update', 'format':'json', 'section':section, 'status':'started',
                'before_sha':before, 'expected_sha':expected, 'undo_id':undo_id, 'version':version+1, 'changed':True, 'at':stamp})
            try:
                with VaultWorkspace._directory_fd(path.parent) as fd:
                    if hashlib.sha256(VaultWorkspace._bytes_at(fd,path.name)).hexdigest() != before:
                        raise ValueError('保存期间方案发生变化；未覆盖原文')
                    if stop.is_set():
                        self.store.change(journal['id'],'workspace-write',status='cancelled')
                        raise ValueError('本轮已停止，未执行方案写入')
                    info = os.stat(path.name,dir_fd=fd,follow_symlinks=False)
                    if not stat.S_IMODE(info.st_mode)&0o222:raise PermissionError('个人方案文件为只读')
                    VaultWorkspace._atomic_at(fd,path.name,saved,stat.S_IMODE(info.st_mode))
                if statement:self.store.change(statement['id'],'growth-plan-statement',status='verified',sha=expected)
                check = self.read_growth_plan()
                if check['sha256'] != expected:raise ValueError('方案写入后回读不一致，结果待核实')
            except Exception:
                if statement:self.store.change(statement['id'],'growth-plan-statement',status='uncertain')
                if self.store.get(journal['id'],'workspace-write')['status']=='started':
                    self.store.change(journal['id'],'workspace-write',status='uncertain')
                raise
            self.store.change(journal['id'],'workspace-write',status='verified',sha=expected)
            return dict(check,saved=True,verified=True,read_back=True,changed=True,section=section,undo_id=undo_id)

    def undo_growth_plan(self, undo_id, expected_sha):
        if not isinstance(undo_id,str) or not re.fullmatch(r'[a-f0-9]{32}',undo_id):raise ValueError('方案恢复标识无效')
        with _LOCK:
            path, current, _ = self._plan_read(); folder=self.store.path/'growth-plan-history'
            metadata=json.loads(VaultWorkspace._bytes(folder/(undo_id+'.meta.json')))
            original=VaultWorkspace._bytes(folder/(undo_id+'.json'))
            if metadata.get('plan_identity')!=hashlib.sha256(str(path).encode()).hexdigest():raise ValueError('恢复副本不属于当前方案')
            if hashlib.sha256(original).hexdigest()!=metadata.get('before_sha'):raise ValueError('方案恢复副本校验失败')
            if expected_sha!=metadata.get('after_sha') or hashlib.sha256(current).hexdigest()!=expected_sha:
                raise ValueError('方案已有新修改，未覆盖当前版本')
            with VaultWorkspace._directory_fd(path.parent) as fd:
                if hashlib.sha256(VaultWorkspace._bytes_at(fd,path.name)).hexdigest()!=expected_sha:raise ValueError('恢复期间方案变化，未覆盖')
                mode=stat.S_IMODE(os.stat(path.name,dir_fd=fd,follow_symlinks=False).st_mode)
                if not mode&0o222:raise PermissionError('个人方案文件为只读，未恢复覆盖')
                VaultWorkspace._atomic_at(fd,path.name,original,mode)
            result=self.read_growth_plan()
            if result['sha256']!=metadata['before_sha']:raise ValueError('方案恢复回读未一致')
            self.store.put('growth-plan-restore',{'undo_id':undo_id,'sha':result['sha256'],'status':'verified'})
            return dict(result,restored=True,verified=True,section=metadata.get('section'))

    def source_freshness(self):
        clock=dt.datetime.now(dt.timezone(dt.timedelta(hours=8)));now=clock.isoformat();today=clock.date().isoformat()
        notice = '版本未变只表示与登记版本一致，不代表本人现状仍有效；文件修改时间不是事实发生日期。'
        try:
            path, raw, plan = self._plan_read()
            plan_row = {'status':'connected','connected':True,'formed_at':plan.get('generated_at'),
                'revised_at':plan.get('updated_at') or plan.get('life_panorama',{}).get('updated_at'),
                'version':plan.get('context_revision',0),'sha256':hashlib.sha256(raw).hexdigest(),
                'file_modified_at':dt.datetime.fromtimestamp(path.stat().st_mtime,dt.timezone.utc).isoformat()}
        except (OSError,ValueError,UnicodeError):
            return {'checked_at':now,'plan':{'status':'unconfigured' if not self.settings.get('growth_plan') else 'unverifiable','connected':False},'sources':[],'notice':notice}
        rows=[]
        register=plan.get('source_register',[])
        sources=[dict(v,id=k) for k,v in register.items() if isinstance(v,dict)] if isinstance(register,dict) else register if isinstance(register,list) else []
        for source in sources[:300]:
            if not isinstance(source,dict):continue
            row={k:source.get(k) for k in ('id','title')}
            row.update(source_date=source.get('date'),registered_at=source.get('registered_at'),
                last_checked_at=source.get('checked_at'),last_read_at=source.get('read_at'),fact_confirmed_at=source.get('confirmed_at'),
                registered_version=source.get('sha256'),current_version=None,status='unverifiable')
            if isinstance(row['source_date'],str) and re.match(r'^\d{4}-\d{2}-\d{2}',row['source_date']) and row['source_date'][:10]>today:
                row['date_warning']='来源日期在未来，不能视为已发生事实'
            try:
                if source.get('root')=='web' or source.get('url') and not (source.get('path') or source.get('relative_path') or source.get('external_key')):
                    row.update(status='external_unchecked',detail='外部链接，本次未联网核对正文或可访问性')
                    rows.append(row);continue
                if source.get('external_key')=='canonical_commitments':
                    config_path=Path(self.settings.get('advisor_config','')).expanduser().absolute()
                    config=json.loads(VaultWorkspace._bytes(config_path))
                    location=config.get('canonical_commitments')
                    if not isinstance(location,str) or not Path(location).expanduser().is_absolute():raise ValueError('外部台账未配置')
                    file=Path(location).expanduser().absolute()
                else:
                    base=self.settings.get('private_root');relative=source.get('path') or source.get('relative_path')
                    if not isinstance(base,str) or not Path(base).expanduser().is_absolute() or not isinstance(relative,str) or not relative:
                        raise ValueError('未登记可核对的本地路径')
                    root=Path(base).expanduser().absolute();file=root/relative
                    if Path(relative).is_absolute() or '..' in Path(relative).parts or not file.is_relative_to(root):raise ValueError('来源路径越界')
                VaultWorkspace._no_symlinks(file)
                current=VaultWorkspace._bytes(file);current_sha=hashlib.sha256(current).hexdigest()
                row.update(current_version=current_sha,file_modified_at=dt.datetime.fromtimestamp(file.stat().st_mtime,dt.timezone.utc).isoformat())
                registered=row['registered_version']
                row.update(status='unchanged' if registered==current_sha else 'changed' if isinstance(registered,str) and re.fullmatch(r'[a-f0-9]{64}',registered) else 'unverifiable',
                           detail='与登记版本一致；现状仍需本人核对' if registered==current_sha else '原文件与登记版本不同，请重新核对' if registered else '可读，但未登记版本基准')
            except FileNotFoundError:row.update(status='missing',detail='登记的来源文件缺失')
            except (OSError,ValueError,UnicodeError):row.update(status='unverifiable',detail='路径、权限或资料结构暂不可核验')
            rows.append(row)
        return {'checked_at':now,'plan':plan_row,'sources':rows,'notice':notice,'truncated':len(sources)>300}
