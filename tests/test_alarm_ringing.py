"""Persistent ringing, current-user acknowledgement and replay without real audio."""
import json
import queue
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src/core")]
import alarm_ringing
import chat
import claude_client
import interaction
import local_actions
import timekeeper
import timer_display
from gui_timers import TimersMixin


class Ringing(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="myn-ringing-")
        self.addCleanup(folder.cleanup)
        self.config = Path(folder.name) / "config"
        self.config.mkdir()
        self.database = self.config.parent / "database" / "jarvis.db"
        self.database.parent.mkdir()

    def ring(self, key="schedule:wake", occurrence="2026-10-21 08:00"):
        return alarm_ringing.activate(self.config, key, occurrence, "早上八点，该起床了")

    def session(self):
        session = chat.ChatSession(self.database)
        session.interaction_notify = mock.Mock()
        return session

    def window(self):
        win = TimersMixin()
        win.session = SimpleNamespace(db_path=self.database)
        win.events = queue.Queue()
        win.busy = False
        win.tts_request_id = 5
        win._speaking = mock.Mock(return_value=False)
        win.append = mock.Mock()
        win.speak = mock.Mock()
        win.stop_speech = mock.Mock()
        win._timers_changed = mock.Mock()
        win.web_active = False
        return win

    def test_persists_and_deduplicates_even_if_schedule_claim_is_retried(self):
        first = self.ring()
        self.assertEqual(self.ring()["id"], first["id"])
        self.assertEqual(len(alarm_ringing.active(Path(str(self.config)))), 1)
        alarm_ringing.dismiss(self.config, [first["id"]])
        self.assertIsNone(self.ring())  # old due-task replay cannot resurrect a dismissal
        self.assertNotEqual(self.ring(occurrence="2026-10-22 08:00")["id"], first["id"])

    def test_due_state_persisted_before_claim_and_future_repeat_remains(self):
        task = timekeeper.add_task(self.config, now=datetime(2026, 10, 20, 20),
                                   category="alarm", kind="say", repeat="daily",
                                   time="08:00", text="起床")
        with mock.patch.object(timekeeper, "_write", side_effect=OSError("interrupted claim")):
            with self.assertRaises(OSError):
                timekeeper.due_tasks(self.config, datetime(2026, 10, 21, 8))
        self.assertEqual(len(alarm_ringing.active(self.config)), 1)
        timekeeper.due_tasks(self.config, datetime(2026, 10, 21, 8))
        self.assertEqual(len(alarm_ringing.active(self.config)), 1)
        session = self.session()
        with mock.patch.object(session, "_key", return_value=""), \
                mock.patch.object(claude_client, "coordinated_reply") as model:
            result = session.ask("我已经起床了")
        self.assertEqual(result["answer"], alarm_ringing.ACK)
        model.assert_not_called()
        self.assertFalse(alarm_ringing.active(self.config))
        saved = timekeeper.get_task(self.config, task["id"])
        self.assertTrue(saved["enabled"])
        self.assertEqual(saved["next_run"], "2026-10-22 08:00")

    def test_semantic_natural_ack_context_and_supplementary_question(self):
        self.ring()
        session = self.session()
        question = "我都洗漱完准备出门了，不用催我啦。今天星期几？"
        verdict = dict(mode="idle", scope="current", certainty="clear", evidence="洗漱完准备出门",
                       reason="明确已经起床", alarm=dict(action="dismiss", scope="current",
                       certainty="clear", evidence="不用催我啦", reason="明确不再需要叫醒"))
        with mock.patch.object(session, "_key", return_value="synthetic"), \
                mock.patch.object(claude_client, "send", return_value=json.dumps(verdict)) as classifier, \
                mock.patch.object(claude_client, "coordinated_reply", return_value="今天是星期二。"):
            result = session.ask(question)
        prompt = json.loads(classifier.call_args.args[2])
        self.assertTrue(prompt["reading_panel"]["ringing_alarms"])
        self.assertFalse(alarm_ringing.active(self.config))
        self.assertIn(alarm_ringing.ACK, result["answer"])
        self.assertIn("星期二", result["answer"])
        session.interaction_notify.assert_called()

    def test_quotes_negation_ambiguity_stale_turn_and_stale_occurrence_cannot_dismiss(self):
        first = self.ring()
        snapshot = alarm_ringing.active(self.config)
        state = interaction.observe(self.config, "我还没起床")
        base = dict(action="dismiss", scope="current", certainty="clear",
                    evidence="起床", reason="test")
        for decision in (dict(base, action="keep"), dict(base, certainty="uncertain"),
                         dict(base, scope="keep"), dict(base, evidence="我已起床"), {}):
            self.assertFalse(alarm_ringing.acknowledge(
                self.config, snapshot, decision, "我还没起床", state["revision"]))
        for utterance in ("我还没起床", "不要关闭闹钟", "昨天我说关闭闹钟", "他说我已经起床了",
                          "“我已经起床了”是什么意思", "我已经起床了吗", "知道了", "嗯"):
            self.assertFalse(alarm_ringing.explicit_ack(utterance))
        newer = interaction.observe(self.config, "别关闭")
        self.assertFalse(alarm_ringing.acknowledge(self.config, snapshot, base, "起床", state["revision"]))
        alarm_ringing.dismiss(self.config, [first["id"]])
        second = self.ring(occurrence="next")
        self.assertFalse(alarm_ringing.acknowledge(self.config, snapshot, base, "起床", newer["revision"]))
        self.assertEqual(alarm_ringing.active(self.config)[0]["id"], second["id"])

    def test_unrelated_reply_or_classifier_failure_keeps_ringing(self):
        self.ring()
        session = self.session()
        for response in (dict(mode="idle", scope="current", certainty="clear",
                              evidence="今天", reason="闲聊", alarm=dict(action="keep")),
                         ValueError("offline")):
            with mock.patch.object(session, "_key", return_value="synthetic"), \
                    mock.patch.object(interaction, "classify",
                                      **({"side_effect": response} if isinstance(response, Exception)
                                         else {"return_value": response})), \
                    mock.patch.object(claude_client, "coordinated_reply", return_value="你好"):
                session.ask("今天天气如何")
            self.assertTrue(alarm_ringing.active(self.config))

    def test_repeat_waits_for_gap_and_voice_then_ack_cancels_queued_audio(self):
        row = self.ring()
        win = self.window()
        with mock.patch("gui_timers.time.time", return_value=1000), \
                mock.patch("alarm_ringing.time.time", return_value=1000):
            win._repeat_ringing()
            win.speak.assert_called_once()
            self.assertEqual(alarm_ringing.active(self.config)[0]["last_attempt"], 1000)
            win._alarm_audio_result(5, "tts_submitted")
        with mock.patch("gui_timers.time.time", return_value=1029):
            win._repeat_ringing()
            self.assertEqual(win.speak.call_count, 1)
        with mock.patch("gui_timers.time.time", return_value=1031):
            win._speaking.return_value = True
            win._repeat_ringing()
            self.assertEqual(win.speak.call_count, 1)
        win._speaking.return_value = False
        with mock.patch("gui_timers.time.time", return_value=1062), \
                mock.patch("alarm_ringing.time.time", return_value=1062):
            win._repeat_ringing()
            self.assertEqual(win.speak.call_count, 2)
        self.assertFalse(win.append.called)  # repeats do not flood conversation
        alarm_ringing.dismiss(self.config, [row["id"]])
        win._repeat_ringing()
        win.stop_speech.assert_called_once()
        self.assertEqual(win.speak.call_count, 2)

    def test_failure_retry_and_restart_and_nonalarm_reminders_do_not_repeat(self):
        self.ring()
        win = self.window()
        win.speak.side_effect = RuntimeError("no audio")
        win._repeat_ringing()
        self.assertTrue(alarm_ringing.active(self.config))
        fresh = self.window()
        alarm_ringing.mark_attempt(self.config, alarm_ringing.active(self.config)[0]["id"], now=0)
        fresh._repeat_ringing()
        fresh.speak.assert_called_once()
        alarm_ringing.dismiss(self.config, [r["id"] for r in alarm_ringing.active(self.config)])
        fresh._sync_ringing()
        fresh._deliver_alarm("喝水", "reminder", "water")
        self.assertFalse(alarm_ringing.active(self.config))

    def test_active_display_and_explicit_delete_remove_ringing_but_not_other_alarms(self):
        task = timekeeper.add_task(self.config, now=datetime(2026, 10, 20, 20),
                                   category="alarm", kind="say", repeat="daily", time="08:00", text="起床")
        timekeeper.due_tasks(self.config, datetime(2026, 10, 21, 8))
        self.ring("schedule:other")
        view = timer_display.snapshot(
            self.config, now=datetime(2026, 10, 21, 8, tzinfo=timekeeper.TOKYO).timestamp())
        self.assertTrue(view["alarm_upcoming"])
        self.assertEqual(view["count"], 0)
        self.assertNotIn("alarms", view)
        self.assertEqual(len(alarm_ringing.active(self.config)), 2)
        context = local_actions.ActionContext(self.config, confirm=lambda _: True)
        content, failed = local_actions.execute("delete_alarm", {"id": task["id"], "permanent": True}, context)
        self.assertFalse(failed, content)
        self.assertEqual([r["source_key"] for r in alarm_ringing.active(self.config)], ["schedule:other"])

    def test_corrupt_store_does_not_get_overwritten_or_claim_schedule(self):
        task = timekeeper.add_task(self.config, now=datetime(2026, 10, 20, 20),
                                   category="alarm", kind="say", repeat="once",
                                   time="08:00", date="2026-10-21", text="起床")
        path = self.config / alarm_ringing.FILE
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            timekeeper.due_tasks(self.config, datetime(2026, 10, 21, 8))
        self.assertEqual(path.read_text(encoding="utf-8"), "{broken")
        self.assertTrue(timekeeper.get_task(self.config, task["id"])["enabled"])

    def completed(self):
        state = interaction.observe(self.config, "我已经起床了")
        return alarm_ringing.acknowledge(self.config, alarm_ringing.active(self.config),
            dict(action="dismiss", scope="current", certainty="clear",
                 evidence="我已经起床了", reason="明确起床"),
            "我已经起床了", state["revision"])

    def test_once_deleted_only_after_ack_repeat_and_unfired_once_kept(self):
        fields = dict(category="alarm", kind="say", time="08:00", text="起床")
        once = timekeeper.add_task(self.config, now=datetime(2026, 10, 20, 20),
            repeat="once", date="2026-10-21", **fields)
        repeat = timekeeper.add_task(self.config, now=datetime(2026, 10, 20, 20),
            repeat="weekdays", **fields)
        future = timekeeper.add_task(self.config, now=datetime(2026, 10, 20, 20),
            repeat="once", date="2026-10-22", **fields)
        timekeeper.due_tasks(self.config, datetime(2026, 10, 21, 8))
        self.assertEqual(len(timekeeper.tasks(self.config)), 3)
        self.assertEqual(len(alarm_ringing.active(self.config)), 2)
        self.completed()
        self.assertEqual({t["id"] for t in timekeeper.tasks(self.config)}, {repeat["id"], future["id"]})
        self.assertEqual(alarm_ringing.active(self.config), [])
        self.assertIn("schedule:" + once["id"], alarm_ringing._load(self.config)["seen"])

    def test_cleanup_failure_retries_after_restart_without_ringing_again(self):
        task = timekeeper.add_task(self.config, now=datetime(2026, 10, 20, 20),
            category="alarm", kind="say", repeat="once", date="2026-10-21", time="08:00", text="起床")
        timekeeper.due_tasks(self.config, datetime(2026, 10, 21, 8))
        with mock.patch.object(timekeeper, "_write", side_effect=OSError("disk busy")):
            self.assertTrue(self.completed())
        self.assertFalse(alarm_ringing.active(self.config))
        self.assertTrue(timekeeper.tasks(self.config))
        self.assertEqual(alarm_ringing.cleanup_completed(Path(str(self.config))), 1)
        self.assertEqual(timekeeper.tasks(self.config), [])
        self.assertIsNone(alarm_ringing.activate(self.config, "schedule:" + task["id"],
                                                "2026-10-21 08:00", "起床"))

    def test_rescheduled_once_and_paused_alarm_not_deleted_legacy_done_removed(self):
        task = timekeeper.add_task(self.config, now=datetime(2026, 10, 20, 20),
            category="alarm", kind="say", repeat="once", date="2026-10-21", time="08:00", text="起床")
        timekeeper.due_tasks(self.config, datetime(2026, 10, 21, 8))
        timekeeper.update_task(self.config, task["id"], now=datetime(2026, 10, 21, 8, 1),
                              date="2026-10-22", enabled=True)
        self.completed()
        self.assertEqual(timekeeper.get_task(self.config, task["id"])["date"], "2026-10-22")
        local_actions._save_reminders(self.config, [
            dict(id="legacy", text="起床", due_utc="2026-10-20T23:00:00+00:00"),
            dict(id="water", text="喝水", due_utc="2026-10-20T23:00:00+00:00")])
        local_actions.due_reminders(self.config,
                                   datetime(2026, 10, 21, 8, tzinfo=timekeeper.TOKYO))
        self.completed()
        self.assertEqual([r["id"] for r in local_actions._reminder_records(self.config)], ["water"])
        # A pause/dismiss operation is not a completion acknowledgement.
        row = self.ring("schedule:" + task["id"], "2026-10-22 08:00")
        alarm_ringing.dismiss(self.config, [row["id"]])
        alarm_ringing.cleanup_completed(self.config)
        self.assertTrue(timekeeper.get_task(self.config, task["id"]))


if __name__ == "__main__":
    unittest.main()
