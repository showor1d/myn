'Settings panels stay inside the main window, fit and center correctly, close with Esc and scroll rather than clipping content.'

import sys
import time
import tkinter as tk
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src" / "core")]

try:
    ROOT = tk.Tk()
    ROOT.withdraw()
except tk.TclError:  # no display
    ROOT = None

import theme


@unittest.skipIf(ROOT is None, "needs a display")
class Panels(unittest.TestCase):
    def setUp(self):
        theme.apply(ROOT, "TkDefaultFont")
        theme.enable_frameless("TkDefaultFont")
        self.counts = []
        self.host = (200, 100, 1040, 680)
        theme.set_host(ROOT, lambda: self.host, None, self.counts.append)

    def tearDown(self):
        for window in theme.panels():
            window.destroy()
        ROOT.update()

    @classmethod
    def tearDownClass(cls):  # leave Tk as other tests expect it
        tk.Toplevel.__init__ = theme._original_init
        theme._frameless["on"] = False
        theme._host.update(rect=None, hwnd=None, changed=None, panels=[])

    def pump(self):
        # On Windows a new window is hidden and shown again 10 ms later (theme._windows_frame
        # applies its rounded frame that way): keep Tk running until that has happened.
        deadline = time.time() + 0.3
        while True:
            ROOT.update()
            if time.time() >= deadline:
                break
            time.sleep(0.01)

    def test_fitted_inside_and_centred(self):
        window = tk.Toplevel(ROOT)
        window.geometry("500x920")  # taller than the main window
        self.pump()
        x, y, w, h = self.host
        self.assertTrue(window._myn_panel)
        self.assertLessEqual(window.winfo_height(), h - 2 * theme.PANEL_MARGIN[1])
        self.assertGreaterEqual(window.winfo_rooty(), y)
        self.assertLessEqual(window.winfo_rooty() + window.winfo_height(), y + h)
        self.assertAlmostEqual(window.winfo_rootx() + window.winfo_width() / 2, x + w / 2, delta=2)
        self.assertEqual(self.counts[-1], 1)
        window.event_generate("<Escape>")
        self.pump()
        self.assertEqual(theme.panels(), [])
        self.assertEqual(self.counts[-1], 0)

    def test_without_a_main_window_it_is_an_ordinary_window(self):
        self.host = None
        window = tk.Toplevel(ROOT)
        window.geometry("400x300")
        self.pump()
        self.assertFalse(window._myn_panel)
        window.destroy()

    def test_content_scrolls_instead_of_being_cut(self):
        window = tk.Toplevel(ROOT)
        window.geometry("400x300")
        body = theme.scroll_body(window, padding=10)
        for index in range(30):
            tk.Label(body, text="第 %d 行" % index).pack(anchor="w")
        last = tk.Label(body, text="保存")
        last.pack()
        self.pump()
        scroller = body.master
        self.assertTrue(scroller._bar.winfo_ismapped())  # a scrollbar, only because it is needed
        scroller._canvas.yview_moveto(1.0)
        self.pump()
        self.assertLessEqual(last.winfo_rooty() + last.winfo_height(), window.winfo_rooty() + window.winfo_height() + 1)


if __name__ == "__main__":
    unittest.main()
