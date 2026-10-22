"""Isolated personal expense, exchange-rate and truthful chat receipt checks."""
import io
import json
import queue
import sys
import tempfile
import unittest
import ssl
import subprocess
import urllib.error
from datetime import datetime
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src/core")]
import expenses
import expense_rates
import expense_chat
import chat
import claude_client
import usage_stats

NOW = datetime(2026, 10, 20, 12, tzinfo=expenses.TOKYO)
QUOTE = dict(date="2026-10-19", source=expense_rates.SOURCE,
             rates=dict(USD="1.2", JPY="180", CNY="8.4"))


class Expenses(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="myn-expenses-")
        self.addCleanup(temp.cleanup)
        self.config = Path(temp.name)

    def add(self, amount=1500, currency="JPY", category="eat", key="one"):
        return expenses.add(self.config, amount, currency, category, "lunch",
                            NOW.isoformat(), key, lambda *_: QUOTE)

    def test_conversions_and_retry_keep_original_quote(self):
        item = self.add()
        self.assertEqual((item["usd"], item["jpy"], item["cny"]), (1000, 1500, 7000))
        with mock.patch.object(expense_rates, "quote", side_effect=AssertionError("no new quote")):
            duplicate = expenses.add(self.config, 1500, "JPY", "eat", "lunch", NOW.isoformat(), "one")
        self.assertEqual(item, duplicate)
        self.assertEqual(len(expenses.records(self.config)), 1)
        with self.assertRaises(ValueError):
            self.add(amount=1600)
        self.assertEqual(expenses.records(self.config)[0], item)

    def test_month_year_receipts_merge_names_across_dates_without_repricing(self):
        self.add()  # eat/lunch: 10 USD, Oct 20
        other_quote = dict(QUOTE, rates=dict(USD="1.4", JPY="180", CNY="8.4"))
        expenses.add(self.config, 70, "CNY", "eat", "lunch", NOW.replace(day=19).isoformat(),
                     "second", lambda *_: other_quote)  # saved 11.67 USD
        expenses.add(self.config, 30, "USD", "eat", "dinner", NOW.isoformat(),
                     "dinner", lambda *_: QUOTE)
        expenses.add(self.config, 2, "USD", "study", "lunch", NOW.isoformat(),
                     "other-category", lambda *_: QUOTE)
        expenses.add(self.config, 4, "USD", "eat", "lunch", NOW.replace(month=9).isoformat(),
                     "september", lambda *_: QUOTE)
        before = expenses.records(self.config)
        month = expenses.report(self.config, "month", now=NOW)
        self.assertEqual([(r["name"], r["cost"]) for r in month["rows"]],
                         [("dinner", "$30.00 USD"), ("lunch", "$21.67 USD"), ("lunch", "$2.00 USD")])
        self.assertEqual(month["rows"][1]["detail"], 'eat · 2 entries · ¥70.00 CNY + ¥1,500 JPY')
        self.assertEqual(month["count"], 4)
        year = expenses.report(self.config, "year", category="eat", now=NOW)
        self.assertEqual([(r["name"], r["cost"]) for r in year["rows"]],
                         [("dinner", "$30.00 USD"), ("lunch", "$25.67 USD")])
        self.assertIn("3 entries", year["rows"][1]["detail"])
        self.assertEqual(year["cost"], "$55.67 USD")
        self.assertEqual(len(expenses.report(self.config, "day", now=NOW)["rows"]), 3)
        self.assertEqual(len(expenses.report(self.config, "week", now=NOW)["rows"]), 4)
        self.assertEqual(expenses.records(self.config), before)

    def test_month_token_receipt_merges_source_and_preserves_token_totals(self):
        usage_stats.add_manual(self.config, NOW, "Codex", 100, 2)
        usage_stats.add_manual(self.config, NOW.replace(day=19), "Codex", 200, 3)
        usage_stats.add_manual(self.config, NOW, "Claude Code", 50, 1)
        report = expenses.report(self.config, "month", category="token", now=NOW)
        self.assertEqual([(r["name"], r["cost"]) for r in report["rows"]],
                         [("Codex", "$5.00 USD"), ("Claude Code", "$1.00 USD")])
        self.assertEqual(report["rows"][0]["detail"], 'tokens · 300 tokens · 2 calls')
        self.assertEqual(report["token_total"], 350)
        self.assertEqual(report["cost"], "$6.00 USD")
        self.assertEqual(len(expenses.report(self.config, "week", category="token", now=NOW)["rows"]), 3)

    def test_receipt_rate_uses_latest_cached_day_not_purchase_quotes(self):
        self.add()
        earlier = dict(QUOTE, rates=dict(USD="1", CNY="10", JPY="150"))
        expenses.add(self.config, 4, "USD", "study", "book", NOW.replace(hour=10).isoformat(),
                     "earlier", lambda *_: earlier)
        before = expenses.records(self.config)
        cache = self.config / "expense-rates.json"
        for usd, expected in (("2", "20 USD = 100 CNY = 2000 JPY"),
                              ("3", "30 USD = 100 CNY = 2000 JPY")):
            cache.write_text(json.dumps(dict(source=expense_rates.SOURCE, rows={
                "2026-10-20": dict(USD=usd, CNY="10", JPY="200")})), encoding="utf-8")
            for category in (None, "eat", "study", "health", "token"):
                report = expenses.report(self.config, category=category, now=NOW)
                self.assertEqual(report["note"], expected)
        self.assertEqual(expenses.records(self.config), before)

    def test_period_rate_is_daily_mean_including_empty_days_not_transactions(self):
        self.add()
        for i in range(4):
            self.add(key="repeat-%s" % i)
        (self.config / "expense-rates.json").write_text(json.dumps(dict(source=expense_rates.SOURCE, rows={
            "2026-10-18": dict(USD="1", CNY="10", JPY="100"),
            "2026-10-19": dict(USD="2", CNY="10", JPY="200"),
            "2026-10-20": dict(USD="6", CNY="20", JPY="600"),
            "2026-10-21": dict(USD="99", CNY="10", JPY="999")})), encoding="utf-8")
        for period in ("week", "month", "year"):
            self.assertEqual(expenses.report(self.config, period, now=NOW)["note"],
                             "20 USD = 100 CNY = 2000 JPY")
        self.assertEqual(expenses.report(self.config, now=NOW)["note"],
                         "30 USD = 100 CNY = 3000 JPY")

    def test_rate_fallback_uses_last_snapshot_per_tokyo_day_and_skips_missing(self):
        self.add()
        late = dict(QUOTE, rates=dict(USD="2", CNY="10", JPY="200"))
        expenses.add(self.config, 4, "USD", "study", "book", "2026-10-20T05:00:00+00:00",
                     "late", lambda *_: late)
        for period in ("day", "week", "month", "year"):
            self.assertEqual(expenses.report(self.config, period, now=NOW.replace(hour=15))["note"],
                             "20 USD = 100 CNY = 2000 JPY")
        self.assertEqual(expenses.report(self.config, offset=-1, now=NOW)["note"], "")

    def test_precision_and_missing_rate_never_create_false_record(self):
        for value in (0, -1, "nan", "Infinity", ".001"):
            with self.assertRaises(ValueError):
                self.add(value, "USD")
        with mock.patch.object(expense_rates, "quote", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                expenses.add(self.config, 3, "USD", "shop", "book", NOW.isoformat(), "x")
        self.assertFalse((self.config / expenses.FILE).exists())
        item = self.add("1.01", "USD")
        self.assertEqual(item["jpy"], 152)  # 151.5 rounds half up

    def test_mixed_currency_shares_filters_totals_and_numeric_rates(self):
        self.add()
        self.add(70, "CNY", "study", "two")
        report = expenses.report(self.config, now=NOW)
        self.assertEqual(report["cost"], "$20.00 USD")
        self.assertEqual(report["totals"], [["USD", "$20.00 USD"], ["CNY", "¥140.00 CNY"], ["JPY", "¥3,000 JPY"]])
        shares = {x["name"]: x["share"] for x in report["categories"]}
        self.assertEqual(shares["eat"], "50.0%")
        self.assertEqual(shares["study"], "50.0%")
        self.assertEqual(len(shares), 9)
        self.assertEqual([c["name"] for c in report["categories"][:2]], ["eat", "study"])
        self.assertEqual(report["categories"][0]["percent"], 50)
        self.assertEqual(report["note"], "14.2857 USD = 100 CNY = 2142.8571 JPY")
        self.assertNotIn(QUOTE["date"], report["note"])
        filtered = expenses.report(self.config, category="eat", now=NOW)
        self.assertEqual(len(filtered["rows"]), 1)
        self.assertEqual(filtered["cost"], "$10.00 USD")
        self.assertIn("¥1,500 JPY", filtered["rows"][0]["detail"])

    def test_trend_fixed_axes_and_recorded_years_use_usd(self):
        self.add()  # 10 USD at noon
        expenses.add(self.config, 3, "USD", "study", "book", NOW.replace(hour=0).isoformat(),
                     "midnight", lambda *_: QUOTE)
        day = expenses.report(self.config, now=NOW)
        self.assertEqual(day["series_labels"], ["%02d:00" % hour for hour in range(24)])
        self.assertEqual(day["trend_ticks"][0], [0, '00:00'])
        self.assertEqual(day["trend_ticks"][-1], [1, '24:00'])
        self.assertEqual(day["series"]["cost_usd"], [3.0] + [0.0]*11 + [10.0] + [0.0]*11)
        for period, count in (("week", 7), ("month", 12), ("year", 1)):
            report = expenses.report(self.config, period, now=NOW)
            self.assertEqual(len(report["series_labels"]), count)
            self.assertEqual(sum(report["series"]["cost_usd"]), 13)
        filtered = expenses.report(self.config, category="eat", now=NOW)
        self.assertEqual(sum(filtered["series"]["cost_usd"]), 10)
        previous = expenses.report(self.config, offset=-1, now=NOW)
        self.assertEqual(len(previous["series_labels"]), 24)
        self.assertFalse(previous["at_present"])
        self.assertTrue(day["at_present"])
        self.assertEqual(day["chart_kind"], "pie")
        self.assertEqual([(s["name"], s["value"]) for s in day["chart_slices"]], [("eat", 10), ("study", 3)])
        self.assertEqual(filtered["chart_slices"][0]["value"], 10)
        self.assertEqual(len(filtered["chart_slices"]), 1)
        self.assertEqual(filtered["chart_slices"][0]["name"], "lunch")
        for period, count in (("day", 24), ("week", 7), ("month", 12)):
            empty = expenses.report(self.config, period, category="health", now=NOW)
            self.assertEqual(len(empty["series_labels"]), count)
            self.assertEqual(sum(empty["series"]["cost_usd"]), 0)
            self.assertEqual(empty["chart_slices"], [])
        # Other months and token-only years must participate in the wider trend.
        usage_stats.add_manual(self.config, NOW.replace(year=2024), "Codex", 100, 5)
        usage_stats.add_manual(self.config, NOW.replace(month=1), "Codex", 100, 7)
        monthly = expenses.report(self.config, "month", now=NOW)
        self.assertEqual(monthly["series_labels"], ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                                                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])
        self.assertEqual([label for _, label in monthly["trend_ticks"]], list("JFMAMJJASOND"))
        self.assertEqual(monthly["series"]["cost_usd"][0], 7)
        self.assertEqual(monthly["cost"], "$13.00 USD")  # receipt remains selected month
        yearly = expenses.report(self.config, "year", now=NOW)
        self.assertEqual(yearly["series_labels"], ["2024", "2026"])
        self.assertEqual(yearly["series"]["cost_usd"], [5, 20])
        self.assertEqual(yearly["chart_kind"], "bar")
        self.assertEqual(yearly["series_positions"], [.25, .75])

    def test_week_starts_sunday_and_empty_calendar_axes_exist(self):
        for period, count in (("day", 24), ("week", 7), ("month", 12), ("year", 0)):
            self.assertEqual(len(expenses.report(self.config, period, now=NOW)["series_labels"]), count)
        sunday = NOW.replace(day=18)
        expenses.add(self.config, 4, "USD", "eat", "Sunday", sunday.isoformat(), "sun", lambda *_: QUOTE)
        expenses.add(self.config, 8, "USD", "eat", "Saturday", sunday.replace(day=17).isoformat(), "sat", lambda *_: QUOTE)
        for today in (sunday, NOW):
            week = expenses.report(self.config, "week", now=today)
            self.assertEqual(week["series_labels"], ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"])
            self.assertEqual(week["series"]["cost_usd"], [4, 0, 0, 0, 0, 0, 0])
            self.assertEqual(week["cost"], "$4.00 USD")
            self.assertEqual(week["date"], "This Week  2026/10/18 Sun – 2026/10/24 Sat")

    def test_receipt_dates_use_english_weekdays_and_slashes(self):
        now = NOW.replace(day=21)
        self.assertEqual(expenses.report(self.config, now=now)["date"], "Today  2026/10/21 Wed")
        self.assertEqual(expenses.report(self.config, offset=-1, now=now)["date"], "Yesterday  2026/10/20 Tue")
        self.assertEqual(expenses.report(self.config, offset=-2, now=now)["date"], "2026/10/19 Mon")
        self.assertEqual(expenses.report(self.config, "month", now=now)["date"], "This Month  2026/10")
        self.assertEqual(expenses.report(self.config, "year", now=now)["date"], "This Year  2026")
        self.assertEqual(expenses.report(self.config, "month", -1, now=now.replace(month=1))["date"], "Last Month  2025/12")

    def test_empty_previous_period_and_ai_ledger_separate(self):
        (self.config / "manual-usage.jsonl").write_text('{"cost_usd":900}', encoding="utf-8")
        self.assertTrue(expenses.report(self.config, now=NOW)["zero"])
        self.add()
        yesterday = expenses.report(self.config, offset=-1, now=NOW)
        self.assertTrue(yesterday["zero"])
        self.assertEqual(yesterday["cost"], "$0.00 USD")
        self.assertEqual(len(expenses.report(self.config, "week", now=NOW)["rows"]), 1)

    def test_rate_feed_weekend_cache_and_stale_rejection(self):
        xml = b'<Envelope><Cube><Cube time="2026-10-16"><Cube currency="USD" rate="1.2"/><Cube currency="JPY" rate="180"/><Cube currency="CNY" rate="8.4"/></Cube><Cube time="2026-10-19"><Cube currency="USD" rate="1.3"/><Cube currency="JPY" rate="181"/><Cube currency="CNY" rate="8.5"/></Cube></Cube></Envelope>'
        with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(xml)):
            quote = expense_rates.quote(self.config, "2026-10-18")
        self.assertEqual(quote["date"], "2026-10-16")
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("cached")):
            self.assertEqual(expense_rates.quote(self.config, "2026-10-19")["rates"]["USD"], "1.3")
            with self.assertRaises(ValueError):
                expense_rates.quote(self.config, "2026-10-01")
            with self.assertRaises(ValueError):
                expense_rates.quote(self.config, "2026-11-01")

    def args(self):
        return dict(amount=1500, currency="JPY", category="eat", description="午饭",
                    evidence="午饭花了1500日元", item_index=0)

    def test_chat_write_verified_then_api_failure_preserves_receipt(self):
        for failure in (False, True):
            session = chat.ChatSession(self.config / ("chat%s.db" % failure))
            session.expense_notify = mock.Mock()
            def brain(*args, **kw):
                self.assertIsNone(kw["on_text"])
                self.assertIn("record_expense", [t["name"] for t in kw["read_tools"]])
                content, failed = kw["tool_executor"]("record_expense", self.args())
                self.assertFalse(failed, content)
                self.assertTrue(json.loads(content)["verified"])
                session.expense_notify.assert_called_once_with()  # before the final model reply
                if failure:
                    raise claude_client.ClaudeUnavailable("offline")
                return "已记账999美元"
            with mock.patch.object(session, "_key", return_value="synthetic"), \
                    mock.patch.object(expense_rates, "quote", return_value=QUOTE), \
                    mock.patch.object(claude_client, "coordinated_reply", side_effect=brain):
                result = session.ask("午饭花了1500日元", heard=failure)
            self.assertIn("$10.00 USD", result["answer"])
            self.assertNotIn("999", result["answer"])

    def test_expense_refresh_is_queued_and_preserves_panel_selection(self):
        from gui_settings import SettingsMixin
        from sheets import SheetHost
        from web_panels3 import ExpensePanel

        class Window(SettingsMixin):
            def _usage_config(win):
                return self.config

        win = Window()
        win.events = queue.Queue()
        win.usage_window = mock.Mock()
        win.usage_window.window.winfo_exists.return_value = True
        sent = []
        win._sheets = SheetHost(sent.append)
        panel = ExpensePanel(win)
        panel.period, panel.offset, panel.category = "week", -1, "eat"
        win.sheets.open(panel)
        before = sent[-1]["spec"]["body"][-1]
        # Write into the selected historical period, then follow the exact GUI callback.
        when = datetime.now(expenses.TOKYO)
        start, _ = usage_stats.bounds("week", -1, when)
        expenses.add(self.config, 1500, "JPY", "eat", "午饭", start.isoformat(),
                     "refresh", lambda *_: QUOTE)
        win._expense_saved()
        self.assertEqual(len(sent), 1)
        win.usage_window.refresh.assert_not_called()
        event, refresh = win.events.get_nowait()
        self.assertEqual(event, "call")
        refresh()
        after = sent[-1]["spec"]["body"][-1]
        self.assertEqual(after["cost"], "$10.00 USD")
        self.assertEqual(after["print_key"], before["print_key"])
        self.assertEqual((panel.period, panel.offset, panel.category), ("week", -1, "eat"))
        win.usage_window.refresh.assert_called_once_with()
        win.sheets.close()
        count = len(sent)
        win.usage_window.window.winfo_exists.return_value = False
        win._usage_updated()
        self.assertEqual(len(sent), count)  # never opens a closed panel

    def test_category_pie_groups_items_and_category_click_toggles(self):
        from sheets import SheetHost
        from web_panels3 import ExpensePanel
        from types import SimpleNamespace
        self.add()  # lunch, 10 USD
        self.add(70, "CNY", "eat", "second-lunch")  # same item, another currency
        expenses.add(self.config, 5, "USD", "eat", "coffee", NOW.isoformat(),
                     "coffee", lambda *_: QUOTE)
        self.add(70, "CNY", "study", "study")
        report = expenses.report(self.config, category="eat", now=NOW)
        self.assertEqual([(s["name"], s["value"]) for s in report["chart_slices"]],
                         [("lunch", 20), ("coffee", 5)])
        self.assertEqual(report["cost"], "$25.00 USD")
        self.assertEqual(report["count"], 3)
        self.assertEqual(sum(s["value"] for s in report["chart_slices"]), 25)
        usage_stats.add_manual(self.config, NOW, "Codex", 100, 2)
        usage_stats.add_manual(self.config, NOW, "Claude Code", 100, 3)
        token = expenses.report(self.config, category="token", now=NOW)
        self.assertEqual({s["name"]: s["value"] for s in token["chart_slices"]},
                         {"Other usage": 5})  # manual entries have no declared task category
        panel = ExpensePanel(SimpleNamespace(_usage_config=lambda: self.config))
        host = SheetHost(lambda _: None)
        host.open(panel)
        panel.period, panel.offset = "week", -1
        for chosen, expected in [("eat", "eat"), ("eat", None), ("eat", "eat"),
                                 ("study", "study"), ("study", None)]:
            panel.act("category:"+chosen, {})
            self.assertEqual(panel.category, expected)
            self.assertEqual((panel.period, panel.offset), ("week", -1))

    def test_failed_save_does_not_notify_and_notify_error_keeps_saved_receipt(self):
        for saved in (False, True):
            session = chat.ChatSession(self.config / ("notify%s.db" % saved))
            session.expense_notify = mock.Mock(side_effect=RuntimeError("UI unavailable"))
            def brain(*args, **kw):
                content, failed = kw["tool_executor"]("record_expense", self.args())
                self.assertEqual(failed, not saved)
                return "已记账"
            with mock.patch.object(session, "_key", return_value="synthetic"), \
                    mock.patch.object(expense_rates, "quote", side_effect=None if saved else OSError("offline"),
                                      return_value=QUOTE), \
                    mock.patch.object(claude_client, "coordinated_reply", side_effect=brain):
                result = session.ask("午饭花了1500日元", heard=True)
            self.assertEqual(session.expense_notify.call_count, int(saved))
            self.assertIn("$10.00 USD" if saved else 'not confirmed as saved', result["answer"])

    def test_chat_failure_and_unverified_promise_cannot_claim_success(self):
        with mock.patch.object(expense_rates, "quote", side_effect=OSError("offline")):
            result, failed = expense_chat.execute("record_expense", self.args(), self.config,
                                                  "午饭花了1500日元", 1)
        self.assertTrue(failed)
        self.assertNotIn("已记账", expense_chat.finish("已记账", [result]))
        self.assertEqual(expenses.records(self.config), [])
        self.assertIn('has not been saved', expense_chat.finish("好的，已经记账了", [], True))
        for question, unattended in [("我没有消费", False), ("午饭花了1500日元", True)]:
            result, failed = expense_chat.execute("record_expense", self.args(), self.config,
                                                  question, 1, unattended)
            self.assertTrue(failed)

    def test_native_tls_fallback_keeps_validation_and_fixed_public_url(self):
        error = urllib.error.URLError(ssl.SSLCertVerificationError("chain"))
        with mock.patch("urllib.request.urlopen", side_effect=error), \
                mock.patch.object(expense_rates.os, "name", "nt"), \
                mock.patch.object(Path, "is_file", return_value=True), \
                mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"<xml/>")) as run:
            self.assertEqual(expense_rates._download(), b"<xml/>")
        args = run.call_args[0][0]
        self.assertEqual(args[-1], expense_rates.SOURCE)
        self.assertNotIn("-k", args)
        self.assertNotIn("--insecure", args)
        self.assertIn("=https", args)

    def test_tool_missing_currency_future_date_and_empty_evidence_fail(self):
        for changes in ({"currency": ""}, {"date": "2099-01-01"}, {"evidence": ""}):
            args = dict(self.args(), **changes)
            result, failed = expense_chat.execute("record_expense", args, self.config,
                                                  "午饭花了1500日元", 1)
            self.assertTrue(failed, result)
        self.assertEqual(expenses.records(self.config), [])

    def test_token_category_uses_existing_ledger_without_duplicate_or_per_call_rounding(self):
        self.add()  # 10 USD
        for i in range(10):
            usage_stats.add_manual(self.config, NOW, "Codex", 100, .001)
        usage_stats.add_manual(self.config, NOW, "Claude Code", 9000, 9.99, estimated=True)
        (self.config / "expense-rates.json").write_text(json.dumps({
            "source": expense_rates.SOURCE, "rows": {"2026-10-19": QUOTE["rates"]}}), encoding="utf-8")
        before = {p.name: p.read_bytes() for p in self.config.iterdir() if p.is_file()}
        for _ in range(2):
            report = expenses.report(self.config, now=NOW)
            self.assertEqual(report["cost"], "$20.00 USD")
            self.assertEqual(report["token_total"], 10000)
            self.assertEqual(next(c["share"] for c in report["categories"] if c["name"] == "token"), "50.0%")
            token = expenses.report(self.config, category="token", now=NOW)
            self.assertEqual(token["cost"], "$10.00 USD")
            self.assertEqual(token["totals"][1][1], "¥70.00 CNY")
            self.assertEqual(token["totals"][2][1], "¥1,500 JPY")
            self.assertNotIn("估算", json.dumps(token["rows"], ensure_ascii=False))
            self.assertEqual(len(token["rows"]), 11)
            self.assertEqual(sum(r["name"] == "Codex" for r in token["rows"]), 10)
            self.assertEqual(len(expenses.report(self.config, "week", category="token", now=NOW)["rows"]), 2)
            self.assertIn("tokens", token["rows"][0]["detail"])
            json.dumps(token)  # page protocol must remain serializable
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.config.iterdir() if p.is_file()})
        with self.assertRaises(ValueError):
            self.add(category="token")

    def test_token_missing_fx_keeps_usd_and_historical_periods(self):
        usage_stats.add_manual(self.config, NOW, "Codex", 700, 3)
        token = expenses.report(self.config, category="token", now=NOW)
        self.assertEqual(token["cost"], "$3.00 USD")
        self.assertEqual(token["categories"][0]["name"], "token")
        self.assertEqual(token["categories"][0]["percent"], 100)
        self.assertEqual(token["totals"][1][1], 'Awaiting conversion')
        self.assertEqual(expenses.report(self.config, offset=-1, category="token", now=NOW)["cost"], "$0.00 USD")
        self.assertEqual(expenses.report(self.config, "month", category="token", now=NOW)["cost"], "$3.00 USD")
        tomorrow = NOW.replace(day=21)
        self.assertTrue(expenses.report(self.config, now=tomorrow)["has_earlier"])


if __name__ == "__main__":
    unittest.main()
