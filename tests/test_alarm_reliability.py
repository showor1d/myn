"""Persistence, truthful receipts, historical evidence and offline alarm dispatch."""
import json
import queue
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src/core")]
import alarm_delivery
import chat
import claude_client
import interaction
import local_actions
import timekeeper
import myn_gui
import schedule_receipts

NOW = datetime(2026, 10, 18, 13, 4, tzinfo=timezone.utc)


class Alarms(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="myn-alarm-reliability-")
        self.addCleanup(temp.cleanup)
        self.config = Path(temp.name)
        self.context = local_actions.ActionContext(self.config, now=lambda: NOW)

    def set_alarm(self):
        content, failed = local_actions.execute(
            "set_reminder", dict(text="该起床了", at="2026-10-19 08:00"), self.context)
        self.assertFalse(failed, content)
        return json.loads(content)

    def test_save_readback_restart_fire_once_and_keep_evidence(self):
        receipt = self.set_alarm()
        self.assertTrue(receipt["verified"])
        self.assertEqual(receipt["due_local"], "2026-10-19 08:00")
        self.assertEqual(local_actions.due_reminders(self.config, NOW), [])
        due = local_actions.due_reminders(self.config, NOW + timedelta(hours=10))
        self.assertEqual([r["id"] for r in due], [receipt["id"]])
        self.assertEqual(local_actions.due_reminders(self.config, NOW + timedelta(days=1)), [])
        # New context, no memory cache: pending empty, historical creation/firing retained.
        other = local_actions.ActionContext(self.config)
        result = json.loads(local_actions._list_reminders({}, other)[0])
        self.assertEqual(result["reminders"], [])
        self.assertEqual(result["history"][0]["status"], "triggered")
        self.assertEqual(result["creation_audit"][0]["tool"], "set_reminder")
        self.assertEqual(result["delivery"], [])  # dispatch is not proof of playing

    def test_cancelled_and_triggered_history_survives_new_alarm(self):
        first = self.set_alarm()
        local_actions.execute("cancel_reminder", {"id": first["id"]}, self.context)
        second = self.set_alarm()
        self.assertEqual([r["id"] for r in local_actions.reminders(self.config)], [second["id"]])
        rows = local_actions._reminder_records(self.config)
        self.assertEqual(rows[0]["status"], "cancelled")

    def test_corrupt_files_fail_without_overwriting(self):
        for filename, name, args in [
            (local_actions.REMINDERS_NAME, "set_reminder", dict(text="x", in_minutes=20)),
            (timekeeper.TASKS, "add_schedule", dict(kind="say", text="x", repeat="daily", time="08:00"))]:
            path = self.config / filename
            path.write_text("{broken", encoding="utf-8")
            content, failed = local_actions.execute(name, args, self.context)
            self.assertTrue(failed, content)
            self.assertEqual(path.read_text(encoding="utf-8"), "{broken")

    def test_save_and_readback_failure_not_success(self):
        for patcher in [mock.patch.object(local_actions, "_save_reminders", side_effect=OSError("disk")),
                        mock.patch.object(local_actions, "_save_reminders", return_value=None)]:
            with patcher:
                content, failed = local_actions.execute(
                    "set_reminder", dict(text="x", in_minutes=20), self.context)
            self.assertTrue(failed, content)
            self.assertFalse(json.loads(content)["ok"])

    def session(self):
        session = chat.ChatSession(self.config / "jarvis.db")
        key = mock.patch.object(session, "_key", return_value="synthetic")
        key.start()
        self.addCleanup(key.stop)
        return session

    def test_model_promise_without_tool_never_streamed_or_saved_as_success(self):
        session = self.session()
        heard = []
        def brain(*args, **kwargs):
            self.assertIsNone(kwargs["on_text"])
            return "好的，已经订好了，明早八点会叫你。"
        with mock.patch.object(claude_client, "coordinated_reply", side_effect=brain):
            result = session.ask("帮我定个明天早上八点的闹钟提醒我起床", on_text=heard.append)
        self.assertIn('No successful reminder creation or edit was recorded', result["answer"])
        self.assertEqual(heard, [])
        self.assertEqual(local_actions.reminders(self.config), [])

    def test_empty_promise_and_clarification_have_distinct_outcomes(self):
        session = self.session()
        for reply, expected in [("没问题，安排上了", "No successful reminder creation or edit was recorded"),
                                ("请问你想设明天几点的闹钟？", "请问你想设明天几点的闹钟？")]:
            with mock.patch.object(claude_client, "coordinated_reply", return_value=reply):
                result = session.ask("帮我定个闹钟")
            self.assertIn(expected, result["answer"])
        self.assertEqual(local_actions.reminders(self.config), [])

    def test_receipt_overrides_wrong_model_time_and_survives_api_failure(self):
        for api_failure in (False, True):
            with self.subTest(api_failure=api_failure):
                session = self.session()
                def brain(*args, **kwargs):
                    content, failed = kwargs["tool_executor"](
                        "set_reminder", dict(text="起床", in_minutes=20))
                    self.assertFalse(failed)
                    self.receipt = json.loads(content)
                    if api_failure:
                        raise claude_client.ClaudeUnavailable("network")
                    return "已设置为晚上九点"
                with mock.patch.object(claude_client, "coordinated_reply", side_effect=brain):
                    result = session.ask("二十分钟后提醒我起床")
                self.assertIn(self.receipt["due_local"], result["answer"])
                self.assertNotIn("晚上九点", result["answer"])
                self.assertIn("set_reminder", result["memory_note"])
                self.assertFalse(result.get("unavailable", False))

    def test_failed_or_declined_tool_overrides_false_success(self):
        session = self.session()
        session.confirm_action = lambda _: False
        local_actions.save_settings(self.config, local_actions.ActionSettings(confirm="always"))
        def brain(*args, **kwargs):
            content, failed = kwargs["tool_executor"]("set_reminder", dict(text="起床", in_minutes=20))
            self.assertTrue(failed)
            return "好的已设好"
        with mock.patch.object(claude_client, "coordinated_reply", side_effect=brain):
            result = session.ask("二十分钟后提醒我起床")
        self.assertIn('The user did not consent', result["answer"])
        self.assertNotIn("已设好", result["answer"])
        self.assertEqual(local_actions.reminders(self.config), [])

    def test_alarm_failure_question_gets_actual_state_not_memory_only(self):
        self.set_alarm()
        local_actions.due_reminders(self.config, NOW + timedelta(days=1))
        session = self.session()
        def brain(*args, **kwargs):
            self.assertIn('"status": "triggered"', kwargs["system_dynamic"])
            self.assertIn('"tool": "set_reminder"', kwargs["system_dynamic"])
            self.assertIn("NOT proof", args[0])
            return "已保存并派发，但没有声音播放记录，原因还无法确认。"
        with mock.patch.object(session, "_rag", side_effect=AssertionError("must use real state")), \
                mock.patch.object(claude_client, "coordinated_reply", side_effect=brain):
            result = session.ask("你昨天有没有帮我定闹钟，为什么没叫我起床？")
        self.assertIn("没有声音播放记录", result["answer"])

    def test_missing_legacy_pending_does_not_erase_creation_audit(self):
        self.set_alarm()
        (self.config / local_actions.REMINDERS_NAME).write_text("[]", encoding="utf-8")
        result = json.loads(local_actions._list_reminders({}, self.context)[0])
        self.assertEqual(result["reminders"], [])
        self.assertEqual(result["history"], [])
        self.assertTrue(result["creation_audit"])
        self.assertIn('absence does not prove one was never set', result["note"])

    def test_retrospective_receipt_is_not_treated_as_a_new_alarm_request(self):
        text = "当时已设好，保存记录中有成功结果，但没有音频记录，无法确定没听见的原因。"
        self.assertEqual(schedule_receipts.finish("昨天帮我定闹钟了吗？", text, [], True), text)

    def window(self):
        win = myn_gui.MYNWindow.__new__(myn_gui.MYNWindow)
        win.session = mock.Mock(db_path=self.config / "jarvis.db")
        win.events = queue.Queue()
        win.root = mock.Mock()
        win.append = mock.Mock()
        win.speak = mock.Mock()
        win.tts_request_id = 10
        win._note_task = mock.Mock()
        win.task_queue = ()
        win.busy = True
        return win

    def test_sleeping_busy_window_alarm_bypasses_waiting_ask_without_beeping(self):
        win = self.window()
        interaction.observe(self.config, "我要睡觉了")
        queued = dict(kind="ask", id="weather", text="天气")
        win.task_queue = (queued,)
        import alarm_ringing
        alarm_ringing.activate(self.config, "schedule:wake", "test", "八点了，起床")
        with mock.patch("gui_timers.threading.Thread") as worker:
            win._queue_task(dict(kind="say", id="wake", text="八点了，起床"))
            worker.assert_not_called()  # no independent beep worker
        win.speak.assert_called_once_with("八点了，起床", purpose="alarm")
        self.assertEqual(win.task_queue, (queued,))
        self.assertEqual(interaction.load(self.config)["mode"], "sleep")
        statuses = [r["status"] for r in alarm_delivery.recent(self.config)]
        self.assertIn("tts_requested", statuses)
        self.assertNotIn("local_tone_sent", statuses)
        self.assertNotIn("已说", str(win._note_task.call_args))

    def test_voice_failure_is_recorded_without_substituting_a_beep(self):
        win = self.window()
        win.speak.side_effect = RuntimeError("cloud unavailable")
        with mock.patch("gui_timers.threading.Thread") as worker:
            win._deliver_alarm("起床", "reminder", "test")
            worker.assert_not_called()
        self.assertIn("tts_failed", [r["status"] for r in alarm_delivery.recent(self.config)])

    def test_voice_only_reminder_keeps_text_and_updates_animation(self):
        win = self.window()
        win.web_active = True
        win.web = mock.Mock()
        win._deliver_alarm("起床", "reminder", "test")
        win.web.js.assert_any_call({"cmd": "timer_finished", "text": "起床"})
        win.append.assert_any_call("MYN", "起床", speakable=True)

    def test_late_audio_error_is_attributed_to_its_alarm_not_next_reply(self):
        win = self.window()
        win.alarm_delivery_request = (10, self.config, "alarm1")
        win._alarm_audio_result(9, "tts_failed", "old")
        self.assertEqual(alarm_delivery.recent(self.config), [])
        win._alarm_audio_result(10, "tts_failed", "API denied")
        self.assertEqual(alarm_delivery.recent(self.config)[0]["detail"], "API denied")
        self.assertIsNone(win.alarm_delivery_request)


if __name__ == "__main__":
    unittest.main()
