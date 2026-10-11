import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from app import Desk
from phase import TZ
from growth_workspace import metadata
from tools.migrate_workspace import render

class GrowthWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.data=self.root/'data';self.data.mkdir();self.vault=self.root/'vault';self.vault.mkdir()
        self.plan=self.root/'plan.json'
        self.plan.write_text(json.dumps({'source_register':[],'domains':[],'current_focus_draft':{'title':'合成当前方向','status':'proposal'},'life_panorama':{'north_star':{'quote':'合成长期愿望'},'pillars':[{'title':'健康与关系'}]}}))
        (self.data/'settings.json').write_text(json.dumps({'growth_plan':str(self.plan),'vault_roots':[{'id':'upward','path':str(self.vault),'writable':True}]}))
        self.desk=Desk(self.data);self.today=dt.datetime.now(TZ).date().isoformat()
    def doc(self,name,text):
        p=self.vault/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text);return p
    def test_new_user_conversation_in_recent_ideas_but_not_ai_or_future(self):
        self.desk.store.put('workspace-message',{'role':'user','text':'新的自媒体想法'})
        self.desk.store.put('workspace-message',{'role':'assistant','text':'助手建议不是本人想法'})
        self.desk.store.put('workspace-message',{'role':'user','text':'未来记录','created_at':'2099-01-01T12:00:00+08:00'})
        rows=self.desk.growth_snapshot()['ideas']
        self.assertEqual([x['body'] for x in rows],['新的自媒体想法'])
    def test_recent_does_not_use_file_modification_time(self):
        self.doc('想法与笔记/历史.md','---\nrecord_kind: "idea"\ncreated_at: "2020-01-01"\n---\n# 历史\n历史原话')
        self.doc('想法与笔记/无日期.md','# 无日期\n未知日期不能当近期')
        self.assertFalse(self.desk.growth_snapshot()['ideas'])
    def test_shared_markdown_replaces_legacy_note_without_duplicates(self):
        n=self.desk.store.put('note',{'title':'原始笔记','body':'旧内容','kind':'idea'})
        content=render('note',n['id'],n,n['rev']).replace('旧内容','外部编辑后的真实内容')
        self.doc('想法与笔记/笔记.md',content)
        rows=self.desk.growth_snapshot()['ideas']
        self.assertEqual(len(rows),1);self.assertIn('外部编辑后的真实内容',rows[0]['body']);self.assertTrue(rows[0]['file_id'])
    def test_undated_migrated_note_does_not_fall_back_to_stale_database(self):
        n=self.desk.store.put('note',{'title':'旧笔记','body':'旧','kind':'idea'})
        self.doc('想法与笔记/笔记.md',f'---\nlegacy_id: "{n["id"]}"\n---\n# 当前无日期')
        self.assertFalse(self.desk.growth_snapshot()['ideas'])
    def test_today_only_adopted_due_actions_and_not_life_panorama(self):
        for status,title in [('active','采用目标'),('draft','候选目标')]:
            self.desk.store.put('goal',{'title':title,'status':status,'steps':[{'text':'当天行动','due':self.today,'done':False},{'text':'无日期','done':False},{'text':'未来','due':'2099-01-01','done':False}]})
        s=self.desk.growth_snapshot()
        self.assertEqual(len(s['today']['actions']),1)
        self.assertEqual(s['today']['actions'][0]['goal_title'],'采用目标')
        self.assertEqual(len(s['today']['unscheduled']),1)
        self.assertEqual(s['panorama']['north_star']['quote'],'合成长期愿望')
        self.assertNotIn('goals',s['panorama'])
    def test_goal_body_overrides_stale_metadata_completion(self):
        goal=self.desk.store.put('goal',{'title':'目标','status':'active','steps':[{'text':'原行动','due':self.today,'done':False}]})
        text=render('goal',goal['id'],goal,goal['rev']).replace('- [ ] 原行动','- [x] 修改后的行动')
        self.doc('目标与行动/目标.md',text)
        s=self.desk.growth_snapshot();self.assertEqual(s['goals'][0]['steps'][0]['text'],'修改后的行动')
        self.assertTrue(s['goals'][0]['steps'][0]['done']);self.assertFalse(s['today']['actions'])
    def test_new_review_and_goal_created_in_markdown(self):
        for kind,folder in [('review','阶段复盘'),('goal','目标与行动')]:
            self.doc(folder+'/新.md',f'---\nrecord_kind: "{kind}"\ncreated_at: "{self.today}"\nstatus: "draft"\nreview_period: {{"start":"{self.today}","end":"{self.today}"}}\n---\n# 新记录\n- [ ] 新行动')
        s=self.desk.growth_snapshot();self.assertEqual(len(s['reviews']),1);self.assertEqual(len(s['goals']),1)
        self.assertEqual(s['goals'][0]['status'],'draft');self.assertFalse(s['today']['actions'])
    def test_context_uses_purpose_and_bounded_scope(self):
        self.desk.store.put('workspace-message',{'role':'user','text':'新问题'})
        context=self.desk.growth_context('review',7)
        self.assertEqual(context['period']['days'],7);self.assertEqual(context['ideas'][0]['body'],'新问题')
        general=self.desk.growth_context('general',7)
        self.assertNotIn('ideas',general);self.assertNotIn('panorama',general)
        with self.assertRaises(ValueError):self.desk.growth_context('invalid',7)
        with self.assertRaises(ValueError):self.desk.growth_snapshot(True)
    def test_excluded_or_symlink_docs_not_included(self):
        out=self.root/'outside.md';out.write_text('private')
        (self.vault/'link.md').symlink_to(out)
        self.assertFalse(self.desk.growth_snapshot()['ideas'])
    def test_frontmatter_is_not_executable_yaml(self):
        data,body=metadata('---\ntitle: !!python/object:fake\ncreated_at: "2026-01-01"\n---\n正文')
        self.assertEqual(body,'正文');self.assertEqual(data['title'],'!!python/object:fake')

    def test_removing_step_never_inherits_another_steps_due_date(self):
        g=self.desk.store.put('goal',{'title':'目标','status':'active','steps':[{'text':'删掉的行动','due':self.today,'done':False},{'text':'保留但未排期','done':False}]})
        text=render('goal',g['id'],g,g['rev'])
        text=text.replace('- [ ] 删掉的行动\n  - 日期：'+self.today+'\n','')
        self.doc('目标与行动/目标.md',text)
        snapshot=self.desk.growth_snapshot()
        self.assertFalse(snapshot['today']['actions'])
        self.assertEqual(snapshot['goals'][0]['steps'][0]['due'],'')
    def test_deleted_migrated_file_never_resurrects_legacy_record_after_restart(self):
        g=self.desk.store.put('goal',{'title':'目标','status':'active','steps':[{'text':'动作','due':self.today,'done':False}]})
        path=self.doc('目标与行动/目标.md',render('goal',g['id'],g,g['rev']))
        self.assertEqual(len(self.desk.growth_snapshot()['goals']),1)
        path.unlink()
        self.desk=Desk(self.data)
        self.assertFalse(self.desk.growth_snapshot()['goals'])
    def test_custom_writable_root_can_supply_growth_records(self):
        settings=json.loads((self.data/'settings.json').read_text())
        settings['vault_roots'][0]['id']='my-personal-vault'
        (self.data/'settings.json').write_text(json.dumps(settings));self.desk=Desk(self.data)
        self.doc('想法与笔记/近期.md',f'---\nrecord_kind: "idea"\ncreated_at: "{self.today}"\n---\n# 自定义根想法')
        rows=self.desk.growth_snapshot()['ideas']
        self.assertEqual(len(rows),1);self.assertTrue(rows[0]['file_id'].startswith('my-personal-vault:'))
    def test_proposals_are_separate_from_active_goals_and_today(self):
        p=json.loads(self.plan.read_text());p['domains']=[{'id':'x','title':'长期学习','status':'proposal','actions':[{'title':'候选练习','due':self.today}]}];self.plan.write_text(json.dumps(p))
        data=self.desk.growth_snapshot()
        self.assertEqual(len(data['goal_proposals']),1);self.assertFalse(data['today']['actions'])

    def test_unadopted_step_is_not_today_even_if_parent_is_active(self):
        g=self.desk.store.put('goal',{'title':'目标','status':'active','steps':[{'text':'候选行动','due':self.today,'status':'draft'}]})
        self.doc('目标与行动/目标.md',render('goal',g['id'],g,g['rev']))
        self.assertFalse(self.desk.growth_snapshot()['today']['actions'])
    def test_migrated_captured_historical_utterance_retains_occurrence_date(self):
        n=self.desk.store.put('note',{'title':'今天整理旧想法','body':'历史原话','kind':'idea'})
        self.desk.store.put('capture',{'request_id':'dated-source','payload':{'source_time':'2020-01-01T00:00:00+08:00','source':'desktop_codex','source_text':'历史原话'},'saved_items':[{'kind':'note','id':n['id']}],'status':'saved'})
        self.doc('想法与笔记/旧想法.md',render('note',n['id'],n,n['rev']))
        self.assertFalse(self.desk.growth_snapshot()['ideas'])
