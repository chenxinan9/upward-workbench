import hashlib
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.migrate_workspace import migrate


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        self.source=self.root/'old';self.source.mkdir();self.target=self.root/'vault'
        self.db=self.source/'desk.sqlite'
        with closing(sqlite3.connect(self.db)) as c,c:c.execute('CREATE TABLE objects (id TEXT PRIMARY KEY,kind TEXT,data TEXT,rev INTEGER)')
    def tearDown(self):self.tmp.cleanup()
    def put(self,kind,ident,data,rev=1):
        with closing(sqlite3.connect(self.db)) as c,c:c.execute('INSERT OR REPLACE INTO objects VALUES(?,?,?,?)',(ident,kind,json.dumps(data,ensure_ascii=False),rev))
    def test_notes_learning_and_goal_retain_state_and_uuid(self):
        self.put('note','note1',{'title':'我的想法','body':'原始想法内容','kind':'idea'})
        self.put('learning','learn1',{'advisor':'naval','file_id':'naval/ref.md','status':'reading','position':40,'insight':'实际心得'})
        self.put('goal','goal1',{'title':'做自媒体','why':'长期积累','status':'paused','due':'2026-12-31','steps':[{'id':'step1','text':'拍摄一期','done':False,'status':'draft','criterion':'公开验收'}]})
        receipt=migrate(self.source,self.target)
        self.assertEqual(receipt['counts']['created'],4)
        text='\n'.join(p.read_text() for p in self.target.rglob('*.md'))
        for value in ['原始想法内容','实际心得','legacy_id: "goal1"','legacy_status: "paused"','"status": "draft"','长期积累','公开验收']:
            self.assertIn(value,text)
    def test_goal_body_readable_but_properties_keep_complete_evidence(self):
        self.put('goal','g',{'title':'完成项目','status':'active','steps':[{'id':'unique-action-id','text':'录制演示','status':'draft','done':True,'capture':{'explicit':False,'source_quote':'候选想法'}}]})
        migrate(self.source,self.target)
        text=next((self.target/'目标与行动').glob('*.md')).read_text()
        properties,body=text.split('---\n\n',1)
        self.assertIn('unique-action-id',properties);self.assertIn('候选想法',properties)
        self.assertNotIn('unique-action-id',body);self.assertNotIn('原完成标记',body)
        self.assertIn('状态：**进行中**',body);self.assertIn('- [ ] 录制演示（候选，尚未采纳）',body)

    def test_rerun_is_idempotent_without_changes(self):
        self.put('note','a',{'title':'笔记','body':'x'})
        first=migrate(self.source,self.target);second=migrate(self.source,self.target)
        self.assertEqual(second['counts'],{'created':0,'skipped':2,'conflicts':0,'errors':0})
    def test_user_edit_conflict_is_not_overwritten(self):
        self.put('note','a',{'title':'笔记','body':'x'});migrate(self.source,self.target)
        path=next((self.target/'想法与笔记').glob('*.md'));path.write_text('用户的新修改')
        result=migrate(self.source,self.target)
        self.assertTrue(result['conflicts']);self.assertEqual(path.read_text(),'用户的新修改')
    def test_source_title_change_does_not_create_duplicate(self):
        self.put('note','a',{'title':'旧标题','body':'x'});migrate(self.source,self.target)
        self.put('note','a',{'title':'新标题','body':'x'},2)
        result=migrate(self.source,self.target)
        self.assertEqual(len(list((self.target/'想法与笔记').glob('*.md'))),1)
        self.assertTrue(any(x['source_id']=='note:a' for x in result['conflicts']))
    def test_dry_run_creates_nothing_and_does_not_modify_db(self):
        self.put('note','a',{'title':'标题','body':'x'})
        before=self.db.read_bytes();result=migrate(self.source,self.target,dry_run=True)
        self.assertFalse(self.target.exists());self.assertEqual(self.db.read_bytes(),before)
        self.assertEqual(result['counts']['created'],2)
    def test_private_execution_and_message_payloads_excluded(self):
        self.put('job','job1',{'prompt':'PRIVATE_PROMPT','response':'PRIVATE_RESPONSE'})
        self.put('message','message1',{'session_id':'s1','text':'PRIVATE_MESSAGE'})
        self.put('session','s1',{'title':'历史讨论','created_at':'2026-10-01'})
        runs=self.source/'runs';runs.mkdir();(runs/'secret.log').write_text('PRIVATE_LOG')
        migrate(self.source,self.target)
        text='\n'.join(p.read_text() for p in self.target.rglob('*') if p.is_file())
        self.assertNotIn('PRIVATE_',text);self.assertNotIn('历史讨论',text)
        self.assertFalse((self.target/'旧会话索引.md').exists())
    def test_optional_session_index_has_counts_without_message_text(self):
        self.put('session','s1',{'title':'历史讨论','created_at':'2026-10-01'})
        self.put('message','m1',{'session_id':'s1','text':'PRIVATE_MESSAGE'})
        migrate(self.source,self.target,include_sessions=True)
        text=(self.target/'旧会话索引.md').read_text()
        self.assertIn('历史讨论',text);self.assertIn('消息数 1',text);self.assertNotIn('PRIVATE_MESSAGE',text)
    def test_planning_copied_without_adopting_or_deleting(self):
        folder=self.source/'planning';folder.mkdir();path=folder/'growth.md'
        original='# 人生候选方案\n\n候选：明年学习英语；未采用。\n';path.write_text(original)
        migrate(self.source,self.target)
        output=next((self.target/'人生方案').glob('*.md')).read_text()
        self.assertIn(original,output);self.assertIn('migration does not adopt proposals',output)
        self.assertEqual(path.read_text(),original)
    def test_draft_never_made_checked_by_migration(self):
        self.put('goal','g',{'title':'候选','status':'draft','steps':[{'id':'s','text':'候选行动','done':True,'status':'draft'}]})
        migrate(self.source,self.target);text=next((self.target/'目标与行动').glob('*.md')).read_text()
        self.assertIn('- [ ] 候选行动',text);self.assertIn('"done": true',text)
    def test_conflicting_home_preserved(self):
        self.target.mkdir();(self.target/'首页.md').write_text('手写首页')
        result=migrate(self.source,self.target)
        self.assertEqual((self.target/'首页.md').read_text(),'手写首页')
        self.assertEqual(result['counts']['conflicts'],1)
    def test_symlink_target_rejected(self):
        real=self.root/'real';real.mkdir();self.target.symlink_to(real,target_is_directory=True)
        with self.assertRaises(ValueError):migrate(self.source,self.target)
    def test_symlink_planning_not_followed(self):
        folder=self.source/'planning';folder.mkdir();outside=self.root/'outside.md';outside.write_text('SECRET')
        (folder/'link.md').symlink_to(outside)
        result=migrate(self.source,self.target)
        self.assertEqual(len(result['errors']),1);self.assertFalse((self.target/'人生方案').exists())
    def test_unsafe_id_has_no_path_traversal(self):
        self.put('note','../../outside',{'title':'x','body':'secret'})
        result=migrate(self.source,self.target)
        self.assertEqual(len(result['errors']),1);self.assertFalse((self.root/'outside.md').exists())
    def test_home_links_to_readable_migrated_files(self):
        self.put('note','a',{'title':'可读标题','body':'x'});migrate(self.source,self.target)
        self.assertIn('[可读标题]',(self.target/'首页.md').read_text())
        self.assertEqual(next((self.target/'想法与笔记').glob('*.md')).name,'可读标题--a.md')

if __name__=='__main__':unittest.main()
