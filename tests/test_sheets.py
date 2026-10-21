'Settings are rendered inside the page, including confirmation prompts, usage entries and folder browsing.'

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src" / "core")]

import sheets


class Echo(sheets.Panel):
    key = "echo"
    title = "Echo"

    def body(self):
        return [{"t": "p", "text": "n=%s" % self.v("n", 0)}]

    def act(self, action, values):
        if action == "boom":
            raise RuntimeError("坏了")
        self.redraw()


class Host(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.host = sheets.SheetHost(self.sent.append, lambda ms, fn: fn())

    def test_open_act_close(self):
        panel = self.host.open(Echo(None))
        self.assertEqual(self.sent[-1]["cmd"], "sheet")
        self.host.event({"event": "sheet", "key": "echo", "action": "x", "values": {"n": 3}})
        self.assertEqual(self.sent[-1]["spec"]["body"][0]["text"], "n=3")  # what you entered is kept
        self.host.event({"event": "sheet", "key": "echo", "action": "boom", "values": {}})
        self.assertEqual(self.sent[-1], {"cmd": "sheet_toast", "text": 'An error occurred: 坏了', "tone": "bad"})
        self.assertTrue(self.host.is_open("echo"))
        self.host.event({"event": "sheet_closed", "key": "echo"})
        self.assertFalse(self.host.is_open())
        self.assertIs(panel.host, self.host)

    def test_questions_are_asked_in_the_page(self):
        answers = []
        self.host.confirm("删除？", answers.append, danger=True)
        ask = self.sent[-1]
        self.assertEqual((ask["cmd"], ask["text"], ask["danger"]), ("sheet_confirm", "删除？", True))
        self.host.event({"event": "sheet_answer", "id": ask["id"], "yes": True})
        self.assertEqual(answers, [True])

    def test_waiting_for_an_answer_from_another_thread(self):
        result = []
        worker = threading.Thread(target=lambda: result.append(self.host.ask_and_wait("关机？", timeout=5)))
        worker.start()
        for _ in range(100):
            if self.sent and self.sent[-1]["cmd"] == "sheet_confirm":
                break
            threading.Event().wait(0.01)
        self.host.event({"event": "sheet_answer", "id": self.sent[-1]["id"], "yes": False})
        worker.join(5)
        self.assertEqual(result, [False])


class Screens(unittest.TestCase):
    def test_usage_receipt_is_read_only_and_keeps_existing_records(self):
        import web_panels3
        import usage_stats
        from datetime import datetime
        with tempfile.TemporaryDirectory() as folder:
            usage_stats.add_manual(Path(folder), datetime.now().astimezone(),
                                   "Existing", 694000, 2.5)
            win = mock.Mock()
            win._usage_config.return_value = Path(folder)
            sent = []
            host = sheets.SheetHost(sent.append)
            host.open(web_panels3.UsagePanel(win))
            spec = sent[-1]["spec"]
            kinds = [node["t"] for node in spec["body"]]
            self.assertIn("usage_receipt", kinds)
            self.assertNotIn("details", kinds)
            receipt = next(n for n in spec["body"] if n["t"] == "usage_receipt")
            self.assertEqual((receipt["tokens"], receipt["cost"]), ("694,000", "$2.50"))
            host.event({"event": "sheet", "key": "usage", "action": "manual", "values": {}})
            self.assertNotEqual(sent[-1]["cmd"], "sheet_modal")
            host.event({"event": "sheet", "key": "usage", "from": "modal", "action": "m_add",
                        "values": {"m_day": "2026-10-15", "m_label": "ChatGPT", "m_tokens": "69.4w", "m_cost": ""}})
            entries = usage_stats.manual_entries(Path(folder))
            self.assertEqual(len(entries), 1)
            self.assertEqual((entries[0]["label"], entries[0]["tokens"], entries[0]["cost_usd"]),
                             ("Existing", 694000, 2.5))

    def test_folder_browser(self):
        import web_panels2
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "b").mkdir()
            (Path(folder) / "a").mkdir()
            (Path(folder) / ".hidden").mkdir()
            (Path(folder) / "file.txt").write_text("x")
            self.assertEqual(web_panels2._subfolders(folder), ["a", "b"])
            self.assertIsNone(web_panels2._subfolders(str(Path(folder) / "missing")))
            self.assertTrue(web_panels2._places())


if __name__ == "__main__":
    unittest.main()
