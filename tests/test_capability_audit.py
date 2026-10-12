"""Independent capability boundary checks using synthetic files and no model API."""
import base64
import builtins
import io
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from attachments import AttachmentStore, _office
from codex_native import approval_response
import app


def encode(data): return base64.b64encode(data).decode()
def office_bytes(raw, filename='word/document.xml'):
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as z: z.writestr(filename,raw)
    return stream.getvalue()


class IndependentCapabilityAudit(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        self.store=AttachmentStore(self.root)
    def tearDown(self):self.tmp.cleanup()
    def test_file_name_cannot_escape(self):
        for name in ('../out.txt','/tmp/out.txt','nested/out.txt','nested\\out.txt'):
            with self.subTest(name=name),self.assertRaises(ValueError):self.store.upload(name,encode(b'ok'))
        self.assertEqual(list(self.store.root.iterdir()),[])
    def test_attachments_cannot_rebind_or_read_across_sessions(self):
        a=self.store.upload('sample.txt',encode(b'SYNTHETIC A'))
        self.store.bind([a['id']],'A')
        with self.assertRaises(PermissionError):self.store.bind([a['id']],'B')
        with self.assertRaises(PermissionError):self.store.read(a['id'],'B')
        self.assertEqual(self.store.read(a['id'],'A')['text'],'SYNTHETIC A')
    def test_failed_batch_does_not_partly_bind(self):
        a=self.store.upload('a.txt',encode(b'a'));b=self.store.upload('b.txt',encode(b'b'))
        self.store.bind([b['id']],'B')
        with self.assertRaises(PermissionError):self.store.bind([a['id'],b['id']],'A')
        self.assertIsNone(self.store._load(a['id'])['session_id'])
    def test_source_text_symlink_is_rejected(self):
        a=self.store.upload('sample.txt',encode(b'a'));self.store.bind([a['id']],'A')
        p=self.store.root/a['id']/'text.txt';p.unlink();target=self.root/'sentinel.txt';target.write_text('SYNTHETIC OUTSIDE');p.symlink_to(target)
        with self.assertRaises(ValueError):self.store.read(a['id'],'A')
    def test_office_does_not_execute_archive_path_entries(self):
        self.store.upload('sample.docx',encode(office_bytes(b'<d><t>SYNTHETIC</t></d>','word/../document.xml')))
        self.assertFalse((self.root/'document.xml').exists())
    def test_utf8_xml_entities_are_rejected(self):
        xml=b'<!DOCTYPE d [<!ENTITY a "SYNTHETIC ENTITY">]><d><t>&a;</t></d>'
        with self.assertRaises(ValueError):_office(office_bytes(xml),'.docx')
    def test_utf16_xml_entities_are_rejected(self):
        xml='<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE d [<!ENTITY a "SYNTHETIC ENTITY">]><d><t>&a;</t></d>'
        with self.assertRaises(ValueError):_office(office_bytes(xml.encode('utf-16')),'.docx')
    def test_corrupt_image_is_not_marked_ready(self):
        with self.assertRaises(ValueError):self.store.upload('broken.png',encode(b'\x89PNG\r\n\x1a\n'))
    def test_valid_png_is_ready_and_roundtrips_to_local_image(self):
        # Real one-pixel PNG: exercise the image decoder, not just the signature.
        raw=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNgSPkPAAHLAWRRDtICAAAAAElFTkSuQmCC')
        item=self.store.upload('synthetic.png',encode(raw))
        self.assertEqual(item['status'],'ready');self.assertEqual(item['kind'],'image')
        self.store.bind([item['id']],'A')
        inputs=self.store.image_inputs([item['id']],'A')
        self.assertEqual(inputs[0]['type'],'localImage')
        self.assertEqual(Path(inputs[0]['path']).read_bytes(),raw)
    def test_missing_image_dependencies_reports_actionable_error(self):
        original_import=builtins.__import__
        def without_pillow(name,*args,**kwargs):
            if name=='PIL' or name.startswith('PIL.'):
                raise ImportError('synthetic environment without Pillow')
            return original_import(name,*args,**kwargs)
        raw=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNgSPkPAAHLAWRRDtICAAAAAElFTkSuQmCC')
        with patch('builtins.__import__',side_effect=without_pillow),patch('attachments.shutil.which',return_value=None):
            with self.assertRaisesRegex(ValueError,'pip install Pillow'):
                self.store.upload('synthetic.png',encode(raw))
        self.assertEqual(list(self.store.root.iterdir()),[])
    def test_pdf_extractor_output_is_bounded(self):
        tool=self.root/'fake-pdftotext'
        tool.write_text('#!/usr/bin/env python3\nimport sys\nsys.stdout.write("SYNTHETIC"*200000)\n')
        tool.chmod(0o700)
        with patch('attachments.shutil.which',return_value=str(tool)):
            result=self.store.upload('synthetic.pdf',encode(b'%PDF-synthetic-test'))
        self.assertLessEqual(result['text_characters'],300000)
        self.assertIn('120',result['note'])
    def test_xlsx_preserves_coordinates_shared_strings_and_formula_notice(self):
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as z:
            z.writestr('xl/sharedStrings.xml','<sst><si><t>SYNTHETIC ITEM</t></si></sst>')
            z.writestr('xl/worksheets/sheet1.xml','<worksheet><c r="A1" t="s"><v>0</v></c><c r="B1"><f>2+3</f><v>5</v></c></worksheet>')
        text,note=_office(stream.getvalue(),'.xlsx')
        self.assertIn('A1: SYNTHETIC ITEM',text);self.assertIn('B1: 5',text);self.assertIn('2+3',text);self.assertIn('未重算',note)
    def test_powerpoint_uses_natural_slide_order(self):
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as z:
            for n in (10,2,1):z.writestr('ppt/slides/slide'+str(n)+'.xml','<slide><t>PAGE '+str(n)+'</t></slide>')
        text,note=_office(stream.getvalue(),'.pptx')
        self.assertLess(text.index('PAGE 1'),text.index('PAGE 2'));self.assertLess(text.index('PAGE 2'),text.index('PAGE 10'))
    def test_persistent_and_expanded_grants_are_rejected(self):
        self.assertEqual(approval_response('item/commandExecution/requestApproval',{}, {'decision':'acceptForSession'}),{'decision':'decline'})
        self.assertEqual(approval_response('item/permissions/requestApproval',{'permissions':{'network':{'enabled':False}}},{'permissions':{'network':{'enabled':True}}}),{'permissions':{},'scope':'turn'})
    def _approval(self, method, params, action, answers=None, wrong_job=False, cancel=False, resolved=False):
        desk=app.Desk(self.root/'desk');job=desk.store.put('workspace-job',{'status':'running','session_id':'A'})
        stop=threading.Event();resolved_event=threading.Event();live={'requests':[]};result=[]
        worker=threading.Thread(target=lambda:result.append(desk.workspace_approval(job,live,{'method':method,'params':params,'cancel_event':resolved_event},stop,lambda e:None)))
        worker.start()
        try:
            deadline=time.monotonic()+3
            while not live['requests'] and time.monotonic()<deadline:time.sleep(.005)
            self.assertTrue(live['requests']);rid=live['requests'][0]['id']
            data={'request_id':rid,'job_id':job['id'],'decision':action,'answers':answers or {}}
            if wrong_job:
                with self.assertRaises(ValueError):desk.workspace_respond(dict(data,job_id='ANOTHER_JOB'))
                self.assertFalse(desk._workspace_requests[rid]['event'].is_set())
            if resolved:resolved_event.set()
            elif cancel:stop.set()
            else:
                desk.workspace_respond(data)
                with self.assertRaises(ValueError):desk.workspace_respond(data)
            worker.join(3);self.assertFalse(worker.is_alive());self.assertNotIn(rid,desk._workspace_requests)
            return result[0]
        finally:
            stop.set();worker.join(3);desk.stop_workspace()
    def test_approval_wrong_job_replay_and_completion(self):
        self.assertEqual(self._approval('item/commandExecution/requestApproval',{'command':'echo synthetic'},'approve',wrong_job=True),{'decision':'accept'})
    def test_cancelled_approval_never_accepts(self):
        self.assertEqual(self._approval('item/commandExecution/requestApproval',{},'approve',cancel=True),{'decision':'decline'})
    def test_server_resolved_request_disappears_without_waiting_for_turn_end(self):
        self.assertEqual(self._approval('item/commandExecution/requestApproval',{},'approve',resolved=True),{'decision':'decline'})
    def test_rejected_user_input_does_not_send_entered_answers(self):
        result=self._approval('item/tool/requestUserInput',{'questions':[{'id':'q','question':'Synthetic?'}]},'reject',{'q':['SYNTHETIC SHOULD STAY UNSENT']})
        self.assertEqual(result,{'answers':{}})
    def test_file_change_without_concrete_changes_is_declined(self):
        desk=app.Desk(self.root/'desk')
        try:
            result=desk.workspace_approval({'id':'synthetic'}, {'requests':[],'native_tools':[]}, {'method':'item/fileChange/requestApproval','params':{'itemId':'missing','reason':'modify file'}}, threading.Event(),lambda e:None)
            self.assertEqual(result,{'decision':'decline'})
            self.assertEqual(desk._workspace_requests,{})
        finally:desk.stop_workspace()
    def test_narrow_scopes_force_knowledge_mode(self):
        desk=app.Desk(self.root/'desk')
        try:
            for scope,current in [('advisors',None),('document',{'file_id':'note:test.md'})]:
                self.assertEqual(desk._workspace_policy({'scope':scope,'execution_mode':'assistant'},current)['execution_mode'],'knowledge')
        finally:desk.stop_workspace()

    def test_mode_and_scope_change_resets_model_history(self):
        desk=app.Desk(self.root/'desk');desk.cli='synthetic-cli'
        try:
            with patch.object(desk,'resolve_model',return_value={'id':'synthetic-model'}),patch('workspace_api.threading.Thread.start'):
                first=desk.workspace_send({'message':'synthetic','execution_mode':'assistant','scope':'personal'})
                sid=first['session']['id'];desk.store.change(first['job']['id'],'workspace-job',status='completed')
                desk.store.change(sid,'workspace-session',thread_id='BROAD_CONTEXT')
                second=desk.workspace_send({'session_id':sid,'message':'synthetic','execution_mode':'knowledge','scope':'advisors'})
                self.assertIsNone(second['session']['thread_id'])
                self.assertEqual(second['job']['request_context']['execution_mode'],'knowledge')
        finally:desk.stop_workspace()
    def test_narrow_scope_cannot_read_previous_attachment(self):
        desk=app.Desk(self.root/'desk');attachment=desk.attachments.upload('synthetic.txt',encode(b'OLD PERSONAL BODY'))
        session=desk.store.put('workspace-session',{'title':'synthetic'})
        desk.attachments.bind([attachment['id']],session['id'])
        desk.store.put('workspace-message',{'session_id':session['id'],'role':'user','text':'synthetic','attachments':[attachment]})
        job=desk.store.put('workspace-job',{'session_id':session['id'],'status':'queued','attachment_ids':[], 'request_context':{'scope':'advisors','execution_mode':'assistant'}})
        checked=[]
        def fake(client,**kw):
            with self.assertRaises(PermissionError):kw['tool_call']('attachment_read',{'attachment_id':attachment['id']})
            self.assertEqual(kw['execution_mode'],'knowledge');checked.append(True)
            return {'status':'completed','text':'synthetic'}
        try:
            with patch('codex_workspace.CodexWorkspace.run_turn',fake):
                desk.workspace_run(job,session,{'id':'synthetic-model'},'synthetic',None,'',False,threading.Event())
            self.assertEqual(checked,[True])
        finally:desk.stop_workspace()

if __name__=='__main__': unittest.main()
