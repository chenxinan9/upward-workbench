"""Public ZIP, fresh workspace and safe upgrade. No model calls/native compiler."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import zipfile
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tools'))
def load(name,relative):
    spec=importlib.util.spec_from_file_location(name,ROOT/relative)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
setup_module=load('install_test_setup','tools/setup_knowledge.py')
installer=load('install_test_desktop','desktop/install.py')
builder=load('install_test_builder','tools/build_release.py')
class WorkspaceInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name).resolve()
    def tearDown(self):self.tmp.cleanup()
    def test_fresh_setup_registers_writable_notes_and_56_bundled_advisors(self):
        data=self.base/'data';report=setup_module.setup(data,initialize_legacy=False)
        self.assertEqual(report['bundled_skills_available'],56);self.assertIsNone(report['index'])
        from app import Desk
        desk=Desk(data);roots=desk.workspace_get('/api/vault/roots',{})['roots']
        self.assertEqual(len([r for r in roots if r['group']=='顾问团']),56)
        writable=[r for r in roots if r['writable']];self.assertEqual(len(writable),1)
        tree=desk.vault.tree(writable[0]['id']);self.assertTrue(tree['entries'])
        self.assertIn('右边',desk.vault.read(tree['entries'][0]['file_id'])['text'])
        before=(data/'settings.json').read_bytes();setup_module.setup(data,initialize_legacy=False)
        self.assertEqual((data/'settings.json').read_bytes(),before)
    def test_existing_settings_and_explicit_obsidian_registration_preserved(self):
        data=self.base/'data';data.mkdir();vault=self.base/'Obsidian';vault.mkdir();(vault/'已有.md').write_text('# 已有')
        settings={'catalog':'custom-catalog.json','advisor_config':'custom-index.json','custom':'preserve',
                  'vault_roots':[{'id':'obsidian','label':'私人库','path':str(vault),'writable':False,'exclude':['私密/*']}]}
        (data/'settings.json').write_text(json.dumps(settings))
        setup_module.setup(data,vaults=[str(vault)],initialize_legacy=False,preserve_existing=True)
        updated=json.loads((data/'settings.json').read_text())
        self.assertEqual(updated['catalog'],settings['catalog']);self.assertEqual(updated['advisor_config'],settings['advisor_config'])
        self.assertEqual(updated['vault_roots'][0],settings['vault_roots'][0]);self.assertEqual(len(updated['vault_roots']),2)
        self.assertEqual((vault/'已有.md').read_text(),'# 已有')
        self.assertEqual(len(list(data.glob('settings.before-workspace-*.json'))),1)
    def test_upgrade_existing_writable_vault_preserves_roots_without_default_notebook(self):
        data=self.base/'data';data.mkdir();vault=self.base/'Shared Obsidian';vault.mkdir()
        (vault/'已有.md').write_text('# 已有正文')
        roots=[{'id':'my-obsidian','label':'我的共享知识库','path':str(vault),'writable':True,'exclude':['隐私/*']}]
        settings={'vault_roots':roots,'custom':'preserve'}
        (data/'settings.json').write_text(json.dumps(settings));before=(data/'settings.json').read_bytes()
        report=setup_module.setup(data,initialize_legacy=False,preserve_existing=True)
        self.assertEqual(json.loads((data/'settings.json').read_text())['vault_roots'],roots)
        self.assertEqual((data/'settings.json').read_bytes(),before)
        self.assertEqual(report['registered_roots'],[])
        self.assertFalse((data/'knowledge').exists())
        self.assertFalse((vault/'开始使用.md').exists())
        self.assertEqual((vault/'已有.md').read_text(),'# 已有正文')
    def test_explicit_new_vault_defaults_readonly_and_does_not_scan(self):
        external=self.base/'外部知识库';external.mkdir();(external/'资料.md').write_text('外部正文')
        with patch('os.walk',side_effect=AssertionError('setup must not scan')):
            setup_module.setup(self.base/'data',vaults=[str(external)],initialize_legacy=False)
        settings=json.loads((self.base/'data/settings.json').read_text())
        self.assertFalse(next(r for r in settings['vault_roots'] if Path(r['path'])==external)['writable'])
    def test_built_zip_contains_workspace_and_runs_fresh_without_private_probes(self):
        archive=self.base/'public.zip';builder.build(archive)
        with zipfile.ZipFile(archive) as zipped:
            names=zipped.namelist()
            for path in ['workspace_api.py','vault.py','codex_workspace.py','web/workspace.html','web/workspace.js','web/workspace.css',
                         'tools/setup_knowledge.py','tools/migrate_workspace.py','tests/test_private_backup.py']:
                self.assertIn('growth-desk/'+path,names)
            self.assertFalse(any('real_workspace_probe' in n or 'schema-evidence' in n for n in names))
            self.assertFalse(any(n.endswith(('settings.json','.sqlite','.jsonl')) for n in names))
            zipped.extractall(self.base/'unpacked')
        source=self.base/'unpacked/growth-desk';data=self.base/'fresh-data'
        subprocess.run([sys.executable,str(source/'tools/setup_knowledge.py'),'--data-dir',str(data)],check=True,capture_output=True,text=True)
        code=('import json,sys; from app import Desk; d=Desk(sys.argv[1]); roots=d.workspace_get("/api/vault/roots",{})["roots"]; '
              'print(json.dumps({"advisors":len(d._workspace_advisors),"writable":sum(r["writable"] for r in roots),'
              '"notes":len(d.vault.tree("upward")["entries"])}))')
        result=subprocess.run([sys.executable,'-c',code,str(data)],cwd=source,check=True,capture_output=True,text=True)
        self.assertEqual(json.loads(result.stdout),{'advisors':56,'writable':1,'notes':1})
    def test_upgrade_replaces_stale_code_and_preserves_user_documents_and_settings(self):
        data=self.base/'data';support=self.base/'support';first=installer.deploy_runtime(ROOT,support,data);runtime=Path(first['runtime'])
        for path in ['vault.py','workspace_api.py','codex_workspace.py','tools/setup_knowledge.py','skills/upward-advisor/scripts/advisor.py','web/workspace.js']:
            self.assertTrue((runtime/path).is_file())
        (runtime/'obsolete.py').write_text('obsolete = True');note=data/'knowledge/我的想法.md';note.write_text('# 本人记录\n不能覆盖')
        settings_file=data/'settings.json';settings=json.loads(settings_file.read_text());settings['personal_setting']='preserve'
        settings_file.write_text(json.dumps(settings));before=settings_file.read_bytes()
        updated=installer.deploy_runtime(ROOT,support,data,upgrade=True)
        self.assertEqual(settings_file.read_bytes(),before);self.assertEqual(note.read_text(),'# 本人记录\n不能覆盖')
        self.assertFalse((runtime/'obsolete.py').exists());self.assertTrue((Path(updated['backup'])/'程序/obsolete.py').exists())
        with self.assertRaises(ValueError):installer.deploy_runtime(ROOT,support,data)
    def test_upgrade_failure_restores_previous_program_and_settings(self):
        data=self.base/'data';support=self.base/'support';installer.deploy_runtime(ROOT,support,data)
        before=(support/'desktop.json').read_bytes();settings=(data/'settings.json').read_bytes()
        (support/'程序/previous-marker.txt').write_text('keep old program');original=installer._module
        def fail_setup(name,path):
            if name=='upward_installed_setup':
                def fail(*args,**kwargs):raise ValueError('synthetic setup failure')
                return types.SimpleNamespace(setup=fail)
            return original(name,path)
        with patch.object(installer,'_module',side_effect=fail_setup):
            with self.assertRaisesRegex(ValueError,'synthetic'):installer.deploy_runtime(ROOT,support,data,upgrade=True)
        self.assertEqual((support/'desktop.json').read_bytes(),before);self.assertEqual((data/'settings.json').read_bytes(),settings)
        self.assertEqual((support/'程序/previous-marker.txt').read_text(),'keep old program')
    def test_final_app_swap_failure_rolls_back_program_entry_and_settings(self):
        data=self.base/'data';support=self.base/'Library/Application Support/向上成长顾问'
        installer.deploy_runtime(ROOT,support,data)
        (support/'程序/previous-marker.txt').write_text('old')
        settings_before=(data/'settings.json').read_bytes();desktop_before=(support/'desktop.json').read_bytes()
        app=self.base/'Desktop/向上工作台.app';(app/'Contents').mkdir(parents=True)
        (app/'Contents/Info.plist').write_bytes(installer.plistlib.dumps({'CFBundleIdentifier':installer.APP_ID}))
        (app/'old-marker').write_text('old desktop')
        real_move=installer.shutil.move
        def simulated_build(source,destination):
            destination.mkdir();(destination/'new-marker').write_text('candidate')
        def failing_move(source,destination):
            if '.向上安装-' in str(source) and str(destination)==str(app):
                raise OSError('synthetic final app swap failure')
            return real_move(source,destination)
        with patch.object(installer.Path,'home',return_value=self.base), patch.object(installer,'build_app',side_effect=simulated_build), \
             patch.object(installer.urllib.request,'urlopen',side_effect=OSError('offline')), patch.object(installer.shutil,'move',side_effect=failing_move):
            with self.assertRaisesRegex(OSError,'synthetic final'):
                installer.install(data,app,upgrade=True,source=ROOT)
        self.assertEqual((app/'old-marker').read_text(),'old desktop')
        self.assertEqual((support/'程序/previous-marker.txt').read_text(),'old')
        self.assertEqual((data/'settings.json').read_bytes(),settings_before)
        self.assertEqual((support/'desktop.json').read_bytes(),desktop_before)
    def test_renamed_desktop_upgrade_keeps_data_and_restores_old_name_on_failure(self):
        for fail in (False, True):
            with self.subTest(final_swap_failure=fail), tempfile.TemporaryDirectory() as folder:
                home=Path(folder).resolve();data=home/'data';support=home/'Library/Application Support/向上成长顾问'
                installer.deploy_runtime(ROOT,support,data)
                before=(data/'settings.json').read_bytes()
                old=home/'Desktop/向上工作台.app';(old/'Contents').mkdir(parents=True)
                (old/'Contents/Info.plist').write_bytes(installer.plistlib.dumps({'CFBundleIdentifier':installer.APP_ID}))
                (old/'old-marker').write_text('existing desktop')
                target=home/'Desktop/睿顾台.app';real_move=installer.shutil.move
                def build(source,destination):
                    destination.mkdir();(destination/'new-marker').write_text('new desktop')
                def move(source,destination):
                    if fail and '.向上安装-' in str(source) and Path(destination)==target:
                        raise OSError('synthetic rename failure')
                    return real_move(source,destination)
                with patch.object(installer.Path,'home',return_value=home), patch.object(installer,'build_app',side_effect=build), patch.object(installer.urllib.request,'urlopen',side_effect=OSError('offline')), patch.object(installer.shutil,'move',side_effect=move):
                    if fail:
                        with self.assertRaisesRegex(OSError,'rename failure'): installer.install(data,target,upgrade=True,source=ROOT)
                        self.assertEqual((old/'old-marker').read_text(),'existing desktop');self.assertFalse(target.exists())
                    else:
                        receipt=installer.install(data,target,upgrade=True,source=ROOT)
                        self.assertFalse(old.exists());self.assertTrue((target/'new-marker').is_file())
                        self.assertEqual((Path(receipt['backup'])/'向上工作台.app/old-marker').read_text(),'existing desktop')
                        self.assertEqual(json.loads((support/'desktop.json').read_text())['data'],str(data))
                self.assertEqual((data/'settings.json').read_bytes(),before)
    def test_unsafe_or_source_private_directory_is_rejected(self):
        with self.assertRaises(ValueError):setup_module.setup(ROOT/'private-data',initialize_legacy=False)
        external=self.base/'outside';external.mkdir();linked=self.base/'linked';linked.symlink_to(external)
        with self.assertRaises(ValueError):setup_module.setup(linked,initialize_legacy=False)
        with self.assertRaises(ValueError):installer.deploy_runtime(ROOT,self.base/'support',linked)
if __name__=='__main__':unittest.main()
