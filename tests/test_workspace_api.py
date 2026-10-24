import tempfile
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
import app


class WorkspaceAPITest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        self.doc = self.vault / 'note.md'
        self.doc.write_text('# 测试\n原文')
        self.desk = app.Desk(self.root / 'data')
        self.desk.settings['vault_roots'] = [{'id':'notes','label':'笔记','path':str(self.vault),'writable':True}]
        self.desk.init_workspace()

    def tearDown(self):
        self.desk.stop_workspace()
        self.tmp.cleanup()

    def test_live_read_conflict_and_undo(self):
        r = self.desk.workspace_get('/api/vault/read', {'id':'notes:note.md'})
        self.doc.write_text('# 外部修改\n新版')
        with self.assertRaises(ValueError):
            self.desk.workspace_post('/api/vault/save', {'id':r['id'],'text':'旧改动','expected_sha':r['sha']})
        current = self.desk.workspace_get('/api/vault/read', {'id':r['id']})
        saved = self.desk.workspace_post('/api/vault/save', {'id':r['id'],'text':'# 保存\n新内容','expected_sha':current['sha']})
        restored = self.desk.workspace_post('/api/vault/undo', {'undo_id':saved['undo_id'],'expected_sha':saved['sha']})
        self.assertEqual(restored['text'], '# 外部修改\n新版')

    def test_persistent_thread_tools_and_receipts(self):
        session = self.desk.store.put('workspace-session', {'title':'测试','thread_id':'existing-thread'})
        job = self.desk.store.put('workspace-job', {'session_id':session['id'],'status':'queued'})
        doc = self.desk.vault.read('notes:note.md')
        def run_turn(client, **kw):
            self.assertEqual(kw['thread_id'], 'existing-thread')
            kw['on_event']({'type':'thread','thread_id':'existing-thread'})
            r = kw['tool_call']('knowledge_read', {'document_id':'notes:note.md'})
            self.assertEqual(r['text'], '# 测试\n原文')
            written = kw['tool_call']('write_current', {'document_id':'notes:note.md','content':'# 测试\n已写回','expected_sha256':r['sha256']})
            self.assertTrue(written['verified'])
            kw['on_event']({'type':'delta','text':'已写回'})
            return {'thread_id':'existing-thread','actual_model':'gpt-test','text':'已写回','status':'completed'}
        import threading
        with patch('codex_workspace.CodexWorkspace.run_turn', run_turn):
            self.desk.workspace_run(job,session,{'id':'gpt-test'},'保存到当前笔记',doc,'',True,threading.Event())
        saved = self.desk.workspace_get('/api/workspace/status', {'session_id':session['id']})
        self.assertEqual(saved['jobs'][0]['status'], 'completed')
        self.assertEqual(saved['messages'][0]['references'][0]['id'], 'notes:note.md')
        self.assertIn('已写回', self.doc.read_text())

    def test_tool_cannot_write_without_current_turn_authority(self):
        session = self.desk.store.put('workspace-session', {'title':'测试','thread_id':'existing'})
        job = self.desk.store.put('workspace-job', {'session_id':session['id'],'status':'queued'})
        doc = self.desk.vault.read('notes:note.md')
        def run_turn(client, **kw):
            with self.assertRaises(PermissionError):
                kw['tool_call']('write_current', {'document_id':doc['file_id'],'content':'拒绝','expected_sha256':doc['sha']})
            return {'thread_id':'existing','text':'只读','status':'completed'}
        import threading
        with patch('codex_workspace.CodexWorkspace.run_turn',run_turn):
            self.desk.workspace_run(job,session,{'id':'gpt-test'},'读一下',doc,'',False,threading.Event())
        self.assertEqual(self.doc.read_text(),'# 测试\n原文')


    def test_partial_read_cannot_replace_full_document(self):
        import threading
        self.doc.write_text('\n'.join('line'+str(i) for i in range(300)))
        session=self.desk.store.put('workspace-session', {'title':'partial'})
        job=self.desk.store.put('workspace-job', {'session_id':session['id'],'status':'queued'})
        doc=self.desk.vault.read('notes:note.md')
        def run_turn(client, **kw):
            tool=kw['tool_call']
            r=tool('knowledge_read',{'document_id':doc['file_id'],'limit':1})
            with self.assertRaisesRegex(ValueError,'全部行'):
                tool('write_current',{'content':'truncated','expected_sha256':r['sha256']})
            tool('knowledge_read',{'document_id':doc['file_id'],'offset':1,'limit':249})
            tool('knowledge_read',{'document_id':doc['file_id'],'offset':250,'limit':100})
            result=tool('write_current',{'content':doc['text']+'\n追加','expected_sha256':r['sha256']})
            self.assertTrue(result['verified'])
            return {'text':'done','status':'completed'}
        with patch('codex_workspace.CodexWorkspace.run_turn',run_turn):
            self.desk.workspace_run(job,session,{'id':'test'},'保存到当前笔记',doc,'',True,threading.Event())
        self.assertEqual(self.doc.read_text(),doc['text']+'\n追加')

    def test_cancel_completed_job_does_not_change_terminal_state(self):
        import threading
        job=self.desk.store.put('workspace-job', {'status':'completed'})
        self.desk._workspace_cancel[job['id']]=threading.Event()
        result=self.desk.workspace_post('/api/workspace/cancel', {'id':job['id']})
        self.assertEqual(result['status'],'completed')

    def test_thread_start_failure_is_terminal(self):
        self.desk.cli='fake'
        with patch.object(self.desk,'resolve_model',return_value={'id':'test'}), patch('workspace_api.threading.Thread.start',side_effect=RuntimeError('resource')):
            with self.assertRaisesRegex(RuntimeError,'无法启动'):
                self.desk.workspace_send({'message':'hello'})
        self.assertEqual(self.desk.store.all('workspace-job')[0]['status'],'failed')
        self.assertFalse(self.desk._workspace_cancel)

    def test_status_and_sessions_use_indexed_bounded_pages_without_store_all(self):
        one=self.desk.store.put('workspace-session', {'title':'one'})
        two=self.desk.store.put('workspace-session', {'title':'two'})
        for i in range(85):
            self.desk.store.put('workspace-message', {'session_id':one['id'],'text':str(i)})
            self.desk.store.put('workspace-message', {'session_id':two['id'],'text':'OTHER '+str(i)})
        for i in range(35):self.desk.store.put('workspace-job', {'session_id':one['id'],'status':'completed','text':str(i)})
        with patch.object(self.desk.store,'all',side_effect=AssertionError('unbounded object scan')):
            first=self.desk.workspace_get('/api/workspace/status',{'session_id':one['id']})
            self.assertEqual(len(first['messages']),80);self.assertEqual(len(first['jobs']),30)
            self.assertEqual(first['messages'][0]['text'],'5')
            self.assertTrue(first['messages_pagination']['has_more'])
            second=self.desk.workspace_get('/api/workspace/status',{'session_id':one['id'],'messages_before':first['messages_pagination']['next_cursor']})
            self.assertEqual([m['text'] for m in second['messages']],['0','1','2','3','4'])
            self.assertFalse(second['messages_pagination']['has_more'])
            sessions=self.desk.workspace_get('/api/workspace/sessions',{'limit':'1'})
            self.assertEqual(sessions['sessions'][0]['id'],two['id'])
            old=self.desk.workspace_get('/api/workspace/sessions',{'limit':'1','before':sessions['pagination']['next_cursor']})
            self.assertEqual(old['sessions'][0]['id'],one['id'])
        with self.desk.store.conn() as conn:
            plan=conn.execute("EXPLAIN QUERY PLAN SELECT rowid,id,data FROM objects WHERE kind=? AND json_extract(data,'$.session_id')=? AND rowid<? ORDER BY rowid DESC LIMIT ?",('workspace-message',one['id'],999999,81)).fetchall()
        details=' '.join(str(r['detail']) for r in plan)
        self.assertIn('workspace_kind_session',details)
        self.assertNotIn('TEMP B-TREE',details)

    def test_pagination_rejects_invalid_cursors(self):
        for value in ['-1','0','abc',True,'9'*30]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.desk.workspace_get('/api/workspace/sessions',{'before':value})

    def test_restart_recovers_pending_jobs_and_write_uncertainty_without_history_scan(self):
        session=self.desk.store.put('workspace-session', {'title':'restart','thread_id':'durable-thread'})
        job=self.desk.store.put('workspace-job', {'session_id':session['id'],'status':'running',
            'references':[{'id':'notes:note.md'}], 'tool_events':[{'name':'knowledge_read','status':'completed'}]})
        journal=self.desk.store.put('workspace-write', {'job_id':job['id'],'document_id':'notes:note.md','status':'started'})
        with patch.object(self.desk.store,'all',side_effect=AssertionError('full historical scan')):
            self.desk.init_workspace()
        recovered=self.desk.store.get(job['id'],'workspace-job')
        self.assertEqual(recovered['status'],'interrupted')
        self.assertEqual(recovered['write_receipts'][0]['status'],'uncertain')
        self.assertEqual(recovered['references'][0]['id'],'notes:note.md')
        self.assertEqual(self.desk.store.get(journal['id'],'workspace-write')['status'],'uncertain')
        self.assertEqual(self.desk.store.get(session['id'],'workspace-session')['thread_id'],'durable-thread')

    def test_write_journal_precedes_side_effect_and_failure_is_not_verified(self):
        session=self.desk.store.put('workspace-session', {'title':'write'})
        job=self.desk.store.put('workspace-job', {'session_id':session['id'],'status':'queued'})
        doc=self.desk.vault.read('notes:note.md');save=self.desk.vault.save
        def failing_save(*args):
            journals=self.desk.store.all('workspace-write')
            self.assertEqual(len(journals),1);self.assertEqual(journals[0]['status'],'started')
            save(*args)
            raise OSError('simulated lost write receipt')
        def run_turn(client,**kw):
            r=kw['tool_call']('knowledge_read',{'document_id':doc['file_id']})
            kw['tool_call']('write_current',{'content':'written but unconfirmed','expected_sha256':r['sha256']})
        with patch('codex_workspace.CodexWorkspace.run_turn',run_turn), patch.object(self.desk.vault,'save',failing_save):
            self.desk.workspace_run(job,session,{'id':'test'},'保存到当前笔记',doc,'',True,threading.Event())
        self.assertEqual(self.doc.read_text(),'written but unconfirmed')
        self.assertEqual(self.desk.store.all('workspace-write')[0]['status'],'uncertain')
        final=self.desk.store.get(job['id'],'workspace-job')
        self.assertEqual(final['status'],'failed');self.assertEqual(final['write_receipts'][0]['status'],'uncertain')

    def test_oversized_unreturned_excerpt_cannot_authorize_write_or_citation(self):
        self.doc.write_text('x'*510000)
        doc=self.desk.vault.read('notes:note.md')
        session=self.desk.store.put('workspace-session', {'title':'large'})
        job=self.desk.store.put('workspace-job', {'session_id':session['id'],'status':'queued'})
        def run_turn(client,**kw):
            with self.assertRaisesRegex(ValueError,'过大'):
                kw['tool_call']('knowledge_read',{'document_id':doc['file_id']})
            with self.assertRaisesRegex(ValueError,'实际读取'):
                kw['tool_call']('write_current',{'content':'wrong','expected_sha256':doc['sha']})
            return {'text':'请拆分长行','status':'completed'}
        with patch('codex_workspace.CodexWorkspace.run_turn',run_turn):
            self.desk.workspace_run(job,session,{'id':'test'},'保存到当前笔记',doc,'',True,threading.Event())
        final=self.desk.store.get(job['id'],'workspace-job')
        self.assertEqual(final['references'],[]);self.assertEqual(self.doc.read_text(),'x'*510000)

    def test_tool_checkpoints_survive_interrupted_process(self):
        session=self.desk.store.put('workspace-session', {'title':'checkpoint'})
        job=self.desk.store.put('workspace-job', {'session_id':session['id'],'status':'queued'})
        def run_turn(client,**kw):
            kw['on_event']({'type':'thread','thread_id':'durable','actual_model':'test'})
            kw['tool_call']('knowledge_read',{'document_id':'notes:note.md'})
            kw['on_event']({'type':'tool','name':'knowledge_read','args':{'document_id':'notes:note.md'},'call_id':'r1','status':'completed'})
            raise SystemExit('simulated process exit')
        with patch('codex_workspace.CodexWorkspace.run_turn',run_turn),self.assertRaises(SystemExit):
            self.desk.workspace_run(job,session,{'id':'test'},'读取当前笔记',None,'',False,threading.Event())
        self.desk.init_workspace()
        final=self.desk.store.get(job['id'],'workspace-job')
        self.assertEqual(final['status'],'interrupted');self.assertEqual(final['actual_model'],'test')
        self.assertEqual(final['references'][0]['id'],'notes:note.md')
        self.assertEqual(final['tool_events'][0]['args']['document_id'],'notes:note.md')

    def test_commentary_deltas_merge_and_checkpoints_are_throttled(self):
        session=self.desk.store.put('workspace-session', {'title':'stream'})
        job=self.desk.store.put('workspace-job', {'session_id':session['id'],'status':'queued'})
        def run_turn(client,**kw):
            for i in range(300):
                kw['on_event']({'type':'progress','channel':'commentary','item_id':'c1','text':'字'})
                kw['on_event']({'type':'delta','text':'答'})
            return {'text':'答'*300,'status':'completed'}
        with patch('codex_workspace.CodexWorkspace.run_turn',run_turn),patch('workspace_api.time.monotonic',return_value=100),patch.object(self.desk.store,'change',wraps=self.desk.store.change) as changes:
            self.desk.workspace_run(job,session,{'id':'test'},'你好',None,'',False,threading.Event())
        final=self.desk.store.get(job['id'],'workspace-job')
        self.assertEqual(len(final['progress_events']),1);self.assertEqual(final['progress_events'][0]['text'],'字'*300)
        self.assertLess(changes.call_count,8)

    def test_queued_cancellation_never_starts_model(self):
        session=self.desk.store.put('workspace-session', {'title':'cancel'})
        job=self.desk.store.put('workspace-job', {'session_id':session['id'],'status':'queued'})
        stop=threading.Event();stop.set()
        with patch('codex_workspace.CodexWorkspace.run_turn',side_effect=AssertionError('must not call model')):
            self.desk.workspace_run(job,session,{'id':'test'},'你好',None,'',False,stop)
        self.assertEqual(self.desk.store.get(job['id'],'workspace-job')['status'],'cancelled')

    def test_line_coverage_intervals_remain_constant_space_for_large_ranges(self):
        from workspace_api import _cover
        coverage=_cover([],0,1000000)
        coverage=_cover(coverage,2,1000)
        self.assertEqual(coverage,[(0,1000000)])



class GrowthWorkspaceTests(unittest.TestCase):
    setUp = WorkspaceAPITest.setUp
    tearDown = WorkspaceAPITest.tearDown
    def fixture(self):
        advisor=self.root/'advisor';advisor.mkdir()
        (advisor/'SKILL.md').write_text('\n'.join('方法'+str(i) for i in range(305)))
        (advisor/'references').mkdir();(advisor/'references/source.md').write_text('证据方法')
        (self.vault/'other.md').write_text('私人限定标记')
        self.desk.settings['vault_roots']=[{'id':'upward','path':str(self.vault),'writable':True}]
        with patch.object(self.desk,'advisor_entries',return_value=[{'name':'test-advisor','title':'测试顾问','installed_path':str(advisor),'status':'installed_verified'}]):
            self.desk.init_workspace()

    def run_growth(self, callback, message='帮我分析', policy=None, current=None):
        session=self.desk.store.put('workspace-session',{'title':'成长测试'})
        job=self.desk.store.put('workspace-job',{'session_id':session['id'],'status':'queued','request_context':policy or {}})
        def fake(client,**kw):
            callback(kw)
            return {'text':'完成','status':'completed','actual_model':'fake'}
        with patch('codex_workspace.CodexWorkspace.run_turn',fake):
            self.desk.workspace_run(job,session,{'id':'fake'},message,current,'',False,threading.Event())
        actual=self.desk.store.get(job['id'],'workspace-job')
        self.assertEqual(actual['status'],'completed',actual.get('error'))
        return actual

    def test_scope_enforced_on_all_tools_and_context(self):
        self.fixture();doc=self.desk.vault.read('upward:note.md')
        for scope in ('advisors','document'):
            def check(kw):
                tool=kw['tool_call'];self.assertNotIn('growth_snapshot',kw['context']);self.assertNotIn('local_capabilities',kw['context'])
                self.assertEqual(tool('knowledge_search',{'query':'私人限定标记'})['results'],[])
                for name,args in [('knowledge_search',{'query':'私人','root_id':'upward'}),('knowledge_tree',{'root_id':'upward'}),('knowledge_read',{'document_id':'upward:other.md'}),('local_action',{'action':'system.info','user_quote':'查看电脑'}),('create_growth_note',{'kind':'idea','title':'x','content':'x'})]:
                    with self.assertRaises(PermissionError):tool(name,args)
                if scope=='document':
                    self.assertIn('原文',tool('knowledge_read',{'document_id':'upward:note.md'})['text'])
                    self.assertEqual(tool('knowledge_search',{'query':'原文'})['results'][0]['file_id'],'upward:note.md')
                else:
                    with self.assertRaises(PermissionError):tool('knowledge_read',{'document_id':'upward:note.md'})
            self.run_growth(check,policy={'scope':scope},current=doc if scope=='document' else None)

    def test_read_and_select_advisor_are_separate_and_full_skill_required(self):
        self.fixture()
        def check(kw):
            tool=kw['tool_call'];args={'name':'test-advisor','reason':'用顾问的方法澄清当前目标'}
            tool('knowledge_read',{'document_id':'advisor-test-advisor:references/source.md'})
            with self.assertRaises(ValueError):tool('select_advisor',args)
            tool('knowledge_read',{'document_id':'advisor-test-advisor:SKILL.md','limit':300})
            with self.assertRaises(ValueError):tool('select_advisor',args)
            tool('knowledge_read',{'document_id':'advisor-test-advisor:SKILL.md','offset':300})
            selected=tool('select_advisor',args);self.assertEqual(selected['status'],'selected');self.assertEqual(len(selected['reference_ids']),1)
        actual=self.run_growth(check,policy={'scope':'advisors','advisor_names':['test-advisor']})
        self.assertEqual(len(actual['advisor_reads']),1);self.assertEqual(len(actual['advisor_selection']),1)
        def only_read(kw):kw['tool_call']('knowledge_read',{'document_id':'advisor-test-advisor:references/source.md'})
        actual=self.run_growth(only_read,policy={'scope':'advisors'})
        self.assertEqual(actual['advisor_selection'],[]);self.assertEqual(actual['advisor_reads'],[])

    def test_growth_create_has_provenance_and_draft_without_current_file(self):
        self.fixture()
        for kind in ('idea','review','goal'):
            def check(kw):
                args={'kind':kind,'title':'我的成长记录','content':'候选：下周练习'}
                receipt=kw['tool_call']('create_growth_note',args)
                self.assertTrue(receipt['verified']);self.assertEqual(receipt,kw['tool_call']('create_growth_note',args))
                text=self.desk.vault.read(receipt['document_id'])['text']
                self.assertIn('record_kind: "'+kind+'"',text);self.assertIn('source: "codex-workspace"',text)
                if kind=='goal':self.assertIn('status: "draft"',text)
                if kind=='review':self.assertIn('review_period:',text)
            actual=self.run_growth(check,message='帮我保存这次讨论的想法和计划',policy={'days':30})
            self.assertEqual(actual['write_receipts'][0]['status'],'verified');self.assertEqual(len(actual['write_receipts']),1)
        def denied(kw):
            with self.assertRaises(PermissionError):kw['tool_call']('create_growth_note',{'kind':'idea','title':'x','content':'x'})
        self.run_growth(denied,message='不要保存，只讨论')

    def test_review_create_with_protection_writes_one_note_and_preserves_other_files(self):
        self.fixture()
        before = {p: p.read_bytes() for p in self.vault.rglob('*') if p.is_file()}
        message = '把刚才的阶段复盘保存为一篇阶段复盘，标题为“合成验收阶段复盘”，建议仍明确标注为未采纳建议，只保存这一篇，不修改其他目标或文件。'
        def check(kw):
            args = {'kind': 'review', 'title': '合成验收阶段复盘', 'content': '## 未采纳建议\n先验证一项行动。'}
            receipt = kw['tool_call']('create_growth_note', args)
            self.assertTrue(receipt['verified'])
            self.assertEqual(receipt, kw['tool_call']('create_growth_note', args))
            text = self.desk.vault.read(receipt['document_id'])['text']
            self.assertIn('adoption: "not-adopted"', text)
            self.assertIn('## 未采纳建议', text)
            with self.assertRaises(ValueError):
                kw['tool_call']('create_growth_note', dict(args, title='不允许第二篇'))
            with self.assertRaises(PermissionError):
                kw['tool_call']('write_current', {'content':'不允许覆盖', 'expected_sha256':'0'*64})
        actual = self.run_growth(check, message=message)
        self.assertEqual(len(actual['write_receipts']), 1)
        for path, content in before.items(): self.assertEqual(path.read_bytes(), content)
        after = {p for p in self.vault.rglob('*') if p.is_file()}
        self.assertEqual(len(after-set(before)), 1)

    def test_scoped_synthetic_review_request_creates_verified_record_without_other_changes(self):
        self.fixture()
        before = {p:p.read_bytes() for p in self.vault.rglob('*') if p.is_file()}
        message = '这是公开合成验收，不是本人资料。10月12日至18日，我练习英语3次，完成摄影草稿1篇；没有发布记录，不能把草稿算作已发布。请实际读取并采用“阶段反思顾问”及必要参考，简短复盘这7天的变化，分清事实与解释，然后把本次阶段复盘保存为新的成长复盘记录，标题“合成一周实践复盘”。只保存复盘，不采纳新目标、不更改其他记录。最后简短说明实际依据和保存位置。'
        def check(kw):
            receipt = kw['tool_call']('create_growth_note', {'kind':'review', 'title':'合成一周实践复盘',
                'content':'## 事实\n英语练习3次，摄影草稿1篇；没有核验发布记录。\n\n## 未采纳建议\n保留可比练习样本。'})
            self.assertTrue(receipt['verified'])
            self.assertTrue(self.desk.vault._split(receipt['document_id'])[1].startswith('阶段复盘/'))
            text = self.desk.vault.read(receipt['document_id'])['text']
            self.assertIn('adoption: "not-adopted"', text)
            self.assertIn('status: "recorded"', text)
            self.assertIn('review_period:', text)
            with self.assertRaises(PermissionError):
                kw['tool_call']('write_current', {'content':'不得覆盖', 'expected_sha256':'0'*64})
        actual = self.run_growth(check, message=message)
        self.assertEqual(len(actual['write_receipts']), 1)
        self.assertEqual(actual['write_receipts'][0]['status'], 'verified')
        for path,content in before.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual(len({p for p in self.vault.rglob('*') if p.is_file()} - set(before)), 1)

    def test_record_fact_or_correction_cannot_create_growth_file(self):
        self.fixture()
        before = {p:p.read_bytes() for p in self.vault.rglob('*') if p.is_file()}
        for message in ['记录显示我本周练习了三次。', '记录里说我已经完成了计划，这是错误的。',
                        '记录中提到我想换方向。', '记录显示练习三次。请简短分析。',
                        '记下的资料显示我本周练习了三次。', '记下的内容需要分析，不需要新增笔记。',
                        '请保存这次复盘，但不要新增任何记录。']:
            def denied(kw):
                with self.assertRaises(PermissionError):
                    kw['tool_call']('create_growth_note', {'kind':'review', 'title':'不应新建', 'content':'无保存命令'})
            actual = self.run_growth(denied, message=message)
            self.assertEqual(actual['write_receipts'], [])
            self.assertEqual({p:p.read_bytes() for p in self.vault.rglob('*') if p.is_file()}, before)
        self.assertEqual(self.desk.store.all('workspace-write'), [])

    def test_growth_create_keeps_one_exact_title_and_preserves_body(self):
        self.fixture()
        for content in ('阶段事实与未采纳建议', '# 单一标题\n\n阶段事实与未采纳建议', '# 不同的小节标题\n\n阶段事实与未采纳建议', '    # 单一标题\n\n保留代码块'):
            def check(kw):
                receipt = kw['tool_call']('create_growth_note', {'kind':'review','title':'单一标题','content':content})
                saved = self.desk.vault.read(receipt['document_id'])['text'].split('\n---\n\n',1)[1]
                self.assertEqual(saved.splitlines().count('# 单一标题'), 1)
                self.assertIn(content, saved)
                self.assertTrue(receipt['verified'])
            self.run_growth(check, message='保存本阶段复盘')

    def test_local_tools_use_existing_permissions_and_current_user_words(self):
        self.fixture();self.desk.settings['local_access']={'enabled':True,'notes_enabled':False,'read_roots':[str(self.vault)],'write_roots':[str(self.vault)]}
        message='查看电脑信息'
        def check(kw):
            result=kw['tool_call']('local_action',{'action':'system.info','user_quote':message})
            self.assertEqual(result['actions'][0]['status'],'completed')
            with self.assertRaises(PermissionError):kw['tool_call']('local_action',{'action':'files.read','user_quote':message,'path':str(self.doc)})
        with patch.object(self.desk,'_system_info',return_value={'os':'fixture'}):self.run_growth(check,message=message)
        def denied(kw):
            with self.assertRaises(PermissionError):kw['tool_call']('local_action',{'action':'system.info','user_quote':'查看电脑信息'})
        self.run_growth(denied,message='不要查看电脑信息')

    def test_scope_change_drops_old_model_thread_and_ignores_private_attachment(self):
        self.fixture();self.desk.cli='fixture'
        sid=self.desk.store.put('workspace-session',{'title':'旧','thread_id':'private-old'})['id']
        with patch.object(self.desk,'resolve_model',return_value={'id':'fake'}),patch('workspace_api.threading.Thread.start'):
            reply=self.desk.workspace_send({'session_id':sid,'message':'只问顾问方法','scope':'advisors','current_file_id':'not-readable:any'})
        self.assertIsNone(reply['session']['thread_id']);self.assertEqual(reply['job']['request_context']['scope'],'advisors')
        self.assertIsNone(reply['job']['request_context']['current_file_id'])

    def test_personal_snapshot_receives_exact_purpose_and_period(self):
        self.fixture()
        def check(kw):self.assertEqual(kw['context']['growth_snapshot'],{'scope_marker':'requested'})
        with patch.object(self.desk,'growth_context',return_value={'scope_marker':'requested'},create=True) as growth:
            self.run_growth(check,policy={'purpose':'review','days':30})
            growth.assert_called_once_with('review',30)

    def test_goal_candidates_cannot_smuggle_adoption_or_completion_in_body(self):
        self.fixture()
        def check(kw):
            receipt=kw['tool_call']('create_growth_note',{'kind':'goal','title':'验证目标','content':'状态：已采用\n- [x] 自动完成'})
            text=self.desk.vault.read(receipt['document_id'])['text']
            self.assertNotIn('状态：已采用',text);self.assertIn('状态：候选',text);self.assertIn('- [ ] 自动完成',text)
            self.goal_id=receipt['document_id']
        self.run_growth(check,message='保存这个目标方案')
        def update(kw):
            tool=kw['tool_call'];r=tool('knowledge_read',{'document_id':self.goal_id,'limit':300})
            args={'document_id':self.goal_id,'expected_sha256':r['sha256'],'status':'active','content':'# 验证目标\n状态：done\n- [x] 新行动\n  - 日期：2026-12-31\n  - 完成标准：发布'}
            receipt=tool('update_growth_goal',args);text=self.desk.vault.read(self.goal_id)['text']
            self.assertTrue(receipt['verified']);self.assertIn('状态：进行中',text);self.assertNotIn('状态：done',text);self.assertIn('- [ ] 新行动',text)
        self.run_growth(update,message='我采用验证目标，安排今天执行。')
        def revise(kw):
            tool=kw['tool_call'];r=tool('knowledge_read',{'document_id':self.goal_id,'limit':300})
            args={'document_id':self.goal_id,'expected_sha256':r['sha256'],'status':'paused','content':'# 验证目标\n- [ ] 新行动\n  - 日期：2026-12-30'}
            with self.assertRaises(PermissionError):tool('update_growth_goal',args)
            args['status']='active';self.assertTrue(tool('update_growth_goal',args)['verified'])
        self.run_growth(revise,message='请更新验证目标的截止日期')
        def finish(kw):
            tool=kw['tool_call'];r=tool('knowledge_read',{'document_id':self.goal_id,'limit':300})
            args={'document_id':self.goal_id,'expected_sha256':r['sha256'],'status':'done','content':'# 验证目标\n- [x] 新行动'}
            with self.assertRaises(ValueError):tool('update_growth_goal',args)
            args['evidence']='本人确认已发布';result=tool('update_growth_goal',args)
            self.assertEqual(result['status'],'done');self.assertIn('状态：已完成',self.desk.vault.read(self.goal_id)['text'])
        self.run_growth(finish,message='我已完成验证目标，已经发布了')

    def test_goal_update_rejects_negation_wrong_target_and_partial_read(self):
        self.fixture();path='目标与行动/goal.md'
        self.desk.vault.create('upward',path,'---\nrecord_kind: "goal"\ntitle: "另一个目标"\nstatus: "draft"\n---\n\n# 另一个目标\n- [ ] 写作')
        fid='upward:'+__import__('urllib.parse',fromlist=['quote']).quote(path,safe='/')
        def negative(kw):
            tool=kw['tool_call'];r=tool('knowledge_read',{'document_id':fid,'limit':300})
            with self.assertRaises(PermissionError):tool('update_growth_goal',{'document_id':fid,'expected_sha256':r['sha256'],'status':'active','content':'x'})
        self.run_growth(negative,message='不要采纳另一个目标')
        self.run_growth(negative,message='请采纳不存在的目标')
        def partial(kw):
            tool=kw['tool_call'];r=tool('knowledge_read',{'document_id':fid,'limit':1})
            with self.assertRaises(ValueError):tool('update_growth_goal',{'document_id':fid,'expected_sha256':r['sha256'],'status':'active','content':'x'})
        self.run_growth(partial,message='请采纳另一个目标')

    def test_conversation_goal_breakdown_preserves_adoption_and_projects_dated_real_actions(self):
        import datetime as dt
        from phase import TZ
        self.fixture();today=dt.datetime.now(TZ).date().isoformat()
        path='目标与行动/breakdown.md'
        original='---\nrecord_kind: "goal"\ntitle: "合成学习目标"\nstatus: "active"\nadoption: "adopted"\n---\n\n状态：进行中\n- [x] 保留已完成行动\n  - 实际结果：本人先前已确认\n'
        value=self.desk.vault.create('upward',path,original);fid=value['file_id']
        body=('## 行动\n- [x] 保留已完成行动\n  - 实际结果：本人先前已确认\n'
              '- [x] 今天读一章\n  - 日期：'+today+'\n  - 完成标准：写出三个要点\n  - 实际结果：尚未执行\n'
              '- [ ] 未来练习\n  - 日期：2099-01-01\n  - 完成标准：做一次演练\n'
              '- [ ] 待确认的下一步\n  - 完成标准：先确认可用时间\n')
        def split(kw):
            tool=kw['tool_call'];read=tool('knowledge_read',{'document_id':fid,'limit':300})
            result=tool('update_growth_goal',{'document_id':fid,'expected_sha256':read['sha256'],'status':'active','content':body})
            self.assertTrue(result['verified'])
        self.run_growth(split,message='把合成学习目标拆成三个行动')
        snapshot=self.desk.growth_snapshot()
        goal=next(g for g in snapshot['goals'] if g.get('file_id')==fid)
        self.assertEqual(goal['status'],'active')
        self.assertTrue(goal['steps'][0]['done'])
        self.assertFalse(goal['steps'][1]['done'])
        self.assertEqual(goal['steps'][1]['criterion'],'写出三个要点')
        self.assertEqual(goal['steps'][1]['outcome'],'尚未执行')
        self.assertEqual([s['text'] for s in snapshot['today']['actions']],['今天读一章'])
        self.assertEqual([s['text'] for s in snapshot['today']['unscheduled']],['待确认的下一步'])
        for message in ('不要拆分合成学习目标','他说把合成学习目标拆成三个行动','能否拆分合成学习目标？',
                        '拆分合成学习目标的结果还太粗糙，需要再讨论。','拆分后的合成学习目标只是一个草稿。',
                        '拆分不是当前要做的事。','把合成学习目标拆成三个行动的结果还太粗糙，需要再讨论。'):
            before=self.desk.vault.read(fid)['sha'];journals=len(self.desk.store.all('workspace-write'))
            def denied(kw):
                read=kw['tool_call']('knowledge_read',{'document_id':fid,'limit':300})
                with self.assertRaises(PermissionError):
                    kw['tool_call']('update_growth_goal',{'document_id':fid,'expected_sha256':read['sha256'],'status':'active','content':body})
            self.run_growth(denied,message=message)
            self.assertEqual(self.desk.vault.read(fid)['sha'],before)
            self.assertEqual(len(self.desk.store.all('workspace-write')),journals)

    def test_explicit_goal_split_other_file_protection_preserves_other_files_and_current_target_ban(self):
        self.fixture()
        original='---\nrecord_kind: "goal"\ntitle: "合成学习目标"\nstatus: "active"\nadoption: "adopted"\n---\n\n状态：进行中\n- [ ] 原步骤\n'
        target=self.desk.vault.create('upward','目标与行动/protected-split.md',original);fid=target['file_id']
        before={p:p.read_bytes() for p in self.vault.rglob('*') if p.is_file()}
        def split(kw):
            read=kw['tool_call']('knowledge_read',{'document_id':fid,'limit':300})
            result=kw['tool_call']('update_growth_goal',{'document_id':fid,'expected_sha256':read['sha256'],'status':'active','content':'- [ ] 拆分后行动\n  - 完成标准：合成验收'})
            self.assertTrue(result['verified'])
        self.run_growth(split,message='请拆分合成学习目标，不修改其他文件。')
        self.assertNotEqual(self.desk.vault.read(fid)['sha'],target['sha'])
        path=self.vault/'目标与行动/protected-split.md'
        for other,content in before.items():
            if other!=path:self.assertEqual(other.read_bytes(),content)
        for message in ['请拆分合成学习目标，不要修改这个目标。','请拆分合成学习目标，不修改任何文件。',
                        '请拆分合成学习目标，不做任何写入。','他说请拆分合成学习目标，不修改其他文件。',
                        '如果有空，请拆分合成学习目标，不修改其他文件。','昨天我让你拆分合成学习目标，不修改其他文件。']:
            all_before={p:p.read_bytes() for p in self.vault.rglob('*') if p.is_file()};journals=len(self.desk.store.all('workspace-write'))
            def denied(kw):
                read=kw['tool_call']('knowledge_read',{'document_id':fid,'limit':300})
                with self.assertRaises(PermissionError):
                    kw['tool_call']('update_growth_goal',{'document_id':fid,'expected_sha256':read['sha256'],'status':'active','content':'- [ ] 不应改动'})
            self.run_growth(denied,message=message)
            self.assertEqual({p:p.read_bytes() for p in self.vault.rglob('*') if p.is_file()},all_before)
            self.assertEqual(len(self.desk.store.all('workspace-write')),journals)

    def test_growth_create_uses_existing_custom_writable_root(self):
        def check(kw):
            receipt=kw['tool_call']('create_growth_note',{'kind':'idea','title':'自定义根想法','content':'保存到当前登记知识库'})
            self.assertTrue(receipt['document_id'].startswith('notes:'))
        self.run_growth(check,message='帮我记下这个想法')

    def test_duplicate_goal_titles_cannot_authorize_arbitrary_target(self):
        self.fixture()
        text='---\nrecord_kind: "goal"\ntitle: "内容创作目标"\nstatus: "draft"\n---\n\n# 内容创作目标\n- [ ] 发布'
        first=self.desk.vault.create('upward','目标与行动/A.md',text)
        second=self.desk.vault.create('upward','目标与行动/B.md',text)
        def check(kw):
            r=kw['tool_call']('knowledge_read',{'document_id':second['file_id'],'limit':300})
            with self.assertRaisesRegex(PermissionError,'同名'):
                kw['tool_call']('update_growth_goal',{'document_id':second['file_id'],'expected_sha256':r['sha256'],'status':'active','content':'# 内容创作目标\n- [ ] 发布'})
        self.run_growth(check,message='请采纳内容创作目标')
        self.assertEqual(self.desk.vault.read(first['file_id'])['sha'],first['sha'])
        self.assertEqual(self.desk.vault.read(second['file_id'])['sha'],second['sha'])

if __name__ == '__main__':
    unittest.main()

class EvidenceVisibilityTests(GrowthWorkspaceTests):
    def test_read_and_search_receipts_publish_without_waiting_for_text(self):
        self.fixture()
        def check(kw):
            kw['on_event']({'type':'progress','turn_id':'accepted-turn','text':'accepted'})
            tool=kw['tool_call']
            result=tool('knowledge_search',{'query':'原文'})
            live=list(self.desk._workspace_live.values())[-1]
            self.assertEqual(live['knowledge_searches'][-1]['count'],len(result['results']))
            self.assertEqual(live['references'],[])
            tool('knowledge_read',{'document_id':'upward:note.md'})
            live=list(self.desk._workspace_live.values())[-1]
            self.assertEqual(live['references'][0]['id'],'upward:note.md')
            self.assertEqual(live['advisor_selection'],[])
        job=self.run_growth(check)
        answer=next(m for m in self.desk.store.all('workspace-message') if m.get('job_id')==job['id'])
        self.assertEqual(answer['knowledge_searches'],job['knowledge_searches'])
        self.assertEqual(answer['provided_context'][0]['status'],'provided')

    def test_previous_receipts_are_separate_and_scope_filtered(self):
        self.fixture()
        session=self.desk.store.put('workspace-session',{'title':'history'})
        prior=self.desk.store.put('workspace-job',{'session_id':session['id'],'status':'failed','request_context':{'scope':'personal'},'references':[{'id':'upward:note.md','title':'private title'}],'advisor_selection':[{'name':'test-advisor','title':'测试顾问','reason':'historical reason'}]})
        job=self.desk.store.put('workspace-job',{'session_id':session['id'],'status':'queued','request_context':{'scope':'personal'}})
        def fake(client,**kw):
            history=kw['context']['previous_evidence']
            self.assertEqual(history['source_job_id'],prior['id'])
            self.assertEqual(history['status'],'historical_only')
            self.assertEqual(history['references'][0]['id'],'upward:note.md')
            return {'text':'继续讨论','status':'completed'}
        with patch('codex_workspace.CodexWorkspace.run_turn',fake):
            self.desk.workspace_run(job,session,{'id':'fake'},'继续讨论',None,'',False,threading.Event())
        result=self.desk.store.get(job['id'],'workspace-job')
        self.assertEqual(result['references'],[])
        self.assertEqual(result['advisor_selection'],[])
        self.assertEqual(result['previous_evidence']['source_job_id'],prior['id'])
        policy=self.desk._workspace_policy({'scope':'advisors'})
        advisors,roots=self.desk._workspace_scope(policy,None)
        self.assertEqual(self.desk._workspace_previous_evidence(job,session,policy,advisors,lambda fid:fid.startswith('advisor-')), {})

    def test_previous_document_scope_cannot_import_other_document(self):
        self.fixture();session=self.desk.store.put('workspace-session',{'title':'scope'})
        self.desk.store.put('workspace-job',{'session_id':session['id'],'status':'completed','request_context':{'scope':'document','current_file_id':'upward:other.md'},'references':[{'id':'upward:other.md','title':'secret'}]})
        job=self.desk.store.put('workspace-job',{'session_id':session['id'],'status':'queued'})
        doc=self.desk.vault.read('upward:note.md');policy=self.desk._workspace_policy({'scope':'document'},doc)
        advisors,roots=self.desk._workspace_scope(policy,doc)
        self.assertEqual(self.desk._workspace_previous_evidence(job,session,policy,advisors,lambda fid:True),{})

class ContextDeliveryTests(unittest.TestCase):
    setUp=WorkspaceAPITest.setUp
    tearDown=WorkspaceAPITest.tearDown
    def test_startup_failure_never_claims_context_was_delivered(self):
        session=self.desk.store.put('workspace-session',{'title':'startup-failure'})
        job=self.desk.store.put('workspace-job',{'session_id':session['id'],'status':'queued'})
        with patch('codex_workspace.CodexWorkspace.run_turn',side_effect=RuntimeError('failed before turn acceptance')):
            self.desk.workspace_run(job,session,{'id':'fake'},'问题',None,'',False,threading.Event())
        result=self.desk.store.get(job['id'],'workspace-job')
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['provided_context'][0]['status'],'prepared')
