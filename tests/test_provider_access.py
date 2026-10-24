import http.client
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import provider_access as providers
from app import Desk
from model_access import ModelAccessMixin

KEY='sk-'+'synthetic-test-only-0123456789'


class Response(io.BytesIO):
    status=200
    def __init__(self,status=200):super().__init__(b'data: {"type":"response.completed"}\n\n');self.status=status


class Upstream:
    def __init__(self,host,timeout):self.host=host;self.timeout=timeout;self.sock=None;self.calls=[];self.closed=False;self.status=200
    def request(self,*args,**kwargs):self.calls.append((args,kwargs))
    def getresponse(self):return Response(self.status)
    def close(self):self.closed=True


class ProviderTests(unittest.TestCase):
    def request(self,relay,*,path='/responses',token=None,host=None,origin=None,model='deepseek-flash',method='POST'):
        connection=http.client.HTTPConnection('127.0.0.1',relay.server.server_port,timeout=3)
        headers={'Authorization':'Bearer '+(relay.token if token is None else token),'Content-Type':'application/json'}
        if host:headers['Host']=host
        if origin:headers['Origin']=origin
        connection.request(method,path,json.dumps({'model':model,'input':'synthetic public fixture'}),headers)
        response=connection.getresponse();result=(response.status,response.read());connection.close();return result

    def test_relay_only_one_fixed_upstream_and_private_header(self):
        connections=[]
        def factory(host,timeout):connections.append(Upstream(host,timeout));return connections[-1]
        with providers.DeepSeekRelay(KEY,threading.Event(),'deepseek-flash',connection_factory=factory) as relay:
            code,body=self.request(relay)
            self.assertEqual(code,200);self.assertIn(b'response.completed',body)
            self.assertNotIn(KEY.encode(),body)
            self.assertEqual(connections[0].host,'api.deepseek.com')
            args,kwargs=connections[0].calls[0]
            self.assertEqual(args,('POST','/responses'))
            self.assertEqual(kwargs['headers']['Authorization'],'Bearer '+KEY)
            for options in ({'token':'wrong'},{'path':'/responses?token='+relay.token},{'path':'/models'},{'host':'evil.test'},
                            {'origin':'https://evil.test'},{'model':'other-model'}):
                with self.subTest(options=options):self.assertIn(self.request(relay,**options)[0],(400,403))
            self.assertEqual(len(connections),1)
        self.assertTrue(connections[0].closed);self.assertEqual(relay.token,'');self.assertEqual(relay.key,'')

    def test_upstream_redirect_and_error_never_expose_body(self):
        for status in (302,401,402,429,503):
            def factory(host,timeout):
                connection=Upstream(host,timeout);connection.status=status
                def response():
                    value=Response(status);value.seek(0);value.write(KEY.encode());value.seek(0);return value
                connection.getresponse=response;return connection
            with providers.DeepSeekRelay(KEY,threading.Event(),'deepseek-flash',connection_factory=factory) as relay:
                code,body=self.request(relay)
                self.assertEqual(code,status if status!=302 else 502)
                self.assertNotIn(KEY.encode(),body);self.assertIn(b'provider_error',body)

    def test_cancel_rejects_additional_requests(self):
        cancel=threading.Event();connections=[]
        def factory(host,timeout):connections.append(Upstream(host,timeout));return connections[-1]
        with providers.DeepSeekRelay(KEY,cancel,'deepseek-flash',connection_factory=factory) as relay:
            cancel.set();self.assertEqual(self.request(relay)[0],403)
            self.assertEqual(connections,[])

    def test_cancel_during_connection_creation_never_starts_upstream(self):
        entered=threading.Event();release=threading.Event();cancel=threading.Event();connections=[]
        def factory(host,timeout):
            entered.set();release.wait(2);connections.append(Upstream(host,timeout));return connections[-1]
        with providers.DeepSeekRelay(KEY,cancel,'deepseek-flash',connection_factory=factory) as relay:
            def request():
                try:self.request(relay)
                except (OSError,http.client.HTTPException):pass
            thread=threading.Thread(target=request);thread.start()
            self.assertTrue(entered.wait(1));cancel.set();release.set();thread.join(2)
            self.assertFalse(thread.is_alive());self.assertEqual(connections[0].calls,[]);self.assertTrue(connections[0].closed)

    def test_runtime_has_no_real_key_in_config_args_or_child_environment(self):
        with tempfile.TemporaryDirectory() as root,patch('provider_access._credential',return_value=KEY),patch.dict(os.environ,{
                'OPENAI_API_KEY':'private-other-service','DEEPSEEK_API_KEY':KEY,providers.ENV_KEY:KEY,'ANOTHER_SECRET':'private-extra'}):
            with providers.deepseek_runtime(root,'deepseek-flash',threading.Event()) as launch:
                encoded=json.dumps({'env':launch['env'],'flags':launch['flags']})
                for value in (KEY,'private-other-service','private-extra'):self.assertNotIn(value,encoded)
                self.assertIn(providers.RELAY_ENV,launch['env'])
                self.assertFalse(any(key=='shell_environment_policy.set.'+providers.RELAY_ENV for key in launch['flags']))
                self.assertEqual(launch['flags']['model_providers.ruigu_deepseek.requires_openai_auth'],False)
                self.assertEqual(launch['flags']['web_search'],'disabled')
                self.assertEqual(launch['env']['CODEX_HOME'],str(Path(root).resolve()/'provider-runtime/deepseek'))
            for path in Path(root).rglob('*'):
                if path.is_file():self.assertNotIn(KEY,path.read_text());self.assertEqual(path.stat().st_mode&0o777,0o600)

    def test_keychain_api_never_persists_key_to_data(self):
        with tempfile.TemporaryDirectory() as root,patch('provider_access.sys.platform','darwin'),patch('provider_access.Keychain') as keychain:
            keychain.return_value.has_key.return_value=True
            result=providers.save_deepseek_key(root,KEY)
            keychain.return_value.save.assert_called_once_with(KEY)
            self.assertTrue(result['configured']);self.assertNotIn(KEY,json.dumps(result))
            revision=providers.provider_revision(root);self.assertEqual(len(revision),32)
            providers.remove_deepseek_key(root)
            self.assertNotEqual(revision,providers.provider_revision(root))
            for path in Path(root).rglob('*'):
                if path.is_file():self.assertNotIn(KEY,path.read_text())

    def test_provider_runtime_closes_when_cli_spawn_fails(self):
        from codex_workspace import CodexWorkspace
        closed=[]
        with tempfile.TemporaryDirectory() as root:
            @contextmanager
            def launch(*args):
                try:yield {'home':Path(root),'flags':{},'env':{}}
                finally:closed.append(True)
            with patch('provider_access.deepseek_runtime',launch),patch('codex_workspace.subprocess.Popen',side_effect=OSError('synthetic spawn failure')):
                with self.assertRaises(OSError):
                    CodexWorkspace('synthetic-cli',root,provider={'service_id':'deepseek'},data_root=root).run_turn('public fixture','deepseek-flash')
            self.assertEqual(closed,[True])

    def test_revision_failure_prevents_keychain_change(self):
        with tempfile.TemporaryDirectory() as root,patch('provider_access.sys.platform','darwin'),patch('provider_access.Keychain') as keychain:
            for operation in (lambda:providers.save_deepseek_key(root,KEY),lambda:providers.remove_deepseek_key(root)):
                with patch('provider_access._record_revision',side_effect=OSError('synthetic disk full')):
                    with self.assertRaises(OSError):operation()
            keychain.assert_not_called()

    def test_keychain_failure_conservatively_invalidates_old_revision(self):
        with tempfile.TemporaryDirectory() as root,patch('provider_access.sys.platform','darwin'),patch('provider_access.Keychain') as keychain:
            class Model(ModelAccessMixin):
                def _provider_root(self):return Path(root)
            model=Model();chosen={'id':'deepseek:deepseek-flash','service_id':'deepseek'}
            providers._record_revision(root);before=model._configuration_fingerprint(chosen)
            keychain.return_value.save.side_effect=ValueError('synthetic keychain refused')
            with self.assertRaises(ValueError):providers.save_deepseek_key(root,KEY)
            after_save=model._configuration_fingerprint(chosen);self.assertNotEqual(before,after_save)
            keychain.return_value.remove.side_effect=ValueError('synthetic keychain refused')
            with self.assertRaises(ValueError):providers.remove_deepseek_key(root)
            self.assertNotEqual(after_save,model._configuration_fingerprint(chosen))

    def test_revision_sync_failure_prevents_keychain_change(self):
        with tempfile.TemporaryDirectory() as root,patch('provider_access.sys.platform','darwin'),patch('provider_access.Keychain') as keychain:
            with patch('provider_access.os.fsync',side_effect=OSError('synthetic sync failed')):
                with self.assertRaises(OSError):providers.save_deepseek_key(root,KEY)
            keychain.assert_not_called()

    def test_nonmac_environment_is_readonly_and_never_saved(self):
        with tempfile.TemporaryDirectory() as root,patch('provider_access.sys.platform','linux'),patch.dict(os.environ,{providers.ENV_KEY:KEY}):
            value=providers.credential_state(root)
            self.assertTrue(value['configured']);self.assertFalse(value['can_save'])
            with self.assertRaises(ValueError):providers.save_deepseek_key(root,KEY)
            with self.assertRaises(ValueError):providers.remove_deepseek_key(root)
            self.assertEqual(list(Path(root).iterdir()),[])

    def test_every_codex_transport_gets_actual_child_environment_without_new_keys(self):
        from codex_workspace import CodexWorkspace, read_model_directory
        from test_stream_transport import Host
        script='''import sys,json,os
assert 'RUIGU_DEEPSEEK_API_KEY' not in os.environ
assert 'RUIGU_PROVIDER_RELAY_TOKEN' not in os.environ
assert os.environ['OPENAI_API_KEY']=='original-codex-fixture'
def read():return json.loads(sys.stdin.readline())
def reply(i,result):print(json.dumps({'id':i,'result':result}),flush=True)
def emit(method,params):print(json.dumps({'method':method,'params':params}),flush=True)
read();reply(1,{})
read()
request=read()
if sys.argv[1]=='directory':
 assert request['method']=='account/read';reply(request['id'],{'account':{'type':'chatgpt'}})
 request=read();assert request['method']=='model/list';reply(request['id'],{'data':[{'id':'test-model'}],'nextCursor':None})
else:
 assert request['method']=='thread/start';reply(request['id'],{'thread':{'id':'thread-1'},'model':'test-model'})
 request=read();assert request['method']=='turn/start';reply(request['id'],{'turn':{'id':'turn-1'}})
 emit('turn/started',{'threadId':'thread-1','turn':{'id':'turn-1'}})
 emit('item/started',{'threadId':'thread-1','turnId':'turn-1','item':{'id':'a1','type':'agentMessage','text':'','phase':'final_answer'}})
 emit('item/completed',{'threadId':'thread-1','turnId':'turn-1','item':{'id':'a1','type':'agentMessage','text':'synthetic answer','phase':'final_answer'}})
 emit('turn/completed',{'threadId':'thread-1','turn':{'id':'turn-1','status':'completed'}})
'''
        captured=[];real_popen=subprocess.Popen
        def spawn(*args,**kwargs):
            environment=kwargs.get('env');self.assertIsInstance(environment,dict)
            self.assertNotIn(providers.ENV_KEY,environment);self.assertNotIn(providers.RELAY_ENV,environment)
            self.assertEqual(environment['OPENAI_API_KEY'],'original-codex-fixture');captured.append(True)
            return real_popen(*args,**kwargs)
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,{providers.ENV_KEY:KEY,providers.RELAY_ENV:'temporary-relay-fixture','OPENAI_API_KEY':'original-codex-fixture'}):
            root=Path(folder);program=root/'protocol.py';program.write_text(script)
            with patch('codex_workspace.subprocess.Popen',side_effect=spawn):
                metadata=read_model_directory('synthetic-cli',home=root,command=[sys.executable,str(program),'directory'])
                self.assertEqual(metadata['models'][0]['id'],'test-model')
                answer=CodexWorkspace('synthetic-cli',root,command=[sys.executable,str(program),'workspace']).run_turn('public fixture','test-model')
                self.assertEqual(answer['text'],'synthetic answer')
                host=Host();job=dict(host.store.job,effective_model='test-model',organize=False)
                text=host.stream_model_text(job,[sys.executable,str(program),'legacy'],root,'public fixture','answer',3)
                self.assertEqual(text,'synthetic answer')
            self.assertEqual(len(captured),3)
            self.assertEqual(os.environ[providers.ENV_KEY],KEY,'不修改主进程原环境')

    def test_cli_version_probe_actual_process_does_not_inherit_new_credentials(self):
        from codex_workspace import resolve_codex_cli
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,{providers.ENV_KEY:KEY,providers.RELAY_ENV:'temporary-relay-fixture','OPENAI_API_KEY':'original-codex-fixture'}):
            program=Path(folder)/'codex'
            program.write_text('#!/usr/bin/env python3\nimport os\nassert "RUIGU_DEEPSEEK_API_KEY" not in os.environ\nassert "RUIGU_PROVIDER_RELAY_TOKEN" not in os.environ\nassert os.environ["OPENAI_API_KEY"]=="original-codex-fixture"\nprint("codex-cli 9.0.0")\n')
            program.chmod(0o700)
            with patch('codex_workspace.sys.platform','linux'),patch('codex_workspace.shutil.which',return_value=str(program)):
                self.assertEqual(resolve_codex_cli(),str(program))

    def test_deepseek_only_catalog_without_chatgpt_account(self):
        class Model(ModelAccessMixin):pass
        with tempfile.TemporaryDirectory() as root,patch('provider_access.credential_state',return_value={'configured':True}):
            model=Model();model.model_state_path=Path(root)/'data';model.model_home=Path(root);model.cli='synthetic-codex'
            with patch('codex_workspace.read_model_directory',side_effect=ValueError('本机Codex尚未登录')):
                catalog=model.model_catalog();self.assertEqual(catalog['status'],'ready')
                chosen=model.resolve_model('deepseek:deepseek-flash')
            self.assertTrue(chosen['selectable']);self.assertFalse(chosen['verified'])
            self.assertEqual(chosen['api_model'],'deepseek-flash');self.assertEqual(chosen['service_id'],'deepseek')
            self.assertIn('尚未查询',chosen['source']);self.assertEqual(chosen['input_modalities'],['text','image'])
            with patch.object(model,'model_catalog',return_value=catalog):
                receipt=model.verify_model(chosen['id'],'deepseek-flash')
                self.assertEqual(receipt['actual_model'],'deepseek-flash')
                with self.assertRaises(ValueError):model.verify_model(chosen['id'],'gpt-other')

    def test_scope_limited_history_excludes_shared_and_tools(self):
        with tempfile.TemporaryDirectory() as root:
            desk=Desk(Path(root)/'data');session=desk.store.put('workspace-session',{'title':'synthetic'})
            policy={'scope':'advisors','execution_mode':'knowledge','advisor_names':[],'current_file_id':None}
            def message(text,context,**extra):
                job=desk.store.put('workspace-job',dict({'session_id':session['id'],'status':'completed','request_context':context},**extra))
                desk.store.put('workspace-message',{'job_id':job['id'],'session_id':session['id'],'role':'assistant','status':'completed',
                    'text':text,'analysis_updates':['PRIVATE_RAW_TOOL_SENTINEL'],'native_tools':['PRIVATE_RAW_TOOL_SENTINEL']})
            message('allowed public chat',policy)
            message('private scope must not cross',dict(policy,scope='personal'))
            message('expired shared snippet',policy,shared_preview_id='old-share')
            current=desk.store.put('workspace-job',{'session_id':session['id'],'status':'queued'})
            history=desk._workspace_transfer_history(current,policy)
            self.assertEqual([row['text'] for row in history],['allowed public chat'])
            self.assertNotIn('PRIVATE_RAW_TOOL_SENTINEL',json.dumps(history))
            desk.stop_workspace()

    def test_service_switch_keeps_visible_session_but_not_foreign_thread(self):
        with tempfile.TemporaryDirectory() as root:
            desk=Desk(Path(root)/'data');desk.cli='synthetic-cli'
            policy={'scope':'advisors','execution_mode':'knowledge','advisor_names':[],'current_file_id':None,'purpose':'general','days':7}
            boundary={'version':3,'scope':'advisors','execution_mode':'knowledge','advisor_names':[],
                      'cwd':str(desk.store.path/'workspace-files'),'document':None}
            session=desk.store.put('workspace-session',{'title':'visible chat','thread_id':'codex-thread','context_boundary':boundary})
            chosen={'id':'deepseek:deepseek-flash','api_model':'deepseek-flash','service_id':'deepseek','service_label':'DeepSeek API','input_modalities':['text','image']}
            with patch.object(desk,'resolve_model',return_value=chosen),patch('workspace_api.threading.Thread'):
                result=desk.workspace_send(dict(policy,session_id=session['id'],message='继续刚才的讨论',model=chosen['id']))
            self.assertEqual(result['session']['id'],session['id']);self.assertIsNone(result['session']['thread_id'])
            self.assertEqual(result['session']['title'],'visible chat');self.assertTrue(result['job']['carry_history'])
            self.assertEqual(result['job']['service_id'],'deepseek')
            desk.store.change(result['job']['id'],'workspace-job',status='failed',error='synthetic failure before thread creation')
            with patch.object(desk,'resolve_model',return_value=chosen),patch('workspace_api.threading.Thread'):
                retry=desk.workspace_send(dict(policy,session_id=session['id'],message='重试继续讨论',model=chosen['id']))
            self.assertIsNone(retry['session']['thread_id']);self.assertTrue(retry['job']['carry_history'])
            desk.store.change(retry['job']['id'],'workspace-job',status='cancelled')
            desk.store.change(session['id'],'workspace-session',thread_id='flash-thread')
            pro=dict(chosen,id='deepseek:deepseek-v4-pro',api_model='deepseek-v4-pro',input_modalities=['text'])
            with patch.object(desk,'resolve_model',return_value=pro),patch('workspace_api.threading.Thread'):
                changed_model=desk.workspace_send(dict(policy,session_id=session['id'],message='继续文字讨论',model=pro['id']))
            self.assertIsNone(changed_model['session']['thread_id']);self.assertTrue(changed_model['job']['carry_history'])
            desk.store.change(changed_model['job']['id'],'workspace-job',status='cancelled')
            narrower=dict(policy,scope='personal',execution_mode='assistant')
            with patch.object(desk,'resolve_model',return_value=dict(chosen,id='gpt-test',api_model='gpt-test',service_id='codex')),patch('workspace_api.threading.Thread'):
                changed=desk.workspace_send(dict(narrower,session_id=session['id'],message='只看本轮范围',model='gpt-test'))
            self.assertFalse(changed['job']['carry_history'])
            desk.stop_workspace()

    def test_http_credentials_endpoints_use_existing_origin_and_token_gate(self):
        import app
        with tempfile.TemporaryDirectory() as root,patch('provider_access.credential_state',return_value={'id':'deepseek','configured':False}):
            server=app.serve(Path(root)/'data',0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                def request(headers):
                    conn=http.client.HTTPConnection('127.0.0.1',server.server_port)
                    conn.request('POST','/api/providers/deepseek',json.dumps({'api_key':KEY}),dict({'Content-Type':'application/json'},**headers))
                    response=conn.getresponse();result=(response.status,response.read());conn.close();return result
                with patch.object(server.app,'configure_deepseek',return_value={'providers':[{'id':'deepseek','configured':True}]}) as configure:
                    self.assertEqual(request({})[0],403)
                    self.assertEqual(request({'X-Desk-Token':server.app.token,'Origin':'https://evil.test'})[0],403)
                    self.assertEqual(configure.call_count,0)
                    code,body=request({'X-Desk-Token':server.app.token,'Origin':'http://127.0.0.1:'+str(server.server_port)})
                    self.assertEqual(code,200);self.assertNotIn(KEY.encode(),body);configure.assert_called_once_with({'api_key':KEY})
            finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
