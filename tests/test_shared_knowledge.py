"""Fixed public samples exercise the real loopback client and desk send boundary."""
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import app
from shared_knowledge import SharedKnowledge, SharedError, SHARED_ROOT


class SharedIntegrationTest(unittest.TestCase):
    def test_shared_preview_tool_scope_revocation_and_ram_only(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory).resolve(); scope={'instance_id':'sample_instance_123456','generation':1,'scope_fingerprint':'a'*64}
            ident='SHR-'+'1'*32; text='公开样本首行\n公开样本第二行\n'
            item={'kind':'shared','share_id':ident,'version':1,'selected_sha256':hashlib.sha256(text.encode()).hexdigest(),
                  'title':'公开样本','source_kind':'text','line_reference':'提取正文行号',
                  'locator':{'kind':'extracted_text_lines','line_start':7,'line_count':3},'text':text,'freshness':'verified'}
            seen=[]
            class Handler(BaseHTTPRequestHandler):
                def log_message(self,*args):pass
                def reply(self,value,status=200):
                    body=json.dumps(value,ensure_ascii=False).encode();self.send_response(status);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
                def do_GET(self):
                    self.reply({'ok':True,'data':dict(scope,shares=[{'share_id':ident,'version':1,'state':'active'}])})
                def do_POST(self):
                    data=json.loads(self.rfile.read(int(self.headers['Content-Length'])));seen.append((self.path,data,dict(self.headers)))
                    if data.get('expected_generation')!=scope['generation']:
                        return self.reply({'ok':False,'error':{'code':'share_conflict','message':'来源范围已变化','retryable':False}},409)
                    if self.path.endswith('/search'):
                        value=dict(item,excerpt='公开样本首行',truncated=False);value.pop('text')
                        return self.reply({'ok':True,'data':dict(scope,mode='selected_literal',scope='explicit_shared_only',items=[value],result_count=1,returned_count=1,truncated=False)})
                    self.reply({'ok':True,'data':dict(scope,items=[dict(item,path='must-never-pass',document_id='private-id')])})
            server=HTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                connection=base/'connection.json';connection.write_text(json.dumps({'version':1,'protocol_version':1,'application':'local-knowledge-workbench','target':'growth','online':True,'port':server.server_port,'instance_id':scope['instance_id'],'token':'sample_'+'t'*40}));connection.chmod(0o600)
                client=SharedKnowledge(connection);desk=app.Desk(base/'data');desk.shared_knowledge=client;desk.cli='fake'
                shares=[{'share_id':ident,'expected_version':1}]
                payload={'message':'请比较公开样本','model':'sample','scope':'advisors','execution_mode':'knowledge','attachment_ids':[]}
                with patch.object(desk,'resolve_model',return_value={'id':'sample','selection_fingerprint':'synthetic-selection'}):
                    preview=desk.workspace_post('/api/workspace/shared/preview',dict(payload,shares=shares,shared_scope=scope))
                    self.assertNotIn('error',preview,preview);self.assertEqual(preview['items'][0]['text'],text);self.assertNotIn('path',preview['items'][0]);self.assertNotIn('document_id',preview['items'][0])
                    with self.assertRaises(SharedError):client.check(preview['preview_id'],preview['preview_sha256'],{'changed':'question'},True)
                    data=dict(payload,shared_preview_id=preview['preview_id'],shared_preview_sha256=preview['preview_sha256'],shared_confirmed=True)
                    with patch('workspace_api.threading.Thread.start',lambda self:None):sent=desk.workspace_send(data)
                    def run_turn(_runner,**kw):
                        kw['before_send']()
                        result=kw['tool_call']('knowledge_search',{'query':'公开','root_id':SHARED_ROOT})
                        self.assertEqual(result['results'][0]['share_id'],ident)
                        result=kw['tool_call']('knowledge_read',{'document_id':ident,'offset':1,'limit':2})
                        self.assertEqual(result['text'],'公开样本第二行\n');self.assertEqual(result['line'],8)
                        with self.assertRaises(PermissionError):kw['tool_call']('knowledge_read',{'document_id':'SHR-'+'2'*32})
                        self.assertNotIn(text,json.dumps(kw['context'],ensure_ascii=False))
                        return {'text':'公开回答','status':'completed','thread_id':'public-thread'}
                    with patch('codex_workspace.CodexWorkspace.run_turn',run_turn):desk.workspace_run(sent['job'],sent['session'],{'id':'sample','selection_fingerprint':'synthetic-selection'},payload['message'],None,'',False,threading.Event())
                    self.assertFalse(client.previews)
                    self.assertNotIn(text,json.dumps(desk.store.all('workspace-job'),ensure_ascii=False))
                    self.assertEqual(desk.store.get(sent['job']['id'])['references'][0]['kind'],'shared')
                    # A changed source before the final protocol boundary keeps local question/history but drops remote continuation.
                    preview=desk.workspace_post('/api/workspace/shared/preview',dict(payload,session_id=sent['session']['id'],shares=shares,shared_scope=scope))
                    data=dict(payload,session_id=sent['session']['id'],shared_preview_id=preview['preview_id'],shared_preview_sha256=preview['preview_sha256'],shared_confirmed=True)
                    with patch('workspace_api.threading.Thread.start',lambda self:None):second=desk.workspace_send(data)
                    def refuse(_runner,**kw):
                        scope['generation']=2
                        kw['before_send']()
                        self.fail('must not reach outbound turn')
                    with patch('codex_workspace.CodexWorkspace.run_turn',refuse):desk.workspace_run(second['job'],second['session'],{'id':'sample','selection_fingerprint':'synthetic-selection'},payload['message'],None,'',False,threading.Event())
                    failed=desk.store.get(second['job']['id']);self.assertTrue(failed['shared_not_sent']);self.assertIsNone(desk.store.get(second['session']['id'])['thread_id'])
                    self.assertTrue(any(v['text']==payload['message'] for v in desk.store.all('workspace-message')))
                    # TTL and connection file protections fail closed without network fallback.
                    scope['generation']=1;expired=client.preview({'message':'sample'},shares,scope);client.previews[expired['preview_id']]['expires_at']=time.time()-1
                    with self.assertRaises(SharedError):client.check(expired['preview_id'])
                    connection.chmod(0o644)
                    with self.assertRaises(SharedError):client.summary()
                self.assertTrue(all(v[2].get('X-Knowledge-Share-Token')=='sample_'+'t'*40 for v in seen))
                desk.stop_workspace()
            finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
