"""Registered temporary artifacts, confirmed startup, and fail-closed cleanup."""
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE/'src'), str(BASE/'src/core')]
import development_cleanup as cleanup
import self_update


class Lifecycle(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix='myn-cleanup-')
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)/'MYN'
        for name, text in {'src/myn_gui.py': 'self_update.on_startup()\nself_update.confirm()\n',
                           'src/core/example.py': 'VALUE = 1\n', 'tests/test_example.py': '# test\n',
                           'AGENTS.md': '# Current\n## Permanent constraints\nKeep data.\n',
                           'personal_data/keep.txt': 'private'}.items():
            path = self.root/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8')
        self.work = self_update.prepare('task', self.root)
        self.backup = self.root/'self-update/manual-backup'
        shutil.copytree(self.work, self.backup)
        self.artifact = self.root/'test-current.log'
        self.artifact.write_text('verified', encoding='utf-8')
        (self.work/'checks.log').write_text('Tests passed', encoding='utf-8')

    def register(self, artifacts=()):
        return cleanup.schedule(self.work, self.backup, self.root, artifacts)

    def retire(self):
        return cleanup.finish_startup(cleanup.startup_batch(self.root), self.root)

    def test_registration_waits_and_success_keeps_recovery_and_evidence(self):
        ident = self.register([self.artifact])
        self.assertTrue(self.work.exists())
        self.assertEqual(self.retire(), [ident])
        self.assertFalse(self.work.exists())
        self.assertFalse(self.artifact.exists())
        self.assertFalse(self_update._base_path(self.work).exists())
        self.assertTrue((self.backup/'src/core/example.py').is_file())
        self.assertEqual((self.backup/'verification/cleanup/checks.log').read_text(), 'Tests passed')
        self.assertEqual((self.root/'personal_data/keep.txt').read_text(), 'private')
        self.assertTrue((self.root/'src/core/example.py').is_file())
        self.assertEqual(cleanup.startup_batch(self.root), {})

    def test_pending_or_failed_startup_does_not_delete_copy(self):
        self.register()
        pending = self.root/'self-update/pending.json'
        pending.write_text('{}')
        self.assertEqual(self.retire(), [])
        self.assertTrue(self.work.exists())
        pending.unlink()
        (self.root/'src/core/example.py').write_text('rolled back')
        self.assertEqual(self.retire(), [])
        self.assertTrue(self.work.exists())

    def test_changed_working_file_is_preserved(self):
        self.register()
        (self.work/'src/core/example.py').write_text('new ongoing edit')
        self.assertEqual(self.retire(), [])
        self.assertTrue(self.work.exists())

    def test_new_file_or_directory_is_preserved(self):
        self.register()
        (self.work/'new-folder').mkdir()
        self.assertEqual(self.retire(), [])
        (self.work/'new-folder').rmdir()
        (self.work/'unregistered.txt').write_text('new')
        self.assertEqual(self.retire(), [])

    def test_modified_backup_preserves_copy(self):
        self.register()
        (self.backup/'src/core/example.py').write_text('broken backup')
        self.assertEqual(self.retire(), [])
        self.assertTrue(self.work.exists())

    def test_only_explicit_unchanged_root_outputs_are_removed(self):
        other = self.root/'test-unregistered.log'
        other.write_text('keep')
        self.register([self.artifact])
        self.artifact.write_text('new results')
        self.assertEqual(len(self.retire()), 1)
        self.assertEqual(self.artifact.read_text(), 'new results')
        self.assertTrue(other.exists())

    def test_external_and_parent_paths_are_rejected(self):
        for work in (self.root, self.root/'src', self.work.parent, self.root.parent):
            with self.subTest(work=work), self.assertRaises(ValueError):
                cleanup.schedule(work, self.backup, self.root)

    def test_unregistered_copy_and_unverified_install_are_rejected(self):
        self_update._base_path(self.work).unlink()
        with self.assertRaises(ValueError):
            self.register()
        self.work = self_update.prepare('task', self.root)
        (self.work/'src/core/example.py').write_text('not deployed')
        with self.assertRaises(ValueError):
            self.register()

    def test_private_data_and_links_refuse_cleanup(self):
        private = self.work/'private.db'
        private.write_text('keep')
        with self.assertRaises(ValueError):
            self.register()
        private.unlink()
        with mock.patch.object(Path, 'is_symlink', return_value=True), self.assertRaises(ValueError):
            self.register()

    def test_arbitrary_artifact_paths_are_rejected(self):
        for path in (self.root/'AGENTS.md', self.root/'src/core/example.py',
                     self.root/'personal_data/keep.txt', self.root.parent/'test-outside.log'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.register([path])

    def test_repeated_finish_is_idempotent(self):
        first = self.register()
        self.assertEqual(self.register(), first)
        self.assertEqual(len(cleanup.startup_batch(self.root)), 1)
        self.assertEqual(self.retire(), [first])
        self.assertEqual(self.retire(), [])

    def test_corrupt_queue_entry_does_not_hide_valid_work(self):
        ident = self.register()
        folder = self.root/'self-update/cleanup'
        (folder/'corrupt.json').write_text('not json')
        (folder/'invalid.json').write_text(json.dumps({'id':'../outside','token':'wrong'}))
        self.assertEqual(len(cleanup.startup_batch(self.root)), 1)
        self.assertEqual(self.retire(), [ident])

    def test_registrations_after_boot_wait_for_next_startup(self):
        boot = cleanup.startup_batch(self.root)
        self.register()
        self.assertEqual(cleanup.finish_startup(boot, self.root), [])
        self.assertTrue(self.work.exists())

    def test_reused_task_invalidates_old_startup_batch(self):
        self.register()
        old = cleanup.startup_batch(self.root)
        self.work = self_update.prepare('task', self.root)
        self.register()
        self.assertEqual(cleanup.finish_startup(old, self.root), [])
        self.assertTrue(self.work.exists())

    def test_locked_file_leaves_retryable_cleanup(self):
        self.register()
        original = Path.unlink
        def unlink(path, *args, **kwargs):
            if path.name == 'example.py':
                raise PermissionError('busy')
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, 'unlink', unlink):
            self.assertEqual(self.retire(), [])
        self.assertTrue(self.work.exists())
        self.assertEqual(len(self.retire()), 1)
        self.assertFalse(self.work.exists())

    def test_native_self_update_registers_and_waits_for_confirmation(self):
        (self.work/'src/core/example.py').write_text('VALUE = 2\n')
        ok, backup = self_update.apply(self.work, 'updated', self.root, checker=lambda root: (True, 'ok'))
        self.assertTrue(ok, backup)
        batch = cleanup.startup_batch(self.root)
        self.assertEqual(len(batch), 1)
        self.assertEqual(cleanup.finish_startup(batch, self.root), [])
        self_update.on_startup(self.root)
        self_update.confirm(self.root)
        self.assertEqual(len(cleanup.finish_startup(batch, self.root)), 1)

    def test_native_rollback_keeps_failed_work(self):
        (self.work/'src/core/example.py').write_text('VALUE = 2\n')
        self.assertTrue(self_update.apply(self.work, 'updated', self.root, checker=lambda root: (True, 'ok'))[0])
        batch = cleanup.startup_batch(self.root)
        self_update.on_startup(self.root)
        self_update.on_startup(self.root)
        self.assertEqual(cleanup.finish_startup(batch, self.root), [])
        self.assertTrue(self.work.exists())


class Integration(unittest.TestCase):
    def test_direct_install_keeps_copy_until_startup_and_failed_install_keeps_evidence(self):
        import myn_gui
        win = myn_gui.MYNWindow.__new__(myn_gui.MYNWindow)
        win.self_fix = {'work':'registered-copy'}
        win._self_fix_discard = mock.Mock()
        win._self_fix_fail = mock.Mock()
        win.append = win.stop_speech = mock.Mock()
        win.root = mock.Mock()
        win._self_fix_applied(True, 'backup')
        win._self_fix_discard.assert_called_once_with({'work':'registered-copy'}, keep_work=True)
        win._self_fix_applied(False, 'failed')
        win._self_fix_fail.assert_called_once_with('failed', preserve_work=True)

    def test_evolution_install_does_not_discard_before_confirmed_startup(self):
        import myn_gui
        win = myn_gui.MYNWindow.__new__(myn_gui.MYNWindow)
        win._evolution_config = lambda: Path('test-only')
        win._self_fix_discard = mock.Mock()
        win.append = win.stop_speech = win._evolution_changed = mock.Mock()
        win.root = mock.Mock()
        with mock.patch('gui_evolution.evolution.update', return_value={'title':'test'}):
            win._evolution_applied('p1', 'registered-copy', True, 'backup')
        win._self_fix_discard.assert_called_once_with({'work':'registered-copy'}, keep_work=True)

    def test_gui_waits_for_health_and_does_not_clean_on_the_ui_thread(self):
        import myn_gui
        win = myn_gui.MYNWindow.__new__(myn_gui.MYNWindow)
        win.root = mock.Mock()
        win.self_fix = win.agent_runner = None
        with mock.patch.object(cleanup, 'startup_batch', return_value={'id':'token'}), \
                mock.patch.object(cleanup, 'finish_startup') as finish, \
                mock.patch('myn_gui.threading.Thread') as thread:
            win._cleanup_startup()
            delay, callback = win.root.after.call_args.args
            self.assertGreater(delay, self_update.CONFIRM_AFTER_S * 1000)
            finish.assert_not_called()
            win.self_fix = object()
            callback()
            thread.assert_not_called()
            win.self_fix = None
            callback()
            thread.assert_called_once()
            finish.assert_not_called()
            thread.call_args.kwargs['target']()
            finish.assert_called_once_with({'id':'token'})

    def test_developer_finish_registers_explicit_copy_and_preserves_bad_paths(self):
        spec = importlib.util.spec_from_file_location('cleanup_finish', BASE/'tools/dev_finish.py')
        finish = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(finish)
        with mock.patch.object(cleanup, 'schedule', return_value='registered') as schedule:
            finish.register_cleanup({})
            schedule.assert_not_called()
            finish.register_cleanup({'cleanup_workdir':'work', 'cleanup_backup':'backup', 'cleanup_artifacts':['test.log']})
            schedule.assert_called_once_with('work', 'backup', finish.ROOT, ['test.log'])
            schedule.side_effect = ValueError('unsafe path')
            finish.register_cleanup({'cleanup_workdir':'bad', 'cleanup_backup':'backup'})
