"""Session management with synthetic records only; never remove conversation evidence."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import app


class WorkspaceSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.desk = app.Desk(self.root / 'data')

    def tearDown(self):
        self.desk.stop_workspace()
        self.tmp.cleanup()

    def session(self, **values):
        return self.desk.store.put('workspace-session', dict(title='合成会话', thread_id='durable-thread', **values))

    def change(self, action, session, **values):
        return self.desk.workspace_post('/api/workspace/session/' + action,
                                       dict(session_id=session['id'], **values))['session']

    def test_rename_archive_restore_preserve_messages_jobs_files_and_native_thread(self):
        session = self.session()
        message = self.desk.store.put('workspace-message', {'session_id':session['id'], 'text':'合成讨论'})
        job = self.desk.store.put('workspace-job', {'session_id':session['id'], 'status':'failed', 'error':'合成错误'})
        folder = self.desk.store.path / 'workspace-files'
        folder.mkdir(exist_ok=True)
        artifact = folder / 'output.md'
        artifact.write_text('合成成果')
        renamed = self.change('rename', session, title='  本周内容计划  ')
        self.assertEqual(renamed['title'], '本周内容计划')
        archived = self.change('archive', session)
        self.assertTrue(archived['archived'])
        self.assertTrue(archived['archived_at'])
        self.assertEqual(self.change('archive', session)['rev'], archived['rev'])
        self.assertEqual(self.desk.workspace_get('/api/workspace/sessions', {})['sessions'], [])
        archived_rows = self.desk.workspace_get('/api/workspace/sessions', {'archived':'1'})['sessions']
        self.assertEqual([row['id'] for row in archived_rows], [session['id']])
        status = self.desk.workspace_get('/api/workspace/status', {'session_id':session['id']})
        self.assertEqual(status['session'], {'id':session['id'], 'title':'本周内容计划',
                                           'archived':True, 'archived_at':archived['archived_at']})
        self.assertEqual(status['messages'], [message])
        self.assertEqual(status['jobs'], [job])
        restored = self.change('restore', session)
        self.assertFalse(restored['archived'])
        self.assertIsNone(restored['archived_at'])
        self.assertEqual(restored['title'], '本周内容计划')
        self.assertEqual(restored['thread_id'], 'durable-thread')
        self.assertEqual(self.change('restore', session)['rev'], restored['rev'])
        self.assertFalse(self.desk.workspace_get('/api/workspace/status', {'session_id':session['id']})['session']['archived'])
        self.assertEqual(self.desk.store.get(message['id']), message)
        self.assertEqual(self.desk.store.get(job['id']), job)
        self.assertEqual(artifact.read_text(), '合成成果')

    def test_running_sessions_cannot_be_archived_but_can_be_renamed(self):
        for status in ('queued', 'running', 'cancelling'):
            with self.subTest(status=status):
                session = self.session()
                job = self.desk.store.put('workspace-job', {'session_id':session['id'], 'status':status})
                with self.assertRaisesRegex(ValueError, '仍在处理'):
                    self.change('archive', session)
                self.assertFalse(self.desk.store.get(session['id']).get('archived'))
                self.assertEqual(self.change('rename', session, title='处理中可改名')['title'], '处理中可改名')
                self.assertEqual(self.desk.store.get(job['id'])['status'], status)
                self.desk.store.change(job['id'], 'workspace-job', status='cancelled')
                self.assertTrue(self.change('archive', session)['archived'])

    def test_archived_session_cannot_start_another_turn_until_restored(self):
        session = self.session()
        self.change('archive', session)
        self.desk.cli = 'synthetic-not-executed'
        with patch.object(self.desk, 'resolve_model', return_value={'id':'synthetic'}), \
                patch('workspace_api.threading.Thread.start') as start:
            with self.assertRaisesRegex(ValueError, '先恢复'):
                self.desk.workspace_send({'session_id':session['id'], 'message':'继续'})
            self.assertFalse(start.called)
        self.assertEqual(self.desk.store.all('workspace-job'), [])
        self.assertEqual(self.desk.store.all('workspace-message'), [])

    def test_management_validates_ids_titles_and_object_kind(self):
        session = self.session()
        for value in (None, '', True, 123, [], {}, '../outside', 'x' * 101):
            for action in ('rename', 'archive', 'restore'):
                with self.subTest(action=action, value=value), self.assertRaisesRegex(ValueError, '标识'):
                    self.desk.workspace_post('/api/workspace/session/' + action, {'session_id':value, 'title':'合法'})
        for title in (None, True, [], {}, '', '   ', 'x' * 81, '换\n行', '换\r行', '制\x00表',
                      '反\u202e转', '换\u2028行', '分\u2029段'):
            with self.subTest(title=title), self.assertRaisesRegex(ValueError, '单行'):
                self.change('rename', session, title=title)
        other = self.desk.store.put('note', {'body':'合成笔记'})
        with self.assertRaisesRegex(ValueError, '不存在'):
            self.change('archive', other)
        self.assertEqual(self.desk.store.get(session['id'])['title'], '合成会话')
        for value in ('true', 'all', True, '2'):
            with self.assertRaisesRegex(ValueError, '筛选'):
                self.desk.workspace_get('/api/workspace/sessions', {'archived':value})

    def test_filtering_precedes_cursor_page_and_renames_do_not_move_cursor(self):
        sessions = [self.session() for _ in range(6)]
        self.change('archive', sessions[4])
        with self.desk.store.conn() as connection:
            original_cursors = {row['id']:row['rowid'] for row in connection.execute('SELECT rowid,id FROM objects')}
        with patch.object(self.desk.store, 'all', side_effect=AssertionError('unbounded scan')):
            page = self.desk.workspace_get('/api/workspace/sessions', {'limit':'2'})
            self.assertEqual([s['id'] for s in page['sessions']], [sessions[5]['id'], sessions[3]['id']])
            self.assertTrue(page['pagination']['has_more'])
            self.change('rename', sessions[1], title='分页间改名')
            self.change('archive', sessions[2])
            older = self.desk.workspace_get('/api/workspace/sessions',
                {'limit':'2', 'before':page['pagination']['next_cursor']})
            self.assertEqual([s['id'] for s in older['sessions']], [sessions[1]['id'], sessions[0]['id']])
            self.assertFalse(older['pagination']['has_more'])
            archived = self.desk.workspace_get('/api/workspace/sessions', {'archived':'1', 'limit':'1'})
            self.assertEqual(archived['sessions'][0]['id'], sessions[4]['id'])
            self.change('restore', sessions[2])
            next_archive = self.desk.workspace_get('/api/workspace/sessions',
                {'archived':'1', 'limit':'1', 'before':archived['pagination']['next_cursor']})
            self.assertEqual(next_archive['sessions'], [])
        with self.desk.store.conn() as connection:
            actual_cursors = {row['id']:row['rowid'] for row in connection.execute('SELECT rowid,id FROM objects')}
        self.assertEqual(actual_cursors, original_cursors)

    def test_new_management_endpoints_keep_http_token_and_origin_guards(self):
        server = app.serve(self.root / 'http', 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        session = server.app.store.put('workspace-session', {'title':'HTTP合成'})
        try:
            def request(action, headers):
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port)
                connection.request('POST', '/api/workspace/session/' + action,
                    json.dumps({'session_id':session['id'], 'title':'HTTP改名'}),
                    dict({'Content-Type':'application/json'}, **headers))
                response = connection.getresponse()
                status, body = response.status, response.read()
                connection.close()
                return status, json.loads(body)
            for action in ('rename', 'archive', 'restore'):
                self.assertEqual(request(action, {})[0], 403)
                self.assertEqual(request(action, {'X-Desk-Token':'wrong'})[0], 403)
                self.assertEqual(request(action, {'X-Desk-Token':server.app.token, 'Origin':'https://evil.example'})[0], 403)
            self.assertEqual(server.app.store.get(session['id'])['title'], 'HTTP合成')
            self.assertEqual(request('rename', {'X-Desk-Token':server.app.token})[1]['session']['title'], 'HTTP改名')
            self.assertTrue(request('archive', {'X-Desk-Token':server.app.token})[1]['session']['archived'])
            self.assertFalse(request('restore', {'X-Desk-Token':server.app.token})[1]['session']['archived'])
        finally:
            server.app.stop_workspace()
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
