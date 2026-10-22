"""Real Tk/WebView2 receipts using synthetic costs, no live accounts or model calls."""
import base64
import json
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src/core")]
import evolution
import evolution_receipt
import expenses
import sheets
import usage_stats
import usage_window
import web_panels3


def seed(config):
    now = datetime.now(expenses.TOKYO)
    point, _ = evolution.add(config, "Repair sleep speech", adopted_plan="Repair and verify the speech entry")
    for run, phase in (("plan", "plan"), ("build", "build")):
        steps = [{"id": run + str(i), "model": "claude-opus-5-5", "at_utc": now.isoformat(),
                  "usage": {"input_tokens": 1000, "output_tokens": 100}, "tools": ["Read"]} for i in range(3)]
        usage_stats.record_agent(config / usage_stats.USAGE_NAME, "claude-opus-5-5", evolution.purpose(phase),
            dict(input_tokens=3000, output_tokens=300), .045, task_key="evolution:" + point["id"],
            run_id=run, steps=steps, calls_complete=True)
    with (config / usage_stats.USAGE_NAME).open("a", encoding="utf-8") as stream:
        for purpose in ("chat_opus", "chat_opus", "scene_analysis"):
            stream.write(json.dumps(dict(at_utc=now.isoformat(), model="claude-opus-5-5", purpose=purpose,
                input_tokens=100, output_tokens=50, cost_usd=.002)) + "\n")
    point.update(status="updated", verification=dict(executed=True, accepted=True, ran=850, skipped=37, failed=0))
    return evolution_receipt.build(config, point, {"result": "kept"})


def classic(config, receipt):
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    closed = []
    window = evolution_receipt.show_tk(root, receipt, lambda: closed.append(True))
    root.update()
    assert window.winfo_viewable()
    assert window.title() == "Evolution cost receipt"
    window.event_generate("<Escape>")
    root.update()
    if window.winfo_exists():
        for child in window.winfo_children():
            if child.winfo_class() == "TButton":
                child.invoke()
    assert closed == [True]
    view = usage_window.UsageWindow(root, config, expense_mode=True)
    view.category = "token"
    view.refresh()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        root.update()
        time.sleep(.02)
    view.draw_receipt()
    texts = [view.receipt.itemcget(i, "text") for i in view.receipt.find_all() if view.receipt.type(i) == "text"]
    assert "Daily chat" in texts and "Long-term memory" in texts and "Self-improvement" in texts, texts
    assert texts.count("claude-opus-5-5") == 9, texts
    view.window.destroy()
    root.destroy()
    print("PASS: Tk receipt, dismissal, daily categories and repeated model rows", flush=True)


class Api:
    host = None
    def emit(self, message):
        if self.host:
            self.host.event(message)


def browser(config, receipt):
    import webview
    result, api = {}, Api()
    window = webview.create_window("MYN cost receipt check", (BASE / "src/web/index.html").as_uri(),
                                  js_api=api, width=1000, height=800)
    def inspect():
        try:
            assert window.events.loaded.wait(20)
            window.evaluate_js("document.body.classList.remove('intro');document.body.style.background='#0b1720'")
            host = sheets.SheetHost(lambda cmd: window.evaluate_js("MYN.receive(" + json.dumps(cmd) + ")"))
            api.host = host
            closed = []
            host.notice(receipt["title"], receipt["body"], lambda: closed.append(True))
            assert window.evaluate_js("!document.getElementById('sheet').hidden && !document.getElementById('sheet-modal').hidden")
            assert window.evaluate_js("document.querySelectorAll('#sheet-modal-box .ui-buttons button').length") == 1
            assert "Succeeded" in window.evaluate_js("document.getElementById('sheet-modal-box').textContent")
            assert window.evaluate_js("document.querySelectorAll('#sheet-modal-box tbody tr').length") == 10
            from webview.platforms.winforms import BrowserView
            from System import Func, Object
            native = BrowserView.instances[window.uid]
            def capture(name):
                task = native.webview.Invoke(Func[Object](lambda: native.webview.CoreWebView2.CallDevToolsProtocolMethodAsync(
                    "Page.captureScreenshot", json.dumps({"format": "png"}))))
                (BASE / name).write_bytes(base64.b64decode(json.loads(str(task.Result))["data"]))
            time.sleep(.6)  # let the panel's opening opacity transition finish
            assert window.evaluate_js("getComputedStyle(document.getElementById('sheet-modal-box')).backgroundColor") == "rgb(16, 33, 46)"
            capture("cost-receipt-preview.png")
            window.evaluate_js("document.querySelector('#sheet-modal-box .ui-buttons button').click()")
            deadline = time.monotonic() + 3
            while not closed and time.monotonic() < deadline:
                time.sleep(.03)
            assert closed == [True]
            panel = host.open(web_panels3.ExpensePanel(SimpleNamespace(_usage_config=lambda: config)))
            panel.category = "token"
            host.draw()
            time.sleep(2.2)
            assert window.evaluate_js("Array.from(document.querySelectorAll('.receipt-use-category')).map(x=>x.textContent)") == ["Daily chat", "Long-term memory", "Self-improvement"]
            assert window.evaluate_js("document.querySelectorAll('.receipt-entry').length") == 9
            capture("daily-cost-preview.png")
            panel.period = "week"
            host.draw()
            assert window.evaluate_js("document.querySelectorAll('.receipt-use-category').length") == 0
            assert window.evaluate_js("document.querySelectorAll('.receipt-entry').length") == 1
            result["passed"] = True
        except Exception:
            import traceback
            result["error"] = traceback.format_exc()
        finally:
            window.destroy()
    webview.start(inspect, gui="edgechromium")
    print(json.dumps(result), flush=True)
    assert result.get("passed"), result


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="myn-cost-ui-") as folder:
        config = Path(folder)
        receipt = seed(config)
        classic(config, receipt)
        browser(config, receipt)
