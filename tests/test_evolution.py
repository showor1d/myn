'Evolution points: storage, levels, budget, plans, copy protection, chat tools, read-only planning and the GUI queue.'

import asyncio
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src" / "core")]

import agent_worker
import evolution
import local_actions
import self_update
import stt


class Temp(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="myn-evo-")
        self.addCleanup(temporary.cleanup)
        self.config = Path(temporary.name)


class Store(Temp):
    def test_add_merges_repeats_and_keeps_the_users_words(self):
        point, merged = evolution.add(self.config, "定个闹钟", "missing", request="帮我定个明天早上八点的闹钟")
        self.assertFalse(merged)
        self.assertEqual((point["status"], point["level"]), ("new", 3))
        # opposite wishes with nearly the same words must stay apart
        self.assertFalse(evolution.add(self.config, "说话慢一点", request="a")[1])
        self.assertFalse(evolution.add(self.config, "说话快一点", request="b")[1])
        again, merged = evolution.add(self.config, "定个闹钟功能", "missing", request="给我设个闹钟", level=2)
        self.assertTrue(merged)
        self.assertEqual(again["id"], point["id"])
        self.assertEqual(again["level"], 2)  # the more urgent level wins
        self.assertEqual([q["text"] for q in again["quotes"]], ["帮我定个明天早上八点的闹钟", "给我设个闹钟"])
        with self.assertRaises(ValueError):
            evolution.add(self.config, "x")

    def test_decisions_follow_the_flow(self):
        point, _ = evolution.add(self.config, "定闹钟", "missing", request="定闹钟")
        with self.assertRaises(ValueError):
            evolution.decide(self.config, point["id"], "adopt")  # nothing proposed yet
        evolution.update(self.config, point["id"], status="proposed", plan={"summary": "用定时任务", "plan": "..."})
        self.assertEqual(evolution.decide(self.config, point["id"], "adopt")["status"], "adopted")
        evolution.update(self.config, point["id"], status="proposed")
        adjusted = evolution.decide(self.config, point["id"], "adjust", note="要能响铃，不只是说话")
        self.assertEqual(adjusted["status"], "new")  # MYN thinks again, with the note
        self.assertIn("要能响铃", evolution.plan_prompt(adjusted))
        self.assertEqual(evolution.decide(self.config, point["id"], "level", level=1)["level"], 1)
        self.assertEqual(evolution.decide(self.config, point["id"], "reject")["status"], "rejected")
        with self.assertRaises(ValueError):
            evolution.decide(self.config, point["id"], "level", level=7)

    def test_unfinished_work_resumes_after_a_restart(self):
        a, _ = evolution.add(self.config, "定闹钟", request="x")
        b, _ = evolution.add(self.config, "说话慢一点", request="y")
        evolution.update(self.config, a["id"], status="planning")
        evolution.update(self.config, b["id"], status="building")
        evolution.recover(self.config)
        self.assertEqual(evolution.get(self.config, a["id"])["status"], "new")
        self.assertEqual(evolution.get(self.config, b["id"])["status"], "adopted")

    def test_queue_order_and_the_quota(self):
        low, _ = evolution.add(self.config, "换个颜色", request="x", level=3)
        soon, _ = evolution.add(self.config, "说话慢一点", request="y", level=2)
        evolution.update(self.config, soon["id"], status="adopted")
        now, _ = evolution.add(self.config, "定闹钟", request="z", level=1)
        self.assertEqual(evolution.next_work(self.config, 1000)[0]["id"], now["id"])  # Level one has priority.
        evolution.update(self.config, now["id"], status="rejected")
        self.assertEqual(evolution.next_work(self.config, 1000), (evolution.get(self.config, soon["id"]), "build"))
        self.assertEqual(evolution.next_work(self.config, 0), (None, None))  # quota used up: wait
        evolution.update(self.config, now["id"], status="new")
        self.assertEqual(evolution.next_work(self.config, 0)[0]["id"], now["id"])  # level one keeps working

    def test_quota_counts_only_this_months_ordinary_evolution_work(self):
        evolution.save_settings(self.config, evolution.Settings(monthly_tokens=10000))
        now = datetime.now(timezone.utc)
        last_month = (now.replace(day=1) - timedelta(days=2)).isoformat()
        lines = [{"at_utc": now.isoformat(), "purpose": "evolution_plan", "model": "claude-opus-5-5",
                  "input_tokens": 3000, "output_tokens": 1000},
                 {"at_utc": now.isoformat(), "purpose": "evolution_build_l1", "model": "claude-opus-5-5",
                  "input_tokens": 50000, "output_tokens": 0},
                 {"at_utc": now.isoformat(), "purpose": "chat_opus", "model": "claude-opus-5-5",
                  "input_tokens": 99999, "output_tokens": 0},
                 {"at_utc": last_month, "purpose": "evolution_mining", "model": "claude-opus-5-5",
                  "input_tokens": 99999, "output_tokens": 0}]
        (self.config / "claude-usage.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
        quota = evolution.quota(self.config)
        self.assertEqual((quota["tokens"], quota["level1_tokens"], quota["limit"]), (54000, 50000, 50))
        self.assertGreater(quota["used"], 0)
        self.assertAlmostEqual(quota["left"] + quota["used"], 50)
        self.assertEqual(evolution.purpose("plan", {"level": 1}), "evolution_plan_l1")
        self.assertEqual(evolution.purpose("plan", {"level": 2}), "evolution_plan")

    def test_settings(self):
        self.assertEqual(evolution.load_settings(self.config), evolution.Settings())
        evolution.save_settings(self.config, evolution.Settings(monthly_tokens=0, mining=False))
        self.assertEqual(evolution.load_settings(self.config).monthly_tokens, 0)
        with self.assertRaises(ValueError):
            evolution.save_settings(self.config, evolution.Settings(monthly_tokens=-1))
        (self.config / evolution.SETTINGS).write_text("{", encoding="utf-8")
        self.assertEqual(evolution.load_settings(self.config), evolution.Settings(monthly_tokens=0, monthly_usd=0, mining=False))


class PlansAndReading(unittest.TestCase):
    def test_plan_parsing(self):
        plan = evolution.parse_plan('想好了。\n{"exists": false, "summary": "用定时任务加一个“响铃”动作", '
                                    '"plan": "1. 在 timekeeper 加 alarm", "files": ["src/core/timekeeper.py"], "size": "small"}')
        self.assertEqual((plan["exists"], plan["size"], plan["files"]), (False, "small", ["src/core/timekeeper.py"]))
        exists = evolution.parse_plan('{"exists": true, "how_to_use": "说：每天早上 8 点叫我起床", "summary": "已经可以"}')
        self.assertTrue(exists["exists"])
        prose = evolution.parse_plan("可以用现有的定时任务来做，只要再加一个响铃的动作就行。")
        self.assertIn("响铃", prose["summary"])
        with self.assertRaises(ValueError):
            evolution.parse_plan("")

    def test_reading_back_needs_the_users_own_lines(self):
        lines = [(10, "user", "10-17 08:00", "你能不能定个闹钟"), (11, "assistant", "10-17 08:00", "我没有闹钟功能")]
        reply = json.dumps({"points": [
            {"title": "定闹钟", "kind": "missing", "why": "想被叫醒", "suggestion": "定时任务", "quote_ids": [10, 11],
             "level": 2, "same_as": "nope"},
            {"title": "MYN 自己说的", "kind": "improvement", "quote_ids": [11]},
            {"title": "编的", "kind": "improvement", "quote_ids": [999]}]}, ensure_ascii=False)
        found = evolution.parse_mining("好的\n" + reply, lines, {"e1"})
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0]["title"], found[0]["level"], found[0]["same_as"]), ("定闹钟", 2, None))
        self.assertEqual([q["message_id"] for q in found[0]["quotes"]], [10])
        prompt = evolution.mining_prompt(lines, [{"id": "e1", "title": "说话慢一点", "status": "new"}])
        self.assertIn('[10] User', prompt)
        self.assertIn("e1：说话慢一点", prompt)

    def test_what_myn_says(self):
        point = {"id": "e1", "title": "定闹钟", "kind": "missing", "level": 2, "status": "proposed",
                 "quotes": [{"text": "帮我定个明天早上八点的闹钟"}],
                 "plan": {"summary": "在定时任务里加一个会响铃的闹钟", "plan": "1. ..."}}
        text, spoken = evolution.announcement(point)
        self.assertIn('You said "帮我定个明天早上八点的闹钟", but this feature is not available yet', spoken)
        self.assertIn('The proposed plan is: 在定时任务里加一个会响铃的闹钟', spoken)
        self.assertIn('adopt', text)
        built = dict(point, status="built", changed=["src/core/timekeeper.py（修改，+20 −2）"])
        self.assertIn("Apply the update?", evolution.announcement(built)[1])


class CopySafety(unittest.TestCase):
    """A copy applied later changes only what it changed itself (self_update base record)."""

    GUI = "import self_update\nself_update.on_startup()\nself_update.confirm()\n"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="myn-copy-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "src" / "myn_gui.py").write_text(self.GUI, encoding="utf-8")
        (self.root / "src" / "a.py").write_text("A = 1\n", encoding="utf-8")
        (self.root / "src" / "b.py").write_text("B = 1\n", encoding="utf-8")
        (self.root / "tests" / "test_x.py").write_text("X = 1\n", encoding="utf-8")
        (self.root / "AGENTS.md").write_text("# MYN\n\n## Permanent constraints\n\n1. 规则\n", encoding="utf-8")

    def test_newer_live_changes_are_not_put_back(self):
        work = self_update.prepare("t1", self.root)
        (work / "src" / "b.py").write_text("B = 2\n", encoding="utf-8")      # the copy's change
        (self.root / "src" / "a.py").write_text("A = 99\n", encoding="utf-8")  # changed live meanwhile
        self.assertEqual(self_update.changes(work, self.root), {"src/b.py": "changed"})
        self.assertEqual(self_update.conflicts(work, self.root), [])
        ok, detail = self_update.apply(work, "test", self.root, checker=lambda root: (True, ""))
        self.assertTrue(ok, detail)
        self.assertEqual((self.root / "src" / "a.py").read_text(encoding="utf-8"), "A = 99\n")  # kept
        self.assertEqual((self.root / "src" / "b.py").read_text(encoding="utf-8"), "B = 2\n")

    def test_the_same_file_changed_on_both_sides_is_refused(self):
        work = self_update.prepare("t2", self.root)
        (work / "src" / "a.py").write_text("A = 2\n", encoding="utf-8")
        (self.root / "src" / "a.py").write_text("A = 99\n", encoding="utf-8")
        self.assertEqual(self_update.conflicts(work, self.root), ["src/a.py"])
        ok, detail = self_update.apply(work, "test", self.root, checker=lambda root: (True, ""))
        self.assertFalse(ok)
        self.assertIn(self_update.CONFLICT, detail)
        self.assertEqual((self.root / "src" / "a.py").read_text(encoding="utf-8"), "A = 99\n")


class ChatTools(Temp):
    def run_tool(self, name, arguments, confirm=None, update=None, policy="risky"):
        local_actions.save_settings(self.config, local_actions.ActionSettings(confirm=policy))
        context = local_actions.ActionContext(self.config, confirm=confirm, evolution_update=update)
        content, is_error = local_actions.execute(name, arguments, context)
        return json.loads(content), is_error

    def test_noting_and_deciding(self):
        result, error = self.run_tool("note_evolution_point", {"title": "定闹钟", "request": "帮我定个闹钟",
                                                               "kind": "missing", "level": 2})
        self.assertFalse(error)
        point_id = result["evolution_point"]["id"]
        self.assertEqual(evolution.get(self.config, point_id)["level"], 2)
        evolution.update(self.config, point_id, status="proposed", plan={"summary": "s", "plan": "p"})
        result, error = self.run_tool("evolution_points", {"action": "adopt", "id": point_id})
        self.assertFalse(error)
        self.assertEqual(evolution.get(self.config, point_id)["status"], "adopted")
        listed, _ = self.run_tool("evolution_points", {"action": "list"})
        self.assertEqual(listed["points"][0]["title"], "定闹钟")
        self.assertIn("quota", listed)

    def test_updating_always_asks_even_when_actions_run_directly(self):
        point, _ = evolution.add(self.config, "定闹钟", request="x")
        evolution.update(self.config, point["id"], status="built", work="W")
        updates, asked = [], []
        result, error = self.run_tool("evolution_points", {"action": "update", "id": point["id"]},
                                      confirm=lambda preview: asked.append(preview) or False,
                                      update=updates.append, policy="never")
        self.assertTrue(error)
        self.assertEqual(updates, [])
        self.assertIn("定闹钟", asked[0])
        result, error = self.run_tool("evolution_points", {"action": "update", "id": point["id"]},
                                      confirm=lambda preview: True,
                                      update=lambda pid: updates.append(pid) or {"updating": True}, policy="never")
        self.assertFalse(error)
        self.assertEqual(updates, [point["id"]])

    def test_voice_goes_to_conversation_instead_of_removed_page(self):
        for said in ("看看进化点", "打开进化点", "打开自我进化"):
            self.assertIsNone(stt.voice_command(said), said)


class PlanningWorker(unittest.TestCase):
    def test_planning_only_reads_and_never_asks(self):
        sdk_options, decisions, asks = {}, [], []

        class Obj:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        class Client:
            def __init__(self, options):
                sdk_options["o"] = options

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def query(self, prompt):
                pass

            async def receive_response(self):
                for tool, data in (("Bash", {"command": "dir"}), ("Write", {"file_path": "x"}), ("Read", {"file_path": "C:/x"})):
                    decisions.append(type(await sdk_options["o"].can_use_tool(tool, data, None)).__name__)
                if False:
                    yield None

        sdk = types.SimpleNamespace(types=types.SimpleNamespace(PermissionResultAllow=type("Allow", (Obj,), {}),
                                                                PermissionResultDeny=type("Deny", (Obj,), {}),
                                                                HookMatcher=Obj),
                                    ClaudeAgentOptions=type("O", (Obj,), {}), ClaudeSDKClient=Client)

        async def go():
            worker = agent_worker.Worker(sdk, asyncio.get_running_loop())
            with mock.patch.object(agent_worker, "emit", lambda m: asks.append(m) if m.get("event") == "ask" else None):
                await worker.run({"task_id": "p", "prompt": "plan", "cwd": "D:/MYN", "mode": "plan"})
        asyncio.run(go())
        options = sdk_options["o"]
        self.assertEqual(options.allowed_tools, ["Read", "Glob", "Grep"])
        for tool in ("Bash", "Write", "Edit", "WebFetch", "WebSearch"):
            self.assertIn(tool, options.disallowed_tools)
        self.assertEqual(decisions, ["Deny", "Deny", "Deny"])
        self.assertEqual(asks, [])  # refused without asking the user


class WindowFlow(Temp):
    """gui_evolution on a bare window with the parts it touches stubbed."""

    def setUp(self):
        super().setUp()
        import myn_gui
        win = self.win = myn_gui.MYNWindow.__new__(myn_gui.MYNWindow)
        win._usage_config = lambda: self.config
        win.root = mock.Mock()
        win.events = mock.Mock()
        self.shown, self.said, self.asked, self.restarted = [], [], [], []
        win.append = lambda who, text, **kw: self.shown.append(text)
        win.speak = lambda text, **kw: self.said.append(text)
        win.stop_speech = lambda: None
        win._set_voice_label = lambda *a, **k: None
        win._ask_user = lambda title, text, then, **kw: self.asked.append((text, then))
        win._restart_now = lambda: self.restarted.append(True)
        win.root.after.side_effect = lambda delay, fn, *a: fn(*a) if fn is win._restart_now else None
        win._speaking = lambda: False
        win._agent_log_add = lambda *a: None
        win.busy = win.draft_active = win.conversation_open = False
        win.self_fix = None
        win.agent_runner = None
        win._evolution_changed = lambda: None

    def test_a_plan_is_proposed_when_myn_is_free(self):
        point, _ = evolution.add(self.config, "定闹钟", "missing", request="帮我定个明天早上八点的闹钟")
        evolution.update(self.config, point["id"], status="planning")
        self.win.evolution_run = {"task_id": "plan1", "point_id": point["id"]}
        self.win._evolution_event({"event": "done", "task_id": "plan1", "ok": True, "result":
                                   '{"exists": false, "summary": "在定时任务里加一个会响铃的闹钟", "plan": "1. ..."}'})
        self.assertIsNone(self.win.evolution_run)
        self.assertEqual(evolution.get(self.config, point["id"])["status"], "proposed")
        self.win.busy = True
        self.win._evolution_announce(self.config)
        self.assertEqual(self.said, [])  # never in the middle of a reply
        self.win.busy = False
        self.win._evolution_announce(self.config)
        self.assertIn('good time to review', self.said[0])
        self.assertNotEqual(evolution.get(self.config, point["id"])["announced"], "proposed")
        self.win._evolution_said_at = 0
        self.win.conversation_open = False
        self.win._evolution_announce(self.config)
        self.assertEqual(len(self.said), 1)  # said once

    def test_a_built_point_is_updated_only_after_yes(self):
        point, _ = evolution.add(self.config, "定闹钟", request="x")
        evolution.update(self.config, point["id"], status="built", work=str(self.config / "work"), result="加了闹钟")
        self.win._evolution_announce(self.config)
        self.assertIn('good time to review', self.said[0])
        self.assertEqual(self.asked, [])
        self.win._evolution_ask_update(evolution.get(self.config, point["id"]))
        text, then = self.asked[0]
        then(False)  # Deferred for later.
        self.assertEqual(evolution.get(self.config, point["id"])["status"], "built")
        with mock.patch("gui_evolution.threading.Thread") as thread, \
                mock.patch.object(self_update, "live_changes", return_value=[]), \
                mock.patch.object(self_update, "apply", return_value=(True, "backup-dir")):
            thread.side_effect = lambda target, daemon: types.SimpleNamespace(start=target)
            self.win.events.put.side_effect = lambda item: item[1]()
            then(True)
        self.assertEqual(evolution.get(self.config, point["id"])["status"], "updated")
        self.assertEqual(self.restarted, [True])

    def test_a_copy_overtaken_by_newer_changes_is_built_again(self):
        point, _ = evolution.add(self.config, "定闹钟", request="x")
        evolution.update(self.config, point["id"], status="built", work=str(self.config / "work"))
        with mock.patch("gui_evolution.threading.Thread") as thread, \
                mock.patch.object(self_update, "live_changes", return_value=["src/core/timekeeper.py"]):
            thread.side_effect = lambda target, daemon: types.SimpleNamespace(start=target)
            self.win.events.put.side_effect = lambda item: item[1]()
            self.win._evolution_update(point["id"])
        self.assertEqual(evolution.get(self.config, point["id"])["status"], "adopted")
        self.assertEqual(self.restarted, [])

    def test_a_finished_build_is_kept_for_the_users_word(self):
        point, _ = evolution.add(self.config, "定闹钟", request="x")
        evolution.update(self.config, point["id"], status="building")
        fix = {"point": point["id"], "work": self.config / "work", "summary": "加了闹钟"}
        self.win.self_fix = fix
        self.win._evolution_built(fix, ["src/core/timekeeper.py（修改，+20 −2）"])
        saved = evolution.get(self.config, point["id"])
        self.assertEqual((saved["status"], saved["work"]), ("built", str(self.config / "work")))
        self.assertIsNone(self.win.self_fix)

    def test_adopted_tested_copy_updates_and_restarts_without_a_second_question(self):
        point, _ = evolution.add(self.config, "字幕修复", adopted_plan="修改字幕换行")
        evolution.update(self.config, point["id"], status="building")
        fix = {"point": point["id"], "work": self.config / "work", "summary": "修改字幕换行"}
        self.win.self_fix = fix
        with mock.patch("gui_evolution.threading.Thread") as thread, \
                mock.patch.object(self_update, "live_changes", return_value=[]), \
                mock.patch.object(self_update, "apply", return_value=(True, "backup-dir")) as apply:
            thread.side_effect = lambda target, daemon: types.SimpleNamespace(start=target)
            self.win.events.put.side_effect = lambda item: item[1]()
            self.win._evolution_built(fix, ["src/gui_conversation.py"])
        apply.assert_called_once()
        self.assertEqual(self.asked, [])
        self.assertEqual(self.restarted, [True])
        self.assertEqual(evolution.get(self.config, point["id"])["status"], "updated")

    def test_auto_update_waits_for_idle_and_resumes_without_api_or_quota(self):
        point, _ = evolution.add(self.config, "字幕修复", adopted_plan="修改字幕换行")
        evolution.update(self.config, point["id"], status="built", work="W")
        evolution.save_settings(self.config, evolution.Settings(monthly_tokens=0))
        evolution.recover(self.config)
        self.win._evolution_update = mock.Mock()
        for busy_attr in ("busy", "conversation_open", "draft_active", "pending_confirmation"):
            setattr(self.win, busy_attr, True)
            self.win._evolution_work(self.config)
            self.win._evolution_update.assert_not_called()
            setattr(self.win, busy_attr, False)
        self.win._evolution_work(self.config)  # no session/API key needed for applying
        self.win._evolution_update.assert_called_once_with(point["id"])
        self.assertEqual(evolution.waiting_for_user(self.config), [])

    def test_auto_update_requires_adoption_and_respects_disabled_group(self):
        point, _ = evolution.add(self.config, "字幕修复")
        evolution.update(self.config, point["id"], status="built", work="W")
        self.win._evolution_update = mock.Mock()
        self.assertFalse(self.win._evolution_auto_update(self.config))
        evolution.update(self.config, point["id"], plan_approved=True)
        local_actions.save_settings(self.config, local_actions.ActionSettings(self_fix=False))
        self.assertFalse(self.win._evolution_auto_update(self.config))
        self.win._evolution_update.assert_not_called()

    def test_failed_auto_apply_preserves_failure_without_restart(self):
        point, _ = evolution.add(self.config, "字幕修复", adopted_plan="修改字幕换行")
        evolution.update(self.config, point["id"], status="built", work="W")
        with mock.patch("gui_evolution.threading.Thread") as thread, \
                mock.patch.object(self_update, "live_changes", return_value=[]), \
                mock.patch.object(self_update, "apply", return_value=(False, "模拟检查失败")):
            thread.side_effect = lambda target, daemon: types.SimpleNamespace(start=target)
            self.win.events.put.side_effect = lambda item: item[1]()
            self.win._evolution_auto_update(self.config)
        self.assertEqual(evolution.get(self.config, point["id"])["status"], "failed")
        self.assertEqual(self.restarted, [])
        self.assertEqual(self.asked, [])


if __name__ == "__main__":
    unittest.main()
