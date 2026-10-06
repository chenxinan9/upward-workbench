"""Safety regressions for the embedded one-command installer; no network writes."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
import zipfile

SOURCE = Path(__file__).resolve().parents[1] / 'install.sh'
PAYLOAD = SOURCE.read_text().split("<<'UPWARD_PYTHON'\n", 1)[1].rsplit('\nUPWARD_PYTHON', 1)[0]
installer = types.ModuleType('quickstart_under_test')
exec(compile(PAYLOAD, str(SOURCE), 'exec'), installer.__dict__)


class Response(io.BytesIO):
    url = 'https://release-assets.githubusercontent.com/example.zip'


class QuickstartTests(unittest.TestCase):
    def test_existing_paths_and_overlap_are_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); original = root / 'existing'; original.mkdir()
            marker = original / 'keep.txt'; marker.write_text('keep')
            for install, data in [(original, root / 'data'), (root / 'app', original),
                                  (root / 'same', root / 'same'), (root / 'app', root / 'app/data')]:
                with self.assertRaises(ValueError): installer.new_paths(install, data)
            link = root / 'dangling'; link.symlink_to(root / 'missing')
            with self.assertRaises(ValueError): installer.new_paths(link, root / 'data')
            self.assertEqual(marker.read_text(), 'keep')
            self.assertFalse((root / 'data').exists())

    def test_bad_download_is_never_extracted(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(installer.urllib.request, 'urlopen', return_value=Response(b'tampered')):
                with self.assertRaisesRegex(ValueError, '校验失败'):
                    installer.download_verified(Path(d) / 'release.zip')
            self.assertFalse((Path(d) / 'growth-desk').exists())

    def test_bounded_download(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(installer, 'MAX_DOWNLOAD', 2), patch.object(installer.urllib.request, 'urlopen', return_value=Response(b'123')):
                with self.assertRaisesRegex(ValueError, '大小限制'):
                    installer.download_verified(Path(d) / 'release.zip')

    def test_bad_archive_paths_and_unlisted_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            archive = root / 'bad.zip'
            with zipfile.ZipFile(archive, 'w') as z: z.writestr('growth-desk/../escape', 'no')
            with self.assertRaisesRegex(ValueError, '异常路径'): installer.extract_verified(archive, root)
            self.assertFalse((root / 'escape').exists())
            with zipfile.ZipFile(archive, 'w') as z:
                info = zipfile.ZipInfo('growth-desk/link'); info.external_attr = 0o120777 << 16
                z.writestr(info, '/tmp/target')
            with self.assertRaises(ValueError): installer.extract_verified(archive, root)
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('growth-desk/release-manifest.json', json.dumps({'files': []}))
                z.writestr('growth-desk/extra', 'not-listed')
            with self.assertRaisesRegex(ValueError, '未登记'): installer.extract_verified(archive, root)

    def test_fixed_release_and_helper_are_self_contained(self):
        self.assertEqual(installer.ARCHIVE_SHA256, 'd7feb1bd16dac189fbd897b0f3b97d56813b94423834e6b969ab93daa5cb9eae')
        self.assertIn('/releases/download/v0.2.1-rc2/', installer.ARCHIVE_URL)
        self.assertNotIn('raw.githubusercontent.com', PAYLOAD)
        self.assertNotIn('shell=True', PAYLOAD)


if __name__ == '__main__': unittest.main()
