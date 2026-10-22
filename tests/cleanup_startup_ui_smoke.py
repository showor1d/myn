"""Actual Tk lifecycle callback retires a synthetic registered copy off the UI thread."""
import sys
import threading
import time
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE/'src'), str(BASE/'src/core'), str(BASE/'tests')]
import tkinter as tk
import myn_gui
import development_cleanup
import self_update
from test_development_cleanup import Lifecycle

case = Lifecycle()
case.setUp()
root = tk.Tk()
root.withdraw()
try:
    case.register([case.artifact])
    win = myn_gui.MYNWindow.__new__(myn_gui.MYNWindow)
    win.root = root
    win.self_fix = win.agent_runner = None
    errors, threads = [], []
    root.report_callback_exception = lambda *args: errors.append(args)
    finish = development_cleanup.finish_startup
    def observed(batch):
        threads.append(threading.get_ident())
        return finish(batch, case.root)
    with mock.patch.object(development_cleanup, 'ROOT', case.root), \
            mock.patch.object(self_update, 'CONFIRM_AFTER_S', 0), \
            mock.patch.object(development_cleanup, 'finish_startup', side_effect=observed):
        win._cleanup_startup()
        assert case.work.exists()
        deadline = time.monotonic()+6
        while case.work.exists() and time.monotonic() < deadline:
            root.update()
            time.sleep(.02)
        assert not errors, errors
        assert not case.work.exists()
        assert threads and all(t != threading.get_ident() for t in threads)
        # Wait for the worker's final receipt rather than exiting during its last write.
        receipt = case.backup/'verification/cleanup/result.json'
        while not receipt.exists() and time.monotonic() < deadline:
            root.update()
            time.sleep(.02)
        assert receipt.is_file()
        assert (case.backup/'src/core/example.py').is_file()
        assert (case.root/'personal_data/keep.txt').read_text() == 'private'
        print('PASS: actual Tk startup delays cleanup, uses a background worker and keeps recovery/data intact.')
finally:
    root.destroy()
    case.doCleanups()
