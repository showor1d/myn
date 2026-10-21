"""Real WebView2 and Tk receipt check, synthetic records only; no API or audio."""
import base64
import json
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src/core")]
import usage_stats
import usage_window
import web_panels3
import sheets


def seed(config):
    now = datetime.now().astimezone()
    usage_stats.add_manual(config, now, "Codex", 9535102, 12.68, estimated=True)
    usage_stats.add_manual(config, now, "Claude", 821406, 4.25, estimated=True)
    usage_stats.add_manual(config, now - timedelta(days=1), "Codex", 1834106, 5.65)


def classic(config):
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    errors = []
    root.report_callback_exception = lambda *args: errors.append(str(args))
    view = usage_window.UsageWindow(root, config)
    deadline = time.monotonic() + 1.4
    while time.monotonic() < deadline:
        root.update()
        time.sleep(.02)
    assert view._print_job is None
    texts = [view.receipt.itemcget(i, "text") for i in view.receipt.find_all()
             if view.receipt.type(i) == "text"]
    assert "$16.93" in texts and "10,356,508" in texts, texts
    assert not hasattr(view, "open_manual")
    view._view_receipt()
    root.update()
    assert view.receipt_viewer.winfo_exists()
    view.receipt_viewer.destroy()
    view.set_period("week")
    root.update()
    assert view._print_job
    assert not hasattr(view, "chart_area")
    view.window.destroy()  # closing mid-print cancels animation
    root.update()
    root.destroy()
    assert not errors, errors
    print("Tk receipt, totals, period, no chart details and close-during-print: PASS", flush=True)


class Api:
    host = None

    def emit(self, message):
        if self.host:
            self.host.event(message)


def browser(config):
    import webview
    api = Api()
    window = webview.create_window("MYN receipt check", (BASE / "src/web/index.html").as_uri(),
                                   js_api=api, width=1040, height=820)
    result = {}

    def inspect():
        try:
            assert window.events.loaded.wait(20)
            window.evaluate_js("document.body.classList.remove('intro'); document.body.style.background='#0b1720'")
            host = sheets.SheetHost(lambda cmd: window.evaluate_js("MYN.receive(" + json.dumps(cmd) + ")"))
            api.host = host
            panel = host.open(web_panels3.UsagePanel(SimpleNamespace(_usage_config=lambda: config)))
            assert not host.last_error, host.last_error
            check = window.evaluate_js("""(() => {
              const p=document.querySelector('.usage-paper');
              return {text:p.textContent, anim:getComputedStyle(p).animationName,
                duration:getComputedStyle(p).animationDuration,
                edits:[...document.querySelectorAll('#sheet-body button')].some(b=>/手动|编辑/.test(b.textContent))};
            })()""")
            assert check["anim"] == "receipt-print-tear" and check["duration"] == "1.25s", check
            assert "$16.93" in check["text"] and "10,356,508" in check["text"] and not check["edits"], check
            window.evaluate_js("document.querySelector('.usage-paper').click()")
            assert not window.evaluate_js("!!document.querySelector('.receipt-viewer')")
            time.sleep(1.4)
            assert window.evaluate_js("document.querySelector('.usage-receipt-layout').dataset.phase") == "detached"
            # Capture the renderer itself, not other applications or desktop pixels.
            from webview.platforms.winforms import BrowserView
            from System import Func, Object
            native = BrowserView.instances[window.uid]

            def protocol(name, args):
                task = native.webview.Invoke(Func[Object](lambda:
                    native.webview.CoreWebView2.CallDevToolsProtocolMethodAsync(name, json.dumps(args))))
                return json.loads(str(task.Result))

            def capture(name):
                shot = protocol("Page.captureScreenshot", {"format": "png"})
                (BASE / name).write_bytes(base64.b64decode(shot["data"]))

            capture(".tmp-usage-receipt.png")
            for i in range(12):
                usage_stats.add_manual(config, datetime.now().astimezone(), "Source "+str(i), 100, 1)
            host.draw()
            assert not window.evaluate_js("document.querySelector('.usage-receipt-layout').classList.contains('printing')")
            assert not window.evaluate_js("!!document.querySelector('.usage-details')")
            scroll = window.evaluate_js("""(() => {
              const body=document.getElementById('sheet-body'), feed=document.querySelector('.receipt-feed');
              body.scrollTop=100; feed.scrollTop=300;
              return {page:body.scrollTop, paper:feed.scrollTop, height:body.clientHeight, content:body.scrollHeight};
            })()""")
            assert scroll["page"] == 0 and scroll["paper"] > 0 and scroll["content"] <= scroll["height"], scroll
            host.draw()
            assert window.evaluate_js("document.querySelector('.receipt-feed').scrollTop") == scroll["paper"]
            window.evaluate_js("document.querySelector('.usage-paper').click()")
            time.sleep(.4)
            image = window.evaluate_js("""(() => {
              const img=document.querySelector('.receipt-viewer img'), b=img.getBoundingClientRect(),
                    stage=img.parentElement.getBoundingClientRect();
              return {loaded:img.complete && img.naturalWidth>0, full:img.naturalHeight>1000,
                      fits:b.height<=stage.height && b.width<=stage.width};
            })()""")
            assert all(image.values()), image
            capture(".tmp-usage-receipt-viewer.png")
            window.evaluate_js("[...document.querySelectorAll('.receipt-viewer button')].find(b=>b.textContent==='原尺寸').click()")
            assert not window.evaluate_js("document.querySelector('.receipt-image-stage').classList.contains('fit')")
            window.evaluate_js("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))")
            assert not window.evaluate_js("!!document.querySelector('.receipt-viewer')")
            assert not window.evaluate_js("document.getElementById('sheet').hidden")
            window.evaluate_js("[...document.querySelectorAll('#sheet-body button')].find(b=>b.textContent==='周').click()")
            time.sleep(.2)
            assert panel.period == "week"
            assert window.evaluate_js("document.querySelector('.usage-receipt-layout').classList.contains('printing')")
            # Mobile width, reduced motion, empty state, and source labels treated as text.
            protocol("Emulation.setDeviceMetricsOverride",
                     {"width": 430, "height": 800, "deviceScaleFactor": 1, "mobile": False})
            protocol("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-reduced-motion", "value": "reduce"}]})
            panel.period, panel.offset = "day", -3
            host.draw()
            assert window.evaluate_js("getComputedStyle(document.querySelector('.usage-paper')).animationName") == "none"
            assert window.evaluate_js("document.querySelector('.usage-paper').textContent.includes('本期暂无用量记录')")
            assert window.evaluate_js("document.getElementById('sheet-body').scrollWidth <= document.getElementById('sheet-body').clientWidth")
            assert window.evaluate_js("document.getElementById('sheet-body').scrollHeight <= document.getElementById('sheet-body').clientHeight")
            capture(".tmp-usage-receipt-narrow.png")
            result["passed"] = True
        except Exception as error:
            result["error"] = repr(error)
        finally:
            window.destroy()

    webview.start(inspect, gui="edgechromium")
    print(json.dumps(result, ensure_ascii=False), flush=True)
    assert result.get("passed"), result


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="myn-receipt-") as directory:
        config = Path(directory)
        seed(config)
        classic(config)
        browser(config)
