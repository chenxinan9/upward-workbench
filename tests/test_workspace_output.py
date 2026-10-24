"""User-clicked output reads, using only synthetic jobs and registered files."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app import Desk


class WorkspaceOutputTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.data=self.root/'data';self.data.mkdir()
        self.vault=self.root/'vault';self.vault.mkdir();self.file=self.vault/'note.md';self.file.write_text('# 原笔记')
        self.plan=self.root/'plan.json'
        self.plan.write_text(json.dumps({'life_panorama':{'title':'NOT-IN-DAILY-PREVIEW'},'current_focus_draft':{'title':'旧重点'},'source_register':[],'domains':[]}))
        (self.data/'settings.json').write_text(json.dumps({'growth_plan':str(self.plan),'vault_roots':[{'id':'upward','path':str(self.vault),'writable':True}]}))
        self.desk=Desk(self.data);self.addCleanup(self.desk.stop_workspace)
        self.session=self.desk.store.put('workspace-session',{'title':'合成会话'})

    def job(self, **values):
        return self.desk.store.put('workspace-job',dict({'session_id':self.session['id'],'status':'queued'},**values))

    def output(self, job, selection='reply'):
        return self.desk.workspace_get('/api/workspace/output',{'job_id':job['id'],'output':selection})

    def test_reply_reads_exact_saved_message_and_streamed_partial_without_path_discovery(self):
        job=self.job(response_preview='正在输出 <script>不可执行</script> /tmp/not-a-real-artifact.html')
        read=self.output(job)
        self.assertEqual(read['status'],'queued');self.assertEqual(read['format'],'markdown')
        self.assertEqual(read['text'],job['response_preview'])
        self.desk.store.put('workspace-message',{'session_id':self.session['id'],'job_id':job['id'],'role':'assistant','text':'已保存的真实回复'})
        self.assertEqual(self.output(job)['text'],'已保存的真实回复')
        self.assertEqual(self.desk.store.all('workspace-artifact'),[])
        self.assertFalse((self.data/'workspace-artifacts').exists())

    def test_verified_vault_output_requires_matching_job_journal_and_reports_later_file_changes(self):
        doc=self.desk.vault.read('upward:note.md')
        receipt={'id':doc['file_id'],'status':'verified','sha':doc['sha'],'title':'原笔记'}
        job=self.job(write_receipts=[receipt])
        with patch.object(self.desk.vault,'read',wraps=self.desk.vault.read) as read:
            with self.assertRaisesRegex(ValueError,'可信写入日志'):self.output(job,'write:0')
            self.assertFalse(read.called)
        self.desk.store.put('workspace-write',{'job_id':job['id'],'document_id':doc['file_id'],'status':'verified','sha':doc['sha']})
        self.assertFalse(self.output(job,'write:0')['changed_since_result'])
        self.file.write_text('# 外部后续修改')
        changed=self.output(job,'write:0')
        self.assertTrue(changed['changed_since_result'])
        self.assertEqual(changed['result_sha'],doc['sha'])
        self.assertEqual(changed['text'],'# 外部后续修改')
        self.assertNotEqual(changed['sha'],changed['result_sha'])

    def test_unverified_or_other_jobs_write_journal_cannot_authorize_a_preview(self):
        doc=self.desk.vault.read('upward:note.md')
        job=self.job(write_receipts=[{'id':doc['file_id'],'sha':doc['sha'],'status':'uncertain'}])
        with self.assertRaisesRegex(ValueError,'回读核验'):self.output(job,'write:0')
        self.desk.store.change(job['id'],'workspace-job',write_receipts=[{'id':doc['file_id'],'sha':doc['sha'],'status':'verified'}])
        other=self.job()
        self.desk.store.put('workspace-write',{'job_id':other['id'],'document_id':doc['file_id'],'sha':doc['sha'],'status':'verified'})
        with self.assertRaisesRegex(ValueError,'可信写入日志'):self.output(job,'write:0')

    def test_artifact_tool_persists_real_job_and_reply_metadata_in_public_advisor_scope_without_knowledge_writes(self):
        before=self.file.read_bytes(),self.plan.read_bytes()
        job=self.job(request_context={'scope':'advisors','execution_mode':'knowledge'})
        def fake(client,**kw):
            create=kw['tool_call']
            first=create('create_workspace_artifact',{'title':'合成网页','format':'html','content':'<script>bad()</script><h1>合成内容</h1>'})
            second=create('create_workspace_artifact',{'title':'合成代码','format':'code','content':'print("never executed")'})
            self.assertTrue(first['verified']);self.assertTrue(second['verified'])
            self.assertEqual(first,create('create_workspace_artifact',{'title':'合成网页','format':'html','content':'<script>bad()</script><h1>合成内容</h1>'}))
            return {'text':'两份真实会话成果。/tmp/made-up.html不是成果。','status':'completed'}
        with patch('codex_workspace.CodexWorkspace.run_turn',fake):
            self.desk.workspace_run(job,self.session,{'id':'fake'},'制作一份合成网页和代码',None,'',False,threading.Event())
        saved=self.desk.store.get(job['id'],'workspace-job')
        self.assertEqual(saved['status'],'completed',saved.get('error'))
        self.assertEqual(len(saved['artifacts']),2);self.assertFalse(saved['write_receipts'])
        reply=next(v for v in self.desk.store.all('workspace-message') if v.get('job_id')==job['id'])
        self.assertEqual(reply['artifacts'],saved['artifacts'])
        preview=self.output(job,'artifact:'+saved['artifacts'][0]['id'])
        self.assertEqual(preview['status'],'completed')
        self.assertEqual(preview['artifact_status'],'verified')
        self.assertIn('<script>bad()</script>',preview['text'])
        self.assertNotIn('bad()',preview['preview_html'])
        self.assertEqual((self.file.read_bytes(),self.plan.read_bytes()),before)
        other=self.job()
        with self.assertRaises(ValueError):self.output(other,'artifact:'+saved['artifacts'][0]['id'])

    def test_completed_artifact_survives_interruption_before_reply_and_restores_real_receipt(self):
        job=self.job(request_context={'scope':'advisors'})
        def interrupted(client,**kw):
            kw['tool_call']('create_workspace_artifact',{'title':'中断前成果','format':'text','content':'已回读的成果'})
            raise SystemExit('synthetic interruption')
        with patch('codex_workspace.CodexWorkspace.run_turn',interrupted),self.assertRaises(SystemExit):
            self.desk.workspace_run(job,self.session,{'id':'fake'},'生成合成成果',None,'',False,threading.Event())
        self.desk=Desk(self.data);self.addCleanup(self.desk.stop_workspace)
        saved=self.desk.store.get(job['id'],'workspace-job')
        self.assertEqual(saved['status'],'interrupted')
        self.assertEqual(len(saved['artifacts']),1)
        output=self.output(job,'artifact:'+saved['artifacts'][0]['id'])
        self.assertEqual(output['text'],'已回读的成果')
        self.assertEqual(output['status'],'interrupted')
        self.assertEqual(output['artifact_status'],'verified')

    def test_verified_artifact_does_not_turn_a_failed_job_into_a_success(self):
        job=self.job(request_context={'scope':'advisors'})
        def failed(client,**kw):
            kw['tool_call']('create_workspace_artifact',{'title':'失败前成果','format':'text','content':'真实生成但整轮未完成'})
            raise RuntimeError('synthetic transport failure')
        with patch('codex_workspace.CodexWorkspace.run_turn',failed):
            self.desk.workspace_run(job,self.session,{'id':'fake'},'生成合成成果',None,'',False,threading.Event())
        saved=self.desk.store.get(job['id'],'workspace-job')
        self.assertEqual(saved['status'],'failed')
        output=self.output(job,'artifact:'+saved['artifacts'][0]['id'])
        self.assertEqual(output['status'],'failed')
        self.assertEqual(output['artifact_status'],'verified')
        self.assertEqual(output['text'],'真实生成但整轮未完成')

    def test_json_plan_preview_shows_only_real_written_section_and_supports_verified_no_change(self):
        for number in range(2):
            job=self.job()
            def fake(client,**kw):
                before=kw['tool_call']('read_growth_plan',{})
                result=kw['tool_call']('update_growth_plan',{'section':'daily','expected_sha256':before['sha256'],'content':'{"title":"合成今日实验"}'})
                self.assertEqual(result['changed'],number==0)
                return {'text':'已核对重点','status':'completed'}
            with patch('codex_workspace.CodexWorkspace.run_turn',fake):
                self.desk.workspace_run(job,self.session,{'id':'fake'},'把今日重点改为合成今日实验。',None,'',False,threading.Event())
            read=self.output(job,'write:0')
            self.assertEqual(read['format'],'json')
            self.assertIn('合成今日实验',read['text'])
            self.assertNotIn('NOT-IN-DAILY-PREVIEW',read['text'])
            self.assertFalse(read['changed_since_result'])
            self.assertEqual(set(json.loads(read['text'])),{'current_focus_draft'})

    def test_invalid_ids_selections_missing_reply_and_plaintext_paths_are_not_artifact_capabilities(self):
        job=self.job()
        for identifier in (None,True,'../outside','x'*33):
            with self.assertRaises(ValueError):self.desk.workspace_get('/api/workspace/output',{'job_id':identifier})
        for output in ('reply','../../etc/passwd','artifact:/tmp/fake.md','write:-1','write:9999'):
            with self.subTest(output=output),self.assertRaises(ValueError):self.output(job,output)


if __name__=='__main__':unittest.main()
