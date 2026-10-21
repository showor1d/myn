"""Completion checks for the interrupted evolution feature; all data is temporary."""
import asyncio
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src/core")]
import agent_worker
import conversation_log
import evolution
import evolution_sources
import local_actions
import self_update
import web_panels3


class Store(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="myn-evolution-check-")
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.config = self.folder / "config"

    def test_manual_completion_keeps_history_and_leaves_queue_after_restart(self):
        for status in evolution.OPEN:
            if status == "updating":
                continue
            with self.subTest(status=status):
                point, _ = evolution.add(self.config, "外部已解决" + status)
                evolution.update(self.config, point["id"], status=status, plan_approved=True)
                completed = evolution.decide(self.config, point["id"], "complete")
                self.assertEqual(completed["status"], "completed")
                self.assertTrue(completed["completed_at"])
                self.assertFalse(completed["plan_approved"])
                self.assertEqual(evolution.decide(self.config, point["id"], "complete"), completed)
                with self.assertRaises(ValueError):
                    evolution.decide(self.config, point["id"], "adjust", note="重新安排")
        evolution.recover(self.config)
        self.assertEqual(evolution.visible_points(self.config), [])
        self.assertEqual(evolution.next_work(self.config, 100000), (None, None))
        self.assertEqual(evolution.waiting_for_user(self.config), [])
        self.assertTrue(all(p["status"] == "completed" for p in evolution.visible_points(self.config, "completed")))

    def test_completion_views_are_separate_and_updating_is_protected(self):
        ids = {}
        for status in ("new", "updating", "updated", "completed", "rejected"):
            point, _ = evolution.add(self.config, "任务" + status)
            evolution.update(self.config, point["id"], status=status)
            ids[status] = point["id"]
        with self.assertRaises(ValueError):
            evolution.decide(self.config, ids["updating"], "complete")
        self.assertEqual({p["status"] for p in evolution.visible_points(self.config)}, {"new", "updating"})
        self.assertEqual({p["status"] for p in evolution.visible_points(self.config, "completed")}, {"completed", "updated"})
        self.assertEqual({p["status"] for p in evolution.visible_points(self.config, "rejected")}, {"rejected"})

    def test_closed_history_does_not_evict_open_work(self):
        first, _ = evolution.add(self.config, "最早任务")
        for i in range(4):
            point, _ = evolution.add(self.config, "已完成任务%d" % i)
            evolution.update(self.config, point["id"], status="rejected")
        with mock.patch.object(evolution, "MAX_POINTS", 2):
            evolution.add(self.config, "最新任务")
            with self.assertRaises(ValueError):
                evolution.add(self.config, "超过上限")
        self.assertIsNotNone(evolution.get(self.config, first["id"]))
        self.assertEqual(len(evolution.points(self.config)), 6)

    def test_opposite_requests_do_not_merge(self):
        for title in ("开启自动朗读", "关闭自动朗读", "说话快一点", "说话慢一点"):
            self.assertFalse(evolution.add(self.config, title)[1])
        self.assertTrue(evolution.add(self.config, "开启自动朗读功能")[1])

    def test_revision_failure_cannot_reuse_old_approval(self):
        point, _ = evolution.add(self.config, "修改字幕", adopted_plan="增加换行")
        evolution.decide(self.config, point["id"], "adjust", note="我想改为更大字号")
        evolution.update(self.config, point["id"], status="failed")
        self.assertEqual(evolution.decide(self.config, point["id"], "retry")["status"], "new")
        with self.assertRaises(ValueError):
            evolution.decide(self.config, point["id"], "adopt")

    def test_failed_approved_build_can_resume(self):
        point, _ = evolution.add(self.config, "修改字幕", adopted_plan="增加换行")
        evolution.update(self.config, point["id"], status="building")
        evolution.update(self.config, point["id"], status="failed")
        self.assertEqual(evolution.decide(self.config, point["id"], "retry")["status"], "adopted")

    def test_corrupt_store_is_preserved(self):
        self.config.mkdir()
        path = self.config / evolution.STORE
        for raw in ("broken", "{}", '[{"id":"e1"}]'):
            path.write_text(raw, encoding="utf-8")
            with self.assertRaises(ValueError):
                evolution.add(self.config, "不能覆盖")
            self.assertEqual(path.read_text(encoding="utf-8"), raw)

    def test_tokyo_month_excludes_next_month_and_l1(self):
        self.config.mkdir()
        records = []
        for stamp, use, n in (("2026-09-30T14:59:00Z", "evolution_plan", 1),
                              ("2026-09-30T15:00:00Z", "evolution_plan", 10),
                              ("2026-10-31T15:00:00Z", "evolution_plan", 100),
                              ("2026-10-19T00:00:00Z", "evolution_build_l1", 30)):
            records.append({"at_utc": stamp.replace("Z", "+00:00"), "model": "test", "purpose": use, "input_tokens": n})
        (self.config / "claude-usage.jsonl").write_text("\n".join(map(json.dumps, records)), encoding="utf-8")
        q = evolution.quota(self.config, datetime(2026, 10, 19, tzinfo=timezone.utc))
        self.assertEqual((q["tokens"], q["level1_tokens"]), (40, 30))

    def test_deadline_order_and_user_control(self):
        evolution.add(self.config, "不着急的事情", level=2)
        early, _ = evolution.add(self.config, "需要明天完成", level=2, deadline="2026-10-20 08:00")
        evolution.add(self.config, "后天才用", level=2, deadline="2026-10-21 08:00")
        self.assertEqual(evolution.next_work(self.config, 100)[0]["id"], early["id"])
        evolution.decide(self.config, early["id"], "deadline", note="")
        self.assertEqual(evolution.get(self.config, early["id"])["deadline"], "")
        self.assertEqual(evolution.next_work(self.config, 0), (None, None))
        evolution.decide(self.config, early["id"], "level", level=1)
        self.assertEqual(evolution.next_work(self.config, 0)[0]["id"], early["id"])
        with self.assertRaises(ValueError):
            evolution.normalize_deadline("2026-02-30 25:00")

    def test_model_deadlines_require_evidence_and_never_exempt(self):
        lines = [(1, "user", "2026-10-19 12:00", "明天早上八点之前要能响铃")]
        item = {"title": "响铃功能", "quote_ids": [1], "level": 1, "deadline": "2026-10-20 08:00"}
        point = evolution.parse_mining(json.dumps({"points": [item]}), lines, set())[0]
        self.assertEqual((point["deadline"], point["level"]), ("2026-10-20 08:00", 3))
        item["deadline"] = "2026-10-22 08:00"
        self.assertEqual(evolution.parse_mining(json.dumps({"points": [item]}), lines, set())[0]["deadline"], "")

    def test_bad_model_structures_rejected(self):
        with self.assertRaises(ValueError):
            evolution.parse_mining('{"points": {}}', [], set())
        for value in ('{"summary": []}', '{"exists": "false", "summary": "有效方案"}', '{broken'):
            with self.assertRaises(ValueError):
                evolution.parse_plan(value)
        self.assertIn('reliable estimate is unavailable', evolution.parse_plan('{"summary":"增加字幕换行"}')["cost_note"])

    def test_token_form_rejects_nonfinite_values(self):
        for value in ("infM", "nan万", "-2M", "1e100M"):
            with self.assertRaises(ValueError):
                web_panels3.parse_tokens(value)
        self.assertEqual(web_panels3.parse_tokens("2.5万"), 25000)
        self.assertEqual(web_panels3.conversation_time("2026-10-18T23:30:00+00:00"), "10-19 08:30")

    def test_direct_fix_defaults_to_quota_and_explicit_level_is_forwarded(self):
        calls = []
        context = local_actions.ActionContext(self.config, start_self_fix=lambda *a, **kw: calls.append((a, kw)) or {})
        local_actions._fix_myself({"problem": "把字幕换行修好"}, context)
        local_actions._fix_myself({"problem": "把字幕换行修好", "level": 1}, context)
        self.assertEqual(calls[0][1], {})
        self.assertEqual(calls[1][1], {"level": 1})

    def test_history_batches_do_not_skip_old_unread_lines(self):
        path = self.folder / "history.db"
        conversation_log.ensure_schema(path)
        with closing(sqlite3.connect(str(path))) as db:
            with db:
                for i in range(405):
                    db.execute("INSERT INTO conversation_messages(role,raw_text,display_text,created_at) VALUES('user',?,?,?)",
                               (str(i), str(i), datetime.now(timezone.utc).isoformat()))
        first, _ = evolution_sources.read(path, self.config)
        second, _ = evolution_sources.read(path, self.config, max(row[0] for row in first))
        self.assertEqual([row[0] for row in first + second], list(range(1, 406)))

    def test_feedback_uses_only_active_user_evidence_and_its_own_cursor(self):
        feedback = self.folder / "preferences/feedback.db"
        feedback.parent.mkdir()
        with closing(sqlite3.connect(str(feedback))) as db:
            with db:
                db.executescript("CREATE TABLE feedback(id INTEGER,status TEXT); "
                                 "CREATE TABLE feedback_events(id INTEGER,feedback_id INTEGER,kind TEXT,at TEXT,value TEXT);")
                db.executemany("INSERT INTO feedback VALUES(?,?)", [(1,"active"),(2,"revoked")])
                db.executemany("INSERT INTO feedback_events VALUES(?,?,?,?,?)", [
                    (1,1,"reason","2026-10-19T01:00:00Z","字幕太小"),
                    (2,2,"reason","2026-10-19T01:00:01Z","已经撤销的话"),
                    (3,1,"rate","2026-10-19T01:00:02Z","")])
        rows, cursor = evolution_sources.read(self.folder / "absent.db", self.config)
        self.assertEqual((rows[0][0], rows[0][3], cursor), (-1,"字幕太小",1))
        self.assertEqual(evolution_sources.read(self.folder / "absent.db", self.config, feedback_after=cursor)[0], [])


class Flow(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="myn-evolution-flow-")
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        import myn_gui
        self.win = myn_gui.MYNWindow.__new__(myn_gui.MYNWindow)
        self.win._usage_config = lambda: self.folder
        self.win._evolution_changed = mock.Mock()
        self.win._agent_log_add = mock.Mock()
        self.win._self_fix_discard = mock.Mock()
        self.win.append = mock.Mock()
        self.win.self_fix = self.win.evolution_run = self.win.agent_runner = None

    def test_rejected_plan_and_build_are_not_revived(self):
        point, _ = evolution.add(self.folder, "测试功能")
        evolution.update(self.folder, point["id"], status="planning")
        self.win.evolution_run = {"task_id":"plan1", "point_id":point["id"]}
        evolution.decide(self.folder, point["id"], "reject")
        self.win._evolution_event({"event":"done", "ok":True, "result":'{"summary":"不能复活"}'})
        self.win._evolution_built({"point":point["id"], "work":"W"}, [])
        self.assertEqual(evolution.get(self.folder, point["id"])["status"], "rejected")

    def test_completed_plan_is_cancelled_and_late_results_do_not_revive_it(self):
        point, _ = evolution.add(self.folder, "外部修复功能")
        evolution.update(self.folder, point["id"], status="planning")
        self.win.evolution_run = {"task_id":"plan1", "point_id":point["id"]}
        self.win.agent_runner = mock.Mock(busy=True)
        evolution.decide(self.folder, point["id"], "complete")
        self.win._evolution_cancel_obsolete()
        self.win.agent_runner.cancel.assert_called_once()
        self.win._evolution_event({"event":"done", "ok":True, "result":'{"summary":"不能复活"}'})
        self.win._evolution_built({"point":point["id"], "work":"W"}, [])
        self.win._evolution_build_failed({"point":point["id"]}, "迟到错误")
        self.assertEqual(evolution.get(self.folder, point["id"])["status"], "completed")

    def test_disabled_group_stops_background_work(self):
        local_actions.save_settings(self.folder, local_actions.ActionSettings(self_fix=False, agent=False))
        with mock.patch("gui_evolution.agent_client.installed") as installed:
            self.win._evolution_work(self.folder)
        installed.assert_not_called()

    def test_quota_pause_retains_the_copy(self):
        point, _ = evolution.add(self.folder, "测试功能", adopted_plan="用户已经采纳")
        evolution.update(self.folder, point["id"], status="building")
        self.win.self_fix = {"point":point["id"], "work":self.folder / "copy"}
        self.win._self_fix_quota_pause()
        saved = evolution.get(self.folder, point["id"])
        self.assertEqual((saved["status"], saved["work"]), ("adopted", str(self.folder / "copy")))
        self.win._self_fix_discard.assert_not_called()

    def test_plan_reader_cannot_escape_program_copy(self):
        self.assertTrue(agent_worker.planning_read_allowed("Read", {"file_path":"src/a.py"}, self.folder))
        self.assertFalse(agent_worker.planning_read_allowed("Read", {"file_path":"../private"}, self.folder))
        self.assertFalse(agent_worker.planning_read_allowed("Glob", {"pattern":"../**"}, self.folder))
        self.assertFalse(agent_worker.planning_read_allowed("Bash", {"command":"dir"}, self.folder))

    def test_worker_stops_at_budget_and_deduplicates_usage(self):
        options, events, interrupts = [], [], []
        class Obj:
            def __init__(self, **kw): self.__dict__.update(kw)
        class Client:
            def __init__(self, options): self.options = options
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            async def query(self, prompt): pass
            async def interrupt(self): interrupts.append(True)
            async def receive_response(self):
                cls = type("AssistantMessage", (Obj,), {})
                yield cls(message_id="one", usage={"input_tokens":7,"output_tokens":3}, content=[])
                yield cls(message_id="one", usage={"input_tokens":7,"output_tokens":3},
                          content=[type("ToolUseBlock", (Obj,), {})(name="Read",input={})])
                result = await self.options.hooks["PreToolUse"][0].hooks[0]({},None,None)
                self_result = result["continue_"]
                events.append({"stopped": not self_result})
        sdk = types.SimpleNamespace(types=types.SimpleNamespace(HookMatcher=Obj),
                                    ClaudeAgentOptions=Obj, ClaudeSDKClient=Client)
        async def run():
            worker = agent_worker.Worker(sdk, asyncio.get_running_loop())
            with mock.patch.object(agent_worker, "emit", events.append):
                await worker.run({"task_id":"budget", "cwd":str(self.folder), "mode":"plan", "token_budget":10})
        asyncio.run(run())
        done = events[-1]
        self.assertEqual(done["error"], "evolution_quota")
        self.assertEqual(sum(done["usage"].values()), 10)
        self.assertEqual(interrupts, [True])
        self.assertIn({"stopped": True}, events)


class CopyRecovery(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="myn-evolution-copy-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name, value in {
            "src/myn_gui.py": "import self_update\nself_update.on_startup()\nself_update.confirm()\n",
            "src/a.py": "A=1\n", "src/b.py": "B=1\n", "src/core/self_update.py": "# protected\n",
            "AGENTS.md": "# MYN\n\n## Permanent constraints\n\n保护数据。\n", "tests/test_a.py": "import unittest\n"
        }.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value, encoding="utf-8")
        self.work = self_update.prepare("test-copy", self.root)

    def test_edited_manifest_cannot_hide_protected_changes(self):
        target = self.work / "src/core/self_update.py"
        target.write_text("# tampered", encoding="utf-8")
        base = json.loads((self.work / self_update.BASE_NAME).read_text())
        base["src/core/self_update.py"] = self_update._digest(target)
        (self.work / self_update.BASE_NAME).write_text(json.dumps(base))
        self.assertTrue(any("Protected" in error for error in self_update.check(self.work, self.root)))
        self.assertEqual(self_update.live_changes(self.work, self.root), [])
        (self.root / "src/b.py").write_text("B=2\n")
        self.assertIn("src/b.py", self_update.live_changes(self.work, self.root))

    def test_automatic_tests_override_inherited_database_path(self):
        import os
        with mock.patch.dict(os.environ, {"MYN_DATA_DIR": "must-not-use", "ANTHROPIC_API_KEY": "test-secret"}), \
                mock.patch.object(self_update.subprocess, "run", return_value=types.SimpleNamespace(
                    stdout="OK\n", stderr="", returncode=0)) as run:
            failures, _ = self_update.run_tests(self.root)
        env = run.call_args.kwargs["env"]
        self.assertNotEqual(env["MYN_DATA_DIR"], "must-not-use")
        self.assertEqual(Path(env["MYN_DATA_DIR"]).parent, Path(env["MYN_PERSONAL_DATA_DIR"]))
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(failures, set())

    def test_partial_replacement_rolls_back_and_preserves_other_edits(self):
        (self.work / "src/a.py").write_text("A=2\n")
        (self.work / "src/c.py").write_text("C=2\n")
        replace = self_update._replace
        def fail(source, target):
            if Path(source) == self.work / "src/c.py":
                (self.root / "src/b.py").write_text("B=99\n")
                raise OSError("simulated replace failure")
            return replace(source, target)
        with mock.patch.object(self_update, "_replace", side_effect=fail):
            ok, _ = self_update.apply(self.work, "test", self.root, checker=lambda root:(True,"ok"))
        self.assertFalse(ok)
        self.assertEqual((self.root / "src/a.py").read_text(), "A=1\n")
        self.assertEqual((self.root / "src/b.py").read_text(), "B=99\n")
        self.assertFalse((self.root / "src/c.py").exists())
        self.assertFalse(self_update._pending_path(self.root).exists())

    def test_unconfirmed_update_cannot_be_overwritten(self):
        (self.work / "src/a.py").write_text("A=2\n")
        ok, _ = self_update.apply(self.work, "test", self.root, checker=lambda root:(True,"ok"), point_id="e1")
        self.assertTrue(ok)
        ok, detail = self_update.apply(self.work, "again", self.root, checker=lambda root:(True,"ok"))
        self.assertFalse(ok)
        self.assertIn('has not confirmed startup', detail)
        self.assertEqual(self_update.evolution_results(self.root)["e1"]["result"], "trying")
        self_update.on_startup(self.root)
        self_update.on_startup(self.root)
        self.assertEqual(self_update.evolution_results(self.root)["e1"]["result"], "rolled_back")


if __name__ == "__main__":
    unittest.main()
