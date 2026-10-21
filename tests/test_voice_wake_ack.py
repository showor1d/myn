'A clearly spoken bare wake word receives a brief spoken acknowledgement.'

import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src" / "core")]

import stt
from gui_voice import VoiceMixin


class FakeWindow(VoiceMixin):
    """Just enough of MYNWindow for _heard; speech itself is replaced."""

    def __init__(self, auto_read=True, busy=False):
        self._stt_job = mock.Mock()  # attention hints must not start a real microphone in tests
        self.stt_settings = stt.STTSettings(wake_words="麦恩", wake_learned="买嗯")
        self.tts_settings = SimpleNamespace(auto_read=auto_read, long_reply="opening")
        self.busy = busy
        self.session = object()
        self.always_on_active = True
        self.listening = False
        self.ducked_at = 0.0
        self.last_barge = ""
        self.wake_checks = ()
        self.last_speaking = 0.0
        self.recent_speech = ()
        self.conversation_open = False
        self.conversation_touch = 0.0
        self.web_active = False
        self.awaiting_until = 0.0
        self.awaiting_after = 0.0
        self.tts_request_id = 0
        self.quiet_tts_request = None
        self.reply_silenced = False
        self.spoken = []
        self.addressed = []
        self.shown = []

    # the real speak() runs; only sound and the chat around it are stand-ins
    def stop_speech(self):
        self.tts_request_id += 1

    def _speech_allowed(self, purpose="reply"):
        return True

    def _stream_speak(self, segments, purpose="reply"):
        if self.reply_silenced:
            return
        self.spoken.append("".join(segments))
        self.recent_speech = tuple(self.recent_speech) + tuple((time.time(), s) for s in segments)

    speech_queue = None

    def append(self, who, text, *args, **kwargs):
        self.shown.append((who, text))

    def _messages(self):
        return []

    def _unduck(self):
        pass

    def _quick_barge(self):
        return False

    def _set_voice_label(self, text, clear_after=None):
        pass

    def _addressed(self, text, heard=False):
        self.addressed.append((text, heard))

    def hear(self, text, **extra):
        now = time.time()
        message = {"event": "heard", "text": text, "start": now - 0.8, "end": now - 0.1}
        message.update(extra)
        self._heard(message)


class WakeAckChecks(unittest.TestCase):
    def test_clear_wake_word_gets_one_short_spoken_reply(self):
        for said in ("麦恩", "买嗯"):  # as set, and as learned from your voice
            window = FakeWindow()
            window.hear(said)
            self.assertEqual(len(window.spoken), 1, said)
            self.assertIn(window.spoken[0], VoiceMixin.WAKE_ACKS)
            self.assertEqual(window.quiet_tts_request, window.tts_request_id)  # no error popup
            self.assertEqual(window.wake_ack_request, window.tts_request_id)
            self.assertGreater(window.awaiting_until, time.time())
            self.assertEqual(window.shown, [])  # never shown in the chat

    def test_sound_alikes_and_voice_only_wakes_stay_silent(self):
        for said, extra in (("迈恩", {}), ("马亚", {}), ("哈喽", {"wake_voice": True, "after_wake": 0.1})):
            window = FakeWindow()
            window.hear(said, **extra)
            self.assertGreater(window.awaiting_until, time.time(), said)  # still woken
            self.assertEqual(window.spoken, [], said)

    def test_auto_read_off_or_reply_being_written_stays_silent(self):
        window = FakeWindow(auto_read=False)
        window.hear("麦恩")
        self.assertEqual(window.spoken, [])
        window = FakeWindow(busy=True)
        window.hear("麦恩")
        self.assertEqual(window.spoken, [])

    def test_heard_ack_or_mm_is_not_the_next_sentence_but_the_real_one_is(self):
        window = FakeWindow()
        with mock.patch("random.choice", return_value="I'm listening."):
            window.hear("麦恩")
        self.assertEqual(window.spoken, ["I'm listening."])
        window.awaiting_after = 0.0  # what follows began after the wake word
        for echo in ("我在", "在呢。", "嗯，我在听", "嗯"):
            window.hear(echo)
        self.assertEqual(window.addressed, [])
        self.assertGreater(window.awaiting_until, time.time())  # still waiting
        window.hear("今天天气怎么样")
        self.assertEqual(window.addressed, [("今天天气怎么样", False)])
        self.assertEqual(window.awaiting_until, 0.0)
        self.assertEqual(window.shown, [])


if __name__ == "__main__":
    unittest.main()
