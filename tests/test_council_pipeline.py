"""Pipeline regressions: real method bytes reach answer, failures never fake success."""
import json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import digest
from desk_fixture import Desk

class PipelineTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.d=Desk(self.root/'data');self.d.cli='synthetic-cli'
  self.skill=self.root/'skill';self.skill.mkdir();(self.skill/'SKILL.md').write_text('---\nname: example\ndescription: 先用证据核对，设计最小实验\n---\n# 合成方法\n唯一方法哨兵：先列反例，再做一次对照验证。')
  catalog=self.root/'catalog.json';catalog.write_text(json.dumps({'entries':[{'name':'example','title':'合成顾问','category':'实验','installed_path':str(self.skill),'status':'installed_verified','url':'https://example.com'}]}));self.d.settings['catalog']=str(catalog)
 def tearDown(self):self.tmp.cleanup()
 def job(self,**extra):
  p=self.d.preview(dict(message='我有一个方案，不知道该不该投入，只讨论，不记录',organize=True,**extra));s=self.d.store.put('session',{'title':'合成'});j=self.d.store.put('job',{'status':'queued','session_id':s['id'],'preview_id':p['id'],'organize':True,'message':p['message'],'prompt':p['prompt']});return p,j
 def route(self):return json.dumps({'selection':[{'name':'example','reason':'用最小对照实验验证投入假设'}]})
 def test_real_skill_in_second_call_and_immutable_evidence(self):
  p,j=self.job();calls=[]
  def model(job,cmd,folder,prompt,stage,timeout):
   calls.append((stage,prompt));return self.route() if stage=='routing' else json.dumps({'answer':'合成顾问的方法提示先列反例，再做一次对照验证。','items':[]})
  with patch.object(self.d,'model_text',side_effect=model):self.d.run(j)
  saved=self.d.store.get(j['id'],'job');self.assertEqual(saved['status'],'completed');self.assertEqual([x[0] for x in calls],['routing','answer']);self.assertNotIn('唯一方法哨兵',calls[0][1]);self.assertIn('唯一方法哨兵',calls[1][1]);self.assertEqual(saved['context_hash'],digest(calls[1][1]));self.assertEqual(saved['capture_status'],'empty')
  (self.skill/'SKILL.md').write_text('后来的方法');c=self.d.messages(j['session_id'])[0]['reply_context'];self.assertIn('唯一方法哨兵',c['contexts'][-1]['text']);self.assertNotIn('后来的方法',c['contexts'][-1]['text']);self.assertEqual(c['advisor_selection'][0]['name'],'example');self.assertNotIn('answer_context',self.d.public_jobs()[0])
 def test_router_invalid_does_not_answer_or_capture(self):
  p,j=self.job()
  with patch.object(self.d,'model_text',return_value='{"selection":[{"name":"unknown","reason":"x"}]}') as call:self.d.run(j)
  self.assertEqual(call.call_count,1);self.assertEqual(self.d.store.get(j['id'])['status'],'failed');self.assertEqual(self.d.store.all('message'),[]);self.assertEqual(self.d.store.all('capture'),[])
 def test_file_changed_after_preview_refuses(self):
  p,j=self.job();(self.skill/'SKILL.md').write_text('新版本')
  with patch.object(self.d,'model_text',return_value=self.route()) as call:self.d.run(j)
  self.assertEqual(call.call_count,1);self.assertEqual(self.d.store.get(j['id'])['status'],'failed');self.assertEqual(self.d.store.all('message'),[])
 def test_cancel_after_router_prevents_second_call(self):
  p,j=self.job()
  def model(*a):self.d.store.change(j['id'],'job',status='cancelled');return self.route()
  with patch.object(self.d,'model_text',side_effect=model) as call:self.d.run(j)
  self.assertEqual(call.call_count,1);self.assertEqual(self.d.store.get(j['id'])['status'],'cancelled');self.assertEqual(self.d.store.all('message'),[])
 def test_answer_failure_never_completed_or_captured(self):
  p,j=self.job()
  with patch.object(self.d,'model_text',side_effect=[self.route(),ValueError('合成超时')]):self.d.run(j)
  self.assertEqual(self.d.store.get(j['id'])['status'],'failed');self.assertEqual(self.d.store.all('message'),[]);self.assertEqual(self.d.store.all('capture'),[])
 def test_unselected_private_note_is_not_sent(self):
  self.d.note({'body':'秘密合成哨兵'});p,j=self.job();calls=[]
  def model(job,cmd,folder,prompt,stage,timeout):calls.append(prompt);return self.route() if stage=='routing' else '{"answer":"检查反例。","items":[]}'
  with patch.object(self.d,'model_text',side_effect=model):self.d.run(j)
  self.assertTrue(calls);self.assertTrue(all('秘密合成哨兵' not in p for p in calls))
if __name__=='__main__':unittest.main()
