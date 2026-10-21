"""Codex counters, subagents and official Astra cost estimates (isolated)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src" / "core")]

import codex_usage
import usage_stats


def event(total, at="2026-10-18T17:30:00Z", last=None):
    return {"type": "event_msg", "timestamp": at,
            "payload": {"type": "token_count", "info": {"total_token_usage": total, "last_token_usage": last}}}


def counts(inp, out, cached=0, written=0, reasoning=0):
    return {"input_tokens": inp, "output_tokens": out, "cached_input_tokens": cached,
            "cache_write_input_tokens": written, "reasoning_output_tokens": reasoning,
            "total_tokens": inp + out}


class CodexUsage(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="myn-codex-usage-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.home, self.root, self.config = self.base / "codex", self.base / "MYN", self.base / "config"
        self.logs = self.home / "sessions" / "2026" / "10" / "19"
        self.logs.mkdir(parents=True)
        self.root.mkdir()

    def log(self, thread, events, session="root", root=None, inherited=False):
        meta = {"type": "session_meta", "payload": {
            "id": thread, "session_id": session, "cwd": str(root or self.root),
            "timestamp": "2026-10-18T17:25:00Z", "base_instructions": "secret instructions"}}
        records = [meta]
        if inherited:
            records.append({"type": "session_meta", "payload": {
                "id": "root", "session_id": "root", "cwd": str(self.root),
                "timestamp": "2026-10-18T17:20:00Z"}})
        records += [{"type": "turn_context", "payload": {"model": "gpt-test", "cwd": str(self.root)}},
                    {"type": "response_item", "payload": {"secret": "private answer"}}] + events
        path = self.logs / (thread + ".jsonl")
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        return path

    def sync(self, session_id="root"):
        return codex_usage.sync(self.config, session_id=session_id, home=self.home, root=self.root)

    def rows(self):
        return [json.loads(line) for line in (self.config / usage_stats.CODEX_NAME).read_text(encoding="utf-8").splitlines()]

    def test_cache_and_reasoning_are_subsets_and_astra_estimate_is_counted(self):
        self.log("root", [event(counts(1000, 100, cached=600, written=100, reasoning=50))])
        result = self.sync()
        row = self.rows()[0]
        self.assertEqual(result["tokens"], 1100)
        self.assertEqual([row[n] for n in usage_stats.TOKEN_FIELDS], [300, 100, 100, 600])
        self.assertEqual(row["reasoning_output_tokens"], 50)
        self.assertAlmostEqual(row["cost_usd"], 0.00985)
        self.assertAlmostEqual(result["cost_usd"], 0.00985)
        self.assertTrue(row["estimated"])
        self.assertEqual(row["pricing"]["model"], "gpt-6-astra")
        calls = usage_stats.load_calls(self.config)
        self.assertEqual(sum(call.tokens for call in calls), 1100)
        self.assertTrue(calls[0].priced)
        self.assertTrue(calls[0].estimated)
        self.assertAlmostEqual(calls[0].cost, 0.00985)
        self.assertEqual(usage_stats.purpose_name(calls[0].purpose), "Codex")
        self.assertEqual(usage_stats._totals(calls)["unpriced_tokens"], 0)
        text = (self.config / usage_stats.CODEX_NAME).read_text(encoding="utf-8")
        self.assertNotIn("secret", text)
        self.assertNotIn("private answer", text)

    def test_old_unknown_dollars_backfilled_without_recount_and_exact_backup(self):
        values = counts(1000, 100, cached=600)
        self.log("root", [event(values, last=values)])
        self.sync()
        ledger = self.config / usage_stats.CODEX_NAME
        rows = self.rows()
        rows[0]["cost_usd"] = None
        rows[0].pop("estimated")
        rows[0].pop("pricing")
        original = (json.dumps(rows[0]) + "\r\n" + "broken trailing line").encode("utf-8")
        ledger.write_bytes(original)
        result = self.sync()
        self.assertEqual((result["added_tokens"], result["tokens"], result["repriced_rows"]), (0, 1100, 1))
        self.assertAlmostEqual(result["cost_usd"], .0096)
        backup = self.config / "codex-usage.before-astra-pricing.jsonl"
        self.assertEqual(backup.read_bytes(), original)
        after = ledger.read_bytes()
        self.assertIn(b"broken trailing line", after)
        self.assertEqual(self.sync()["repriced_rows"], 0)
        self.assertEqual(ledger.read_bytes(), after)
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(sum(c.tokens for c in usage_stats.load_calls(self.config)), 1100)

    def test_priced_rows_never_overwritten_and_missing_logs_still_estimated(self):
        path = self.log("root", [event(counts(100, 10)), event(counts(300, 30), "2026-10-18T18:00:00Z")])
        self.sync()
        rows = self.rows()
        rows[0].update(cost_usd=7.0, estimated=False)
        rows[1].update(cost_usd=None)
        ledger = self.config / usage_stats.CODEX_NAME
        ledger.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        path.unlink()
        result = self.sync()
        self.assertEqual(result["added_tokens"], 0)
        self.assertEqual(self.rows()[0], rows[0])
        self.assertEqual(self.rows()[1]["pricing"]["context"], "standard_assumed")
        self.assertAlmostEqual(result["cost_usd"], 7.003)

    def test_long_context_is_per_request_and_strictly_above_272k(self):
        first, last = counts(272000, 1000, cached=100000), counts(273000, 1000, cached=200000, reasoning=200)
        total = {name: first[name] + last[name] for name in first}
        self.log("root", [event(first, last=first), event(total, "2026-10-18T18:00:00Z", last=last)])
        self.sync()
        a, b = self.rows()
        self.assertEqual(a["pricing"]["context"], "short")
        self.assertAlmostEqual(a["cost_usd"], 1.87)
        self.assertEqual(b["pricing"]["context"], "long")
        self.assertAlmostEqual(b["cost_usd"], 1.935)

    def test_large_cumulative_counter_does_not_turn_small_requests_into_long_context(self):
        one = counts(100000, 1000, cached=90000)
        events = [event({k: v * n for k, v in one.items()}, "2026-10-18T18:%02d:00Z" % n, last=one)
                  for n in range(1, 5)]
        self.log("root", events)
        self.sync()
        self.assertTrue(all(r["pricing"]["context"] == "short" for r in self.rows()))
        self.assertAlmostEqual(sum(r["cost_usd"] for r in self.rows()), .96)

    def test_known_fast_flex_and_batch_tiers_and_missing_tier_assumption(self):
        one = counts(1000, 100, cached=600)
        for tier, multiplier, assumed in [(None, 1, True), ("priority", 2, False),
                                           ("fast", 2, False), ("flex", .5, False), ("batch", .5, False)]:
            with self.subTest(tier=tier):
                switch = {"type": "turn_context", "payload": {"model": "gpt-6-astra", "cwd": str(self.root),
                                                              "service_tier": tier}}
                self.log("thread-" + str(tier), [switch, event(one, last=one)])
                self.sync()
                row = next(r for r in self.rows() if r["thread"] == "thread-" + str(tier))
                self.assertAlmostEqual(row["cost_usd"], .0096 * multiplier)
                self.assertEqual(row["pricing"]["service_tier_assumed"], assumed)

    def test_aggregate_gap_uses_disclosed_standard_estimate(self):
        self.log("root", [event(counts(600000, 300), last=counts(1000, 10))])
        self.sync()
        row = self.rows()[0]
        self.assertEqual(row["pricing"]["context"], "standard_assumed")
        self.assertIsNone(row["pricing"]["request_input_tokens"])
        self.assertAlmostEqual(row["cost_usd"], 6.015)

    def test_repeated_counters_resync_and_new_output_count_once(self):
        first, second = event(counts(100, 10)), event(counts(300, 30, cached=80), "2026-10-18T18:30:00Z")
        path = self.log("root", [first, first, second, second])
        self.assertEqual(self.sync()["added_tokens"], 330)
        self.assertEqual(self.sync()["added_tokens"], 0)
        before = (self.config / usage_stats.CODEX_NAME).read_bytes()
        self.sync()
        self.assertEqual((self.config / usage_stats.CODEX_NAME).read_bytes(), before)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event(counts(400, 40, cached=100), "2026-10-18T19:00:00Z")) + "\n")
        result = self.sync()
        self.assertEqual((result["tokens"], result["added_tokens"]), (440, 110))
        self.assertEqual(len(self.rows()), 3)

    def test_subagents_have_independent_counters_and_first_metadata_wins(self):
        self.log("root", [event(counts(100, 10))])
        self.log("child", [event(counts(100, 10), "2026-10-18T17:24:00Z"),  # inherited older event
                           event(counts(250, 25))], inherited=True)
        self.log("other-session", [event(counts(9999, 99))], session="other")
        self.log("other-project", [event(counts(9999, 99))], root=self.base / "other")
        result = self.sync()
        self.assertEqual((result["sessions"], result["threads"], result["tokens"]), (1, 2, 385))
        self.assertEqual({r["thread"] for r in self.rows()}, {"root", "child"})

    def test_only_registered_sessions_refreshed_and_old_manual_entries_untouched(self):
        self.log("root", [event(counts(100, 10))])
        self.assertEqual(self.sync(None)["tokens"], 0)
        self.assertFalse((self.config / usage_stats.CODEX_NAME).exists())
        self.sync()
        self.log("root", [event(counts(200, 20))])
        result = self.sync(None)
        self.assertEqual((result["tokens"], result["added_tokens"]), (220, 110))
        usage_stats.add_manual(self.config, usage_stats.datetime(2026, 10, 15, tzinfo=usage_stats.timezone.utc),
                               "Earlier ChatGPT work", tokens=900, source="chatgpt")
        old = (self.config / usage_stats.OTHER_NAME).read_bytes()
        self.sync()
        self.assertEqual((self.config / usage_stats.OTHER_NAME).read_bytes(), old)

    def test_cleaned_up_or_truncated_logs_never_erase_or_recount_imported_work(self):
        path = self.log("root", [event(counts(100, 10)), event(counts(200, 20), "2026-10-18T18:00:00Z")])
        self.sync()
        self.log("root", [event(counts(250, 25), "2026-10-18T19:00:00Z")])
        self.assertEqual(self.sync()["added_tokens"], 55)
        path.unlink()
        self.assertEqual(self.sync()["tokens"], 275)

    def test_bad_usage_records_and_partial_last_line_are_skipped(self):
        valid = counts(100, 10)
        path = self.log("root", [event({**valid, "input_tokens": True}),
                                 event({**valid, "cached_input_tokens": 1000}),
                                 event({**valid, "reasoning_output_tokens": 1000}),
                                 event({**valid, "total_tokens": 9999}), event(valid)])
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"type":"event_msg","payload":{"type":"token_count",')
        self.assertEqual(self.sync()["tokens"], 110)

    def test_archived_duplicate_logs_do_not_double_count(self):
        path = self.log("root", [event(counts(100, 10))])
        archived = self.home / "archived_sessions"
        archived.mkdir()
        (archived / path.name).write_bytes(path.read_bytes())
        self.assertEqual(self.sync()["tokens"], 110)
        self.assertEqual(len(self.rows()), 1)

    def test_workspace_change_does_not_get_billed_to_myn(self):
        change = {"type": "turn_context", "payload": {"model": "gpt-test", "cwd": str(self.base / "elsewhere")}}
        back = {"type": "turn_context", "payload": {"model": "gpt-test", "cwd": str(self.root)}}
        self.log("root", [event(counts(100, 10)), change,
                          event(counts(300, 30), "2026-10-18T18:00:00Z"), back,
                          event(counts(400, 40), "2026-10-18T19:00:00Z")])
        self.assertEqual(self.sync()["tokens"], 220)
        self.assertEqual(self.sync()["tokens"], 220)

    def test_daily_update_temporary_config_never_reads_real_codex_logs(self):
        with mock.patch.object(codex_usage, "sync") as sync:
            self.config.mkdir()
            usage_stats.daily_update(self.config, force=True)
        sync.assert_not_called()


if __name__ == "__main__":
    unittest.main()
