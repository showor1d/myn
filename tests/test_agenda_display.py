"""Calendar tool -> saved data -> main view, using only temporary personal data."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src/core"), str(BASE / "tests")]
import agenda
import agenda_display
import chat
import claude_client
import local_actions
import stt
import web_panels
from test_web_tools import reply


class Display(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="myn-agenda-display-")
        self.addCleanup(folder.cleanup)
        self.config = Path(folder.name)

    def test_week_days_empty_days_recurrence_and_no_writes(self):
        agenda.add(self.config, title="运动", start="2026-10-05 19:00", repeat="weekly")
        agenda.add(self.config, title="生日", start="2026-10-28")
        before = {p.name: p.read_bytes() for p in self.config.iterdir()}
        view = agenda_display.snapshot(self.config, "2026-10-26", "2026-11-01")
        self.assertEqual([d["weekday"] for d in view["days"]], ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
        self.assertEqual(view["days"][0]["events"][0]["title"], "运动")
        self.assertEqual(view["days"][2]["events"][0]["time"], 'All day')
        self.assertEqual(view["days"][1]["events"], [])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.config.iterdir()})

    def test_one_day_overnight_and_exclusive_end(self):
        agenda.add(self.config, title="跨夜", start="2026-10-27 23:00", end="2026-10-28 02:00")
        agenda.add(self.config, title="前一天结束", start="2026-10-27 22:00", end="2026-10-28 00:00")
        agenda.add(self.config, title="次日", start="2026-10-29 00:00")
        view = agenda_display.snapshot(self.config, "2026-10-28", "2026-10-28")
        self.assertEqual(len(view["days"]), 1)
        self.assertEqual([e["title"] for e in view["days"][0]["events"]], ["跨夜"])
        self.assertEqual(view["days"][0]["events"][0]["time"], 'Continued from previous day – 02:00')

    def test_all_events_and_updates_visible(self):
        for i in range(65):
            agenda.add(self.config, title="安排%d" % i, start="2026-10-28")
        event = agenda.events(self.config)[0]
        agenda.update(self.config, event["id"], title="更新后的安排")
        view = agenda_display.snapshot(self.config, "2026-10-28", "2026-10-28")
        self.assertEqual(len(view["days"][0]["events"]), 65)
        self.assertIn("更新后的安排", [e["title"] for e in view["days"][0]["events"]])
        agenda.delete(self.config, event["id"])
        self.assertEqual(len(agenda_display.snapshot(self.config, "2026-10-28", "2026-10-28")["days"][0]["events"]), 64)

    def test_tool_callback_internal_lookup_and_error(self):
        received = []
        ctx = local_actions.ActionContext(self.config, show_agenda=received.append,
                                         now=lambda: datetime(2026, 10, 20, tzinfo=timezone.utc))
        args = {"from_date": "2026-10-28", "to_date": "2026-10-28"}
        content, error = local_actions.execute("list_events", args, ctx)
        self.assertFalse(error)
        self.assertEqual(len(received[0]["days"]), 1)
        self.assertEqual(json.loads(content)["count"], 0)
        local_actions.execute("list_events", dict(args, display=False), ctx)
        self.assertEqual(len(received), 1)
        (self.config / agenda.FILE).write_text("{broken", encoding="utf-8")
        self.assertTrue(local_actions.execute("list_events", args, ctx)[1])
        self.assertEqual(len(received), 1)  # No fabricated empty agenda on read failure.

    def test_menu_page_removed_and_tools_retained(self):
        self.assertNotIn("agenda", web_panels.PANELS)
        self.assertFalse((BASE / "src/agenda_window.py").exists())
        self.assertNotIn('data-item="agenda"', (BASE / "src/web/index.html").read_text(encoding="utf-8"))
        names = {t["name"] for t in local_actions.TOOLS["calendar"]}
        self.assertEqual(names, {"add_event", "list_events", "update_event", "delete_event"})
        self.assertNotEqual(stt.voice_command("打开日程"), "agenda")

    def test_chat_query_routes_real_calendar_to_display(self):
        db = self.config / "data/jarvis.db"
        db.parent.mkdir()
        session = chat.ChatSession(db)
        agenda.add(db.parent, title="答辩", start="2026-10-28 10:00")
        received, requests = [], []
        session.show_agenda = received.append
        def opener(request, timeout):
            requests.append(json.loads(request.data.decode("utf-8")))
            if len(requests) == 1:
                return reply([{"type": "tool_use", "id": "toolu_a", "name": "list_events",
                               "input": {"from_date": "2026-10-28", "to_date": "2026-10-28"}}], stop="tool_use")
            return reply([{"type": "text", "text": "当天上午十点答辩。"}])
        with mock.patch.object(session, "_key", return_value="k"), \
                mock.patch.object(claude_client.urllib.request, "urlopen", side_effect=opener):
            result = session.ask("10月28日我有什么安排？")
        self.assertEqual(result["answer"], "当天上午十点答辩。")
        self.assertEqual(received[0]["days"][0]["events"][0]["title"], "答辩")
        self.assertEqual(len(received[0]["days"]), 1)


if __name__ == "__main__":
    unittest.main()

