"""Alarms remain durable and audible while separate from timer lists and counts."""
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src/core")]
import local_actions
import schedule_receipts
import timekeeper
import timer_display
from gui_timers import TimersMixin

NOW = datetime(2026, 10, 20, 20, tzinfo=timekeeper.TOKYO)


class Alarms(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="myn-alarm-separate-")
        self.addCleanup(folder.cleanup)
        self.config = Path(folder.name)
        self.context = local_actions.ActionContext(self.config, now=lambda: NOW,
                                                  confirm=lambda _: True)

    def call(self, tool_name, **args):
        raw, failed = local_actions.execute(tool_name, args, self.context)
        self.assertFalse(failed, raw)
        return json.loads(raw)

    def alarm(self, **args):
        fields = dict(time="08:00", date="2026-10-21", text="该起床了")
        fields.update(args)
        return self.call("add_alarm", **fields)

    def test_new_alarm_separate_list_count_receipt_and_once_after_restart(self):
        result = self.alarm()
        timer = self.call("start_timer", minutes=5)
        self.call("add_schedule", kind="ask", text="天气", repeat="daily", time="09:00")
        self.assertTrue(result["verified"])
        self.assertEqual(len(self.call("list_alarms")["alarms"]), 1)
        self.assertEqual(len(self.call("list_schedules")["schedules"]), 1)
        view = timer_display.snapshot(Path(str(self.config)), now=NOW.timestamp())
        self.assertEqual((view["count"], view["alarm_upcoming"]), (2, True))
        self.assertNotIn("alarms", view)
        self.assertNotIn("alarm_count", view)
        answer = schedule_receipts.finish("明早八点叫醒我", "", [("add_alarm", result, False)], True)
        self.assertIn('Saved Alarm', answer)
        self.assertNotIn('Scheduled task', answer)
        self.assertEqual(timekeeper.due_tasks(self.config, datetime(2026, 10, 21, 7, 59)), [])
        due = timekeeper.due_tasks(self.config, datetime(2026, 10, 21, 8))
        self.assertEqual([t["id"] for t in due], [result["schedule"]["id"]])
        self.assertEqual(timekeeper.due_tasks(self.config, datetime(2026, 10, 21, 8)), [])
        self.assertFalse(timer_display.snapshot(self.config, now=NOW.timestamp())["alarm_upcoming"])
        self.assertTrue(self.call("list_alarms")["ringing"])
        self.assertTrue(self.call("list_timers")["timers"])

    def test_repeat_pause_resume_modify_and_delete_stay_separate(self):
        alarm = self.alarm(repeat="daily")["schedule"]
        self.call("update_alarm", id=alarm["id"], enabled=False)
        self.assertFalse(timer_display.snapshot(self.config, now=NOW.timestamp())["alarm_upcoming"])
        self.call("update_alarm", id=alarm["id"], enabled=True, time="09:15", name="晨间")
        self.assertEqual(self.call("list_alarms")["alarms"][0]["next_run"], "2026-10-21 09:15")
        self.assertEqual(self.call("list_schedules")["schedules"], [])
        due = timekeeper.due_tasks(self.config, datetime(2026, 10, 21, 9, 15))
        self.assertEqual(len(due), 1)
        self.assertEqual(timekeeper.get_task(self.config, alarm["id"])["next_run"], "2026-10-22 09:15")
        self.call("delete_alarm", id=alarm["id"], permanent=True)
        self.assertEqual(self.call("list_alarms")["alarms"], [])

    def test_legacy_records_read_only_classification_and_edit_without_duplicates(self):
        task = timekeeper.add_task(self.config, now=NOW.replace(tzinfo=None),
                                  kind="say", text="叫醒我", time="08:00", repeat="daily")
        legacy = dict(id="old", text="起床", due_utc="2026-10-20T23:00:00+00:00")
        local_actions._save_reminders(self.config, [legacy])
        path = self.config / local_actions.REMINDERS_NAME
        before = path.read_bytes()
        self.assertEqual(len(self.call("list_alarms")["alarms"]), 2)
        self.assertEqual(self.call("list_schedules")["schedules"], [])
        self.assertEqual(self.call("list_reminders")["reminders"], [])
        self.assertEqual(timer_display.snapshot(self.config)["count"], 0)
        self.assertEqual(path.read_bytes(), before)
        self.call("update_alarm", id=task["id"], name="晨间", text="早上好")
        self.assertEqual(len(self.call("list_alarms")["alarms"]), 2)
        self.call("update_alarm", id="reminder:old", time="09:00", text="早上好")
        self.assertEqual(len(local_actions._reminder_records(self.config)), 1)
        self.assertTrue(timer_display.snapshot(self.config, now=NOW.timestamp())["alarm_upcoming"])
        self.call("delete_alarm", id="reminder:old", permanent=True)
        self.assertEqual(local_actions.reminders(self.config), [])

    def test_alarm_tools_do_not_mutate_other_task_and_deletion_requires_consent(self):
        task = self.call("add_schedule", kind="say", text="喝水", repeat="daily", time="21:00")["schedule"]
        for name in ("update_alarm", "delete_alarm"):
            raw, failed = local_actions.execute(name, {"id": task["id"]}, self.context)
            self.assertTrue(failed, raw)
        alarm = self.alarm()["schedule"]
        for name in ("update_schedule", "delete_schedule", "run_schedule_now"):
            raw, failed = local_actions.execute(name, {"id": alarm["id"]}, self.context)
            self.assertTrue(failed, raw)
        self.context.confirm = lambda _: False
        local_actions.save_settings(self.config, local_actions.ActionSettings(confirm="always"))
        raw, failed = local_actions.execute("delete_alarm", {"id": alarm["id"]}, self.context)
        self.assertTrue(failed)
        local_actions.save_settings(self.config, local_actions.ActionSettings())
        self.assertEqual(len(self.call("list_alarms")["alarms"]), 1)
        local_actions.save_settings(self.config, local_actions.ActionSettings(reminders=False))
        raw, failed = local_actions.execute("add_alarm", {"time": "08:00", "text": "起床"}, self.context)
        self.assertTrue(failed)
        self.assertTrue(set(local_actions.GROUP_OF) <= set(local_actions.LABELS))

    def test_invalid_alarm_cannot_confirm_saved(self):
        raw, failed = local_actions.execute("add_alarm", dict(time="25:00", text="起床"), self.context)
        self.assertTrue(failed)
        answer = schedule_receipts.finish("定闹钟", "已设好", [("add_alarm", json.loads(raw), failed)], True)
        self.assertNotIn('Saved Alarm', answer)
        self.assertEqual(self.call("list_alarms")["alarms"], [])

    def test_spoken_alarm_dispatch_bypasses_busy_task_queue(self):
        task = timekeeper.get_task(self.config, self.alarm()["schedule"]["id"])
        window = TimersMixin()
        window.busy = True
        window.session = Mock(db_path=self.config / "database" / "jarvis.db")
        window._deliver_alarm = Mock()
        window._note_task = Mock()
        window._queue_task(task)
        window._deliver_alarm.assert_called_once_with("该起床了", "alarm", "schedule:" + task["id"])
        self.assertFalse(window.task_queue)


if __name__ == "__main__":
    unittest.main()
