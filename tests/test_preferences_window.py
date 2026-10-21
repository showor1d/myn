'Communication preference controls and reply feedback use temporary data only.'

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src" / "core")]

try:
    import tkinter as tk
    _root = tk.Tk()
    _root.destroy()
    HAVE_TK = True
except Exception:  # no tkinter or no display
    HAVE_TK = False

import chat  # noqa: E402
import claude_client  # noqa: E402
import stt  # noqa: E402
from test_preferences import FakeAPI  # noqa: E402


class VoiceCommandCheck(unittest.TestCase):
    def test_spoken_request_opens_preferences(self):
        self.assertEqual(stt.voice_command("打开交流偏好"), "preferences")
        self.assertEqual(stt.voice_command("看看交流偏好"), "preferences")
        self.assertEqual(stt.voice_command("打开设置"), "settings")


@unittest.skipUnless(HAVE_TK, "needs Tk and a display")
class WindowChecks(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="myn-prefs-ui-")
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / "data" / "jarvis.db"
        self.db.parent.mkdir()
        self.session = chat.ChatSession(self.db)
        self.api = FakeAPI()
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def ask(self, text):
        with mock.patch.object(self.session, "_key", return_value="k"), \
                mock.patch.object(claude_client.urllib.request, "urlopen", side_effect=self.api):
            return self.session.ask(text)

    @property
    def store(self):
        return self.session.preferences.store

    def window(self):
        """Just enough of the main window for the feedback controls."""
        import myn_gui
        window = myn_gui.MYNWindow.__new__(myn_gui.MYNWindow)
        window.root = self.root
        window.session = self.session
        window.font_family = "TkDefaultFont"
        window.reply_links = {}
        window.reply_ratings = {}
        window.labels = []
        window._set_voice_label = lambda text, clear_after=None: window.labels.append(text)
        window._queue_chat = lambda: None
        window.errors = []
        window.append = lambda speaker, text, *args, **kwargs: window.errors.append(text)
        return window

    def test_thumbs_rate_once_and_clicking_again_takes_it_back(self):
        self.api.answers = ["这是一个非常棒的问题！下面我将从五个方面详细介绍……"]
        result = self.ask("这个功能能实现吗？")
        self.assertIsNotNone(result.get("reply_id"))
        window = self.window()
        window.rate_reply(result["reply_id"], -1)
        window.rate_reply(result["reply_id"], -1)  # the same click again from the page
        rows = self.store.feedback_list()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rating"], -1)
        self.assertEqual(rows[0]["question"], "这个功能能实现吗？")
        self.assertIn("五个方面", rows[0]["reply"])
        window.rate_reply(result["reply_id"], 0)
        self.assertEqual(self.store.feedback_list()[0]["status"], "revoked")
        self.assertEqual(window.labels[-1], 'Rating withdrawn')
        self.assertEqual(window.errors, [])

    def test_web_page_feedback_events_reach_the_store(self):
        result = self.ask("你好")
        window = self.window()
        window.web = object()
        window.web_active = True
        window._web_event({"event": "feedback", "reply": result["reply_id"], "rating": 1})
        self.assertEqual(self.session.preferences.rating_of(result["reply_id"]), 1)
        window._web_event({"event": "feedback", "reply": "not a number", "rating": 1})  # ignored
        self.assertEqual(len(self.store.feedback_list()), 1)

    def test_teach_dialog_saves_reason_and_ideal_and_the_next_reply_uses_them(self):
        import preferences_window
        self.api.answers = ["这是一个非常棒的问题！下面我将从五个方面详细介绍……"]
        result = self.ask("这个功能能实现吗？")
        done = []
        dialog = preferences_window.FeedbackDialog(self.root, self.session.preferences, result["reply_id"],
                                                   "", lambda rating, made: done.append((rating, made)))
        dialog.rating.set(-1)
        dialog.reason.insert("1.0", "太繁琐了，以后简短一点")
        dialog.ideal.insert("1.0", "可以实现，主要需要以下三个模块……")
        dialog.save()
        self.assertEqual(done[0][0], -1)
        item = self.store.feedback_list()[0]
        self.assertEqual(item["ideal"], "可以实现，主要需要以下三个模块……")
        self.assertIn("繁琐", item["reason"])
        self.assertTrue(self.store.examples())
        self.ask("这个能做吗？")
        system = self.api.last_system
        self.assertIn("可以实现，主要需要以下三个模块", system)

    def test_preferences_window_shows_and_manages_everything(self):
        import preferences_window
        self.ask("你好")
        self.ask("以后不要每次都夸我")
        view = preferences_window.PreferencesWindow(self.root, self.session.preferences)
        rules = view.rule_tree.get_children()
        self.assertEqual(len(rules), 1)
        self.assertIn("夸", view.rule_tree.item(rules[0], "values")[0])
        self.assertEqual(view.rule_tree.item(rules[0], "values")[2], 'Explicitly stated by you')
        self.assertEqual(len(view.feedback_tree.get_children()), 1)
        self.assertTrue(view.history_tree.get_children())
        view.rule_tree.selection_set(rules[0])
        view._rule_detail()
        self.assertIn("Based on this feedback", view.rule_box.get("1.0", tk.END))
        # Learning stays on; individual rules may still be deleted and restored.
        self.assertFalse(self.store.learning_paused)
        with mock.patch.object(preferences_window.messagebox, "askyesno", return_value=True):
            view._delete_rule()
        self.assertEqual(view.rule_tree.get_children(), ())
        self.ask("在吗")
        self.assertNotIn("不要每次都夸我", self.api.last_system)
        # ...and undo that deletion from the learning history.
        latest = view.history_tree.get_children()[0]
        view.history_tree.selection_set(latest)
        view._undo()
        self.assertEqual(len(view.rule_tree.get_children()), 1)
        with mock.patch.object(preferences_window.messagebox, "showinfo") as shown:
            view._export()
        self.assertIn("preferences-", shown.call_args[0][1])
        view._fill_effect()
        self.assertIn("Last 7 days", view.effect_box.get("1.0", tk.END))
        view.window.destroy()

    def test_manual_rule_dialog(self):
        import preferences_window
        dialog = preferences_window.RuleDialog(self.root, self.store)
        dialog.text.insert("1.0", "讲物理题时先列已知量")
        dialog.scenes.everywhere.set(False)
        dialog.scenes.vars["study"][0].set(True)
        dialog.save()
        rule = self.store.rules()[0]
        self.assertEqual((rule["source"], rule["status"], rule["scenes"]), ("manual", "active", ["study"]))
        self.assertIn("先列已知量", self.session.preferences.build_context("study")[0])
        self.assertEqual(self.session.preferences.build_context("coding")[0], "")


if __name__ == "__main__":
    unittest.main()
