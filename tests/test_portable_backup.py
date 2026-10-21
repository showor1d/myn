"""Two-machine backups, data fidelity, authenticated imports and failure recovery."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / 'src'), str(BASE / 'src/core')]
import portable_backup as pb
import credentials
import preferences
import storage
import chat


class CompleteBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='myn-complete-backup-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.source = self.base / 'old-machine/personal_data'
        self.target = self.base / 'new-machine/personal_data'
        self.source.mkdir(parents=True)
        self.target.mkdir(parents=True)
        self.password = 'test-only-transfer-password'
        self.archive = self.base / 'transfer.mynbackup'
        # This test never reads or exports a real credential/environment value.
        self.env = patch.dict(os.environ, {'ANTHROPIC_API_KEY': '', 'ELEVENLABS_API_KEY': ''})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.session = chat.ChatSession(self.source / 'database/jarvis.db')
        self.session.record_input('明天要学习，也喜欢简短的回复。')
        store = self.session.preferences.store
        store.add_rule('先说结论', 'other', 'less', [preferences.ALL], source='manual')
        # Preserve a pre-policy disabled setting byte-for-byte in a transferred backup.
        with closing(store._connect()) as connection, connection:
            connection.execute("UPDATE meta SET value='1' WHERE key='learning_paused'")
        self.write('config/tts-settings.json', {'volume': .32, 'provider': 'elevenlabs', 'voice_id': 'voice-test'})
        self.write('config/stt-settings.json', {'device': 'Headset mic', 'wake_words': '小麦'})
        import wake_learning
        for seed in range(3):
            wake_learning.enroll(self.source / 'config', [[(seed+i+j)/100 for j in range(13)] for i in range(30)])
        wake_learning.adapt(self.source / 'config', 'confirmed-transfer', [[(7+i+j)/100 for j in range(13)] for i in range(30)])
        self.write('config/evolution-points.json', [{'id': 'x', 'status': 'proposed', 'plan': {'text': '保留方案'}}])
        self.write('config/evolution-settings.json', {'monthly_usd': 50, 'task_usd': 15})
        self.write('config/scheduled-tasks.json', [{'id': 'alarm1', 'repeat': 'weekdays', 'skip_dates': ['2026-10-22']}])
        self.write('config/calendar.json', [{'title': '课程'}])
        self.write('config/agent-settings.json', {'workspace': str(self.source / 'documents')})
        self.write('config/file-access.json', {key: [str(self.source / 'documents')] for key in ('read', 'write', 'delete', 'execute')})
        self.write('media/.player-settings.json', {'volume': .21})
        import audio_console
        audio_console.save(self.source / 'media', dict(audio_console.defaults(), pitch=-3, effect='pitch'))
        self.write('media/Music/example.lrc', '[00:01.00]示例歌词', raw=True)
        self.write('media/Music/example.mp3', 'fake-media-payload', raw=True)
        self.write('config/codex-usage.jsonl', '{"input_tokens":1234}\n', raw=True)
        credentials.save_key(self.source / 'config', 'fake-test-api-key')
        credentials.save_elevenlabs_key(self.source / 'config', 'fake-test-voice-key')
        expenses = self.source / 'config/expenses.sqlite3'
        with closing(sqlite3.connect(str(expenses))) as db, db:
            db.execute('CREATE TABLE expenses (title TEXT, usd REAL)')
            db.execute("INSERT INTO expenses VALUES ('Lunch', 12.5)")

    def write(self, relative, value, raw=False):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if raw else json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def create(self):
        return pb.create(self.source, self.archive, self.password)

    def test_complete_transfer_fidelity_and_existing_destination_kept(self):
        self.create()
        self.assertNotIn(b'fake-test-api-key', self.archive.read_bytes())
        stage = pb.prepare(self.target, self.archive, self.password)
        (self.target / 'local-old-file.txt').write_text('do not lose me')
        old = pb.install(self.target, stage)
        import wake_learning
        self.assertEqual(wake_learning.active(self.target / 'config'), wake_learning.active(self.source / 'config'))
        self.assertEqual(wake_learning.load(self.target / 'config')['adaptive'][0]['id'], 'confirmed-transfer')
        self.assertEqual((self.target/'config/wake-voice.last-good.json').read_bytes(),
                         (self.source/'config/wake-voice.last-good.json').read_bytes())
        self.assertEqual((old / 'local-old-file.txt').read_text(), 'do not lose me')
        for file in ('media/Music/example.mp3', 'media/Music/example.lrc', 'media/.player-settings.json', 'media/.audio-console.json',
                     'config/calendar.json', 'config/evolution-settings.json', 'config/scheduled-tasks.json',
                     'config/stt-settings.json', 'config/codex-usage.jsonl', 'config/tts-settings.json'):
            self.assertEqual((self.target / file).read_bytes(), (self.source / file).read_bytes(), file)
        restored = chat.ChatSession(self.target / 'database/jarvis.db')
        self.assertEqual(restored.preferences.store.rules(), self.session.preferences.store.rules())
        self.assertFalse(restored.preferences.store.learning_paused)
        self.assertEqual(restored.preferences.store.setting('learning_paused'), '1')
        self.assertEqual(credentials.get_key(self.target / 'config'), 'fake-test-api-key')
        self.assertEqual(credentials.get_elevenlabs_key(self.target / 'config'), 'fake-test-voice-key')
        cfg = json.loads((self.target / 'config/agent-settings.json').read_text())
        self.assertEqual(cfg['workspace'], str(self.target / 'documents'))
        with closing(sqlite3.connect(str(self.target / 'config/expenses.sqlite3'))) as db:
            self.assertEqual(db.execute('SELECT * FROM expenses').fetchall(), [('Lunch', 12.5)])
        with closing(sqlite3.connect(str(restored.db_path))) as db:
            self.assertEqual(db.execute('SELECT raw_text FROM sent_messages').fetchone()[0], '明天要学习，也喜欢简短的回复。')

    def test_incorrect_password_and_tamper_never_change_destination(self):
        self.create()
        sentinel = self.target / 'keep.txt'
        sentinel.write_text('untouched')
        with self.assertRaises(ValueError):
            pb.prepare(self.target, self.archive, 'wrong-password')
        raw = bytearray(self.archive.read_bytes())
        raw[len(raw)//2] ^= 1
        self.archive.write_bytes(raw)
        with self.assertRaises(ValueError):
            pb.prepare(self.target, self.archive, self.password)
        self.assertEqual(sentinel.read_text(), 'untouched')
        self.assertFalse(list(pb.transfer_dir(self.target).glob('restore-*')))

    def test_wal_database_and_old_backups_are_included(self):
        database = self.source / 'config/expenses.sqlite3'
        db = sqlite3.connect(str(database))
        self.addCleanup(db.close)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute("INSERT INTO expenses VALUES ('Coffee', 3)")
        db.commit()
        self.session.preferences.store.backup('manual')
        self.create()
        stage = pb.prepare(self.target, self.archive, self.password)
        with closing(sqlite3.connect(str(stage / 'config/expenses.sqlite3'))) as copied:
            self.assertEqual(copied.execute('SELECT COUNT(*) FROM expenses').fetchone()[0], 2)
        self.assertTrue(list((stage / 'preferences/backups').glob('*.db')))
        self.assertFalse(list(stage.rglob('*-wal')))

    def test_failed_swap_and_interrupted_swap_restore_original(self):
        self.create()
        stage = pb.prepare(self.target, self.archive, self.password)
        (self.target / 'keep.txt').write_text('original')
        rename = Path.rename
        def fail(path, dest):
            if path == stage:
                raise OSError('simulated disk error')
            return rename(path, dest)
        with patch.object(Path, 'rename', fail), self.assertRaises(OSError):
            pb.install(self.target, stage)
        self.assertEqual((self.target / 'keep.txt').read_text(), 'original')
        directory = pb.transfer_dir(self.target)
        old = directory / 'before-crash'
        self.target.rename(old)
        stage.rename(self.target)
        pb._json(directory / 'transaction.json', {'old': old.name, 'stage': stage.name})
        pb.recover(self.target)
        self.assertEqual((self.target / 'keep.txt').read_text(), 'original')
        self.assertTrue(list(directory.glob('interrupted-*')))

    def altered_archive(self, edit):
        self.create()
        raw = self.base / 'plain.zip'
        pb._crypt_file(self.archive, raw, self.password, False)
        with zipfile.ZipFile(raw) as archive:
            contents = {n: archive.read(n) for n in archive.namelist()}
        manifest = json.loads(contents['manifest.json'])
        edit(manifest, contents)
        contents['manifest.json'] = json.dumps(manifest).encode()
        with zipfile.ZipFile(raw, 'w') as archive:
            for name, content in contents.items():
                archive.writestr(name, content)
        changed = self.base / 'changed.mynbackup'
        pb._crypt_file(raw, changed, self.password, True)
        return changed

    def test_authenticated_but_invalid_manifest_and_traversal_rejected(self):
        def edit(manifest, contents):
            name = 'media/Music/example.mp3'
            manifest['files']['../../outside.txt'] = manifest['files'].pop(name)
            contents['data/../../outside.txt'] = contents.pop('data/' + name)
        changed = self.altered_archive(edit)
        with self.assertRaises(ValueError):
            pb.prepare(self.target, changed, self.password)
        self.assertFalse((self.base / 'outside.txt').exists())

    def test_future_format_rejected(self):
        changed = self.altered_archive(lambda manifest, _: manifest.update(version=999))
        with self.assertRaisesRegex(ValueError, 'version'):
            pb.prepare(self.target, changed, self.password)

    def test_pending_code_copy_cannot_auto_apply_on_new_machine(self):
        self.write('config/evolution-points.json', [{'id': 'x', 'status': 'built', 'work': 'D:/old-copy',
                                                   'plan_approved': True, 'plan': {'text': 'keep proposal'}}])
        self.create()
        stage = pb.prepare(self.target, self.archive, self.password)
        point = json.loads((stage / 'config/evolution-points.json').read_text(encoding='utf-8'))[0]
        self.assertEqual(point['status'], 'failed')
        self.assertIsNone(point['work'])
        self.assertEqual(point['plan']['text'], 'keep proposal')

    def test_backup_files_never_overwritten_and_archive_recursion_excluded(self):
        self.write('portable-backups/previous.mynbackup', 'previous file', raw=True)
        self.create()
        before = pb.digest(self.archive)
        with self.assertRaises(ValueError):
            self.create()
        self.assertEqual(pb.digest(self.archive), before)
        stage = pb.prepare(self.target, self.archive, self.password)
        self.assertFalse((stage / 'portable-backups').exists())

    def test_staging_changed_after_validation_is_not_installed(self):
        self.create()
        stage = pb.prepare(self.target, self.archive, self.password)
        (self.target / 'keep.txt').write_text('original')
        (stage / 'media/Music/example.mp3').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'changed'):
            pb.install(self.target, stage)
        self.assertEqual((self.target / 'keep.txt').read_text(), 'original')

    def test_credentials_rebound_to_the_receiving_windows_identity(self):
        def machine(identity):
            def crypt(data, protect):
                if protect:
                    return identity + data
                if not data.startswith(identity):
                    raise ValueError('wrong Windows user')
                return data[len(identity):]
            return crypt
        with patch.object(credentials, '_crypt', side_effect=machine(b'OLD:')):
            credentials.save_key(self.source / 'config', 'portable-claude-key')
            credentials.save_elevenlabs_key(self.source / 'config', 'portable-voice-key')
            self.create()
        with patch.object(credentials, '_crypt', side_effect=machine(b'NEW:')):
            stage = pb.prepare(self.target, self.archive, self.password)
            self.assertEqual(credentials.get_key(stage / 'config'), 'portable-claude-key')
            self.assertEqual(credentials.get_elevenlabs_key(stage / 'config'), 'portable-voice-key')

    def test_restored_library_starts_without_revalidating_another_computers_legacy_path(self):
        import data_location
        self.write('config/migration.json', {'source': str(self.source / 'database/jarvis.db'), 'source_digest': 'different-machine'})
        self.create()
        stage = pb.prepare(self.target, self.archive, self.password)
        pb.install(self.target, stage)
        with patch.dict(os.environ, {'MYN_PERSONAL_DATA_DIR': str(self.target), 'MYN_DATA_DIR': str(self.target / 'database'),
                                     'MYN_CONFIG_DIR': str(self.target / 'config')}):
            database, mode = data_location.prepare_data()
        self.assertEqual(database, self.target / 'database/jarvis.db')
        self.assertEqual(mode, 'existing')


if __name__ == '__main__':
    unittest.main()
