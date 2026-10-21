"""Timers and scheduled tasks: countdowns (pause / resume / add / cancel / due once), when
scheduled tasks run (every repeat kind), missed runs, the chat tools (deleting asks; an
action that normally asks is asked when the task is made) and running an 'ask' task by
itself (not saved as something the user said, actions needing a yes are declined)."""

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src" / "core"), str(BASE / "tests")]

import chat
import claude_client
import local_actions
import timekeeper
from test_web_tools import reply

NOW = datetime(2026, 10, 17, 10, 0)  # Saturday, Tokyo wall-clock time


class Timers(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="myn-timers-")
        self.addCleanup(folder.cleanup)
        self.config = Path(folder.name)

    def test_countdown(self):
        item = timekeeper.start_timer(self.config, minutes=5, label="泡面", now=1000)
        self.assertEqual(timekeeper.timer_for_model(item)["left"], '5 minutes')
        self.assertEqual(timekeeper.clock(timekeeper.timers(self.config, now=1060)[0]["left"]), "04:00")
        timekeeper.control_timer(self.config, "pause", now=1060)
        self.assertEqual(timekeeper.due_timers(self.config, now=9999), [])  # paused never runs out
        timekeeper.control_timer(self.config, "resume", now=2000)
        timekeeper.control_timer(self.config, "add", minutes=1, now=2000)
        left = timekeeper.timers(self.config, now=2000)[0]["left"]
        self.assertEqual(left, 300)  # 4 minutes left + 1
        self.assertEqual(timekeeper.due_timers(self.config, now=2299), [])
        done = timekeeper.due_timers(self.config, now=2300)
        self.assertEqual(timekeeper.timer_done_text(done[0]), "Time is up: 泡面 (6 minutes).")
        self.assertEqual(timekeeper.due_timers(self.config, now=2400), [])  # said once
        self.assertEqual(timekeeper.timers(self.config), [])

    def test_several_and_checks(self):
        first = timekeeper.start_timer(self.config, seconds=30, now=0)
        timekeeper.start_timer(self.config, minutes=25, label="番茄钟", now=0)
        with self.assertRaises(timekeeper.TimerError):
            timekeeper.control_timer(self.config, "pause", now=1)  # which one?
        timekeeper.control_timer(self.config, "cancel", first["id"], now=1)
        self.assertEqual([t["label"] for t in timekeeper.timers(self.config, now=1)], ["番茄钟"])
        for bad in ({"minutes": 0}, {"minutes": 2000}, {"seconds": -5}, {"minutes": True}):
            with self.subTest(bad=bad), self.assertRaises(timekeeper.TimerError):
                timekeeper.start_timer(self.config, **bad)
        self.assertEqual(timekeeper.timer_done_text({"seconds": 30}), 'Time is up for 30 seconds.')


class Schedules(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="myn-schedules-")
        self.addCleanup(folder.cleanup)
        self.config = Path(folder.name)

    def add(self, **fields):
        return timekeeper.add_task(self.config, now=NOW, **fields)

    def test_next_run_for_each_repeat(self):
        cases = [({"repeat": "once", "time": "09:00"}, "2026-10-18 09:00"),  # passed today -> tomorrow
                 ({"repeat": "daily", "time": "22:30"}, "2026-10-17 22:30"),
                 ({"repeat": "weekdays", "time": "08:00"}, "2026-10-19 08:00"),  # Sat -> Mon
                 ({"repeat": "weekends", "time": "11:00"}, "2026-10-17 11:00"),
                 ({"repeat": "weekly", "time": "18:00", "days": [4]}, "2026-10-23 18:00"),
                 ({"repeat": "monthly", "time": "09:00", "day_of_month": 31}, "2026-10-31 09:00"),
                 ({"repeat": "interval", "every_minutes": 60, "from_time": "09:00", "to_time": "21:00"},
                  "2026-10-17 11:00"),
                 ({"repeat": "interval", "every_minutes": 30}, "2026-10-17 10:30")]
        for fields, expected in cases:
            with self.subTest(fields=fields):
                task = self.add(kind="say", text="喝水", **fields)
                self.assertEqual(task["next_run"], expected)
        november = timekeeper.next_after(dict(self.add(kind="say", text="x", repeat="monthly", time="09:00",
                                                       day_of_month=31)), datetime(2026, 11, 1))
        self.assertEqual(november, datetime(2026, 11, 30, 9, 0))  # 31st -> last day
        late = timekeeper.next_after(self.add(kind="say", text="x", repeat="interval", every_minutes=60,
                                              from_time="09:00", to_time="21:00"), datetime(2026, 10, 17, 21, 30))
        self.assertEqual(late, datetime(2026, 10, 18, 9, 0))
        self.assertEqual(timekeeper.describe_when(self.add(kind="say", text="x", repeat="weekly", time="18:00",
                                                           days=[0, 4])), 'Every Mon、Fri at 18:00')

    def test_checks(self):
        for bad in ({"kind": "say", "text": "", "repeat": "daily", "time": "08:00"},
                    {"kind": "say", "text": "x", "repeat": "daily", "time": "8点"},
                    {"kind": "say", "text": "x", "repeat": "once", "date": "2026-10-16", "time": "08:00"},
                    {"kind": "say", "text": "x", "repeat": "weekly", "time": "08:00"},
                    {"kind": "say", "text": "x", "repeat": "interval", "every_minutes": 1},
                    {"kind": "action", "repeat": "daily", "time": "08:00", "action": {"name": "write_file"}},
                    {"kind": "dance", "repeat": "daily", "time": "08:00"}):
            with self.subTest(bad=bad), self.assertRaises(timekeeper.TimerError):
                self.add(**bad)

    def test_due_moves_on_and_missed_runs(self):
        daily = self.add(kind="say", text="该睡觉了", repeat="daily", time="10:05")
        once = self.add(kind="say", text="出门", repeat="once", time="10:10")
        self.assertEqual(timekeeper.due_tasks(self.config, datetime(2026, 10, 17, 10, 4)), [])
        due = timekeeper.due_tasks(self.config, datetime(2026, 10, 17, 10, 6))
        self.assertEqual([t["text"] for t in due], ["该睡觉了"])
        self.assertEqual(timekeeper.get_task(self.config, daily["id"])["next_run"], "2026-10-18 10:05")
        self.assertEqual(timekeeper.due_tasks(self.config, datetime(2026, 10, 17, 10, 7)), [])  # once
        self.assertEqual([t["text"] for t in timekeeper.due_tasks(self.config, datetime(2026, 10, 17, 10, 10))],
                         ["出门"])
        finished = timekeeper.get_task(self.config, once["id"])
        self.assertEqual((finished["enabled"], finished["finished"]), (False, True))
        # MYN was off for two days: the run is noted as missed, not done late
        self.assertEqual(timekeeper.due_tasks(self.config, datetime(2026, 10, 20, 12, 0)), [])
        missed = timekeeper.get_task(self.config, daily["id"])
        self.assertIn('Missed', missed["last_result"])
        self.assertEqual(missed["next_run"], "2026-10-21 10:05")

    def test_pause_change_delete(self):
        task = self.add(kind="ask", text="今天天气怎么样", repeat="daily", time="08:00", name="早报")
        paused = timekeeper.update_task(self.config, task["id"], enabled=False, now=NOW)
        self.assertEqual(paused["next_run"], "")
        self.assertEqual(timekeeper.due_tasks(self.config, datetime(2026, 10, 18, 8, 0)), [])
        moved = timekeeper.update_task(self.config, task["id"], enabled=True, time="07:30", now=NOW)
        self.assertEqual((moved["next_run"], moved["text"], moved["name"]), ("2026-10-18 07:30", "今天天气怎么样", "早报"))
        with self.assertRaises(timekeeper.TimerError):
            timekeeper.update_task(self.config, task["id"], kind="say", now=NOW)
        timekeeper.delete_task(self.config, task["id"])
        self.assertEqual(timekeeper.tasks(self.config), [])


class Tools(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="myn-schedule-tools-")
        self.addCleanup(folder.cleanup)
        self.config = Path(folder.name)
        self.asked, self.answer, self.ran = [], True, []

        def confirm(preview):
            self.asked.append(preview)
            return self.answer

        now = datetime(2026, 10, 17, 1, 0, tzinfo=timezone.utc)  # 10:00 in Tokyo
        self.context = local_actions.ActionContext(self.config, confirm=confirm, now=lambda: now,
                                                   run_task=self.ran.append)

    def run_action(self, tool, **args):
        content, is_error = local_actions.execute(tool, args, self.context)
        return json.loads(content), is_error

    def test_timer_tools(self):
        result, error = self.run_action("start_timer", minutes=3, label="泡面")
        self.assertFalse(error)
        self.assertEqual(result["said"], 'Start timer 3 minutes')
        listing, _ = self.run_action("list_timers")
        self.assertEqual([t["label"] for t in listing["timers"]], ["泡面"])
        paused, _ = self.run_action("timer_control", action="pause")
        self.assertEqual(paused["timer"]["state"], 'Paused')
        self.run_action("timer_control", action="cancel")
        self.assertEqual(self.run_action("list_timers")[0]["timers"], [])
        self.assertEqual(self.asked, [])  # timers never ask

    def test_schedule_tools(self):
        result, error = self.run_action("add_schedule", kind="ask", text="告诉我天气和今天的安排",
                                        repeat="daily", time="08:00", name="早报")
        self.assertFalse(error)
        self.assertEqual((result["schedule"]["when"], result["schedule"]["next_run"]), ('Daily 08:00', "2026-10-18 08:00"))
        volume, _ = self.run_action("add_schedule", kind="action", action_name="set_volume",
                                    action_arguments={"level": 20}, repeat="weekdays", time="23:00")
        self.assertEqual(self.asked, [])  # volume: no need to ask
        self.assertIn("volume", volume["schedule"]["what"])
        self.answer = False
        refused, error = self.run_action("add_schedule", kind="action", action_name="power",
                                         action_arguments={"action": "shutdown"}, repeat="once", time="23:30")
        self.assertTrue(error)
        self.assertIn("run without asking again when due", self.asked[-1])
        self.assertEqual(len(self.run_action("list_schedules")[0]["schedules"]), 2)
        self.answer = True
        self.run_action("add_schedule", kind="action", action_name="power",
                        action_arguments={"action": "shutdown"}, repeat="once", time="23:30")
        schedules = self.run_action("list_schedules")[0]["schedules"]
        self.assertEqual(len(schedules), 3)
        early = [s for s in schedules if s["name"] == "早报"][0]
        changed, _ = self.run_action("update_schedule", id=early["id"], enabled=False)
        self.assertFalse(changed["schedule"]["enabled"])
        self.run_action("run_schedule_now", id=early["id"])
        self.assertEqual([t["name"] for t in self.ran], ["早报"])
        asked_before = len(self.asked)
        self.answer = False
        _, error = self.run_action("delete_schedule", id=early["id"])
        self.assertTrue(error)
        self.assertEqual(len(self.asked), asked_before + 1)
        self.answer = True
        self.run_action("delete_schedule", id=early["id"])
        self.assertEqual(len(self.run_action("list_schedules")[0]["schedules"]), 2)
        # a group the user switched off cannot be scheduled either
        import dataclasses
        local_actions.save_settings(self.config, dataclasses.replace(local_actions.load_settings(self.config),
                                                                     media=False))
        _, error = self.run_action("add_schedule", kind="action", action_name="set_volume",
                                   action_arguments={"level": 5}, repeat="daily", time="07:00")
        self.assertTrue(error)


class ScheduledAsk(unittest.TestCase):
    def test_runs_by_itself_without_saving_it_as_something_said(self):
        with tempfile.TemporaryDirectory(prefix="myn-scheduled-ask-") as folder:
            db = Path(folder) / "data" / "jarvis.db"
            db.parent.mkdir()
            session = chat.ChatSession(db)
            asked = []
            session.confirm_action = lambda preview: asked.append(preview) or True
            payloads = []

            def opener(request, timeout):
                payloads.append(json.loads(request.data.decode("utf-8")))
                if len(payloads) == 1:
                    return reply([{"type": "tool_use", "id": "toolu_a", "name": "power",
                                   "input": {"action": "shutdown"}}], stop="tool_use")
                return reply([{"type": "text", "text": "早上好，今天晴。"}])

            with mock.patch.object(session, "_key", return_value="k"), \
                    mock.patch.object(claude_client.urllib.request, "urlopen", side_effect=opener):
                # Scheduled requests still receive action tools even when they resemble memory questions.
                result = session.ask_scheduled({"name": "早报", "text": "我今天有什么安排？"})
            self.assertTrue(result["answer"].startswith("早上好，今天晴。"))
            self.assertIn('did not consent', result["answer"])  # says honestly the shutdown was not done
            self.assertEqual(asked, [])  # nobody was asked: the shutdown was declined
            tool_result = payloads[1]["messages"][-1]["content"][0]
            self.assertTrue(tool_result.get("is_error"))
            self.assertIn("power", [tool["name"] for tool in payloads[0]["tools"]])
            # closing(): "with sqlite3.connect()" alone only commits, and an open file can't be
            # deleted on Windows when the temporary folder is cleaned up.
            with closing(sqlite3.connect(str(db))) as connection:
                saved = connection.execute("SELECT count(*) FROM sent_messages").fetchone()[0]
            self.assertEqual(saved, 0)  # not saved as something the user said
            self.assertFalse(session._unattended)


if __name__ == "__main__":
    unittest.main()
