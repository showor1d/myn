"""MYN's own calendar: events, repeats, reminders (each occurrence once), the chat tools
(deleting asks first) and the agenda in the chat's context."""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src" / "core"), str(BASE / "tests")]

import agenda
import chat
import claude_client
import local_actions
from test_web_tools import reply

NOW = datetime(2026, 10, 17, 10, 0)  # a Saturday, Tokyo wall-clock time


class Store(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="myn-agenda-")
        self.addCleanup(folder.cleanup)
        self.config = Path(folder.name)

    def test_add_change_delete(self):
        event = agenda.add(self.config, title="看牙医", start="2026-10-20 15:00", end="16:00",
                           location="市中心诊所", remind=30)
        self.assertEqual((event["start"], event["end"], event["all_day"]), ("2026-10-20 15:00", "2026-10-20 16:00", False))
        found = agenda.occurrences(self.config, datetime(2026, 10, 20), datetime(2026, 10, 21))
        self.assertEqual(agenda.describe(found[0]), '10/20 Tue 15:00–16:00 看牙医（市中心诊所）')
        agenda.update(self.config, event["id"], start="2026-10-21 10:00", end="")
        self.assertEqual(agenda.get(self.config, event["id"])["start"], "2026-10-21 10:00")
        self.assertEqual(agenda.get(self.config, event["id"])["location"], "市中心诊所")  # kept
        agenda.delete(self.config, event["id"])
        self.assertEqual(agenda.events(self.config), [])
        self.assertTrue((self.config / "calendar.json.bak").exists())  # the previous version is kept

    def test_checks(self):
        for bad in ({"title": "", "start": "2026-10-20 15:00"}, {"title": "x", "start": "明天下午"},
                    {"title": "x", "start": "2026-10-20 15:00", "end": "14:00"},
                    {"title": "x", "start": "2026-10-20", "repeat": "hourly"},
                    {"title": "x", "start": "2026-10-20", "remind": -5}):
            with self.subTest(bad=bad), self.assertRaises(agenda.AgendaError):
                agenda.add(self.config, **bad)
        all_day = agenda.add(self.config, title="妈妈生日", start="2026-11-03", repeat="yearly")
        self.assertTrue(all_day["all_day"])
        (self.config / agenda.FILE).write_text("{broken", encoding="utf-8")
        with self.assertRaises(agenda.AgendaError):
            agenda.events(self.config)

    def test_repeats(self):
        agenda.add(self.config, title="健身", start="2026-10-05 19:00", repeat="weekly")
        agenda.add(self.config, title="交房租", start="2026-01-31", repeat="monthly")
        agenda.add(self.config, title="喝水", start="2026-10-01 08:00", repeat="daily")
        week = agenda.occurrences(self.config, datetime(2026, 10, 19), datetime(2026, 10, 26))
        self.assertEqual([e["start"] for e in week if e["title"] == "健身"], [datetime(2026, 10, 19, 19, 0)])
        self.assertEqual(len([e for e in week if e["title"] == "喝水"]), 7)
        rent = agenda.occurrences(self.config, datetime(2027, 2, 1), datetime(2027, 3, 1))
        self.assertEqual([e["start"].day for e in rent if e["title"] == "交房租"], [28])  # 31st -> end of February

    def test_reminders_once_each(self):
        agenda.add(self.config, title="开会", start="2026-10-17 10:20", remind=30, repeat="daily")
        agenda.add(self.config, title="不提醒的", start="2026-10-17 10:10")
        due = agenda.due_reminders(self.config, NOW)
        self.assertEqual([e["title"] for e in due], ["开会"])
        self.assertEqual(agenda.reminder_text(due[0]), 'Event reminder: In 20 minutes, 开会。')
        self.assertEqual(agenda.due_reminders(self.config, NOW + timedelta(minutes=5)), [])  # said once
        tomorrow = agenda.due_reminders(self.config, NOW + timedelta(days=1))
        self.assertEqual([e["title"] for e in tomorrow], ["开会"])  # tomorrow's occurrence again
        agenda.add(self.config, title="出差", start="2026-10-17", remind=0)
        later = agenda.due_reminders(self.config, NOW.replace(hour=9, minute=5))
        self.assertEqual(agenda.reminder_text(later[0]), "Event reminder: Today, 出差。")

    def test_upcoming_for_the_chat(self):
        agenda.add(self.config, title="早上的事", start="2026-10-17 08:00")
        agenda.add(self.config, title="晚饭", start="2026-10-17 19:00", location="新宿")
        agenda.add(self.config, title="周一例会", start="2026-10-19 09:00")
        agenda.add(self.config, title="太远", start="2026-10-25 10:00")
        self.assertEqual(agenda.upcoming(self.config, NOW),
                         ['10/17 Sat 19:00 晚饭（新宿）', '10/19 Mon 09:00 周一例会'])

    def test_month_grid(self):
        weeks = agenda.month_days(2026, 10)
        self.assertEqual(weeks[0][:3], [None, None, None])  # 1 October 2026 is a Thursday
        self.assertEqual(weeks[0][3].day, 1)
        self.assertEqual(sum(1 for week in weeks for day in week if day), 31)


class Tools(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="myn-agenda-tools-")
        self.addCleanup(folder.cleanup)
        self.config = Path(folder.name)
        self.asked, self.answer = [], True

        def confirm(preview):
            self.asked.append(preview)
            return self.answer

        now = datetime(2026, 10, 17, 1, 0, tzinfo=timezone.utc)  # 10:00 in Tokyo
        self.context = local_actions.ActionContext(self.config, confirm=confirm, now=lambda: now)

    def run_action(self, name, **args):
        content, is_error = local_actions.execute(name, args, self.context)
        return json.loads(content), is_error

    def test_add_list_change_delete(self):
        result, error = self.run_action("add_event", title="看牙医", start="2026-10-20 15:00", remind_minutes=30)
        self.assertFalse(error)
        self.assertEqual(result["said"], '10/20 Tue 15:00 看牙医')
        self.assertEqual(self.asked, [])  # adding is direct
        listing, _ = self.run_action("list_events")
        self.assertEqual([e["title"] for e in listing["events"]], ["看牙医"])
        self.assertEqual((listing["from_date"], listing["to_date"]), ("2026-10-17", "2026-10-23"))
        event_id = listing["events"][0]["id"]
        changed, _ = self.run_action("update_event", id=event_id, start="2026-10-20 16:00")
        self.assertEqual(changed["said"], '10/20 Tue 16:00 看牙医')
        self.answer = False
        result, error = self.run_action("delete_event", id=event_id)
        self.assertTrue(error)
        self.assertIn("Delete calendar event: 2026-10-20 16:00 看牙医", self.asked[-1])
        self.assertEqual(len(agenda.events(self.config)), 1)
        self.answer = True
        self.run_action("delete_event", id=event_id)
        self.assertEqual(agenda.events(self.config), [])
        result, error = self.run_action("add_event", title="x", start="下周三")
        self.assertTrue(error)
        self.assertIn("YYYY-MM-DD", result["error"])


class InChat(unittest.TestCase):
    def test_tomorrow_is_in_the_context_and_events_can_be_added(self):
        with tempfile.TemporaryDirectory(prefix="myn-agenda-chat-") as folder:
            db = Path(folder) / "data" / "jarvis.db"
            db.parent.mkdir()
            session = chat.ChatSession(db)
            config = db.parent
            agenda.add(config, title="和朋友吃饭", start=(datetime.now(agenda.TOKYO) + timedelta(hours=26)).strftime("%Y-%m-%d %H:%M"))
            payloads = []

            def opener(request, timeout):
                payloads.append(json.loads(request.data.decode("utf-8")))
                if len(payloads) == 1:
                    return reply([{"type": "tool_use", "id": "toolu_a", "name": "add_event",
                                   "input": {"title": "交报告", "start": "2026-12-01 09:00"}}], stop="tool_use")
                return reply([{"type": "text", "text": "记下了。"}])

            with mock.patch.object(session, "_key", return_value="k"), \
                    mock.patch.object(claude_client.urllib.request, "urlopen", side_effect=opener):
                answer = session.ask("12月1号早上9点提醒我交报告")["answer"]
                self.assertIn('Saved calendar event', answer)
                self.assertIn("09:00 交报告", answer)
                self.assertIn('No reminder set', answer)
            dynamic = json.dumps(payloads[0]["system"], ensure_ascii=False)
            self.assertIn("和朋友吃饭", dynamic)
            self.assertIn("add_event", [tool["name"] for tool in payloads[0]["tools"]])
            self.assertEqual([e["title"] for e in agenda.events(config)], ["和朋友吃饭", "交报告"])


if __name__ == "__main__":
    unittest.main()
