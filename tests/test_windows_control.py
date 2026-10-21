"""Windows and shortcuts: list, switch, minimise / close, press keys, type text — with the
user's levels (closing, quitting / deleting shortcuts and typing are asked first)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src" / "core")]

import local_actions
import windows_control as wc


class FakeDesktop:
    def __init__(self):
        self.list = [{"id": 11, "title": "微信", "program": "WeChat", "state": "normal"},
                     {"id": 22, "title": "无标题 - 记事本", "program": "notepad", "state": "minimized"},
                     {"id": 33, "title": "YouTube - Google Chrome", "program": "chrome", "state": "maximized"}]
        self.front_id, self.calls, self.typed = 33, [], ""
        self.focus_works = True

    def windows(self):
        return list(self.list)

    def focus(self, window_id):
        self.calls.append(("focus", window_id))
        if self.focus_works:
            self.front_id = window_id
        return self.focus_works

    def show(self, window_id, action):
        self.calls.append((action, window_id))

    def close(self, window_id):
        self.calls.append(("close", window_id))

    def front(self):
        return self.front_id

    def press(self, codes):
        self.calls.append(("press", tuple(codes)))

    def type_text(self, text):
        self.typed += text


class Keys(unittest.TestCase):
    def test_parsing(self):
        self.assertEqual(wc.parse_keys("Ctrl + S"), ("ctrl+s", [0x11, 0x53]))
        self.assertEqual(wc.parse_keys("shift+ctrl+t")[0], "ctrl+shift+t")
        self.assertEqual(wc.parse_keys("F5")[1], [0x74])
        self.assertEqual(wc.parse_keys("Control+Return")[0], "ctrl+enter")
        for bad in ("", "ctrl+苹果", "a+b+c+d+e"):
            with self.subTest(bad=bad), self.assertRaises(wc.WindowError):
                wc.parse_keys(bad)
        self.assertTrue(wc.is_risky_keys(wc.parse_keys("Alt+F4")[0]))
        self.assertTrue(wc.is_risky_keys(wc.parse_keys("ctrl+w")[0]))
        self.assertFalse(wc.is_risky_keys(wc.parse_keys("ctrl+s")[0]))

    def test_matching_titles(self):
        windows = FakeDesktop().list
        self.assertEqual(wc.match(windows, "记事本")["id"], 22)
        self.assertEqual(wc.match(windows, "chrome")["id"], 33)  # program name works too
        self.assertEqual(wc.match(windows, "微信")["id"], 11)
        with self.assertRaises(wc.WindowError):
            wc.match(windows, "Photoshop")


class Actions(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="myn-windows-")
        self.addCleanup(folder.cleanup)
        self.config = Path(folder.name) / "config"
        self.config.mkdir()
        self.desk, self.asked, self.answer = FakeDesktop(), [], True

        def confirm(preview):
            self.asked.append(preview)
            return self.answer

        self.context = local_actions.ActionContext(self.config, confirm=confirm, desktop=self.desk)

    def run_action(self, name, **args):
        content, is_error = local_actions.execute(name, args, self.context)
        return json.loads(content), is_error

    def test_list_and_switch_without_asking(self):
        result, _ = self.run_action("list_windows")
        self.assertEqual([w["title"] for w in result["windows"]][0], "微信")
        self.assertNotIn("id", result["windows"][0])
        result, error = self.run_action("focus_window", title="记事本")
        self.assertFalse(error)
        self.assertEqual(self.desk.calls, [("focus", 22)])
        self.run_action("window_state", title="chrome", action="minimize")
        self.assertEqual(self.desk.calls[-1], ("minimize", 33))
        self.assertEqual(self.asked, [])

    def test_closing_is_asked(self):
        self.answer = False
        result, error = self.run_action("window_state", title="微信", action="close")
        self.assertTrue(error)
        self.assertNotIn(("close", 11), self.desk.calls)
        self.assertEqual(self.asked, ['Close window: 微信'])
        self.answer = True
        self.run_action("window_state", title="微信", action="close")
        self.assertIn(("close", 11), self.desk.calls)

    def test_shortcuts_safe_ones_directly_closing_ones_asked(self):
        result, error = self.run_action("press_keys", keys="ctrl+s", window="记事本")
        self.assertFalse(error)
        self.assertEqual(self.desk.calls[-2:], [("focus", 22), ("press", (0x11, 0x53))])
        self.assertEqual(result["window"], "无标题 - 记事本")
        self.assertEqual(self.asked, [])
        self.run_action("press_keys", keys="down", repeat=3)
        self.assertEqual(self.desk.calls[-3:], [("press", (0x28,))] * 3)
        self.answer = False
        result, error = self.run_action("press_keys", keys="alt+f4")
        self.assertTrue(error)
        self.assertEqual(self.asked, ['In "Foreground window", press alt+f4'])
        self.assertNotIn(("press", (0x12, 0x73)), self.desk.calls)

    def test_typing_is_always_asked_and_shows_the_text(self):
        self.answer = False
        result, error = self.run_action("type_text", text="晚上一起吃饭吗", window="微信")
        self.assertTrue(error)
        self.assertEqual(self.desk.typed, "")
        self.assertIn("晚上一起吃饭吗", self.asked[0])
        self.assertIn("微信", self.asked[0])
        self.answer = True
        result, error = self.run_action("type_text", text="晚上一起吃饭吗", window="微信")
        self.assertFalse(error)
        self.assertEqual(self.desk.typed, "晚上一起吃饭吗")  # no Enter: sending is the user's call

    def test_myn_in_front_and_failures_are_explained(self):
        self.desk.front_id = 999  # MYN's own window (not in the list)
        result, error = self.run_action("press_keys", keys="ctrl+c")
        self.assertTrue(error)
        self.assertIn('MYN is the foreground window', result["error"])
        self.desk.focus_works = False
        result, error = self.run_action("focus_window", title="微信")
        self.assertTrue(error)
        self.assertIn("Cannot bring", result["error"])
        result, error = self.run_action("focus_window", title="Photoshop")
        self.assertIn("No window title contains", result["error"])

    def test_group_can_be_turned_off(self):
        local_actions.save_settings(self.config, local_actions.ActionSettings(windows=False))
        self.assertIn("press_keys", [t["name"] for t in local_actions.tools_for(local_actions.load_settings(self.config))])
        result, error = self.run_action("list_windows")
        self.assertTrue(error)


if __name__ == "__main__":
    unittest.main()
