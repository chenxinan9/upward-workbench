import base64
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import app


class BackendRetentionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.desk = app.Desk(Path(self.temp.name) / 'desk')

    def tearDown(self):
        self.desk.stop_workspace()
        self.temp.cleanup()

    def test_stream_checkpoints_are_durable_without_full_history_copies(self):
        session = self.desk.store.put('workspace-session', {'title': 'synthetic'})
        job = self.desk.store.put('workspace-job', {'session_id': session['id'], 'status': 'running'})
        def run(client, **kw):
            for i in range(100):
                kw['on_event']({'type': 'native_tool', 'name': 'commandExecution', 'status': 'completed',
                             'item_id': str(i), 'detail': {'output': 'x' * 3000}})
            saved = self.desk.store.get(job['id'])
            self.assertEqual(len(saved['native_tools']), 100)
            return {'thread_id': 'synthetic', 'text': 'done', 'status': 'completed'}
        with patch('codex_workspace.CodexWorkspace.run_turn', run):
            self.desk.workspace_run(job, session, {'id': 'synthetic'}, 'test', None, '', False, threading.Event())
        saved = self.desk.store.get(job['id'])
        self.assertEqual(saved['status'], 'completed')
        with self.desk.store.conn() as conn:
            count, size = conn.execute('SELECT COUNT(*),SUM(LENGTH(data)) FROM versions WHERE id=?', (job['id'],)).fetchone()
        print('stream_measurement', {'version_rows':count,'version_chars':size,'current_chars':len(json.dumps(saved))})
        self.assertLess(count, 6)
        self.assertLess(size, 4 * len(json.dumps(saved)))
        reopened = app.Desk(self.desk.store.path)
        self.assertEqual(len(reopened.store.get(job['id'])['native_tools']), 100)
        reopened.stop_workspace()

    def test_slow_attachment_parse_does_not_block_cancel_or_status(self):
        session = self.desk.store.put('workspace-session', {'title': 'synthetic'})
        job = self.desk.store.put('workspace-job', {'session_id': session['id'], 'status': 'running'})
        stop = threading.Event()
        self.desk._workspace_cancel[job['id']] = stop
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def office(*args):
            entered.set()
            release.wait(3)
            return 'synthetic', 'test'
        def upload():
            try:
                self.desk.workspace_post('/api/workspace/attachments', {'name': 'test.docx', 'data_base64': base64.b64encode(b'synthetic').decode()})
            except Exception as exc:
                errors.append(exc)
        def cancel():
            self.desk.workspace_post('/api/workspace/cancel', {'id': job['id']})
            self.desk.workspace_get('/api/workspace/status', {'session_id': session['id']})
            done.set()
        with patch('attachments._office', office):
            uploader = threading.Thread(target=upload); uploader.start()
            self.assertTrue(entered.wait(2))
            canceller = threading.Thread(target=cancel); canceller.start()
            responsive = done.wait(.3)
            release.set(); uploader.join(3); canceller.join(3)
        self.assertFalse(errors)
        self.assertTrue(responsive, '附件解析占用了停止/状态共用锁')
        self.assertTrue(stop.is_set())

    def test_remove_unsent_attachment_deletes_only_its_registered_draft(self):
        item = self.desk.workspace_post('/api/workspace/attachments', {
            'name': 'draft.txt', 'data_base64': base64.b64encode(b'synthetic').decode(), 'draft_id': 'draft-a'})['attachment']
        result = self.desk.workspace_post('/api/workspace/attachments/remove', {'id': item['id'], 'draft_id': 'draft-a'})
        self.assertTrue(result['deleted'])
        self.assertFalse((self.desk.attachments.root / item['id']).exists())

    def test_restart_preserves_latest_evidence_without_deleting_old_versions(self):
        job = self.desk.store.put('workspace-job', {'status': 'running'})
        with self.desk.store.conn() as conn:
            original = tuple(conn.execute('SELECT rev,data FROM versions WHERE id=?', (job['id'],)).fetchone())
        self.desk.store.checkpoint(job['id'], response_preview='partial', write_receipts=[{'sha': 'synthetic', 'status': 'verified'}])
        reopened = app.Desk(self.desk.store.path)
        saved = reopened.store.get(job['id'])
        self.assertEqual(saved['status'], 'interrupted')
        self.assertEqual(saved['response_preview'], 'partial')
        self.assertEqual(saved['write_receipts'][0]['sha'], 'synthetic')
        with self.desk.store.conn() as conn:
            self.assertEqual(tuple(conn.execute('SELECT rev,data FROM versions WHERE id=? ORDER BY rev LIMIT 1', (job['id'],)).fetchone()), original)
        with self.assertRaises(ValueError): reopened.store.checkpoint(job['id'], response_preview='late')
        reopened.stop_workspace()

    def test_advisor_summary_comes_only_from_actual_frontmatter(self):
        folder = Path(self.temp.name).resolve() / 'skill'; folder.mkdir()
        entry = {'name': 'synthetic', 'title': 'Synthetic', 'category': '公开测试', 'installed_path': str(folder), 'status': 'installed_verified'}
        for text, expected in [('---\nname: synthetic\ndescription: >\n  真实方法简介\n  与适用条件\n---\n# 正文', '真实方法简介 与适用条件'),
                               ('# 标题\n不可当用途简介的正文', ''), ('---\nname: synthetic\n---\n正文', '')]:
            (folder / 'SKILL.md').write_text(text)
            with patch.object(self.desk, 'advisor_entries', return_value=[entry]): self.desk.init_workspace()
            advisor = self.desk._workspace_advisors[0]
            self.assertEqual(advisor['description'], expected)
            self.assertEqual(advisor['category'], '公开测试')
            self.assertEqual(advisor['skill_id'], 'advisor-synthetic:SKILL.md')

    def test_advisor_method_excerpt_uses_only_first_two_actual_method_items(self):
        folder = Path(self.temp.name).resolve() / 'method-skill'; folder.mkdir()
        entry = {'name': 'synthetic', 'installed_path': str(folder), 'status': 'installed_verified'}
        cases = [
            ('# Title\n## 操作方法\n1. 先核对事实。\n2. 比较具体取舍。\n3. 不应展示。\n## 其它\n1. 也不展示。', '先核对事实。 比较具体取舍。'),
            ('---\nname: synthetic\n---\n## 操作方法\n1. 缺简介仍有实际方法。', '缺简介仍有实际方法。'),
            ('## 其它\n1. 不得当作方法。', ''),
            ('## 操作方法补充\n1. 不得当作方法。', ''),
            ('## 操作方法\n无编号叙述\n## 其它\n1. 不得当作方法。', ''),
            ('## 操作方法\n1. <img src=x onerror=alert(1)>\n2. 只是原文。', '<img src=x onerror=alert(1)> 只是原文。'),
            ('## 操作方法\n1. ' + '长' * 400, '长' * 320),
        ]
        for content, expected in cases:
            with self.subTest(content=content[:50]):
                (folder / 'SKILL.md').write_text(content)
                with patch.object(self.desk, 'advisor_entries', return_value=[entry]): self.desk.init_workspace()
                self.assertEqual(self.desk._workspace_advisors[0]['method_excerpt'], expected)

    def test_checkpoint_keeps_revision_conflicts_and_business_versions(self):
        job = self.desk.store.put('workspace-job', {'status': 'running'})
        current = self.desk.store.checkpoint(job['id'], response_preview='durable')
        self.assertEqual(current['rev'], job['rev'] + 1)
        with self.assertRaisesRegex(ValueError, '记录已变化'):
            self.desk.store.put('workspace-job', job, job['id'], job['rev'])
        note = self.desk.store.put('note', {'text': 'original'})
        self.desk.store.change(note['id'], 'note', text='edited')
        with self.desk.store.conn() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM versions WHERE id=?', (note['id'],)).fetchone()[0], 2)
        with self.assertRaises(ValueError): self.desk.store.checkpoint(note['id'], text='bad')


if __name__ == '__main__':
    unittest.main()
