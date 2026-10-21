"""Unified UI, restore integration and backup compatibility (isolated data only)."""
import os
import queue
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / 'src'), str(BASE / 'src/core')]
import chat
import stt
import sheets
from settings_navigation import AGENTS_SECTIONS
from gui_settings import SettingsMixin
from web_backup import BackupPanel


class BackupUI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='myn-backup-ui-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'personal_data'
        self.path = self.root / 'database/jarvis.db'
        self.session = chat.ChatSession(self.path)

    def test_one_entry_and_legacy_voice_aliases(self):
        self.assertEqual(AGENTS_SECTIONS, ())
        self.assertFalse(any(label in ('交流偏好', '个人资料库') for _, label, _ in AGENTS_SECTIONS))
        for text, action in (('打开备份', 'personal'), ('打开个人资料库', 'personal'), ('打开交流偏好', 'preferences')):
            self.assertEqual(stt.voice_command(text), action)

    def test_unified_panel_keeps_preference_management_and_old_backup_actions(self):
        win = SimpleNamespace(session=self.session)
        panel = BackupPanel(win, self.session.preferences)
        host = sheets.SheetHost(lambda cmd: None)
        host.open(panel)
        tabs = panel.body()[0]['tabs']
        self.assertEqual([tab['key'] for tab in tabs], ['complete', 'rules', 'feedback', 'examples', 'history', 'effect', 'data'])
        self.assertNotIn('Pause automatic learning', str(tabs[1]))
        with patch.object(win.session.preferences.store, 'backup', wraps=win.session.preferences.store.backup) as pref_backup:
            panel.act('library_create', {})
            pref_backup.assert_called_once_with('manual')
        self.assertTrue(list((self.root / 'backups').glob('*.db')))

    def test_old_database_restore_rebinds_all_current_callbacks(self):
        win = SettingsMixin()
        win.session = self.session
        callbacks = {name: Mock() for name in ('start_agent', 'agent_status', 'evolution_update', 'show_agenda',
                    'agenda_context_provider', 'reader_context_provider', 'music_control', 'microphone_control',
                    'interaction_notify', 'expense_notify', 'voice_ready_notify')}
        for name, callback in callbacks.items():
            setattr(win.session, name, callback)
        panel = BackupPanel(win, self.session.preferences)
        win._sheets = SimpleNamespace(panel=panel)
        win._reconnect_session()
        self.assertIsNot(win.session, self.session)
        self.assertIs(panel.store, win.session.preferences.store)
        for name, callback in callbacks.items():
            self.assertIs(getattr(win.session, name), callback)

    def test_restore_worker_waits_before_swap_then_relaunches(self):
        import backup_restore_worker as worker
        import portable_backup as pb
        root = self.root
        folder = pb.transfer_dir(root)
        request = folder / 'request.json'
        pb._json(request, {'root': str(root), 'stage': str(folder / 'restore-test'), 'pid': 123})
        calls = []
        with patch.object(worker, 'wait_for_exit', side_effect=lambda pid: calls.append('wait')), \
             patch.object(pb, 'install', side_effect=lambda *a: calls.append('install') or folder / 'before-test'), \
             patch.object(worker.subprocess, 'Popen', side_effect=lambda *a, **kw: calls.append('restart')):
            worker.main(request)
        self.assertEqual(calls, ['wait', 'install', 'restart'])
        self.assertFalse(request.exists())
        self.assertTrue((folder / 'result.json').exists())

    def test_export_and_import_button_flow_uses_real_encrypted_archive(self):
        import backup_ui
        root = self.root
        archive = root / 'portable-backups/button-flow.mynbackup'
        win = SimpleNamespace(session=self.session, root=Mock(), events=queue.Queue(),
                              web_active=False, close=Mock(), _show_error=Mock(),
                              _ask_user=lambda title, text, then, **kw: then(True))
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': '', 'ELEVENLABS_API_KEY': ''}), \
             patch.object(backup_ui.filedialog, 'asksaveasfilename', return_value=str(archive)), \
             patch.object(backup_ui.filedialog, 'askopenfilename', return_value=str(archive)), \
             patch.object(backup_ui.simpledialog, 'askstring', return_value='button-flow-password'), \
             patch.object(backup_ui.messagebox, 'showinfo'), \
             patch.object(backup_ui.messagebox, 'showerror') as error, \
             patch.object(backup_ui, 'launch_restore') as launch:
            backup_ui.transfer(win)
            _, finished = win.events.get(timeout=20)
            finished()
            self.assertTrue(archive.exists())
            self.assertFalse(win.backup_working)
            backup_ui.transfer(win, importing=True)
            _, finished = win.events.get(timeout=20)
            finished()
            error.assert_not_called()
            launch.assert_called_once()
            restored_stage = launch.call_args.args[1]
            self.assertTrue((restored_stage / 'database/jarvis.db').exists())
            win.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
