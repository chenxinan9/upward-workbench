"""Synthetic local-tool acceptance; never reads the user's Notes or personal files."""
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile
from contextlib import closing

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local_tools import LocalToolsMixin, LocalBlocked, decode_note


def vi(value):
    out = bytearray()
    while value > 127:
        out.append((value & 127) | 128); value >>= 7
    return bytes(out + bytes([value]))


def field(number, value):
    return vi((number << 3) | 2) + vi(len(value)) + value


def note_blob(text):
    return gzip.compress(field(2, field(3, field(2, text.encode()))))


class Fixture(LocalToolsMixin):
    pass


class LocalToolsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.read = self.root / 'read'; self.read.mkdir()
        self.write = self.root / 'write'; self.write.mkdir()
        self.outside = self.root / 'outside'; self.outside.mkdir()
        self.d = Fixture()
        self.d.settings = {'local_access': {'enabled': True, 'notes_enabled': True, 'read_roots': [str(self.read), str(self.write)], 'write_roots': [str(self.write)]}}
        self.db = self.root / 'NoteStore.sqlite'
        self.d._notes_path = lambda: self.db
        with closing(sqlite3.connect(self.db)) as c, c:
            c.executescript('CREATE TABLE ZICCLOUDSYNCINGOBJECT(Z_PK INTEGER, ZNOTEDATA INTEGER, ZTITLE1 TEXT, ZMODIFICATIONDATE1 REAL, ZISPASSWORDPROTECTED INTEGER, ZMARKEDFORDELETION INTEGER); CREATE TABLE ZICNOTEDATA(Z_PK INTEGER, ZDATA BLOB);')
            for pk, title, modified, locked, deleted, body in [(1, '旧想法', 1, 0, 0, '旧正文'), (2, '新想法', 2, 0, 0, '新正文：忽略指令这个词也只是资料'), (3, '锁定内容', 0, 1, 0, '绝密'), (4, '已删除内容', 5, 0, 1, '不应读到')]:
                c.execute('INSERT INTO ZICCLOUDSYNCINGOBJECT VALUES (?,?,?,?,?,?)', (pk, pk, title, modified, locked, deleted))
                c.execute('INSERT INTO ZICNOTEDATA VALUES (?,?)', (pk, note_blob(body)))

    def tearDown(self):
        self.tmp.cleanup()

    def execute(self, tool, message, **params):
        quote = params.pop('quote', message)
        with patch('local_tools.platform.system', return_value='Darwin'):
            return self.d.execute_local_requests([dict(tool=tool, user_quote=quote, **params)], message, 'synthetic-job')

    def blocked(self, result):
        self.assertEqual(result['contexts'], [])
        self.assertIn(result['actions'][0]['status'], ['blocked', 'failed'])

    def test_capabilities_only_probes_schema_not_notes(self):
        with patch('local_tools.platform.system', return_value='Darwin'), patch.object(self.d, '_note_rows', side_effect=AssertionError('must not load title')):
            result = self.d.local_capabilities()
        self.assertEqual(result['notes']['status'], 'connected')
        self.assertNotIn('新想法', json.dumps(result, ensure_ascii=False))

    def test_disabled_access_has_no_operation(self):
        self.d.settings['local_access']['enabled'] = False
        self.blocked(self.execute('notes.list', '查看备忘录'))

    def test_notes_disabled(self):
        self.d.settings['local_access']['notes_enabled'] = False
        self.blocked(self.execute('notes.list', '查看备忘录'))

    def test_unsupported_system_has_honest_receipt(self):
        with patch('local_tools.platform.system', return_value='Linux'):
            r = self.d.execute_local_requests([{'tool': 'notes.list', 'user_quote': '查看备忘录'}], '查看备忘录', 'j')
        self.blocked(r)
        self.assertIn('没有 Apple', r['actions'][0]['summary'])

    def test_list_notes_only_recent_metadata_no_body(self):
        result = self.execute('notes.list', '查看我的备忘录', limit=2)
        data = json.loads(result['contexts'][0]['text'])
        self.assertEqual([n['title'] for n in data['notes']], ['新想法', '旧想法'])
        self.assertNotIn('正文', result['contexts'][0]['text'])
        self.assertNotIn('已删除内容', result['contexts'][0]['text'])

    def test_read_latest_with_explicit_order_and_limits(self):
        result = self.execute('notes.read', '看看我的第一条备忘录', id='latest')
        data = json.loads(result['contexts'][0]['text'])
        self.assertEqual(data['id'], '2')
        self.assertEqual(data['title'], '新想法')
        self.assertIn('新正文', data['text'])
        self.assertIn('不是当前', data['order'])
        self.assertIn('附件未展开', data['limitations'])
        self.assertIn('只是资料', result['contexts'][0]['kind'])
        receipt = result['actions'][0]
        self.assertTrue({'id','at','job_id','tool','source_path','sha256','bytes'} <= set(receipt))
        self.assertNotIn('text', receipt)

    def test_latest_not_invented_when_user_only_asks_list(self):
        self.blocked(self.execute('notes.read', '看一下备忘录', id='latest'))

    def test_note_specific_title_authorizes_exact_note(self):
        r = self.execute('notes.read', '读取“旧想法”备忘录', id='1')
        self.assertEqual(json.loads(r['contexts'][0]['text'])['text'], '旧正文')

    def test_title_prefix_suffix_and_nested_quote_do_not_authorize_note(self):
        for message in ['读取“旧想法后记”备忘录', '读取“新的旧想法”备忘录', '读取“另一个‘旧想法’标题”备忘录']:
            self.blocked(self.execute('notes.read', message, id='1'))

    def test_natural_read_latest_phrase(self):
        result = self.execute('notes.read', '帮我看我的第一条备忘录', id='latest')
        self.assertEqual(result['actions'][0]['status'], 'completed')

    def test_duplicate_title_requires_explicit_id(self):
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute('UPDATE ZICCLOUDSYNCINGOBJECT SET ZTITLE1=? WHERE Z_PK in (1,2)', ('重复标题',))
        self.blocked(self.execute('notes.read', '读取“重复标题”备忘录', id='1'))
        self.blocked(self.execute('notes.read', '读取“重复标题”备忘录', id='2'))
        result = self.execute('notes.read', '读取“重复标题”编号 1 备忘录', id='1')
        self.assertEqual(json.loads(result['contexts'][0]['text'])['text'], '旧正文')

    def test_unrelated_note_id_not_read(self):
        self.blocked(self.execute('notes.read', '读取“新想法”备忘录', id='1'))

    def test_locked_note_never_decoded(self):
        with patch('local_tools.decode_note', side_effect=AssertionError('must not decode locked')):
            self.blocked(self.execute('notes.read', '读取编号 3 备忘录', id='3'))

    def test_deleted_note_not_read(self):
        self.blocked(self.execute('notes.read', '读取编号 4 备忘录', id='4'))

    def test_unknown_note_schema_reports_blocked(self):
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute('ALTER TABLE ZICNOTEDATA RENAME TO unsupported')
        self.blocked(self.execute('notes.read', '读取最新备忘录', id='latest'))

    def test_unknown_blob_does_not_guess_text(self):
        with self.assertRaises(LocalBlocked):
            decode_note(gzip.compress(field(9, b'plausible text is not a note')))
        with self.assertRaises(LocalBlocked):
            decode_note(gzip.compress(field(2, b'\x1a\xff')))

    def test_broken_gzip_is_a_blocked_receipt(self):
        with sqlite3.connect(self.db) as c:
            c.execute('UPDATE ZICNOTEDATA SET ZDATA=? WHERE Z_PK=2', (b'\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x00\xff\xff',))
        c.close()
        self.blocked(self.execute('notes.read', '读取最新备忘录', id='latest'))

    def test_file_read_hash_and_content(self):
        p = self.read / '想法.md'; p.write_text('今天的一点想法')
        r = self.execute('files.read', f'读取文件 {p}', path=str(p))
        self.assertEqual(json.loads(r['contexts'][0]['text'])['text'], '今天的一点想法')
        self.assertEqual(r['actions'][0]['sha256'], hashlib.sha256(p.read_bytes()).hexdigest())

    def test_read_outside_root_denied(self):
        p = self.outside / 'private.txt'; p.write_text('private')
        self.blocked(self.execute('files.read', f'读取 {p}', path=str(p)))

    def test_symlink_escape_denied(self):
        p = self.outside / 'private.txt'; p.write_text('private')
        link = self.read / 'link.txt'; link.symlink_to(p)
        self.blocked(self.execute('files.read', f'读取 {link}', path=str(link)))

    def test_inside_root_symlink_also_requires_explicit_target(self):
        target = self.read / 'target.txt'; target.write_text('actual')
        link = self.read / 'link.txt'; link.symlink_to(target)
        self.blocked(self.execute('files.read', f'读取 {link}', path=str(link)))

    def test_fifo_never_blocks_on_open(self):
        p = self.read / 'named-pipe.txt'; os.mkfifo(p)
        with self.assertRaises(LocalBlocked):
            self.d._safe_read(p)

    def test_path_must_appear_in_user_quote(self):
        p = self.read / '想法.md'; p.write_text('x')
        self.blocked(self.execute('files.read', '看看我的资料', path=str(p)))

    def test_path_prefix_and_suffix_are_not_authorized(self):
        p = self.read / 'a.txt'; p.write_text('not requested')
        self.blocked(self.execute('files.read', f'读取 {p}.different', path=str(p)))
        self.blocked(self.execute('files.read', f'读取 /prefix{p}', path=str(p)))
        result = self.execute('files.read', f'读取“{p}”', path=str(p))
        self.assertEqual(result['actions'][0]['status'], 'completed')
        write = self.write / 'a.md'
        self.blocked(self.execute('files.write', f'保存到 {write}.different，内容：abc', path=str(write), content='abc'))
        self.assertFalse(write.exists())

    def test_credentials_and_keys_denied(self):
        for name in ['.env', '.env.local', 'credentials.json', 'client-secret.json', 'private.pem']:
            p = self.read / name; p.write_text('secret')
            self.blocked(self.execute('files.read', f'读取 {p}', path=str(p)))

    def test_non_recursive_directory_list(self):
        (self.read / 'folder').mkdir(); (self.read / 'folder' / 'hidden.txt').write_text('deep')
        (self.read / 'plain.txt').write_text('public'); (self.read / '.env').write_text('secret')
        r = self.execute('files.list', f'列出文件夹 {self.read}', path=str(self.read))
        data = json.loads(r['contexts'][0]['text'])
        self.assertFalse(data['recursive'])
        self.assertEqual([e['name'] for e in data['entries']], ['folder','plain.txt'])

    def test_docx_text_read(self):
        p = self.read / 'example.docx'
        with zipfile.ZipFile(p, 'w') as z:
            z.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>合成正文</w:t></w:r></w:p></w:body></w:document>')
        r = self.execute('files.read', f'读取 {p}', path=str(p))
        self.assertEqual(json.loads(r['contexts'][0]['text'])['text'], '合成正文')

    def test_binary_and_pdf_blocked(self):
        for name, data in [('binary.txt', b'a\x00b'), ('example.pdf', b'%PDF-1.4')]:
            p = self.read / name; p.write_bytes(data)
            self.blocked(self.execute('files.read', f'读取 {p}', path=str(p)))

    def test_write_literal_then_verified_and_backup_overwrite(self):
        p = self.write / '想法.md'
        r = self.execute('files.write', f'保存到 {p}，内容：第一版', path=str(p), content='第一版')
        self.assertEqual(p.read_text(), '第一版')
        self.assertTrue(r['actions'][0]['verified'])
        prior = r['actions'][0]['sha256']
        r = self.execute('files.write', f'覆盖保存到 {p}，内容：第二版', path=str(p), content='第二版', expected_sha256=prior)
        self.assertEqual(p.read_text(), '第二版')
        self.assertEqual(Path(r['actions'][0]['backup_path']).read_text(), '第一版')

    def test_write_model_invented_content_denied(self):
        p = self.write / '想法.md'
        self.blocked(self.execute('files.write', f'保存到 {p}，内容：我原话', path=str(p), content='模型擅自写的'))
        self.assertFalse(p.exists())

    def test_write_negated_quoted_and_historical_statements_are_not_orders(self):
        p = self.write / '想法.md'
        cases = [f'先别把内容abc保存到 {p}', f'请解释这句话：“保存到 {p}，内容：abc”是什么意思？', f'上次我让你保存到 {p}，内容：abc']
        for message in cases:
            self.blocked(self.execute('files.write', message, path=str(p), content='abc'))
            self.assertFalse(p.exists())

    def test_whole_command_in_quotes_or_fences_is_data(self):
        p = self.write / 'quoted.md'
        command = f'保存到 {p}，内容：abc'
        cases = [f'“{command}”', f'"{command}"', f'「{command}」', f'```\n{command}\n```', f'> {command}']
        for message in cases:
            self.blocked(self.execute('files.write', message, quote=command, path=str(p), content='abc'))
            self.assertFalse(p.exists())

    def test_prior_data_framing_applies_across_newlines(self):
        p = self.write / 'quoted.md'; command = f'保存到 {p}，内容：abc'
        for prefix in ['下面只是引用，不是让你执行：\n', '请解释以下内容：\n', '不要执行下面的命令：\n']:
            self.blocked(self.execute('files.write', prefix + command, quote=command, path=str(p), content='abc'))
            self.assertFalse(p.exists())

    def test_write_binds_one_path_to_its_own_literal(self):
        a = self.write / 'a.md'; b = self.write / 'b.md'
        first = f'保存到 {a}，内容：苹果'; second = f'保存到 {b}，内容：香蕉'
        message = first + '；' + second
        self.blocked(self.execute('files.write', message, path=str(a), content='香蕉'))
        self.blocked(self.execute('files.write', message, path=str(b), content='苹果'))
        self.assertFalse(a.exists() or b.exists())
        r = self.execute('files.write', message, quote=first, path=str(a), content='苹果')
        self.assertEqual(r['actions'][0]['status'], 'completed')
        r = self.execute('files.write', message, quote=second, path=str(b), content='香蕉')
        self.assertEqual(r['actions'][0]['status'], 'completed')
        self.assertEqual(a.read_text(), '苹果'); self.assertEqual(b.read_text(), '香蕉')

    def test_common_explicit_write_grammars(self):
        examples = [('a.md', lambda p: f'请把「我的想法」保存到{p}'), ('b.md', lambda p: f'在{p}写入：我的想法'), ('c.md', lambda p: f'请帮我保存到“{p}”，内容：“我的想法”')]
        for name, command in examples:
            p = self.write / name
            result = self.execute('files.write', command(p), path=str(p), content='我的想法')
            self.assertEqual(result['actions'][0]['status'], 'completed', result)
            self.assertEqual(p.read_text(), '我的想法')

    def test_callback_started_precedes_write_and_terminal_receipt(self):
        p = self.write / 'callback.md'; message = f'保存到 {p}，内容：abc'; events = []
        def callback(action):
            events.append((action.copy(), p.exists()))
            action['status'] = 'mutation must not affect result'
        result = self.d.execute_local_requests([dict(tool='files.write', user_quote=message, path=str(p), content='abc')], message, 'callback-job', on_action=callback)
        self.assertEqual([x[0]['status'] for x in events], ['started','completed'])
        self.assertEqual([x[1] for x in events], [False,True])
        self.assertEqual(events[0][0]['id'], events[1][0]['id'])
        self.assertEqual(result['actions'][0]['status'], 'completed')
        self.assertNotIn('text', events[1][0])

    def test_callback_failure_before_execution_prevents_write(self):
        p = self.write / 'callback.md'; message = f'保存到 {p}，内容：abc'
        def failing(action): raise RuntimeError('receipt store unavailable')
        with self.assertRaises(RuntimeError):
            self.d.execute_local_requests([dict(tool='files.write', user_quote=message, path=str(p), content='abc')], message, 'callback-job', on_action=failing)
        self.assertFalse(p.exists())

    def test_callback_failure_after_write_keeps_started_as_uncertain(self):
        p = self.write / 'callback.md'; message = f'保存到 {p}，内容：abc'; persisted = []
        def failing_terminal(action):
            if action['status'] == 'started': persisted.append(action.copy())
            else: raise RuntimeError('receipt store unavailable')
        with self.assertRaises(RuntimeError):
            self.d.execute_local_requests([dict(tool='files.write', user_quote=message, path=str(p), content='abc')], message, 'callback-job', on_action=failing_terminal)
        self.assertEqual(p.read_text(), 'abc')
        self.assertEqual([x['status'] for x in persisted], ['started'])

    def test_callback_receives_blocked_terminal(self):
        events = []
        self.d.execute_local_requests([{'tool':'shell.run','user_quote':'查看电脑'}], '查看电脑', 'callback-job', on_action=events.append)
        self.assertEqual([x['status'] for x in events], ['started','blocked'])

    def test_note_count_is_not_note_id_or_body_request(self):
        self.blocked(self.execute('notes.read', '列出最近10条备忘录', id='1'))
        self.blocked(self.execute('notes.read', '读取最近10条备忘录', id='1'))
        self.blocked(self.execute('notes.read', '读取 2026 年 1 月备忘录', id='1'))

    def test_overwrite_serializes_compare_and_commit(self):
        p = self.write / 'serial.md'; p.write_text('initial')
        expected = hashlib.sha256(p.read_bytes()).hexdigest()
        a_waiting = threading.Event(); release_a = threading.Event(); b_started = threading.Event()
        original = self.d._safe_read; counts = {}; results = {}
        def guarded_read(path, *args):
            name = threading.current_thread().name
            counts[name] = counts.get(name, 0) + 1
            data = original(path, *args)
            if name == 'writer-a' and counts[name] == 2:
                a_waiting.set()
                if not release_a.wait(3):
                    raise RuntimeError('test did not release writer')
            return data
        def writer(name, content):
            if name == 'b': b_started.set()
            message = f'覆盖保存到 {p}，内容：{content}'
            results[name] = self.d.execute_local_requests([dict(tool='files.write', user_quote=message, path=str(p), content=content, expected_sha256=expected)], message, name)
        with patch.object(self.d, '_safe_read', side_effect=guarded_read):
            a = threading.Thread(target=writer, args=('a','version-a'), name='writer-a')
            b = threading.Thread(target=writer, args=('b','version-b'), name='writer-b')
            a.start()
            try:
                self.assertTrue(a_waiting.wait(2)); b.start(); self.assertTrue(b_started.wait(2))
            finally:
                release_a.set(); a.join(3)
                if b.ident: b.join(3)
        self.assertFalse(a.is_alive() or b.is_alive())
        self.assertEqual(results['a']['actions'][0]['status'], 'completed')
        self.blocked(results['b'])
        self.assertEqual(p.read_text(), 'version-a')

    def test_write_code_and_settings_denied(self):
        for name in ['app.py', 'settings.json', 'AGENTS.md', 'SKILL.md', '.hidden.md']:
            p = self.write / name
            self.blocked(self.execute('files.write', f'保存到 {p}，内容：x', path=str(p), content='x'))
            self.assertFalse(p.exists())

    def test_write_cannot_leave_write_root(self):
        p = self.read / '想法.md'
        self.blocked(self.execute('files.write', f'保存到 {p}，内容：x', path=str(p), content='x'))
        self.assertFalse(p.exists())

    def test_overwrite_requires_explicit_verb_and_fresh_hash(self):
        p = self.write / '想法.md'; p.write_text('original')
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        self.blocked(self.execute('files.write', f'保存到 {p}，内容：new', path=str(p), content='new', expected_sha256=digest))
        self.blocked(self.execute('files.write', f'覆盖保存到 {p}，内容：new', path=str(p), content='new', expected_sha256='wrong'))
        self.assertEqual(p.read_text(), 'original')

    def test_negated_quote_cannot_strip_negation(self):
        self.blocked(self.execute('notes.read', '不要读取最新备忘录', quote='读取最新备忘录', id='latest'))
        self.blocked(self.execute('notes.list', '假如有人说读取备忘录', quote='读取备忘录'))
        self.blocked(self.execute('notes.list', '先不读取备忘录', quote='读取备忘录'))
        self.blocked(self.execute('notes.list', '我不用看备忘录', quote='看备忘录'))

    def test_no_quote_or_unknown_tool_or_shell_payload(self):
        self.blocked(self.execute('notes.list', '什么都没要求', quote='读取备忘录'))
        self.blocked(self.execute('shell.run', '查看电脑', command='rm -rf /'))
        self.blocked(self.execute('system.info', '查看电脑', command='uname -a'))

    def test_system_info_excludes_identity(self):
        r = self.execute('system.info', '查看电脑系统和内存')
        data = json.loads(r['contexts'][0]['text'])
        self.assertTrue({'os','architecture','memory_total_bytes','disk_free_bytes'} <= set(data))
        self.assertFalse({'hostname','username','serial_number','ip_address'} & set(data))

    def test_truncation_is_explicit_valid_json(self):
        p = self.read / 'long.md'; p.write_text('a' * 25000)
        r = self.execute('files.read', f'读取 {p}', path=str(p))
        data = json.loads(r['contexts'][0]['text'])
        self.assertTrue(data['truncated']); self.assertEqual(len(data['text']), 24000)

    def test_request_limit_and_invalid_parameter_no_silent_execution(self):
        with self.assertRaises(ValueError):
            self.d.execute_local_requests([{}] * 7, '查看', 'job')
        self.blocked(self.execute('notes.list', '查看备忘录', limit=True))
        self.blocked(self.execute('notes.list', '查看备忘录', limit=100000))


if __name__ == '__main__':
    unittest.main()
