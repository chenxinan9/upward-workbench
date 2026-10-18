"""Focused safety checks over synthetic registered JSON plans."""
import json
import datetime as dt
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import Desk
from growth_plan import plan_intent


class PlanSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.data=self.root/'data';self.data.mkdir()
        self.file=self.root/'plan.json'
        self.original={'generated_at':'2020-01-01','life_panorama':{
            'north_star':{'quote':'原方向','source_id':'old','date':'2020-01-01'},
            'pillars':[{'id':'health','title':'健康','desired_life':'有精力'},
                       {'id':'business','title':'事业','desired_life':'做有价值的事'}],
            'route':[{'title':'验证阶段','description':'原路线'}],'history':[]},
            'current_focus_draft':{'title':'原重点','date':'2020-01-01','reason':'保留原因'},
            'source_register':[],'unknown':{'preserved':True}}
        self.file.write_text(json.dumps(self.original,ensure_ascii=False))
        (self.data/'settings.json').write_text(json.dumps({'growth_plan':str(self.file)}))
        self.d=Desk(self.data);self.addCleanup(self.d.stop_workspace)

    def update(self,section,changes,message):
        return self.d.update_growth_plan(section,json.dumps(changes,ensure_ascii=False),
            self.d.read_growth_plan()['sha256'],message=message,job_id='synthetic',stop=threading.Event())

    def test_entire_reported_or_question_context_cannot_authorize_a_later_clause(self):
        for text in ('他说，把今日重点改成录视频并保存。','我昨天说过，把今日重点改为录视频。',
                     '如何操作呢，把今日重点更新为录视频。','朋友建议，把人生全景更新为自由写作。'):
            self.assertFalse(plan_intent(text,'daily') or plan_intent(text,'panorama'),text)
        self.assertTrue(plan_intent('把今日重点设为拍摄演示，不修改其他内容。','daily'))
        self.assertTrue(plan_intent('不要改长期方向，只把今日重点定为录制演示。','daily'))

    def test_unrequested_or_protected_array_targets_cannot_be_changed(self):
        before=self.file.read_bytes()
        cases=[({'pillars':[{'index':0,'desired_life':'恶意夹带'}]},'请把人生全景中的长期方向更新为写作，不修改支柱和长期路线。'),
               ({'pillars':[{'index':1,'desired_life':'错误对象'}]},'请把人生全景健康维度的期望生活更新为规律训练，其他维度不改。'),
               ({'pillars':[{'index':1,'desired_life':'错误对象'}]},'请把人生全景健康维度的期望生活更新为健康支持事业，其他维度不改。'),
               ({'route':[{'index':0,'description':'未请求路线'}]},'请把长期方向更新为自由写作。')]
        for patch,message in cases:
            with self.subTest(message=message),self.assertRaises(PermissionError):self.update('panorama',patch,message)
            self.assertEqual(self.file.read_bytes(),before)

    def test_writing_cannot_make_a_previously_readable_projection_unreadable(self):
        initial=dict(self.original)
        initial['life_panorama']=dict(initial['life_panorama'],history=[{'description':'合'*118000}])
        self.file.write_text(json.dumps(initial,ensure_ascii=False));before=self.file.read_bytes()
        self.assertLess(len(json.dumps(self.d.read_growth_plan(),ensure_ascii=False)),120000)
        with self.assertRaisesRegex(ValueError,'过长'):
            self.update('daily',{'reason':'明'*2800},'请更新今日重点的理由并保存。')
        self.assertEqual(self.file.read_bytes(),before)
        self.assertFalse(self.d.store.all('workspace-write'))

    def test_restore_is_real_json_readback_and_never_overwrites_newer_edits(self):
        before=self.file.read_bytes()
        result=self.update('daily',{'title':'拍摄演示'},'把今日重点设为拍摄演示。')
        self.assertEqual(result['format'],'json');self.assertTrue(result['read_back'])
        restored=self.d.undo_growth_plan(result['undo_id'],result['sha256'])
        self.assertTrue(restored['verified']);self.assertEqual(self.file.read_bytes(),before)
        result=self.update('daily',{'title':'再拍一段'},'把今日重点设为再拍一段。')
        self.file.write_text(json.dumps(dict(self.original,externally_changed=True)))
        external=self.file.read_bytes()
        with self.assertRaises(ValueError):self.d.undo_growth_plan(result['undo_id'],result['sha256'])
        self.assertEqual(self.file.read_bytes(),external)

    def test_new_direction_has_readable_current_statement_and_keeps_old_attribution_in_history(self):
        result=self.update('panorama',{'north_star':{'quote':'新的长期方向'}},'把长期方向更新为新的长期方向。')
        star=result['life_panorama']['north_star'];self.assertNotEqual(star['source_id'],'old')
        self.assertIn('新的长期方向',self.d.source(star['source_id'])['text'])
        self.assertIn('把长期方向更新为新的长期方向',result['update_statement']['text'])
        history=result['life_panorama']['history'];self.assertEqual(history[-1]['source_id'],'old')
        self.assertEqual(json.loads(self.file.read_text())['generated_at'],'2020-01-01')

    def test_configured_external_canonical_uses_its_registered_target_without_relative_path(self):
        ledger=self.root/'ledger.json';ledger.write_text('{"updated_at":"2020-01-01"}')
        config=self.root/'config.json';config.write_text(json.dumps({'canonical_commitments':str(ledger)}))
        self.d.settings['advisor_config']=str(config)
        plan=dict(self.original,source_register=[{'id':'external','external_key':'canonical_commitments','date':'2020-01-01'}])
        self.file.write_text(json.dumps(plan));row=self.d.source_freshness()['sources'][0]
        self.assertNotEqual(row['status'],'missing');self.assertIsNotNone(row['current_version'])
        self.assertIsNone(row['registered_at']);self.assertIsNone(row['last_checked_at'])

    def test_registered_read_time_is_never_relabelled_as_a_fact_check(self):
        for dates in ({'read_at':'2026-10-15'},{'checked_at':'2026-10-14'},
                      {'read_at':'2026-10-15','checked_at':'2026-10-14'}):
            plan=dict(self.original,source_register=[dict(id='web',root='web',url='https://example.org',**dates)])
            self.file.write_text(json.dumps(plan));row=self.d.source_freshness()['sources'][0]
            self.assertEqual(row['last_checked_at'],dates.get('checked_at'))
            self.assertEqual(row['last_read_at'],dates.get('read_at'))
            self.assertIsNone(row['fact_confirmed_at'])

    def test_protected_daily_title_cannot_be_replaced_while_reason_can_be_updated(self):
        message='不要修改今日重点的标题，只把今日重点的理由更新为今天有时间。'
        before=self.file.read_bytes()
        with self.assertRaises(PermissionError):self.update('daily',{'title':'错误替换'},message)
        self.assertEqual(self.file.read_bytes(),before)
        result=self.update('daily',{'reason':'今天有时间'},message)
        self.assertEqual(result['current_focus_draft']['title'],'原重点')
        self.assertEqual(result['current_focus_draft']['reason'],'今天有时间')
        result=self.update('daily',{'title':'拍摄演示','reason':'今天完成一次验证'},
            '不要改我的长期方向，只把今日重点改成拍摄演示，原因是今天完成一次验证，请保存。')
        self.assertEqual(result['current_focus_draft']['reason'],'今天完成一次验证')

    def test_restore_respects_readonly_files_and_preserves_writable_mode(self):
        result=self.update('daily',{'title':'拍摄演示'},'把今日重点设为拍摄演示。')
        self.file.chmod(0o400);before=self.file.read_bytes()
        with self.assertRaises(PermissionError):self.d.undo_growth_plan(result['undo_id'],result['sha256'])
        self.assertEqual(self.file.read_bytes(),before);self.assertEqual(self.file.stat().st_mode&0o777,0o400)
        self.file.chmod(0o640);self.d.undo_growth_plan(result['undo_id'],result['sha256'])
        self.assertEqual(self.file.stat().st_mode&0o777,0o640)

    def test_same_text_explicit_daily_choice_confirms_proposal_and_today_draft_stays_unconfirmed_before(self):
        today=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date().isoformat()
        for status in ('proposal','draft',None):
            plan=dict(self.original);plan['current_focus_draft']={'title':'拍摄演示','date':today}
            if status:plan['current_focus_draft']['status']=status
            self.file.write_text(json.dumps(plan));focus=self.d.growth_snapshot()['today']['focus']
            self.assertFalse(focus['applies_today']);self.assertIn('尚未确认',focus['date_note'])
            result=self.update('daily',{'title':'拍摄演示'},'把今日重点设为拍摄演示。')
            self.assertTrue(result['changed']);self.assertEqual(result['current_focus_draft']['status'],'confirmed')

    def test_restart_keeps_json_receipt_routing_and_real_restore(self):
        before=self.file.read_bytes();job=self.d.store.put('workspace-job',{'status':'running'})
        result=self.d.update_growth_plan('daily',json.dumps({'title':'拍摄演示'}),self.d.read_growth_plan()['sha256'],
            message='把今日重点设为拍摄演示。',job_id=job['id'],stop=threading.Event())
        restarted=Desk(self.data);self.addCleanup(restarted.stop_workspace)
        recovered=restarted.store.get(job['id'],'workspace-job')['write_receipts'][0]
        self.assertEqual(recovered['format'],'json');self.assertEqual(recovered['operation'],'plan_update')
        self.assertEqual(recovered['section'],'daily');self.assertEqual(recovered['sha'],result['sha256'])
        restored=restarted.workspace_post('/api/workspace/growth-plan/undo',{'undo_id':recovered['undo_id'],'expected_sha':recovered['sha']})
        self.assertTrue(restored['verified']);self.assertEqual(self.file.read_bytes(),before)


if __name__=='__main__':unittest.main()
