"""Dated alarm exceptions, permanent-scope guard, and real switch persistence."""
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src/core")]
import alarm_ringing
import local_actions
import schedule_receipts
import timekeeper
from gui_timers import TimersMixin

NOW = datetime(2026, 10, 21, 20)


class Occurrences(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="myn-alarm-occurrences-")
        self.addCleanup(folder.cleanup)
        self.config = Path(folder.name)
        self.context = local_actions.ActionContext(self.config,
            now=lambda: NOW.replace(tzinfo=timekeeper.TOKYO))

    def alarm(self, **fields):
        options = dict(kind="say", category="alarm", text="起床", time="08:00", repeat="weekdays")
        options.update(fields)
        return timekeeper.add_task(self.config, now=NOW, **options)

    def call(self, name, **args):
        raw, failed = local_actions.execute(name, args, self.context)
        self.assertFalse(failed, raw)
        return json.loads(raw)

    def test_skip_tomorrow_survives_restart_and_next_week_still_rings(self):
        alarm = self.alarm()
        result = self.call("skip_alarm", id=alarm["id"], date="2026-10-22")
        self.assertEqual(result["next_run"], "2026-10-23 08:00")
        reload = Path(str(self.config))
        self.assertEqual(timekeeper.get_task(reload, alarm["id"])["repeat"], "weekdays")
        self.assertEqual(timekeeper.due_tasks(reload, datetime(2026, 10, 22, 8)), [])
        self.assertEqual(alarm_ringing.active(reload), [])
        for day in (23, 26, 27):
            self.assertEqual(len(timekeeper.due_tasks(reload, datetime(2026, 10, day, 8))), 1)
        receipt = schedule_receipts.finish("", "", [("skip_alarm", result, False)], True)
        self.assertIn("2026-10-22 08:00", receipt)
        self.assertIn("2026-10-23 08:00", receipt)

    def test_future_skip_does_not_skip_earlier_occurrence_or_other_alarm(self):
        first = self.alarm()
        other = self.alarm(time="09:00")
        self.call("skip_alarm", id=first["id"], date="2026-10-23")
        self.assertEqual(timekeeper.get_task(self.config, first["id"])["next_run"], "2026-10-22 08:00")
        self.assertEqual(len(timekeeper.due_tasks(self.config, datetime(2026, 10, 22, 8))), 1)
        self.assertEqual(timekeeper.get_task(self.config, first["id"])["next_run"], "2026-10-26 08:00")
        self.assertEqual(timekeeper.get_task(self.config, other["id"])["next_run"], "2026-10-22 09:00")

    def test_invalid_dates_and_unscoped_delete_cannot_change_alarm(self):
        alarm = self.alarm()
        for args in (dict(date="2026-10-24"), dict(date="2026-10-22", time="09:00"),
                     dict(date="tomorrow"), dict(date="2026-10-20"), {}):
            raw, failed = local_actions.execute("skip_alarm", dict(id=alarm["id"], **args), self.context)
            self.assertTrue(failed, raw)
            self.assertEqual(timekeeper.get_task(self.config, alarm["id"]), alarm)
        for args in ({}, {"permanent": False}, {"permanent": "true"}):
            raw, failed = local_actions.execute("delete_alarm", dict(id=alarm["id"], **args), self.context)
            self.assertTrue(failed, raw)
        self.call("delete_alarm", id=alarm["id"], permanent=True)
        self.assertEqual(timekeeper.tasks(self.config), [])

    def test_multiple_skips_idempotence_and_pause_resume_preserve_exceptions(self):
        alarm = self.alarm()
        for day in (22, 23, 23):
            self.call("skip_alarm", id=alarm["id"], date="2026-10-%s" % day)
        self.call("update_alarm", id=alarm["id"], enabled=False)
        self.call("update_alarm", id=alarm["id"], enabled=True)
        current = timekeeper.get_task(self.config, alarm["id"])
        self.assertEqual(current["next_run"], "2026-10-26 08:00")
        self.assertEqual(len(current["skipped_runs"]), 2)

    def test_once_and_legacy_skip_are_not_recreated(self):
        alarm = self.alarm(repeat="once", date="2026-10-22")
        self.call("skip_alarm", id=alarm["id"], date="2026-10-22")
        self.assertFalse(timekeeper.get_task(self.config, alarm["id"])["enabled"])
        self.assertEqual(timekeeper.due_tasks(self.config, datetime(2026, 10, 22, 8)), [])
        local_actions._save_reminders(self.config, [
            dict(id="old", text="起床", due_utc="2026-10-21T23:00:00+00:00")])
        self.call("skip_alarm", id="reminder:old", date="2026-10-22")
        self.assertEqual(local_actions.reminders(self.config), [])

    def test_switch_round_trip_and_failure_receipt(self):
        alarm = self.alarm()
        window = TimersMixin()
        window.session = Mock(db_path=self.config / "jarvis.db")
        window.web = Mock()
        window._timers_changed = Mock()
        with patch("gui_timers.config_for_db", return_value=self.config), \
                patch("timekeeper.local_now", return_value=NOW):
            window._alarm_mini_event(dict(event="alarm_toggle", id=alarm["id"], enabled=False, request="1"))
            self.assertFalse(timekeeper.get_task(self.config, alarm["id"])["enabled"])
            result = window.web.js.call_args[0][0]
            self.assertEqual(result["request"], "1")
            self.assertFalse(result["alarms"][0]["enabled"])
            self.assertEqual(result["error"], "")
            window._alarm_mini_event(dict(event="alarm_toggle", id="missing", enabled=True, request="2"))
            self.assertTrue(window.web.js.call_args[0][0]["error"])

    def test_skipped_stale_next_run_never_dispatches_or_creates_ringing(self):
        task = self.alarm()
        task["skipped_runs"] = ["2026-10-22 08:00"]
        timekeeper._write(self.config / timekeeper.TASKS, [task])
        self.assertEqual(timekeeper.due_tasks(self.config, datetime(2026, 10, 22, 8)), [])
        self.assertEqual(alarm_ringing.active(self.config), [])

    def test_other_recurrences_and_failed_write(self):
        for fields, day, at, expected in (
            (dict(repeat="weekly", days=[3]), "2026-10-22", "08:00", "2026-10-29 08:00"),
            (dict(repeat="monthly", day_of_month=22), "2026-10-22", "08:00", "2026-11-22 08:00"),
            (dict(repeat="interval", every_minutes=60, date="2026-10-22"),
             "2026-10-22", "08:00", "2026-10-22 09:00")):
            task = self.alarm(**fields)
            result = self.call("skip_alarm", id=task["id"], date=day, time=at)
            self.assertEqual(result["next_run"], expected)
        task = self.alarm()
        with patch("timekeeper._write", side_effect=OSError("disk full")):
            raw, failed = local_actions.execute("skip_alarm",
                dict(id=task["id"], date="2026-10-22"), self.context)
        self.assertTrue(failed, raw)
        self.assertEqual(timekeeper.get_task(self.config, task["id"]), task)


if __name__ == "__main__":
    unittest.main()
