import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vault import VaultWorkspace


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / 'vault'
        self.root.mkdir()
        self.data = self.base / 'state'
        self.settings = {'vault_roots': [{'id': 'life', 'label': '人生知识库',
                                        'path': str(self.root), 'writable': True}]}
        self.v = VaultWorkspace(self.settings, self.data)

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return self.v.file_id('life', relative)

    def test_cache_budget_stays_correct_after_changes_and_eviction(self):
        self.v._max_scan_bytes=1100
        first=self.put('a.md','# A\n'+'a'*800);second=self.put('b.md','# B\n'+'b'*800)
        self.v.read(first);self.v.read(second)
        self.assertLessEqual(self.v._cache_bytes,1100)
        self.assertEqual(self.v._cache_bytes,sum(v[0][2] for v in self.v._cache.values()))
        self.put('b.md','# B\nchanged')
        current=self.v.read(second)
        saved=self.v.save(second,'# B\nfinal',current['sha'])
        self.assertEqual(self.v.read(second)['text'],'# B\nfinal')
        self.assertEqual(self.v._cache_bytes,sum(v[0][2] for v in self.v._cache.values()))
        self.assertGreaterEqual(self.v._cache_bytes,0)

    def test_no_implicit_roots(self):
        v = VaultWorkspace({}, self.data)
        self.assertEqual(v.roots(), [])
        self.assertEqual(v.search('人生'), [])
        self.assertEqual(v.tree()['entries'], [])

    def test_explicit_root_required(self):
        for path in ['', 'relative']:
            with self.assertRaises(ValueError):
                VaultWorkspace({'vault_roots': [{'id': 'x', 'path': path}]}, self.data)

    def test_traversal_rejected(self):
        for relative in ['../outside.md', '/etc/test.md', 'a/../../x.md', 'a//b.md',
                         'a/./b.md', r'a\b.md', '.obsidian/config.md', 'auth/tokens.md',
                         'runtime/run.md', 'passwords.md', 'database.sqlite', '包.zip']:
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                self.v.create('life', relative, '秘密')
        with self.assertRaises(ValueError):
            self.v.read('life:%2E%2E/outside.md')
        self.assertFalse((self.base / 'outside.md').exists())

    def test_symlink_read_write_and_root_rejected(self):
        outside = self.base / 'outside'; outside.mkdir()
        (outside / 'secret.md').write_text('不应读取')
        (self.root / 'linked').symlink_to(outside)
        (self.root / 'shortcut.md').symlink_to(outside / 'secret.md')
        self.assertEqual(self.v.tree('life')['entries'], [])
        self.assertEqual(self.v.search('不应'), [])
        for fid in ['life:linked/secret.md', 'life:shortcut.md']:
            with self.assertRaises(ValueError): self.v.read(fid)
        with self.assertRaises(ValueError): self.v.create('life', 'linked/new.md', '不得写入')
        linkedroot = self.base / 'linkedroot'; linkedroot.symlink_to(self.root)
        v = VaultWorkspace({'vault_roots': [{'id': 'x', 'path': str(linkedroot)}]}, self.data)
        self.assertFalse(v.roots()[0]['available'])
        self.assertFalse((outside / 'new.md').exists())

    def test_external_edits_immediately_visible_in_read_search(self):
        fid = self.put('近况.md', '# 近况\n昨天测试')
        first = self.v.read(fid)
        self.assertTrue(self.v.search('昨天'))
        (self.root / '近况.md').write_text('# 近况\n今天发布', encoding='utf-8')
        second = self.v.read(fid)
        self.assertNotEqual(first['sha'], second['sha'])
        self.assertIn('今天发布', second['text'])
        self.assertTrue(self.v.search('今天发布'))
        self.assertFalse(self.v.search('昨天'))

    def test_optimistic_conflict_preserves_external_edit(self):
        fid = self.put('计划.md', '原文')
        before = self.v.read(fid)
        (self.root / '计划.md').write_text('Obsidian 中的新编辑')
        with self.assertRaisesRegex(ValueError, '外部修改'):
            self.v.save(fid, 'AI 建议', before['sha'])
        self.assertEqual((self.root / '计划.md').read_text(), 'Obsidian 中的新编辑')

    def test_save_atomic_backup_and_undo(self):
        fid = self.put('计划.md', '# 原计划\n一次实验')
        original = self.v.read(fid)
        saved = self.v.save(fid, '# 新计划\n两次实验', original['sha'])
        self.assertTrue(saved['changed'])
        backup = self.data / 'vault-history' / (saved['undo_id'] + '.md')
        self.assertEqual(backup.read_text(), original['text'])
        restored = self.v.undo(saved['undo_id'], saved['sha'])
        self.assertEqual(restored['sha'], original['sha'])
        self.assertEqual(list(self.root.glob('.upward-*')), [])

    def test_undo_preserves_original_bom_and_crlf_bytes(self):
        raw = b'\xef\xbb\xbf# Original\r\nLine two\r\n'
        (self.root / 'bom.md').write_bytes(raw)
        fid = self.v.file_id('life', 'bom.md')
        saved = self.v.save(fid, '# New', self.v.read(fid)['sha'])
        self.v.undo(saved['undo_id'], saved['sha'])
        self.assertEqual((self.root / 'bom.md').read_bytes(), raw)

    def test_undo_refuses_newer_disk_content(self):
        fid = self.put('计划.md', '第一版')
        saved = self.v.save(fid, '第二版', self.v.read(fid)['sha'])
        (self.root / '计划.md').write_text('外部第三版')
        with self.assertRaises(ValueError): self.v.undo(saved['undo_id'], saved['sha'])
        self.assertEqual((self.root / '计划.md').read_text(), '外部第三版')

    def test_create_idempotent_and_no_overwrite(self):
        one = self.v.create('life', '日记/新想法.md', '# 想法\n内容')
        two = self.v.create('life', '日记/新想法.md', '# 想法\n内容')
        self.assertTrue(one['created']); self.assertFalse(two['created'])
        self.assertEqual(one['file_id'], two['file_id'])
        with self.assertRaises(ValueError): self.v.create('life', '日记/新想法.md', '不同')
        self.assertEqual(len(list(self.root.rglob('*.md'))), 1)

    def test_search_chinese_late_section(self):
        body = '# 长篇资料\n' + ''.join(f'第 {i} 段普通背景\n' for i in range(800))
        body += '这一段记录自媒体日更的反馈闭环与受众需求。\n'
        fid = self.put('研究/长篇.md', body)
        found = self.v.search('反馈闭环 受众需求')
        self.assertEqual(found[0]['file_id'], fid)
        self.assertGreater(found[0]['line'], 800)
        self.assertIn('反馈闭环', found[0]['excerpt'])
        self.assertEqual(found[0]['sha'], hashlib.sha256(body.encode()).hexdigest())

    def test_links_backlinks_update_after_external_edit(self):
        a = self.put('项目/创作.md', '# 创作\n[[纳瓦尔]]\n[经验](../经验.md)\n[[失效]]')
        b = self.put('顾问/纳瓦尔.md', '# 纳瓦尔\n方法')
        c = self.put('经验.md', '# 经验')
        self.v.search('方法')  # explicit search builds the collection link index
        doc = self.v.read(a)
        self.assertEqual({l['file_id'] for l in doc['links'] if l['resolved']}, {b, c})
        self.assertTrue(any(l['target'] == '失效' and not l['resolved'] for l in doc['links']))
        self.assertEqual(self.v.read(b)['backlinks'][0]['file_id'], a)
        (self.root / '项目/创作.md').write_text('# 创作\n没有引用')
        self.assertEqual(self.v.read(b)['backlinks'], [])

    def test_hidden_private_and_non_markdown_omitted(self):
        for name in ['.hidden.md', '.git/internal.md', '.obsidian/workspace.md',
                     'runs/prompt.md', 'auth.md', 'node_modules/readme.md']:
            self.put(name, '不可检索标记')
        (self.root / 'binary.zip').write_bytes(b'PK')
        good = self.put('可读.md', '# 可读\n有用资料')
        self.assertEqual([x['file_id'] for x in self.v.tree('life')['entries']], [good])
        self.assertEqual(self.v.search('不可检索'), [])

    def test_readonly_and_multiple_collections(self):
        fid = self.put('知识.md', '# 方法\n学习规律')
        readonly = VaultWorkspace({'vault_roots': [{'id': 'life', 'path': str(self.root),
                                                   'writable': False}]}, self.data)
        self.assertFalse(readonly.read(fid)['writable'])
        with self.assertRaises(ValueError): readonly.save(fid, '改', readonly.read(fid)['sha'])
        with self.assertRaises(ValueError): readonly.create('life', '新.md', '改')
        root2 = self.base / 'second'; root2.mkdir(); (root2 / '研究.md').write_text('学习规律')
        settings = {'vault_roots': self.settings['vault_roots'] + [{'id': 'obsidian', 'path': str(root2)}]}
        v = VaultWorkspace(settings, self.data)
        self.assertEqual({r['root_id'] for r in v.search('学习规律')}, {'life', 'obsidian'})

    def test_deleted_files_disappear_and_new_files_appear(self):
        fid = self.put('旧.md', '可检索')
        self.assertEqual(len(self.v.search('可检索')), 1)
        (self.root / '旧.md').unlink()
        new = self.put('新.md', '可检索')
        self.assertEqual([r['file_id'] for r in self.v.search('可检索')], [new])
        with self.assertRaises(FileNotFoundError): self.v.read(fid)

    def test_txt_is_readable_searchable_and_always_readonly(self):
        fid = self.put('天涯/经验.txt', '第一段\n最后一段的人生启发')
        self.assertIn('人生启发', self.v.read(fid)['text'])
        self.assertFalse(self.v.read(fid)['writable'])
        self.assertEqual(self.v.search('人生启发')[0]['file_id'], fid)
        entry = self.v.tree('life', '天涯')['entries'][0]
        self.assertTrue(entry['read_only']); self.assertFalse(entry['writable'])
        with self.assertRaises(ValueError): self.v.save(fid, '覆盖', self.v.read(fid)['sha'])
        with self.assertRaises(ValueError): self.v.create('life', '新.txt', '不可新建')
        self.put('tokens.txt', '敏感标记')
        self.assertEqual(self.v.search('敏感标记'), [])
        with self.assertRaises(ValueError): self.v.read('life:tokens.txt')

    def test_bounded_search_continues_to_later_files(self):
        for index in range(6):
            self.put(f'{index}.md', f'# 记录 {index}\n共同关键词')
        settings = dict(self.settings, vault_scan_budget={'files': 2})
        v = VaultWorkspace(settings, self.data)
        self.assertEqual(len(v.search('共同关键词')), 2)
        self.assertFalse(v.scan_status['complete'])
        self.assertTrue(v.scan_status['has_more'])
        self.assertEqual(len(v.search('共同关键词')), 4)
        self.assertEqual(len(v.search('共同关键词')), 6)
        self.assertEqual(len(v.search('共同关键词')), 6)
        self.assertTrue(v.scan_status['complete'])
        self.assertFalse(v.scan_status['has_more'])
        self.assertEqual(v.scan_status['scanned_files'], 6)

    def test_root_scoped_search_does_not_scan_other_roots(self):
        self.put('甲.md', '共同词')
        other = self.base / 'other'; other.mkdir(); (other / '乙.md').write_text('共同词')
        settings = {'vault_roots': self.settings['vault_roots'] + [{'id': 'other', 'path': str(other)}]}
        v = VaultWorkspace(settings, self.data)
        self.assertEqual({d['root_id'] for d in v.search('共同词', root_id='life')}, {'life'})
        self.assertEqual(v.scan_status['scope'], ['life'])
        with self.assertRaises(ValueError): v.search('词', root_id='unknown')

    def test_read_reuses_body_and_relation_cache_and_limits_scope(self):
        a = self.put('甲.md', '[[乙]]')
        self.put('乙.md', '内容')
        other = self.base / 'other'; other.mkdir(); (other / '外部.md').write_text('不同集合')
        settings = {'vault_roots': self.settings['vault_roots'] + [{'id': 'other', 'path': str(other)}]}
        v = VaultWorkspace(settings, self.data)
        v.read(a)
        with patch.object(v, '_bytes', wraps=v._bytes) as spy, patch('vault.os.walk', side_effect=AssertionError('read must not walk')):
            doc = v.read(a)
        self.assertEqual(spy.call_count, 1)  # current note is always read directly
        self.assertEqual(doc['scan']['scope'], ['life'])

    def test_partial_relationship_scan_is_marked(self):
        for index in range(4): self.put(f'{index}.md', '# 文档')
        v = VaultWorkspace(dict(self.settings, vault_scan_budget={'files': 1}), self.data)
        doc = v.read(v.file_id('life', '3.md'))
        self.assertIn('文档', doc['text'])  # direct read works outside the scan budget
        self.assertFalse(doc['scan']['complete'])
        self.assertIn('尚未建立', doc['scan']['reason'])

    def test_replaced_parent_cannot_redirect_atomic_save(self):
        fid = self.put('folder/note.md', 'authorized original')
        original = self.v.read(fid)
        outside = self.base / 'outside'; outside.mkdir()
        (outside / 'note.md').write_text('must remain untouched')
        moved = self.root / 'moved'
        atomic = self.v._atomic_at
        def swap_parent(parent_fd, name, data, mode=0o600, replace=True):
            if name == 'note.md':
                (self.root / 'folder').rename(moved)
                (self.root / 'folder').symlink_to(outside)
            return atomic(parent_fd, name, data, mode, replace)
        with patch.object(self.v, '_atomic_at', side_effect=swap_parent):
            with self.assertRaises(ValueError):
                self.v.save(fid, 'authorized changed', original['sha'])
        self.assertEqual((outside / 'note.md').read_text(), 'must remain untouched')
        self.assertEqual((moved / 'note.md').read_text(), 'authorized changed')

    def test_search_revalidates_earlier_matches_between_batches(self):
        for index in range(4): self.put(f'{index}.md', '关键词')
        v = VaultWorkspace(dict(self.settings, vault_scan_budget={'files': 2}), self.data)
        v.search('关键词')
        (self.root / '0.md').write_text('已变更')
        results = v.search('关键词')
        self.assertNotIn('0.md', [r['path'] for r in results])

    def test_stat_never_walks_and_detects_external_edits(self):
        fid = self.put('近况.md', '原内容')
        with patch('vault.os.walk', side_effect=AssertionError('stat must not scan')):
            first = self.v.stat(fid)
            with patch.object(self.v, '_bytes', wraps=self.v._bytes) as spy:
                unchanged = self.v.stat(fid)
            self.assertEqual(spy.call_count, 0)
            self.assertEqual(first['sha'], unchanged['sha'])
            self.assertNotIn('text', first); self.assertNotIn('links', first)
            (self.root / '近况.md').write_text('外部新内容')
            changed = self.v.stat(fid)
            self.assertNotEqual(first['sha'], changed['sha'])

    def test_collection_exclude_rules_cover_tree_read_search_and_write(self):
        excluded = ['原件摘录-旧.md', '健康原件/报告.md', '91-运行资料快照A/run.md',
                    '归集记录/日志.md', '子目录/资料来源/来源.md', '人物/SKILL.md',
                    '运行错误日志.md', '子目录/岗位库/岗位.md', '顾问/examples/样例.md']
        for name in excluded: self.put(name, '排除哨兵')
        allowed = self.put('日记/正常.md', '可以阅读')
        rules = ['原件摘录-*', '健康原件/*', '91-运行资料快照*/*', '归集记录/*',
                 '*资料来源/*', '*SKILL.md', '*运行*日志*', '*岗位库/*', '*/examples/*']
        settings = {'vault_roots': [dict(self.settings['vault_roots'][0], exclude=rules)]}
        v = VaultWorkspace(settings, self.data)
        for name in excluded:
            with self.subTest(name=name), self.assertRaises(ValueError):
                v.read(v.file_id('life', name))
        self.assertEqual(v.search('排除哨兵'), [])
        self.assertEqual(v.search('可以阅读')[0]['file_id'], allowed)
        names = [entry['path'] for entry in v.tree('life')['entries']]
        self.assertNotIn('健康原件', names); self.assertNotIn('归集记录', names)
        with self.assertRaises(ValueError): v.tree('life', '健康原件')
        with self.assertRaises(ValueError): v.create('life', '健康原件/新.md', '不许写')
        with self.assertRaises(ValueError): v.stat(v.file_id('life', '原件摘录-旧.md'))

    def test_initialization_and_stat_never_scan_collections(self):
        with patch('vault.os.walk', side_effect=AssertionError('must not scan during startup')):
            v = VaultWorkspace(self.settings, self.data)
            self.assertTrue(v.roots()[0]['available'])

    def test_fair_scan_finds_late_notes_and_advisors_in_first_batch(self):
        # Real files, not mocked traversal: a large first root used to consume
        # every batch before the user's recent note or any advisor was visited.
        for index in range(300): self.put(f'{index:04}.md', '大库普通资料')
        roots = list(self.settings['vault_roots'])
        for index in range(55):
            advisor = self.base / f'advisor-{index}'; advisor.mkdir()
            (advisor / 'SKILL.md').write_text(f'# 顾问 {index}\n共同命中词')
            roots.append({'id': f'advisor-{index}', 'path': str(advisor)})
        notes = self.base / 'recent'; notes.mkdir()
        (notes / '刚刚新增.md').write_text('# 近期想法\n共同命中词')
        roots.append({'id': 'recent', 'path': str(notes)})
        v = VaultWorkspace({'vault_roots': roots, 'vault_scan_budget': {'files': 57}}, self.data)
        results = v.search('共同命中词', limit=100)
        self.assertEqual(len(results), 56)
        self.assertIn('recent', {r['root_id'] for r in results})
        self.assertEqual(len({r['root_id'] for r in results if r['root_id'].startswith('advisor-')}), 55)
        self.assertTrue(v.scan_status['has_more'])

    def test_round_robin_position_survives_budget_continuation(self):
        for index in range(10): self.put(f'{index}.md', '普通')
        late = self.base / 'late'; late.mkdir(); (late / 'note.md').write_text('晚集合关键词')
        roots = self.settings['vault_roots'] + [{'id': 'late', 'path': str(late)}]
        v = VaultWorkspace({'vault_roots': roots, 'vault_scan_budget': {'files': 1}}, self.data)
        self.assertEqual(v.search('晚集合关键词'), [])
        second = v.search('晚集合关键词')
        self.assertEqual([r['root_id'] for r in second], ['late'])
        self.assertEqual(v.scan_status['scanned_files'], 2)

    def test_tree_titles_from_bounded_frontmatter_and_h1_keep_filename(self):
        self.put('random-id.md', '---\ntitle: "我的人生方向"\n---\n# 次要标题\n内容')
        self.put('other-id.md', '# 最近的成长复盘\n正文')
        self.put('fallback.md', '普通文本')
        self.put('late-title.md', 'x' * 9000 + '\n# 不应读到的后半标题')
        entries = {entry['filename']: entry for entry in self.v.tree('life')['entries']}
        self.assertEqual(entries['random-id.md']['title'], '我的人生方向')
        self.assertEqual(entries['random-id.md']['path'], 'random-id.md')
        self.assertEqual(entries['other-id.md']['title'], '最近的成长复盘')
        self.assertEqual(entries['fallback.md']['title'], 'fallback')
        self.assertEqual(entries['late-title.md']['title'], 'late-title')
        self.assertEqual(self.v.read('life:random-id.md')['title'], '我的人生方向')


if __name__ == '__main__':
    unittest.main()
