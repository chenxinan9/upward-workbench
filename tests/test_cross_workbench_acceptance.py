"""Independent cross-workbench acceptance; synthetic loopback providers only."""
import copy
from contextlib import closing
import hashlib
import datetime as dt
import sqlite3
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import Desk
from shared_knowledge import SharedKnowledge, SharedError, SHARED_ROOT
from creator_evidence import read_creator_evidence


class LoopbackProvider:
    """Small independent HTTP fixture; it never accesses a real workbench."""
    def __init__(self, respond):
        self.respond = respond
        self.requests = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def dispatch(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                data = json.loads(raw) if raw else None
                request = {'method': self.command, 'path': self.path,
                           'headers': dict(self.headers), 'data': data}
                owner.requests.append(request)
                status, value = owner.respond(request)
                body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            do_GET = do_POST = dispatch
        self.server = HTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
    @property
    def port(self):
        return self.server.server_port
    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def file_signature(base):
    """Reads may affect atime; content, ownership location, mode and mtime may not change."""
    result = {}
    for path in sorted(Path(base).rglob('*')):
        info = path.lstat()
        result[path.relative_to(base).as_posix()] = {
            'mode': info.st_mode, 'mtime_ns': info.st_mtime_ns, 'size': info.st_size,
            'dev': info.st_dev, 'ino': info.st_ino,
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() and not path.is_symlink() else None}
    return result


def make_creator_fixture(base):
    """Implements the independently recorded release/registration/DB contract."""
    base = Path(base); base.mkdir(parents=True, exist_ok=True)
    data = base / 'data'; data.mkdir()
    state = data / 'desktop-state'; state.mkdir()
    database = data / 'state.sqlite3'
    workspace_id = 'synthetic_creator_workspace'
    rows = [
        ('projects', 'public-example-1', {'id': 'public-example-1', 'title': '合成摄影练习一',
          'status': 'draft', 'created_at': '2026-10-10T04:00:00+08:00', 'updated_at': '2026-10-10T05:00:00+08:00'}),
        ('projects', 'public-example-2', {'id': 'public-example-2', 'title': '合成摄影练习二',
          'status': 'draft', 'created_at': '2026-10-09T04:00:00+08:00', 'updated_at': '2026-10-10T06:00:00+08:00'}),
        ('publication_records', 'declared-publish', {'id': 'declared-publish', 'project_id': 'public-example-1',
          'project_title': '合成摄影练习一', 'platform': 'public-example-platform', 'phase': 'published',
          'status': '已发布', 'reported_date': '2026-10-06', 'recorded_at': '2026-10-06T12:00:00+08:00',
          'updated_at': '2026-10-06T12:00:00+08:00', 'evidence_level': 'user_statement',
          'reported_by_user': True, 'platform_verified': False, 'public_readback_verified': False,
          'live_verified': False, 'source_label': '合成用户声明，未核验平台'})]
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute('PRAGMA journal_mode=DELETE')
        connection.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)')
        connection.executemany('INSERT INTO metadata VALUES (?,?)', [('purpose','production'),('workspace_id',workspace_id)])
        connection.execute('CREATE TABLE records(kind TEXT,id TEXT,payload TEXT,PRIMARY KEY(kind,id))')
        connection.executemany('INSERT INTO records VALUES (?,?,?)', [(k,i,json.dumps(v,ensure_ascii=False)) for k,i,v in rows])
    def identity(path):
        info = path.stat(); return [info.st_dev, info.st_ino]
    registration = data / 'native-registration.json'
    registration_data = {'schema': 2, 'purpose': 'production', 'workspace_id': workspace_id,
        'workspace_root': str(base), 'state_root': str(state), 'workspace_identity': identity(base),
        'database_identity': identity(database), 'database_schema': 0,
        'directories': {'data': identity(data), 'desktop-state': identity(state)}}
    registration.write_text(json.dumps(registration_data,ensure_ascii=False,indent=2))
    release = base / 'release.json'
    registration_sha = hashlib.sha256(registration.read_bytes()).hexdigest()
    release_data = {'app_id': 'local.an.workbench', 'purpose': 'production',
        'workspace': {'registration_path': str(registration), 'registration_sha256': registration_sha}}
    release.write_text(json.dumps(release_data,ensure_ascii=False,indent=2))
    binding = {'release_path': str(release), 'release_sha256': hashlib.sha256(release.read_bytes()).hexdigest(),
        'workspace_id': workspace_id, 'registration_sha256': registration_sha}
    return {'root': base, 'database': database, 'registration': registration, 'release': release,
            'binding': binding, 'rows': rows}


def reseal_creator(fixture, registration_change=None, release_change=None):
    """Only test-owned pins are resigned, so each inner identity check can be exercised."""
    registration = json.loads(fixture['registration'].read_text())
    if registration_change:
        registration_change(registration)
    fixture['registration'].write_text(json.dumps(registration, ensure_ascii=False, indent=2))
    registration_sha = hashlib.sha256(fixture['registration'].read_bytes()).hexdigest()
    release = json.loads(fixture['release'].read_text())
    release['workspace']['registration_sha256'] = registration_sha
    if release_change:
        release_change(release)
    fixture['release'].write_text(json.dumps(release, ensure_ascii=False, indent=2))
    fixture['binding']['registration_sha256'] = registration_sha
    fixture['binding']['release_sha256'] = hashlib.sha256(fixture['release'].read_bytes()).hexdigest()


class CreatorWorkBenchAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.fixture = make_creator_fixture(self.root / 'creator')
        data_root = self.root / 'advisor'; data_root.mkdir()
        (data_root / 'settings.json').write_text(json.dumps({'creator_binding': self.fixture['binding']}))
        self.desk = Desk(data_root)
        self.addCleanup(self.desk.stop_workspace)

    def records(self, rows, replace=False):
        with closing(sqlite3.connect(self.fixture['database'])) as connection, connection:
            if replace:
                connection.execute('DELETE FROM records')
            connection.executemany('INSERT OR REPLACE INTO records VALUES (?,?,?)', [
                (kind, ident, json.dumps(value, ensure_ascii=False)) for kind, ident, value in rows])

    def read(self):
        return read_creator_evidence(self.fixture['binding'])

    def test_registered_source_reads_only_the_two_allowed_kinds_without_mutation(self):
        self.records([('private_drafts', 'forbidden', {'id': 'forbidden', 'body': 'DO-NOT-READ-PRIVATE-DRAFT'})])
        before = file_signature(self.fixture['root'])
        result = self.desk.creator_evidence()
        self.assertEqual(result['status'], 'connected', result)
        self.assertTrue(result['identity_verified'])
        self.assertEqual(len(result['works']), 2)
        self.assertEqual(len(result['publications']), 1)
        self.assertEqual(result['publications'][0]['at'], '2026-10-06')
        self.assertTrue(result['publications'][0]['reported_by_user'])
        for field in ('live_verified', 'platform_verified', 'public_readback_verified'):
            self.assertFalse(result['publications'][0][field])
        self.assertEqual(result['latest_record_at'], '2026-10-10T06:00:00+08:00')
        self.assertNotEqual(result['read_at'], result['latest_record_at'])
        self.assertNotIn('DO-NOT-READ-PRIVATE-DRAFT', json.dumps(result))
        self.assertEqual(file_signature(self.fixture['root']), before)

    def test_wrong_release_application_and_wrong_registered_workspace_are_not_evidence(self):
        for kind in ('application', 'workspace', 'database_inode', 'schema'):
            with self.subTest(kind=kind):
                fixture = make_creator_fixture(self.root / kind)
                if kind == 'application':
                    reseal_creator(fixture, release_change=lambda v: v.update(app_id='unrelated.application'))
                elif kind == 'workspace':
                    reseal_creator(fixture, registration_change=lambda v: v.update(workspace_id='another-workspace'))
                elif kind == 'database_inode':
                    reseal_creator(fixture, registration_change=lambda v: v.update(database_identity=[0, 0]))
                else:
                    with closing(sqlite3.connect(fixture['database'])) as connection, connection:
                        connection.execute('PRAGMA user_version=99')
                before = file_signature(fixture['root'])
                result = read_creator_evidence(fixture['binding'])
                self.assertEqual(result['status'], 'unavailable')
                self.assertFalse(result['identity_verified'])
                self.assertEqual(result['works'] + result['publications'], [])
                self.assertEqual(file_signature(fixture['root']), before)

    def test_database_metadata_cannot_self_certify_another_workspaces_identity(self):
        with closing(sqlite3.connect(self.fixture['database'])) as connection, connection:
            connection.execute("UPDATE metadata SET value='wrong-workspace' WHERE key='workspace_id'")
        self.assertEqual(self.read()['status'], 'unavailable')

    def test_failed_pinned_binding_never_falls_back_to_legacy_http_port(self):
        provider = LoopbackProvider(lambda request: (200, {'works': [{'title': 'WRONG-LEGACY-RECORD'}]}))
        self.addCleanup(provider.close)
        self.desk.settings['creator_port'] = provider.port
        self.fixture['release'].write_text('{}')
        result = self.desk.creator_evidence()
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(provider.requests, [])
        self.assertNotIn('WRONG-LEGACY-RECORD', json.dumps(result))

    def test_each_read_rechecks_pins_and_new_database_records_instead_of_using_old_cache(self):
        self.assertEqual(len(self.read()['works']), 2)
        self.records([('projects', 'new-work', {'id': 'new-work', 'title': '新登记的公开练习',
            'created_at': '2026-10-11', 'updated_at': '2026-10-12'})])
        self.assertEqual(len(self.read()['works']), 3)
        self.fixture['release'].write_text('{}')
        failed = self.read()
        self.assertEqual(failed['status'], 'unavailable')
        self.assertEqual(failed['works'], [])

    def test_malformed_row_is_partial_and_cannot_claim_complete_coverage(self):
        with closing(sqlite3.connect(self.fixture['database'])) as connection, connection:
            connection.execute("INSERT INTO records VALUES ('projects','broken','not json')")
        result = self.read()
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(result['identity_verified'])
        self.assertFalse(result['coverage']['complete'])
        self.assertEqual(len(result['works']), 2)
        self.assertIn('缺失不表示没有创作', ' '.join(result['warnings']))

    def test_oversize_record_kind_is_not_silently_reported_as_complete(self):
        self.records([('projects', 'too-long', {'id': 'too-long', 'body': 'x' * 1_200_000})])
        result = self.read()
        self.assertEqual(result['status'], 'partial')
        self.assertFalse(result['coverage']['complete'])
        self.assertEqual(result['works'], [])
        self.assertEqual(len(result['publications']), 1)

    def test_realistic_large_creation_body_stays_readable_but_is_not_projected(self):
        sentinel = 'LARGE-CREATOR-PRIVATE-BODY-MUST-NOT-PROJECT-'
        self.records([('projects', 'large-work', {'id': 'large-work', 'title': '正常大小的完整作品',
            'body': sentinel + 'x' * 208765, 'created_at': '2026-10-11', 'updated_at': '2026-10-12'})])
        before = file_signature(self.fixture['root'])
        result = self.read()
        self.assertEqual(result['status'], 'connected')
        self.assertEqual(len(result['works']), 3)
        self.assertNotIn(sentinel, json.dumps(result))
        self.assertEqual(file_signature(self.fixture['root']), before)

    def test_wal_header_is_refused_before_opening_and_never_creates_shm_or_other_files(self):
        with closing(sqlite3.connect(self.fixture['database'])) as connection, connection:
            self.assertEqual(connection.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
        before = file_signature(self.fixture['root'])
        self.assertEqual(self.read()['status'], 'unavailable')
        self.assertEqual(file_signature(self.fixture['root']), before)
        self.assertFalse(Path(str(self.fixture['database']) + '-shm').exists())

    def test_symlinked_database_and_coordinating_sidecars_are_refused(self):
        for suffix in ('-wal', '-shm', '-journal'):
            sidecar = Path(str(self.fixture['database']) + suffix)
            sidecar.write_text('SYNTHETIC-COORDINATION')
            before = file_signature(self.fixture['root'])
            self.assertEqual(self.read()['status'], 'unavailable')
            self.assertEqual(file_signature(self.fixture['root']), before)
            sidecar.unlink()
        original = self.fixture['database'].with_name('original.sqlite3')
        self.fixture['database'].rename(original)
        self.fixture['database'].symlink_to(original)
        self.assertEqual(self.read()['status'], 'unavailable')

    def test_recorded_today_without_occurrence_date_does_not_become_todays_publication(self):
        today = dt.datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        self.records([('publication_records', 'date-unknown', {'id': 'date-unknown', 'project_id': 'public-example-1',
            'title': '只知道登记日', 'platform': 'example', 'status': '已发布', 'recorded_at': today,
            'reported_by_user': True})], replace=True)
        result = self.desk.phase_snapshot(7)
        self.assertEqual(result['creator']['status'], 'partial')
        self.assertEqual(result['creator']['publications'][0]['recorded_at'], today)
        self.assertIsNone(result['creator']['publications'][0]['at'])
        self.assertEqual(result['creator']['days'][-1]['known_count'], 0)
        self.assertIsNone(result['creator']['days'][-1]['count'])
        self.assertFalse(any(e['kind'] == 'publication' for e in result['events']))

    def test_multi_platform_same_work_counts_once_and_pending_future_fixture_are_not_achievements(self):
        today = dt.datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        rows = []
        for ident, platform, project, status in [
            ('platform-a', 'A', 'work-a', '已发布'), ('platform-b', 'B', 'work-a', '已发布'),
            ('duplicate-a', 'A', 'work-a', '已发布'), ('pending', 'C', 'work-b', 'pending')]:
            rows.append(('publication_records', ident, {'id': ident, 'project_id': project,
                'title': '同作品多平台' if project == 'work-a' else '仍待确认', 'platform': platform,
                'post_id': 'same-post' if platform == 'A' else ident, 'status': status,
                'reported_date': today, 'recorded_at': today, 'reported_by_user': True}))
        rows += [('projects', 'fixture-work', {'id': 'fixture-work', 'title': 'fixture', 'purpose': 'acceptance'}),
                 ('publication_records', 'fixture-publication', {'id': 'fixture-publication', 'project_id': 'fixture-work',
                    'title': '不算正式成绩', 'status': '已发布', 'reported_date': today}),
                 ('publication_records', 'future', {'id': 'future', 'project_id': 'future', 'title': '未来',
                    'status': '已发布', 'reported_date': '2999-01-01'})]
        self.records(rows, replace=True)
        result = self.desk.phase_snapshot(7)
        self.assertEqual(result['creator']['days'][-1]['known_count'], 1)
        self.assertEqual(len(result['creator']['publications']), 3)
        self.assertEqual(result['creator']['status'], 'partial')
        self.assertFalse(result['creator']['coverage']['complete'])
        self.assertIn('缺记录不等于当天没有创作', result['creator']['summary'])

    def test_read_time_does_not_reactivate_old_publication_or_claim_zero_when_unconfigured(self):
        result = self.desk.phase_snapshot(7)
        self.assertEqual(result['creator']['publications'], [])
        self.assertTrue(result['creator']['read_at'])
        self.desk.settings.pop('creator_binding')
        result = self.desk.phase_snapshot(7)
        self.assertEqual(result['creator']['status'], 'not_configured')
        self.assertTrue(all(day['count'] is None for day in result['creator']['days']))


class SharedWorkBenchAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.scope = {'instance_id': 'SYNTHETIC_INSTANCE_1234', 'generation': 1,
                      'scope_fingerprint': 'a' * 64}
        self.selected_id = 'SHR-' + '1' * 32
        self.unselected_id = 'SHR-' + '2' * 32
        self.selected_text = 'SELECTED-SYNTHETIC-LINE-1\nSELECTED-SYNTHETIC-LINE-2'
        self.unselected_text = 'NEVER-SHARE-UNSELECTED-SYNTHETIC-BODY'
        self.token = 'SYNTHETIC_TOKEN_' + 't' * 40
        self.mode = 'normal'
        self.items = {
            self.selected_id: self.item(self.selected_id, self.selected_text, '明确选择片段'),
            self.unselected_id: self.item(self.unselected_id, self.unselected_text, '没有选择片段')
        }
        self.provider = LoopbackProvider(self.respond)
        self.addCleanup(self.provider.close)
        self.connection_path = self.root / 'connection.json'
        self.write_connection()
        self.client = SharedKnowledge(self.connection_path)
        self.shares = [{'share_id': self.selected_id, 'expected_version': 1}]
        self.vault = self.root / 'vault'; self.vault.mkdir()
        self.document = self.vault / 'scope.md'; self.document.write_text('第一行公开文字\n第二行公开文字')
        data_root = self.root / 'data'; data_root.mkdir()
        (data_root / 'settings.json').write_text(json.dumps({'vault_roots': [
            {'id': 'upward', 'path': str(self.vault), 'writable': True}]}))
        self.desk = Desk(data_root)
        self.addCleanup(self.desk.stop_workspace)
        self.desk.shared_knowledge = self.client
        self.desk.cli = 'synthetic-no-process'
        self.model = {'id': 'synthetic-model', 'selection_fingerprint': 'synthetic-directory-v1'}
        model_patch = patch.object(self.desk, 'resolve_model', side_effect=lambda ident: dict(self.model, id=ident or self.model['id']))
        model_patch.start(); self.addCleanup(model_patch.stop)
        creator_patch = patch.object(self.desk, 'creator_evidence', return_value={
            'status': 'not_configured', 'works': [], 'publications': [], 'warnings': []})
        creator_patch.start(); self.addCleanup(creator_patch.stop)

    def item(self, ident, text, title):
        return {'kind': 'shared', 'share_id': ident, 'version': 1,
                'selected_sha256': hashlib.sha256(text.encode()).hexdigest(), 'title': title,
                'source_kind': 'text', 'line_reference': '提取正文第7行起',
                'locator': {'kind': 'extracted_text_lines', 'line_start': 7,
                            'line_count': len(text.split('\n'))},
                'text': text, 'freshness': 'verified'}

    def write_connection(self, **changes):
        data = {'version': 1, 'protocol_version': 1, 'application': 'local-knowledge-workbench',
                'target': 'growth', 'online': True, 'port': self.provider.port,
                'instance_id': self.scope['instance_id'], 'token': self.token}
        data.update(changes)
        self.connection_path.write_text(json.dumps(data))
        self.connection_path.chmod(0o600)

    def respond(self, request):
        if (request['headers'].get('X-Knowledge-Share-Token') != self.token
                or request['headers'].get('X-Knowledge-Share-Instance') != self.scope['instance_id']):
            return 403, {'ok': False, 'error': {'code': 'unauthorized', 'message': '合成连接身份不符'}}
        if self.mode == 'echo_error':
            return 403, {'ok': False, 'error': {'code': 'unauthorized',
                'message': '不要展示合成凭证 ' + self.token}}
        if self.mode == 'timeout':
            return 503, {'ok': False, 'error': {'code': 'shared_unavailable', 'message': '合成来源超时', 'retryable': True}}
        if self.mode == 'malformed':
            return 200, b'not json'
        if self.mode == 'redirect':
            return 302, {'ok': True, 'location': 'https://example.invalid/never-follow'}
        if self.mode == 'oversized':
            return 200, b'x' * 100001
        if self.mode == 'revoked':
            return 409, {'ok': False, 'error': {'code': 'share_revoked', 'message': '合成片段已经撤回'}}
        scope = copy.deepcopy(self.scope)
        if self.mode == 'wrong_instance':
            scope['instance_id'] = 'WRONG_SYNTHETIC_INSTANCE'
        if self.mode == 'wrong_scope':
            scope['generation'] += 1
        if request['method'] == 'GET':
            shares = [{'share_id': ident, 'version': item['version'], 'state': 'active'}
                      for ident, item in self.items.items()]
            return 200, {'ok': True, 'data': dict(scope, shares=shares)}
        if self.mode == 'catalog_active_read_conflict':
            return 409, {'ok': False, 'error': {'code': 'share_source_changed',
                'message': '当前来源范围或隐私规则已变化，请重新分享', 'retryable': False}}
        data = request['data'] or {}
        if data.get('expected_generation') != self.scope['generation']:
            return 409, {'ok': False, 'error': {'code': 'share_conflict', 'message': '合成范围发生变化'}}
        wanted = [entry['share_id'] for entry in data.get('shares', [])]
        values = [copy.deepcopy(self.items[ident]) for ident in wanted if ident in self.items]
        if self.mode == 'extra_unselected':
            values.append(copy.deepcopy(self.items[self.unselected_id]))
        if self.mode == 'stale_text' and values:
            values[0]['text'] += '\nBODY-CHANGED-WITHOUT-SHA'
        if request['path'].endswith('/search'):
            hits = []
            for value in values:
                value['excerpt'] = value.pop('text')[:100]
                value['truncated'] = False
                hits.append(value)
            return 200, {'ok': True, 'data': dict(scope, mode='selected_literal',
                scope='explicit_shared_only', items=hits, result_count=len(hits), truncated=False)}
        return 200, {'ok': True, 'data': dict(scope, items=values)}

    def payload(self, **changes):
        data = {'message': '比较我明确选择的合成片段', 'model': self.model['id'],
                'scope': 'advisors', 'execution_mode': 'knowledge', 'purpose': 'general',
                'days': 7, 'attachment_ids': []}
        data.update(changes)
        return data

    def preview(self, payload=None):
        return self.desk.workspace_post('/api/workspace/shared/preview', dict(
            payload or self.payload(), shares=self.shares, shared_scope=self.scope))

    def send_data(self, payload, preview):
        return dict(payload, shared_preview_id=preview['preview_id'],
                    shared_preview_sha256=preview['preview_sha256'], shared_confirmed=True)


    def test_shared_summary_is_metadata_only_and_never_automatically_reads_a_body(self):
        summary = self.client.summary()
        self.assertEqual({row['share_id'] for row in summary['shares']}, set(self.items))
        visible = json.dumps(summary, ensure_ascii=False)
        for forbidden in (self.token, self.selected_text, self.unselected_text, str(self.connection_path)):
            self.assertNotIn(forbidden, visible)
        self.assertEqual([(r['method'], r['path']) for r in self.provider.requests],
                         [('GET', '/api/v1/shared/growth')])

    def test_unselected_extra_body_and_unverified_changed_text_are_rejected(self):
        for mode in ('extra_unselected', 'stale_text', 'wrong_instance', 'wrong_scope', 'malformed', 'redirect', 'oversized'):
            with self.subTest(mode=mode):
                self.mode = mode
                with self.assertRaises(SharedError):
                    self.client.read(self.shares, self.scope)
        self.mode = 'extra_unselected'
        with self.assertRaises(SharedError):
            self.client.search('合成', 12, self.shares, self.scope)

    def test_preview_context_binding_rejects_each_effective_context_change(self):
        names = [a['name'] for a in self.desk._workspace_advisors]
        mutations = [
            ('question', {'message': '改过的问题'}), ('model', {'model': 'another-synthetic-model'}),
            ('scope', {'scope': 'advisors'}), ('purpose', {'purpose': 'review'}),
            ('period', {'days': 30}), ('execution', {'execution_mode': 'assistant'}),
            ('selection', {'selection': '第二行公开文字'}), ('attachments', {'attachment_ids': ['synthetic-attachment']}),
            ('write', {'allow_write': True}), ('advisors', {'advisor_names': names[:1]}),
            ('session', {}), ('document-sha', {}), ('model-effort', {})
        ]
        for kind, changes in mutations:
            with self.subTest(kind=kind):
                self.model.pop('reasoning_effort', None)
                self.document.write_text('第一行公开文字\n第二行公开文字')
                session = self.desk.store.put('workspace-session', {'title': '独立上下文绑定'})
                payload = self.payload(scope='personal', current_file_id='upward:scope.md',
                    selection='第一行公开文字', session_id=session['id'])
                preview = self.preview(payload); self.assertNotIn('error', preview)
                changed = dict(payload, **changes)
                if kind == 'session':
                    changed['session_id'] = self.desk.store.put('workspace-session', {'title': '另一个会话'})['id']
                elif kind == 'document-sha':
                    self.document.write_text('第一行公开文字\n第二行公开文字\n外部刚改过')
                elif kind == 'model-effort':
                    self.model['reasoning_effort'] = 'high'
                with patch('workspace_api.threading.Thread.start', lambda thread: None):
                    with self.assertRaises(SharedError):
                        self.desk.workspace_send(self.send_data(changed, preview))

    def test_revoked_or_changed_selection_is_rejected_before_queueing(self):
        for mode in ('revoked', 'wrong_scope', 'stale_text', 'timeout'):
            with self.subTest(mode=mode):
                self.mode = 'normal'; payload = self.payload(); preview = self.preview(payload)
                self.assertNotIn('error', preview)
                self.mode = mode
                before = len(self.desk.store.all('workspace-job'))
                with patch('workspace_api.threading.Thread.start', lambda thread: None):
                    with self.assertRaises(SharedError):
                        self.desk.workspace_send(self.send_data(payload, preview))
                self.assertEqual(len(self.desk.store.all('workspace-job')), before)
                self.assertEqual(payload['message'], '比较我明确选择的合成片段')
                self.assertEqual(self.client.previews[preview['preview_id']]['shares'], self.shares)

    def test_shared_unselected_access_and_midturn_revocation_fail_closed(self):
        payload = self.payload(); preview = self.preview(payload)
        with patch('workspace_api.threading.Thread.start', lambda thread: None):
            sent = self.desk.workspace_send(self.send_data(payload, preview))
        def fake(client, **kw):
            kw['before_send']()
            self.assertNotIn(self.selected_text, json.dumps(kw['context'], ensure_ascii=False))
            self.assertNotIn(self.unselected_text, json.dumps(kw['context'], ensure_ascii=False))
            self.assertNotIn(self.token, json.dumps(kw['context'], ensure_ascii=False))
            result = kw['tool_call']('knowledge_read', {'document_id': self.selected_id})
            self.assertEqual(result['text'], self.selected_text)
            with self.assertRaises(PermissionError):
                kw['tool_call']('knowledge_read', {'document_id': self.unselected_id})
            self.mode = 'revoked'
            with self.assertRaises(SharedError):
                kw['tool_call']('knowledge_read', {'document_id': self.selected_id})
            with self.assertRaises(SharedError):
                kw['tool_call']('knowledge_search', {'query': '合成', 'root_id': SHARED_ROOT})
            return {'text': '只读边界验收完成', 'status': 'completed', 'actual_model': 'synthetic-model'}
        with patch('codex_workspace.CodexWorkspace.run_turn', fake):
            self.desk.workspace_run(sent['job'], sent['session'], self.model, payload['message'],
                                    None, '', False, threading.Event())
        final = self.desk.store.get(sent['job']['id'])
        self.assertEqual(final['status'], 'failed', final.get('error'))
        self.assertTrue(final['shared_invalidated_during_turn'])
        self.assertFalse(final['shared_not_sent'])
        stored = json.dumps(self.desk.store.all('workspace-job') + self.desk.store.all('workspace-message'), ensure_ascii=False)
        for forbidden in (self.token, self.unselected_text, self.selected_text, str(self.connection_path)):
            self.assertNotIn(forbidden, stored)
        self.assertFalse(self.client.previews)

    def test_upstream_error_cannot_reflect_the_connection_token_to_public_receipts(self):
        self.mode = 'echo_error'
        public = self.desk.workspace_get('/api/workspace/shared', {})
        self.assertNotIn(self.token, json.dumps(public, ensure_ascii=False))
        self.assertIn('error', public)


    def test_active_catalog_with_unreadable_source_requires_resharing_not_fake_readiness(self):
        summary = self.client.summary()
        self.assertEqual(summary['shares'][0]['state'], 'active')
        self.assertFalse(summary.get('can_reference', False))
        self.mode = 'catalog_active_read_conflict'
        result = self.desk.workspace_post('/api/workspace/shared/read', {
            'shares': self.shares, 'scope': self.scope})
        self.assertIn('error', result)
        self.assertIn('重新分享', result['error']['message'])
        self.assertFalse(result['error']['retryable'])
        self.assertNotIn('items', result)
        preview = self.preview()
        self.assertIn('error', preview)
        self.assertFalse(self.client.previews)
        self.assertEqual(self.shares, [{'share_id': self.selected_id, 'expected_version': 1}])

    def test_transport_timeout_is_retryable_without_body_or_token_in_the_error(self):
        with patch('shared_knowledge.http.client.HTTPConnection.getresponse', side_effect=TimeoutError('synthetic timeout')):
            public = self.desk.workspace_get('/api/workspace/shared', {})
        self.assertIn('error', public)
        self.assertTrue(public['error']['retryable'])
        for forbidden in (self.token, self.selected_text, self.unselected_text):
            self.assertNotIn(forbidden, json.dumps(public, ensure_ascii=False))

    def test_offline_or_insecure_connection_file_is_not_bypassed(self):
        before = len(self.provider.requests)
        self.write_connection(online=False)
        with self.assertRaises(SharedError):
            self.client.summary()
        self.assertEqual(len(self.provider.requests), before)
        self.write_connection()
        self.connection_path.chmod(0o644)
        with self.assertRaises(SharedError):
            self.client.summary()
        self.assertEqual(len(self.provider.requests), before)

    def test_midturn_revocation_also_invalidates_an_existing_remote_thread_if_transport_catches_tool_error(self):
        """Real transports can report a rejected tool as model input rather than raising it."""
        payload = self.payload(); preview = self.preview(payload)
        with patch('workspace_api.threading.Thread.start', lambda thread: None):
            first = self.desk.workspace_send(self.send_data(payload, preview))
        def first_turn(client, **kw):
            kw['before_send']()
            kw['tool_call']('knowledge_read', {'document_id': self.selected_id})
            return {'text': '合成首轮', 'status': 'completed', 'thread_id': 'synthetic-shared-remote-thread'}
        with patch('codex_workspace.CodexWorkspace.run_turn', first_turn):
            self.desk.workspace_run(first['job'], first['session'], self.model, payload['message'],
                                    None, '', False, threading.Event())
        payload['session_id'] = first['session']['id']
        preview = self.preview(payload)
        with patch('workspace_api.threading.Thread.start', lambda thread: None):
            second = self.desk.workspace_send(self.send_data(payload, preview))
        def second_turn(client, **kw):
            self.assertEqual(kw['thread_id'], 'synthetic-shared-remote-thread')
            kw['before_send']()
            kw['tool_call']('knowledge_read', {'document_id': self.selected_id})
            self.mode = 'revoked'
            try:
                kw['tool_call']('knowledge_read', {'document_id': self.selected_id})
            except SharedError:
                pass  # Mirrors transport tool-error handling, not a host-level crash.
            return {'text': '来源已经不可核对', 'status': 'completed', 'thread_id': 'synthetic-shared-remote-thread'}
        with patch('codex_workspace.CodexWorkspace.run_turn', second_turn):
            self.desk.workspace_run(second['job'], second['session'], self.model, payload['message'],
                                    None, '', False, threading.Event())
        self.assertIsNone(self.desk.store.get(first['session']['id'])['thread_id'])
        final = self.desk.store.get(second['job']['id'])
        self.assertEqual(final['status'], 'failed')
        self.assertTrue(final['shared_invalidated_during_turn'])

    def test_preview_expiry_or_confirmation_replay_cannot_queue_a_second_generation(self):
        payload = self.payload(); preview = self.preview(payload)
        self.client.previews[preview['preview_id']]['expires_at'] = 0
        with patch('workspace_api.threading.Thread.start', lambda thread: None):
            with self.assertRaises(SharedError):
                self.desk.workspace_send(self.send_data(payload, preview))
        self.assertEqual(self.desk.store.all('workspace-job'), [])
        preview = self.preview(payload)
        with patch('workspace_api.threading.Thread.start', lambda thread: None):
            self.desk.workspace_send(self.send_data(payload, preview))
            before = len(self.desk.store.all('workspace-job'))
            with self.assertRaises((SharedError, ValueError)):
                self.desk.workspace_send(self.send_data(payload, preview))
        self.assertEqual(len(self.desk.store.all('workspace-job')), before)
