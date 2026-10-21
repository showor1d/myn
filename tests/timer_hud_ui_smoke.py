"""Real WebView2 timer animation; synthetic data, no scheduling or audio."""
import json
import sys
import time
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import webview
import web_window

RESULT = {}
start = webview.start


def inspect(window):
    try:
        assert window.events.loaded.wait(20)
        RESULT["initial"] = window.evaluate_js("""(() => {
          document.body.classList.remove('intro');
          return {noTimers: !document.querySelector('.timer-chip,.alarm-indicator'),
                  removed: !document.querySelector('[data-item="timers"]')};
        })()""")
        assert RESULT["initial"] == {"noTimers": True, "removed": True}
        running = dict(id="t1", label="番茄钟", kind="countdown", remaining=90, total=120, paused=False, when="")
        reminder = dict(id="r1", label="喝水", kind="scheduled", remaining=3600, total=0, paused=False,
                     when="10月21日 08:00")
        paused = dict(id="t2", label="<img src=x>休息一下", kind="countdown", remaining=45, total=60, paused=True, when="")

        def update(items, count=None, alarm_upcoming=False):
            window.evaluate_js("MYN.receive(" + json.dumps(dict(cmd="timers", items=items,
                               alarm_upcoming=alarm_upcoming,
                               count=len(items) if count is None else count), ensure_ascii=False) + ")")

        update([running, reminder, paused], 5, True)
        RESULT["active"] = window.evaluate_js("""(() => {
          const h = document.getElementById('timer-hud');
          const rows = h.querySelectorAll('.timer-chip');
          return {hidden: h.hidden, count: rows.length, text: h.textContent, unsafe: !!h.querySelector('img'),
             pointer: getComputedStyle(h).pointerEvents,
             orbit: getComputedStyle(rows[1].querySelector('svg')).animationName,
             paused: getComputedStyle(rows[2].querySelector('.timer-dot')).animationName,
             time: rows[0].querySelector('.timer-value').textContent,
             pausedTime: rows[2].querySelector('.timer-value').textContent,
             progress: rows[0].style.getPropertyValue('--timer-progress')};
        })()""")
        active = RESULT["active"]
        assert not active["hidden"] and active["count"] == 3 and not active["unsafe"]
        assert active["pointer"] == "none" and active["orbit"] == "timer-orbit" and active["paused"] == "none"
        assert "2 more timers" in active["text"]
        RESULT["alarm"] = window.evaluate_js("""(() => {
          const a = document.querySelector('.alarm-indicator');
          return {count:document.querySelectorAll('.alarm-indicator').length, text:a.textContent,
                  icon:!!a.querySelector('svg'), role:a.tagName,
                  label:a.getAttribute('aria-label'),
                  details:!!document.querySelector('.alarm-chip, .alarm-time')};
        })()""")
        assert RESULT["alarm"]["count"] == 1 and RESULT["alarm"]["icon"] and not RESULT["alarm"]["details"]
        assert not RESULT["alarm"]["text"].strip()
        assert RESULT["alarm"]["role"] == "BUTTON" and RESULT["alarm"]["label"] == "Alarm scheduled within the next 24 hours"
        time.sleep(1.3)
        RESULT["elapsed"] = window.evaluate_js("""({
          time: document.querySelector('.timer-chip .timer-value').textContent,
          pausedTime: document.querySelector('.timer-chip.paused .timer-value').textContent
        })""")
        assert RESULT["elapsed"]["time"] != active["time"]
        assert RESULT["elapsed"]["pausedTime"] == active["pausedTime"]
        update([], alarm_upcoming=True)
        assert not window.evaluate_js("document.getElementById('timer-hud').hidden")
        assert window.evaluate_js("document.querySelectorAll('.timer-chip').length") == 0
        update([])
        assert window.evaluate_js("!document.querySelector('.timer-chip,.alarm-indicator')")
        assert window.evaluate_js("document.querySelectorAll('.alarm-indicator').length") == 0
        window.evaluate_js("MYN.receive({cmd:'timer_finished',text:'番茄钟结束，休息一下吧'})")
        assert not window.evaluate_js("document.getElementById('timer-hud').hidden")
        time.sleep(4.3)
        assert window.evaluate_js("document.querySelector('.timer-finished').hidden")
        RESULT["passed"] = True
        if "--interactive" in sys.argv:
            window.set_title("MYN Timer Animation Check")
            running.update(remaining=1480, total=1500)
            paused["label"] = "休息一下"
            update([running, reminder, paused], 5, True)
            print("READY FOR VISUAL CHECK", flush=True)
            window.events.closed.wait(300)
    except Exception as error:
        RESULT["error"] = repr(error)
    finally:
        window.destroy()


if __name__ == "__main__":
    webview.start = lambda _serve, window, **kw: start(inspect, window, **kw)
    with tempfile.TemporaryDirectory(prefix="myn-timer-ui-") as folder:
        os.environ["MYN_PERSONAL_DATA_DIR"] = folder
        web_window.main()
    print(json.dumps(RESULT, ensure_ascii=False))
    sys.exit(0 if RESULT.get("passed") else 1)
