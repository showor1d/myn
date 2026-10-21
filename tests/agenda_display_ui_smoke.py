"""Actual WebView2 rendering with synthetic calendar data; no personal data or model."""
import json
import sys
import time
from datetime import date, timedelta
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
        window.evaluate_js("document.body.classList.remove('intro')")
        assert window.evaluate_js("document.getElementById('agenda-display').hidden")
        assert window.evaluate_js("!document.querySelector('[data-item=\"agenda\"]')")
        days = []
        for i in range(7):
            when = date(2026, 10, 26) + timedelta(days=i)
            days.append(dict(date=str(when), weekday="周"+"一二三四五六日"[i],
                             label="%d月%d日" % (when.month, when.day), events=[]))
        days[0]["events"] = [dict(time="10:00 – 11:00", title="项目讨论", location="会议室", note="带上资料")]
        days[2]["events"] = [dict(time="全天", title="<img src=x>生日", location="", note="")]
        payload = dict(cmd="agenda_display", from_date="2026-10-26", to_date="2026-11-01", days=days)
        def show(data):
            window.evaluate_js("MYN.receive(" + json.dumps(data, ensure_ascii=False) + ")")
        show(payload)
        RESULT["week"] = window.evaluate_js("""(() => {
          const h = document.getElementById('agenda-display'), c = h.querySelector('.agenda-day');
          return {count:h.querySelectorAll('.agenda-day').length, text:h.textContent, hidden:h.hidden,
            unsafe:!!h.querySelector('img'), animation:getComputedStyle(c).animationName,
            blur:getComputedStyle(h).backdropFilter, overflow:getComputedStyle(h.querySelector('.agenda-grid')).overflowY};
        })()""")
        assert RESULT["week"]["count"] == 7 and not RESULT["week"]["hidden"] and not RESULT["week"]["unsafe"]
        assert RESULT["week"]["animation"] == "agenda-arrive" and RESULT["week"]["blur"] == "none"
        assert RESULT["week"]["overflow"] == "auto" and "No events" in RESULT["week"]["text"]
        # Identical refresh must preserve nodes (no flashing on answer completion).
        window.evaluate_js("window.savedAgendaCard = document.querySelector('.agenda-day')")
        show(payload)
        assert window.evaluate_js("window.savedAgendaCard === document.querySelector('.agenda-day')")
        day = dict(cmd="agenda_display", from_date="2026-10-28", to_date="2026-10-28", days=[days[2]])
        show(day)
        assert window.evaluate_js("document.querySelectorAll('.agenda-day').length === 1")
        assert window.evaluate_js("!!document.querySelector('.agenda-grid.single')")
        # Many entries remain accessible through scrolling.
        day["days"][0]["events"] = [dict(time="10:00", title="安排%d" % n, location="", note="") for n in range(65)]
        show(day)
        assert window.evaluate_js("document.querySelectorAll('.agenda-event').length === 65")
        assert window.evaluate_js("document.querySelector('.agenda-grid').scrollHeight > document.querySelector('.agenda-grid').clientHeight")
        window.evaluate_js("document.querySelector('.agenda-close').click()")
        assert window.evaluate_js("document.getElementById('agenda-display').hidden")
        assert window.evaluate_js("!document.body.classList.contains('agenda-showing')")
        RESULT["passed"] = True
        if "--interactive" in sys.argv:
            days[2]["events"] = [dict(time="全天", title="朋友生日", location="", note="准备礼物")]
            window.set_title("MYN Agenda Animation Check")
            show(payload)
            time.sleep(1)
            print(window.evaluate_js("JSON.stringify({hidden:document.getElementById('agenda-display').hidden, count:document.querySelectorAll('.agenda-day').length})"), flush=True)
            print("READY FOR VISUAL CHECK", flush=True)
            window.events.closed.wait(300)
    except Exception as error:
        RESULT["error"] = repr(error)
    finally:
        window.destroy()


if __name__ == "__main__":
    webview.start = lambda _serve, window, **kw: start(inspect, window, **kw)
    web_window.main()
    print(json.dumps(RESULT, ensure_ascii=False))
    sys.exit(0 if RESULT.get("passed") else 1)
