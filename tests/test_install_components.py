"""Downloads and uninstall never trust unverified bytes or unrelated paths."""
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'src'))
import install_components as setup


class Reply(io.BytesIO):
    def __init__(self, data, status=200, headers=None):
        super().__init__(data)
        self.status = status
        self.headers = headers or {'Content-Length': str(len(data))}


class ComponentChecks(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='myn-component-test-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.report = lambda *_: None

    def spec(self, data):
        return {'url': 'https://example.invalid/component', 'sha256': hashlib.sha256(data).hexdigest()}

    def test_verified_download_and_cache(self):
        target = self.root / 'download'
        opener = mock.Mock(return_value=Reply(b'verified'))
        setup.download(self.spec(b'verified'), target, self.report, opener)
        setup.download(self.spec(b'verified'), target, self.report, opener)
        self.assertEqual(target.read_bytes(), b'verified')
        self.assertEqual(opener.call_count, 1)

    def test_interrupted_download_resumes_at_verified_range(self):
        target = self.root / 'download'
        target.with_suffix('.partial').write_bytes(b'first')
        opener = mock.Mock(return_value=Reply(b'last', 206,
            {'Content-Length': '4', 'Content-Range': 'bytes 5-8/9'}))
        setup.download(self.spec(b'firstlast'), target, self.report, opener)
        self.assertEqual(opener.call_args.args[0].get_header('Range'), 'bytes=5-')
        self.assertEqual(target.read_bytes(), b'firstlast')

    def test_completed_partial_is_verified_without_network(self):
        target = self.root / 'download'
        target.with_suffix('.partial').write_bytes(b'complete')
        opener = mock.Mock()
        setup.download(self.spec(b'complete'), target, self.report, opener)
        opener.assert_not_called()

    def test_hash_mismatch_cannot_be_installed(self):
        target = self.root / 'download'
        with mock.patch.object(setup.time, 'sleep'):
            with self.assertRaises(ValueError):
                setup.download(self.spec(b'good'), target, self.report, lambda *_a, **_k: Reply(b'bad'))
        self.assertFalse(target.exists())

    def test_paths_cannot_escape_root(self):
        for name in ('../personal_data', 'C:/private', '/private', 'runtime/../../private'):
            with self.assertRaises((ValueError, OSError)):
                setup.inside(self.root, name)

    def fixture(self, bad=False):
        archive = self.root / 'synthetic.whl'
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('../escape' if bad else 'torch/__init__.py', 'synthetic component')
        model = self.root / 'synthetic-model'
        model.write_bytes(b'synthetic weights')
        (self.root / 'src').mkdir()
        specs = {'torch': self.spec(archive.read_bytes()), 'whisper': self.spec(model.read_bytes())}
        (self.root / 'src/component-lock.json').write_text(json.dumps(specs))
        def download(spec, target, report):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(archive if spec == specs['torch'] else model, target)
        return download

    def test_install_and_uninstall_preserve_data_and_unknown_files(self):
        downloader = self.fixture()
        with mock.patch.object(setup, 'download', downloader), mock.patch.object(setup.subprocess, 'run', return_value=mock.Mock(returncode=0)):
            setup.install(self.root, self.report)
        unknown = self.root / setup.SITE / 'torch/user-added.txt'
        unknown.write_text('preserve')
        personal = self.root / 'personal_data'
        personal.mkdir()
        (personal / 'sentinel').write_text('preserve')
        self.assertTrue((self.root / 'installation-ready.json').exists())
        setup.uninstall(self.root)
        self.assertFalse((self.root / setup.MODEL).exists())
        self.assertTrue(unknown.exists())
        self.assertTrue((personal / 'sentinel').exists())

    def test_modified_component_is_preserved_on_uninstall(self):
        path = setup.inside(self.root, setup.MODEL)
        path.parent.mkdir(parents=True)
        path.write_bytes(b'modified')
        (self.root / 'component-receipt.json').write_text(json.dumps({'files': {setup.MODEL: 'not-the-same'}}))
        setup.uninstall(self.root)
        self.assertEqual(path.read_bytes(), b'modified')

    def test_archive_traversal_is_rejected_before_install(self):
        downloader = self.fixture(bad=True)
        with mock.patch.object(setup, 'download', downloader):
            with self.assertRaises(ValueError):
                setup.install(self.root, self.report)
        self.assertFalse((self.root / 'escape').exists())


if __name__ == '__main__':
    unittest.main()
