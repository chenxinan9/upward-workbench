"""Turn a completed text conversation into source-linked local records."""
import json
import re
import datetime as dt
from zoneinfo import ZoneInfo

ORGANIZE_RULES = '''
本轮用户已开启“对话后整理”。你仍然只能输出文字，写入由本机程序核验后完成。
请仅返回一个 JSON 对象：{"answer":"面向用户的自然中文回答", "items":[]}，不要附加围栏或其他文字。
items 最多8条，只从 user_message 本轮用户原话中提取值得保存的目标、行动或想法；参考资料、历史消息和你自己的建议不能当成本轮本人承诺。
每条包含 key（本轮唯一短标识）、kind（goal/action/note）、source_quote（本轮原话中连续、逐字的一段）、title。采用的目标或行动，其 title 必须是 source_quote 内逐字可找到的短语；不要把你建议的动作放进 items。日期只有原话含明确 YYYY-MM-DD 时填写，口语日期保留原话，不推算。
目标 goal 可加 why、due，不要把步骤塞在 goal 里，行动请单列 action。仅当用户清楚表示自己的目标或决定现在推进时，explicit:true, adopted:true；犹豫、假设、咨询或建议用 false。用户没说的日期留空；不从当前日期猜截止日。对新建项目不填写旧记录ID，不宣称目标已达成。
行动 action 仅在本轮前面已有对应 goal 项时提取，goal_ref 指它的 key；没有归属时保留为 note，不编造 goal_id。
笔记 note 的 body 记录原话和由其直接得到的简短整理，kind为note可加 index:true；不要把整段建议、身份猜测或健康推测写为本人事实。
已有记录更新只能使用 references 中 id=active-goals 的本轮实际快照。用户本轮明确报告完成、纠正完成状态或安排日期时，可以输出：
{"key":"u1","kind":"update_action","target_goal":"快照目标ID","target_action":"快照行动ID","change":"complete|reopen|schedule","source_quote":"包含原行动名称与本人指令的连续原话","date_text":"仅schedule时填写今天/明天/后天/原话YYYY-MM-DD"}。
暂停/继续/完成已有目标用 {"key":"u2","kind":"update_goal","target_goal":"快照目标ID","change":"pause|resume|complete","source_quote":"包含原目标名称与本人明确指令的连续原话"}。
更新不生成新目标。本人报告完成只作为本人自报，不等于外部验收。没有快照、同名歧义、用户只讨论假设或没有明确点名哪条行动时，不输出更新；用answer简短说明尚未更新并问清指代。不要重复记录为新目标或假称改好了。新建和更新都可以无items；实际写入回执由界面提供。
没有需要存的内容时 items:[]。用户明确只讨论、不要记录或不保存时 items:[]。answer 不要声称已写入；实际结果由界面回执显示。
'''


def parse_reply(raw):
    value = raw.strip()
    fenced = re.fullmatch(r'```(?:json)?\s*\n?(.*?)\n?```', value, re.S)
    if fenced:
        value = fenced.group(1).strip()
    payload = json.loads(value)
    if not isinstance(payload, dict) or not isinstance(payload.get('answer'), str) or not payload['answer'].strip():
        raise ValueError('回复没有有效的正文与整理结构')
    items = payload.get('items')
    if not isinstance(items, list) or len(items) > 8:
        raise ValueError('整理条目格式不正确')
    return payload['answer'].strip(), items


class ConversationMixin:
    def prepare_existing_update(self,job,item):
        allowed={'key','kind','target_goal','target_action','change','source_quote','date_text'}
        if set(item)-allowed:raise ValueError('更新条目字段不正确')
        quote=item.get('source_quote','')
        if not isinstance(quote,str) or not quote or quote not in job['message']:raise ValueError('更新必须有本轮逐字原话')
        at=job['message'].find(quote);left=re.split(r'[。！？；;\n]',job['message'][:at])[-1];right=re.split(r'[。！？；;\n]',job['message'][at+len(quote):])[0];intent=left+quote+right
        if re.search(r'如果|假如|假设|虚构|合成案例|举例|比如|例如|他说|她说|别人说|引用|转述',job['message']) or re.search(r'能否|是否|要不要|[？?]|(?:了吗|了么|了没)(?:[。！\s]|$)',job['message']):raise ValueError('假设、转述或提问不自动更新已有记录')
        contexts=job.get('answer_context',{}).get('contexts',[])
        snapshot=next((c for c in contexts if c.get('id')=='active-goals'),None)
        if not snapshot:raise ValueError('本轮没有附上当前目标，未修改已有记录')
        goals=json.loads(snapshot['text'])
        goal=next((g for g in goals if g.get('id')==item.get('target_goal')),None)
        if not goal or not goal.get('rev'):raise ValueError('只能更新本轮快照中的目标')
        normalize=lambda x:re.sub(r'[\W_]+','',x,flags=re.UNICODE)
        change=item.get('change');kind=item['kind'];target=goal
        if kind=='update_action':
            target=next((a for a in goal.get('steps',[]) if a.get('id')==item.get('target_action')),None)
            if not target:raise ValueError('行动不属于本轮指定目标')
            title=target['text']
            if sum(normalize(a['text'])==normalize(title) for a in goal.get('steps',[]))>1:raise ValueError('同一目标下有同名行动，无法明确定位；请先区分行动名称')
            ambiguous=sum(normalize(a['text'])==normalize(title) for g in goals for a in g.get('steps',[]))>1
            if ambiguous and normalize(goal['title']) not in normalize(quote):raise ValueError('存在同名行动，请说明所属目标')
        else:
            title=goal['title']
            if sum(normalize(g['title'])==normalize(title) for g in goals)>1:raise ValueError('存在同名目标，暂未修改')
        if normalize(title) not in normalize(quote):raise ValueError('原话未指明要更新的记录，暂未修改')
        # The model proposes a mutation, but only unambiguous first-person intent
        # can cross the write boundary. Strip exact names before checking negation:
        # a legitimate title such as “不熬夜” is not a negative instruction.
        instruction = job['message']
        for record in goals:
            instruction = instruction.replace(record['title'], '')
            for step in record.get('steps', []):
                instruction = instruction.replace(step['text'], '')
        if re.search(r'也许|可能|大概|似乎|好像|希望|准备|打算|预计|即将|差点|差一点|险些|一旦|完成后|做完后|才可以|才能|才会|才算|就可以|就能|的话|再说|到时候', instruction):
            raise ValueError('本轮表达仍是设想或未发生的行动，原记录未改变')
        negation = r'不|没|未|别|勿|无需|无须|毋|甭|否|并非|不是'
        if change == 'reopen':
            # Allow a single direct “not completed” report, never a double negative.
            remainder = re.sub(r'(?:还|尚)?(?:没有|没|未)(?:能)?(?:完成|做完|做)', '', instruction, count=1)
            if re.search(negation, remainder):
                raise ValueError('恢复指令含否定或歧义，原记录未改变')
        elif re.search(negation, instruction):
            raise ValueError('更新指令含否定或歧义，原记录未改变')
        if re.search(r'前者|后者|那个|这个|它|其中|分别', instruction):
            raise ValueError('本轮有复合指代，请直接说明要修改的行动或目标名称')
        mentioned_steps = [(g['id'], a['id']) for g in goals for a in g.get('steps', []) if normalize(a['text']) in normalize(quote)]
        if kind == 'update_action' and any(aid != target['id'] for gid, aid in mentioned_steps):
            # Identical names in distinct goals are handled by the explicit parent
            # name guard above; genuinely different actions need separate quotes.
            other_names = [a['text'] for g in goals for a in g.get('steps', []) if a['id'] != target['id'] and normalize(a['text']) != normalize(title)]
            if any(normalize(name) in normalize(quote) for name in other_names):
                raise ValueError('一句中有多条行动，无法确定更新对象；请分别明确说明')
        if kind == 'update_goal' and any(g['id'] != goal['id'] and normalize(g['title']) in normalize(quote) for g in goals):
            raise ValueError('一句中有多个目标，请分别明确说明目标状态')
        if kind == 'update_goal' and change == 'complete' and mentioned_steps:
            raise ValueError('完成某条行动不代表整个目标完成，目标状态未改变')
        if change == 'complete':
            label = re.escape(title)
            modifiers = r'(?:已|已经|我|今天|刚刚|刚|刚才|现在|终于|全部|都|顺利|[，,:：\s])*'
            completed = r'(?:完成|做完|搞定|达成|发布)(?:了)?'
            if not re.search(label + modifiers + completed + r'|' + completed + r'(?:了|这项|这个|了我的|我的|[\s])*' + label, quote):
                raise ValueError('完成陈述未明确对应这条记录，原记录未改变')
        base={'key':item.get('key'),'kind':'action' if kind=='update_action' else 'goal','id':target['id'],'title':title,'source_quote':quote,'explicit':True,'adopted':True}
        if kind=='update_action':base.update(goal_id=goal['id'],goal_rev=goal['rev'])
        else:base['rev']=goal['rev']
        if change=='complete':
            if not re.search(r'完成(?:了)?|做完(?:了)?|搞定|已发布|发布了|达成(?:了)?',quote) or re.search(r'还没|没有|未完成|没做完|没完成|不算|别.*(?:完成|标记)|不要|差点|差一点|险些|准备|打算|计划|预计|即将|完成后|做完后|一旦|等.*完成',intent):raise ValueError('未找到明确完成陈述，原记录未改变')
            if kind=='update_action':base.update(done=True,outcome='本人自报：'+quote)
            else:base.update(status='done',evidence='本人自报：'+quote)
        elif change=='reopen' and kind=='update_action':
            if not re.search(r'没完成|没做完|未完成|没有完成|撤销完成|恢复待做|重新打开|还没做',quote) or re.search(r'不要|别|不需要|无需|不必|不想|不恢复|不撤销|不重新',intent):raise ValueError('未找到明确恢复待做指令')
            base.update(done=False,outcome='本人更正：'+quote)
        elif change=='schedule' and kind=='update_action':
            date_text=item.get('date_text','')
            if not isinstance(date_text,str) or not date_text or date_text not in quote:raise ValueError('排期日期必须来自本人原话')
            if not re.search(r'安排|排到|改到|放到|计划|今天.*做|明天.*做|后天.*做',quote) or re.search(r'别|不要|不安排|不做|没决定',intent):raise ValueError('未找到明确排期指令')
            date_mentions = set(re.findall(r'今天|明天|后天|\d{4}-\d{2}-\d{2}', quote))
            if date_mentions != {date_text}:
                raise ValueError('这句中有多个日期，请明确本次要安排的单个日期')
            if date_text in ('今天','明天','后天'):
                day=dt.datetime.fromisoformat(job['created_at']).astimezone(ZoneInfo(self.settings.get('timezone','Asia/Shanghai'))).date()
                due=day+dt.timedelta(days={'今天':0,'明天':1,'后天':2}[date_text])
            else:
                due=dt.date.fromisoformat(date_text)
                if due.isoformat()!=date_text:raise ValueError('日期需要明确的YYYY-MM-DD')
            base['due']=due.isoformat()
        elif kind=='update_goal' and change in ('pause','resume'):
            pattern=r'暂停|先停|暂时停' if change=='pause' else r'继续|恢复|重新推进'
            if not re.search(pattern,quote) or re.search(r'别|不要|不想|不暂停|不恢复',intent):raise ValueError('目标状态指令不明确')
            base['status']='paused' if change=='pause' else 'active'
        else:raise ValueError('不支持这种已有记录更新')
        return base

    def organize_reply(self, job_id):
        """Retry stored extraction without calling the model again."""
        job = self.store.get(job_id, 'job')
        if job.get('status') != 'completed':
            raise ValueError('这轮讨论尚未完成，不能整理')
        if not job.get('organize'):
            raise ValueError('这轮没有开启对话整理')
        if 'capture_items' not in job:
            raise ValueError('这轮回复没有有效整理结构，原对话已保留')
        if re.search(r'(?:只|仅)(?:想|是)?(?:讨论|聊聊|聊天|聊一下)|(?:不|不要|别|不必|不用)(?:再|自动|先)?(?:记录|保存|写入|入库|整理|记下来)|先别存',job['message']):
            return self.store.change(job_id,'job',capture_status='empty',capture_error='',capture_receipt=None)
        if not job['capture_items']:
            return self.store.change(job_id, 'job', capture_status='empty', capture_error='', capture_receipt=None)
        try:
            # Existing notes/goals are only changed by explicit ID/revision bridge calls,
            # never by an ID generated by the model in conversational mode.
            allowed = {'key','kind','source_quote','title','why','due','steps','explicit','adopted','body','index','goal_ref','criterion'}
            items = []
            for item in job['capture_items']:
                if isinstance(item,dict) and item.get('kind') in ('update_action','update_goal'):
                    items.append(self.prepare_existing_update(job,item));continue
                if not isinstance(item, dict) or set(item) - allowed:
                    raise ValueError('整理条目包含不支持的字段，未自动应用')
                item = dict(item)
                if item.get('kind') in ('goal','action'):
                    normalized=lambda x:re.sub(r'[\W_]+','',x,flags=re.UNICODE)
                    if item.get('adopted') and (normalized(item.get('title','')) not in normalized(item.get('source_quote','')) or re.search(r'如果|假如|假设|比如|例如|也许|先看看|(?:还|尚)?没(?:有)?决定|未决定|尚未采用|暂不采用',job['message'])):
                        item['adopted']=False;item['explicit']=False
                    if item.get('due') and item['due'] not in job['message']:
                        item['due']=''
                if item.get('kind') == 'note':
                    item['index'] = True
                if item.get('kind') == 'goal':
                    # Only real work changes completion. Never accept model-made results.
                    for step in item.get('steps', []):
                        if not isinstance(step, dict) or set(step) - {'text','due','criterion','done'} or step.get('done'):
                            raise ValueError('整理不能生成已完成的行动或改写既有行动')
                steps=item.pop('steps',[]) if item.get('kind')=='goal' else []
                items.append(item)
                for i,step in enumerate(steps):
                    items.append({'key':item['key']+'-step-'+str(i),'kind':'action','source_quote':item['source_quote'],'title':step['text'],'goal_ref':item['key'],'due':step.get('due',''),'criterion':step.get('criterion',''),'explicit':False,'adopted':False})
            receipt = self.capture({'request_id':'chat-'+job_id,'source':'workbench_conversation',
                'source_text':job['message'],'source_id':'job:'+job_id,'source_time':job['created_at'],'items':items})
            status = 'saved' if receipt.get('status') in ('completed','saved') else 'partial'
            return self.store.change(job_id,'job',capture_status=status,capture_error='',capture_receipt=receipt)
        except Exception as error:
            return self.store.change(job_id,'job',capture_status='failed',capture_error=str(error)[:400])

    def completed_reply(self, job, raw):
        answer = raw.strip()
        fields = {}
        if job.get('organize'):
            try:
                answer, items = parse_reply(raw)
                fields = {'capture_items':items,'capture_status':'pending','capture_error':''}
            except (ValueError, TypeError):
                fields = {'capture_status':'failed','capture_error':'回复已保留，但整理格式未通过；未自动创建目标或记录。'}
        saved = self.store.change(job['id'],'job',status='completed',response=answer,raw_response=raw,**fields)
        self.store.put('message',{'session_id':job['session_id'],'role':'assistant','text':answer,'status':'completed','job_id':job['id']})
        if 'capture_items' in fields:
            saved = self.organize_reply(job['id'])
        return saved
