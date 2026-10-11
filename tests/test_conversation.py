"""Real capture integration with synthetic model envelopes; no live model in CI."""
import json,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from desk_fixture import Desk
from conversation import parse_reply

class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.d=Desk(Path(self.tmp.name)/'data')
    def tearDown(self):self.tmp.cleanup()
    def job(self,message):
        s=self.d.store.put('session',{'title':'合成讨论'})
        return self.d.store.put('job',{'session_id':s['id'],'message':message,'status':'completed','organize':True})
    def test_plain_answer_and_real_goal_receipt(self):
        j=self.job('我的目标是读完一本书，每周读一章。')
        payload={'answer':'先保留这个目标，日期待你安排。','items':[{'key':'g1','kind':'goal','source_quote':j['message'],'title':'读完一本书','explicit':True,'adopted':True,'steps':[{'text':'每周读一章','done':False}]}]}
        result=self.d.completed_reply(j,json.dumps(payload,ensure_ascii=False))
        self.assertEqual(result['response'],payload['answer']);self.assertEqual(result['capture_status'],'saved')
        g=self.d.store.all('goal')[0];self.assertEqual(g['status'],'active');self.assertFalse(g['due']);self.assertFalse(g['steps'][0]['due']);self.assertEqual(g['steps'][0]['status'],'draft')
        self.assertEqual(self.d.read(g['origin_id'])['text'],j['message'])
        self.d.organize_reply(j['id']);self.assertEqual(len(self.d.store.all('goal')),1)
    def test_suggestion_is_draft(self):
        j=self.job('也许可以试试每周读书，还没有决定。')
        p={'answer':'可以先评估时间。','items':[{'key':'maybe','kind':'goal','source_quote':j['message'],'title':'每周读书','explicit':False,'adopted':False}]}
        self.d.completed_reply(j,json.dumps(p));self.assertEqual(self.d.store.all('goal')[0]['status'],'draft')
    def test_user_opt_out_blocks_model_items(self):
        j=self.job('我的目标是读书，但不要保存，只讨论。')
        p={'answer':'只讨论。','items':[{'key':'x','kind':'goal','source_quote':j['message'],'title':'读书','explicit':True,'adopted':True}]}
        r=self.d.completed_reply(j,json.dumps(p));self.assertEqual(r['capture_status'],'empty');self.assertEqual(self.d.store.all('goal'),[])
    def test_hallucinated_source_preserves_answer_not_goal(self):
        j=self.job('今天聊学习。')
        p={'answer':'先看学习中的困难。','items':[{'key':'x','kind':'goal','source_quote':'我承诺每天读书','title':'读书','explicit':True,'adopted':True}]}
        r=self.d.completed_reply(j,json.dumps(p));self.assertEqual(r['status'],'completed');self.assertEqual(r['capture_status'],'failed');self.assertEqual(self.d.store.all('goal'),[])
    def test_malformed_and_unexpected_write_fields(self):
        j=self.job('我的目标是读书。');r=self.d.completed_reply(j,'只有正文，没有整理结构')
        self.assertEqual(r['status'],'completed');self.assertEqual(r['capture_status'],'failed');self.assertEqual(r['response'],'只有正文，没有整理结构')
        with self.assertRaises(ValueError):self.d.organize_reply(j['id'])
        j=self.job('我的目标是读书。')
        p={'answer':'保留原话。','items':[{'key':'x','kind':'goal','source_quote':j['message'],'title':'读书','id':'a'*32}]}
        r=self.d.completed_reply(j,json.dumps(p));self.assertEqual(r['capture_status'],'failed');self.assertFalse(self.d.store.all('goal'))
    def test_generated_steps_and_title_do_not_become_commitments(self):
        j=self.job('我决定学摄影。')
        p={'answer':'可以考虑练习。','items':[{'key':'g','kind':'goal','source_quote':j['message'],'title':'学摄影','explicit':True,'adopted':True,'steps':[{'text':'每天练习三小时','done':False}]}]}
        r=self.d.completed_reply(j,json.dumps(p));g=self.d.store.all('goal')[0]
        self.assertEqual(g['status'],'active');self.assertEqual(g['steps'][0]['status'],'draft')
        j=self.job('我决定学摄影。')
        p={'answer':'建议先观察。','items':[{'key':'x','kind':'goal','source_quote':j['message'],'title':'购买专业相机','explicit':True,'adopted':True,'due':'2026-12-31'}]}
        self.d.completed_reply(j,json.dumps(p));g=self.d.store.all('goal')[0]
        self.assertEqual(g['status'],'draft');self.assertEqual(g['due'],'')
    def test_opt_out_variations_and_uncertain_intention(self):
        for phrase in ['本次只讨论，不保存','只聊聊','不记录','先别存']:
            j=self.job('我的目标是读书，但'+phrase+'。')
            p={'answer':'了解。','items':[{'key':'g','kind':'goal','source_quote':j['message'],'title':'读书','explicit':True,'adopted':True}]}
            r=self.d.completed_reply(j,json.dumps(p));self.assertEqual(r['capture_status'],'empty')
        self.assertFalse(self.d.store.all('goal'))
        j=self.job('你建议我每周读书，我还没决定。')
        p={'answer':'先考虑。','items':[{'key':'g','kind':'goal','source_quote':j['message'],'title':'每周读书','explicit':True,'adopted':True}]}
        self.d.completed_reply(j,json.dumps(p));self.assertEqual(self.d.store.all('goal')[0]['status'],'draft')
    def test_preview_opt_out(self):
        p=self.d.preview({'message':'只讨论','organize':False});self.assertFalse(p['organize']);self.assertNotIn('items 最多8条',p['prompt'])
        p=self.d.preview({'message':'我的目标是读书'});self.assertTrue(p['organize']);self.assertIn('items 最多8条',p['prompt'])

if __name__=='__main__':unittest.main()
