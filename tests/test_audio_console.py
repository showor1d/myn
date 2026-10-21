import sys
import tempfile
import unittest
from pathlib import Path
from urllib.request import urlopen

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'),
               str(Path(__file__).resolve().parents[1] / 'src/core')]
import audio_console
from media_bridge import MediaBridge


class AudioConsoleTests(unittest.TestCase):
    def test_capture_permission_requires_current_page_and_explicit_action(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge = MediaBridge(folder)
            page = (Path(__file__).resolve().parents[1] / 'src/web/index.html').as_uri()
            self.assertFalse(bridge._audio_capture_allowed('file:///', page))
            bridge.audio_console_authorize_capture()
            self.assertTrue(bridge._audio_capture_allowed('file:///', page))
            self.assertFalse(bridge._audio_capture_allowed('https://example.com', page))
            self.assertFalse(bridge._audio_capture_allowed('file:///', 'file:///other.html'))
            bridge._audio_capture_until = 0
            self.assertFalse(bridge._audio_capture_allowed('file:///', page))

    def test_preferences_and_runtime_state_boundary(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge = MediaBridge(folder)
            values = bridge.audio_console_settings()
            values.update(music_eq=[5, 3, 0, 0, -2, -3], effect='pitch', pitch=-4)
            bridge.audio_console_save(values)
            self.assertEqual(MediaBridge(folder).audio_console_settings(), values)
            for key in ('sending', 'monitoring', 'capturing'):
                with self.assertRaises(ValueError):
                    bridge.audio_console_save(dict(values, **{key: True}))
            self.assertEqual(bridge.audio_console_settings(), values)

    def test_invalid_settings_cannot_replace_saved_values(self):
        with tempfile.TemporaryDirectory() as folder:
            original = audio_console.save(folder, audio_console.defaults())
            for key, value in [('pitch', float('nan')), ('mic_gain', True),
                               ('mic_eq', [0]*5), ('music_eq', [13]*6),
                               ('music_send', -1), ('effect', 'unknown'),
                               ('input_device', []), ('mic_clean', 'true')]:
                with self.assertRaises(ValueError):
                    audio_console.save(folder, dict(original, **{key: value}))
                self.assertEqual(audio_console.load(folder), original)

    def test_web_audio_cors_and_token_scope(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge = MediaBridge(folder)
            (bridge.library.folders['music'] / 'test.wav').write_bytes(b'RIFF-test')
            url = bridge.media_list()['items'][0]['url']
            try:
                with urlopen(url) as response:
                    self.assertEqual(response.headers['Access-Control-Allow-Origin'], '*')
                    self.assertEqual(response.read(), b'RIFF-test')
                with self.assertRaises(Exception):
                    urlopen(url.replace(bridge.server.token, 'invalid'))
            finally:
                bridge._close_media()


if __name__ == '__main__':
    unittest.main()
