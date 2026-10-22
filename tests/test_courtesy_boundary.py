"""Replay the ordinary-mode false courtesy route without models or live microphones."""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / 'src'), str(BASE / 'src/core'), str(BASE / 'tests')]
import interaction
import stt
from data_location import config_for_db
from test_voice_wake_ack import FakeWindow


class CourtesyBoundary(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix='myn-courtesy-')
        self.addCleanup(folder.cleanup)
        self.window = FakeWindow()
        self.window.session = SimpleNamespace(db_path=Path(folder.name) / 'database/jarvis.db')
        self.window.stt_settings = stt.STTSettings(wake_words='Hey Mine', wake_learned='')
        self.window._note_heard = mock.Mock()
        self.window._judge_heard = mock.Mock()
        self.window._voice_trace = mock.Mock()
        self.config = config_for_db(self.window.session.db_path)

    def test_incident_acoustic_thank_you_never_opens_idle_conversation(self):
        self.window.hear('Thank you.', wake_voice=True, after_wake=.96, language='en')
        self.assertEqual(self.window.addressed, [])
        self.assertFalse(self.window.conversation_open)
        self.window._judge_heard.assert_not_called()
        self.window.tts_player.play_wake_chime.assert_not_called()
        self.window._voice_trace.assert_any_call('discard', reason='wake_required')

    def test_all_scenes_and_candidate_sources_require_evidence_for_courtesies(self):
        for scene in ('我醒了', '我要学习了', '我要睡觉了'):
            interaction.observe(self.config, scene)
            for text in ('Thank you.', '谢谢。', '不客气', 'ありがとうございます', 'Good night.'):
                for flags in ({}, {'wake_voice': True, 'after_wake': .9}, {'streaming_wake': True}):
                    with self.subTest(scene=scene, text=text, flags=flags):
                        self.window.hear(text, **flags)
                        self.assertEqual(self.window.addressed, [])
                        self.assertFalse(self.window.conversation_open)
        self.window._judge_heard.assert_not_called()

    def test_courtesy_after_a_weak_bare_wake_is_still_unconfirmed(self):
        self.window.hear('Hello', wake_voice=True, after_wake=.1)
        self.assertFalse(self.window.conversation_open)
        self.assertEqual(self.window.awaiting_until, 0)
        self.window.awaiting_after = 0
        self.window.hear('Thank you.')
        self.assertEqual(self.window.addressed, [])

    def test_deliberate_wake_and_recent_conversation_still_allow_thanks(self):
        self.window.hear('Hey Mine, thank you.')
        self.assertEqual(self.window.addressed, [])
        self.window._judge_heard.assert_called_with('thank you.')
        self.window.addressed.clear()
        self.window.hear('Hey Mine')
        self.window.awaiting_after = 0
        self.window.hear('谢谢。')
        self.assertEqual(self.window.addressed, [])
        self.window._judge_heard.assert_called_with('谢谢。')
        self.window.addressed.clear()
        self.window.conversation_open = True
        self.window.conversation_touch = time.time() - 2
        with mock.patch.object(self.window, '_last_reply_end', return_value=time.time() - 2):
            self.window.hear('Thank you.')
        self.assertEqual(self.window.addressed, [])
        self.window._judge_heard.assert_called_with('Thank you.')

    def test_courtesy_guard_does_not_swallow_a_request_or_alarm_stop(self):
        self.window.hear('Thank you, what time is it?', wake_voice=True, after_wake=1)
        self.assertEqual(self.window.addressed, [])
        self.window._judge_heard.assert_not_called()
        self.window.hear('Hey Mine, Thank you, what time is it?')
        self.window._judge_heard.assert_called_once_with('Thank you, what time is it?')
        self.window._judge_heard.reset_mock()
        with mock.patch('alarm_ringing.active', return_value={'id': 'test-alarm'}):
            self.window.hear('stop the alarm')
        self.assertEqual(self.window.addressed, [])
        self.window._judge_heard.assert_called_once_with('stop the alarm')

if __name__ == '__main__':
    unittest.main()
