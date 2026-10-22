"""Isolated accounting: no duplicate billing, truthful call counts and restart outcomes."""
import asyncio
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src/core"), str(BASE / "tests")]
import agent_worker
import evolution
import evolution_receipt
import expenses
import sheets
import usage_detail
import usage_stats
import usage_window
from gui_evolution import EvolutionMixin
from test_work_mode import FakeSdk


class Accounting(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="myn-cost-receipt-")
        self.addCleanup(folder.cleanup)
        self.config = Path(folder.name)
        self.now = datetime.now(expenses.TOKYO)
        self.point, _ = evolution.add(self.config, "Fix sleep speech", adopted_plan="Improve speech routing")

    def write(self, rows):
        (self.config / usage_stats.USAGE_NAME).write_text(
            "".join(json.dumps(dict(at_utc=self.now.isoformat(), model="claude-opus-5-5", **r)) + "\n" for r in rows), encoding="utf-8")

    def agent(self, run="run-1", complete=True, phase="build"):
        steps = [{"id": "message-1", "at_utc": self.now.isoformat(), "model": "claude-opus-5-5",
                  "usage": {"input_tokens": 100, "output_tokens": 20}, "tools": ["Read"]},
                 {"id": "message-2", "at_utc": self.now.isoformat(), "model": "claude-opus-5-5",
                  "usage": {"input_tokens": 100, "output_tokens": 40}, "tools": ["Edit", "PowerShell"]}]
        usage_stats.record_agent(self.config / usage_stats.USAGE_NAME, "claude-opus-5-5",
            evolution.purpose(phase), {"input_tokens": 200, "output_tokens": 60}, .12,
            task_key="evolution:" + self.point["id"], run_id=run, steps=steps, calls_complete=complete)

    def test_day_keeps_same_model_calls_separate_and_classifies_work(self):
        self.write([dict(purpose=p, input_tokens=100, cost_usd=.01) for p in
                    ("chat_opus", "chat_opus", "scene_analysis", "preference_reflection", "evolution_build")])
        report = expenses.report(self.config, "day", category="token", now=self.now)
        self.assertEqual(len(report["rows"]), 5)
        self.assertEqual([r["name"] for r in report["rows"]], ["claude-opus-5-5"] * 5)
        self.assertEqual({r["usage_category"] for r in report["rows"]}, {"Daily chat", "Long-term memory", "Self-improvement"})
        self.assertEqual(report["token_total"], 500)
        self.assertEqual(report["cost"], "$0.05 USD")
        for period in ("week", "month", "year"):
            summary = expenses.report(self.config, period, category="token", now=self.now)
            self.assertEqual(len(summary["rows"]), 1)
            self.assertEqual(summary["rows"][0]["name"], "Opus 5.5")
            self.assertNotIn("usage_category", summary["rows"][0])
            self.assertEqual(summary["token_total"], 500)
            self.assertEqual(summary["cost"], "$0.05 USD")

    def test_legacy_token_view_uses_daily_details_only(self):
        self.write([dict(purpose="chat_opus", input_tokens=100, cost_usd=.001)] * 2)
        for period, size in (("day", 2), ("week", 1), ("month", 1), ("year", 1)):
            result = usage_stats.report(self.config, period, now=self.now)
            data = usage_window.receipt_data(result, period)
            self.assertEqual(len(data["rows"]), size)
            if period == "day":
                self.assertEqual(data["rows"][0]["cost"], "$0.001000")
                self.assertIn("Daily chat", data["rows"][0]["usage_category"])

    def test_agent_details_sum_to_task_total_without_double_billing(self):
        self.agent()
        self.agent()  # duplicate result delivery
        self.assertEqual(len(usage_stats.load_calls(self.config)), 1)
        rows = usage_detail.entries(usage_stats.load_calls(self.config), self.config)
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(sum(r["cost_usd"] for r in rows), .12)
        self.assertEqual(sum(r["tokens"] for r in rows), 260)
        self.assertTrue(all(r["cost_basis"] == "Allocated estimate" for r in rows))
        self.assertEqual(usage_stats.report(self.config, "month", now=self.now)["totals"]["cost_usd"], .12)

    def test_retries_remain_separate_and_count_towards_point_total(self):
        self.agent("plan", phase="plan")
        self.agent("build-1")
        self.agent("build-2")
        point = dict(self.point, status="updated", verification={"executed": True, "accepted": True, "ran": 80, "skipped": 2, "failed": 0})
        receipt = evolution_receipt.build(self.config, point, {"result": "kept"})
        self.assertEqual(receipt["calls"], 6)
        self.assertTrue(receipt["calls_complete"])
        self.assertAlmostEqual(receipt["total_usd"], .36)
        self.assertEqual(len(receipt["stages"]), 5)  # plan, two builds, local verification/restart
        self.assertIn("80 tests", str(receipt))

    def test_no_success_receipt_before_startup_confirmation(self):
        point = dict(self.point, status="updated")
        self.assertIsNone(evolution_receipt.build(self.config, point, {"result": "trying"}))
        self.assertIsNone(evolution_receipt.build(self.config, point, {}))
        self.assertIn("Succeeded", evolution_receipt.build(self.config, point, {"result": "kept"})["status"])

    def test_failures_and_rollbacks_are_not_presented_as_success(self):
        self.agent()
        for point, result in ((dict(self.point, status="updated"), {"result": "rolled_back"}),
                              (dict(self.point, status="failed", reason="Tests failed"), {})):
            receipt = evolution_receipt.build(self.config, point, result)
            self.assertTrue(receipt["status"].startswith("Failed"))
            self.assertEqual(receipt["total_usd"], .12)

    def test_earlier_restart_result_cannot_confirm_a_new_attempt(self):
        point = dict(self.point, status="updated", receipt_requested_at="2026-10-21T12:00:00+00:00")
        self.assertIsNone(evolution_receipt.build(self.config, point,
            {"result": "kept", "at_utc": "2026-10-20T12:00:00+00:00"}))

    def test_missing_historical_call_counts_are_explicit(self):
        self.write([dict(purpose="evolution_build", status="agent_done", input_tokens=100,
                         cost_usd=.3, task_key="evolution:" + self.point["id"])])
        receipt = evolution_receipt.build(self.config, dict(self.point, status="updated"), {"result": "kept"})
        self.assertFalse(receipt["calls_complete"])
        self.assertIn("Unavailable in history", str(receipt))
        self.assertEqual(receipt["total_usd"], .3)

    def test_partial_observation_does_not_allocate_all_cost_to_known_calls(self):
        self.agent(complete=False)
        rows = usage_detail.entries(usage_stats.load_calls(self.config), self.config)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["calls_complete"])
        self.assertIn("at least 2", usage_detail.description(rows[0]))
        self.assertEqual(rows[0]["cost_usd"], .12)

    def test_malformed_call_detail_preserves_the_billed_total(self):
        for detail in ("broken", [None], [{"id": [], "usage": {}}],
                       [{"id": "a", "model": "claude-opus-5-5", "usage": {"input_tokens": "bad"}}]):
            with self.subTest(detail=detail):
                self.write([dict(purpose="evolution_build", status="agent_done", input_tokens=100,
                                 cost_usd=.3, usage_steps=detail, calls_complete=True)])
                rows = usage_detail.entries(usage_stats.load_calls(self.config), self.config)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["cost_usd"], .3)
                self.assertFalse(rows[0]["calls_complete"])

    def test_popup_is_pending_across_restart_and_closes_once(self):
        evolution.update(self.config, self.point["id"], status="updated", receipt_pending=True)
        class Window(EvolutionMixin):
            _evolution_receipt_open = False
            web_active = True
            def _evolution_config(inner):
                return self.config
        window = Window()
        sent = []
        window.sheets = sheets.SheetHost(sent.append)
        with mock.patch("self_update.evolution_results", return_value={self.point["id"]: {"result": "kept"}}):
            window._evolution_receipts()
            window._evolution_receipts()
            self.assertEqual(len(sent), 1)
            self.assertTrue(evolution.get(self.config, self.point["id"])["receipt_pending"])
            window.sheets.event({"event": "sheet_answer", "id": sent[0]["id"], "yes": True})
            window._evolution_receipts()
            self.assertEqual(len(sent), 1)
        self.assertFalse(evolution.get(self.config, self.point["id"])["receipt_pending"])


class WorkerCalls(unittest.TestCase):
    def run_messages(self, messages):
        sdk = FakeSdk([])
        class Client(sdk.ClaudeSDKClient):
            async def receive_response(self):
                for item in messages:
                    yield item
        sdk.ClaudeSDKClient = Client
        emitted = []
        async def run():
            with mock.patch.object(agent_worker, "emit", emitted.append):
                await agent_worker.Worker(sdk, asyncio.get_running_loop()).run({"task_id": "t", "prompt": "synthetic", "cwd": str(BASE)})
        asyncio.run(run())
        return emitted[-1]

    def message(self, kind, **kw):
        return type(kind, (), kw)()

    def test_message_fragments_deduplicate_by_model_message_id(self):
        messages = [self.message("AssistantMessage", message_id="a", model="actual-model",
            usage=dict(input_tokens=100, output_tokens=n), content=[self.message("TextBlock", text="private response")]) for n in (10, 30)]
        messages += [self.message("ResultMessage", subtype="success", is_error=False, num_turns=9,
            total_cost_usd=.1, usage=dict(input_tokens=100, output_tokens=30), result="done")]
        done = self.run_messages(messages)
        self.assertEqual(len(done["usage_steps"]), 1)
        self.assertTrue(done["calls_complete"])
        self.assertEqual(done["usage_steps"][0]["model"], "actual-model")
        self.assertEqual(done["usage_steps"][0]["usage"]["output_tokens"], 30)
        self.assertNotIn("private response", str(done["usage_steps"]))

    def test_missing_ids_or_missing_usage_do_not_claim_exact_call_count(self):
        done = self.run_messages([self.message("AssistantMessage", content=[], model="m", usage={})])
        self.assertFalse(done["calls_complete"])

    def test_worker_turn_count_is_not_a_model_call_count(self):
        done = self.run_messages([self.message("ResultMessage", subtype="success", is_error=False,
            num_turns=14, total_cost_usd=.2, usage=dict(input_tokens=200), result="done")])
        self.assertEqual(done["usage_steps"], [])
        self.assertFalse(done["calls_complete"])

    def test_transport_uuids_do_not_prove_distinct_model_calls(self):
        messages = [self.message("AssistantMessage", uuid="frame-" + str(n), model="m",
                    usage={"input_tokens": 10}, content=[]) for n in (1, 2)]
        done = self.run_messages(messages)
        self.assertFalse(done["calls_complete"])
        self.assertTrue(all(not row["identified"] for row in done["usage_steps"]))
