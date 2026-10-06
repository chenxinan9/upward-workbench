"""Regression cases for the source-backed growth paths; all content is synthetic."""
import hashlib,json,sys,tempfile,unittest
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import Desk
class GrowthTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.d=Desk(self.root/'data')
  self.private=self.root/'private';self.private.mkdir();(self.private/'fact.md').write_text('# 本人原文\n2026-01-01：这是合成资料。')
  self.plan=self.root/'plan.json';self.plan.write_text(json.dumps({'source_register':[{'id':'fact','path':'fact.md','title':'合成资料'}],'domains':[{'id':'career','title':'职业','keywords':['面试'],'advisor_slugs':['sample'],'actions':[]}]}));self.d.settings.update(growth_plan=str(self.plan),private_root=str(self.private))
  skill=self.root/'sample';(skill/'references').mkdir(parents=True);(skill/'SKILL.md').write_text('# 合成顾问\n先检查证据。');(skill/'references'/'practice.md').write_text('# 面试练习\n准备一段事例。')
  catalog=self.root/'catalog.json';catalog.write_text(json.dumps({'entries':[{'name':'sample','title':'合成面试顾问','category':'职业','status':'installed_verified','installed_path':str(skill),'url':'https://example.org','license':'MIT'}]}));self.d.settings['catalog']=str(catalog);self.skill=skill
 def tearDown(self):self.tmp.cleanup()
 def test_plan_is_not_automatically_a_goal(self):
  self.assertEqual(len(self.d.plan()['domains']),1);self.assertEqual(self.d.store.all('goal'),[])
 def test_adoption_is_idempotent_and_keeps_evidence(self):
  d={'domain_id':'career','title':'测试目标','why':'核对合成资料','steps':[{'text':'练习','criterion':'留一条结果'}]}
  a=self.d.adopt(d);b=self.d.adopt(d);self.assertEqual(a['id'],b['id']);self.assertEqual(a['origin_id'],'domain:career');self.assertEqual(a['status'],'active');self.assertEqual(a['steps'][0]['criterion'],'留一条结果')
 def test_reference_read_and_traversal(self):
  listing=self.d.advisor_files('sample');self.assertEqual(len(listing['files']),2)
  self.assertIn('事例',self.d.read('advisor-file:sample:references/practice.md')['text'])
  with self.assertRaises(ValueError):self.d.read('advisor-file:sample:../plan.json')
  (self.skill/'references'/'escape.md').symlink_to(self.private/'fact.md')
  with self.assertRaises(ValueError):self.d.read('advisor-file:sample:references/escape.md')
 def test_source_allowlist(self):
  self.assertIn('合成资料',self.d.read('source:fact')['text'])
  with self.assertRaises(ValueError):self.d.read('source:../plan')
  (self.private/'fact.md').unlink();(self.private/'fact.md').symlink_to(self.plan)
  with self.assertRaises(ValueError):self.d.read('source:fact')
 def test_learning_keeps_history_and_real_practice(self):
  f='advisor-file:sample:references/practice.md';a=self.d.learn({'advisor':'sample','file_id':f,'status':'reading'})
  b=self.d.learn({'advisor':'sample','file_id':f,'status':'read','insight':'学到一个方法'})
  self.assertEqual(a['id'],b['id']);self.assertEqual(len(self.d.store.all('learning')),1)
  with self.assertRaises(ValueError):self.d.learn({'advisor':'sample','file_id':f,'status':'practiced'})
  p=self.d.learn({'advisor':'sample','file_id':f,'status':'practiced','insight':'实际写了一次，仍需改进'})
  self.assertEqual(self.d.learn({'advisor':'sample','file_id':f,'status':'reading'})['status'],'practiced')
 def test_visible_retrieval(self):
  result=self.d.context_candidates({'question':'面试怎么准备'})
  self.assertIn('plan:career',result['suggested_ids']);self.assertIn('advisor:sample',result['suggested_ids']);self.assertEqual(self.d.store.all('job'),[])
  p=self.d.preview({'message':'面试怎么准备','refs':result['suggested_ids']});self.assertTrue(any(c['id']=='plan:career' for c in p['contexts']))
 def test_record_to_action_and_goal(self):
  n=self.d.note({'body':'准备事例'});g=self.d.goal_from_record({'source_id':'note:'+n['id'],'title':'来自想法的目标','steps':[{'text':'准备'}]});self.assertEqual(g['origin_id'],'note:'+n['id'])
  n2=self.d.note({'body':'做一次练习'});a={'source_id':'note:'+n2['id'],'goal_id':g['id'],'text':'练习并留结果','criterion':'一条记录'}
  self.d.add_record_action(a);self.assertEqual(len(self.d.add_record_action(a)['steps']),2)
  self.assertEqual(self.d.store.get(n2['id'],'note')['body'],'做一次练习')
 def test_commitments_read_without_rewriting(self):
  p=self.root/'commitments.json';raw=json.dumps({'updated_at':'2026-01-01','periods':{'week':{'next_actions':[{'commitment':'未承诺'}],'goal_statuses':[]}}});p.write_text(raw)
  cfg=self.root/'cfg.json';cfg.write_text(json.dumps({'canonical_commitments':str(p)}));self.d.settings['advisor_config']=str(cfg)
  self.assertEqual(self.d.commitments()['next_actions'][0]['commitment'],'未承诺');self.assertEqual(p.read_text(),raw)
 def test_audit_regressions_source_snapshot_and_second_action(self):
  plan=json.loads(self.plan.read_text());plan['source_register'][0]['sha256']=hashlib.sha256((self.private/'fact.md').read_bytes()).hexdigest();plan['domains'][0]['source_ids']=['fact'];plan['domains'][0]['actions']=[{'id':'candidate','title':'原行动','canonical_action_ref':'fixture-A1'}];self.plan.write_text(json.dumps(plan))
  g=self.d.adopt({'domain_id':'career','title':'采用测试','steps':[{'text':'第一步','plan_action_id':'candidate'}]})
  self.assertEqual(g['steps'][0]['canonical_action_ref'],'fixture-A1');self.assertEqual(g['source_snapshot']['sources'][0]['id'],'fact')
  snapshot=g['source_snapshot'];g['source_snapshot']={'replacement':True};g=self.d.goal(g);self.assertEqual(g['source_snapshot'],snapshot)
  (self.private/'fact.md').write_text('新原文');r=self.d.source('fact');self.assertTrue(r['source_changed']);self.assertIn('已变化',r['text'])
  n=self.d.note({'body':'两个不同动作'});x={'source_id':'note:'+n['id'],'goal_id':g['id'],'text':'动作甲'};self.d.add_record_action(x);x['text']='动作乙';g=self.d.add_record_action(x);self.assertEqual(len(g['steps']),3);self.assertEqual(len(self.d.add_record_action(x)['steps']),3)
 def test_audit_regressions_learning_and_search_failure(self):
  g=self.d.goal({'title':'学习目标'});a={'advisor':'sample','status':'read','goal_id':g['id'],'position':70,'insight':'一个理解'};self.d.learn(a)
  r=self.d.learn({'advisor':'sample','status':'read','goal_id':''});self.assertEqual(r['goal_id'],'');self.assertEqual(r['position'],70)
  r=self.d.learn({'advisor':'sample','status':'reading','position':80});self.assertEqual(r['status'],'read');self.assertEqual(r['position'],80)
  with patch.object(self.d,'search',side_effect=RuntimeError('fixture index failed')):
   self.assertTrue(self.d.context_candidates({'question':'面试准备'})['warnings'])
 def test_historical_reply_keeps_sent_source_snapshot(self):
  session=self.d.store.put('session',{'title':'合成会话'});p=self.d.preview({'session_id':session['id'],'message':'合成问题','refs':['source:fact']});j=self.d.store.put('job',{'session_id':session['id'],'preview_id':p['id']});self.d.store.put('message',{'session_id':session['id'],'job_id':j['id'],'role':'assistant','status':'completed','text':'合成回答'})
  (self.private/'fact.md').write_text('后来的版本');c=self.d.messages(session['id'])[0]['reply_context'];self.assertIn('合成资料',c['contexts'][0]['text']);self.assertNotIn('后来的版本',c['contexts'][0]['text']);self.assertEqual(c['context_hash'],p['hash'])
 def test_public_bundle_on_fresh_device(self):
  self.d.settings={};self.assertEqual(len(self.d.catalog()),51);self.assertTrue(all(x['installed'] for x in self.d.catalog()))
  for a in self.d.catalog():self.assertTrue(self.d.advisor(a['name'])['text'])
  self.assertEqual(len(self.d.method_registry()),33)
if __name__=='__main__':unittest.main()
