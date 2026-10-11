import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_workspace import CodexWorkspace, WorkspaceProtocolError, explicit_write_intent, TOOLS, safe_command

SERVER = r'''
import json,sys,pathlib
root=pathlib.Path(sys.argv[1]); mode=sys.argv[2]
def read():
 line=sys.stdin.readline()
 if not line: raise SystemExit
 obj=json.loads(line)
 with (root/'requests.jsonl').open('a') as f:f.write(json.dumps(obj)+'\n')
 return obj
def send(obj):print(json.dumps(obj),flush=True)
def reply(id,result):send({'id':id,'result':result})
def emit(method,**params):send({'method':method,'params':params})
init=read()
if mode=='bad_json':
 print('not-json',flush=True);sys.exit()
reply(init['id'],{})
read()
request=read(); params=request['params']; state=root/'thread.json'
if request['method']=='thread/start':
 assert params['ephemeral'] is False
 assert params['sandbox']=='read-only'
 assert params['dynamicTools'][0]['type']=='function'
 state.write_text(json.dumps({'id':'thread-1','turns':0,'tools':params['dynamicTools']}))
else:
 assert request['method']=='thread/resume'
 assert 'dynamicTools' not in params
 assert params['threadId']=='thread-1'
 assert state.exists()
info=json.loads(state.read_text())
model='unexpected-model' if mode=='wrong_model' else 'test-model'
reply(request['id'],{'thread':{'id':'thread-1'},'model':model})
request=read(); assert request['method']=='turn/start'
info['turns']+=1; state.write_text(json.dumps(info)); tid='turn-'+str(info['turns'])
reply(request['id'],{'turn':{'id':tid}})
emit('turn/started',threadId='thread-1',turn={'id':tid,'status':'inProgress'})
if mode=='cancel':
 request=read();assert request['method']=='turn/interrupt'
 reply(request['id'],{})
 emit('turn/completed',threadId='thread-1',turn={'id':tid,'status':'interrupted'})
 sys.exit()
if mode=='unknown_request':
 send({'id':100,'method':'item/commandExecution/requestApproval','params':{'threadId':'thread-1','turnId':tid}})
 read();sys.exit()
if mode=='tool' or mode.startswith('write') or mode in ('duplicate','wrong_turn','bad_args','foreign_namespace'):
 if mode.startswith('write'):
  args={'content':'model generated summary','expected_sha256':'a'*64}; name='write_current'
 elif mode=='bad_args':
  args={'query':'x','danger':'/etc/passwd'};name='knowledge_search'
 else:args={'document_id':'advisors:naval/references/wealth.md','offset':0,'limit':100};name='knowledge_read'
 call={'id':100,'method':'item/tool/call','params':{'threadId':'thread-1','turnId':('wrong' if mode=='wrong_turn' else tid),'callId':'call-1','tool':name,'arguments':args}}
 if mode=='foreign_namespace':call['params']['namespace']='shell'
 send(call); result=read()
 (root/'tool-response.json').write_text(json.dumps(result))
 if mode=='duplicate':send(dict(call,id=101));read();sys.exit()
if mode=='tool':
 send({'id':101,'method':'item/tool/call','params':{'threadId':'thread-1','turnId':tid,'callId':'call-2','tool':'knowledge_tree','arguments':{'root_id':'advisors','path':'naval/references'}}})
 read()
 send({'id':102,'method':'item/tool/call','params':{'threadId':'thread-1','turnId':tid,'callId':'call-3','tool':'knowledge_search','arguments':{'query':'财富','root_id':'advisors','limit':4}}})
 read()
emit('item/started',threadId='thread-1',turnId=tid,item={'type':'reasoning','id':'r','content':[{'text':'HIDDEN-REASONING'}]})
emit('item/reasoning/textDelta',threadId='thread-1',turnId=tid,delta='HIDDEN-REASONING')
emit('item/reasoning/summaryTextDelta',threadId='thread-1',turnId=tid,itemId='r',summaryIndex=0,delta='核对资料来源')
emit('item/started',threadId='thread-1',turnId=tid,item={'type':'agentMessage','id':'c','phase':'commentary','text':''})
emit('item/agentMessage/delta',threadId='thread-1',turnId=tid,itemId='c',delta='正在查阅资料')
emit('item/started',threadId='thread-1',turnId=tid,item={'type':'agentMessage','id':'a','phase':'final','text':''})
text='回答第'+str(info['turns'])+'轮'
if mode != 'no_delta':emit('item/agentMessage/delta',threadId='thread-1',turnId=tid,itemId='a',delta=text)
emit('item/completed',threadId='thread-1',turnId=tid,item={'type':'agentMessage','id':'a','phase':'final','text':text})
if mode=='early_exit':sys.exit()
emit('turn/completed',threadId='thread-1',turn={'id':tid,'status':'completed'})
'''


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.server=self.root/'server.py';self.server.write_text(SERVER)
        self.events=[];self.calls=[]
    def tearDown(self):self.tmp.cleanup()
    def run_fake(self,mode='normal',**kw):
        runner=CodexWorkspace('unused',self.root,command=[sys.executable,str(self.server),str(self.root),mode])
        def tool(name,args):
            self.calls.append((name,args))
            return {'verified':True,'sha256':'b'*64,'document_id':args.get('document_id'),'lines':['source material']}
        defaults=dict(tool_call=tool,on_event=self.events.append)
        defaults.update(kw)
        message=defaults.pop('message','请核对资料')
        return runner.run_turn(message,'test-model',**defaults)
    def requests(self):return [json.loads(x) for x in (self.root/'requests.jsonl').read_text().splitlines()]
    def test_two_turns_resume_same_persisted_thread(self):
        a=self.run_fake();b=self.run_fake(thread_id=a['thread_id'])
        self.assertEqual((a['text'],b['text']),('回答第1轮','回答第2轮'))
        self.assertEqual(b['thread_id'],'thread-1')
        req=self.requests();methods=[x.get('method') for x in req]
        self.assertEqual(methods.count('thread/start'),1);self.assertEqual(methods.count('thread/resume'),1)
        self.assertEqual(methods.count('turn/start'),2)
        self.assertTrue(req[0]['params']['capabilities']['experimentalApi'])
    def test_tools_are_multiple_dynamic_calls_with_real_receipts(self):
        result=self.run_fake('tool')
        self.assertEqual([n for n,_ in self.calls],['knowledge_read','knowledge_tree','knowledge_search'])
        self.assertEqual(len(result['tools']),3)
        response=json.loads((self.root/'tool-response.json').read_text())['result']
        self.assertTrue(response['success']);self.assertEqual(response['contentItems'][0]['type'],'inputText')
        self.assertEqual(sum(e['type']=='tool' for e in self.events),6)
    def test_only_public_summary_and_commentary(self):
        result=self.run_fake()
        self.assertNotIn('HIDDEN-REASONING',json.dumps(self.events))
        self.assertNotIn('正在查阅',result['text'])
        self.assertTrue(any(e['type']=='summary' for e in self.events))
        self.assertTrue(any(e['type']=='delta' for e in self.events))
    def test_commentary_events_include_stable_item_identity_for_ui_coalescing(self):
        self.run_fake()
        events=[e for e in self.events if e.get('channel')=='commentary']
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['type'],'progress')
        self.assertEqual(events[0]['item_id'],'c')
        self.assertEqual(events[0]['text'],'正在查阅资料')

    def test_final_only_message_preserved(self):
        result=self.run_fake('no_delta')
        self.assertEqual(result['text'],'回答第1轮')
        self.assertTrue(any(e['type']=='message' for e in self.events))
    def test_write_authority_and_current_document_binding(self):
        result=self.run_fake('write',message='把刚才讨论保存到当前笔记',write_authorized=True,current_document_id='notes:current.md')
        self.assertEqual(self.calls[0][1]['document_id'],'notes:current.md')
        self.assertEqual(result['tools'][0]['status'],'completed')
        self.assertNotIn('content',next(e for e in self.events if e['type']=='tool')['args'])
    def test_write_refused_without_per_turn_authority(self):
        result=self.run_fake('write',message='保存到当前笔记',current_document_id='notes:x')
        self.assertEqual(self.calls,[]);self.assertEqual(result['tools'][0]['status'],'failed')
    def test_write_refused_without_current_explicit_intent(self):
        for message in ['你好','不要保存到当前笔记','假如我要求保存到当前笔记','“保存到当前笔记”是什么意思']:
            result=self.run_fake('write',message=message,write_authorized=True,current_document_id='notes:x')
            self.assertEqual(result['tools'][0]['status'],'failed')
        self.assertEqual(self.calls,[])
    def test_resume_cannot_reuse_previous_write_authority(self):
        first=self.run_fake('write',message='保存到当前笔记',write_authorized=True,current_document_id='notes:x')
        self.calls.clear()
        second=self.run_fake('write',thread_id=first['thread_id'],message='继续聊聊',current_document_id='notes:x')
        self.assertEqual(self.calls,[]);self.assertEqual(second['tools'][0]['status'],'failed')
    def test_write_requires_verified_receipt(self):
        result=self.run_fake('write',message='保存到当前笔记',write_authorized=True,current_document_id='notes:x',tool_call=lambda n,a:{'verified':False})
        self.assertEqual(result['tools'][0]['status'],'failed')
    def test_unknown_args_and_namespace_never_execute(self):
        for mode in ('bad_args','foreign_namespace'):
            result=self.run_fake(mode);self.assertEqual(result['tools'][0]['status'],'failed')
        self.assertEqual(self.calls,[])
    def test_cross_turn_tool_request_rejected(self):
        with self.assertRaisesRegex(WorkspaceProtocolError,'不属于'):self.run_fake('wrong_turn')
        self.assertEqual(self.calls,[])
    def test_duplicate_tool_never_reexecuted(self):
        with self.assertRaisesRegex(WorkspaceProtocolError,'重复'):self.run_fake('duplicate')
        self.assertEqual(len(self.calls),1)
    def test_unexpected_native_tool_request_rejected(self):
        with self.assertRaisesRegex(WorkspaceProtocolError,'未开放'):self.run_fake('unknown_request')
        self.assertEqual(self.calls,[])
    def test_model_mismatch_stops_before_private_question(self):
        with self.assertRaisesRegex(WorkspaceProtocolError,'实际模型'):self.run_fake('wrong_model')
        self.assertFalse(any(x.get('method')=='turn/start' for x in self.requests()))
    def test_malformed_protocol_not_success(self):
        with self.assertRaisesRegex(WorkspaceProtocolError,'JSON'):self.run_fake('bad_json')
    def test_early_exit_not_success(self):
        with self.assertRaisesRegex(WorkspaceProtocolError,'完成回执'):self.run_fake('early_exit')
    def test_cancel_sends_interrupt_and_preserves_thread(self):
        cancel=threading.Event()
        def event(e):
            self.events.append(e)
            if e['type']=='progress' and 'turn_id' in e:cancel.set()
        result=self.run_fake('cancel',cancel_event=cancel,on_event=event)
        self.assertEqual(result['status'],'cancelled');self.assertEqual(result['thread_id'],'thread-1')
        self.assertTrue(any(x.get('method')=='turn/interrupt' for x in self.requests()))
    def test_cancel_before_start_has_no_subprocess(self):
        cancel=threading.Event();cancel.set();result=self.run_fake(cancel_event=cancel)
        self.assertEqual(result['status'],'cancelled');self.assertFalse((self.root/'requests.jsonl').exists())
    def test_timeout_is_failure(self):
        with self.assertRaisesRegex(WorkspaceProtocolError,'超时'):self.run_fake('cancel',timeout=.1)
    def test_write_intent_negative_questions_reported_and_conditional_are_rejected(self):
        messages = [
            '不保存到当前笔记，只讨论', '不用再保存到当前笔记', '暂时不需要自动写回当前文档',
            '没有让你修改当前笔记', '未授权你更新当前笔记', '别再替换当前文档',
            '无需自动更新当前笔记', '取消保存到当前笔记', '停止写回当前文档',
            '如何保存到当前笔记', '能否保存到当前笔记', '保存到当前笔记吗',
            '请保存到当前笔记？', '要不要修改当前笔记', '怎么修改当前笔记',
            '昨天我说保存到当前笔记', '我上次要求保存到当前笔记',
            '他说保存到当前笔记', '比如保存到当前笔记', '如果可以就保存到当前笔记',
            '“保存到当前笔记”是什么意思', "'保存到当前笔记'是什么意思",
            '『保存到当前笔记』', '```保存到当前笔记```',
            '只是想讨论保存到当前笔记的方式', '不是让你保存到当前笔记',
            '保存到当前笔记，先别执行', '保存到当前笔记这个功能很慢',
            '我想了解保存到当前笔记的流程', '保存到当前笔记这句话是什么意思',
        ]
        for message in messages:
            with self.subTest(message=message):
                self.assertFalse(explicit_write_intent(message))

    def test_write_intent_current_direct_requests_still_work(self):
        for message in ['帮我保存到当前笔记', '请将讨论结果写回当前文档',
                        '把刚才讨论整理到当前笔记', '更新当前文档，补充今天的计划',
                        '我要保存到当前笔记', '我想把讨论结果保存到当前笔记',
                        '把之前的想法整理进这篇笔记', '修改当前笔记的错别字',
                        '保存这段内容到当前笔记：“我不需要证明自己”']:
            with self.subTest(message=message):
                self.assertTrue(explicit_write_intent(message))

    def test_dispatch_host_enabled_but_native_execution_stays_disabled(self):
        with patch.dict(os.environ, {'CODEX_HOME':str(self.root)}):
            command=safe_command('codex')
        config={part.split('=',1)[0]:part.split('=',1)[1] for part in command if '=' in part}
        self.assertEqual(config['features.code_mode_host'],'true')
        for name in ['code_mode','shell_tool','unified_exec','js_repl','browser_use','apps','plugins']:
            self.assertEqual(config['features.'+name],'false')
        self.assertEqual(config['sandbox_mode'],'"read-only"')
    def test_tool_schema_is_fixed_and_does_not_offer_arbitrary_paths(self):
        self.assertEqual([x['name'] for x in TOOLS],['knowledge_search','knowledge_tree','knowledge_read','write_current','select_advisor','create_growth_note','update_growth_goal','local_action'])
        write=next(x['inputSchema'] for x in TOOLS if x['name']=='write_current');self.assertEqual(set(write['properties']),{'content','expected_sha256'})
        self.assertFalse(write['additionalProperties'])

    def test_growth_write_intent_and_adoption_boundaries(self):
        from codex_workspace import explicit_growth_intent, explicit_goal_intent, _validated_args
        for message in ['保存我的想法','帮我记下这个想法','把这次复盘整理到知识库']:
            self.assertTrue(explicit_growth_intent(message),message)
        for message in ['不要保存想法','他说“保存想法”','如何保存想法？','保存到 /tmp/a.md','保存当前笔记']:
            self.assertFalse(explicit_growth_intent(message),message)
        self.assertTrue(explicit_goal_intent('请采纳这个目标','active'))
        self.assertTrue(explicit_goal_intent('请更新目标的截止日期','active','active'))
        self.assertFalse(explicit_goal_intent('请更新目标的截止日期','paused','active'))
        self.assertTrue(explicit_goal_intent('请暂停这个目标','paused','active'))
        self.assertFalse(explicit_goal_intent('请修改目标','draft','active'))
        self.assertTrue(explicit_goal_intent('请把目标退回候选','draft','active'))
        for message in ['只是讨论，不要采纳','他说采纳这个目标','如果采纳这个目标','请修改这个目标']:
            self.assertFalse(explicit_goal_intent(message,'active'),message)
        with self.assertRaises(ValueError):_validated_args('local_action',{'action':'shell','user_quote':'运行shell'})
        with self.assertRaises(ValueError):_validated_args('create_growth_note',{'kind':'script','title':'x','content':'x'})

    def test_natural_goal_commitments_are_not_confused_with_plans_or_reports(self):
        from codex_workspace import explicit_goal_intent
        for text in ['我采用刚才这个目标，安排今天执行。','我决定执行这个目标','我决定采用这个目标','我正式采纳这个计划']:
            self.assertTrue(explicit_goal_intent(text,'active','draft'),text)
        for text in ['我已完成这个目标，证据在记录里','我已经完成这个目标','我完成了这个目标','请把这个目标标记为已完成']:
            self.assertTrue(explicit_goal_intent(text,'done','active'),text)
        for text in ['今天完成10分钟练习','把今天完成10分钟练习保存到目标里','请把明天完成练习整理到目标','我计划明天完成这个目标','我打算完成这个目标']:
            self.assertFalse(explicit_goal_intent(text,'done','active'),text)
        for text in ['“我采用这个目标”','我不采用这个目标','我采用这个目标吗？','昨天我采用这个目标','我之前说过我采用这个目标','分析以下原话：\n我采用这个目标','昨天我采用这个目标。请保存讨论记录']:
            self.assertFalse(explicit_goal_intent(text,'active','draft'),text)
        for text in ['“我已完成这个目标”','我没完成这个目标','我已完成这个目标吗？','我昨天完成了这个目标','分析以下原话：\n我已完成这个目标','他说我已完成这个目标']:
            self.assertFalse(explicit_goal_intent(text,'done','active'),text)

if __name__=='__main__':unittest.main()
