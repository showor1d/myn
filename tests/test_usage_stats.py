"""Token and cost statistics: pricing, periods, the spoken answer and spoken questions."""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src" / "core")]

import usage_stats  # noqa: E402

TOKYO = timezone(timedelta(hours=9))
NOW = datetime(2026, 10, 17, 10, 30, tzinfo=TOKYO)  # a Saturday


def line(when, model="claude-sonnet-5-5", purpose="chat_sonnet", i=0, o=0, cw=0, cr=0, **extra):
    record = {"at_utc": when.astimezone(timezone.utc).isoformat(), "purpose": purpose, "model": model,
              "status": "answered", "input_tokens": i, "output_tokens": o,
              "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr}
    record.update(extra)
    return json.dumps(record)


class Stats(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="myn-usage-")
        self.addCleanup(folder.cleanup)
        self.config = Path(folder.name)
        self.log = self.config / usage_stats.USAGE_NAME

    def write(self, *lines):
        with self.log.open("a", encoding="utf-8") as log:
            for text in lines:
                log.write(text + "\n")

    def test_prices(self):
        # Sonnet 5.5: $2 in, $10 out, $2.50 cache write, $0.10 cache read per million
        cost, guessed = usage_stats.cost_of(json.loads(line(NOW, i=1000000, o=100000, cw=200000, cr=1000000)))
        self.assertAlmostEqual(cost, 2 + 1 + 0.5 + 0.1)
        self.assertFalse(guessed)
        cost, _ = usage_stats.cost_of(json.loads(line(NOW, model="claude-opus-5-5", i=1000000, o=1000000)))
        self.assertAlmostEqual(cost, 24)
        # Haiku 5.5 costs five times more when the prompt is over 100K tokens
        small, _ = usage_stats.cost_of(json.loads(line(NOW, model="claude-haiku-5-5", i=100000)))
        large, _ = usage_stats.cost_of(json.loads(line(NOW, model="claude-haiku-5-5", i=90000, cr=20000)))
        self.assertAlmostEqual(small, 0.01)
        self.assertAlmostEqual(large, 90000 * 0.5 / 1e6 + 20000 * 0.05 / 1e6)
        cost, _ = usage_stats.cost_of(json.loads(line(NOW, web_search_requests=3)))
        self.assertAlmostEqual(cost, 0.03)
        cost, guessed = usage_stats.cost_of(json.loads(line(NOW, model="claude-sonnet-9", i=1000000)))
        self.assertTrue(guessed)
        self.assertAlmostEqual(cost, 2)
        self.assertEqual(usage_stats.cost_of({"cost_usd": 0.42, "model": "x"}), (0.42, False))

    def test_periods_and_series(self):
        self.write(line(NOW - timedelta(hours=1), i=1000, o=500),                  # today 9:xx
                   line(NOW - timedelta(days=1), model="claude-opus-5-5", i=2000),  # yesterday (Friday)
                   line(NOW - timedelta(days=6), i=10),                             # last Sunday: last week
                   line(NOW - timedelta(days=40), i=10),                            # September
                   line(NOW - timedelta(days=400), i=10),                           # last year
                   json.dumps({"at_utc": NOW.isoformat(), "model": "claude-sonnet-5-5", "status": "http_400",
                               "input_tokens": None}),                              # not billed
                   "not json")
        day = usage_stats.report(self.config, "day", 0, NOW)
        self.assertEqual(day["totals"]["calls"], 1)
        self.assertEqual(day["totals"]["tokens"], 1500)
        self.assertEqual(len(day["series"]), 24)
        self.assertEqual(day["series"][9]["tokens"], 1500)
        self.assertTrue(day["series"][11]["future"])
        self.assertEqual(day["previous"]["calls"], 1)
        self.assertTrue(day["label"].startswith('Today（2026-10-17 Sat'))
        self.assertEqual(usage_stats.report(self.config, "day", -1, NOW)["by_model"][0][0], "Opus 5.5")
        week = usage_stats.report(self.config, "week", 0, NOW)
        self.assertEqual(week["totals"]["calls"], 2)
        self.assertEqual(len(week["series"]), 7)
        self.assertTrue(week["series"][0]["label"].startswith('Mon'))
        self.assertEqual(usage_stats.report(self.config, "week", -1, NOW)["totals"]["calls"], 1)
        month = usage_stats.report(self.config, "month", 0, NOW)
        self.assertEqual(month["totals"]["calls"], 3)
        # The monthly view charts the months of the year and marks the current month.
        self.assertEqual([bucket["label"] for bucket in month["series"]], ['%02d' % m for m in range(1, 13)])
        self.assertEqual([bucket["label"] for bucket in month["series"] if bucket["selected"]], ['10'])
        self.assertEqual(month["series"][8]["calls"], 1)  # September
        self.assertEqual(usage_stats.report(self.config, "month", -1, NOW)["totals"]["calls"], 1)
        year = usage_stats.report(self.config, "year", 0, NOW)
        self.assertEqual(year["totals"]["calls"], 4)
        # the year view charts years (2025, 2026, …) from the first record on
        self.assertEqual([bucket["label"] for bucket in year["series"]], ["2025", "2026"])
        self.assertEqual([bucket["calls"] for bucket in year["series"]], [1, 4])
        self.assertEqual(usage_stats.report(self.config, "year", -1, NOW)["totals"]["calls"], 1)
        self.assertEqual(usage_stats.bounds("month", -10, NOW)[0].strftime("%Y-%m"), "2025-12")
        with self.assertRaises(ValueError):
            usage_stats.report(self.config, "decade")

    def test_spoken_answer_and_tool_summary(self):
        self.assertEqual(usage_stats.spoken(usage_stats.report(self.config, "day", 0, NOW)),
                         'Today has no usage records or spending.')
        self.write(line(NOW, i=120000, o=30000, purpose="scene_analysis"), line(NOW, i=5000, web_search_requests=2))
        result = usage_stats.report(self.config, "day", 0, NOW)
        text = usage_stats.spoken(result)
        self.assertEqual(text, 'Today used 155.0K tokens and cost approximately USD 0.57. ')
        self.assertNotIn("日元", text)
        summary = usage_stats.for_model(result)
        self.assertEqual(summary["tokens"], 155000)
        self.assertEqual(summary["by_use"][0]["use"], 'Scene understanding')
        self.assertEqual(summary["say_this"], text)
        self.assertNotIn("cost_jpy", summary)

    def test_haiku_is_left_out(self):
        self.write(line(NOW, model="claude-haiku-5-5", purpose="conversation_judge", i=1000, o=10),
                   line(NOW, model="claude-opus-5-5", i=1000))
        result = usage_stats.report(self.config, "day", 0, NOW)
        self.assertEqual(result["totals"]["calls"], 1)
        self.assertEqual([name for name, _ in result["by_model"]], ["Opus 5.5"])

    def test_other_sources(self):
        # ElevenLabs: money by characters (Flash $0.05, Multilingual $0.10 per 1,000)
        usage_stats.record_voice(self.config, "eleven_flash_v2_5", 2000, when=NOW)
        usage_stats.record_voice(self.config, "eleven_multilingual_v2", 1000, when=NOW)
        # Claude Code working on MYN (written as a whole file elsewhere)
        (self.config / usage_stats.CLAUDE_CODE_NAME).write_text(json.dumps({
            "at_utc": NOW.isoformat(), "source": "claude_code", "label": "Claude Code（开发 MYN）",
            "purpose": "claude_code", "model": "claude-opus-5-5", "input_tokens": 100, "output_tokens": 1000,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 1000000, "cost_usd": 0.3}) + "\n",
            encoding="utf-8")
        # ChatGPT by hand: tokens only
        chatgpt = usage_stats.add_manual(self.config, NOW - timedelta(days=2), "ChatGPT（GPT-6.1 Sol）", 694000,
                                         source="chatgpt")
        today = usage_stats.report(self.config, "day", 0, NOW)
        groups = dict(today["by_model"])
        self.assertAlmostEqual(groups["ElevenLabs speech"]["cost_usd"], 0.2)
        self.assertEqual(groups['ElevenLabs speech']["tokens"], 0)
        self.assertAlmostEqual(groups["Claude Code"]["cost_usd"], 0.3)
        self.assertEqual(groups["Claude Code"]["tokens"], 1001100)
        self.assertEqual(dict(today["by_purpose"])['Speech synthesis']["calls"], 2)
        self.assertIn("The largest share was Claude Code at 60.0%", usage_stats.spoken(today))
        before = usage_stats.report(self.config, "day", -2, NOW)
        self.assertEqual(before["totals"]["tokens"], 694000)
        self.assertEqual(before["totals"]["other"], 694000)
        self.assertEqual(before["totals"]["cost_usd"], 0)
        self.assertEqual(before["totals"]["unpriced"], ["ChatGPT（GPT-6.1 Sol）"])
        self.assertTrue(usage_stats.spoken(before).startswith("Two days ago used 694.0K tokens"), usage_stats.spoken(before))
        self.assertEqual(usage_stats.spoken(before), "Two days ago used 694.0K tokens, all from ChatGPT（GPT-6.1 Sol）, with no cost records.")
        self.assertIn("have no cost records and are excluded", usage_stats.spoken(usage_stats.report(self.config, "week", 0, NOW)))
        week = usage_stats.report(self.config, "week", 0, NOW)
        self.assertEqual(week["totals"]["tokens"], 694000 + 1001100)
        # hand entries can be listed and removed; other lines stay
        self.assertEqual([entry["id"] for entry in usage_stats.manual_entries(self.config)], [chatgpt])
        self.assertTrue(usage_stats.remove_manual(self.config, chatgpt))
        self.assertEqual(usage_stats.manual_entries(self.config), [])
        self.assertEqual(usage_stats.report(self.config, "day", 0, NOW)["totals"]["calls"], 3)
        with self.assertRaises(ValueError):
            usage_stats.add_manual(self.config, NOW, "x")  # neither tokens nor cost
        with self.assertRaises(ValueError):
            usage_stats.remove_manual(self.config, "elevenlabs:1")

    def test_no_empty_periods_before_the_first_record(self):
        self.write(line(NOW - timedelta(days=2), i=1000))  # first record: Thursday 15th
        month = usage_stats.report(self.config, "month", 0, NOW)
        self.assertEqual([b["label"] for b in month["series"]], ['10', '11', '12'])  # Omit empty months before the first record.
        self.assertEqual([b["label"] for b in usage_stats.report(self.config, "year", 0, NOW)["series"]], ["2026"])
        week = usage_stats.report(self.config, "week", 0, NOW)
        self.assertTrue(week["series"][0]["label"].startswith('Thu'))  # Mon–Wed left out
        self.assertTrue(usage_stats.report(self.config, "day", 0, NOW)["has_earlier"])
        self.assertFalse(usage_stats.report(self.config, "day", -2, NOW)["has_earlier"])
        self.assertFalse(month["has_earlier"])
        self.assertEqual(len(usage_stats.report(self.config, "day", -2, NOW)["series"]), 24 - 10)

    def test_named_uses_and_two_decimal_dollars(self):
        shares = (("Brain 系统与架构设计", 19.7), ("功能规划与需求分析", 29.4), ("Bug 排查与调试", 20.9))
        for use, percent in shares:
            usage_stats.add_manual(self.config, NOW, "ChatGPT（开发 MYN）", round(1000000 * percent / 100),
                                   source="chatgpt", use=use)
        uses = dict(usage_stats.report(self.config, "day", 0, NOW)["by_purpose"])
        self.assertEqual(uses["功能规划与需求分析"]["tokens"], 294000)
        self.assertEqual(usage_stats.manual_entries(self.config)[0]["use"] in dict(shares), True)
        self.assertEqual(usage_stats.money_text(0.001), 'USD 0.00')
        self.assertEqual(usage_stats.money_text(12.345), 'USD 12.35')
        self.write(line(NOW, i=1000))
        self.assertIn("cost approximately USD 0.00", usage_stats.spoken(usage_stats.report(self.config, "day", 0, NOW)))
    def test_hand_entered_cost_can_be_filled_in_later(self):
        # an existing token-only ChatGPT entry gets a (guessed) dollar amount afterwards
        self.write(line(NOW, i=1000000))  # $2 of Sonnet today
        chatgpt = usage_stats.add_manual(self.config, NOW - timedelta(days=2), "ChatGPT（GPT-6.1 Sol）", 694000,
                                         source="chatgpt")
        other = usage_stats.add_manual(self.config, NOW - timedelta(days=2), "Gemini", cost_usd=0.5)
        before_text = (self.config / usage_stats.OTHER_NAME).read_text(encoding="utf-8").splitlines()
        self.assertTrue(usage_stats.set_manual_cost(self.config, chatgpt, 3.25, estimated=True))
        lines = (self.config / usage_stats.OTHER_NAME).read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[1], before_text[1])  # the other line stays exactly as it was
        entry = usage_stats.manual_entries(self.config)
        self.assertEqual({item["id"]: (item["cost_usd"], item["estimated"]) for item in entry},
                         {chatgpt: (3.25, True), other: (0.5, False)})
        for period in ("day", "week", "month", "year"):
            with self.subTest(period=period):
                result = usage_stats.report(self.config, period, -2 if period == "day" else 0, NOW)
                expected = 3.75 if period == "day" else 5.75
                self.assertAlmostEqual(result["totals"]["cost_usd"], expected)
                self.assertEqual(result["totals"]["unpriced"], [])
                self.assertTrue(result["totals"]["estimated"])
                groups = dict(result["by_model"])
                self.assertAlmostEqual(groups["ChatGPT（GPT-6.1 Sol）"]["cost_usd"], 3.25)
                self.assertTrue(groups["ChatGPT（GPT-6.1 Sol）"]["estimated"])
                self.assertAlmostEqual(groups["Gemini"]["cost_usd"], 0.5)
        summary = usage_stats.for_model(usage_stats.report(self.config, "day", -2, NOW))
        source = {item["source"]: item for item in summary["by_source"]}["ChatGPT（GPT-6.1 Sol）"]
        self.assertEqual((source["cost_usd"], source["cost_known"], source["cost_is_estimate"]), (3.25, True, True))
        self.assertEqual(summary["sources_without_cost"], [])
        self.assertEqual(usage_stats.spoken(usage_stats.report(self.config, "day", -2, NOW)),
                         'Two days ago used 694.0K tokens and cost approximately USD 3.75. The largest share was ChatGPT（GPT-6.1 Sol） at 86.7%.')
        # back to unknown: not counted again; a cost-only entry cannot lose its cost
        self.assertTrue(usage_stats.set_manual_cost(self.config, chatgpt, None))
        self.assertAlmostEqual(usage_stats.report(self.config, "day", -2, NOW)["totals"]["cost_usd"], 0.5)
        self.assertFalse(usage_stats.manual_entries(self.config)[0]["estimated"])
        with self.assertRaises(ValueError):
            usage_stats.set_manual_cost(self.config, other, None)
        with self.assertRaises(ValueError):
            usage_stats.set_manual_cost(self.config, chatgpt, -1)
        with self.assertRaises(ValueError):
            usage_stats.set_manual_cost(self.config, "elevenlabs:1", 1)
        self.assertFalse(usage_stats.set_manual_cost(self.config, "manual:missing", 1))
        # a new entry can be marked as an estimate straight away
        guessed = usage_stats.add_manual(self.config, NOW, "Claude.ai", cost_usd=2, estimated=True)
        self.assertTrue({item["id"]: item for item in usage_stats.manual_entries(self.config)}[guessed]["estimated"])

    def test_daily_update_syncs_elevenlabs_and_writes_daily_summaries(self):
        tokyo_now = NOW
        # MYN's own request today already carries ElevenLabs' history id
        usage_stats.record_voice(self.config, "eleven_flash_v2_5", 40, when=NOW - timedelta(hours=1), history_id="own")
        self.write(line(NOW - timedelta(days=1), i=1000000))  # yesterday: $2 of Sonnet
        history = [
            {"history_item_id": "own", "date_unix": (NOW - timedelta(hours=1)).timestamp(), "text": "x" * 40,
             "model_id": "eleven_flash_v2_5"},                                      # counted already
            {"history_item_id": "web", "date_unix": (NOW - timedelta(days=1)).timestamp(), "text": "你" * 2000,
             "model_id": "eleven_multilingual_v2"},                                 # used outside MYN
            {"history_item_id": "later", "date_unix": (NOW + timedelta(minutes=5)).timestamp(), "text": "y"}]
        asked = []

        class Response:
            def __init__(self, body):
                self.body = body
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
            def read(self, _limit):
                return json.dumps(self.body).encode("utf-8")

        def opener(request, timeout):
            asked.append(request.full_url)
            self.assertEqual(request.get_header("Xi-api-key"), "k")
            return Response({"history": history, "has_more": False})

        state = usage_stats.daily_update(self.config, "k", tokyo_now, opener)
        self.assertEqual(state["elevenlabs"], 'Synced; 1 new requests')
        self.assertEqual(state["last_run"], "2026-10-17")
        self.assertIsNone(usage_stats.daily_update(self.config, "k", tokyo_now, opener))  # once a day
        self.assertEqual(len(asked), 1)
        yesterday = usage_stats.report(self.config, "day", -1, NOW)
        self.assertAlmostEqual(yesterday["totals"]["cost_usd"], 2 + 0.2)
        self.assertNotIn("你你", (self.config / usage_stats.OTHER_NAME).read_text(encoding="utf-8"))
        days = [json.loads(text) for text in
                (self.config / usage_stats.DAILY_SUMMARY).read_text(encoding="utf-8").splitlines()]
        self.assertEqual([day["date"] for day in days], ["2026-10-16"])  # finished days only
        self.assertEqual(days[0]["cost_usd"], 2.2)
        self.assertEqual(days[0]["by_source"]['ElevenLabs speech']["cost_usd"], 0.2)
        # the next day: a second sync adds nothing twice; the summary gains the 17th
        state = usage_stats.daily_update(self.config, "k", tokyo_now + timedelta(days=1), opener)
        self.assertEqual(state["elevenlabs"], 'Synced; 1 new requests')  # only the one made after the first run
        ids = [json.loads(text).get("id") for text in
               (self.config / usage_stats.OTHER_NAME).read_text(encoding="utf-8").splitlines()]
        self.assertEqual(sorted(ids), ["elevenlabs:later", "elevenlabs:own", "elevenlabs:web"])  # none twice
        days = (self.config / usage_stats.DAILY_SUMMARY).read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(text)["date"] for text in days], ["2026-10-16", "2026-10-17"])

    def test_daily_update_reports_problems_and_skips_lines_written_without_ids(self):
        import urllib.error
        usage_stats.record_voice(self.config, "eleven_flash_v2_5", 10, when=NOW - timedelta(hours=2))  # 0.35.x line

        def forbidden(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 401, "no", {}, None)

        state = usage_stats.daily_update(self.config, "k", NOW, forbidden)
        self.assertIn("HTTP 401", state["elevenlabs"])
        self.assertIn('access', state["elevenlabs"])
        self.assertEqual(state["summary"], "ok")
        self.assertEqual(usage_stats.daily_update(self.config, None, NOW, force=True)["elevenlabs"],
                         'No ElevenLabs key; skipped')
        self.assertEqual(usage_stats.daily_state(self.config)["last_run"], "2026-10-17")

        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
            def read(self, _limit):
                return json.dumps({"history": [{"history_item_id": "h", "date_unix": (NOW - timedelta(hours=2)).timestamp(),
                                                "text": "abcdefghij", "model_id": "eleven_flash_v2_5"}]}).encode()

        added = usage_stats.sync_elevenlabs(self.config, "k", NOW, lambda request, timeout: Response())
        self.assertEqual(added, 0)  # the same request as MYN's own line without an id

    def test_background_task_lines(self):
        usage_stats.record_agent(self.log, "claude-sonnet-5-5", "agent_task",
                                 {"input_tokens": 100, "output_tokens": 50}, 0.25)
        usage_stats.record_agent(self.log, "", "agent_task", {}, None)  # nothing known: no line
        lines = self.log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        result = usage_stats.report(self.config, "day", 0, datetime.now().astimezone())
        self.assertAlmostEqual(result["totals"]["cost_usd"], 0.25)
        self.assertEqual(result["by_purpose"][0][0], 'Background tasks')

    def test_spoken_questions(self):
        cases = {"今天花了多少token": ("day", 0), "今天用了多少托肯": ("day", 0),
                 "这周你花了多少钱": ("week", 0), "上周的费用是多少": ("week", -1),
                 "这个月花了多少费用": ("month", 0), "今年API花了多少": ("year", 0),
                 "昨天用了多少 tokens": ("day", -1), "前天花了多少token": ("day", -2), "how many tokens today": ("day", 0)}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(usage_stats.spoken_question(text), expected)
        for text in ("昨天买菜花了多少钱", "今天天气怎么样", "token 是什么", "打开用量统计"):
            with self.subTest(text=text):
                self.assertIsNone(usage_stats.spoken_question(text))


if __name__ == "__main__":
    unittest.main()
