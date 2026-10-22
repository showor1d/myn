"""MYN's main window drawn by WebView2 (the Edge engine built into Windows), on the GPU.

Runs as its own process, started by myn_gui.py. It shows src/web/index.html in a
frameless window and relays messages as JSON lines:

  stdin  (from MYN):  {"js": {...}}            -> MYN.receive({...}) in the page
                      {"window": "minimize" | "toggle_maximize" | "show" | "close"}
  stdout (to MYN):    {"event": ...}           -> whatever the page emitted, plus
                      {"event": "unavailable", "error": ...} when pywebview is missing
                      {"event": "closed"}      when the window is gone

The page only draws and reports clicks and typing; all of MYN's logic stays in
myn_gui.py. Nothing personal is written to disk here (private_mode, no storage).
"""

import json
import os
import sys
import threading
from pathlib import Path
from media_bridge import MediaBridge
import app_icon

HERE = Path(__file__).resolve().parent
PAGE = HERE / "web" / "index.html"
# Voice commands must be able to resume the local audio graph without a mouse click.
os.environ['WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS'] = (
    os.environ.get('WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS', '') +
    ' --autoplay-policy=no-user-gesture-required').strip()
_out_lock = threading.Lock()


def utf8_pipes():
    """Speak UTF-8 on the pipes to MYN. Windows would otherwise use the system code
    page (GBK, Shift-JIS…) and Chinese, Japanese or emoji text turns into mojibake."""
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def out(message):
    with _out_lock:
        sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
        sys.stdout.flush()


class Api(MediaBridge):
    """Methods the page can call as window.pywebview.api.<name>(...)."""

    def emit(self, message):
        if isinstance(message, dict) and isinstance(message.get("event"), str):
            out(message)


def apply_glass(window):
    """Clear DWM glass keeps a normal hit-testable HWND; never use a colour key.

    WebView alpha reveals the desktop without the Acrylic blur material.
    On unsupported systems keep the normal dark, interactive host background.
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        from System import Action
        from System.Drawing import Color

        def configure():
            native = window.native
            native.TransparencyKey = Color.Empty
            native.Opacity = 1.0
            # Prepare both surfaces before DWM sees the first frame. Changing
            # the host colour after glass setup left the initial opaque surface
            # cached until a later activation/non-client repaint.
            native.BackColor = Color.Black
            native.webview.DefaultBackgroundColor = Color.Transparent
            hwnd = wintypes.HWND(native.Handle.ToInt64())
            dwm = ctypes.windll.dwmapi
            kind = ctypes.c_int(1)  # DWMSBT_NONE: no Acrylic/Mica background blur
            dark = ctypes.c_int(1)
            dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), 4)
            result = dwm.DwmSetWindowAttribute(hwnd, 38, ctypes.byref(kind), 4)
            margins = (ctypes.c_int * 4)(-1, -1, -1, -1)
            extended = dwm.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins)) if result == 0 else -1
            # On Windows 8+ this legacy API enables alpha composition without
            # blur. An empty region leaves every pixel unblurred; unlike a
            # layered colour-key window, the client still receives mouse input.
            class AlphaComposition(ctypes.Structure):
                _fields_ = [("flags", wintypes.DWORD), ("enabled", wintypes.BOOL),
                            ("region", wintypes.HANDLE), ("transition", wintypes.BOOL)]
            gdi = ctypes.windll.gdi32
            gdi.CreateRectRgn.restype = wintypes.HANDLE
            gdi.DeleteObject.argtypes = [wintypes.HANDLE]
            region = gdi.CreateRectRgn(0, 0, -1, -1)
            try:
                alpha = AlphaComposition(3, True, region, False)
                composed = dwm.DwmEnableBlurBehindWindow(hwnd, ctypes.byref(alpha))
            finally:
                gdi.DeleteObject(region)
            if result == 0 and extended == 0 and composed == 0:
                window._myn_glass = True
                # Commit frame/compositor changes without changing focus, moving,
                # hiding or showing the window. Include the WebView child HWND.
                user = ctypes.windll.user32
                user.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0037)
                user.RedrawWindow(hwnd, None, None, 0x0581)
                dwm.DwmFlush()
            else:
                window._myn_glass = False
                native.BackColor = Color.FromArgb(4, 9, 16)
                native.webview.DefaultBackgroundColor = Color.FromArgb(4, 9, 16)

        if window.native.InvokeRequired:
            window.native.Invoke(Action(configure))
        else:
            configure()
    except Exception as error:
        out({"event": "log", "error": "Glass unavailable; keeping interactive background: %s" % str(error)[:160]})


def serve(window):
    """Forward MYN's commands to the page and the window, line by line."""
    maximized = False
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        try:
            if "js" in message:
                window.evaluate_js("window.MYN && MYN.receive(%s)" % json.dumps(message["js"], ensure_ascii=False))
            action = message.get("window")
            if action == "minimize":
                window.minimize()
            elif action == "toggle_maximize":
                if maximized:
                    window.restore()
                else:
                    window.maximize()
                maximized = not maximized
            elif action == "show":
                window.restore()
                window.show()
            elif action == "close":
                window.destroy()
                return
        except Exception as error:  # a closing window can refuse a late command
            out({"event": "log", "error": str(error)[:200]})
    # MYN went away: close the window too.
    try:
        window.destroy()
    except Exception:
        pass


def main():
    utf8_pipes()
    app_icon.identify()
    try:
        import webview
    except Exception as error:
        out({"event": "unavailable", "error": "pywebview: %s" % error})
        return 3
    size = (1040, 680)
    api = Api()
    window = webview.create_window(
        "MYN", url=PAGE.as_uri(), js_api=api, width=size[0], height=size[1],
        min_size=(720, 520), frameless=True, easy_drag=False, transparent=False,
        background_color="#040910")
    # before_show runs synchronously on the WinForms UI thread, before Show().
    # loaded then commits the child surface after WebView2 has navigated.
    window.events.before_show += lambda: app_icon.apply_web(window)
    window.events.before_show += lambda: apply_glass(window)
    window.events.loaded += lambda: apply_glass(window)
    window.events.loaded += lambda: api._attach_media(window)
    window.events.closed += lambda: out({"event": "closed"})
    window.events.closed += api._close_media
    try:
        webview.start(serve, window, gui="edgechromium" if os.name == "nt" else None,
                      private_mode=True, debug=bool(os.environ.get("MYN_WEB_DEBUG")))
    except Exception as error:
        out({"event": "unavailable", "error": "webview: %s" % error})
        return 4
    out({"event": "closed"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
