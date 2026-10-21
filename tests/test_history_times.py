'Conversation history retains local timestamps, previous conversation end and silence duration. Test sleep questions with original message times and bounded history tools.'

import json
import sqlite3
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
import conversation_log
import local_tools
from test_web_tools import reply

UTC = timezone.utc


def at(db, row_id, moment):
    with sqlite3.connect(str(db)) as connection:
        connection.execute("UPDATE conversation_messages SET created_at=? WHERE id=?", (moment.isoformat(), row_id))


class HistoryTimes(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="myn-history-times-")
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name) / "data" / "jarvis.db"
        self.db.parent.mkdir()
        self.session = chat.ChatSession(self.db)
        night = datetime(2026, 10, 16, 22, 56, 28, tzinfo=UTC)  # 07:56 Tokyo
        for offset, (role, text) in enumerate([("user", "我觉得我睡到自然醒比较好一点。"),
                                               ("assistant", "嗯，自然醒挺好的，晚安。"),
                                               ("user", "好，睡了。")]):
            if role == "user":
                row = conversation_log.append_user(self.db, text, "night")
            else:
                row = conversation_log.append_assistant(self.db, text, "night", None)
            at(self.db, row, night + timedelta(minutes=offset * 1.5))

    def test_local_times_and_the_last_break(self):
        self.assertEqual(conversation_log.local_time("2026-10-16T22:59:26+00:00"), '2026-10-17 Sat 07:59')
        now = datetime(2026, 10, 17, 5, 17, 40, tzinfo=UTC)  # 14:17 Tokyo
        gap = conversation_log.last_break(self.db, now=now)
        self.assertEqual(gap["previous_conversation_ended_at"], '2026-10-17 Sat 07:59')
        self.assertEqual(gap["last_message_then"]["text"], "好，睡了。")
        self.assertEqual(gap["silence"], "6 hours 18 minutes")
        # once a new conversation has started, the break is the one before it
        row = conversation_log.append_user(self.db, "我起床了。", "day")
        at(self.db, row, datetime(2026, 10, 17, 5, 17, 26, tzinfo=UTC))
        gap = conversation_log.last_break(self.db, now=now)
        self.assertEqual((gap["previous_conversation_ended_at"], gap["this_conversation_started_at"]),
                         ('2026-10-17 Sat 07:59', '2026-10-17 Sat 14:17'))

    def test_history_tools_give_tokyo_times(self):
        content, error = local_tools.execute("history_by_time", {"start_at": "2026-10-17T07:00:00+09:00",
                                                                  "end_at": "2026-10-17T09:00:00+09:00"}, self.db)
        result = json.loads(content)["result"]
        self.assertFalse(error)
        self.assertEqual([(m["time"], m["role"]) for m in result["messages"]],
                         [('2026-10-17 Sat 07:56:28', "user"), ('2026-10-17 Sat 07:57:58', "assistant"),
                          ('2026-10-17 Sat 07:59:28', "user")])
        content, error = local_tools.execute("history_by_time", {"start_at": "2026-10-17T07:00:00",
                                                                  "end_at": "2026-10-17T09:00:00+09:00"}, self.db)
        self.assertTrue(error)  # a time without timezone is refused, not guessed
        content, _ = local_tools.execute("search_history", {"query": "自然醒"}, self.db)
        found = json.loads(content)["result"]
        self.assertEqual(found[0]["time_local"], '2026-10-17 Sat 07:56:28')
        self.assertNotIn("created_at", found[0])  # no UTC time to misread

    def test_how_long_did_i_sleep(self):
        payloads = []

        def opener(request, timeout):
            payloads.append(json.loads(request.data.decode("utf-8")))
            return reply([{"type": "text", "text": "你大概早上 8 点睡的，睡了六个多小时。"}])

        with mock.patch.object(self.session, "_key", return_value="k"), \
                mock.patch.object(claude_client.urllib.request, "urlopen", side_effect=opener):
            self.session.ask("我起床了。")
            self.session.ask("我睡了多久啊")
        # The wake-up scene acknowledgement is now local and needs no API call.
        second, = payloads
        dynamic = json.dumps(second["system"], ensure_ascii=False)
        self.assertIn("previous_conversation_ended_at", dynamic)
        self.assertIn('Sat 07:59', dynamic)
        self.assertIn("好，睡了。", dynamic)
        self.assertIn("history_by_time", [tool["name"] for tool in second["tools"]])
        self.assertIn("never write such markers yourself", dynamic)
        earlier = second["messages"][0]["content"]  # The wake-up message includes its timestamp.
        self.assertRegex(earlier, '^\\[2026-\\d\\d-\\d\\d [A-Za-z]{3} \\d\\d:\\d\\d\\] 我起床了。$')
        self.assertEqual(second["messages"][-1]["content"], "我睡了多久啊")  # the current one as typed


if __name__ == "__main__":
    unittest.main()
