"""Dollar budget, restart-safe daily reports, and conversation-only management."""
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src/core")]
import agent_client
import evolution
import interaction
import local_actions
import stt


class VoiceEvolution(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="myn-evolution-budget-")
        self.addCleanup(temp.cleanup)
        self.config = Path(temp.name)

    def spend(self, amount, purpose="evolution_plan", when=None):
        row = {"at_utc": (when or datetime.now(timezone.utc)).isoformat(),
               "model": "claude-sonnet-5-5", "purpose": purpose,
               "input_tokens": 100, "cost_usd": amount}
        with (self.config / "claude-usage.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")

    def tool(self, **args):
        text, error = local_actions.execute("evolution_points", args, local_actions.ActionContext(self.config))
        self.assertFalse(error, text)
        return json.loads(text)

    def proposed(self, title="字幕换行"):
        point, _ = evolution.add(self.config, title, request="希望改进" + title)
        return evolution.update(self.config, point["id"], status="proposed", plan={"summary": "改善显示", "plan": "支持自动换行"})

    def test_default_legacy_migration_and_invalid_budget(self):
        (self.config / evolution.SETTINGS).write_text('{"monthly_tokens": 1234, "mining": false}', encoding="utf-8")
        saved = evolution.load_settings(self.config)
        self.assertEqual(saved.monthly_usd, 50)
        self.assertFalse(saved.mining)
        for amount in (-1, True, float("nan"), float("inf"), "30"):
            with self.assertRaises(ValueError):
                evolution.save_settings(self.config, replace(saved, monthly_usd=amount))
        self.assertEqual(evolution.load_settings(self.config), saved)
        self.tool(action="settings", monthly_usd=0)
        self.assertFalse(evolution.quota(self.config)["can_start"])

    def test_monthly_cost_includes_level_one_but_not_external_development(self):
        self.spend(4, "evolution_plan", datetime(2026, 9, 30, 15, tzinfo=timezone.utc))
        self.spend(7, "evolution_build_l1", datetime(2026, 10, 4, tzinfo=timezone.utc))
        self.spend(2, "self_fix", datetime(2026, 10, 5, tzinfo=timezone.utc))
        self.spend(200, "chat", datetime(2026, 10, 4, tzinfo=timezone.utc))
        self.spend(200, "codex", datetime(2026, 10, 4, tzinfo=timezone.utc))
        self.spend(99, when=datetime(2026, 10, 31, 15, tzinfo=timezone.utc))
        q = evolution.quota(self.config, datetime(2026, 10, 5, tzinfo=timezone.utc))
        self.assertEqual((q["cost_usd"], q["remaining_usd"], q["tokens"]), (13, 37, 300))

    def test_slowdown_stop_tolerance_and_month_rollover(self):
        self.tool(action="settings", monthly_usd=30)
        routine, _ = evolution.add(self.config, "常规改进", level=3)
        self.assertEqual(evolution.next_work(self.config)[0]["id"], routine["id"])
        self.spend(27)
        self.assertEqual(evolution.next_work(self.config), (None, None))
        urgent, _ = evolution.add(self.config, "优先进化", level=1)
        self.assertEqual(evolution.next_work(self.config)[0]["id"], urgent["id"])
        evolution.decide(self.config, urgent["id"], "reject")
        approved = self.proposed("已采纳的改进")
        evolution.decide(self.config, approved["id"], "adopt")
        self.assertEqual(evolution.next_work(self.config)[1], "build")
        self.spend(3)
        self.assertEqual(evolution.next_work(self.config), (None, None))
        self.assertIsNone(evolution.runner_settings(self.config, agent_client.AgentSettings()))
        self.assertEqual(evolution.runner_settings(self.config, agent_client.AgentSettings(), continuing=True).max_budget_usd, 3)
        self.spend(3)
        self.assertIsNone(evolution.runner_settings(self.config, agent_client.AgentSettings(), continuing=True))
        next_month = (datetime.now(evolution.LOCAL_TIMEZONE).replace(day=28) + timedelta(days=4)).replace(day=1)
        self.assertEqual(evolution.quota(self.config, next_month)["stage"], "normal")

    def test_sdk_limit_does_not_create_paid_retry_loop(self):
        self.tool(action="settings", monthly_usd=30)
        self.spend(29)
        self.assertEqual(evolution.runner_settings(self.config, agent_client.AgentSettings()).max_budget_usd, 4)
        evolution.pause_budget(self.config)
        self.assertIsNone(evolution.runner_settings(self.config, agent_client.AgentSettings(), continuing=True))
        self.assertEqual(evolution.next_work(self.config), (None, None))
        self.tool(action="settings", monthly_usd=40)
        self.assertEqual(evolution.runner_settings(self.config, agent_client.AgentSettings()).max_budget_usd, 15)

    def test_daily_offer_survives_restart_and_waits_for_idle(self):
        point = self.proposed()
        now = datetime(2026, 10, 21, 14, 59, tzinfo=timezone.utc)  # 23:59 Tokyo
        interaction.observe(self.config, "我要睡觉了")
        with patch.object(evolution, "_now", return_value=now):
            self.assertFalse(interaction.offer(self.config, point))
            interaction.observe(self.config, "我有空了")
            self.assertTrue(interaction.offer(self.config, point))
            evolution.decide(self.config, point["id"], "reject")
            interaction.current_invitation(self.config)
            another = self.proposed("另一个改进")
            # Re-read persisted state as a new process would.
            self.assertEqual(interaction.load(self.config)["evolution_report_day"], "2026-10-21")
            self.assertFalse(interaction.offer(self.config, another))
        with patch.object(evolution, "_now", return_value=now + timedelta(minutes=2)):
            self.assertTrue(interaction.offer(self.config, another))

    def test_conversation_tools_cover_former_page_and_priorities(self):
        for phrase in ("打开进化点", "看看进化任务", "把进化预算调整到四十美元"):
            self.assertIsNone(stt.voice_command(phrase))
        point = self.proposed()
        self.tool(action="level", id=point["id"], level=1)
        self.tool(action="adjust", id=point["id"], note="还需要保留原来的字体")
        self.assertEqual(evolution.get(self.config, point["id"])["status"], "new")
        self.tool(action="deadline", id=point["id"], note="2026-12-01 08:00")
        self.tool(action="complete", id=point["id"])
        self.assertEqual(self.tool(action="list")["points"], [])
        self.assertEqual(self.tool(action="list", view="completed")["points"][0]["id"], point["id"])
        result = self.tool(action="settings", monthly_usd=45, mining=False, default_level=2)
        self.assertEqual(result["settings"], {"monthly_usd": 45, "task_usd": 15, "mining": False, "default_level": 2})
        self.assertTrue(result["saved"])
        self.assertFalse(self.tool(action="settings")["saved"])

    def test_lowering_budget_cancels_running_work_and_keeps_copy(self):
        import myn_gui
        win = myn_gui.MYNWindow.__new__(myn_gui.MYNWindow)
        win._usage_config = lambda: self.config
        win._evolution_changed = Mock()
        win._agent_log_add = Mock()
        win._self_fix_discard = Mock()
        win.agent_runner = Mock(busy=True)
        point = self.proposed()
        evolution.decide(self.config, point["id"], "adopt")
        evolution.update(self.config, point["id"], status="building")
        win.self_fix = {"point": point["id"], "work": "saved-copy", "budget_limit": 30}
        self.tool(action="settings", monthly_usd=20)
        win._evolution_cancel_obsolete()
        win.agent_runner.cancel.assert_called_once()
        win._self_fix_round_done({"error": "cancelled", "ok": False})
        saved = evolution.get(self.config, point["id"])
        self.assertEqual((saved["status"], saved["work"]), ("adopted", "saved-copy"))
        win._self_fix_discard.assert_not_called()

    def test_explicit_review_does_not_use_daily_proactive_slot(self):
        first = self.proposed()
        second = self.proposed("另一个方案")
        self.assertTrue(interaction.offer(self.config, first))
        for point in (first, second):
            self.assertIn("review_text", self.tool(action="show", id=point["id"]))
        self.tool(action="adopt", id=second["id"])
        self.assertTrue(evolution.get(self.config, second["id"])["plan_approved"])

    def test_spoken_text_reaches_real_chat_tools_and_persists(self):
        import chat
        import claude_client
        from data_location import config_for_db
        session = chat.ChatSession(self.config / "database" / "jarvis.db")
        config = config_for_db(session.db_path)
        captured = []
        def model(*args, **kwargs):
            names = {tool["name"] for tool in kwargs["read_tools"]}
            self.assertIn("note_evolution_point", names)
            self.assertIn("evolution_points", names)
            captured.append(kwargs["tool_executor"]("note_evolution_point", {
                "title": "学习时的字幕改进", "request": args[1], "kind": "improvement", "level": 1}))
            captured.append(kwargs["tool_executor"]("evolution_points", {"action": "settings", "monthly_usd": 42}))
            return "已记录一级进化点，月预算已调整为42美元。"
        with patch.object(session, "_optional_key", return_value="test-key"), patch.object(claude_client, "coordinated_reply", side_effect=model):
            result = session.ask("帮我建一个改进学习字幕的一级进化任务，月预算以后按42美元来", heard=True)
        self.assertEqual([error for _, error in captured], [False, False])
        self.assertEqual(evolution.points(config)[0]["level"], 1)
        self.assertEqual(evolution.load_settings(config).monthly_usd, 42)
        self.assertIn("42", result["answer"])
        self.assertIn("evolution_points", result["memory_note"])


if __name__ == "__main__":
    unittest.main()
