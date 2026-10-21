'Estimate sleep from a long silence between bedtime and wake-up messages, ignoring brief microphone noise.'

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
import presence
from test_web_tools import reply

UTC = timezone.utc
NIGHT = [("user", "我有点困了。", datetime(2026, 10, 16, 22, 55, 31, tzinfo=UTC)),
         ("assistant", "困了就去睡吧。", datetime(2026, 10, 16, 22, 55, 34, tzinfo=UTC)),
         ("user", "闹钟啥的。", datetime(2026, 10, 16, 22, 56, 59, tzinfo=UTC)),
         ("user", "在就睡。", datetime(2026, 10, 16, 22, 57, 1, tzinfo=UTC)),
         ("assistant", "好，去睡吧，晚安。", datetime(2026, 10, 16, 22, 57, 2, tzinfo=UTC)),
         ("user", "you", datetime(2026, 10, 16, 22, 59, 26, tzinfo=UTC)),
         ("user", "马云,我起床了。", datetime(2026, 10, 17, 5, 16, 58, tzinfo=UTC))]
ASKED = datetime(2026, 10, 17, 5, 36, 50, tzinfo=UTC)


class Presence(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="myn-presence-")
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name) / "data" / "jarvis.db"
        self.db.parent.mkdir()
        self.config = self.db.parent
        self.session = chat.ChatSession(self.db)  # a complete MYN database
        for role, text, moment in NIGHT:
            row = (conversation_log.append_user(self.db, text, "s") if role == "user"
                   else conversation_log.append_assistant(self.db, text, "s", None))
            with sqlite3.connect(str(self.db)) as connection:
                connection.execute("UPDATE conversation_messages SET created_at=? WHERE id=?", (moment.isoformat(), row))

    def test_noise_is_not_the_user(self):
        for noise in ("you", "Yeah.", "嗯", "嗯嗯", "Oh", "Thank you.", "うん", "啊…"):
            self.assertFalse(presence.meaningful(noise), noise)
        for said in ("在就睡。", "好啊", "我起床了", "计时"):
            self.assertTrue(presence.meaningful(said), said)

    def test_the_real_morning(self):
        guess = presence.sleep_estimate(self.db, self.config, now=ASKED)
        self.assertEqual((guess["fell_asleep_around"], guess["woke_up_around"], guess["slept_about"]),
                         ('2026-10-17 Sat 07:57:01', '2026-10-17 Sat 14:16:58', "6 hours 20 minutes"))
        self.assertEqual(guess["likelihood"], 'Very likely')
        self.assertEqual(guess["last_words_before"], "在就睡。")
        self.assertTrue(any("sleep" in reason for reason in guess["why"]))
        # while still asleep (asked at noon, nothing after 07:57): away since then
        noon = datetime(2026, 10, 17, 3, 0, tzinfo=UTC)
        away = presence.sleep_estimate(self.db, self.config, now=noon)
        self.assertTrue(away["still_away_now"])
        self.assertIsNone(away["woke_up_around"])
        # the previous conversation also ends at the real last words, not at the "you"
        gap = conversation_log.last_break(self.db, now=ASKED)
        self.assertEqual(gap["previous_conversation_ended_at"], '2026-10-17 Sat 07:57')

    def test_keyboard_and_mouse(self):
        # MYN ran all night and nobody touched the computer: one more reason
        start = datetime(2026, 10, 16, 22, 50, tzinfo=UTC)
        for minute in range(0, 400, 1):
            presence.note(self.config, idle_seconds=99999, now=start + timedelta(minutes=minute))
        guess = presence.sleep_estimate(self.db, self.config, now=ASKED)
        self.assertTrue(any("no keyboard or mouse activity" in reason for reason in guess["why"]), guess["why"])
        stored = json.loads((self.config / presence.FILE).read_text(encoding="utf-8"))
        self.assertEqual(len(stored["running"]), 1)  # one merged span, not 400 entries
        # someone used the computer at 11:00 Tokyo: that wasn't one long sleep
        presence.note(self.config, idle_seconds=5, now=datetime(2026, 10, 17, 2, 0, tzinfo=UTC))
        guess = presence.sleep_estimate(self.db, self.config, now=ASKED)
        self.assertNotEqual(guess["slept_about"], "6 hours 20 minutes")
        self.assertEqual(guess["fell_asleep_around"], '2026-10-17 Sat 10:59:55')  # the latest quiet stretch

    def test_no_long_silence(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "j.db"
            conversation_log.ensure_schema(db)
            conversation_log.append_user(db, "你好", "s")
            self.assertIsNone(presence.sleep_estimate(db, folder))

    def test_asked_in_chat(self):
        session = self.session
        payloads = []

        def opener(request, timeout):
            payloads.append(json.loads(request.data.decode("utf-8")))
            return reply([{"type": "text", "text": "你大概 7:57 睡的，睡了 6 小时 20 分左右。"}])

        real = presence.sleep_estimate
        with mock.patch.object(session, "_key", return_value="k"), \
                mock.patch.object(presence, "sleep_estimate", lambda db, config, now=None: real(db, config, now=ASKED)), \
                mock.patch.object(claude_client.urllib.request, "urlopen", side_effect=opener):
            result = session.ask("我昨晚睡了多久?")
        self.assertIn("6 小时 20 分", result["answer"])
        system = json.dumps(payloads[0]["system"], ensure_ascii=False)
        self.assertIn("sleep_estimate", system)
        self.assertIn("07:57:01", system)
        self.assertIn("在就睡。", system)
        self.assertIn("history_by_time", [tool["name"] for tool in payloads[0]["tools"]])  # ordinary chat, with tools


if __name__ == "__main__":
    unittest.main()
