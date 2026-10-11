"""Independent daily-experience contract tests; synthetic records, no live model."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desk_fixture import Desk


class ExperienceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Desk(Path(self.tmp.name) / 'data')
        self.a = self.d.store.put('session', {'title': '合成摄影讨论'})
        self.b = self.d.store.put('session', {'title': '合成英语讨论'})
    def tearDown(self):
        self.tmp.cleanup()
    def msg(self, sid, text):
        return self.d.store.put('message', {'session_id': sid, 'role': 'user', 'text': text, 'status': 'completed'})
    def test_chat_status_session_isolation_and_incremental_cursor(self):
        self.msg(self.a['id'], 'A 的摄影问题')
        self.msg(self.b['id'], 'B 的英语问题')
        first = self.d.chat_status(self.a['id'])
        self.assertEqual([m['text'] for m in first['messages']], ['A 的摄影问题'])
        self.assertFalse(first['unchanged'])
        same = self.d.chat_status(self.a['id'], first['cursor'])
        self.assertTrue(same['unchanged']); self.assertNotIn('messages', same)
        self.msg(self.b['id'], 'B 的另一条问题')
        self.assertTrue(self.d.chat_status(self.a['id'], first['cursor'])['unchanged'])
        self.msg(self.a['id'], 'A 的第二条问题')
        latest = self.d.chat_status(self.a['id'], first['cursor'])
        self.assertFalse(latest['unchanged'])
        self.assertEqual([m['text'] for m in latest['messages']], ['A 的摄影问题', 'A 的第二条问题'])
    def test_job_stage_updates_without_replacing_unchanged_messages(self):
        self.msg(self.a['id'], '讨论摄影')
        job = self.d.store.put('job', {'session_id': self.a['id'], 'status': 'running', 'council_phase': 'selecting', 'prompt':'private prompt', 'routing_prompt':'private routing', 'raw_response':'private raw', 'capture_items':[]})
        self.d.store.put('job', {'session_id': self.b['id'], 'status': 'running', 'council_phase': 'answering'})
        start = self.d.chat_status(self.a['id'])
        self.assertEqual(start['job']['id'], job['id'])
        self.d.store.change(job['id'], 'job', council_phase='answering')
        result = self.d.chat_status(self.a['id'], start['cursor'])
        self.assertTrue(result['unchanged']); self.assertNotIn('messages', result)
        self.assertEqual(result['job']['council_phase'], 'answering')
        self.assertFalse({'prompt','routing_prompt','raw_response','capture_items'} & set(result['job']))
        self.d.store.change(job['id'], 'job', status='failed', error='合成服务中断')
        result = self.d.chat_status(self.a['id'], start['cursor'])
        self.assertEqual(result['job']['status'], 'failed'); self.assertEqual(result['job']['error'], '合成服务中断')
    def test_preview_selected_material_and_history_are_frozen(self):
        self.msg(self.a['id'], '只属于摄影的历史')
        self.msg(self.b['id'], '不应发送的英语历史')
        material={'id':'source:synthetic','title':'合成摄影材料','kind':'测试原文','text':'摄影原文 v1'}
        with patch.object(self.d, 'read', return_value=material):
            p=self.d.preview({'session_id':self.a['id'],'message':'分析这份摄影材料','refs':['source:synthetic']})
        material['text']='后来改变的原文'
        self.msg(self.a['id'], '预览后的新消息')
        stored=self.d.store.get(p['id'],'preview')
        self.assertEqual(stored['contexts'][0]['text'],'摄影原文 v1')
        self.assertEqual([m['text'] for m in stored['history']],['只属于摄影的历史'])
        self.assertEqual(stored['refs'],['source:synthetic'])
        b=self.d.preview({'session_id':self.b['id'],'message':'英语问题','refs':[]})
        self.assertEqual(b['contexts'],[])
        self.assertEqual([m['text'] for m in b['history']],['不应发送的英语历史'])
    def test_active_goal_context_contains_real_ids_and_versions_only_when_selected(self):
        g=self.d.goal({'title':'合成摄影集','status':'active','steps':[{'text':'选三张照片','done':False,'due':'2026-10-09'}]})
        self.d.goal({'title':'还没决定的建议','status':'draft','steps':[]})
        p=self.d.preview({'message':'看看当前目标','include_goals':True})
        goals=json.loads(next(c['text'] for c in p['contexts'] if c['id']=='active-goals'))
        self.assertEqual(len(goals),1); self.assertEqual(goals[0]['id'],g['id']); self.assertEqual(goals[0]['rev'],g['rev'])
        self.assertEqual(goals[0]['steps'][0]['id'],g['steps'][0]['id'])
        p=self.d.preview({'message':'不要附目标','include_goals':False})
        self.assertFalse(any(c['id']=='active-goals' for c in p['contexts']))

if __name__ == '__main__':
    unittest.main()
