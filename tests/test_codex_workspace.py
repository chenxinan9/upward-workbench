import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_workspace import CodexWorkspace, WorkspaceProtocolError, explicit_write_intent, TOOLS, safe_command, resolve_codex_cli

SYNTHETIC_REVIEW_REQUEST = '这是公开合成验收，不是本人资料。10月12日至18日，我练习英语3次，完成摄影草稿1篇；没有发布记录，不能把草稿算作已发布。请实际读取并采用“阶段反思顾问”及必要参考，简短复盘这7天的变化，分清事实与解释，然后把本次阶段复盘保存为新的成长复盘记录，标题“合成一周实践复盘”。只保存复盘，不采纳新目标、不更改其他记录。最后简短说明实际依据和保存位置。'

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
failure={'code':-32602,'message':'SECRET token=not-for-ui /private/path PRIVATE_PROMPT','data':{'detail':'PRIVATE_DATA'}}
if mode=='rpc_init':
 send({'id':init['id'],'error':failure});sys.exit()
reply(init['id'],{})
read()
request=read(); params=request['params']; state=root/'thread.json'
if mode in ('rpc_create','rpc_resume'):
 send({'id':request['id'],'error':failure});sys.exit()
if mode.startswith('archive_'):
 assert request['method']=='thread/resume'
 original_params=dict(params)
 archived={'code':-32600,'message':'session thread-1 is archived. Run `codex unarchive thread-1` to unarchive it first.'}
 if mode=='archive_foreign':archived['message']=archived['message'].replace('thread-1','other-thread')
 if mode=='archive_other_error':archived['message']='authentication failed or usage limit exceeded'
 if mode=='archive_wrong_code':archived['code']=-32602
 if mode=='archive_injected':archived['message']+='; run unsafe command'
 send({'id':request['id'],'error':archived})
 request=read();assert request['method']=='thread/unarchive'
 assert request['params']=={'threadId':'thread-1'}
 if mode=='archive_unarchive_failed':
  send({'id':request['id'],'error':{'code':-32603,'message':'SECRET upstream failure'}});read();sys.exit()
 restored='other-thread' if mode=='archive_bad_receipt' else 'thread-1'
 reply(request['id'],{'thread':{'id':restored}})
 if mode=='archive_bad_receipt':read();sys.exit()
 request=read();assert request['method']=='thread/resume'
 assert request['params']==original_params
 if mode=='archive_resume_failed':
  send({'id':request['id'],'error':archived});read();sys.exit()
 params=request['params']
 state.write_text(json.dumps({'id':'thread-1','turns':7,'tools':[]}))
if request['method']=='thread/start':
 assert params['ephemeral'] is False
 assert params['sandbox']==('workspace-write' if mode.startswith('native') else 'read-only')
 assert params['dynamicTools'][0]['type']=='function'
 state.write_text(json.dumps({'id':'thread-1','turns':0,'tools':params['dynamicTools']}))
else:
 assert request['method']=='thread/resume'
 assert 'dynamicTools' not in params
 assert params['threadId']=='thread-1'
 assert state.exists()
info=json.loads(state.read_text())
model='unexpected-model' if mode in ('wrong_model','archive_wrong_model') else 'test-model'
reply(request['id'],{'thread':{'id':'thread-1'},'model':model})
request=read(); assert request['method']=='turn/start'
if mode=='rpc_turn':
 send({'id':request['id'],'error':failure});sys.exit()
info['turns']+=1; state.write_text(json.dumps(info)); tid='turn-'+str(info['turns'])
reply(request['id'],{'turn':{'id':tid}})
emit('turn/started',threadId='thread-1',turn={'id':tid,'status':'inProgress'})
if mode in ('slow_progress','heartbeat_only','private_progress'):
 import time
 for _ in range(7):
  if mode=='slow_progress':emit('item/reasoning/summaryTextDelta',threadId='thread-1',turnId=tid,itemId='reason',delta='checking ')
  elif mode=='private_progress':emit('item/reasoning/textDelta',threadId='thread-1',turnId=tid,itemId='reason',delta='PRIVATE_SENTINEL')
  else:emit('unrelated/heartbeat',threadId='thread-1')
  time.sleep(.12)
if mode=='cancel':
 request=read();assert request['method']=='turn/interrupt'
 reply(request['id'],{})
 emit('turn/completed',threadId='thread-1',turn={'id':tid,'status':'interrupted'})
 sys.exit()
if mode=='unknown_request':
 send({'id':100,'method':'item/commandExecution/requestApproval','params':{'threadId':'thread-1','turnId':tid}})
 read();sys.exit()
if mode.startswith('native'):
 emit('item/started',threadId='thread-1',turnId=tid,item={'type':'commandExecution','id':'exec-1','status':'inProgress','command':'printf result','cwd':str(root)})
 emit('item/commandExecution/outputDelta',threadId='thread-1',turnId=tid,itemId='exec-1',delta='actual stream')
 method='item/commandExecution/requestApproval'
 approval_params={'threadId':'thread-1','turnId':tid,'itemId':'exec-1','command':'printf result'}
 if mode=='native_input':
  method='item/tool/requestUserInput';approval_params['questions']=[{'id':'q1','header':'选择','question':'保存哪一项','options':[{'label':'一','description':'第一项'}]}]
 if mode=='native_mcp':
  method='mcpServer/elicitation/request';approval_params.update(serverName='test',mode='form',message='输入',requestedSchema={'type':'object','properties':{}})
 if mode=='native_permissions':
  method='item/permissions/requestApproval';approval_params['permissions']={'network':{'enabled':True}}
 if mode=='native_wrong_turn':approval_params['turnId']='wrong-turn'
 call={'id':'approval-1','method':method,'params':approval_params}
 send(call)
 emit('item/reasoning/summaryTextDelta',threadId='thread-1',turnId=tid,itemId='r',summaryIndex=0,delta='审批期间仍有真实流')
 if mode=='native_resolved':
  emit('serverRequest/resolved',threadId='thread-1',requestId='approval-1')
 else:
  result=read();(root/'approval-response.json').write_text(json.dumps(result))
  if mode=='native_cancel':
   assert result['method']=='turn/interrupt'
   reply(result['id'],{})
   emit('turn/completed',threadId='thread-1',turn={'id':tid,'status':'interrupted'});sys.exit()
  if mode=='native_duplicate':send(call);read();sys.exit()
 emit('item/completed',threadId='thread-1',turnId=tid,item={'type':'commandExecution','id':'exec-1','status':'completed','command':'printf result','exitCode':0,'aggregatedOutput':'actual stream'})
 for kind in ['webSearch','mcpToolCall','imageView','fileChange']:
  emit('item/started',threadId='thread-1',turnId=tid,item={'type':kind,'id':kind,'query':'example','server':'test','tool':'lookup','changes':[],'path':'fixture.png'})
  emit('item/completed',threadId='thread-1',turnId=tid,item={'type':kind,'id':kind,'status':'completed'})
if mode=='tool' or mode.startswith('write') or mode in ('duplicate','wrong_turn','bad_args','foreign_namespace','growth_create','artifact_create'):
 if mode=='artifact_create':
  args={'title':'合成网页','format':'html','content':'<h1>合成正文</h1>'};name='create_workspace_artifact'
 elif mode=='growth_create':
  args={'kind':'review','title':'合成一周实践复盘','content':'事实：练习3次，草稿1篇。建议尚未采纳。'};name='create_growth_note'
 elif mode.startswith('write'):
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


class CliResolutionTests(unittest.TestCase):
    def resolve(self, versions, configured=None):
        def probe(args, **kw):
            result = versions[args[0]]
            if isinstance(result, Exception): raise result
            return subprocess.CompletedProcess(args, 0, result)
        with patch('codex_workspace.sys.platform', 'darwin'), \
             patch('codex_workspace.shutil.which', return_value='/path/codex'), \
             patch('codex_workspace.Path.is_file', lambda path: str(path) in versions), \
             patch('codex_workspace.os.access', return_value=True), \
             patch('codex_workspace.subprocess.run', side_effect=probe) as run:
            result = resolve_codex_cli(configured)
            return result, run.call_count

    def test_explicit_cli_is_never_replaced_or_probed(self):
        self.assertEqual(self.resolve({}, '/chosen/codex'), ('/chosen/codex', 0))

    def test_newer_official_bundle_used_for_both_catalog_and_conversation(self):
        bundle = '/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex'
        self.assertEqual(self.resolve({'/path/codex':'codex-cli 0.153.4\n',
            bundle:'codex-cli 0.162.0-alpha.17.2\n'})[0], bundle)
        self.assertEqual(self.resolve({'/path/codex':'codex-cli 0.163.0\n',
            bundle:'codex-cli 0.162.0-alpha.17.2\n'})[0], '/path/codex')

    def test_unreadable_or_malformed_bundle_cannot_displace_path(self):
        bundle = '/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex'
        for value in ('not a Codex version', subprocess.TimeoutExpired(bundle,2)):
            with self.subTest(value=type(value).__name__):
                self.assertEqual(self.resolve({'/path/codex':'codex-cli 0.153.4\n',bundle:value})[0], '/path/codex')
        self.assertIsNone(self.resolve({bundle:'unrecognized'})[0])
        self.assertIsNone(self.resolve({})[0])

    def test_prerelease_serial_stage_and_stable_precedence(self):
        bundle = '/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex'
        cases = [
            ('0.162.0-alpha.17.2','0.162.0-alpha.18.0',bundle),
            ('0.162.0-alpha.17.10','0.162.0-alpha.17.2','/path/codex'),
            ('0.162.0-alpha.99','0.162.0-beta.1',bundle),
            ('0.162.0-beta.99','0.162.0-rc.1',bundle),
            ('0.162.0-rc.99','0.162.0',bundle),
            ('0.162.0','0.162.0-rc.99','/path/codex'),
            ('0.162.0','0.163.0-preview.1','/path/codex'),
        ]
        for path_version,bundle_version,expected in cases:
            with self.subTest(path=path_version,bundle=bundle_version):
                self.assertEqual(self.resolve({'/path/codex':'codex-cli '+path_version+'\n',
                    bundle:'codex-cli '+bundle_version+'\n'})[0],expected)


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
    def test_c20_before_send_refuses_real_protocol_before_turn_payload(self):
        from shared_knowledge import SharedError
        def refuse(): raise SharedError('共享范围变化，未发送')
        with self.assertRaisesRegex(SharedError,'未发送'):
            self.run_fake(before_send=refuse)
        requests=self.requests()
        self.assertTrue(any(v.get('method')=='thread/start' for v in requests))
        self.assertFalse(any(v.get('method')=='turn/start' for v in requests))
        self.assertNotIn('请核对资料',json.dumps(requests,ensure_ascii=False))
        self.assertFalse(any(v.get('type')=='thread' for v in self.events))

    def test_two_turns_resume_same_persisted_thread(self):
        a=self.run_fake();b=self.run_fake(thread_id=a['thread_id'])
        self.assertEqual((a['text'],b['text']),('回答第1轮','回答第2轮'))
        self.assertEqual(b['thread_id'],'thread-1')
        req=self.requests();methods=[x.get('method') for x in req]
        self.assertEqual(methods.count('thread/start'),1);self.assertEqual(methods.count('thread/resume'),1)
        self.assertEqual(methods.count('turn/start'),2)
        self.assertTrue(req[0]['params']['capabilities']['experimentalApi'])

    def test_rpc_failure_names_the_request_phase_without_exposing_upstream_text_or_retrying(self):
        for mode, phase, thread in [('rpc_init','建立连接',None), ('rpc_create','创建会话',None),
                                    ('rpc_resume','恢复会话','thread-1'), ('rpc_turn','开始本轮',None)]:
            with self.subTest(mode=mode):
                requests_path = self.root / 'requests.jsonl'
                requests_path.unlink(missing_ok=True)
                with self.assertRaises(WorkspaceProtocolError) as raised:
                    self.run_fake(mode, thread_id=thread)
                message = str(raised.exception)
                self.assertIn(phase, message)
                self.assertIn('-32602', message)
                self.assertIn('参数未被当前接口接受', message)
                self.assertNotIn('拒绝', message)
                for secret in ('SECRET', 'not-for-ui', '/private/path', 'PRIVATE_PROMPT', 'PRIVATE_DATA'):
                    self.assertNotIn(secret, message)
                methods = [request.get('method') for request in self.requests()]
                self.assertLessEqual(methods.count('thread/start'), 1)
                self.assertLessEqual(methods.count('thread/resume'), 1)
                self.assertLessEqual(methods.count('turn/start'), 1)

    def test_rpc_failure_only_exposes_a_bounded_integer_code(self):
        from codex_workspace import _request_error
        for error in ({'code':True}, {'code':['unsafe']}, {'code':'SECRET'}, {'code':2 ** 100},
                      {'message':'SECRET'}, 'SECRET', None):
            with self.subTest(error=error):
                text = _request_error(error, '建立连接')
                self.assertNotIn('SECRET', text)
                self.assertNotIn('代码', text)

    def test_archived_thread_is_unarchived_once_and_resumed_with_the_same_config_before_one_turn(self):
        result = self.run_fake('archive_recover', thread_id='thread-1')
        self.assertEqual(result['thread_id'], 'thread-1')
        self.assertEqual(result['actual_model'], 'test-model')
        self.assertEqual(result['text'], '回答第8轮')
        requests = self.requests()
        methods = [request.get('method') for request in requests]
        self.assertEqual(methods, ['initialize','initialized','thread/resume','thread/unarchive','thread/resume','turn/start'])
        resumes = [request for request in requests if request.get('method') == 'thread/resume']
        self.assertEqual(resumes[0]['params'], resumes[1]['params'])
        unarchive = next(request for request in requests if request.get('method') == 'thread/unarchive')
        self.assertEqual(unarchive['params'], {'threadId':'thread-1'})
        before_turn = json.dumps(requests[:-1], ensure_ascii=False)
        self.assertNotIn('请核对资料', before_turn)
        self.assertTrue(any('尚未发送本轮问题' in event.get('text','') for event in self.events))

    def test_unarchive_failure_or_second_resume_error_stops_without_starting_a_turn(self):
        for mode, phase, code in [('archive_unarchive_failed','恢复已归档会话','-32603'),
                                  ('archive_resume_failed','再次连接原会话','-32600')]:
            with self.subTest(mode=mode):
                (self.root / 'requests.jsonl').unlink(missing_ok=True)
                with self.assertRaises(WorkspaceProtocolError) as raised:
                    self.run_fake(mode, thread_id='thread-1')
                text = str(raised.exception)
                self.assertIn(phase, text)
                self.assertIn(code, text)
                self.assertNotIn('SECRET', text)
                methods = [request.get('method') for request in self.requests()]
                self.assertEqual(methods.count('thread/unarchive'), 1)
                self.assertEqual(methods.count('thread/resume'), 1 if mode=='archive_unarchive_failed' else 2)
                self.assertNotIn('turn/start', methods)
                self.assertNotIn('thread/start', methods)

    def test_only_exact_matching_archived_resume_error_can_unarchive_current_thread(self):
        for mode in ('archive_foreign','archive_other_error','archive_wrong_code','archive_injected'):
            with self.subTest(mode=mode):
                (self.root / 'requests.jsonl').unlink(missing_ok=True)
                with self.assertRaises(WorkspaceProtocolError):
                    self.run_fake(mode, thread_id='thread-1')
                methods = [request.get('method') for request in self.requests()]
                self.assertEqual(methods, ['initialize','initialized','thread/resume'])
                self.assertFalse(self.calls)

    def test_unarchive_receipt_and_resumed_model_are_verified_before_sending_user_content(self):
        for mode in ('archive_bad_receipt','archive_wrong_model'):
            with self.subTest(mode=mode):
                (self.root / 'requests.jsonl').unlink(missing_ok=True)
                with self.assertRaisesRegex(WorkspaceProtocolError, '不匹配|实际模型'):
                    self.run_fake(mode, thread_id='thread-1')
                methods = [request.get('method') for request in self.requests()]
                self.assertNotIn('turn/start', methods)
                self.assertEqual(methods.count('thread/unarchive'), 1)
                self.assertNotIn('请核对资料', json.dumps(self.requests(), ensure_ascii=False))

    def test_cancellation_before_archive_recovery_does_not_unarchive_or_start_a_turn(self):
        cancel = threading.Event()
        def progress(event):
            self.events.append(event)
            if '尚未发送本轮问题' in event.get('text',''):
                cancel.set()
        with self.assertRaisesRegex(WorkspaceProtocolError, '已停止'):
            self.run_fake('archive_recover', thread_id='thread-1', cancel_event=cancel, on_event=progress)
        methods = [request.get('method') for request in self.requests()]
        self.assertNotIn('thread/unarchive', methods)
        self.assertNotIn('turn/start', methods)

    def test_archive_recovery_still_checks_pre_send_guard_before_any_model_turn(self):
        def refuse():
            raise ValueError('合成发送前核验停止')
        with self.assertRaisesRegex(ValueError, '发送前核验停止'):
            self.run_fake('archive_recover', thread_id='thread-1', before_send=refuse)
        methods = [request.get('method') for request in self.requests()]
        self.assertEqual(methods.count('thread/unarchive'), 1)
        self.assertEqual(methods.count('thread/resume'), 2)
        self.assertNotIn('turn/start', methods)
        self.assertNotIn('请核对资料', json.dumps(self.requests(), ensure_ascii=False))

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
    def test_active_research_can_exceed_inactivity_window(self):
        result=self.run_fake('slow_progress',timeout=3,idle_timeout=.4)
        self.assertEqual(result['status'],'completed')

    def test_unrelated_heartbeat_does_not_keep_turn_alive(self):
        with self.assertRaisesRegex(WorkspaceProtocolError,'未返回新内容'):
            self.run_fake('heartbeat_only',timeout=3,idle_timeout=.3)

    def test_hard_deadline_still_bounds_active_research(self):
        with self.assertRaisesRegex(WorkspaceProtocolError,'总时限'):
            self.run_fake('slow_progress',timeout=.5,idle_timeout=.4)

    def test_invalid_timeout_never_spawns_a_process(self):
        for value in (float('inf'),float('nan'),True,0,-1):
            with self.assertRaises(ValueError):self.run_fake(timeout=value)
        self.assertFalse((self.root/'requests.jsonl').exists())

    def test_internal_reasoning_is_liveness_only_and_never_exposed(self):
        result=self.run_fake('private_progress',timeout=3,idle_timeout=.4)
        self.assertEqual(result['status'],'completed')
        self.assertNotIn('PRIVATE_SENTINEL',json.dumps([result,self.events]))

    def test_approval_wait_does_not_consume_inactivity_budget(self):
        import time
        def approve(request):
            time.sleep(.6)
            return {'decision':'accept'}
        result=self.run_fake('native',execution_mode='assistant',context={'scope':'personal'},
                             approval_callback=approve,timeout=3,idle_timeout=.3)
        self.assertEqual(result['status'],'completed')

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
        self.assertEqual([x['name'] for x in TOOLS],['knowledge_search','knowledge_tree','knowledge_read','write_current','select_advisor','create_growth_note','update_growth_goal','local_action','create_workspace_artifact','attachment_read','read_growth_plan','update_growth_plan'])
        write=next(x['inputSchema'] for x in TOOLS if x['name']=='write_current');self.assertEqual(set(write['properties']),{'content','expected_sha256'})
        self.assertFalse(write['additionalProperties'])

    def test_assistant_native_stream_and_one_shot_approval(self):
        requests=[]
        def approve(req):
            requests.append(req)
            return {'decision':'accept'}
        result=self.run_fake('native',execution_mode='assistant',context={'scope':'personal'},approval_callback=approve)
        self.assertEqual(result['execution_mode'],'assistant')
        self.assertEqual(len(result['native_tools']),5)
        response=json.loads((self.root/'approval-response.json').read_text())
        self.assertEqual(response,{'id':'approval-1','result':{'decision':'accept'}})
        self.assertEqual(requests[0]['thread_id'],'thread-1')
        self.assertEqual(requests[0]['turn_id'],'turn-1')
        self.assertTrue(any(e['type']=='native_tool' and e.get('detail',{}).get('text')=='actual stream' for e in self.events))
        params=next(x['params'] for x in self.requests() if x.get('method')=='turn/start')
        self.assertEqual(params['sandboxPolicy']['writableRoots'],[str(self.root.resolve())])
        self.assertTrue(params['sandboxPolicy']['excludeSlashTmp'])
        self.assertEqual(params['approvalPolicy'],'on-request')
        self.assertNotIn('environments',params)
        start=next(x['params'] for x in self.requests() if x.get('method')=='thread/start')
        self.assertNotIn('environments',start)

    def test_native_default_and_invalid_callback_deny(self):
        for callback in [None,lambda req:{'decision':'acceptForSession'},lambda req:{'decision':{'acceptWithExecpolicyAmendment':{'execpolicy_amendment':['sh']}}}]:
            self.run_fake('native',execution_mode='assistant',context={'scope':'personal'},approval_callback=callback)
            self.assertEqual(json.loads((self.root/'approval-response.json').read_text())['result'],{'decision':'decline'})

    def test_native_user_input_and_mcp_responses(self):
        for mode,response in [('native_input',{'answers':{'q1':{'answers':['一']}}}),('native_mcp',{'action':'decline','content':None}),('native_permissions',{'permissions':{},'scope':'turn'})]:
            self.run_fake(mode,execution_mode='assistant',context={'scope':'personal'},approval_callback=lambda req:response)
            self.assertEqual(json.loads((self.root/'approval-response.json').read_text())['result'],response)

    def test_native_cross_turn_and_duplicate_requests_rejected(self):
        for mode in ['native_wrong_turn','native_duplicate']:
            with self.assertRaises(WorkspaceProtocolError):
                self.run_fake(mode,execution_mode='assistant',context={'scope':'personal'})

    def test_approval_wait_does_not_block_stream_or_cancel(self):
        cancel=threading.Event();release=threading.Event()
        def approve(req):
            release.wait(3)
            return {'decision':'accept'}
        def event(e):
            self.events.append(e)
            if e['type']=='summary' and e['text']=='审批期间仍有真实流':cancel.set()
        try:
            result=self.run_fake('native_cancel',execution_mode='assistant',context={'scope':'personal'},approval_callback=approve,cancel_event=cancel,on_event=event)
            self.assertEqual(result['status'],'cancelled')
            self.assertFalse(any(x.get('result',{}).get('decision')=='accept' for x in self.requests()))
        finally:release.set()

    def test_pending_approval_timeout_revokes_callback(self):
        observed=[]
        def approve(req):
            observed.append(req)
            req['cancel_event'].wait(2)
            return {'decision':'accept'}
        with self.assertRaisesRegex(WorkspaceProtocolError,'超时'):
            self.run_fake('native',execution_mode='assistant',context={'scope':'personal'},approval_callback=approve,timeout=.15)
        self.assertTrue(observed)
        self.assertTrue(observed[0]['cancel_event'].is_set())
        self.assertFalse(any(x.get('result',{}).get('decision')=='accept' for x in self.requests()))

    def test_resolved_request_discards_late_decision(self):
        release=threading.Event()
        def approve(req):
            release.wait(3)
            return {'decision':'accept'}
        try:
            result=self.run_fake('native_resolved',execution_mode='assistant',context={'scope':'personal'},approval_callback=approve)
            self.assertEqual(result['status'],'completed')
            self.assertFalse(any(x.get('id')=='approval-1' and 'result' in x for x in self.requests()))
        finally:release.set()

    def test_scope_narrowing_disables_native_at_process_and_turn(self):
        for scope in ['advisors','document',None]:
            self.run_fake(execution_mode='assistant',context={'scope':scope})
            start=[x for x in self.requests() if x.get('method')=='thread/start'][-1]
            self.assertEqual(start['params']['sandbox'],'read-only')
            self.assertEqual(start['params']['approvalPolicy'],'never')
            self.assertEqual(start['params']['environments'],[])
            turn=[x for x in self.requests() if x.get('method')=='turn/start'][-1]
            self.assertEqual(turn['params']['environments'],[])

    def test_assistant_command_enables_supported_native_not_desktop_tools(self):
        (self.root/'config.toml').write_text('[mcp_servers.demo]\ncommand="example"\n[mcp_servers.demo.tools."write/file"]\napproval_mode="approve"\n')
        with patch.dict(os.environ,{'CODEX_HOME':str(self.root)}):
            native=safe_command('codex','assistant',self.root)
            restricted=safe_command('codex')
        config={part.split('=',1)[0]:part.split('=',1)[1] for part in native if '=' in part}
        self.assertEqual(config['web_search'],'"live"')
        self.assertEqual(config['features.shell_tool'],'true')
        self.assertEqual(config['features.unified_exec'],'true')
        self.assertEqual(config['features.view_image'],'true')
        self.assertEqual(config['sandbox_mode'],'"workspace-write"')
        self.assertNotIn('mcp_servers.demo.enabled',config)
        self.assertEqual(config['mcp_servers.demo.default_tools_approval_mode'],'"prompt"')
        self.assertEqual(config['mcp_servers.demo.tools."write/file".approval_mode'],'"prompt"')
        self.assertEqual(config['approvals_reviewer'],'"user"')
        self.assertIn('mcp_servers.demo.enabled=false',restricted)
        for key in ['computer_use','browser_use','in_app_browser','apps','plugins']:
            self.assertEqual(config['features.'+key],'false')

    def test_authorized_local_image_and_attachment_tool(self):
        image=self.root/'image.png';image.write_bytes(b'fixture')
        self.run_fake(input_items=[{'type':'localImage','path':str(image)}])
        turn=[x for x in self.requests() if x.get('method')=='turn/start'][-1]
        self.assertEqual(turn['params']['input'][-1],{'type':'localImage','path':str(image)})
        from codex_workspace import _validated_args
        self.assertEqual(_validated_args('attachment_read',{'attachment_id':'att-1','offset':300,'limit':100})['offset'],300)
        with self.assertRaises(ValueError):_validated_args('attachment_read',{'path':'/private'})
        with self.assertRaises(ValueError):self.run_fake(input_items=[{'type':'skill','path':'/private'}])

    def test_permission_response_cannot_expand_requested_grant(self):
        from codex_native import approval_response
        method='item/permissions/requestApproval'
        result=approval_response(method,{'permissions':{'network':{'enabled':True}}},{'permissions':{'fileSystem':{'write':['/']}}})
        self.assertEqual(result,{'permissions':{},'scope':'turn'})
        result=approval_response(method,{'permissions':{'network':{'enabled':True}}},{'permissions':{'network':{'enabled':True}},'scope':'session'})
        self.assertEqual(result['scope'],'turn')

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

    def test_scoped_current_save_request_is_not_negated_by_missing_publishing_records(self):
        from codex_workspace import explicit_growth_intent
        for message in [SYNTHETIC_REVIEW_REQUEST,
                        '没有发布记录。请保存本阶段复盘，不更改其他记录。',
                        '记录显示练习三次。请保存本次阶段复盘，不更改其他记录。',
                        '只保存这次复盘，不采纳新目标、不更改其他记录。',
                        '只记录这次想法，不更改其他文件。',
                        '记下这个想法。', '记录一下这次复盘。', '记录这次想法。',
                        '请把这次讨论记录到知识库。']:
            with self.subTest(message=message):
                self.assertTrue(explicit_growth_intent(message))
        self.assertFalse(explicit_write_intent(SYNTHETIC_REVIEW_REQUEST))
        denied = ['没有发布记录。', '记录显示我本周练习了三次。',
                  '记录里说我已经完成了计划，这是错误的。', '记录中提到我想换方向。',
                  '记录显示练习三次。请简短分析。',
                  '记下的资料显示我本周练习了三次。', '记下的内容需要分析，不需要新增笔记。',
                  '请保存这次复盘，但不要新增任何记录。', '保存复盘，不新建笔记。',
                  '不要保存复盘，也不要更改其他记录。',
                  '只保存复盘，但这轮先不要保存。', '只是讨论这次复盘，不更改其他记录。',
                  '引用“只保存复盘，不更改其他记录”。', '他说“只保存复盘，不更改其他记录”。',
                  '转述他的要求：只保存复盘，不更改其他记录。',
                  '先把复盘整理清楚，后面可能保存。', '下次再记录这次想法。',
                  '如果核对清楚，再保存复盘。', '昨天我说过只保存复盘。',
                  '先分析这个复盘，之后考虑记录到知识库。',
                  '请保存想法，不要记录复盘。', '请保存想法，先别帮我记录复盘。',
                  '保存复盘，不再记录想法。']
        for message in denied:
            with self.subTest(message=message):
                self.assertFalse(explicit_growth_intent(message))

    def test_artifact_tool_protocol_requires_verified_host_result_and_valid_schema(self):
        from codex_workspace import _validated_args
        result=self.run_fake('artifact_create',message='请生成一份可展开的网页',execution_mode='knowledge')
        self.assertEqual([name for name,args in self.calls],['create_workspace_artifact'])
        self.assertEqual(result['tools'][0]['status'],'completed')
        response=json.loads((self.root/'tool-response.json').read_text())['result']
        self.assertTrue(response['success'])
        result=self.run_fake('artifact_create',tool_call=lambda name,args:{'verified':False})
        self.assertEqual(result['tools'][0]['status'],'failed')
        response=json.loads((self.root/'tool-response.json').read_text())['result']
        self.assertFalse(response['success'])
        for args in [dict(title='x',format='exe',content='x'),
                     dict(title='x',format='text',content='x',path='/tmp/outside'),
                     dict(title='x',format='text',content='x'*262145)]:
            with self.subTest(args={k:v[:20] for k,v in args.items()}),self.assertRaises(ValueError):
                _validated_args('create_workspace_artifact',args)

    def test_goal_breakdown_is_an_explicit_update_without_new_adoption_or_completion(self):
        from codex_workspace import explicit_goal_intent
        for text in ['把合成学习目标拆成三个行动','请拆分合成学习目标','请帮我拆分合成学习目标','把合成学习目标拆成行动并保存','请把合成学习目标分解成步骤',
                     '请拆分合成学习目标，不修改其他文件。',
                     '请帮我拆分合成学习目标，不更改其他目标。']:
            self.assertTrue(explicit_goal_intent(text,'active','active'),text)
            self.assertFalse(explicit_goal_intent(text,'active','draft'),text)
            self.assertFalse(explicit_goal_intent(text,'done','active'),text)
        for text in ['不要拆分合成学习目标','他说把合成学习目标拆成三个行动','能否拆分合成学习目标？',
                     '拆分合成学习目标的结果还太粗糙，需要再讨论。','拆分后的合成学习目标只是一个草稿。',
                     '拆分不是当前要做的事。','把合成学习目标拆成三个行动的结果还太粗糙，需要再讨论。',
                     '请告诉我拆分合成学习目标后的结果','如果有时间，请拆分合成学习目标',
                     '昨天我让你拆分合成学习目标。',
                     '请拆分合成学习目标，不要修改这个目标。',
                     '请拆分合成学习目标，不修改任何文件。',
                     '请拆分合成学习目标，不做任何写入。',
                     '他说请拆分合成学习目标，不修改其他文件。',
                     '如果有空，请拆分合成学习目标，不修改其他文件。',
                     '昨天我让你拆分合成学习目标，不修改其他文件。']:
            self.assertFalse(explicit_goal_intent(text,'active','active'),text)

    def test_scoped_review_save_passes_the_real_protocol_tool_guard(self):
        result = self.run_fake('growth_create', message=SYNTHETIC_REVIEW_REQUEST)
        self.assertEqual([name for name,args in self.calls], ['create_growth_note'])
        response = json.loads((self.root/'tool-response.json').read_text())['result']
        self.assertTrue(response['success'])
        self.assertEqual(result['tools'][0]['status'], 'completed')

    def test_record_facts_cannot_pass_protocol_create_note_guard(self):
        for message in ['记录显示我本周练习了三次。', '记录里说我已经完成了计划，这是错误的。',
                        '记录中提到我想换方向。', '记录显示练习三次。请简短分析。',
                        '记下的资料显示我本周练习了三次。', '记下的内容需要分析，不需要新增笔记。',
                        '请保存这次复盘，但不要新增任何记录。']:
            with self.subTest(message=message):
                self.calls.clear()
                result = self.run_fake('growth_create', message=message)
                self.assertEqual(self.calls, [])
                response = json.loads((self.root/'tool-response.json').read_text())['result']
                self.assertFalse(response['success'])
                self.assertEqual(result['tools'][0]['status'], 'failed')

    def test_growth_create_with_other_file_protection_does_not_authorize_replacement(self):
        from codex_workspace import explicit_growth_intent
        messages = [
            '把刚才的阶段复盘保存为一篇阶段复盘，标题为“合成验收阶段复盘”，建议仍明确标注为未采纳建议，只保存这一篇，不修改其他目标或文件。',
            '保存复盘，不修改其他目标或文件',
            '请保存这次想法，不要改写其它文档和笔记。',
            '不要修改其他文件，帮我保存这次复盘。',
        ]
        for message in messages:
            with self.subTest(message=message):
                self.assertTrue(explicit_growth_intent(message))
                self.assertFalse(explicit_write_intent(message))
        denied = [
            '不要保存复盘，也不要修改其他文件',
            '保存复盘，不修改任何文件',
            '保存复盘，不做任何写入',
            '保存复盘，不修改其他目标或文件，但先不执行',
            '保存复盘，不修改其他文件，只是讨论',
            '保存复盘，不修改其他文件？',
            '如果确认无误，保存复盘，不修改其他文件',
            '昨天我要求保存复盘，不修改其他文件',
            '他说保存复盘，不修改其他文件',
            '“保存复盘，不修改其他文件”',
            '```保存复盘，不修改其他文件```',
            '保存到当前笔记，不修改其他目标',
            '保存到当前文件，不修改其他目标',
            '更新阶段目标，不修改其他文件',
            '保存复盘并覆盖旧稿，不修改其他文件',
            '保存到 /tmp/review.md，不修改其他文件',
            '不要修改其他目标或文件',
            '请保存复盘，不修改其他文件但不要保存',
        ]
        for message in denied:
            with self.subTest(message=message): self.assertFalse(explicit_growth_intent(message))

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
