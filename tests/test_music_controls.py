import sys
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src/core")]
from media_bridge import MediaBridge
import local_actions
from gui_webview import WebViewMixin
import chat
import claude_client


class MusicControls(unittest.TestCase):
    def test_chat_receives_browser_success_instead_of_unpacking_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            session=chat.ChatSession(Path(folder)/'database/jarvis.db')
            session.music_control=Mock(return_value=dict(ok=True,playing=True,name='sample.wav',volume=.2))
            def model(*args,**kwargs):
                text,error=kwargs['tool_executor']('music_control',{'action':'play'})
                self.assertFalse(error)
                self.assertTrue(json.loads(text)['playing'])
                return '音乐已经开始播放。'
            with patch.object(session,'_optional_key',return_value='synthetic'), \
                    patch.object(claude_client,'coordinated_reply',side_effect=model):
                result=session.ask('放点音乐',heard=False)
            self.assertEqual(result['answer'],'音乐已经开始播放。')
            session.music_control.assert_called_once_with('play')

    def test_volume_round_trip_new_instance_and_invalid_write(self):
        with tempfile.TemporaryDirectory() as folder:
            api = MediaBridge(folder)
            self.assertEqual(api.media_settings(), {"volume": 1, "muted": False})
            api.media_save_settings(.27, True)
            fresh = MediaBridge(folder)
            self.assertEqual(fresh.media_settings(), {"volume": .27, "muted": True})
            for volume in [-1, 2, float("nan"), True, "loud"]:
                with self.assertRaises(ValueError):
                    fresh.media_save_settings(volume, False)
            self.assertEqual(fresh.media_settings()["volume"], .27)

    def test_tool_receipt_and_failures(self):
        with tempfile.TemporaryDirectory() as folder:
            context = local_actions.ActionContext(folder, music_control=Mock(
                return_value={"ok": True, "playing": True, "name": "sample.wav"}))
            content, error = local_actions.execute("music_control", {"action": "play"}, context)
            self.assertFalse(error)
            result = json.loads(content)
            self.assertTrue(result["playing"])
            context.music_control.assert_called_once_with("play")
            context.music_control.return_value = {"ok": False, "error": "音乐文件夹为空"}
            with self.assertRaisesRegex(local_actions.ActionError, "为空"):
                local_actions._music_control({"action": "play"}, context)
            context.music_control = None
            with self.assertRaises(local_actions.ActionError):
                local_actions._music_control({"action": "play"}, context)

    def test_gui_waits_for_matching_browser_receipt(self):
        window = WebViewMixin()
        window.web_active = True
        window.sheets = Mock()
        window.sheets.event.return_value = False
        window.web = Mock()
        def reply(message):
            window._web_event({"event": "music_result", "request": "old", "ok": True})
            window._web_event({"event": "music_result", "request": message["request"],
                               "ok": True, "playing": True, "name": "test.wav"})
        window.web.js.side_effect = reply
        results = []
        thread = threading.Thread(target=lambda: results.append(window._music_control("play")))
        thread.start()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(results[0]["playing"])
        self.assertEqual(window._music_requests, {})
