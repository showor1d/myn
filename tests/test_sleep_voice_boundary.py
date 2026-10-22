"""Replay false courtesy transcripts and preserve deliberate speech during sleep."""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src/core"), str(BASE / "tests")]
import interaction
import stt
from data_location import config_for_db
from gui_interaction import InteractionMixin
from test_voice_wake_ack import FakeWindow


class SleepBoundary(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="myn-sleep-boundary-")
        self.addCleanup(folder.cleanup)
        self.window = FakeWindow()
        self.window.tts_purpose = "reply"
        self.window.session = SimpleNamespace(db_path=Path(folder.name) / "database/jarvis.db")
        self.window.stt_settings = stt.STTSettings(wake_words="Hey Mine", wake_learned="")
        self.window._note_heard = mock.Mock()
        self.window._judge_heard = mock.Mock()
        self.window._voice_trace = mock.Mock()
        self.config = config_for_db(self.window.session.db_path)
        interaction.observe(self.config, "我要睡觉了")

    def test_false_thank_you_never_starts_a_sleep_conversation(self):
        for flags in ({}, {"wake_voice": True, "after_wake": 0.96}, {"streaming_wake": True}):
            with self.subTest(flags=flags):
                self.window.hear("Thank you.", **flags)
                self.assertEqual(self.window.addressed, [])
                self.assertEqual(self.window.spoken, [])
                self.assertFalse(self.window.conversation_open)
                self.window._judge_heard.assert_not_called()
        self.window._voice_trace.assert_any_call("discard", reason="wake_required")
        self.assertEqual(self.window._note_heard.call_count, 3)

    def test_exact_wake_and_question_still_work(self):
        self.window.hear("Hey Mine, what time is it?")
        self.assertEqual(self.window.addressed, [])
        self.window._judge_heard.assert_called_once_with("what time is it?")
        self.window._judged("what time is it?", (True, False, False), self.window._judge_generation)
        self.assertEqual(self.window.addressed, [("what time is it?", True)])

    def test_exact_wake_allows_followup_courtesy(self):
        self.window.hear("Hey Mine")
        self.window.awaiting_after = 0
        self.window.hear("Thank you.")
        self.window._judge_heard.assert_called_once_with("Thank you.")
        self.assertEqual(self.window.addressed, [])

    def test_weak_wake_requires_addressing_for_a_real_question(self):
        self.window.hear("what time is it?", wake_voice=True, after_wake=1)
        self.assertEqual(self.window.addressed, [])
        self.window._judge_heard.assert_not_called()
        self.assertFalse(self.window.conversation_open)

    def test_recent_deliberate_conversation_keeps_courtesy(self):
        self.window.conversation_open = True
        self.window.conversation_touch = time.time() - 2
        with mock.patch.object(self.window, "_last_reply_end", return_value=time.time() - 2):
            self.window.hear("Thank you.")
        self.window._judge_heard.assert_called_once_with("Thank you.")
        self.assertEqual(self.window.addressed, [])

    def test_alarm_response_remains_available_in_sleep(self):
        with mock.patch("alarm_ringing.active", return_value={"id": "test-alarm"}):
            self.window.hear("stop the alarm")
        self.assertEqual(self.window.addressed, [])
        self.window._judge_heard.assert_not_called()
        self.window.hear("Hey Mine")
        self.window.hear("stop the alarm")
        self.window._judge_heard.assert_called_once_with("stop the alarm")

    def test_entering_sleep_invalidates_pending_addressing(self):
        self.window._usage_config = lambda: self.config
        self.window._sync_ringing = mock.Mock()
        self.window._judge_generation = 4
        self.window._judge_pending = ("background speech",)
        self.window._judge_running = True
        InteractionMixin._interaction_changed(self.window)
        self.window._judged("Thank you.", (True, True, False), generation=4)
        self.assertEqual(self.window.addressed, [])
        self.assertEqual(self.window._judge_pending, ())
        self.assertFalse(self.window._judge_running)

    def test_known_hallucination_is_removed_without_global_thanks_filter(self):
        self.assertEqual(stt.clean_transcript("有请不吝点赞订阅转发打赏支持明镜与点点栏目"), "")
        self.assertEqual(stt.clean_transcript("Thank you."), "Thank you.")
