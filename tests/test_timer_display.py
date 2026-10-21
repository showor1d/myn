"""Main-view timing state follows persisted timers; reading must never trigger them."""
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src/core")]
import local_actions
import timekeeper
import timer_display
import stt
import web_panels


class Display(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="myn-timer-display-")
        self.addCleanup(temp.cleanup)
        self.config = Path(temp.name)

    def test_countdown_pause_resume_add_cancel_and_restart(self):
        item = timekeeper.start_timer(self.config, seconds=90, now=1000, label="泡面")
        view = timer_display.snapshot(self.config, now=1030)["items"][0]
        self.assertEqual((view["remaining"], view["total"], view["paused"]), (60, 90, False))
        timekeeper.control_timer(self.config, "pause", item["id"], now=1030)
        self.assertEqual(timer_display.snapshot(self.config, now=5000)["items"][0]["remaining"], 60)
        timekeeper.control_timer(self.config, "resume", item["id"], now=5000)
        timekeeper.control_timer(self.config, "add", item["id"], seconds=30, now=5000)
        view = timer_display.snapshot(Path(str(self.config)), now=5010)["items"][0]
        self.assertEqual((view["remaining"], view["total"], view["paused"]), (80, 120, False))
        timekeeper.control_timer(self.config, "cancel", item["id"], now=5010)
        self.assertEqual(timer_display.snapshot(self.config)["items"], [])

    def test_due_visual_does_not_fire_or_delete_timer(self):
        timekeeper.start_timer(self.config, seconds=5, now=1000)
        self.assertEqual(timer_display.snapshot(self.config, now=1006)["items"][0]["remaining"], 0)
        self.assertEqual(len(timekeeper.timers(self.config, now=1006)), 1)
        self.assertEqual(len(timekeeper.due_timers(self.config, now=1006)), 1)
        self.assertEqual(timer_display.snapshot(self.config, now=1006)["items"], [])

    def test_nearest_three_and_total_count_with_paused_last(self):
        for sec in (90, 30, 120, 60):
            timekeeper.start_timer(self.config, seconds=sec, now=1000)
        paused = timekeeper.start_timer(self.config, seconds=10, now=1000)
        timekeeper.control_timer(self.config, "pause", paused["id"], now=1000)
        result = timer_display.snapshot(self.config, now=1000)
        self.assertEqual(result["count"], 5)
        self.assertEqual([i["remaining"] for i in result["items"]], [30, 60, 90])

    def test_reminders_and_repeating_tasks_survive_reload_and_hidden_when_inactive(self):
        now = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
        context = local_actions.ActionContext(self.config, now=lambda: now)
        content, failed = local_actions.execute(
            "set_reminder", dict(text="早上起床", at="2026-10-21 08:00"), context)
        self.assertFalse(failed, content)
        task = timekeeper.add_task(self.config, now=now.astimezone(timekeeper.TOKYO).replace(tzinfo=None),
                                  kind="say", text="喝水", repeat="daily", time="22:00")
        items = timer_display.snapshot(self.config, now=now.timestamp())["items"]
        self.assertEqual([i["remaining"] for i in items], [3600, 39600])
        self.assertEqual(items[1]["when"], '10/21 08:00')
        timekeeper.update_task(self.config, task["id"], enabled=False,
                              now=now.astimezone(timekeeper.TOKYO).replace(tzinfo=None))
        self.assertEqual(timer_display.snapshot(self.config, now=now.timestamp())["count"], 1)

    def test_settings_page_removed_but_chat_tools_remain(self):
        self.assertNotIn("timers", web_panels.PANELS)
        self.assertFalse((BASE / "src/timers_window.py").exists())
        tools = local_actions.tools_for(local_actions.ActionSettings())
        self.assertTrue({"start_timer", "timer_control", "set_reminder", "add_schedule",
                         "update_schedule", "delete_schedule"} <= {t["name"] for t in tools})
        self.assertNotIn('data-item="timers"', (BASE / "src/web/index.html").read_text(encoding="utf-8"))

    def test_alarm_icon_tracks_next_24_hours_without_details_or_mutation(self):
        now = datetime(2026, 10, 20, 8)
        epoch = now.replace(tzinfo=timekeeper.TOKYO).timestamp()
        self.assertFalse(timer_display.snapshot(self.config, now=epoch)["alarm_upcoming"])
        task = timekeeper.add_task(self.config, now=now, category="alarm", kind="say",
                                  repeat="once", date="2026-10-21", time="08:00", text="私人内容")
        for at, expected in ((epoch - 1, False), (epoch, True),
                             (epoch + 86400, True), (epoch + 86401, False)):
            with self.subTest(at=at):
                view = timer_display.snapshot(self.config, now=at)
                self.assertEqual(view, dict(cmd="timers", items=[], count=0, alarm_upcoming=expected))
        self.assertTrue(timekeeper.get_task(self.config, task["id"])["enabled"])
        timekeeper.update_task(self.config, task["id"], enabled=False, now=now)
        self.assertFalse(timer_display.snapshot(self.config, now=epoch)["alarm_upcoming"])
        local_actions._save_reminders(self.config, [
            dict(id="legacy", text="起床", due_utc="2026-10-20T23:00:00+00:00")])
        self.assertFalse(timer_display.snapshot(self.config, now=epoch - 1)["alarm_upcoming"])
        self.assertTrue(timer_display.snapshot(self.config, now=epoch)["alarm_upcoming"])
        self.assertFalse(timer_display.snapshot(self.config, now=epoch + 86401)["alarm_upcoming"])
        self.assertEqual(len(local_actions.reminders(self.config)), 1)


if __name__ == "__main__":
    unittest.main()
