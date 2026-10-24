"""Bounded creator fixtures: source identity, occurrence dates and zero source writes."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import creator_evidence as creator


class CreatorEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.data = self.root / 'data'; self.data.mkdir()
        (self.data / 'desktop-state').mkdir()
        self.database = self.data / 'state.sqlite3'
        with closing(sqlite3.connect(self.database)) as db:
            db.executescript('CREATE TABLE records(kind TEXT,id TEXT,payload TEXT,PRIMARY KEY(kind,id)); CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);')
            db.executemany('INSERT INTO metadata VALUES(?,?)', [('purpose', 'production'), ('workspace_id', 'synthetic-workspace')]); db.commit()
        self.registration = self.data / 'native-registration.json'
        self.release = self.root / 'release.json'
        self.pin()

    def tearDown(self):
        self.temp.cleanup()

    def pin(self):
        identity = lambda path: [path.stat().st_dev, path.stat().st_ino]
        self.registration.write_text(json.dumps(dict(schema=2, purpose='production', workspace_id='synthetic-workspace',
            workspace_root=str(self.root), state_root=str(self.data / 'desktop-state'), workspace_identity=identity(self.root),
            database_identity=identity(self.database), database_schema=0,
            directories={'data': identity(self.data), 'desktop-state': identity(self.data / 'desktop-state')})))
        checksum = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
        self.release.write_text(json.dumps(dict(app_id='local.an.workbench', purpose='production',
            workspace=dict(registration_path=str(self.registration), registration_sha256=checksum(self.registration)))))
        self.binding = dict(release_path=str(self.release), release_sha256=checksum(self.release),
            workspace_id='synthetic-workspace', registration_sha256=checksum(self.registration))

    def put(self, kind, **record):
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('INSERT OR REPLACE INTO records VALUES(?,?,?)', (kind, record['id'], json.dumps(record))); db.commit()

    def stable_binding(self):
        return dict(self.binding, registration_path=str(self.registration))

    def change_release(self, **values):
        release = json.loads(self.release.read_text())
        release.update(values)
        self.release.write_text(json.dumps(release))

    def test_occurrence_and_update_dates_are_separate_and_private_fields_not_projected(self):
        self.put('projects', id='p', title='公开合成作品', created_at='2025-01-01', updated_at='2025-01-05', user_quote='不投影的正文')
        self.put('publication_records', id='r', title='公开合成作品', project_id='p', status='已发布', reported_date='2025-01-02', recorded_at='2025-01-03', platform_verified=False)
        before = self.database.read_bytes(), self.database.stat().st_mtime_ns
        result = creator.read_creator_evidence(self.binding)
        self.assertEqual(result['status'], 'connected')
        self.assertEqual(result['publications'][0]['at'], '2025-01-02')
        self.assertEqual(result['latest_record_at'], '2025-01-05')
        self.assertFalse(result['publications'][0]['live_verified'])
        self.assertNotIn('不投影的正文', json.dumps(result, ensure_ascii=False))
        self.assertEqual(before, (self.database.read_bytes(), self.database.stat().st_mtime_ns))

    def test_recorded_only_does_not_become_a_publication_day(self):
        self.put('publication_records', id='r', status='已发布', recorded_at='2025-01-03')
        result = creator.read_creator_evidence(self.binding)
        self.assertEqual(result['status'], 'partial')
        self.assertIsNone(result['publications'][0]['at'])
        self.assertEqual(result['publications'][0]['recorded_at'], '2025-01-03')

    def test_replaced_database_and_changed_pins_are_not_cached_as_connected(self):
        self.assertEqual(creator.read_creator_evidence(self.binding)['status'], 'connected')
        moved = self.database.with_suffix('.old'); self.database.rename(moved); self.database.write_bytes(moved.read_bytes())
        self.assertEqual(creator.read_creator_evidence(self.binding)['status'], 'unavailable')
        self.pin(); self.release.write_text('{}')
        self.assertEqual(creator.read_creator_evidence(self.binding)['status'], 'unavailable')

    def test_oversized_payload_is_not_loaded_or_called_complete(self):
        self.put('projects', id='p', title='x' * 1_000_001)
        result = creator.read_creator_evidence(self.binding)
        self.assertEqual(result['status'], 'partial'); self.assertEqual(result['works'], [])

    def test_wal_transition_never_opens_source_with_sqlite_or_creates_sidecars(self):
        connect = sqlite3.connect; switched = False
        def open_snapshot(path, *args, **kwargs):
            nonlocal switched
            self.assertNotIn(str(self.database), str(path))
            if not switched:
                switched = True
                with closing(connect(self.database)) as source:
                    source.execute('PRAGMA journal_mode=WAL')
            return connect(path, *args, **kwargs)
        with patch.object(creator.sqlite3, 'connect', side_effect=open_snapshot):
            result = creator.read_creator_evidence(self.binding)
        self.assertEqual(result['status'], 'unavailable')
        self.assertFalse(Path(str(self.database) + '-wal').exists())
        self.assertFalse(Path(str(self.database) + '-shm').exists())

    def test_explicit_registration_binding_survives_release_version_updates_without_source_writes(self):
        binding = self.stable_binding()
        self.put('projects', id='p', title='同一批准资料库', updated_at='2025-01-05')
        for sequence in (61, 62):
            with self.subTest(sequence=sequence):
                self.change_release(version='0.3.1', sequence=sequence)
                before = {p:(p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob('*') if p.is_file()}
                self.assertNotEqual(hashlib.sha256(self.release.read_bytes()).hexdigest(), binding['release_sha256'])
                result = creator.read_creator_evidence(binding)
                self.assertEqual(result['status'], 'connected')
                self.assertTrue(result['identity_verified'])
                self.assertEqual(result['works'][0]['id'], 'p')
                self.assertEqual({p:(p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob('*') if p.is_file()}, before)

    def test_legacy_binding_still_requires_exact_release_bytes(self):
        self.change_release(version='new-version', sequence=62)
        self.assertEqual(creator.read_creator_evidence(self.binding)['status'], 'unavailable')

    def test_stable_binding_still_rejects_changed_application_or_registration_metadata(self):
        binding = self.stable_binding()
        original = self.release.read_bytes()
        another = self.data / 'other-registration.json'
        another.write_bytes(self.registration.read_bytes())
        workspace = json.loads(original)['workspace']
        mutations = [dict(app_id='other.application'), dict(purpose='test'),
                     dict(workspace=dict(workspace, registration_path=str(another))),
                     dict(workspace=dict(workspace, registration_sha256='0'*64))]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.release.write_bytes(original)
                self.change_release(**mutation)
                with patch.object(creator, '_json_pin', wraps=creator._json_pin) as read:
                    result = creator.read_creator_evidence(binding)
                self.assertEqual(result['status'], 'unavailable')
                self.assertFalse(result['identity_verified'])
                self.assertFalse(any(call.args[0] == another for call in read.call_args_list))

    def test_invalid_explicit_registration_path_never_falls_back_to_legacy_mode(self):
        invalid = [None, '', True, 12, [], {}, 'relative.json', '~/registration.json',
                   str(self.registration)+'/', str(self.data)+'/./native-registration.json',
                   str(self.data)+'/../data/native-registration.json', str(self.data)+'//native-registration.json']
        self.assertEqual(creator.read_creator_evidence(self.binding)['status'], 'connected')
        for value in invalid:
            with self.subTest(value=value):
                result = creator.read_creator_evidence(dict(self.binding, registration_path=value))
                self.assertEqual(result['status'], 'unavailable')
                self.assertEqual(result['works'], [])

    def test_stable_binding_rejects_symlink_or_foreign_owner(self):
        alias = self.data / 'alias.json'
        alias.symlink_to(self.registration)
        directory_alias = self.root / 'alias-data'
        directory_alias.symlink_to(self.data, target_is_directory=True)
        for path in (alias, directory_alias / self.registration.name):
            with self.subTest(path=path):
                self.change_release(workspace=dict(json.loads(self.release.read_text())['workspace'], registration_path=str(path)))
                self.assertEqual(creator.read_creator_evidence(dict(self.binding, registration_path=str(path)))['status'], 'unavailable')
        self.pin()
        self.assertEqual(creator.read_creator_evidence(self.stable_binding())['status'], 'connected')
        release_alias = self.root / 'alias-release.json'
        release_alias.symlink_to(self.release)
        self.assertEqual(creator.read_creator_evidence(dict(self.stable_binding(), release_path=str(release_alias)))['status'], 'unavailable')
        uid = os.getuid()
        with patch.object(creator.os, 'getuid', return_value=uid+1):
            self.assertEqual(creator.read_creator_evidence(self.stable_binding())['status'], 'unavailable')

    def test_stable_binding_keeps_original_registration_content_hash(self):
        binding = self.stable_binding()
        registration = json.loads(self.registration.read_text())
        registration['workspace_id'] = 'other-workspace'
        self.registration.write_text(json.dumps(registration))
        self.assertEqual(creator.read_creator_evidence(binding)['status'], 'unavailable')
        self.change_release(workspace={'registration_path':str(self.registration),
                            'registration_sha256':hashlib.sha256(self.registration.read_bytes()).hexdigest()})
        self.assertEqual(creator.read_creator_evidence(binding)['status'], 'unavailable')

    def test_stable_binding_rejects_different_workspace_metadata_and_database_schema(self):
        binding = self.stable_binding()
        self.assertEqual(creator.read_creator_evidence(dict(binding, workspace_id='other-workspace'))['status'], 'unavailable')
        for key in ('workspace_id', 'purpose'):
            with self.subTest(key=key), closing(sqlite3.connect(self.database)) as db:
                original = db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()[0]
                db.execute('UPDATE metadata SET value=? WHERE key=?', ('other', key)); db.commit()
                self.assertEqual(creator.read_creator_evidence(binding)['status'], 'unavailable')
                db.execute('UPDATE metadata SET value=? WHERE key=?', (original, key)); db.commit()
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('PRAGMA user_version=1')
        self.assertEqual(creator.read_creator_evidence(binding)['status'], 'unavailable')

    def test_stable_binding_rejects_replaced_database_inode(self):
        binding = self.stable_binding()
        moved = self.database.with_suffix('.old')
        self.database.rename(moved)
        self.database.write_bytes(moved.read_bytes())
        self.assertEqual(creator.read_creator_evidence(binding)['status'], 'unavailable')

    def test_stable_binding_detects_release_or_registration_change_during_snapshot_read(self):
        original_release = self.release.read_bytes()
        original_registration = self.registration.read_bytes()
        connect = sqlite3.connect
        for target in (self.release, self.registration):
            with self.subTest(target=target.name):
                self.release.write_bytes(original_release)
                self.registration.write_bytes(original_registration)
                def changing_read(path, *args, **kwargs):
                    self.assertNotIn(str(self.database), str(path))
                    value = json.loads(target.read_text())
                    value['sequence'] = 999
                    target.write_text(json.dumps(value))
                    return connect(path, *args, **kwargs)
                with patch.object(creator.sqlite3, 'connect', side_effect=changing_read):
                    result = creator.read_creator_evidence(self.stable_binding())
                self.assertEqual(result['status'], 'unavailable')
                self.assertEqual(result['works'], [])
                self.assertFalse(result['identity_verified'])

    def test_stable_binding_bounds_and_rejects_invalid_or_nonfinite_release_json(self):
        original = self.release.read_text()
        invalid = ['[]', 'not-json', ' '*4_000_001, '['*1100 + '0' + ']'*1100]
        invalid += [original[:-1] + ',"extra":'+number+'}' for number in ('NaN', 'Infinity', '-Infinity', '1e1000000')]
        for raw in invalid:
            with self.subTest(prefix=raw[:45]):
                self.release.write_text(raw)
                self.assertEqual(creator.read_creator_evidence(self.stable_binding())['status'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
