"""Bounded creator fixtures: source identity, occurrence dates and zero source writes."""
from contextlib import closing
import hashlib
import json
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


if __name__ == '__main__':
    unittest.main()
