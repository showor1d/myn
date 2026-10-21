"""Regression checks for active dock controls and shared speech-worker shutdown."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / 'src'), str(BASE / 'src/core')]

import worker_process
from gui_frameless import FramelessMixin
from gui_webview import WebViewMixin


class SpeechWorkerShutdown(unittest.TestCase):
    def test_absent_or_exited_workers_are_not_killed(self):
        process = Mock()
        process.poll.return_value = 0
        with patch.object(worker_process.subprocess, 'run') as run:
            worker_process.terminate(None)
            worker_process.terminate(process)
        run.assert_not_called()
        process.kill.assert_not_called()

    def test_windows_worker_stops_its_launcher_tree_without_a_shell(self):
        process = Mock(pid=12345)
        process.poll.return_value = None
        with patch.object(worker_process.os, 'name', 'nt'), \
                patch.object(worker_process.subprocess, 'run') as run:
            worker_process.terminate(process)
        self.assertEqual(run.call_args.args[0], ['taskkill', '/PID', '12345', '/T', '/F'])
        self.assertFalse(run.call_args.kwargs.get('shell', False))
        process.kill.assert_not_called()

    def test_non_windows_worker_uses_process_handle(self):
        process = Mock()
        process.poll.return_value = None
        with patch.object(worker_process.os, 'name', 'posix'), \
                patch.object(worker_process.subprocess, 'run') as run:
            worker_process.terminate(process)
        process.kill.assert_called_once_with()
        run.assert_not_called()


class DockWindow(WebViewMixin, FramelessMixin):
    pass


class ClassicDock(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        self.addCleanup(self.root.destroy)
        self.root.geometry('1040x680')
        self.win = win = DockWindow()
        win.root = self.root
        win.core_canvas = tk.Canvas(self.root, highlightthickness=0)
        win.core_canvas.pack(fill='both', expand=True)
        for name in ('agents_entry', 'usage_entry', 'input_entry', 'close_icon', 'minimize_icon', 'voice_label'):
            setattr(win, name, win.core_canvas.create_text(0, 0, text=name))
        for name in ('open_agents_settings', 'open_usage_stats', 'toggle_input', 'close', 'minimize'):
            setattr(win, name, Mock())
        win._timer_hud_data = {}
        win.CHAT_TOP_GAP = 168
        win.input_visible = False
        self.root.update()
        win._position_scene_controls(SimpleNamespace(width=1040, height=680))

    def test_dock_clicks_do_not_drag_and_route_to_the_correct_action(self):
        win = self.win
        for name, action in (('agents', win.open_agents_settings),
                             ('usage', win.open_usage_stats), ('input', win.toggle_input)):
            x, y = win._dock_xy(name)
            event = SimpleNamespace(x=x, y=y, x_root=x, y_root=y)
            win._press_scene(event)
            self.assertIsNone(win._drag_origin)
            win._release_scene(event)
            action.assert_called_once_with()
        # The old gear location is ordinary window-drag space now.
        event = SimpleNamespace(x=944, y=24, x_root=944, y_root=24)
        win._press_scene(event)
        self.assertIsNotNone(win._drag_origin)
        win._click_scene_controls(event)
        win.close.assert_not_called()
        win.minimize.assert_not_called()
        win.open_agents_settings.assert_called_once_with()

    def test_hover_and_window_buttons_still_work_after_menu_removal(self):
        win = self.win
        x, y = win._dock_xy('usage')
        win._hover_scene_controls(SimpleNamespace(x=x, y=y))
        self.assertEqual(win.core_canvas.cget('cursor'), 'hand2')
        self.assertEqual(win.core_canvas.itemcget(win.usage_entry, 'fill'), '#e1f1f4')
        win._leave_scene_controls(None)
        self.assertEqual(win.core_canvas.cget('cursor'), '')
        win._click_scene_controls(SimpleNamespace(x=1016, y=24))
        win._click_scene_controls(SimpleNamespace(x=982, y=24))
        win.close.assert_called_once_with()
        win.minimize.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
