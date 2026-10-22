"""Fresh-system restore and independent interpreter selection."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / 'src'), str(BASE / 'src/core')]
import first_run
import portable_backup
import credentials
import runtime_paths
import tts
import agent_client


class InstallerRestoreChecks(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='myn-installer-')
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.legacy = mock.patch('data_location.legacy_candidates', return_value=[])
        self.legacy.start()
        self.addCleanup(self.legacy.stop)
        self.keys = mock.patch.dict(os.environ, {'ANTHROPIC_API_KEY': '', 'ELEVENLABS_API_KEY': ''})
        self.keys.start()
        self.addCleanup(self.keys.stop)

    def make_archive(self):
        root = self.folder / 'original'
        (root / 'database').mkdir(parents=True)
        import storage
        storage.init_db(root / 'database/jarvis.db')
        with closing(sqlite3.connect(root / 'database/jarvis.db')) as db:
            db.execute('CREATE TABLE example (value TEXT)')
            db.execute("INSERT INTO example VALUES ('preserved synthetic memory')")
            db.commit()
        credentials.save_key(root / 'config', 'synthetic-claude-credential')
        credentials.save_elevenlabs_key(root / 'config', 'synthetic-speech-credential')
        (root / 'config/tts-settings.json').write_text(json.dumps({'provider': 'qwen3-local'}))
        archive = self.folder / 'portable.mynbackup'
        portable_backup.create(root, archive, 'test-password-only')
        return archive

    def test_restore_needs_no_model_or_api_call_and_reprotects_keys(self):
        archive = self.make_archive()
        target = self.folder / 'new system'
        first_run.restore_empty(target, archive, 'test-password-only')
        with closing(sqlite3.connect(target / 'database/jarvis.db')) as db:
            self.assertEqual(db.execute('SELECT value FROM example').fetchone()[0], 'preserved synthetic memory')
        self.assertEqual(credentials.get_key(target / 'config'), 'synthetic-claude-credential')
        self.assertEqual(credentials.get_elevenlabs_key(target / 'config'), 'synthetic-speech-credential')
        self.assertEqual(tts.load_settings(target / 'config').provider, 'elevenlabs')
        self.assertFalse(first_run.needed(target))

    def test_wrong_password_does_not_create_target_database(self):
        target = self.folder / 'new'
        with self.assertRaises(ValueError):
            first_run.restore_empty(target, self.make_archive(), 'incorrect-password')
        self.assertFalse((target / 'database/jarvis.db').exists())

    def test_existing_library_is_never_replaced_by_first_start(self):
        archive = self.make_archive()
        target = self.folder / 'existing'
        (target / 'database').mkdir(parents=True)
        database = target / 'database/jarvis.db'
        database.write_bytes(b'original sentinel')
        with self.assertRaisesRegex(ValueError, 'Existing data'):
            first_run.restore_empty(target, archive, 'test-password-only')
        self.assertEqual(database.read_bytes(), b'original sentinel')

    def test_legacy_library_skips_welcome(self):
        legacy = self.folder / 'legacy'
        legacy.mkdir()
        (legacy / 'jarvis.db').touch()
        with mock.patch('data_location.legacy_candidates', return_value=[legacy]):
            self.assertFalse(first_run.needed(self.folder / 'new'))

    def test_bundled_workers_resolve_after_relocation(self):
        for kind in ('speech', 'agent'):
            executable = self.folder / 'runtime' / kind / 'python.exe'
            executable.parent.mkdir(parents=True)
            executable.touch()
            self.assertEqual(runtime_paths.worker_python(kind, self.folder), executable)
        package = self.folder / 'runtime/agent/Lib/site-packages/claude_agent_sdk'
        package.mkdir(parents=True)
        self.assertTrue(agent_client.installed(self.folder / 'runtime/agent/python.exe'))

    def test_source_workers_keep_existing_layout(self):
        self.assertIn('local-tts', str(runtime_paths.worker_python('speech', self.folder)))
        self.assertIn('local-agent', str(runtime_paths.worker_python('agent', self.folder)))

    def test_private_git_is_exposed_only_when_present(self):
        with mock.patch.dict(os.environ, {'PATH': 'original'}, clear=False):
            runtime_paths.activate(self.folder)
            self.assertEqual(os.environ['PATH'], 'original')
            bash = self.folder / 'runtime/git/bin/bash.exe'
            bash.parent.mkdir(parents=True)
            bash.touch()
            runtime_paths.activate(self.folder)
            self.assertEqual(os.environ['CLAUDE_CODE_GIT_BASH_PATH'], str(bash))
            self.assertTrue(os.environ['PATH'].endswith('original'))

    def test_saved_settings_cannot_reenable_retired_local_synthesis(self):
        tts.save_settings(self.folder, tts.TTSSettings(provider='qwen3-local', volume=0.23))
        saved = tts.load_settings(self.folder)
        self.assertEqual(saved.provider, 'elevenlabs')
        self.assertEqual(saved.volume, 0.23)


if __name__ == '__main__':
    unittest.main()
