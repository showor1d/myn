"""Cold-start visual probe: never applies glass, moves, shows or activates MYN.

Uses the production entry point with a synthetic checkerboard behind it. Inspect
with a window capture before sending any input; activation counts expose test
actions that could otherwise hide a startup-composition defect.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import webview
import web_window

create = webview.create_window
start = webview.start
backdrop = None
counts = {"activated": 0, "deactivated": 0}


def create_probe(*args, **kwargs):
    global backdrop
    backdrop = create("MYN cold-start backdrop", x=220, y=120, width=1080, height=720,
                      html='''<body style="margin:0;color:white;font:32px Arial;
                      background:repeating-conic-gradient(#283f77 0 25%,#be693c 0 50%) 0/80px 80px">
                      COLD START 123<br>SHARP BACKGROUND 456</body>''')
    kwargs.update(x=240, y=140)
    window = create(*args, **kwargs)
    def track():
        def active(*_):
            counts["activated"] += 1
            print(json.dumps(counts), flush=True)
        def inactive(*_):
            counts["deactivated"] += 1
            print(json.dumps(counts), flush=True)
        window.native.Activated += active
        window.native.Deactivate += inactive
    window.events.before_show += track
    return window


def inspect(window):
    window.events.loaded.wait(25)
    print("COLD PAGE LOADED; NO TEST-DRIVEN ACTIVATION OR GLASS REAPPLICATION", flush=True)
    window.events.closed.wait(300)
    backdrop.destroy()


if __name__ == "__main__":
    webview.create_window = create_probe
    webview.start = lambda _serve, window, **kw: start(inspect, window, **kw)
    web_window.main()
