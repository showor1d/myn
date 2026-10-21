"""MYN's side of the WebView2 window: starts web_window.py and talks to it.

Messages from the window arrive on MYN's event queue as ("web", {...}); commands
go out with show_chat / fade_chat / voice / input / window. If the window cannot
start (pywebview not installed, no WebView2, crash), MYN keeps its classic Tk
window instead, so the app always opens.
"""

import importlib.util
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
WINDOW_SCRIPT = HERE / "web_window.py"
ASSETS = HERE / "assets"
READY_TIMEOUT_MS = 20000


def available():
    """Whether the GPU window can be tried: pywebview installed and not switched off."""
    if os.environ.get("MYN_UI", "").lower() == "tk":
        return False
    try:
        return importlib.util.find_spec("webview") is not None
    except (ImportError, ValueError):
        return False


class WebFrontend:
    def __init__(self, events, command=None):
        """events: MYN's queue; command: override for tests (argv list)."""
        self.events = events
        self.ready = False
        self.closed = False
        self._lock = threading.Lock()
        argv = command or [sys.executable, str(WINDOW_SCRIPT)]
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        # Both ends speak UTF-8 (the window also sets it itself): see web_window.utf8_pipes.
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        self.process = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            encoding="utf-8", errors="replace", bufsize=1, creationflags=flags, env=env)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.process.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict):
                self.events.put(("web", message))
        self.events.put(("web", {"event": "closed"}))

    def _send(self, message):
        if self.closed:
            return
        with self._lock:
            try:
                self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
                self.process.stdin.flush()
            except (OSError, ValueError):
                self.closed = True

    def js(self, command):
        self._send({"js": command})

    def window(self, action):
        self._send({"window": action})

    def configure(self):
        """Tell the page about an optional looping background video."""
        video = ASSETS / "background.mp4"
        self.js({"cmd": "config", "video": video.as_uri() if video.is_file() else None})

    def close(self):
        if self.closed:
            return
        self.window("close")
        self.closed = True
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
