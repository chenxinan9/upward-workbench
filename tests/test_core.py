"""Isolated regression tests. All records and model events are synthetic."""
import concurrent.futures, http.client, json, sys, tempfile, threading, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app

class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name);self.d=app.Desk(self.path/'data')
    def tearDown(self):self.tmp.cleanup()
    def test_note_idempotency_and_versions(self):
        n=self.d.note({'body':'第一行\n第二行','request_id':'once'})
        self.assertEqual(n['id'],self.d.note({'body':'第一行\n第二行','request_id':'once'})['id'])
        v=self.d.note(dict(n,body='新版'))
        with self.assertRaises(ValueError):self.d.note(dict(n,body='过期请求'))
        self.assertEqual(self.d.read('note:'+n['id'])['text'],'新版')
        self.assertIn('新版',(self.d.store.path/'knowledge'/(n['id']+'.md')).read_text())
        with self.d.store.conn() as c:self.assertEqual(c.execute('select count(*) from versions where id=?',(n['id'],)).fetchone()[0],2)
    def test_concurrent_duplicate(self):
        with concurrent.futures.ThreadPoolExecutor(8) as e:items=list(e.map(lambda _:self.d.note({'body':'唯一记录','request_id':'parallel'}),range(12)))
        self.assertEqual(len({x['id'] for x in items}),1)
    def test_archive_preserves_and_recovers(self):
        n=self.d.note({'body':'归档哨兵'});self.d.store.change(n['id'],'note',archived=True)
        self.assertIn('已归档',self.d.read('note:'+n['id'])['kind'])
        self.d.store.change(n['id'],'note',archived=False)
        self.assertEqual(self.d.read('note:'+n['id'])['text'],'归档哨兵')
    def test_goal_evidence_cycle_and_unique_steps(self):
        with self.assertRaises(ValueError):self.d.goal({'title':'测试','status':'done'})
        a=self.d.goal({'title':'长期'});b=self.d.goal({'title':'近期','parent_id':a['id']})
        with self.assertRaises(ValueError):self.d.goal(dict(a,parent_id=b['id']))
        step={'text':'一步','id':'a'*32}
        with self.assertRaises(ValueError):self.d.goal({'title':'重复','steps':[dict(step),dict(step)]})
        with self.assertRaises(ValueError):self.d.goal({'title':'恶意','steps':[{'text':'一步','id':'\" onclick=bad'}]})
    def test_advisor_symlink_refused(self):
        folder=self.path/'skill';folder.mkdir();outside=self.path/'outside';outside.write_text('private sentinel');(folder/'SKILL.md').symlink_to(outside)
        catalog=self.path/'catalog.json';catalog.write_text(json.dumps({'entries':[{'name':'demo','title':'示例','status':'installed_verified','installed_path':str(folder),'url':'https://example.org'}]}));self.d.settings['catalog']=str(catalog)
        with self.assertRaises(ValueError):self.d.advisor('demo')
        (folder/'SKILL.md').unlink();(folder/'SKILL.md').write_text('可读正文');self.assertEqual(self.d.advisor('demo')['text'],'可读正文')
    def test_knowledge_directory_symlink_refused(self):
        out=self.path/'outside';out.mkdir();(self.d.store.path/'knowledge').symlink_to(out)
        with self.assertRaises(ValueError):self.d.note({'body':'不要写到外面'})
        self.assertEqual(list(out.iterdir()),[])
    def test_restart_interrupted(self):
        j=self.d.store.put('job',{'status':'cancelling'});d=app.Desk(self.d.store.path)
        self.assertEqual(d.store.get(j['id'])['status'],'interrupted')
    def test_preview_explicit_context(self):
        n=self.d.note({'body':'仅选中的资料'});self.d.note({'body':'未选中私人正文'})
        p=self.d.preview({'message':'测试问题','refs':['note:'+n['id']]})
        self.assertIn('仅选中的资料',p['prompt']);self.assertNotIn('未选中私人正文',p['prompt'])
        self.assertEqual(p['history'],[])
    def test_failed_index_keeps_note(self):
        n=self.d.note({'body':'故障保留'});self.d.store.change(n['id'],'note',index_generation='test')
        self.d._index(n['id'],'test')
        self.assertEqual(self.d.store.get(n['id'])['index_state'],'failed');self.assertEqual(self.d.read('note:'+n['id'])['text'],'故障保留')
    def test_cross_site_and_static_whitelist(self):
        server=app.serve(self.path/'http',0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            port=server.server_port
            def req(method,path,headers={},body=None):
                c=http.client.HTTPConnection('127.0.0.1',port);c.request(method,path,body=body,headers=headers);r=c.getresponse();v=(r.status,r.read());c.close();return v
            self.assertEqual(req('GET','/api/state',{'Origin':'https://evil.example'})[0],403)
            self.assertEqual(req('POST','/api/notes',{'Content-Type':'application/json'},'{}')[0],403)
            self.assertEqual(req('GET','/../app.py')[0],404)
            status,raw=req('GET','/api/state');self.assertEqual(status,200);token=json.loads(raw)['token']
            self.assertEqual(req('POST','/api/notes',{'Content-Type':'application/json','X-Desk-Token':token},json.dumps({'body':'本机合法'}))[0],200)
        finally:server.shutdown();server.server_close();thread.join()
if __name__=='__main__':unittest.main()
