"""The GPU window's client: availability switch and the JSON-lines protocol."""
import json
import os
import queue
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "core")]

import web_frontend  # noqa: E402
import web_window  # noqa: E402


class WebFrontendChecks(unittest.TestCase):
    def test_classic_window_can_be_forced(self):
        with mock.patch.dict(os.environ, {"MYN_UI": "tk"}):
            self.assertFalse(web_frontend.available())

    def test_messages_flow_both_ways(self):
        folder = Path(tempfile.mkdtemp())
        log = folder / "in.jsonl"
        fake = folder / "fake.py"
        fake.write_text(textwrap.dedent("""
            import json, sys
            print(json.dumps({"event": "ready"}), flush=True)
            with open(sys.argv[1], "w", encoding="utf-8") as log:
                for line in sys.stdin:
                    log.write(line); log.flush()
                    if json.loads(line).get("window") == "close":
                        break
        """), encoding="utf-8")
        events = queue.Queue()
        front = web_frontend.WebFrontend(events, command=[sys.executable, str(fake), str(log)])
        self.assertEqual(events.get(timeout=10), ("web", {"event": "ready"}))
        front.js({"cmd": "voice", "text": "你好"})
        front.close()
        self.assertEqual(events.get(timeout=10), ("web", {"event": "closed"}))
        lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(lines, [{"js": {"cmd": "voice", "text": "你好"}}, {"window": "close"}])
        front.js({"cmd": "voice", "text": "ignored after close"})  # no error

    def test_window_pipes_carry_any_text_intact(self):
        # A Chinese-locale Windows child defaults to GBK pipes; the window forces UTF-8.
        script = ("import json, sys; sys.path.insert(0, %r); import web_window; web_window.utf8_pipes(); "
                  "line = sys.stdin.readline(); web_window.out({'event': 'echo', 'text': json.loads(line)['text']})"
                  % str(ROOT / "src"))
        text = "晚上好！东京今晚晴，17°C。ちょっと待って ✓ — “引号” 😀"
        env = dict(os.environ, PYTHONIOENCODING="gbk")
        result = subprocess.run([sys.executable, "-c", script], input=json.dumps({"text": text}, ensure_ascii=False) + "\n",
                                capture_output=True, encoding="utf-8", env=env, timeout=30)
        self.assertEqual(json.loads(result.stdout), {"event": "echo", "text": text}, result.stderr)

    def test_window_process_gets_utf8_environment(self):
        events = queue.Queue()
        script = "import os, json; print(json.dumps({'event': 'env', 'io': os.environ.get('PYTHONIOENCODING')}), flush=True)"
        front = web_frontend.WebFrontend(events, command=[sys.executable, "-c", script])
        self.assertEqual(events.get(timeout=10), ("web", {"event": "env", "io": "utf-8"}))
        front.close()

    def test_page_and_window_script_exist(self):
        self.assertTrue(web_window.PAGE.is_file())
        page = web_window.PAGE.read_text(encoding="utf-8")
        for needed in ('id="scene"', 'id="chat"', 'id="bar"', "app.js", "pywebview-drag-region"):
            self.assertIn(needed, page)

    def test_window_api_only_forwards_events(self):
        sent = []
        with mock.patch.object(web_window, "out", sent.append):
            web_window.Api().emit({"event": "send", "text": "hi"})
            web_window.Api().emit({"no": "event"})
            web_window.Api().emit("text")
        self.assertEqual(sent, [{"event": "send", "text": "hi"}])


if __name__ == "__main__":
    unittest.main()
