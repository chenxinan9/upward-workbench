"""Synthetic reply artifacts: provenance, bounded storage and inert previews."""
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from app import Store
from vault import VaultWorkspace
from workspace_artifacts import WorkspaceArtifacts, MAX_BYTES, static_preview


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.store=Store(self.root/'data');self.artifacts=WorkspaceArtifacts(self.store)
        self.session=self.store.put('workspace-session',{'title':'合成会话'})
        self.job=self.store.put('workspace-job',{'session_id':self.session['id'],'status':'running'})

    def create(self, title='合成文档', format='markdown', content='# 已生成\n仅合成正文'):
        return self.artifacts.create(self.job['id'],self.session['id'],title,format,content)

    def file(self, value):
        from workspace_artifacts import FORMATS
        return self.artifacts.root/self.session['id']/self.job['id']/(value['id']+'.'+FORMATS[value['format']])

    def test_real_file_hash_journal_private_modes_and_identical_request_reuse(self):
        value=self.create();again=self.create()
        self.assertEqual(value,again)
        self.assertEqual(len(self.artifacts.list(self.job['id'],self.session['id'])),1)
        file=self.file(value)
        self.assertEqual(stat.S_IMODE(file.stat().st_mode),0o600)
        for folder in (file.parent,file.parent.parent,self.artifacts.root):
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode),0o700)
        result=self.artifacts.read(self.job['id'],self.session['id'],value['id'])
        self.assertEqual(result['text'],'# 已生成\n仅合成正文')
        self.assertEqual(result['sha'],hashlib.sha256(file.read_bytes()).hexdigest())
        self.assertEqual(self.store.get(value['id'],'workspace-artifact')['status'],'verified')
        self.assertNotIn('path',value)

    def test_file_count_and_utf8_byte_limit_are_enforced_before_new_writes(self):
        for number in range(4):self.create(title=str(number))
        with self.assertRaisesRegex(ValueError,'最多4份'):self.create(title='第五份')
        self.assertEqual(len(list(self.artifacts.root.rglob('*.md'))),4)
        for content in ('x'*(MAX_BYTES+1),'字'*(MAX_BYTES//3+1)):
            with self.assertRaisesRegex(ValueError,'256KiB'):self.create(content=content)

    def test_invalid_format_title_json_and_ids_never_create_files(self):
        cases=[dict(format='exe'),dict(format=[]),dict(title=''),dict(title='换\n行'),
               dict(format='json',content='NaN'),dict(format='json',content='{"x":1e99999}'),
               dict(format='json',content='{bad'),dict(content='\x00binary')]
        for values in cases:
            with self.subTest(values=values),self.assertRaises((ValueError,TypeError)):
                self.create(**values)
        for identifier in ('../outside','a/b',None):
            with self.assertRaises(ValueError):
                self.artifacts.create(identifier,self.session['id'],'x','text','x')
        self.assertFalse(self.artifacts.root.exists())
        self.assertEqual(self.store.all('workspace-artifact'),[])

    def test_stopped_or_foreign_job_cannot_create_and_other_job_cannot_read(self):
        other=self.store.put('workspace-job',{'session_id':self.session['id'],'status':'running'})
        value=self.create()
        with self.assertRaises(ValueError):self.artifacts.read(other['id'],self.session['id'],value['id'])
        self.store.change(self.job['id'],'workspace-job',status='cancelled')
        with self.assertRaisesRegex(ValueError,'已结束'):self.create(title='不应写入')
        other_session=self.store.put('workspace-session',{'title':'另一个合成会话'})
        with self.assertRaises(ValueError):
            self.artifacts.read(self.job['id'],other_session['id'],value['id'])

    def test_tampered_or_symlink_file_and_symlink_root_fail_closed(self):
        value=self.create();file=self.file(value)
        file.write_text('被改动')
        with self.assertRaisesRegex(ValueError,'已有变化'):
            self.artifacts.read(self.job['id'],self.session['id'],value['id'])
        outside=self.root/'outside.txt';outside.write_text('PRIVATE_SENTINEL')
        file.unlink();file.symlink_to(outside)
        with self.assertRaises((OSError,ValueError)):
            self.artifacts.read(self.job['id'],self.session['id'],value['id'])
        fresh=self.root/'fresh';fresh.mkdir();fresh_store=Store(fresh/'data')
        session=fresh_store.put('workspace-session',{'title':'新会话'})
        job=fresh_store.put('workspace-job',{'session_id':session['id'],'status':'running'})
        artifacts=WorkspaceArtifacts(fresh_store);artifacts.root.symlink_to(self.root,target_is_directory=True)
        before={p for p in self.root.glob('*.md')}
        with self.assertRaises(ValueError):artifacts.create(job['id'],session['id'],'x','markdown','x')
        self.assertEqual({p for p in self.root.glob('*.md')},before)
        self.assertEqual(outside.read_text(),'PRIVATE_SENTINEL')

    def test_failed_readback_keeps_uncertain_journal_and_never_reports_verified(self):
        with patch.object(VaultWorkspace,'_bytes_at',return_value=b'wrong'):
            with self.assertRaisesRegex(ValueError,'回读不一致'):self.create()
        rows=self.artifacts.list(self.job['id'],self.session['id'])
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['status'],'uncertain')
        with self.assertRaisesRegex(ValueError,'尚未核验'):
            self.artifacts.read(self.job['id'],self.session['id'],rows[0]['id'])

    def test_static_preview_drops_active_html_navigation_network_and_css_resource_urls(self):
        content='''<h1>保留标题</h1><script>fetch('https://evil.example')</script>
        <meta http-equiv="refresh" content="0;url=https://evil.example"><base href="https://evil.example">
        <link rel="stylesheet" href="https://evil.example"><iframe src="https://evil.example"></iframe>
        <form action="https://evil.example"><input name="x"></form><svg onload="alert(1)"></svg>
        <a href="javascript:alert(1)" target="_top">静态文字</a><img src="https://evil.example" onerror="alert(1)">
        <style>@import 'https://evil.example';body{background:url(https://evil.example)}</style>
        <p style="background:u/**/rl(https://evil.example)" onclick="alert(1)">正文</p>'''
        preview=static_preview(content)
        for bad in ('<script','<iframe','<form','<svg','onerror','onclick','javascript:','evil.example','target=','href='):
            self.assertNotIn(bad,preview.lower())
        self.assertIn('保留标题',preview);self.assertIn('静态文字',preview)
        self.assertIn('Content-Security-Policy',preview)
        self.assertIn('default-src',preview);self.assertIn('form-action',preview)

    def test_static_preview_removes_all_image_sources_including_data_raster_and_svg(self):
        source='<img src="data:image/png;base64,aGVsbG8="><img src="https://evil.example/x.png"><img src="data:image/svg+xml;base64,PHN2Zy8+">'
        preview=static_preview(source)
        self.assertNotIn('<img',preview)
        self.assertNotIn('base64',preview)
        self.assertNotIn('evil.example',preview)
        self.assertIn("img-src &#x27;none&#x27;",preview)

    def test_original_html_remains_exact_source_while_only_derived_preview_is_stripped(self):
        source='<script>unsafe()</script><p>安全静态正文</p>'
        value=self.create(format='html',content=source)
        read=self.artifacts.read(self.job['id'],self.session['id'],value['id'])
        self.assertEqual(read['text'],source)
        self.assertNotIn('unsafe()',read['preview_html'])
        self.assertIn('安全静态正文',read['preview_html'])


if __name__=='__main__':unittest.main()
