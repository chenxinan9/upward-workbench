"""Independent phase and local-route acceptance; synthetic records only."""
import datetime as realdt
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desk_fixture import Desk
from phase import TZ, local_date

REAL_DATETIME = realdt.datetime
class FrozenDateTime(REAL_DATETIME):
    @classmethod
    def now(cls, tz=None):
        instant = cls(2026, 10, 7, 12, 0, tzinfo=TZ)
        return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

class PhaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.d = Desk(self.root / 'data')
        self.plan = {'current_focus_draft': {'title': '自媒体'}, 'life_panorama': {}, 'source_register': []}
        self.patches = [patch('phase.dt.datetime', FrozenDateTime), patch.object(self.d, 'plan', side_effect=lambda:self.plan), patch.object(self.d, 'creator_evidence', return_value={'status':'not_configured','works':[],'publications':[],'warnings':['未连接，不代表没有成果']} )]
        for p in self.patches:p.start()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def note(self, body, at, **kw):
        return self.d.store.put('note',dict(title=body,body=body,created_at=at,kind='idea',**kw))
    def msg(self, text, at, role='user', **kw):
        return self.d.store.put('message',dict(text=text,created_at=at,role=role,status='completed',**kw))
    def test_shanghai_day_conversion(self):
        self.assertEqual(local_date('2026-09-30T16:00:00Z'),'2026-10-01')
        self.assertEqual(local_date('2026-09-30T15:59:59Z'),'2026-09-30')
        self.assertEqual(local_date('2026-10-01T00:00:00'),'2026-10-01')
        self.assertIsNone(local_date('nonsense'))
    def test_seven_days_inclusive_boundaries_and_future(self):
        self.note('included-start','2026-09-30T16:00:00Z')
        self.note('too-old','2026-09-30T15:59:59Z')
        self.note('included-now','2026-10-07T04:00:00Z')
        self.note('future-same-day','2026-10-07T04:00:01Z')
        self.note('future-next-day','2026-10-08T00:00:00+08:00')
        s=self.d.phase_snapshot(7)
        self.assertEqual(s['period']['start'],'2026-10-01')
        self.assertEqual(s['period']['end'],'2026-10-07')
        self.assertEqual({n['body'] for n in s['ideas']},{'included-start','included-now'})
    def test_thirty_days_and_updated_at_cannot_revive_old_record(self):
        self.note('included-30','2026-09-07T16:00:00Z')
        old=self.note('excluded-30','2026-09-07T15:59:59Z')
        self.d.store.change(old['id'],'note',title='recently modified old note')
        s=self.d.phase_snapshot(30)
        self.assertEqual(s['period']['start'],'2026-09-08')
        self.assertEqual([x['body'] for x in s['ideas']],['included-30'])
        self.assertEqual(self.d.phase_snapshot(7)['ideas'],[])
    def test_invalid_periods_rejected(self):
        for days in [True,False,0,1,8,31,'broken']:
            with self.subTest(days=days),self.assertRaises((ValueError,TypeError)):self.d.phase_snapshot(days)
    def test_capture_dedup_uses_original_user_words_and_excludes_assistant(self):
        m=self.msg('今天想做视频','2026-10-07T01:00:00Z',job_id='job1')
        self.msg('你今天已经发布十篇视频','2026-10-07T01:01:00Z',role='assistant')
        n=self.d.store.put('note',{'title':'模型摘要','body':'已发布十篇','kind':'conversation','created_at':'2026-10-07T01:02:00Z'})
        self.d.store.put('note',{'title':'游离摘要','body':'不存在的成就','kind':'conversation','created_at':'2026-10-07T01:03:00Z'})
        receipts={'captures':[{'id':'c1','created_at':'2026-10-07T01:02:00Z','source':'workbench_conversation','source_id':'job:job1','source_text':'今天想做视频','saved_items':[{'id':n['id'],'kind':'note'}],'status':'saved'}]}
        with patch.object(self.d,'capture_recent',return_value=receipts):s=self.d.phase_snapshot()
        self.assertEqual(len(s['ideas']),1)
        self.assertEqual(s['ideas'][0]['body'],'今天想做视频')
        self.assertNotIn('十篇',json.dumps(s,ensure_ascii=False))
        self.assertNotIn('不存在的成就',json.dumps({k:s[k] for k in ('ideas','reviews','events')},ensure_ascii=False))
        self.assertEqual([x['body'] for x in s['conclusions']],['不存在的成就'])
        self.assertIn('不等同本人事实',s['conclusions'][0]['source'])
        self.assertEqual([e['kind'] for e in s['events']],['capture'])
    def test_new_capture_of_old_utterance_is_not_a_new_idea(self):
        n=self.d.store.put('note',{'title':'本次整理','body':'摘要','kind':'conversation','created_at':'2026-10-07T01:02:00Z'})
        receipts={'captures':[{'id':'c-old','created_at':'2026-10-07T01:02:00Z','source':'workbench_conversation','source_id':'job:old','source_time':'2026-09-01T01:00:00Z','source_text':'九月一日的旧想法','saved_items':[{'id':n['id'],'kind':'note'}],'status':'saved'}]}
        with patch.object(self.d,'capture_recent',return_value=receipts):s=self.d.phase_snapshot()
        self.assertEqual(s['ideas'],[], '旧原话新整理时只能作为整理事件，不能复活为本阶段的新想法')
        self.assertEqual([e['kind'] for e in s['events']],['capture'])
    def test_ideas_order_by_actual_instant_not_timestamp_spelling(self):
        self.note('较早上海','2026-10-07T09:00:00+08:00')
        self.note('较晚UTC','2026-10-07T03:00:00Z')
        self.assertEqual([x['body'] for x in self.d.phase_snapshot()['ideas']],['较晚UTC','较早上海'])
    def review_job(self,intent='phase_review'):
        period={'days':7,'start':'2026-10-01','end':'2026-10-07','timezone':'Asia/Shanghai'}
        context={'intent':intent,'stage_days':7,'context_hash':'immutable-context-fixture','contexts':[{'id':'phase:7','title':'当轮阶段','text':json.dumps({'period':period})},{'id':'source:S15','title':'当轮方法','text':'历史方法'}],'advisor_selection':[{'name':'example','title':'合成顾问','reason':'阶段卡点'}]}
        job=self.d.store.put('job',{'status':'completed','answer_context':context})
        message=self.d.store.put('message',{'job_id':job['id'],'role':'assistant','text':'当轮阶段结论','status':'completed','created_at':'2026-10-07T01:00:00Z'})
        return job,message,period
    def test_review_save_edit_keeps_original_period_and_exports_it(self):
        job,message,period=self.review_job()
        saved=self.d.note({'kind':'review','title':'阶段复盘','body':'合成复盘正文','source':'codex:'+job['id']})
        self.assertEqual(saved['review_period'],period)
        self.assertEqual(saved['review_basis']['context_hash'],'immutable-context-fixture')
        self.assertEqual(saved['review_basis']['sources'][1]['id'],'source:S15')
        self.assertEqual(saved['review_basis']['advisors'][0]['name'],'example')
        self.assertEqual(saved['basis_ref'],'message:'+message['id'])
        # A saved review is a snapshot; later source loss must not erase its period.
        with self.d.store.conn() as c:c.execute('DELETE FROM objects WHERE id=?',(job['id'],))
        edited=self.d.note(dict(saved,body='编辑后的复盘正文'))
        self.assertEqual(edited.get('review_period'),period)
        self.assertEqual(edited.get('review_basis'),saved['review_basis'])
        self.assertEqual(edited.get('basis_ref'),saved['basis_ref'])
        exported=(self.d.store.path/'knowledge'/(saved['id']+'.md')).read_text()
        self.assertIn('2026-10-01 — 2026-10-07',exported);self.assertIn('Asia/Shanghai',exported)
        self.assertEqual(self.d.phase_snapshot()['reviews'][0]['review_period'],period)
    def test_legacy_review_recovers_only_its_own_original_job_basis(self):
        job,message,period=self.review_job()
        self.d.store.put('note',{'kind':'review','title':'旧复盘','body':'旧正文','source':'codex:'+job['id'],'created_at':'2026-10-07T02:00:00Z'})
        row=self.d.phase_snapshot()['reviews'][0]
        self.assertEqual(row['review_period'],period);self.assertEqual(row['basis_ref'],'message:'+message['id'])
    def test_discussion_and_missing_or_malformed_jobs_do_not_fabricate_review_basis(self):
        job,message,period=self.review_job(intent='discussion')
        self.assertEqual(self.d.review_metadata('codex:'+job['id']),{})
        self.assertEqual(self.d.review_metadata('codex:missing'),{})
        for source in [None,'','note:somewhere','codex:missing','codex:'+job['id']]:
            saved=self.d.note({'kind':'review','body':'合成手工记录','source':source or ''})
            self.assertNotIn('review_period',saved);self.assertNotIn('review_basis',saved)
        malformed=self.d.store.put('job',{'status':'completed','answer_context':{'intent':'phase_review','contexts':[{'id':'phase:7','title':'坏数据','text':'{broken'}]}})
        self.assertEqual(self.d.review_metadata('codex:'+malformed['id']),{})
        self.assertTrue(all('review_period' not in n for n in self.d.phase_snapshot()['reviews']))
    def test_goal_background_is_not_claimed_as_recent_event(self):
        self.d.store.put('goal',{'title':'完成一组摄影练习','created_at':'2020-01-01','status':'active'})
        self.d.store.put('learning',{'advisor':'旧学习','created_at':'2020-01-01','status':'completed'})
        s=self.d.phase_snapshot()
        self.assertEqual(s['goals'][0]['title'],'完成一组摄影练习')
        self.assertEqual(s['events'],[])
    def test_multiplatform_publication_is_one_work_pending_not_counted(self):
        def publication(i, project, platform, phase='published'):
            return dict(id=i,project_id=project,title=project,platform=platform,phase=phase,status=phase,at='2026-10-07T01:00:00Z',source='local',live_verified=False)
        evidence={'status':'connected','works':[],'warnings':[],'publications':[publication('a','one','youtube'),publication('b','one','douyin'),publication('c','two','bilibili','pending')]}
        with patch.object(self.d,'creator_evidence',return_value=evidence):s=self.d.phase_snapshot()
        self.assertEqual(s['creator']['days'][-1]['count'],1)
        self.assertEqual(len(s['creator']['publications']),3)
        self.assertIn('不代表刚刚核验',s['creator']['summary'])
    def test_missing_creator_source_not_reported_as_zero_productivity(self):
        s=self.d.phase_snapshot()
        self.assertEqual(s['creator']['status'],'not_configured')
        self.assertIn('不等于',s['creator']['summary'])
        self.assertTrue(all(x['status']=='尚无发布留底' for x in s['creator']['days']))
        self.assertTrue(any(x['id']=='creator' and x['status']=='not_configured' for x in s['sources']))
    def test_context_is_bounded_valid_json_with_large_realistic_data(self):
        for i in range(100):
            self.note('想法'+str(i)+'长'*5000,'2026-10-07T01:00:00Z')
        for i in range(40):
            self.d.store.put('goal',{'title':'目标'+str(i),'created_at':'2026-01-01','steps':[{'text':'步'*5000}]*50})
        self.plan['life_panorama']={'long':'全景'*50000}
        context=self.d.phase_context()
        self.assertLessEqual(len(context['text']),24000)
        self.assertEqual(context['included_characters'],len(context['text']))
        self.assertEqual(json.loads(context['text'])['period']['days'],7)
        self.assertTrue(context['truncated'])
    def test_actual_historical_method_bytes_and_missing_source_are_explicit(self):
        private=self.root/'private';private.mkdir()
        (private/'让我伟大.md').write_text('真实历史方法哨兵：先找瓶颈，再核对阶段成果。')
        self.d.settings['private_root']=str(private)
        self.plan['source_register']=[{'id':'S15','title':'让我伟大','evidence_type':'historical_method','path':'让我伟大.md','date':'2026-05-22'},{'id':'S16','title':'让我伟大 2','evidence_type':'historical_method','path':'missing.md','date':'2026-06-04'}]
        contexts=self.d.phase_contexts()
        source=next(c for c in contexts if c['id']=='source:S15')
        self.assertIn('真实历史方法哨兵',source['text'])
        self.assertTrue(source['current_sha256'])
        missing=next(c for c in contexts if c['id']=='source:S16')
        self.assertIn('不能声称',missing['text'])
        self.assertIn('历史',source['kind'])

class CreatorBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.d=Desk(Path(self.tmp.name)/'data');self.d.settings['creator_port']=60019
    def tearDown(self):self.tmp.cleanup()
    def test_bridge_excludes_synthetic_and_duplicates_and_forbids_proxy_redirect(self):
        class Response:
            def __init__(self,value):self.raw=json.dumps(value).encode()
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def read(self,n):return self.raw[:n]
        state={'projects':[{'id':'p','title':'production','created_at':'2026-10-07'},{'id':'test','title':'synthetic','synthetic':True}]}
        row={'id':'r','platform':'youtube','post_id':'post','project_id':'p','project_title':'production','evidence_at':'2026-10-07','phase':'published'}
        history={'records':[row,dict(row,id='r-duplicate'),dict(row,id='test-r',post_id='test-post',project_id='test')]}
        with patch('phase.urllib.request.build_opener') as build:
            build.return_value.open.side_effect=[Response(state),Response(history)]
            result=self.d.creator_evidence()
            self.assertEqual(len(result['publications']),1)
            self.assertEqual(len(result['works']),1)
            self.assertEqual(build.call_args.args[0].proxies,{})
            self.assertIsNone(build.call_args.args[1].redirect_request(None,None,None,None,None,None))
            urls=[c.args[0] for c in build.return_value.open.call_args_list]
            self.assertTrue(all(u.startswith('http://127.0.0.1:60019/') for u in urls))
            self.d.creator_evidence();self.assertEqual(build.return_value.open.call_count,2)
    def test_failed_reads_are_unavailable_and_not_silent_zero(self):
        with patch('phase.urllib.request.build_opener') as build:
            build.return_value.open.side_effect=OSError('offline')
            s=self.d.creator_evidence()
        self.assertEqual(s['status'],'unavailable')
        self.assertEqual(len(s['warnings']),2)
        self.assertTrue(all('缺失不表示没有成果' in w for w in s['warnings']))

class LocalRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.d=Desk(self.root/'data');self.d.cli='synthetic'
        skill=self.root/'skill';skill.mkdir();(skill/'SKILL.md').write_text('---\nname: example\ndescription: 合成测试\n---\n# 方法\n方法哨兵')
        catalog=self.root/'catalog.json';catalog.write_text(json.dumps({'entries':[{'name':'example','title':'合成顾问','category':'实验','installed_path':str(skill),'status':'installed_verified','url':'https://example.com'}]}));self.d.settings['catalog']=str(catalog)
        self.local=self.root/'local';self.local.mkdir();self.file=self.local/'fixture.txt';self.file.write_text('本地文件正文哨兵')
        self.d.settings['local_access']={'enabled':True,'read_roots':[str(self.local)],'write_roots':[str(self.local)],'notes_enabled':False}
    def tearDown(self):self.tmp.cleanup()
    def job(self,message=None,**options):
        message=message or '请读取文件 '+str(self.file)
        p=self.d.preview({'message':message,'organize':False,**options});s=self.d.store.put('session',{'title':'test'})
        j=self.d.store.put('job',{'status':'queued','session_id':s['id'],'preview_id':p['id'],'organize':False,'message':message,'prompt':p['prompt']})
        return j
    def routed(self,j,tool='files.read',**extra):
        request={'tool':tool,'user_quote':j['message'],**extra}
        if tool=='files.read':request['path']=str(self.file)
        return json.dumps({'selection':[{'name':'example','reason':'核对本地证据'}],'local_requests':[request]})
    def test_real_file_context_and_action_receipt_reach_answer_and_message(self):
        j=self.job();calls=[]
        def model(job,cmd,folder,prompt,stage,timeout):
            calls.append((stage,prompt));return self.routed(j) if stage=='routing' else '已读取合成文件，建议核对来源。'
        with patch.object(self.d,'model_text',side_effect=model):self.d.run(j)
        saved=self.d.store.get(j['id'])
        self.assertEqual(saved['status'],'completed',saved.get('error'))
        self.assertEqual(len(calls),2)
        self.assertNotIn('本地文件正文哨兵',calls[0][1])
        self.assertIn('本地文件正文哨兵',calls[1][1])
        self.assertIn('sha256',calls[1][1])
        self.assertEqual(saved['local_actions'][0]['status'],'completed')
        reply=self.d.messages(j['session_id'])[0]['reply_context']
        self.assertEqual(reply['local_actions'][0]['tool'],'files.read')
    def test_router_cancellation_prevents_local_action(self):
        j=self.job()
        def model(*args):self.d.store.change(j['id'],'job',status='cancelled');return self.routed(j)
        with patch.object(self.d,'model_text',side_effect=model),patch.object(self.d,'execute_local_requests') as execute:self.d.run(j)
        execute.assert_not_called();self.assertEqual(self.d.store.get(j['id'])['status'],'cancelled')
        self.assertEqual(self.d.store.all('message'),[])
    def test_answer_failure_preserves_receipt_without_fake_reply_or_capture(self):
        j=self.job()
        with patch.object(self.d,'model_text',side_effect=[self.routed(j),ValueError('answer failed')]):self.d.run(j)
        saved=self.d.store.get(j['id']);self.assertEqual(saved['status'],'failed')
        self.assertEqual(saved['local_actions'][0]['status'],'completed')
        self.assertEqual(self.d.store.all('message'),[]);self.assertEqual(self.d.store.all('capture'),[])
    def test_started_receipt_is_persisted_before_tool_and_terminal_after(self):
        j=self.job();perform=self.d._perform_local;seen=[]
        def check(request,quote):
            seen.extend(a['status'] for a in self.d.store.get(j['id'])['local_actions'])
            return perform(request,quote)
        with patch.object(self.d,'_perform_local',side_effect=check),patch.object(self.d,'model_text',side_effect=[self.routed(j),'完成']):self.d.run(j)
        self.assertEqual(seen,['started'])
        self.assertEqual(self.d.store.get(j['id'])['local_actions'][0]['status'],'completed')
    def test_restart_turns_only_started_receipts_uncertain_and_job_interrupted(self):
        j=self.job()
        self.d.store.change(j['id'],'job',status='running',local_actions=[{'id':'a','tool':'files.write','status':'started'},{'id':'b','tool':'files.read','status':'completed','sha256':'fixture'}])
        reloaded=Desk(self.d.store.path)
        saved=reloaded.store.get(j['id'])
        self.assertEqual(saved['status'],'interrupted')
        self.assertEqual([a['status'] for a in saved['local_actions']],['uncertain','completed'])
        self.assertIn('勿直接重复写入',saved['local_actions'][0]['summary'])
        self.assertEqual(reloaded.store.all('message'),[])
    def test_cancel_after_tool_preserves_receipt_but_cannot_answer(self):
        j=self.job();perform=self.d._perform_local
        def cancel_after(request,quote):
            result=perform(request,quote)
            self.d.store.change(j['id'],'job',status='cancelled')
            return result
        with patch.object(self.d,'_perform_local',side_effect=cancel_after),patch.object(self.d,'model_text',return_value=self.routed(j)) as model:self.d.run(j)
        self.assertEqual(model.call_count,1)
        saved=self.d.store.get(j['id']);self.assertEqual(saved['status'],'cancelled')
        self.assertEqual(saved['local_actions'][0]['status'],'completed')
        self.assertEqual(self.d.store.all('message'),[])
    def test_real_literal_write_records_verified_receipt_before_answer_failure(self):
        target=self.local/'saved.txt';j=self.job('保存到 '+str(target)+'，内容：合成原文')
        route=self.routed(j,'files.write',path=str(target),content='合成原文')
        with patch.object(self.d,'model_text',side_effect=[route,ValueError('answer failed')]):self.d.run(j)
        saved=self.d.store.get(j['id'])
        self.assertEqual(saved['status'],'failed')
        self.assertEqual(saved['local_actions'][0]['status'],'completed',saved['local_actions'])
        self.assertTrue(saved['local_actions'][0]['verified'])
        self.assertEqual(target.read_text(),'合成原文')
        self.assertEqual(self.d.store.all('message'),[])
    def test_batch_cancel_during_first_tool_preserves_first_and_prevents_second(self):
        j=self.job();perform=self.d._perform_local
        route=json.loads(self.routed(j));route['local_requests']*=2
        def cancel_during(request,quote):
            self.d.cancel(j['id'])
            return perform(request,quote)
        with patch.object(self.d,'_perform_local',side_effect=cancel_during) as tool,patch.object(self.d,'model_text',return_value=json.dumps(route)) as model:self.d.run(j)
        self.assertEqual(tool.call_count,1)
        self.assertEqual(model.call_count,1)
        saved=self.d.store.get(j['id']);self.assertEqual(saved['status'],'cancelled')
        self.assertEqual([a['status'] for a in saved['local_actions']],['completed'])
        self.assertIn('已经开始的本地读写不自动撤回',saved['error'])
        self.assertEqual(self.d.store.all('message'),[])
    def test_cancel_returns_while_tool_io_is_still_waiting(self):
        j=self.job();perform=self.d._perform_local
        route=json.loads(self.routed(j));route['local_requests']*=2
        entered=threading.Event();release=threading.Event();cancelled=threading.Event();errors=[]
        def slow(request,quote):
            entered.set()
            if not release.wait(3):raise RuntimeError('test release timed out')
            return perform(request,quote)
        def work():
            try:self.d.run(j)
            except BaseException as error:errors.append(error)
        def cancel():
            try:self.d.cancel(j['id'])
            except BaseException as error:errors.append(error)
            finally:cancelled.set()
        with patch.object(self.d,'_perform_local',side_effect=slow) as tool,patch.object(self.d,'model_text',return_value=json.dumps(route)):
            worker=threading.Thread(target=work);worker.start();canceller=None
            try:
                self.assertTrue(entered.wait(2),'tool did not start')
                canceller=threading.Thread(target=cancel);canceller.start()
                self.assertTrue(cancelled.wait(1),'cancel incorrectly waits for the whole local I/O batch')
                self.assertTrue(worker.is_alive(),'worker should still be waiting on I/O')
                self.assertFalse(release.is_set())
            finally:
                release.set();worker.join(3)
                if canceller:canceller.join(3)
        self.assertFalse(errors,errors)
        self.assertFalse(worker.is_alive())
        self.assertEqual(tool.call_count,1)
        saved=self.d.store.get(j['id']);self.assertEqual(saved['status'],'cancelled')
        self.assertEqual([a['status'] for a in saved['local_actions']],['completed'])
        self.assertEqual(self.d.store.all('message'),[])
    def test_phase_review_intent_and_days_are_frozen_in_reply_context(self):
        context={'id':'phase:30','title':'合成阶段','kind':'fixture','text':'{"fixture":"阶段哨兵"}'}
        with patch.object(self.d,'phase_contexts',return_value=[context]) as phase:
            j=self.job('请帮我阶段复盘',include_stage=True,stage_days=30,intent='phase_review')
        phase.assert_called_once_with(30)
        preview=self.d.store.get(j['preview_id'])
        self.assertEqual(preview['intent'],'phase_review');self.assertEqual(preview['stage_days'],30)
        route=json.dumps({'selection':[{'name':'example','reason':'分析阶段'}]})
        with patch.object(self.d,'model_text',side_effect=[route,'合成阶段复盘结论']):self.d.run(j)
        self.d.store.change(preview['id'],'preview',intent='discussion',stage_days=7)
        reply=self.d.messages(j['session_id'])[0]['reply_context']
        self.assertEqual(reply['intent'],'phase_review');self.assertEqual(reply['stage_days'],30)
        self.assertIn('阶段哨兵',reply['contexts'][0]['text'])
    def test_default_stage_context_does_not_turn_discussion_into_review(self):
        context={'id':'phase:7','title':'阶段','kind':'fixture','text':'{}'}
        with patch.object(self.d,'phase_contexts',return_value=[context]):
            j=self.job('今天聊聊一个点子',include_stage=True,stage_days=7)
        route=json.dumps({'selection':[{'name':'example','reason':'讨论点子'}]})
        with patch.object(self.d,'model_text',side_effect=[route,'一般讨论']):self.d.run(j)
        reply=self.d.messages(j['session_id'])[0]['reply_context']
        self.assertEqual(reply['intent'],'discussion');self.assertEqual(reply['stage_days'],7)
        self.assertTrue(any(c['id']=='phase:7' for c in reply['contexts']))
    def test_disabled_notes_are_blocked_and_no_invented_context(self):
        j=self.job('请读取备忘录第一条内容')
        calls=[]
        def model(job,cmd,folder,prompt,stage,timeout):calls.append(prompt);return self.routed(j,'notes.read',id='latest') if stage=='routing' else '备忘录未连接，无法读取。'
        with patch.object(self.d,'model_text',side_effect=model):self.d.run(j)
        action=self.d.store.get(j['id'])['local_actions'][0]
        self.assertEqual(action['status'],'blocked')
        self.assertIn('blocked',calls[-1])
        self.assertNotIn('本地文件正文哨兵',calls[-1])

if __name__=='__main__':unittest.main()
