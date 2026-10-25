import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from model_access import ModelAccessMixin

class Models(ModelAccessMixin): pass

class ModelAccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.desk=Models(); self.desk.model_home=self.root; self.desk.model_state_path=self.root/'data'
        self.write_config(); self.catalog([self.row()])
    def tearDown(self): self.tmp.cleanup()
    def write_config(self, extra=''):
        provider = '' if 'model_provider=' in extra else 'model_provider="fixture"\n'
        (self.root/'config.toml').write_text('model="gpt-fixture"\nmodel_reasoning_effort="xhigh"\napi_key="SECRET_SENTINEL"\n'+provider+extra)
    def catalog(self, rows):
        (self.root/'models.json').write_text(json.dumps({'models':rows}))
        (self.root/'opencodex.config.toml').write_text('model_catalog_json="models.json"\nopenai_base_url="https://secret.invalid/route?token=TOKEN"\n')
    def row(self, ident='gpt-fixture', levels=('low','high','xhigh')):
        return {'slug':ident,'display_name':'Fixture','visibility':'list','supported_reasoning_levels':[{'effort':e} for e in levels],'base_instructions':'PRIVATE_SYSTEM_SENTINEL','secret':'SECRET_MODEL'}
    def receipt_file(self): return self.root/'data/model-verification.json'
    def selectable(self): return [model for model in self.desk.model_catalog()['models'] if model['selectable']]

    def test_slow_metadata_startup_can_finish_after_the_old_ten_second_limit(self):
        import codex_workspace
        fixture = self.root/'slow_metadata.py'
        fixture.write_text('''import sys,json,time
def read(): return json.loads(sys.stdin.readline())
def reply(i,r): print(json.dumps({'id':i,'result':r}),flush=True)
assert read()['method']=='initialize';time.sleep(11);reply(1,{})
assert read()['method']=='initialized'
assert read()['method']=='account/read';reply(2,{'account':{'type':'chatgpt'}})
assert read()['method']=='model/list';reply(3,{'data':[{'id':'gpt-public'}],'nextCursor':None})
assert not sys.stdin.readline()
''')
        command=[sys.executable,'-B',str(fixture)]
        with self.assertRaisesRegex(ValueError,'模型目录读取超时'):
            codex_workspace.read_model_directory('fixture',home=self.root,command=command,timeout=.1)
        self.assertIsNone(codex_workspace._DIRECTORY_PROCESS,'超时必须回收子进程，后续重试才能继续')
        self.assertFalse(codex_workspace._DIRECTORY_PROCESS_LOCK.locked())
        result=codex_workspace.read_model_directory('fixture',home=self.root,command=command)
        self.assertEqual(result['models'],[{'id':'gpt-public'}])
        self.assertIsNone(codex_workspace._DIRECTORY_PROCESS)

    def test_native_success_cache_lifetime_starts_when_metadata_finishes(self):
        self.write_config('model_provider="openai"\n')
        self.desk.cli='fixture-codex'
        clock=[100.0]
        def metadata(*args,**kwargs):
            clock[0]+=25
            return {'models':[self.row('gpt-public')],'account_type':'chatgpt'}
        with patch('model_access.time.monotonic',side_effect=lambda:clock[0]), patch('codex_workspace.read_model_directory',side_effect=metadata) as read:
            selected=self.desk.resolve_model('gpt-public')
            self.assertEqual(self.desk._model_directory_cache['at'],125)
            clock[0]=154.9
            self.desk.require_model_selection(selected)
            self.assertEqual(read.call_count,1,'完成后的发送检查应复用目录，不能按启动时刻提前失效')
            clock[0]=155
            self.desk.require_model_selection(selected)
            self.assertEqual(read.call_count,2,'目录仍有明确的30秒有效期，不能无限沿用')

    def test_failed_directory_is_blocked_briefly_then_recovers_without_explicit_refresh(self):
        self.write_config('model_provider="openai"\n')
        self.desk.cli='fixture-codex'
        clock=[100.0]
        result={'models':[self.row('gpt-public')],'account_type':'chatgpt'}
        with patch('model_access.time.monotonic',side_effect=lambda:clock[0]), patch('codex_workspace.read_model_directory',side_effect=[ValueError('模型目录读取超时'),result]) as read:
            with self.assertRaisesRegex(ValueError,'模型目录读取超时'):self.desk.resolve_model('gpt-public')
            clock[0]=101.9
            self.assertFalse(any(m['selectable'] for m in self.desk.model_catalog()['models']))
            self.assertEqual(read.call_count,1,'短失败缓存抑制并发重复派生，不放行旧目录')
            clock[0]=102
            selected=self.desk.resolve_model('gpt-public')
            self.assertEqual(read.call_count,2,'失败后两秒即可重核，不应继续锁住用户30秒')
            self.assertTrue(selected['selectable'])

    def test_overlapping_refreshes_share_the_read_but_a_later_refresh_reads_again(self):
        import threading
        for failed in (False,True):
            with self.subTest(failed=failed):
                self.desk._model_directory_cache=None
                reading=threading.Event();waiting=threading.Event();release=threading.Event()
                lock=threading.RLock();results=[];errors=[]
                class ObservedLock:
                    def __enter__(self):
                        if threading.current_thread().name=='directory-waiter':waiting.set()
                        return lock.__enter__()
                    def __exit__(self,*args):return lock.__exit__(*args)
                def metadata(*args,**kwargs):
                    reading.set()
                    if not release.wait(2):raise AssertionError('测试未释放目录读取屏障')
                    if failed:raise ValueError('模型目录读取超时')
                    return {'models':[self.row('gpt-public')],'account_type':'chatgpt'}
                def refresh():
                    try:results.append(self.desk._native_directory(refresh=True))
                    except Exception as error:errors.append(error)
                with patch('model_access._DIRECTORY_LOCK',ObservedLock()), patch('codex_workspace.read_model_directory',side_effect=metadata) as read:
                    first=threading.Thread(target=refresh,name='directory-reader')
                    second=threading.Thread(target=refresh,name='directory-waiter')
                    first.start()
                    try:
                        self.assertTrue(reading.wait(2))
                        second.start();self.assertTrue(waiting.wait(2))
                    finally:
                        release.set();first.join(2)
                        if second.ident is not None:second.join(2)
                    self.assertFalse(first.is_alive());self.assertFalse(second.is_alive())
                    self.assertEqual(errors,[]);self.assertEqual(len(results),2)
                    self.assertEqual(read.call_count,1,'等待同一次目录读取的刷新不能再串行启动一个进程')
                    self.assertEqual(results[0],results[1]);self.assertIsNot(results[0],results[1])
                    self.assertEqual(bool(results[0]['error']),failed)
                    self.desk._native_directory(refresh=True)
                    self.assertEqual(read.call_count,2,'上一轮完成后才发起的新刷新仍必须真正重读')

    def test_native_metadata_multiple_untried_models_selection_and_current_scope(self):
        from codex_workspace import read_model_directory
        self.write_config('model_provider="openai"\n')
        self.catalog([self.row(),self.row('gemini-web/public')])
        fixture = self.root/'metadata.py'
        fixture.write_text('''import sys,json
def read(): return json.loads(sys.stdin.readline())
def reply(i,r): print(json.dumps({'id':i,'result':r}),flush=True)
assert read()['method']=='initialize';reply(1,{})
assert read()['method']=='initialized'
assert read()['method']=='account/read';reply(2,{'account':{'type':'chatgpt','planType':'pro','email':'PRIVATE_EMAIL'}})
request=read();assert request['method']=='model/list' and request['params']['includeHidden'] is False
reply(3,{'data':[{'id':'gpt-public-a','isDefault':True,'supportedReasoningEfforts':[{'reasoningEffort':'high'}]},
{'id':'gpt-public-b','supportedReasoningEfforts':[{'reasoningEffort':'low'},{'reasoningEffort':'high'}]}],'nextCursor':None})
assert not sys.stdin.readline()
''')
        self.desk.cli = str(fixture)
        calls = []
        def metadata(cli,home):
            calls.append(cli)
            return read_model_directory(cli,home=home,command=[sys.executable,'-B',str(fixture)])
        with patch('codex_workspace.read_model_directory',side_effect=metadata):
            catalog = self.desk.model_catalog()
            self.assertEqual(catalog['default_id'],'gpt-public-a')
            offered = [row for row in catalog['models'] if row['selectable']]
            self.assertEqual([row['id'] for row in offered],['gpt-public-a','gpt-public-b'])
            self.assertTrue(all(not row['verified'] and '尚无独立连接检查回执' in row['verification'] for row in offered))
            for text in ('PRIVATE_EMAIL','SECRET_SENTINEL','TOKEN',str(self.root)):
                self.assertNotIn(text,json.dumps(catalog))
            selected = self.desk.resolve_model('gpt-public-b')
            self.assertEqual(selected['reasoning_effort'],'high')
            self.desk.require_model_selection(selected)
            self.assertEqual(len(calls),1,'目录在同配置与CLI身份下单份缓存，发送校验不重复派生')
            for unsupported in ('gpt-fixture','gemini-web/public','unknown-model'):
                with self.assertRaises(ValueError): self.desk.resolve_model(unsupported)
            with self.assertRaisesRegex(ValueError,'不一致'):
                self.desk.verify_model('gpt-public-b','wrong-model')
            self.assertFalse(self.receipt_file().exists())
            self.write_config('model_provider="openai"\nmodel_reasoning_summary="brief"\n')
            with self.assertRaisesRegex(ValueError,'已变化'): self.desk.require_model_selection(selected)
            selected = self.desk.resolve_model('gpt-public-b')
            self.assertEqual(len(calls),2)
            (self.root/'auth.json').write_text('PUBLIC_STAT_ONLY_SENTINEL')
            with patch('codex_workspace.read_model_directory',side_effect=ValueError('目录连接失败')):
                failed = self.desk.model_catalog()
                self.assertEqual(failed['status'],'unavailable')
                self.assertFalse(any(row['selectable'] for row in failed['models']))
                with self.assertRaisesRegex(ValueError,'目录连接失败'):self.desk.require_model_selection(selected)
                self.assertEqual(selected['id'],'gpt-public-b','失败保留调用方所选身份，不回退默认')

    def test_explicit_catalog_precedes_cache_only_codex_gemini_candidates(self):
        self.catalog([self.row(),self.row('gemini-web/pro'),self.row('deepseek/no'),{**self.row('gpt-hidden'),'visibility':'hide'},{'slug':'--unsafe'}])
        (self.root/'models_cache.json').write_text(json.dumps({'models':[self.row('gpt-cached')]}))
        rows=self.desk.configured_models()
        self.assertEqual([m['id'] for m in rows],['gpt-fixture','gemini-web/pro'])
        for secret in ('SECRET','PRIVATE_SYSTEM','secret.invalid','TOKEN',str(self.root)): self.assertNotIn(secret,json.dumps(rows))
        self.assertEqual(self.selectable(),[])
        self.assertEqual(self.desk.model_catalog()['status'],'checking')

    def test_explicit_refresh_reloads_catalog_and_rejects_old_scope(self):
        self.write_config('model_provider="openai"\n')
        self.desk.cli='fixture-codex'
        first={'models':[self.row('gpt-old')],'account_type':'chatgpt'}
        second={'models':[self.row('gpt-old'),self.row('gpt-6.1-sol')],'account_type':'chatgpt'}
        with patch('codex_workspace.read_model_directory',side_effect=[first,second,ValueError('模型目录读取失败')]) as read:
            selected=self.desk.resolve_model('gpt-old')
            self.desk.model_catalog()
            self.assertEqual(read.call_count,1)
            refreshed=self.desk.model_catalog(refresh=True)
            self.assertEqual(read.call_count,2)
            self.assertTrue(next(row for row in refreshed['models'] if row['id']=='gpt-6.1-sol')['selectable'])
            self.assertFalse(next(row for row in refreshed['models'] if row['id']=='gpt-6.1-sol')['verified'])
            self.assertEqual(selected['id'],'gpt-old')
            with self.assertRaisesRegex(ValueError,'已变化'):self.desk.require_model_selection(selected)
            failed=self.desk.model_catalog(refresh=True)
            self.assertEqual(failed['status'],'unavailable')
            self.assertFalse(any(row['selectable'] for row in failed['models']))

    def test_http_refresh_is_explicit_and_does_not_probe_a_model(self):
        import http.client
        import threading
        import app
        with patch('app.resolve_codex_cli',return_value='fixture-codex'):
            server=app.serve(self.root/'http',0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with patch.object(server.app,'model_catalog',side_effect=lambda **kw:kw) as read:
                for suffix,expected in (('',False),('?refresh=1',True),('?refresh=bad',False)):
                    conn=http.client.HTTPConnection('127.0.0.1',server.server_port)
                    conn.request('GET','/api/models'+suffix);response=conn.getresponse()
                    self.assertEqual(response.status,200)
                    self.assertEqual(json.loads(response.read()),{'refresh':expected});conn.close()
                self.assertEqual(read.call_count,3)
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_configuration_never_implies_available_and_real_receipt_required(self):
        with self.assertRaises(ValueError): self.desk.resolve_model()
        receipt=self.desk.verify_model('gpt-fixture','gpt-fixture')
        result=self.desk.model_catalog()
        self.assertEqual(result['default_model'],'gpt-fixture'); self.assertEqual(result['status'],'ready')
        self.assertTrue(result['models'][0]['verified']); self.assertEqual(result['models'][0]['verified_at'],receipt['verified_at'])
        self.assertEqual(self.receipt_file().stat().st_mode & 0o777,0o600)

    def test_mismatched_receipt_cannot_verify(self):
        with self.assertRaisesRegex(ValueError,'不一致'): self.desk.verify_model('gpt-fixture','other')
        self.assertEqual(self.selectable(),[])

    def test_resolves_effort_snapshot_and_rejects_unlisted_or_injection(self):
        self.catalog([self.row(),self.row('gemini-web/limited',('high','max'))])
        self.desk.verify_model('gpt-fixture','gpt-fixture'); self.desk.verify_model('gemini-web/limited','gemini-web/limited')
        result=self.desk.resolve_model(); self.assertEqual(result['reasoning_effort'],'xhigh'); self.assertEqual(result['routing_effort'],'low')
        limited=self.desk.resolve_model('gemini-web/limited'); self.assertEqual(limited['routing_effort'],'high'); self.assertEqual(limited['reasoning_effort'],'high')
        for ident in ('unknown','--model=x','a\n-c model=x',['gpt-fixture']):
            with self.subTest(ident=ident),self.assertRaises(ValueError): self.desk.resolve_model(ident)

    def test_dated_receipt_remains_usable_and_future_timestamp_is_rejected(self):
        self.desk.verify_model('gpt-fixture','gpt-fixture'); original=json.loads(self.receipt_file().read_text())
        for offset in (-25,1):
            state=json.loads(json.dumps(original)); state['models']['gpt-fixture']['verified_at']=(dt.datetime.now(dt.timezone.utc)+dt.timedelta(hours=offset)).isoformat()
            self.receipt_file().write_text(json.dumps(state)); self.assertEqual(len(self.selectable()),1 if offset < 0 else 0)

    def test_configuration_change_and_explicit_failure_remove_option(self):
        self.desk.verify_model('gpt-fixture','gpt-fixture')
        self.catalog([self.row(levels=('high',))]); self.assertEqual(self.selectable(),[])
        self.desk.verify_model('gpt-fixture','gpt-fixture'); self.assertEqual(len(self.selectable()),1)
        self.desk.invalidate_model('gpt-fixture'); self.assertEqual(self.selectable(),[])

    def test_router_binding_change_invalidates_but_credential_not_hashed_or_exposed(self):
        model=self.desk.resolve_model('gpt-fixture',require_verified=False); before=self.desk._configuration_fingerprint(model)
        p=self.root/'opencodex.config.toml'; text=p.read_text(); p.write_text(text.replace('TOKEN','NEW_SECRET'))
        self.assertEqual(before,self.desk._configuration_fingerprint(model))
        p.write_text(text.replace('secret.invalid','changed.invalid'))
        self.assertNotEqual(before,self.desk._configuration_fingerprint(model))

    def test_custom_provider_does_not_inherit_native_cache_allowlist(self):
        self.write_config('model_provider="custom"\n'); (self.root/'opencodex.config.toml').unlink()
        (self.root/'models_cache.json').write_text(json.dumps({'models':[self.row('gpt-cached')]}))
        self.assertEqual([m['id'] for m in self.desk.configured_models()],['gpt-fixture'])
        with self.assertRaises(ValueError): self.desk.resolve_model('gpt-cached',require_verified=False)

    def test_malformed_files_shapes_and_receipts_are_safe(self):
        self.catalog([None,'model',[],{'slug':'gemini-web/pro','supported_reasoning_levels':'not a list'},self.row()])
        self.assertEqual(len(self.desk.configured_models()),2)
        self.desk.verify_model('gpt-fixture','gpt-fixture'); self.receipt_file().write_text('{bad')
        self.assertEqual(self.selectable(),[])
        (self.root/'models.json').write_text('{bad'); (self.root/'config.toml').write_text('{bad')
        self.assertEqual(self.desk.configured_models(),[])

    def test_live_removal_rejects_old_selection(self):
        self.catalog([self.row(),self.row('gemini-web/temp')]); self.desk.verify_model('gemini-web/temp','gemini-web/temp')
        old=self.desk.resolve_model('gemini-web/temp'); self.catalog([self.row()])
        self.assertEqual(old['id'],'gemini-web/temp')
        with self.assertRaises(ValueError): self.desk.resolve_model(old['id'])

if __name__=='__main__':unittest.main()
