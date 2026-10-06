import importlib.util,json,sqlite3,tempfile,unittest,zipfile
from pathlib import Path
MODULE=Path(__file__).resolve().parents[1]/'skills/upward-advisor/scripts/advisor.py'
spec=importlib.util.spec_from_file_location('advisor',MODULE);a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)

class AdvisorTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.profile=self.root/'profile';self.profile.mkdir()
  self.cfg={'database':str(self.root/'index.sqlite'),'collections':[{'id':'profile','path':str(self.profile),'evidence':'synthetic_fixture','status':'check_as_of_date','exclude':['excluded/*']} ]}
 def tearDown(self):self.tmp.cleanup()
 def build(self):return a.build(self.cfg)
 def test_chinese_two_char_and_literal_wildcard(self):
  (self.profile/'a.md').write_text('合成示例：职业选择需要核实。增长为10%，预算20_单位。')
  self.build();self.assertEqual(len(a.local_search(self.cfg,['职业'],5)),1)
  self.assertEqual(len(a.local_search(self.cfg,['10%'],5)),1)
  self.assertEqual(len(a.local_search(self.cfg,['99%'],5)),0)
 def test_trigram_and_casefold(self):
  (self.profile/'a.md').write_text('学习方法与 Wealth judgment')
  self.build();self.assertEqual(len(a.local_search(self.cfg,['学习方法'],5)),1)
  self.assertEqual(len(a.local_search(self.cfg,['wealth'],5)),1)
 def test_symlinks_hidden_and_excluded(self):
  outside=self.root/'outside.txt';outside.write_text('PRIVATE CANARY')
  (self.profile/'leak.txt').symlink_to(outside)
  (self.profile/'escape').symlink_to(self.root,target_is_directory=True)
  (self.profile/'.hidden.md').write_text('PRIVATE CANARY')
  (self.profile/'excluded').mkdir();(self.profile/'excluded/a.txt').write_text('PRIVATE CANARY')
  (self.profile/'safe.md').write_text('安全测试')
  self.build();self.assertEqual(a.local_search(self.cfg,['canary'],5),[])
 def test_duplicate_and_metadata(self):
  for name in ['a.md','b.md']:(self.profile/name).write_text('合成档案：身体记录日期过期')
  report=self.build();self.assertEqual(report['collections'][0]['duplicates_skipped'],1)
  r=a.local_search(self.cfg,['身体'],5)[0];self.assertEqual(r['evidence'],'synthetic_fixture');self.assertEqual(r['status'],'check_as_of_date')
 def test_pdf_page_boundaries(self):
  (self.profile/'pages.txt').write_text('第一页内容\f第二页独特内容')
  self.build();r=a.local_search(self.cfg,['第二页'],5)[0];self.assertEqual(r['page'],2)
 def test_docx(self):
  with zipfile.ZipFile(self.profile/'file.docx','w') as z:z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>合成履历</w:t></w:r></w:p></w:body></w:document>')
  self.build();self.assertTrue(a.local_search(self.cfg,['履历'],5))
 def test_missing_collection_reported(self):
  self.cfg['collections'][0]['path']=str(self.root/'missing');self.assertTrue(self.build()['errors'])
 def test_failed_rebuild_preserves_previous_index(self):
  (self.profile/'a.md').write_text('保留旧索引');self.build()
  self.cfg['collections'].append({'id':'missing','path':str(self.root/'missing')})
  self.assertTrue(self.build()['previous_index_preserved'])
  self.assertTrue(a.local_search(self.cfg,['保留'],5))
 def test_rebuild_removes_deleted_source(self):
  p=self.profile/'a.md';p.write_text('一次性内容');self.build();p.unlink();self.build();self.assertEqual(a.local_search(self.cfg,['一次性'],5),[])
 def test_configuration_relative_to_file(self):
  f=self.root/'config.json';f.write_text(json.dumps({'database':'index.sqlite','collections':[{'id':'p','path':'profile'}]}))
  self.assertEqual(a.load_config(f)['collections'][0]['path'],str(self.profile.resolve()))
 def test_tianya_citation_and_full_page(self):
  path=self.root/'tianya.sqlite';db=sqlite3.connect(path)
  db.executescript('CREATE TABLE docs(id TEXT,title TEXT,topic TEXT,path TEXT,repo TEXT);CREATE TABLE pages(doc_id TEXT,page INTEGER,text TEXT);')
  db.execute('INSERT INTO docs VALUES (?,?,?,?,?)',('TY-TEST','合成帖子','职业','fixture.txt','synthetic'))
  db.execute('INSERT INTO pages VALUES (?,?,?)',('TY-TEST',7,'职业选择：合成测试，不是真实故事。'));db.commit();db.close();self.cfg['tianya_db']=str(path)
  r=a.tianya_search(self.cfg,['职业'],2)[0];self.assertEqual(r['page'],7);self.assertEqual(r['status'],'historical_unverified')
  self.assertIn('合成测试',a.tianya_search(self.cfg,[],1,doc='TY-TEST',page=7)[0]['text'])

if __name__=='__main__':unittest.main()
