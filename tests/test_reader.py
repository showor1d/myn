"""The reader panel (2026-10-17): voice phrases that open and close it, and the page
pieces it needs. The panel itself is checked in Chromium (drawing, circling, layout)."""

import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src" / "core")]

import stt


class Reader(unittest.TestCase):
    def test_voice(self):
        for text in ("关掉阅读框", "收起全文", "把这个框关掉", "把全文收起来"):
            self.assertEqual(stt.voice_command(text), "reader_close", text)
        for text in ("打开全文", "展开全文", "打开阅读框"):
            self.assertEqual(stt.voice_command(text), "reader_open", text)
        for text in ("关掉", "打开日程", "把这段翻译一下"):
            self.assertNotEqual(stt.voice_command(text), "reader_close", text)

    def test_page_has_the_panel(self):
        page = (BASE / "src" / "web" / "index.html").read_text(encoding="utf-8")
        script = (BASE / "src" / "web" / "app.js").read_text(encoding="utf-8")
        for piece in ('id="reader"', 'data-tool="pen"', 'data-tool="marker"', 'data-tool="lasso"',
                      'data-tool="eraser"', 'id="ask"'):
            self.assertIn(piece, page)
        for piece in ("function circledText", "function renderMarkdown", '"ask_about"', '"selection_used"'):
            self.assertIn(piece, script)
        self.assertNotRegex(script, r"innerHTML\s*[+]?=")  # replies are put in as text, never as HTML


if __name__ == "__main__":
    unittest.main()
