"""Real resource installation and read-only retrieval over synthetic fixtures."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
from bundle_resources import advisor_resources, knowledge_resources, knowledge_roots


def load(relative, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


installer = load('advisors-bundle/install.py', 'nested_skill_installer')
builder = load('tools/build_release.py', 'nested_resource_builder')


def inventory(root, paths):
    return [{'path': p, 'bytes': len((root / p).read_bytes()),
             'sha256': hashlib.sha256((root / p).read_bytes()).hexdigest()} for p in paths]


class ResourceBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.program = self.base / 'program'; self.program.mkdir()
        self.bundle = self.program / 'advisors-bundle'; self.bundle.mkdir()
        self.skill = self.bundle / 'sample-coach'; self.skill.mkdir()
        files = {'SKILL.md': '# 合成顾问\n阅读 references/nested/source.md 与 examples/session.md',
                 'LICENSE': 'MIT synthetic fixture', 'references/nested/source.md': '# 来源\n间隔练习合成证据',
                 'examples/session.md': '# 合成演练\n不含个人资料'}
        for rel, text in files.items():
            path = self.skill / rel; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text)
        self.provenance = {'files': inventory(self.skill, files)}
        self.write_provenance()
        (self.bundle / 'manifest.json').write_text(json.dumps({'entries': [{'name': 'sample-coach', 'path': 'sample-coach'}]}))
        self.knowledge = self.program / 'knowledge-bundle'
        text = self.knowledge / 'life-guide/book/chapter.md'; text.parent.mkdir(parents=True)
        text.write_text('# 合成公开资料\n间隔练习与低成本验证')
        self.manifest = {'schema_version': 1, 'collections': [{'id': 'life-guide', 'title': '合成公开库', 'path': 'life-guide', 'license': 'CC-BY-4.0', 'source_url': 'https://example.org/synthetic'}],
                         'files': inventory(self.knowledge, ['life-guide/book/chapter.md'])}
        self.write_manifest()

    def tearDown(self): self.tmp.cleanup()
    def write_provenance(self): (self.skill / 'provenance.json').write_text(json.dumps(self.provenance))
    def write_manifest(self): (self.knowledge / 'manifest.json').write_text(json.dumps(self.manifest))

    def test_installs_registered_nested_files_with_exact_bytes_and_refuses_overwrite(self):
        with patch.object(installer, 'ROOT', self.bundle):
            dst, pairs = installer.plan(self.base / 'installed', ['sample-coach'])
            self.assertEqual(installer.copy_selected(dst, pairs), ['sample-coach'])
            for rel, data in advisor_resources(self.skill).items():
                self.assertEqual((dst / 'sample-coach' / rel).read_bytes(), data)
            marker = dst / 'sample-coach/references/nested/source.md'; before = marker.read_bytes()
            with self.assertRaises(ValueError): installer.plan(dst, ['sample-coach'])
            self.assertEqual(marker.read_bytes(), before)

    def test_checksum_tamper_prevents_installation(self):
        (self.skill / 'references/nested/source.md').write_text('modified')
        with patch.object(installer, 'ROOT', self.bundle):
            with self.assertRaisesRegex(ValueError, 'checksum'): installer.plan(self.base / 'installed', [])
        self.assertFalse((self.base / 'installed').exists())

    def test_source_changed_after_plan_is_revalidated_before_any_write(self):
        with patch.object(installer, 'ROOT', self.bundle):
            dst, pairs = installer.plan(self.base / 'installed', [])
            (self.skill / 'examples/session.md').write_text('changed')
            with self.assertRaises(ValueError): installer.copy_selected(dst, pairs)
            self.assertFalse(dst.exists())

    def test_parent_traversal_and_duplicate_provenance_are_rejected(self):
        for bad in ('../escape.md', '/escape.md', 'references/../escape.md', 'references\\escape.md'):
            with self.subTest(path=bad):
                original = self.provenance['files'][0]['path']; self.provenance['files'][0]['path'] = bad; self.write_provenance()
                with self.assertRaises(ValueError): advisor_resources(self.skill)
                self.provenance['files'][0]['path'] = original
        self.provenance['files'].append(self.provenance['files'][0]); self.write_provenance()
        with self.assertRaises(ValueError): advisor_resources(self.skill)

    def test_unregistered_file_and_symlink_are_rejected(self):
        extra = self.skill / 'references/unreviewed.md'; extra.write_text('not approved')
        with self.assertRaises(ValueError): advisor_resources(self.skill)
        extra.unlink(); extra.symlink_to(self.base / 'outside')
        with self.assertRaises(ValueError): advisor_resources(self.skill)

    def test_destination_symlink_parent_rejected_without_outside_writes(self):
        outside = self.base / 'outside'; outside.mkdir()
        link = self.base / 'link'; link.symlink_to(outside, target_is_directory=True)
        with patch.object(installer, 'ROOT', self.bundle):
            with self.assertRaises(ValueError): installer.plan(link / 'skills', [])
        self.assertEqual(list(outside.iterdir()), [])

    def test_knowledge_hash_traversal_and_overlapping_collections_rejected(self):
        self.manifest['files'][0]['sha256'] = '0' * 64; self.write_manifest()
        with self.assertRaises(ValueError): knowledge_resources(self.knowledge)
        self.manifest['files'][0]['path'] = '../escape.md'; self.write_manifest()
        with self.assertRaises(ValueError): knowledge_resources(self.knowledge)
        self.manifest['collections'].append(dict(self.manifest['collections'][0], id='other', path='life-guide/book'))
        self.write_manifest()
        with self.assertRaises(ValueError): knowledge_resources(self.knowledge)

    def test_builder_only_collects_reviewed_manifest_files(self):
        with patch.object(builder, 'ROOT', self.program), patch.object(builder, 'FILES', []):
            paths = builder.release_files()
            self.assertIn('advisors-bundle/sample-coach/references/nested/source.md', paths)
            self.assertIn('advisors-bundle/sample-coach/examples/session.md', paths)
            self.assertIn('knowledge-bundle/life-guide/book/chapter.md', paths)
            (self.knowledge / 'life-guide/unreviewed.md').write_text('unreviewed')
            with self.assertRaises(ValueError): builder.release_files()

    def test_readonly_public_vault_search_read_and_advisor_scope_exclude_personal(self):
        import app
        private = self.base / 'personal'; private.mkdir()
        (private / 'private.md').write_text('# 合成私人\n不应进入顾问资料范围')
        with patch('workspace_api.PROGRAM_ROOT', self.program):
            desk = app.Desk(self.base / 'data')
            desk.settings['vault_roots'] = [{'id':'notes', 'label':'个人', 'path':str(private), 'writable':True}]
            catalog = self.base / 'catalog.json'
            catalog.write_text(json.dumps({'entries':[{'name':'sample-coach','title':'合成顾问','category':'学习','url':'https://example.org/synthetic','status':'installed_verified','installed_path':str(self.skill)}]}))
            desk.settings['catalog'] = str(catalog)
            desk.init_workspace()
        try:
            root_id = 'public-knowledge-life-guide'
            fid = root_id + ':book/chapter.md'
            roots = desk.workspace_get('/api/vault/roots', {})['roots']
            self.assertFalse(next(r for r in roots if r['id'] == root_id)['writable'])
            hits = desk.workspace_get('/api/vault/search', {'q':'间隔练习', 'root_id':root_id})['results']
            self.assertEqual(hits[0]['id'], fid)
            doc = desk.workspace_get('/api/vault/read', {'id':fid})
            self.assertIn('低成本验证', doc['text'])
            advisor_example = 'advisor-sample-coach:examples/session.md'
            self.assertIn('合成演练', desk.workspace_get('/api/vault/read', {'id':advisor_example})['text'])
            self.assertTrue(desk.workspace_get('/api/vault/tree', {'root_id':'advisor-sample-coach','path':'references/nested'})['entries'])
            with self.assertRaises((ValueError, PermissionError)):
                desk.workspace_post('/api/vault/save', {'id':fid, 'text':'bad', 'expected_sha':doc['sha']})
            session = desk.store.put('workspace-session', {'title':'合成范围核验'})
            job = desk.store.put('workspace-job', {'session_id':session['id'], 'status':'queued', 'request_context':{'purpose':'learn', 'scope':'advisors'}})
            def run_turn(client, **kw):
                tool = kw['tool_call']
                self.assertIn('间隔练习', tool('knowledge_read', {'document_id':fid})['text'])
                self.assertTrue(tool('knowledge_search', {'query':'间隔练习', 'root_id':root_id})['results'])
                self.assertIn('合成演练', tool('knowledge_read', {'document_id':advisor_example})['text'])
                with self.assertRaises(PermissionError): tool('knowledge_read', {'document_id':'notes:private.md'})
                with self.assertRaises(PermissionError): tool('knowledge_search', {'query':'私人', 'root_id':'notes'})
                return {'text':'合成验收成功', 'status':'completed'}
            with patch('codex_workspace.CodexWorkspace.run_turn', run_turn):
                desk.workspace_run(job,session,{'id':'synthetic'},'参考公开资料',None,'',False,threading.Event())
            self.assertEqual(desk.store.get(job['id'],'workspace-job')['status'],'completed')
        finally: desk.stop_workspace()


if __name__ == '__main__': unittest.main()
