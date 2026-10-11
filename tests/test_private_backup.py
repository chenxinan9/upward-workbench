from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile
from tools.backup import backup, restore

class PrivateBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.data=self.root/'data';self.data.mkdir()
        self.vault=self.root/'vault';self.vault.mkdir();(self.vault/'note.md').write_text('# 用户正文\n重要内容')
        with closing(sqlite3.connect(self.data/'desk.sqlite')) as db, db: db.execute('CREATE TABLE objects(id TEXT,kind TEXT,data TEXT,rev INTEGER)')
        (self.data/'knowledge').mkdir();(self.data/'knowledge/local.md').write_text('本机笔记')
        (self.data/'settings.json').write_text(json.dumps({'vault_roots':[
            {'id':'upward','path':str(self.vault),'writable':True,'exclude':['hidden.md']},
            {'id':'local','path':str(self.data/'knowledge'),'writable':True}],
            'local_access':{'enabled':True,'write_roots':[str(self.vault)]},'advisor_script':'untrusted-old-connection'}))
        self.archive=self.root/'private.zip'
    def tearDown(self): self.tmp.cleanup()
    def test_default_backup_declares_external_omission_and_restores_internal_roots(self):
        receipt=backup(self.data,self.archive)
        self.assertEqual(receipt['external_vaults_not_included'],['upward'])
        dest=self.root/'restored';restore(dest,self.archive)
        config=json.loads((dest/'settings.json').read_text())
        self.assertEqual(config['vault_roots'][0]['path'],str(dest/'knowledge'))
        self.assertFalse(config['local_access']['enabled']);self.assertNotIn('advisor_script',config)
        self.assertEqual((dest/'knowledge/local.md').read_text(),'本机笔记')
    def test_shared_text_is_restored_to_new_copy_and_excluded_data_stays_out(self):
        (self.vault/'hidden.md').write_text('EXCLUDED');(self.vault/'asset.bin').write_bytes(b'BINARY')
        receipt=backup(self.data,self.archive,['upward'])
        self.assertEqual(receipt['included_vaults'][0]['files'],1)
        dest=self.root/'restored';restore(dest,self.archive)
        config=json.loads((dest/'settings.json').read_text());root=next(r for r in config['vault_roots'] if r['id']=='upward')
        self.assertEqual(root['path'],str(dest/'shared-vaults/upward'))
        self.assertEqual((Path(root['path'])/'note.md').read_text(),(self.vault/'note.md').read_text())
        self.assertFalse((Path(root['path'])/'hidden.md').exists())
        self.assertEqual((self.vault/'hidden.md').read_text(),'EXCLUDED')
    def test_restored_vault_preserves_exclusions_and_empty_roots(self):
        (self.vault/'note.md').unlink()
        backup(self.data,self.archive,['upward']);dest=self.root/'restored'
        receipt=restore(dest,self.archive)
        config=json.loads((dest/'settings.json').read_text());root=next(r for r in config['vault_roots'] if r['id']=='upward')
        self.assertEqual(root['exclude'],['hidden.md']);self.assertTrue(Path(root['path']).is_dir())
        self.assertEqual(receipt['shared_vaults'],1)

    def test_restore_rejects_lexically_internal_traversal_root(self):
        backup(self.data,self.archive)
        with zipfile.ZipFile(self.archive) as z:entries={i.filename:z.read(i) for i in z.infolist()}
        config=json.loads(entries['settings.json']);config['vault_roots']=[{'id':'escape','path':str(self.data)+'/../..','writable':True}]
        entries['settings.json']=json.dumps(config).encode();malicious=self.root/'crafted.zip'
        with zipfile.ZipFile(malicious,'w') as z:
            for name,raw in entries.items():z.writestr(name,raw)
        dest=self.root/'restored'
        with self.assertRaisesRegex(ValueError,'越界'):restore(dest,malicious)
        self.assertFalse(dest.exists())

    def test_refuses_overwrite_and_missing_database_without_creating_one(self):
        backup(self.data,self.archive)
        with self.assertRaises(ValueError):backup(self.data,self.archive)
        with self.assertRaises(ValueError):restore(self.data,self.archive)
        empty=self.root/'empty';empty.mkdir()
        with self.assertRaises(ValueError):backup(empty,self.root/'empty.zip')
        self.assertFalse((empty/'desk.sqlite').exists())
    def test_bad_archives_do_not_leave_partial_restores(self):
        for name in ['../escape','/absolute','a\\b','C:/file']:
            p=self.root/'malicious.zip'
            with zipfile.ZipFile(p,'w') as z:z.writestr('desk.sqlite',b'not-a-db');z.writestr(name,'bad')
            target=self.root/'restore'
            with self.assertRaises((ValueError,sqlite3.Error)):restore(target,p)
            self.assertFalse(target.exists())
    def test_symlink_and_size_limit_leave_no_archive(self):
        (self.data/'link').symlink_to(self.vault/'note.md')
        with self.assertRaises(ValueError):backup(self.data,self.archive)
        self.assertFalse(self.archive.exists());(self.data/'link').unlink()
        with self.assertRaises(ValueError):backup(self.data,self.archive,max_bytes=1)
        self.assertFalse(self.archive.exists())
    def test_archive_cannot_live_inside_selected_vault(self):
        with self.assertRaises(ValueError):backup(self.data,self.vault/'backup.zip',['upward'])
        self.assertFalse((self.vault/'backup.zip').exists())

if __name__=='__main__':unittest.main()
