"""Actions on the computer: groups, the two levels (direct / ask first), write grants,
reminders, and the chat round trip. The Windows platform is replaced by a fake."""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src" / "core"), str(BASE / "tests")]

import chat
import claude_client
import local_actions
import local_tools
from test_web_tools import reply


class FakePlatform:
    def __init__(self):
        self.calls, self.level, self.muted = [], 40, False

    def volume(self, level=None, mute=None):
        self.calls.append(("volume", level, mute))
        if level is not None:
            self.level = level
        if mute is not None:
            self.muted = mute
        return {"level": self.level, "muted": self.muted}

    def media_key(self, action):
        self.calls.append(("media", action))

    def volume_keys(self, level=None, change=None, mute=None):
        self.calls.append(("keys", level, change, mute))

    def open(self, target):
        self.calls.append(("open", target))

    def start_menu(self):
        return [("Steam", r"C:\menu\Steam.lnk"), ("Steam 帮助", r"C:\menu\help.lnk"), ("记事本", r"C:\menu\notepad.lnk")]

    def dark_mode(self, dark):
        self.calls.append(("dark", dark))

    def lock(self):
        self.calls.append(("lock",))

    def power(self, action):
        self.calls.append(("power", action))


class Actions(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="myn-actions-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "personal_data" / "config"
        self.config.mkdir(parents=True)
        self.platform = FakePlatform()
        self.asked = []
        self.answer = True
        self.now = datetime(2026, 10, 17, 3, 0, tzinfo=timezone.utc)
        self.context = local_actions.ActionContext(self.config, confirm=self.confirm,
                                                   platform=self.platform, now=lambda: self.now)

    def confirm(self, preview):
        self.asked.append(preview)
        return self.answer

    def run_action(self, _tool_name, **args):
        content, is_error = local_actions.execute(_tool_name, args, self.context)
        return json.loads(content), is_error

    def test_levels_match_what_the_user_asked_for(self):
        direct = {name for name, level in local_actions.LEVELS.items() if level == "direct"}
        self.assertTrue({"set_volume", "media_control", "open_settings", "set_dark_mode",
                         "lock_screen", "set_reminder", "open_url"} <= direct)
        self.assertEqual({name for name, level in local_actions.LEVELS.items() if level == "ask"},
                         {"open_app", "open_path", "write_file", "make_folder", "power", "type_text", "delete_event", "delete_schedule"})
        self.assertTrue({"skip_alarm", "delete_alarm"} <= direct)
        self.assertEqual(set(local_actions.LABELS), set(local_actions.LEVELS))

    def test_volume_and_media_happen_without_asking(self):
        self.assertEqual(self.run_action("set_volume", change=15)[0], {"ok": True, "level": 55, "muted": False})
        self.assertEqual(self.run_action("set_volume", level=150)[1], True)
        self.assertEqual(self.run_action("set_volume", mute=True)[0]["muted"], True)
        self.assertEqual(self.run_action("set_volume", change=-100)[0]["level"], 0)
        self.assertFalse(self.run_action("media_control", action="next")[1])
        self.assertTrue(self.run_action("media_control", action="format_disk")[1])
        self.assertFalse(self.run_action("set_dark_mode", dark=True)[1])
        self.assertFalse(self.run_action("open_settings", page="sound")[1])
        self.assertTrue(self.run_action("open_settings", page="regedit")[1])
        self.assertFalse(self.run_action("lock_screen")[1])
        self.assertEqual(self.asked, [])
        self.assertIn(("open", "ms-settings:sound"), self.platform.calls)

    def test_volume_falls_back_to_the_volume_keys(self):
        def broken(level=None, mute=None):
            raise OSError("no powershell")
        self.platform.volume = broken
        result, error = self.run_action("set_volume", change=10)
        self.assertFalse(error)
        self.assertTrue(result["approximate"])
        self.assertEqual(self.platform.calls[-1], ("keys", None, 10, None))

    def test_important_actions_ask_first_and_a_no_stops_them(self):
        self.answer = False
        result, error = self.run_action("open_app", name="steam")
        self.assertTrue(error)
        self.assertIn('did not consent', result["error"])
        self.assertEqual(self.asked, ['Open program: Steam'])
        self.assertNotIn(("open", r"C:\menu\Steam.lnk"), self.platform.calls)
        self.answer = True
        self.assertEqual(self.run_action("open_app", name="steam")[0]["opened"], "Steam")
        self.assertEqual(self.run_action("open_app", name="记事")[0]["opened"], "记事本")
        self.assertTrue(self.run_action("open_app", name="Photoshop")[1])
        self.assertFalse(self.run_action("power", action="shutdown")[1])
        self.assertEqual(self.asked[-1], 'Shut down in 60 seconds')
        count = len(self.asked)
        self.assertFalse(self.run_action("power", action="cancel")[1])  # cancelling never asks
        self.assertEqual(len(self.asked), count)

    def test_no_way_to_ask_means_no_important_action(self):
        context = local_actions.ActionContext(self.config, confirm=None, platform=self.platform)
        _, error = local_actions.execute("power", {"action": "restart"}, context)
        self.assertTrue(error)
        self.assertNotIn(("power", "restart"), self.platform.calls)

    def test_switching_groups_off_and_confirm_policies(self):
        local_actions.save_settings(self.config, local_actions.ActionSettings(media=False))
        result, error = self.run_action("set_volume", level=10)
        self.assertTrue(error)
        self.assertIn('has not enabled', result["error"])
        tool = next(tool for tool in local_actions.tools_for(local_actions.load_settings(self.config))
                    if tool["name"] == "set_volume")
        self.assertIn("Permission disabled", tool["description"])
        local_actions.save_settings(self.config, local_actions.ActionSettings(confirm="always"))
        self.run_action("set_volume", level=10)
        self.assertEqual(self.asked, ['Adjust system volume'])
        local_actions.save_settings(self.config, local_actions.ActionSettings(confirm="never"))
        self.run_action("open_app", name="Steam")
        self.assertEqual(len(self.asked), 1)
        (self.config / local_actions.SETTINGS_NAME).write_text("{broken", encoding="utf-8")
        self.assertEqual(local_actions.load_settings(self.config),
                         local_actions.ActionSettings(**{group: False for group in local_actions.GROUPS},
                                                      confirm="always"))  # broken file: everything off

    def test_files_only_inside_folders_granted_for_writing(self):
        notes = self.root / "notes"
        notes.mkdir()
        target = notes / "todo.md"
        self.assertIn('outside authorized', self.run_action("write_file", path=str(target), content="买牛奶")[0]["error"])
        local_tools.grant_read(self.config, notes)
        self.assertTrue(self.run_action("write_file", path=str(target), content="x")[1])  # read is not write
        local_actions.grant_write(self.config, notes)
        result, error = self.run_action("write_file", path=str(target), content="买牛奶\n")
        self.assertFalse(error, result)
        self.assertIn("Create file:", self.asked[-1])
        self.assertIn("买牛奶", self.asked[-1])
        self.assertTrue(self.run_action("write_file", path=str(target), content="again")[1])  # create won't overwrite
        self.run_action("write_file", path=str(target), content="鸡蛋\n", mode="append")
        self.assertEqual(target.read_text(encoding="utf-8"), "买牛奶\n鸡蛋\n")
        for bad in (str(notes / ".." / "escape.txt"), str(notes / "run.bat"), "relative.txt",
                    str(notes / "api_keys.txt"), str(notes / "missing" / "a.txt")):
            self.assertTrue(self.run_action("write_file", path=bad, content="x")[1], bad)
        self.assertFalse((self.root / "escape.txt").exists())
        self.assertFalse(self.run_action("make_folder", path=str(notes / "photos"))[1])
        self.assertTrue((notes / "photos").is_dir())
        with self.assertRaises(ValueError):
            local_actions.grant_write(self.config, self.config)
        with self.assertRaises(ValueError):
            local_actions.grant_write(self.config, self.root)  # contains MYN's own data
        local_actions.revoke_write(self.config, str(notes.resolve()))
        self.assertTrue(self.run_action("make_folder", path=str(notes / "more"))[1])

    def test_open_path_opens_granted_files_but_never_runs_programs(self):
        docs = self.root / "docs"
        docs.mkdir()
        (docs / "a.pdf").write_text("x", encoding="utf-8")
        (docs / "setup.exe").write_text("x", encoding="utf-8")
        self.assertTrue(self.run_action("open_path", path=str(docs / "a.pdf"))[1])
        local_tools.grant_read(self.config, docs)
        self.assertFalse(self.run_action("open_path", path=str(docs / "a.pdf"))[1])
        self.assertTrue(self.run_action("open_path", path=str(docs / "setup.exe"))[1])
        self.assertTrue(self.run_action("open_url", url="file:///C:/Windows")[1])
        self.assertFalse(self.run_action("open_url", url="https://example.com")[1])

    def test_reminders_are_kept_listed_cancelled_and_fired_once(self):
        first = self.run_action("set_reminder", text="喝水", in_minutes=20)[0]
        self.assertEqual(first["due_local"], "2026-10-17 12:20")
        second = self.run_action("set_reminder", text="开会", at="2026-10-17 15:00")[0]
        self.assertTrue(self.run_action("set_reminder", text="过去", at="2026-10-17 08:00")[1])
        self.assertTrue(self.run_action("set_reminder", text="", in_minutes=5)[1])
        listed = self.run_action("list_reminders")[0]["reminders"]
        self.assertEqual([item["text"] for item in listed], ["喝水", "开会"])
        self.assertFalse(self.run_action("cancel_reminder", id=second["id"])[1])
        self.assertTrue(self.run_action("cancel_reminder", id="nope")[1])
        self.assertEqual(local_actions.due_reminders(self.config, self.now), [])
        due = local_actions.due_reminders(self.config, self.now + timedelta(minutes=21))
        self.assertEqual([item["text"] for item in due], ["喝水"])
        self.assertEqual(local_actions.due_reminders(self.config, self.now + timedelta(days=1)), [])
        self.assertEqual(self.asked, [])

    def test_audit_has_names_not_contents(self):
        notes = self.root / "n"
        notes.mkdir()
        local_actions.grant_write(self.config, notes)
        self.run_action("write_file", path=str(notes / "secret-diary.txt"), content="今天的心事")
        self.run_action("set_volume", level=30)
        audit = (self.config / local_tools.AUDIT_FILE).read_text(encoding="utf-8")
        self.assertIn('"tool": "set_volume"', audit)
        self.assertNotIn("今天的心事", audit)
        self.assertNotIn("diary", audit)


class ChatRoundTrip(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="myn-actions-")
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / "data" / "jarvis.db"
        self.db.parent.mkdir()
        self.session = chat.ChatSession(self.db)

    def test_turn_it_up_in_a_chat(self):
        platform = FakePlatform()
        payloads = []

        def opener(request, timeout):
            payloads.append(json.loads(request.data.decode("utf-8")))
            if len(payloads) == 1:
                return reply([{"type": "tool_use", "id": "toolu_v", "name": "set_volume",
                               "input": {"change": 10}}], stop="tool_use")
            return reply([{"type": "text", "text": "好的，调到 50 了。"}])

        with mock.patch.object(self.session, "_key", return_value="k"), \
                mock.patch.object(local_actions, "Platform", return_value=platform), \
                mock.patch.object(claude_client.urllib.request, "urlopen", side_effect=opener):
            result = self.session.ask("把音量调大一点")
        self.assertEqual(result["answer"], "好的，调到 50 了。")
        self.assertEqual(platform.level, 50)
        self.assertIn("set_volume", [tool["name"] for tool in payloads[0]["tools"]])
        self.assertIn("allowed you to act", payloads[0]["system"][0]["text"])
        self.assertEqual(json.loads(payloads[1]["messages"][-1]["content"][0]["content"])["level"], 50)

    def test_actions_are_remembered_so_the_next_turn_knows_they_happened(self):
        platform = FakePlatform()
        payloads = []

        def opener(request, timeout):
            payloads.append(json.loads(request.data.decode("utf-8")))
            if len(payloads) == 1:
                return reply([{"type": "tool_use", "id": "toolu_v", "name": "set_volume",
                               "input": {"level": 40}}], stop="tool_use")
            return reply([{"type": "text", "text": "调到 40 了。"}])

        with mock.patch.object(self.session, "_key", return_value="k"), \
                mock.patch.object(local_actions, "Platform", return_value=platform), \
                mock.patch.object(claude_client.urllib.request, "urlopen", side_effect=opener):
            first = self.session.ask("音量调到40")
            self.assertEqual(first["answer"], "调到 40 了。")  # what the user sees: no note
            self.session.ask("现在多少")
        import conversation_log
        row = conversation_log.get(self.db, first["reply_id"])
        self.assertEqual(row["display_text"], "调到 40 了。")
        self.assertIn('[MYN executed: set_volume', row["raw_text"])
        self.assertIn('"level": 40', row["raw_text"])
        history = json.dumps(payloads[2]["messages"], ensure_ascii=False)
        self.assertIn('[MYN executed: set_volume', history)  # the next turn sees what really happened
        self.assertIn("trust those notes", payloads[2]["system"][0]["text"])

    def test_myn_can_look_at_its_own_state(self):
        self.session.status_provider = lambda: {"version": "9.9", "background_task": {"running": False}}
        payloads = []

        def opener(request, timeout):
            payloads.append(json.loads(request.data.decode("utf-8")))
            if len(payloads) == 1:
                return reply([{"type": "tool_use", "id": "toolu_s", "name": "myn_status", "input": {}}],
                             stop="tool_use")
            return reply([{"type": "text", "text": "现在没有后台任务。"}])

        with mock.patch.object(self.session, "_key", return_value="k"), \
                mock.patch.object(claude_client.urllib.request, "urlopen", side_effect=opener):
            self.assertEqual(self.session.ask("现在有任务在跑吗")["answer"], "现在没有后台任务。")
        self.assertIn("myn_status", [tool["name"] for tool in payloads[0]["tools"]])
        self.assertIn('"version": "9.9"', payloads[1]["messages"][-1]["content"][0]["content"])
        self.assertIn("Be capable, not timid", payloads[0]["system"][0]["text"])
        self.assertNotIn("are unavailable.", payloads[0]["system"][0]["text"].replace("is unavailable", ""))

    def test_myn_can_tell_what_it_cost(self):
        payloads = []

        def opener(request, timeout):
            payloads.append(json.loads(request.data.decode("utf-8")))
            if len(payloads) == 1:
                return reply([{"type": "tool_use", "id": "toolu_u", "name": "usage_report",
                               "input": {"period": "week"}}], stop="tool_use")
            return reply([{"type": "text", "text": "这周花了一点点。"}])

        with mock.patch.object(self.session, "_key", return_value="k"), \
                mock.patch.object(claude_client.urllib.request, "urlopen", side_effect=opener):
            self.assertEqual(self.session.ask("这周 Claude 一共花了我多少")["answer"], "这周花了一点点。")
        self.assertIn("usage_report", [tool["name"] for tool in payloads[0]["tools"]])
        result = json.loads(payloads[1]["messages"][-1]["content"][0]["content"])
        self.assertTrue(result["period"].startswith('This week'))
        self.assertIn("say_this", result)

    def test_myn_can_fill_in_a_hand_entered_cost_after_asking(self):
        config = chat.data_location.config_for_db(self.db)
        entry = chat.usage_stats.add_manual(config, datetime.now().astimezone(), "ChatGPT", 694000,
                                            source="chatgpt")
        asked, payloads = [], []
        self.session.confirm_action = lambda preview: (asked.append(preview), len(asked) > 1)[1]

        def opener(request, timeout):
            payloads.append(json.loads(request.data.decode("utf-8")))
            if len(payloads) in (1, 2):  # the first time the user says no, the second time yes
                return reply([{"type": "tool_use", "id": "toolu_m%d" % len(payloads), "name": "manual_usage",
                               "input": {"action": "set_cost", "id": entry, "cost_usd": 1.5,
                                         "estimated": True}}], stop="tool_use")
            return reply([{"type": "text", "text": "已补上。"}])

        with mock.patch.object(self.session, "_key", return_value="k"), \
                mock.patch.object(claude_client.urllib.request, "urlopen", side_effect=opener):
            result = self.session.ask("把 ChatGPT 那条的费用改成估算 1.5 美元")
        self.assertTrue(result["answer"].startswith("已补上。"), result["answer"])
        self.assertIn("manual_usage", [tool["name"] for tool in payloads[0]["tools"]])
        self.assertEqual(len(asked), 2)
        self.assertIn("ChatGPT", asked[0])
        self.assertIn('USD 1.5 (Estimated)', asked[0])
        self.assertIn("did not agree", payloads[1]["messages"][-1]["content"][0]["content"])
        saved = chat.usage_stats.manual_entries(config)[0]
        self.assertEqual((saved["cost_usd"], saved["estimated"]), (1.5, True))
        self.assertIn("manual_usage", result["memory_note"])

    def test_important_action_in_a_chat_waits_for_the_window(self):
        platform, asked = FakePlatform(), []
        self.session.confirm_action = lambda preview: (asked.append(preview), False)[1]

        def opener(request, timeout):
            if not asked:
                return reply([{"type": "tool_use", "id": "toolu_p", "name": "power",
                               "input": {"action": "shutdown"}}], stop="tool_use")
            return reply([{"type": "text", "text": "好的，不关机。"}])

        with mock.patch.object(self.session, "_key", return_value="k"), \
                mock.patch.object(local_actions, "Platform", return_value=platform), \
                mock.patch.object(claude_client.urllib.request, "urlopen", side_effect=opener):
            result = self.session.ask("关机吧")
        self.assertEqual(asked, ['Shut down in 60 seconds'])
        self.assertNotIn(("power", "shutdown"), platform.calls)
        self.assertEqual(result["answer"].split("\n")[0], "好的，不关机。")


if __name__ == "__main__":
    unittest.main()
