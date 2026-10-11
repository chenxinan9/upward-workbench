import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from desk_fixture import Desk


class ModelCheckTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.d=Desk(Path(self.tmp.name)/'data')
 def tearDown(self):self.tmp.cleanup()
 def wait_check(self):
  deadline=time.monotonic()+2
  while self.d.model_check_status()['status']=='running' and time.monotonic()<deadline:time.sleep(.01)
  self.assertEqual(self.d.model_check_status()['status'],'completed')
 def test_check_is_single_flight_and_never_sends_drafts(self):
  entered=threading.Event();release=threading.Event();calls=[]
  def probe(ident):calls.append(ident);entered.set();release.wait(1);return ident
  with patch.object(self.d,'_probe_model',side_effect=probe):
   first=self.d.check_models();self.assertTrue(entered.wait(1));second=self.d.check_models()
   self.assertEqual(first['started_at'],second['started_at']);release.set();self.wait_check()
   self.d.check_models()
  self.assertEqual(calls,['gpt-fixture']);self.assertEqual(self.d.model_check_status()['results'][0]['status'],'verified')
  self.assertEqual(self.d.store.all('job'),[]);self.assertEqual(self.d.store.all('session'),[])
 def test_failed_explicit_recheck_removes_previously_verified_option(self):
  with patch.object(self.d,'_probe_model',side_effect=ValueError('synthetic failure')):
   self.d.check_models();self.wait_check()
  self.assertEqual(self.d.model_catalog()['models'],[])
  with self.assertRaises(ValueError):self.d.preview({'message':'keep my draft'})
 def test_default_missing_verification_never_bypasses_allowlist(self):
  self.d.invalidate_model('gpt-fixture')
  for request in ({'message':'x'},{'message':'x','model':'gpt-fixture'},{'message':'x','model':'alien'}):
   with self.assertRaises(ValueError):self.d.preview(request)
  self.assertEqual(self.d.store.all('preview'),[])
 def test_preview_model_is_bound_to_call_and_receipt_io_cannot_undo_reply(self):
  p=self.d.preview({'message':'只讨论','organize':False,'model':'gpt-fixture'})
  self.d.cli='fixture-cli'
  with patch('threading.Thread.start'):
   j=self.d.send({'preview_id':p['id']})
  calls=[];name=p['advisor_council']['catalog'][0]['name']
  def model(job,cmd,folder,prompt,stage,timeout):
   calls.append(cmd)
   if stage=='routing':return json.dumps({'selection':[{'name':name,'reason':'合成匹配原因'}]})
   self.d.store.change(j['id'],'job',actual_model='gpt-fixture')
   return '合成正常回答'
  with patch.object(self.d,'model_text',side_effect=model),patch.object(self.d,'verify_model',side_effect=OSError('receipt unavailable')):
   self.d.run(j)
  saved=self.d.store.get(j['id']);self.assertEqual(saved['status'],'completed');self.assertIn('receipt_warning',saved)
  self.assertEqual(len(calls),2);self.assertTrue(all('model="gpt-fixture"' in cmd for cmd in calls))
  self.assertEqual(self.d.messages(j['session_id'])[-1]['text'],'合成正常回答')
  repeated=self.d.send({'preview_id':p['id']});self.assertNotIn('routing_response',repeated);self.assertNotIn('answer_context',repeated)


if __name__=='__main__':unittest.main()
