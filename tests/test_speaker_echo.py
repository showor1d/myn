"""Speaker playback plus headset input: no live microphone, model or personal data."""
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "src"),
               str(Path(__file__).resolve().parents[1] / "src/core")]
import stt
from gui_webview import WebViewMixin
from test_voice_wake_ack import FakeWindow


class SpeakerEcho(unittest.TestCase):
    def window(self):
        window = FakeWindow()
        window.tts_settings.output_device = "Speakers (花再 Halo PixelBar)"
        window.stt_settings = stt.STTSettings(device="Microphone (HyperX Cloud III Wireless)")
        window._quick_barge = lambda: WebViewMixin._quick_barge(window)
        window.conversation_open = True
        window.conversation_touch = time.time()
        window.last_speaking = time.time()
        window.recent_speech = [(time.time(), "今天东京天气晴朗，下午可能有阵雨，出门记得带伞。")]
        window._note_heard = Mock()
        window._voice_trace = Mock()
        return window

    def test_output_not_microphone_controls_immediate_pause(self):
        window = self.window()
        with patch.dict(os.environ, {"MYN_BARGE_IN": ""}):
            self.assertFalse(window._quick_barge())
            window.tts_settings.output_device = "Headphones (HyperX Cloud III Wireless)"
            self.assertTrue(window._quick_barge())
            window.tts_settings.output_device = ""
            self.assertFalse(window._quick_barge())

    def test_echo_does_not_answer_save_or_interrupt_even_with_forced_pause(self):
        for forced in ("", "1", "0"):
            with self.subTest(forced=forced), patch.dict(os.environ, {"MYN_BARGE_IN": forced}):
                window = self.window()
                window.interrupt_speech = Mock()
                window.hear("下午可能有阵雨，出门记得带伞")
                self.assertEqual(window.addressed, [])
                window._note_heard.assert_not_called()
                window.interrupt_speech.assert_not_called()
                window._voice_trace.assert_any_call("discard", reason="echo")

    def test_real_interruption_same_topic_and_short_rejection_get_through(self):
        for text in ("等一下，我想问别的", "东京明天会下雨吗", "不要了", "停一下"):
            with self.subTest(text=text):
                window = self.window()
                window.hear(text)
                self.assertEqual(window.addressed, [])
                window._judge_heard.assert_called_once_with(text)

    def test_spoken_wake_word_cannot_wake_itself(self):
        window = self.window()
        window.recent_speech = [(time.time(), "麦恩，你可以这样叫我。")]
        window.interrupt_speech = Mock()
        window.hear("麦恩，你可以这样叫我")
        window.interrupt_speech.assert_not_called()
        self.assertEqual(window.addressed, [])

    def test_tail_blocks_echo_but_not_user_speech(self):
        window = self.window()
        window.last_speaking = time.time() - 1
        window.recent_speech = [(time.time()-1, "我在。")]
        window.hear("我在", start=time.time()-.1)
        window._note_heard.assert_not_called()
        window.conversation_open = True
        window.conversation_touch = time.time()
        window.hear("不要了", start=time.time()-.1)
        self.assertEqual(window.addressed, [])
        window._judge_heard.assert_called_once_with("不要了")

    def test_unspoken_reader_text_is_not_echo_and_old_echo_does_not_block(self):
        window = self.window()
        window._messages = lambda: [("myn", "你先解释一下第二题", None)]
        window.hear("你先解释一下第二题")
        self.assertEqual(window.addressed, [])
        window._judge_heard.assert_called_once_with("你先解释一下第二题")
        window.last_speaking = time.time()-10
        window.conversation_open = False
        window.hear("下午可能有阵雨，出门记得带伞")
        window._note_heard.assert_called_once()

    def test_short_echo_must_match_whole_segment(self):
        self.assertEqual(stt.echo_overlap("我在", ["我在。"]), 1)
        self.assertEqual(stt.echo_overlap("不要了", ["你可以说不要了或者稍后再看"]), 0)

    def test_speaker_volume_event_does_not_pause_its_own_playback(self):
        window = self.window()
        window.tts_player = Mock()
        window._speaking = lambda: True
        with patch.dict(os.environ, {"MYN_BARGE_IN": ""}):
            WebViewMixin._mic_level(window, {"speech": True, "level": .8})
        window.tts_player.pause.assert_not_called()
        self.assertTrue(window.always_on_active)


if __name__ == "__main__":
    unittest.main()
