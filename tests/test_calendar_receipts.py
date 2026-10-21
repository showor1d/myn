"""Calendar reminder success must be based on its own persisted tool receipt."""
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src/core")]
import agenda
import chat
import claude_client
import local_actions
import schedule_receipts


class CalendarReceipts(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="myn-calendar-receipts-")
        self.addCleanup(folder.cleanup)
        self.config = Path(folder.name)
        self.context = local_actions.ActionContext(self.config, confirm=lambda _: True)

    def execute(self, name, **args):
        text, failed = local_actions.execute(name, args, self.context)
        return name, json.loads(text), failed

    def test_saved_calendar_with_reminder_never_becomes_false_failure(self):
        receipt = self.execute("add_event", title="课程", start="2026-10-25 12:00",
                               end="13:00", remind_minutes=30)
        self.assertTrue(receipt[1]["verified"])
        text = schedule_receipts.finish("记得提醒我", "操作失败了", [receipt], True)
        self.assertIn('Saved calendar event', text)
        self.assertIn("12:00–13:00", text)
        self.assertIn('Remind 30 minutes before', text)
        self.assertNotIn('Failed', text)
        self.assertEqual(len(agenda.events(self.config)), 1)
        self.assertEqual(agenda.due_reminders(self.config, datetime(2026, 10, 25, 11, 29)), [])
        self.assertEqual(len(agenda.due_reminders(self.config, datetime(2026, 10, 25, 11, 30))), 1)
        self.assertEqual(agenda.due_reminders(self.config, datetime(2026, 10, 25, 11, 31)), [])

    def test_update_retains_lead_time_and_confirms_saved_time(self):
        event = agenda.add(self.config, title="课程", start="2026-10-25 10:00", remind=30)
        receipt = self.execute("update_event", id=event["id"], start="2026-10-25 12:00", end="13:00")
        text = schedule_receipts.finish("中午到一点，记得提醒我", "好的", [receipt], True)
        self.assertIn('Updated calendar event', text)
        self.assertIn('Remind 30 minutes before', text)
        self.assertEqual(agenda.events(self.config)[0]["remind"], 30)
        receipt = self.execute("update_event", id=event["id"], remind_minutes=60)
        self.assertIn("Remind 60 minutes before", schedule_receipts.finish("只提前一小时提醒一次", "", [receipt], True))
        self.assertEqual(len(agenda.events(self.config)), 1)
        self.assertEqual(agenda.due_reminders(self.config, datetime(2026, 10, 25, 10, 59)), [])
        self.assertEqual(len(agenda.due_reminders(self.config, datetime(2026, 10, 25, 11, 0))), 1)
        self.assertEqual(agenda.due_reminders(self.config, datetime(2026, 10, 25, 11, 30)), [])

    def test_no_reminder_is_not_a_failed_event_or_fake_reminder(self):
        receipt = self.execute("add_event", title="课程", start="2026-10-25 12:00")
        text = schedule_receipts.finish("记得提醒我", "设置好了", [receipt], True)
        self.assertIn('Saved calendar event', text)
        self.assertIn('No reminder set', text)
        self.assertIn('Remind at the event time', text)
        self.assertIsNone(agenda.events(self.config)[0]["remind"])

    def test_readback_mismatch_and_write_failure_do_not_confirm(self):
        original_get = agenda.get
        with mock.patch.object(agenda, "get", side_effect=lambda c, i: dict(original_get(c, i), remind=None)):
            receipt = self.execute("add_event", title="课程", start="2026-10-25 12:00", remind_minutes=30)
        self.assertTrue(receipt[2])
        text = schedule_receipts.finish("记得提醒我", "设置好了", [receipt], True)
        self.assertNotIn('Saved calendar event', text)
        with mock.patch.object(agenda, "_write", side_effect=OSError("disk unavailable")):
            receipt = self.execute("update_event", id=agenda.events(self.config)[0]["id"], remind_minutes=5)
        self.assertTrue(receipt[2])
        self.assertNotIn('Updated calendar event', schedule_receipts.finish("", "", [receipt], True))

    def test_delete_and_all_day_reminder_truthful(self):
        receipt = self.execute("add_event", title="全天活动", start="2026-10-25", remind_minutes=0)
        self.assertIn('Remind at 09:00 that day', schedule_receipts.finish("", "", [receipt], False))
        deleted = self.execute("delete_event", id=receipt[1]["id"])
        self.assertIn('Calendar event deleted', schedule_receipts.finish("", "", [deleted], False))
        self.assertEqual(agenda.events(self.config), [])

    def test_real_chat_collects_calendar_receipt_even_when_model_fails_after_save(self):
        session = chat.ChatSession(self.config / "jarvis.db")
        def model(*args, **kwargs):
            self.assertIsNone(kwargs["on_text"])
            text, failed = kwargs["tool_executor"]("add_event", dict(
                title="课程", start="2026-10-25 12:00", end="13:00", remind_minutes=30))
            self.assertFalse(failed, text)
            raise claude_client.ClaudeUnavailable("service stopped after save")
        with mock.patch.object(session, "_key", return_value="synthetic"), \
                mock.patch.object(claude_client, "coordinated_reply", side_effect=model):
            result = session.ask("周天中午到一点有课，记得提醒我")
        self.assertIn('Saved calendar event', result["answer"])
        self.assertIn('Remind 30 minutes before', result["answer"])
        self.assertIn('do not create duplicates', result["answer"])
        self.assertEqual(len(agenda.events(self.config)), 1)
        evidence = schedule_receipts.snapshot(self.config)["calendar"]
        self.assertEqual(evidence["events"][0]["remind_minutes"], 30)


if __name__ == "__main__":
    unittest.main()
