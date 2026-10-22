'An exact wake receives a local cue without delaying the following request.'

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
        self.root = mock.Mock()
        self._judge_heard = mock.Mock()  # model decisions are supplied explicitly by tests
        self._stt_job = mock.Mock()  # attention hints must not start a real microphone in tests
        self.stt_settings = stt.STTSettings(wake_words="麦恩", wake_learned="买嗯")
        self.tts_settings = SimpleNamespace(auto_read=auto_read, long_reply="opening", output_device="Selected speaker", volume=.6)
        self.tts_player = mock.Mock()
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
            self.assertEqual(window.spoken, [])
            window.tts_player.set_output_device.assert_called_once_with("Selected speaker")
            window.tts_player.play_wake_chime.assert_called_once_with(.6)
            self.assertGreater(window.awaiting_until, time.time())
            self.assertEqual(window.shown, [])  # never shown in the chat

    def test_sound_alikes_and_voice_only_wakes_stay_silent(self):
        for said, extra in (("迈恩", {}), ("马亚", {}), ("哈喽", {"wake_voice": True, "after_wake": 0.1})):
            window = FakeWindow()
            window.hear(said, **extra)
            self.assertFalse(window.conversation_open, said)
            self.assertEqual(window.spoken, [], said)
            window.tts_player.play_wake_chime.assert_not_called()

    def test_auto_read_off_or_reply_being_written_stays_silent(self):
        window = FakeWindow(auto_read=False)
        window.hear("麦恩")
        self.assertEqual(window.spoken, [])
        window.tts_player.play_wake_chime.assert_called_once()
        window = FakeWindow(busy=True)
        window.hear("麦恩")
        self.assertEqual(window.spoken, [])
        window.tts_player.play_wake_chime.assert_called_once()

    def test_heard_ack_or_mm_is_not_the_next_sentence_but_the_real_one_is(self):
        window = FakeWindow()
        window.hear("麦恩")
        self.assertEqual(window.spoken, [])
        window.awaiting_after = 0.0  # what follows began after the wake word
        for echo in ("我在", "在呢。", "嗯，我在听", "嗯"):
            window.hear(echo)
        self.assertEqual(window.addressed, [])
        self.assertGreater(window.awaiting_until, time.time())  # still waiting
        window.hear("今天天气怎么样")
        self.assertEqual(window.addressed, [])
        window._judge_heard.assert_called_once_with("今天天气怎么样")
        window._judged("今天天气怎么样", (True, False, False), window._judge_generation)
        self.assertEqual(window.addressed, [("今天天气怎么样", True)])
        self.assertEqual(window.awaiting_until, 0.0)
        self.assertEqual(window.shown, [])

    def test_duplicate_wakes_are_debounced_and_later_wakes_sound_again(self):
        window = FakeWindow()
        with mock.patch('gui_voice.time.monotonic', side_effect=[10, 10.1, 12]):
            for _ in range(3):
                window.hear("麦恩")
        self.assertEqual(window.tts_player.play_wake_chime.call_count, 2)

    def test_wake_and_command_sound_without_losing_the_command(self):
        window = FakeWindow()
        window.hear("麦恩，今天天气怎么样")
        window.tts_player.play_wake_chime.assert_called_once()
        self.assertEqual(window.addressed, [])
        window._judge_heard.assert_called_once_with("今天天气怎么样")
        window._judged("今天天气怎么样", (True, False, False), window._judge_generation)
        self.assertEqual(window.addressed, [("今天天气怎么样", True)])

    def test_device_failure_still_opens_the_followup_window(self):
        import tts
        window = FakeWindow()
        window.tts_player.play_wake_chime.side_effect = tts.TTSError('Unavailable')
        window.hear("麦恩")
        self.assertGreater(window.awaiting_until, time.time())
        self.assertEqual(window.spoken, [])


class LocalChime(unittest.TestCase):
    def test_waveform_is_short_bounded_and_has_silent_edges(self):
        import io
        import struct
        import wave
        from tts_player import wake_chime
        with wave.open(io.BytesIO(wake_chime()), 'rb') as sound:
            self.assertAlmostEqual(sound.getnframes() / sound.getframerate(), .18)
            data = sound.readframes(sound.getnframes())
            values = struct.unpack('<%dh' % (len(data) // 2), data)
            self.assertEqual((values[0], values[-1]), (0, 0))
            self.assertLessEqual(max(abs(v) for v in values), 7200)
            self.assertGreater(max(values), 5000)

    def test_cue_does_not_replace_speech_or_queued_audio(self):
        from tts_player import MP3Player
        player = MP3Player()
        player.mixer = mock.Mock()
        player.pending.append((b'queued speech', .5))
        player.play_wake_chime(.4)
        player.mixer.music.stop.assert_not_called()
        player.mixer.music.load.assert_not_called()
        self.assertEqual(len(player.pending), 1)
        player.mixer.find_channel.return_value.set_volume.assert_called_once_with(.4)
        player.mixer.find_channel.return_value.play.assert_called_once()
        player.stop()  # the next utterance may interrupt TTS but not its 180 ms cue
        player.cue_channel.stop.assert_not_called()
        player.close()
        self.assertIsNone(player.cue_channel)


if __name__ == "__main__":
    unittest.main()
