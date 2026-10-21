"""Music alarm persistence, validated creation, delivery fallback and acknowledgement."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / 'src'), str(BASE / 'src/core')]
import alarm_ringing
import alarm_music
import timekeeper
import local_actions
import schedule_receipts
from media_bridge import MediaBridge
from gui_timers import TimersMixin


class MusicAlarms(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='myn-music-alarm-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.config = self.root / 'config'
        self.config.mkdir()
        self.bridge = MediaBridge(self.root / 'media')
        (self.bridge.library.folders['music'] / 'wake.wav').write_bytes(b'test')
        self.song = self.bridge.library.scan()[0]
        self.now = datetime(2026, 10, 21, 7)

    def create(self, **changes):
        args = dict(kind='say', category='alarm', time='08:00', text='Wake up',
                    repeat='once', ringtone_id=self.song['id'])
        args.update(changes)
        return timekeeper.add_task(self.config, now=self.now, **args)

    def window(self):
        win = TimersMixin()
        win.session = SimpleNamespace(db_path=self.root / 'database' / 'jarvis.db')
        win.web_active = True
        win.web = mock.Mock()
        win.busy = False
        win._speaking = mock.Mock(return_value=False)
        win.tts_request_id = 3
        win.speak = mock.Mock()
        win.stop_speech = mock.Mock()
        win.append = mock.Mock()
        win._timers_changed = mock.Mock()
        return win

    def test_create_readback_and_restore_spoken(self):
        task = self.create()
        saved = timekeeper.get_task(self.config, task['id'])
        self.assertEqual(saved['ringtone_name'], 'wake.wav')
        self.assertEqual(timekeeper.task_for_model(saved)['ringtone_id'], self.song['id'])
        changed = timekeeper.update_task(self.config, task['id'], now=self.now, time='09:00')
        self.assertEqual(changed['ringtone_id'], self.song['id'])
        cleared = timekeeper.update_task(self.config, task['id'], now=self.now, ringtone_id='')
        self.assertEqual(cleared['ringtone_id'], '')

    def test_invalid_missing_or_nonmusic_sound_does_not_create(self):
        for ident in ('../../outside.mp3', 'a'*64, None):
            with self.assertRaises(timekeeper.TimerError):
                self.create(ringtone_id=ident)
        (self.bridge.library.folders['video'] / 'film.mp4').write_bytes(b'video')
        video = next(r for r in self.bridge.library.scan() if r['mode'] == 'video')
        with self.assertRaises(timekeeper.TimerError):
            self.create(ringtone_id=video['id'])
        self.assertEqual(timekeeper.tasks(self.config), [])

    def test_default_used_only_on_new_unspecified_alarms(self):
        old = self.create(ringtone_id='')
        alarm_music.configure(self.config, ringtone_id=self.song['id'])
        task = timekeeper.add_task(self.config, now=self.now, kind='say', category='alarm', time='08:00', text='Wake')
        self.assertEqual(task['ringtone_id'], self.song['id'])
        self.assertEqual(self.create(ringtone_id='')['ringtone_id'], '')
        self.assertEqual(timekeeper.update_task(self.config, old['id'], now=self.now, time='09:00')['ringtone_id'], '')
        alarm_music.configure(self.config, ringtone_id='')
        self.assertEqual(timekeeper.get_task(self.config, task['id'])['ringtone_id'], self.song['id'])

    def test_independent_volume_persists_and_validates_without_changing_music(self):
        self.bridge.media_save_settings(.2, True)
        alarm_music.configure(self.config, ringtone_id=self.song['id'], volume=.65)
        self.assertEqual(alarm_music.settings(Path(str(self.config)))['volume'], .65)
        self.assertEqual(self.bridge.media_settings(), dict(volume=.2, muted=True))
        self.bridge.media_save_settings(.9, False)
        self.assertEqual(alarm_music.settings(self.config)['volume'], .65)
        for value in (-.1, 2, True, None, float('nan')):
            with self.assertRaises(ValueError):
                alarm_music.configure(self.config, volume=value)
        self.assertEqual(alarm_music.settings(self.config)['volume'], .65)
        alarm_music.configure(self.config, volume=0)
        self.assertEqual(alarm_music.settings(self.config)['volume'], 0)

    def test_corrupt_settings_preserved(self):
        path = self.config / alarm_music.SETTINGS
        path.write_text('{broken', encoding='utf-8')
        with self.assertRaises(ValueError):
            alarm_music.configure(self.config, volume=.5)
        self.assertEqual(path.read_text(encoding='utf-8'), '{broken')
        win = self.window()
        win._sync_alarm_music([])
        self.assertEqual(win.web.js.call_args.args[0]['alarms'], [])

    def test_voice_creation_receipt_lists_song(self):
        context = local_actions.ActionContext(self.config, confirm=lambda _: True,
            now=lambda: datetime(2026, 10, 20, 22, tzinfo=timezone.utc))
        raw, failed = local_actions.execute('list_alarm_music', {}, context)
        self.assertFalse(failed, raw)
        self.assertEqual(json.loads(raw)['songs'][0]['id'], self.song['id'])
        raw, failed = local_actions.execute('add_alarm', dict(time='08:00', text='Wake',
            ringtone_id=self.song['id']), context)
        self.assertFalse(failed, raw)
        data = json.loads(raw)
        reply = schedule_receipts.finish('set alarm', '', [('add_alarm', data, False)], True)
        self.assertIn('wake.wav', reply)

    def test_default_button_never_creates_an_alarm(self):
        win = self.window()
        message = dict(request='same-click', ringtone_id=self.song['id'])
        win._music_alarm_settings(message)
        win._music_alarm_settings(message)
        self.assertEqual(timekeeper.tasks(self.config), [])
        self.assertTrue(win.web.js.call_args.args[0]['ok'])
        self.assertEqual(alarm_music.settings(self.config)['ringtone_id'], self.song['id'])

    def test_voice_default_volume_then_alarm_without_sound(self):
        context = local_actions.ActionContext(self.config, confirm=lambda _: True,
            now=lambda: datetime(2026, 10, 20, 22, tzinfo=timezone.utc))
        raw, failed = local_actions.execute('alarm_sound_settings', dict(ringtone_id=self.song['id'], volume=.6), context)
        self.assertFalse(failed, raw)
        raw, failed = local_actions.execute('add_alarm', dict(time='08:00', text='Wake'), context)
        self.assertFalse(failed, raw)
        self.assertEqual(json.loads(raw)['schedule']['ringtone_id'], self.song['id'])

    def test_due_persists_song_then_ack_stops_audio_and_cleans_only_once(self):
        once = self.create()
        recurring = self.create(repeat='daily')
        timekeeper.due_tasks(self.config, datetime(2026, 10, 21, 8))
        active = alarm_ringing.active(self.config)
        self.assertEqual(len(active), 2)
        self.assertTrue(all(r['ringtone_id'] == self.song['id'] for r in active))
        win = self.window()
        win._sync_ringing()
        self.assertEqual(len(win.web.js.call_args.args[0]['alarms']), 2)
        win.busy = True
        win._sync_ringing()
        self.assertTrue(win.web.js.call_args.args[0]['paused'])
        alarm_ringing.dismiss(self.config, [r['id'] for r in active], completed=True)
        win._sync_ringing()
        self.assertEqual(win.web.js.call_args.args[0]['alarms'], [])
        self.assertEqual([r['id'] for r in timekeeper.tasks(self.config)], [recurring['id']])

    def test_failure_falls_back_without_resurrecting_dismissed_alarm(self):
        row = alarm_ringing.activate(self.config, 'schedule:x', '2026-10-21 08:00',
                                    'Wake', self.song['id'], self.song['name'])
        win = self.window()
        win._deliver_alarm('Wake', 'alarm', 'schedule:x')
        win.speak.assert_not_called()
        win._alarm_music_result(dict(id=row['id'], ok=False))
        win.speak.assert_called_once_with('Wake', purpose='alarm')
        alarm_ringing.dismiss(self.config, [row['id']])
        win._alarm_music_result(dict(id=row['id'], ok=False))
        self.assertEqual(win.speak.call_count, 1)
        self.assertEqual(alarm_ringing.active(self.config), [])


if __name__ == '__main__':
    unittest.main()
