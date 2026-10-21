"""Independent output/input routing, using fake audio devices and temporary settings."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src/core")]
import stt
import tts
import tts_player
import web_panels


class AudioDevices(unittest.TestCase):
    def test_output_setting_roundtrip_and_old_default(self):
        with tempfile.TemporaryDirectory(prefix="myn-audio-settings-") as folder:
            path = Path(folder) / tts.SETTINGS_NAME
            path.write_text(json.dumps({"volume": .4}), encoding="utf-8")
            self.assertEqual(tts.load_settings(folder).output_device, "")
            settings = tts.TTSSettings(output_device="Speakers (synthetic)", volume=.4)
            tts.save_settings(folder, settings)
            self.assertEqual(tts.load_settings(folder), settings)
        for invalid in (None, 1, "x"*201, "x\0y"):
            with self.assertRaises(ValueError):
                tts.TTSSettings(output_device=invalid).validate()

    def test_player_opens_selected_output_and_reuses_it(self):
        mixer = mock.Mock()
        mixer.get_init.side_effect = [None, (44100, -16, 2)]
        player = tts_player.MP3Player()
        player.set_output_device("Speakers (synthetic)")
        with mock.patch.dict(sys.modules, {"pygame": SimpleNamespace(mixer=mixer)}):
            player._music()
            player._music()
        mixer.init.assert_called_once_with(devicename="Speakers (synthetic)")

    def test_missing_selected_output_does_not_silently_play_elsewhere(self):
        mixer = mock.Mock()
        mixer.get_init.return_value = None
        mixer.init.side_effect = RuntimeError("unplugged")
        player = tts_player.MP3Player()
        player.set_output_device("Speakers (missing)")
        with mock.patch.dict(sys.modules, {"pygame": SimpleNamespace(mixer=mixer)}):
            with self.assertRaisesRegex(tts.TTSError, 'playback device'):
                player._music()
        mixer.init.assert_called_once_with(devicename="Speakers (missing)")
        self.assertIsNone(player.mixer)

    def test_switch_clears_old_audio_and_releases_output(self):
        player = tts_player.MP3Player()
        mixer, channel = mock.Mock(), mock.Mock()
        player.mixer, player.pcm_channel = mixer, channel
        player.pending.append(b"old")
        player.pcm_pending.append(b"old-pcm")
        player.set_output_device("Speakers (new)")
        mixer.quit.assert_called_once()
        channel.stop.assert_called_once()
        self.assertFalse(player.pending or player.pcm_pending)
        self.assertIsNone(player.mixer)
        self.assertEqual(player.output_device, "Speakers (new)")

    def test_panel_saves_speaker_and_headset_mic_independently(self):
        with tempfile.TemporaryDirectory(prefix="myn-device-panel-") as folder, \
                mock.patch.object(web_panels, "user_data_dir", return_value=Path(folder)), \
                mock.patch.object(stt, "missing_parts", return_value=["test"]), \
                mock.patch.object(tts_player, "output_devices", return_value=["Speakers (synthetic)"]), \
                mock.patch.object(web_panels.credentials, "elevenlabs_key_source", return_value="none"):
            win = SimpleNamespace(tts_settings=tts.TTSSettings(), stt_settings=stt.STTSettings(),
                                  _apply_voice_settings=mock.Mock())
            panel = web_panels.VoicePanel(win)
            values = dict(provider="elevenlabs", voice_id=tts.DEFAULT_VOICE_ID, auto_read=True,
                          opening_only=True, output_device="Speakers (synthetic)",
                          microphone="Microphone (headset)", voice_enabled=True, always_listen=True,
                          wake_words="MYN", language="auto")
            panel.act("save", values)
            self.assertEqual(tts.load_settings(folder).output_device, "Speakers (synthetic)")
            self.assertEqual(stt.load_settings(folder).device, "Microphone (headset)")
            self.assertTrue(stt.load_settings(folder).always_listen)
            win._apply_voice_settings.assert_called_once()
            values["output_device"] = web_panels.DEFAULT_OUTPUT
            panel.act("save", values)
            self.assertEqual(tts.load_settings(folder).output_device, "")
            self.assertEqual(stt.load_settings(folder).device, "Microphone (headset)")


if __name__ == "__main__":
    unittest.main()
