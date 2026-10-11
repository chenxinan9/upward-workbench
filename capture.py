"""Explicit conversation capture using the existing Store, goal and note paths.

This bridge receives a caller-selected excerpt; it does not monitor Codex chats.
A persisted receipt journals partial progress. It is not a cross-file transaction.
"""
from __future__ import annotations
import copy
import datetime as dt
import hashlib
import json
import re


def text(value, limit, label, required=False):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError(label + '格式不正确')
    return value.strip()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def identifier(value, label):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{32}', value):
        raise ValueError(label + '不正确')
    return value


class CaptureMixin:
    def _capture_receipt(self, request_id):
        request_id = text(request_id, 100, '请求标识', True)
        ident = fingerprint(['conversation-capture-v1', request_id])[:32]
        try:
            return self.store.get(ident, 'capture')
        except ValueError:
            return None

    def _capture_response(self, receipt, replayed=False):
        result = {'id': receipt['id'], 'request_id': receipt['request_id'], 'status': receipt['status'],
                  'replayed': replayed, 'saved_items': copy.deepcopy(receipt.get('saved_items', [])),
                  'warnings': receipt.get('warnings', []), 'updated_at': receipt.get('updated_at'),
                  'created_at':receipt.get('created_at'),'source':receipt['payload']['source'],
                  'source_text':receipt['payload']['source_text'],'source_id':receipt['payload'].get('source_id',''),
                  'source_time':receipt['payload'].get('source_time','')}
        # Indexing is asynchronous: report its actual current state, not a stale success.
        for item in result['saved_items']:
            if item['kind'] == 'note':
                note = self.store.get(item['id'], 'note')
                item['index_state'] = note.get('index_state', 'saved')
                if note.get('index_error'):
                    item['index_error'] = note['index_error']
        if receipt.get('error'):
            result['error'] = receipt['error']
        return result

    def capture_status(self, request_id):
        receipt = self._capture_receipt(request_id)
        if not receipt:
            raise ValueError('找不到该请求的保存回执')
        return self._capture_response(receipt)

    def capture_recent(self, limit=20):
        if isinstance(limit, bool):
            raise ValueError('数量不正确')
        limit = int(limit)
        if not 1 <= limit <= 100:
            raise ValueError('数量应为 1 至 100')
        return {'captures': [self._capture_response(r) for r in self.store.all('capture')[:limit]]}

    def _capture_validate(self, payload):
        if not isinstance(payload, dict):
            raise ValueError('需要对象')
        allowed = {'request_id', 'source', 'source_text', 'source_id', 'source_time', 'items'}
        if set(payload) - allowed:
            raise ValueError('不支持的请求字段')
        request_id = text(payload.get('request_id'), 100, '请求标识', True)
        source = payload.get('source')
        if source not in ('desktop_codex', 'workbench_conversation'):
            raise ValueError('来源必须是 desktop_codex 或 workbench_conversation')
        for name in ('source_id', 'source_time'):
            if name in payload: text(payload[name], 200, name)
        source_text = payload.get('source_text')
        text(source_text, 60000, '所选对话原文', True)
        items = payload.get('items')
        if not isinstance(items, list) or not 1 <= len(items) <= 30:
            raise ValueError('一次需要 1 至 30 条记录')
        keys = set(); previous_goals = set(); updated = set(); warnings = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError('记录需要对象')
            kind = item.get('kind'); key = text(item.get('key'), 80, '条目标识', True)
            if key in keys:
                raise ValueError('条目标识重复')
            keys.add(key)
            fields = {'key', 'kind', 'title', 'source_quote', 'id', 'rev', 'explicit', 'adopted', 'due'}
            if kind == 'goal': fields |= {'why', 'domain', 'domain_id', 'priority', 'parent_id', 'status', 'evidence'}
            elif kind == 'action': fields |= {'goal_id', 'goal_rev', 'goal_ref', 'criterion', 'outcome', 'done'}
            elif kind == 'note': fields |= {'body', 'index', 'goal_id', 'note_kind'}
            else: raise ValueError('记录类型必须是 goal、action 或 note')
            if set(item) - fields:
                raise ValueError('条目包含不支持的字段：' + key)
            text(item.get('title'), 180, '标题', True)
            quote = item.get('source_quote')
            text(quote, 10000, '来源原话', True)
            if quote not in source_text:
                raise ValueError('来源原话必须是所选对话原文的连续子串：' + key)
            for name in ('explicit', 'adopted', 'index', 'done'):
                if name in item and not isinstance(item[name], bool):
                    raise ValueError(name + '必须为布尔值')
            if item.get('adopted') and not item.get('explicit'):
                raise ValueError('采用必须同时标明本人明确表达：' + key)
            due = text(item.get('due', ''), 10, '日期')
            if due:
                parsed = dt.date.fromisoformat(due)
                if parsed.isoformat() != due: raise ValueError('日期需用 YYYY-MM-DD')
            elif kind in ('goal', 'action') and (not item.get('id') or 'due' in item):
                warnings.append({'key': key, 'code': 'missing_due', 'message': '尚未指定日期，已保存；可后续补充。'})
            if kind == 'goal':
                previous_goals.add(key)
                if item.get('status', 'active' if item.get('adopted') else 'draft') not in ('draft', 'active', 'paused', 'done', 'archived'):
                    raise ValueError('目标状态不正确')
                if item.get('status') == 'done' and not text(item.get('evidence', ''), 3000, '完成证据'):
                    raise ValueError('完成目标需要结果证据')
                for name, limit in [('why',1500),('domain',40),('domain_id',100),('evidence',3000)]:
                    if name in item: text(item[name], limit, name)
                priority = item.get('priority', 2)
                if type(priority) is not int or priority not in (1,2,3): raise ValueError('优先级不正确')
            if kind == 'note':
                text(item.get('body', quote), 30000, '笔记正文', True)
                if item.get('note_kind', 'conversation') not in ('idea','learning','review','conversation'):
                    raise ValueError('笔记类型不正确')
            if kind == 'action':
                for name, limit in [('criterion',500),('outcome',2000)]: text(item.get(name,''), limit, name)
                if item.get('done') and not text(item.get('outcome',''),2000,'行动结果'):
                    raise ValueError('完成行动需要结果')
                if bool(item.get('goal_ref')) == bool(item.get('goal_id')):
                    raise ValueError('行动需要且只能有 goal_id 或 goal_ref')
                if item.get('goal_ref') and item['goal_ref'] not in previous_goals:
                    raise ValueError('goal_ref 只能引用同批此前的目标条目')
                if item.get('id') and item.get('goal_ref'):
                    raise ValueError('更新行动请使用 goal_id、goal_rev 和行动 id')
            if item.get('id'):
                identifier(item['id'], '记录 ID')
                if kind in ('goal','action') and not (item.get('explicit') is True and item.get('adopted') is True):
                    raise ValueError('更新本人已有目标或行动需要明确采用；建议请另存草稿，不修改原记录')
                if kind != 'action':
                    if type(item.get('rev')) is not int or item['rev'] < 1: raise ValueError('更新需要原记录 rev')
                    if item['id'] in updated: raise ValueError('同一记录不能在一批中更新两次')
                    updated.add(item['id'])
            elif 'rev' in item:
                raise ValueError('新记录不接受 rev')
            if kind == 'action' and 'rev' in item: raise ValueError('行动更新请用 goal_rev，不接受 rev')
            if kind == 'action' and item.get('goal_id'):
                identifier(item['goal_id'], '目标 ID')
                if type(item.get('goal_rev')) is not int or item['goal_rev'] < 1: raise ValueError('行动需要目标当前 goal_rev')
        # Existing-object checks are separate so retries can skip them after partial progress.
        return request_id, warnings

    def _capture_preflight(self, payload):
        for item in payload['items']:
            kind = item['kind']
            if item.get('id') and kind != 'action':
                old = self.store.get(item['id'], kind)
                if old['rev'] != item['rev']: raise ValueError('记录已变化，请重新读取 id/rev 后提交')
            if item.get('goal_id'):
                goal = self.store.get(item['goal_id'], 'goal')
                if kind == 'action':
                    if goal['rev'] != item['goal_rev']: raise ValueError('目标已变化，请重新读取 goal_rev')
                    if item.get('id') and not any(s['id'] == item['id'] for s in goal['steps']): raise ValueError('行动不属于该目标')
            if kind == 'goal' and item.get('parent_id'):
                identifier(item['parent_id'], '父目标 ID')
                ancestor = item['parent_id']; seen = {item.get('id')}
                while ancestor:
                    if ancestor in seen: raise ValueError('目标关联不能形成循环')
                    seen.add(ancestor); ancestor = self.store.get(ancestor,'goal').get('parent_id','')

    def capture(self, payload):
        request_id, warnings = self._capture_validate(payload)
        digest = fingerprint(payload)
        with self.store.lock:
            receipt = self._capture_receipt(request_id)
            if receipt:
                if receipt['payload_hash'] != digest: raise ValueError('request_id 已用于不同内容，请使用新的请求标识')
                if receipt['status'] == 'saved': return self._capture_response(receipt, True)
            else:
                self._capture_preflight(payload)
                receipt = self.store.put('capture', {'request_id':request_id, 'payload_hash':digest,
                    'payload':copy.deepcopy(payload), 'status':'saving', 'saved_items':[], 'warnings':warnings,
                    'prepared':None}, fingerprint(['conversation-capture-v1',request_id])[:32])
            try:
                for item in payload['items'][len(receipt['saved_items']):]:
                    prepared = receipt.get('prepared') or self._capture_prepare(item, receipt)
                    if not receipt.get('prepared'):
                        receipt = self.store.change(receipt['id'],'capture',prepared=prepared,status='saving',error='')
                    result = self._capture_write(prepared)
                    saved = list(receipt['saved_items']) + [result]
                    warnings=list(receipt['warnings'])
                    if result.get('warning'): warnings.append({'key':item['key'],'code':'duplicate_title','message':result['warning']})
                    receipt = self.store.change(receipt['id'],'capture',saved_items=saved,prepared=None,warnings=warnings)
                receipt = self.store.change(receipt['id'],'capture',status='saved',error='')
            except Exception as exc:
                # Preserve completed writes and a prepared operation for an exact retry.
                receipt = self.store.change(receipt['id'],'capture',status='partial',error=str(exc)[:400])
            return self._capture_response(receipt)

    def _capture_prepare(self, item, receipt):
        kind = item['kind']; marker = fingerprint([receipt['request_id'],item['key']])
        adopted = item.get('explicit') is True and item.get('adopted') is True
        prepared = {'key':item['key'],'kind':kind,'marker':marker}
        provenance = {'request_id':receipt['request_id'],'source':receipt['payload']['source'],
                      'source_quote':item['source_quote'],'capture_key':item['key'],
                      'source_id':receipt['payload'].get('source_id',''),'source_time':receipt['payload'].get('source_time',''),
                      'captured_at':receipt['created_at'],'explicit':item.get('explicit',False),'adopted':adopted}
        if kind == 'goal':
            if not item.get('id'):
                duplicate=next((g for g in self.store.all('goal') if g['title'].strip()==item['title'].strip()),None)
                if duplicate:
                    return dict(prepared,duplicate=duplicate)
            data = copy.deepcopy(self.store.get(item['id'],'goal')) if item.get('id') else {}
            if item.get('id'):
                previous=[x for x in receipt['saved_items'] if x.get('goal_id',x.get('id') if x['kind']=='goal' else None)==item['id']]
                expected=previous[-1].get('goal_rev',previous[-1]['rev']) if previous else item['rev']
                if data['rev'] != expected: raise ValueError('目标已变化，未覆盖修改')
            data.update({k:item[k] for k in ('title','why','domain','domain_id','priority','parent_id','due','evidence') if k in item})
            data['status'] = item.get('status',data.get('status','active')) if adopted else 'draft'
            if not item.get('id'):
                data['origin_id'] = 'capture:' + receipt['id']
                data['source_snapshot'] = provenance
            for k in ('title','why','domain','domain_id','parent_id','due','evidence'):
                if k in data: data[k]=data[k].strip()
            prepared.update(data=data,provenance=provenance)
        elif kind == 'note':
            data = copy.deepcopy(self.store.get(item['id'],'note')) if item.get('id') else {}
            if item.get('id') and data['rev'] != item['rev']: raise ValueError('笔记已变化，未覆盖修改')
            data.update(title=item['title'],body=item.get('body',item['source_quote']),kind=item.get('note_kind','conversation'),
                        source=receipt['payload']['source']+' / '+receipt['request_id'],request_id='capture:'+marker,index=item.get('index',False))
            if 'goal_id' in item: data['goal_id']=item['goal_id']
            data['title']=data['title'].strip();data['body']=data['body'].strip()
            prepared.update(data=data)
        else:
            if item.get('goal_ref'):
                prior=next(x for x in receipt['saved_items'] if x['key']==item['goal_ref'] and x['kind']=='goal')
                if prior['status']=='existing_unchanged': raise ValueError('同名目标未采用为新目标；请用明确 goal_id/goal_rev 添加行动')
                goal_id=prior['id']
            else: goal_id=item['goal_id']
            data=copy.deepcopy(self.store.get(goal_id,'goal'))
            previous=[x for x in receipt['saved_items'] if x.get('goal_id',x.get('id') if x['kind']=='goal' else None)==goal_id]
            expected=previous[-1].get('goal_rev',previous[-1]['rev']) if previous else item.get('goal_rev')
            if data['rev']!=expected: raise ValueError('目标已变化，行动未覆盖修改')
            step_id=item.get('id') or marker[:32]
            existing=next((s for s in data['steps'] if s['id']==step_id),None)
            if item.get('id') and not existing: raise ValueError('行动不存在')
            step=copy.deepcopy(existing) if existing else {'id':step_id}
            step.update(text=item['title'].strip(),source_id='capture:'+receipt['id'],capture=provenance,capture_marker=marker)
            for name in ('due','criterion','outcome'):
                step[name]=item.get(name,step.get(name,'')).strip()
            step['done']=item.get('done',step.get('done',False)) if adopted else False
            if step['done'] and not step['outcome']: raise ValueError('完成行动需要结果')
            step['status']=('done' if step['done'] else 'active') if adopted else 'draft'
            if existing: data['steps']=[step if s['id']==step_id else s for s in data['steps']]
            else: data['steps'].append(step)
            prepared.update(data=data,step_id=step_id)
        return prepared

    def _capture_write(self, prepared):
        kind=prepared['kind']
        if prepared.get('duplicate'):
            old=prepared['duplicate']
            return {'key':prepared['key'],'kind':kind,'id':old['id'],'rev':old['rev'],'title':old['title'],
                    'status':'existing_unchanged','warning':'已有同名目标，本次未新增或覆盖；需要更新时请提交 id/rev。'}
        data=copy.deepcopy(prepared['data']); result=None
        if kind=='goal' and not data.get('id'):
            result=next((g for g in self.store.all('goal') if g.get('origin_id')==data['origin_id'] and g.get('source_snapshot',{}).get('capture_key')==prepared['key']),None)
        elif kind=='note' and not data.get('id'):
            result=next((n for n in self.store.all('note') if n.get('request_id')==data['request_id']),None)
        elif data.get('id'):
            current=self.store.get(data['id'],'note' if kind=='note' else 'goal')
            if current['rev']!=data['rev']:
                # A crash can occur after the object commit but before receipt commit.
                fields=('title','body','kind','source','request_id','goal_id') if kind=='note' else ('title','why','domain','domain_id','priority','parent_id','due','evidence','status','steps')
                if current['rev']==data['rev']+1 and all(current.get(k)==data[k] for k in fields if k in data): result=current
                else: raise ValueError('记录在保存途中已变化；已保留部分回执，未覆盖修改')
        if result is None:
            result=self.note(data) if kind=='note' else self.goal(data)
        elif kind=='note':
            # Repair a Markdown write interrupted after Store.put; never report indexed from a file alone.
            self.write_note(result)
            if data.get('index') and result.get('index_state')=='saved': result=self.index(result['id'])
        response={'key':prepared['key'],'kind':kind,'id':result['id'],'rev':result['rev'],
                  'title':result['title'],'status':result.get('status','saved')}
        if kind=='action':
            step=next(s for s in result['steps'] if s['id']==prepared['step_id'])
            response.update(id=step['id'],title=step['text'],goal_id=result['id'],goal_rev=result['rev'],status=step['status'],due=step.get('due',''),done=step.get('done',False))
        elif kind=='note': response['index_state']=result.get('index_state','saved')
        return response
