import base64
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from attachments import AttachmentStore


class AttachmentLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = AttachmentStore(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def upload(self, draft_id='draft-a'):
        return self.store.upload('draft.txt', base64.b64encode(b'synthetic').decode(), draft_id)

    def test_other_draft_survives_removal_and_restart(self):
        item = self.upload()
        self.store.retain(item['id'], 'draft-b')
        result = self.store.remove(item['id'], 'draft-a')
        self.assertEqual(result['retained'], 'other_draft')
        reopened = AttachmentStore(self.temp.name)
        self.assertEqual(reopened._load(item['id'])['draft_refs'], ['draft-b'])
        self.assertTrue(reopened.remove(item['id'], 'draft-b')['deleted'])
        self.assertFalse((reopened.root / item['id']).exists())

    def test_sent_and_legacy_attachments_are_not_deleted(self):
        sent = self.upload()
        self.store.bind([sent['id']], 'session')
        self.assertEqual(self.store.remove(sent['id'], 'draft-a')['retained'], 'sent')
        self.assertEqual(self.store.read(sent['id'], 'session')['text'], 'synthetic')
        legacy = self.upload(None)
        self.store.retain(legacy['id'], 'restored-legacy')
        self.assertEqual(self.store.remove(legacy['id'], 'restored-legacy')['retained'], 'legacy')

    def test_wrong_draft_and_symlink_cannot_delete(self):
        item = self.upload()
        with self.assertRaises(PermissionError): self.store.remove(item['id'], 'wrong')
        folder = self.store.root / item['id']
        renamed = self.store.root / 'kept'; folder.rename(renamed); folder.symlink_to(renamed)
        with self.assertRaises(ValueError): self.store.remove(item['id'], 'draft-a')
        self.assertTrue((renamed / 'source.txt').exists())

    def test_remove_retry_after_lost_receipt_is_idempotent_but_rejects_unsafe_paths(self):
        item = self.upload()
        self.assertTrue(self.store.remove(item['id'], 'draft-a')['deleted'])
        self.assertTrue(self.store.remove(item['id'], 'draft-a')['deleted'])
        for bad_id, bad_draft in [('../outside', 'draft-a'), (item['id'], '../draft')]:
            with self.assertRaises(ValueError): self.store.remove(bad_id, bad_draft)
        folder = self.store.root / item['id']; folder.write_text('not a directory')
        with self.assertRaises(ValueError): self.store.remove(item['id'], 'draft-a')
        folder.unlink(); folder.symlink_to(self.store.root / 'missing-target')
        with self.assertRaises(ValueError): self.store.remove(item['id'], 'draft-a')
        folder.unlink()
        root = self.store.root; kept = root.with_name('kept-root'); root.rename(kept); root.symlink_to(kept)
        with self.assertRaises(ValueError): self.store.remove(item['id'], 'draft-a')
        root.unlink(); kept.rename(root)
        original = Path.lstat
        def denied(path, *args, **kw):
            if path == folder: raise PermissionError('synthetic denied')
            return original(path, *args, **kw)
        with patch.object(Path, 'lstat', denied):
            with self.assertRaises(PermissionError): self.store.remove(item['id'], 'draft-a')

    def test_binding_and_removal_are_mutually_exclusive(self):
        item = self.upload()
        entered, release, removed = threading.Event(), threading.Event(), threading.Event()
        original = self.store._save
        def save(meta):
            entered.set(); release.wait(2); original(meta)
        results = []
        with patch.object(self.store, '_save', save):
            binder = threading.Thread(target=lambda: self.store.bind([item['id']], 'session'))
            binder.start(); self.assertTrue(entered.wait(2))
            def remove():
                results.append(self.store.remove(item['id'], 'draft-a')); removed.set()
            remover = threading.Thread(target=remove); remover.start()
            self.assertFalse(removed.wait(.1))
            release.set(); binder.join(2); remover.join(2)
        self.assertEqual(results[0]['retained'], 'sent')
        self.assertEqual(self.store.read(item['id'], 'session')['text'], 'synthetic')

    def test_slow_parse_reserves_quota_but_not_attachment_lock(self):
        entered, release = threading.Event(), threading.Event()
        errors = []
        def parse(*args):
            entered.set(); release.wait(2); raise ValueError('synthetic parse failure')
        def upload():
            try: self.store.upload('draft.docx', base64.b64encode(b'123456').decode(), 'a')
            except ValueError as exc: errors.append(str(exc))
        with patch('attachments.MAX_TOTAL_BYTES', 10), patch('attachments._office', parse):
            worker = threading.Thread(target=upload); worker.start(); self.assertTrue(entered.wait(2))
            acquired = self.store.lock.acquire(timeout=.1)
            self.assertTrue(acquired)
            if acquired: self.store.lock.release()
            with self.assertRaisesRegex(ValueError, '500 MB'):
                self.store.upload('second.txt', base64.b64encode(b'123456').decode(), 'b')
            release.set(); worker.join(2)
            self.store.upload('after.txt', base64.b64encode(b'123456').decode(), 'c')
        self.assertEqual(errors, ['synthetic parse failure'])
        self.assertEqual(len(list(self.store.root.glob('*/source*'))), 1)

    def test_removal_wins_before_bind_without_creating_a_sent_attachment(self):
        import shutil
        item = self.upload()
        entered, release = threading.Event(), threading.Event()
        errors = []
        original = shutil.rmtree
        def remove_tree(folder):
            entered.set(); release.wait(2); original(folder)
        def bind():
            try: self.store.bind([item['id']], 'session')
            except ValueError as exc: errors.append(str(exc))
        with patch('attachments.shutil.rmtree', remove_tree):
            remover = threading.Thread(target=lambda: self.store.remove(item['id'], 'draft-a'))
            remover.start(); self.assertTrue(entered.wait(2))
            binder = threading.Thread(target=bind); binder.start()
            release.set(); remover.join(2); binder.join(2)
        self.assertEqual(len(errors), 1)
        self.assertFalse((self.store.root / item['id']).exists())


if __name__ == '__main__':
    unittest.main()
