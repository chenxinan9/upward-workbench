"""Capture regression tests: synthetic conversations, isolated stores, no live server."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from desk_fixture import Desk
from capture import CaptureMixin

# Remains usable both before and after app.py wires in the mixin.
CaptureDesk=Desk if issubclass(Desk,CaptureMixin) else type('CaptureDesk',(CaptureMixin,Desk),{})

class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.d=CaptureDesk(Path(self.tmp.name)/'data')
        self.payload={'request_id':'fixture-1','source':'desktop_codex','source_id':'synthetic-thread',
                      'source_text':'我决定学习摄影。周五先拍三张。试试新构图。',
                      'items':[{'key':'g1','kind':'goal','title':'学习摄影','source_quote':'我决定学习摄影。','explicit':True,'adopted':True},
                               {'key':'a1','kind':'action','goal_ref':'g1','title':'拍三张','source_quote':'周五先拍三张。','explicit':True,'adopted':True,'due':'2026-10-09'},
                               {'key':'n1','kind':'note','title':'构图想法','source_quote':'试试新构图。'}]}
    def tearDown(self): self.tmp.cleanup()
    def test_real_ids_source_and_replay(self):
        r=self.d.capture(self.payload)
        self.assertEqual(r['status'],'saved');self.assertEqual(len(r['saved_items']),3)
        g=self.d.store.get(r['saved_items'][0]['id'],'goal')
        self.assertEqual(g['origin_id'],'capture:'+r['id']);self.assertEqual(g['status'],'active')
        self.assertEqual(g['source_snapshot']['source_quote'],'我决定学习摄影。')
        self.assertEqual(g['source_snapshot']['source_id'],'synthetic-thread')
        self.assertTrue(g['source_snapshot']['captured_at'])
        self.assertEqual(g['steps'][0]['id'],r['saved_items'][1]['id'])
        self.assertEqual(g['steps'][0]['source_id'],'capture:'+r['id'])
        self.assertIn(self.payload['source_text'],self.d.read(g['steps'][0]['source_id'])['text'])
        self.assertTrue((self.d.store.path/'knowledge'/(r['saved_items'][2]['id']+'.md')).is_file())
        self.assertEqual(self.d.search('构图')[0]['id'],'note:'+r['saved_items'][2]['id'])
        again=self.d.capture(copy.deepcopy(self.payload))
        self.assertTrue(again['replayed']);self.assertEqual(again['saved_items'],r['saved_items'])
        self.assertEqual(len(self.d.store.all('goal')),1);self.assertEqual(len(self.d.store.all('note')),1)
        self.assertEqual(self.d.capture_status('fixture-1')['id'],r['id'])
        self.assertEqual(len(self.d.capture_recent()['captures']),1)
    def test_same_request_changed_content_refused(self):
        self.d.capture(self.payload);changed=copy.deepcopy(self.payload);changed['items'][0]['title']='不同标题'
        with self.assertRaisesRegex(ValueError,'不同内容'):self.d.capture(changed)
        self.assertEqual(len(self.d.store.all('goal')),1)
    def test_all_quotes_validated_before_any_write(self):
        self.payload['items'][-1]['source_quote']='不在原文'
        with self.assertRaisesRegex(ValueError,'连续子串'):self.d.capture(self.payload)
        self.assertEqual(self.d.store.all('goal'),[]);self.assertEqual(self.d.store.all('capture'),[])
    def test_unadopted_suggestions_are_draft(self):
        for i in self.payload['items'][:2]:i.pop('explicit');i.pop('adopted')
        self.payload['items'][0]['status']='active'
        r=self.d.capture(self.payload)
        self.assertEqual(r['saved_items'][0]['status'],'draft');self.assertEqual(r['saved_items'][1]['status'],'draft')
        self.assertEqual(r['warnings'][0]['code'],'missing_due')
    def test_same_source_multiple_goals_not_collapsed(self):
        p=copy.deepcopy(self.payload);p['items']=[p['items'][0],dict(p['items'][0],key='g2',title='理解光线')]
        r=self.d.capture(p);self.assertEqual(len({x['id'] for x in r['saved_items']}),2)
        self.assertEqual(len(self.d.store.all('goal')),2)
    def test_same_title_new_request_no_overwrite(self):
        r=self.d.capture(self.payload);p=copy.deepcopy(self.payload);p['request_id']='another';p['items']=p['items'][:1]
        before=self.d.store.get(r['saved_items'][0]['id'],'goal');a=self.d.capture(p)
        self.assertEqual(a['saved_items'][0]['status'],'existing_unchanged')
        self.assertEqual(a['saved_items'][0]['id'],before['id']);self.assertEqual(len(self.d.store.all('goal')),1)
        self.assertEqual(self.d.store.get(before['id'],'goal'),before)
        self.assertIn('duplicate_title',[w['code'] for w in a['warnings']])
    def test_explicit_goal_update_requires_current_rev(self):
        r=self.d.capture(self.payload);g=self.d.store.get(r['saved_items'][0]['id'],'goal')
        p=copy.deepcopy(self.payload);p['request_id']='update';p['items']=[dict(p['items'][0],id=g['id'],rev=g['rev'],title='摄影练习',due='2026-11-01')]
        a=self.d.capture(p);self.assertEqual(a['saved_items'][0]['id'],g['id'])
        self.assertEqual(a['saved_items'][0]['rev'],g['rev']+1)
        self.assertEqual(self.d.store.get(g['id'],'goal')['steps'],g['steps'])
        p['request_id']='stale'
        with self.assertRaisesRegex(ValueError,'已变化'):self.d.capture(p)
        p['items'][0].pop('rev')
        with self.assertRaisesRegex(ValueError,'rev'):self.d.capture(p)
    def test_existing_action_update_and_concurrent_revision(self):
        r=self.d.capture(self.payload);g=self.d.store.get(r['saved_items'][0]['id'],'goal')
        item=dict(self.payload['items'][1]);item.pop('goal_ref');item.update(goal_id=g['id'],goal_rev=g['rev'],id=g['steps'][0]['id'],title='拍四张')
        p=dict(self.payload,request_id='step-update',items=[item]);a=self.d.capture(p)
        self.assertEqual(a['saved_items'][0]['id'],g['steps'][0]['id']);self.assertEqual(len(self.d.store.get(g['id'],'goal')['steps']),1)
        latest=self.d.store.get(g['id'],'goal')['steps'][0]
        self.assertEqual(latest['source_id'],'capture:'+a['id'])
        self.assertIn(p['source_text'],self.d.read(latest['source_id'])['text'])
        p['request_id']='step-stale'
        with self.assertRaisesRegex(ValueError,'goal_rev'):self.d.capture(p)
    def test_suggestion_cannot_modify_existing_goal_or_action(self):
        r=self.d.capture(self.payload);g=self.d.store.get(r['saved_items'][0]['id'],'goal')
        goal=dict(self.payload['items'][0],id=g['id'],rev=g['rev']);goal.pop('adopted')
        action=dict(self.payload['items'][1],goal_id=g['id'],goal_rev=g['rev'],id=g['steps'][0]['id']);action.pop('goal_ref');action.pop('adopted')
        for item in (goal,action):
            with self.assertRaisesRegex(ValueError,'明确采用'):self.d.capture(dict(self.payload,request_id='suggested-update',items=[item]))
        self.assertEqual(self.d.store.get(g['id'],'goal'),g)
    def test_action_update_preserves_omitted_fields_and_goal_status(self):
        g=self.d.goal({'title':'完成的练习','status':'done','evidence':'合成证据','steps':[{'text':'原动作','done':True,'due':'2026-10-01','criterion':'三张','outcome':'已完成三张'}]})
        item={'key':'a','kind':'action','source_quote':'周五先拍三张。','title':'动作标题修订','goal_id':g['id'],'goal_rev':g['rev'],'id':g['steps'][0]['id'],'explicit':True,'adopted':True}
        r=self.d.capture(dict(self.payload,request_id='preserve-action',items=[item]));self.assertEqual(r['status'],'saved')
        latest=self.d.store.get(g['id'],'goal');step=latest['steps'][0]
        self.assertTrue(step['done']);self.assertEqual(step['due'],'2026-10-01');self.assertEqual(step['outcome'],'已完成三张');self.assertEqual(step['criterion'],'三张')
        self.assertEqual(r['saved_items'][0]['status'],'done');self.assertEqual(r['warnings'],[])
        item=dict(self.payload['items'][0],id=g['id'],rev=latest['rev'],title='目标改名')
        r=self.d.capture(dict(self.payload,request_id='preserve-goal-status',items=[item]))
        self.assertEqual(r['saved_items'][0]['status'],'done')
    def test_new_goal_crash_after_commit_recovers_without_duplicate(self):
        original=self.d.goal; calls=[]
        def fail_once(data):
            result=original(data);calls.append(1)
            if len(calls)==1:raise OSError('synthetic after-commit failure')
            return result
        with patch.object(self.d,'goal',side_effect=fail_once):r=self.d.capture(self.payload)
        self.assertEqual(r['status'],'partial');self.assertEqual(len(self.d.store.all('goal')),1)
        r=self.d.capture(self.payload);self.assertEqual(r['status'],'saved');self.assertEqual(len(self.d.store.all('goal')),1)
        self.assertEqual(len(self.d.store.all('note')),1)
    def test_updated_goal_crash_after_commit_recovers(self):
        g=self.d.goal({'title':'原目标'})
        p=dict(self.payload,request_id='update-crash',items=[dict(self.payload['items'][0],id=g['id'],rev=g['rev'])])
        original=self.d.goal
        def fail(data):original(data);raise OSError('synthetic after commit')
        with patch.object(self.d,'goal',side_effect=fail):r=self.d.capture(p)
        self.assertEqual(r['status'],'partial');self.assertEqual(self.d.store.get(g['id'],'goal')['rev'],2)
        r=self.d.capture(p);self.assertEqual(r['status'],'saved');self.assertEqual(self.d.store.get(g['id'],'goal')['rev'],2)
    def test_note_file_failure_repaired_and_index_not_falsely_claimed(self):
        p=dict(self.payload,items=[dict(self.payload['items'][-1],index=True)])
        with patch.object(self.d,'write_note',side_effect=OSError('synthetic disk failure')):r=self.d.capture(p)
        self.assertEqual(r['status'],'partial');self.assertEqual(len(self.d.store.all('note')),1)
        with patch.object(self.d,'index',side_effect=lambda nid:self.d.store.change(nid,'note',index_state='failed',index_error='fixture disconnected')):
            r=self.d.capture(p)
        self.assertEqual(r['status'],'saved');self.assertEqual(r['saved_items'][0]['index_state'],'failed')
        self.assertTrue((self.d.store.path/'knowledge'/(r['saved_items'][0]['id']+'.md')).exists())
        self.assertEqual(self.d.capture_status(p['request_id'])['saved_items'][0]['index_error'],'fixture disconnected')
    def test_parallel_replay_and_restart(self):
        responses=[]
        threads=[threading.Thread(target=lambda:responses.append(self.d.capture(copy.deepcopy(self.payload)))) for _ in range(3)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(len(responses),3);self.assertEqual(len(self.d.store.all('goal')),1)
        again=CaptureDesk(self.d.store.path).capture(self.payload)
        self.assertTrue(again['replayed']);self.assertEqual(len(again['saved_items']),3)
    def test_bad_dates_and_booleans_and_unknown_fields_fail_before_writes(self):
        variants=[{'due':'2026-02-30'},{'adopted':'true'},{'explicit':False,'adopted':True},{'bogus':True}]
        for changes in variants:
            p=copy.deepcopy(self.payload);p['items'][0].update(changes)
            with self.assertRaises(ValueError):self.d.capture(p)
        self.assertEqual(self.d.store.all('capture'),[])

class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec=importlib.util.spec_from_file_location('capture_cli',Path(__file__).resolve().parents[1]/'tools/capture_conversation.py')
        cls.cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cls.cli)
    def test_remote_or_credential_urls_rejected(self):
        for value in ['https://example.com','http://127.0.0.1:1234/path','http://user:pass@localhost:1234','http://localhost:1234?x=1','http://localhost']:
            with self.assertRaises(ValueError):self.cli.local_url(value)
        self.assertEqual(self.cli.local_url('http://127.0.0.1:60128/'),'http://127.0.0.1:60128')
    def test_token_only_sent_internally(self):
        calls=[]
        def fake(opener,base,path,payload=None,token=None):
            calls.append((path,token))
            if path=='/api/identity':return {'application':'growth-desk'}
            if path=='/api/state':return {'token':'fixture-secret-token','private':'not-output'}
            return {'status':'saved','saved_items':[]}
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'input.json';p.write_text('{}')
            with patch.object(self.cli,'call',side_effect=fake),patch('sys.stdout',new_callable=io.StringIO) as out:
                code=self.cli.main(['save','--file',str(p)])
            self.assertEqual(code,0);self.assertNotIn('fixture-secret-token',out.getvalue());self.assertNotIn('not-output',out.getvalue())
            self.assertEqual(calls[-1],('/api/capture','fixture-secret-token'))
    def test_redirect_refused(self):
        with self.assertRaises(ValueError):self.cli.NoRedirect().redirect_request(None,None,302,'',{},'https://example.com')

if __name__=='__main__': unittest.main()
