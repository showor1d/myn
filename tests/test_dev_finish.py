"""Claude Code's work on MYN counted from its logs; the finish note; MYN restarting to say it."""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src" / "core")]

import claude_code_usage
import usage_stats

spec = importlib.util.spec_from_file_location("dev_finish", BASE / "tools" / "dev_finish.py")
dev_finish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dev_finish)


def line(message_id, at, usage, session="s1", model="claude-opus-5-5", kind="assistant"):
    return json.dumps({"type": kind, "sessionId": session, "requestId": "r-" + message_id, "timestamp": at,
                       "message": {"id": message_id, "model": model, "usage": usage,
                                   "content": [{"type": "text", "text": "secret answer text"}]}})


class ClaudeCodeUsage(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="myn-ccu-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.logs, self.config = self.root / "projects" / "D--MYN", self.root / "config"
        self.logs.mkdir(parents=True)
        self.config.mkdir()

    def test_project_folder_name(self):
        folder = claude_code_usage.project_dir(r"D:\MYN", home=r"C:\Users\x")
        self.assertEqual(folder.name, "D--MYN")
        self.assertEqual(folder.parent, Path(r"C:\Users\x") / ".claude" / "projects")

    def test_each_response_counted_once_and_priced_with_hour_cache(self):
        usage = {"input_tokens": 10, "output_tokens": 1000, "cache_creation_input_tokens": 1000000,
                 "cache_read_input_tokens": 2000000,
                 "cache_creation": {"ephemeral_1h_input_tokens": 1000000, "ephemeral_5m_input_tokens": 0}}
        same = line("m1", "2026-10-18T01:10:00Z", usage)
        (self.logs / "s1.jsonl").write_text("\n".join([same, same, same,  # one response, three blocks
                                                       line("u", "2026-10-18T01:11:00Z", usage, kind="user"),
                                                       "not json"]), encoding="utf-8")
        result = claude_code_usage.sync(self.config, self.logs)
        rows = [json.loads(x) for x in (self.config / usage_stats.CLAUDE_CODE_NAME).read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["output_tokens"], 1000)  # not 3000
        # Opus 5.5 $4 in / $20 out; 1-hour cache writes 2x input, reads 0.1x input
        expected = (10 * 4 + 1000 * 20 + 1000000 * 8 + 2000000 * 0.4) / 1e6
        self.assertAlmostEqual(rows[0]["cost_usd"], expected, places=6)
        self.assertEqual(result["sessions"], 1)
        self.assertNotIn("secret answer text", (self.config / usage_stats.CLAUDE_CODE_NAME).read_text(encoding="utf-8"))
        # the statistics use the stored dollars
        calls = usage_stats.load_calls(self.config)
        self.assertAlmostEqual(sum(call.cost for call in calls), expected, places=6)

    def test_older_lines_and_vanished_sessions_are_kept_and_reruns_add_nothing(self):
        path = self.config / usage_stats.CLAUDE_CODE_NAME
        old = {"at_utc": "2026-10-16T05:30:00+00:00", "source": "claude_code", "cost_usd": 11.98,
               "input_tokens": 1, "output_tokens": 2, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        gone = dict(old, session="cleaned-up", cost_usd=1.0)
        path.write_text(json.dumps(old) + "\n" + json.dumps(gone) + "\n", encoding="utf-8")
        usage = {"input_tokens": 5, "output_tokens": 5}
        (self.logs / "a.jsonl").write_text(line("m1", "2026-10-18T01:00:00Z", usage), encoding="utf-8")
        claude_code_usage.sync(self.config, self.logs)
        (self.logs / "a.jsonl").write_text("\n".join([line("m1", "2026-10-18T01:00:00Z", usage),
                                                      line("m2", "2026-10-18T03:00:00Z", usage)]), encoding="utf-8")
        claude_code_usage.sync(self.config, self.logs)
        claude_code_usage.sync(self.config, self.logs)
        rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 4)  # old, gone, and two hours of s1
        self.assertEqual([r.get("session") for r in rows].count("s1"), 2)
        self.assertIn(11.98, [r["cost_usd"] for r in rows])

    def test_daily_update_only_reads_the_logs_for_myns_own_data(self):
        with mock.patch.object(claude_code_usage, "sync") as sync:
            usage_stats.daily_update(self.config, None, force=True)
        sync.assert_not_called()  # a temporary folder (tests) never pulls in the real logs


class FinishNote(unittest.TestCase):
    def test_note_from_the_last_message(self):
        message = "**修好了。**全部 357 项测试现在都通过。\n\n第二段细节 [文件](a.py) `code`"
        note = dev_finish.note_for(message, True)
        self.assertTrue(note["text"].startswith('Changes completed.\n\n修好了。全部 357 项测试现在都通过。'))
        self.assertNotIn("第二段", note["text"])
        self.assertEqual(note["spoken"], 'Changes completed.修好了。')
        self.assertTrue(note["restart"])
        quiet = dev_finish.note_for("", False)
        self.assertEqual((quiet["text"], quiet["restart"]), ('Task completed.', False))
        for message in ("Changes completed.未来24小时内只显示图标。",
                        "**Changes completed.**\n\n未来24小时内只显示图标。",
                        "任务已完成！Changes completed.未来24小时内只显示图标。"):
            note = dev_finish.note_for(message, True)
            self.assertEqual(note["spoken"], 'Changes completed.未来24小时内只显示图标。')
            self.assertEqual(note["text"], 'Changes completed.\n\n未来24小时内只显示图标。')
        self.assertEqual(dev_finish.note_for("任务已完成", False)["spoken"], 'Task completed.')

    def test_hook_input_is_read_as_utf8_bytes(self):
        import io
        payload = json.dumps({"last_assistant_message": "修好了。"}, ensure_ascii=False).encode("utf-8")
        stream = io.TextIOWrapper(io.BytesIO(payload), encoding="gbk")  # what Windows Python 3.9 gives
        self.assertEqual(dev_finish.read_hook(stream)["last_assistant_message"], "修好了。")
        self.assertEqual(dev_finish.read_hook(io.TextIOWrapper(io.BytesIO(b"not json"))), {})
        self.assertEqual(dev_finish.read_hook(io.TextIOWrapper(io.BytesIO(b""))), {})

    def test_last_message_read_from_the_transcript(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "t.jsonl"
            path.write_text("\n".join([
                json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "早的"}]}}),
                json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "x"}]}}),
                json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "最后的话"}]}}),
                json.dumps({"type": "user", "message": {"content": "hi"}})]), encoding="utf-8")
            self.assertEqual(dev_finish.last_message({"transcript_path": str(path)}), "最后的话")
            self.assertEqual(dev_finish.last_message({"last_assistant_message": "直接给的"}), "直接给的")


class MynSaysItIsDone(unittest.TestCase):
    """MYNWindow's watcher, on a bare window with the parts it touches stubbed."""

    def setUp(self):
        import myn_gui
        temporary = tempfile.TemporaryDirectory(prefix="myn-devnote-")
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.win = myn_gui.MYNWindow.__new__(myn_gui.MYNWindow)
        self.win._dev_file = lambda name: self.state / name
        self.win._usage_config = lambda: self.state
        self.win.root = mock.Mock()
        self.said, self.shown, self.restarted = [], [], []
        self.win.append = lambda who, text, **kw: self.shown.append(text)
        self.win.speak = lambda text, **kw: self.said.append(text)
        self.win.stop_speech = lambda: None
        self.win._set_voice_label = lambda *a, **k: None
        self.win._restart_now = lambda: self.restarted.append(True)
        self.win.root.after.side_effect = lambda delay, fn, *a: fn(*a) if fn is self.win._restart_now else None
        self.win._speaking = lambda: False
        self.win.busy = False
        self.win.draft_active = False
        self.win.self_fix = None
        self.win.agent_runner = None

    def write(self, name, note):
        (self.state / name).write_text(json.dumps(note, ensure_ascii=False), encoding="utf-8")

    def test_program_change_waits_while_busy_then_restarts_and_the_next_start_says_it(self):
        self.write("dev-done.json", {"text": "Changes completed.\n\n修好了。", "spoken": "Changes completed.修好了。", "restart": True})
        self.win.busy = True
        self.win._dev_done_check()
        self.assertEqual(self.restarted, [])  # never in the middle of a reply
        self.win.busy = False
        self.win._dev_done_check()
        self.assertEqual(self.restarted, [True])
        self.assertTrue((self.state / "dev-notice.json").exists())
        # the new start
        self.win.dev_notice = None
        self.win._dev_startup()
        self.assertEqual(self.shown, ['Changes completed.\n\n修好了。'])
        self.assertEqual(self.win.dev_notice, 'Changes completed.修好了。')
        self.assertFalse((self.state / "dev-notice.json").exists())

    def test_only_tests_or_docs_changed_it_just_says_so(self):
        self.write("dev-done.json", {"text": "Task completed.", "spoken": "Task completed.", "restart": False})
        self.win._dev_done_check()
        self.assertEqual((self.restarted, self.said), ([], ['Task completed.']))
        self.assertFalse((self.state / "dev-done.json").exists())

    def test_a_note_left_while_myn_was_closed_is_shown_at_start(self):
        self.write("dev-done.json", {"text": "Changes completed.", "restart": True})
        self.win.dev_notice = None
        self.win._dev_startup()
        self.assertEqual(self.shown, ['Changes completed.'])
        self.assertFalse((self.state / "dev-done.json").exists())


if __name__ == "__main__":
    unittest.main()
