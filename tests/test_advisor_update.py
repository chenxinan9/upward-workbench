"""New advisor methods must be discoverable and leave actual read/selection receipts."""
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from desk_fixture import Desk
from tools.check_public import check


class AdvisorUpdateTests(unittest.TestCase):
    def test_new_methods_use_the_existing_scoped_read_and_selection_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            desk = Desk(Path(tmp) / 'data')
            entries = {a['name']: a for a in desk.workspace_get('/api/workspace/options', {})['advisors']}
            for name in ('think-decision-option-review', 'think-premortem', 'stage-reflection', 'life-design-compass', 'fact-check'):
                with self.subTest(name=name):
                    entry = entries[name]
                    self.assertTrue(entry['description'])
                    self.assertTrue(entry['method_excerpt'])
                    session = desk.store.put('workspace-session', {'title': '公开合成验收'})
                    job = desk.store.put('workspace-job', {'session_id': session['id'], 'status': 'queued',
                        'request_context': {'scope': 'advisors', 'purpose': 'general', 'days': 7, 'advisor_names': [name]}})

                    def run(_client, **kw):
                        tool = kw['tool_call']
                        with self.assertRaises(ValueError):
                            tool('select_advisor', {'name': name, 'reason': '先读取完整技能再形成选用回执'})
                        result = tool('knowledge_read', {'document_id': entry['skill_id'], 'limit': 250})
                        self.assertEqual(result['text'].splitlines(), desk.vault.read(entry['skill_id'])['text'].splitlines())
                        chosen = tool('select_advisor', {'name': name, 'reason': '合成问题需要这个方法的可检查输出'})
                        self.assertEqual(chosen['skill_sha'], result['sha256'])
                        return {'text': '合成路径已核验', 'status': 'completed', 'actual_model': 'fake'}

                    with patch('codex_workspace.CodexWorkspace.run_turn', run):
                        desk.workspace_run(job, session, {'id': 'fake'}, '只分析，不保存', None, '', False, threading.Event())
                    saved = desk.store.get(job['id'], 'workspace-job')
                    self.assertEqual(saved['status'], 'completed', saved.get('error'))
                    self.assertEqual([a['name'] for a in saved['advisor_selection']], [name])
                    self.assertEqual(saved['references'][0]['id'], entry['skill_id'])
                    self.assertEqual(saved.get('write_receipts', []), [])

    def test_scene_binary_exception_is_narrow_and_requires_png_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scene = root / 'desktop/assets/study-scene.png'
            scene.parent.mkdir(parents=True)
            scene.write_bytes((ROOT / 'desktop/assets/study-scene.png').read_bytes())
            self.assertTrue(check(root, ['desktop/assets/study-scene.png'])['passed'])
            scene.write_bytes(b'not a png')
            self.assertFalse(check(root, ['desktop/assets/study-scene.png'])['passed'])
            other = root / 'unreviewed.png'; other.write_bytes(b'\x89PNG\r\n\x1a\n\xff')
            self.assertFalse(check(root, ['unreviewed.png'])['passed'])
