"""Real Tk/WebView2 refresh transitions, using temporary synthetic expenses only."""
import copy
import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src/core")]
from expense_ui_smoke import seed, Api
import expenses
import sheets
import usage_window
import web_panels3


def changed(report):
    result = copy.deepcopy(report)
    result["cost"] = "$250.00 USD"
    for item in result["categories"]:
        item["percent"] = 75 if item["name"] == "eat" else 0
    # A new category must grow into the existing pie; existing slices shrink.
    result["chart_slices"] = [dict(name="eat", value=100, color="#9744ec"),
                              dict(name="health", value=150, color="#d47977")]
    result["series"]["cost_usd"] = [100 if i == 0 else 25
                                    for i in range(len(result["series"]["cost_usd"]))]
    return result


def classic(config):
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    view = usage_window.UsageWindow(root, config, expense_mode=True)
    root.update()
    for period in ("day", "week", "month", "year"):
        view.set_period(period)
        view._metrics_fraction = view._rings_fraction = 1
        view._paint_metric_amount()
        before = copy.deepcopy(view._metric_state)
        view.result = changed(view.result)
        view.fill_cards()
        assert view._metric_state["amount"] == before["amount"] == 150
        assert view._metric_state["rings"] == before["rings"]
        view._metrics_fraction = view._rings_fraction = .5
        view._paint_metric_amount()
        view._draw_categories()
        view._draw_expense_trend()
        assert view._metric_state["amount"] == 200
        for name, value in view._metric_target["rings"].items():
            assert abs(view._metric_state["rings"][name]-(before["rings"][name]+value)/2) < .001
        # A second save begins at the displayed midpoint, not the previous target.
        view.result["cost"] = "$350.00 USD"
        view.fill_cards()
        assert view._metric_state["amount"] == 200
        started = view._metrics_started
        view.fill_cards()
        assert view._metrics_started == started
        view._metrics_fraction = view._rings_fraction = 1
        view._paint_metric_amount()
        assert view._metric_state["amount"] == 350
        view.result["cost"] = "$0.00 USD"
        view.result["chart_slices"] = []
        for item in view.result["categories"]:
            item["percent"] = 0
        view.fill_cards()
        assert view._metric_state["amount"] == 350
        view._metrics_fraction = view._rings_fraction = .5
        view._paint_metric_amount()
        view._draw_expense_trend()
        assert view._metric_state["amount"] == 175
    view.set_period("day")
    view.select_category("eat")
    view._metrics_fraction = view._rings_fraction = 1
    view._paint_metric_amount()
    assert view._metric_state["pies"][0]["name"] == "午饭"
    view.result = changed(view.result)
    view.result["chart_slices"] = [dict(name="午饭", value=100, color="#9744ec"),
                                 dict(name="咖啡", value=150, color="#d47977")]
    view.fill_cards()
    view._metrics_fraction = view._rings_fraction = .5
    view._paint_metric_amount()
    view._draw_expense_trend()
    assert {s["name"] for s in view._metric_state["pies"]} == {"午饭", "咖啡"}
    view.window.destroy()
    root.destroy()
    print("Tk refresh transitions PASS", flush=True)


def browser(config):
    import webview
    api, result = Api(), {}
    window = webview.create_window("MYN refresh transitions", (BASE / "src/web/index.html").as_uri(),
                                  js_api=api, width=1040, height=820)
    def js(code):
        return window.evaluate_js(code)
    def seek(progress):
        return js("""(() => {
          const r=document.querySelector('.usage-receipt-layout');
          r.startMetrics(performance.now()-(%s || -100), r.metricsFrom); r.stopMetrics();
          return r.metricsSnapshot();
        })()""" % (progress*1200))
    def check():
        try:
            assert window.events.loaded.wait(20)
            host = sheets.SheetHost(lambda cmd: js("MYN.receive("+json.dumps(cmd)+")"))
            api.host = host
            panel = web_panels3.ExpensePanel(SimpleNamespace(_usage_config=lambda: config))
            for period in ("day", "week", "month", "year"):
                report = expenses.report(config, period)
                panel._report = lambda: (report, None)
                host.open(panel)
                before = seek(1)
                report = changed(report)
                host.draw()
                first = seek(0)
                assert first["amount"] == before["amount"] == 150, first
                assert first["rings"] == before["rings"]
                middle = seek(.5)
                assert abs(middle["amount"]-237.5) < .1, middle
                target = js("JSON.parse(document.querySelector('.usage-receipt-layout').metricsTarget)")
                for name, value in target["rings"].items():
                    assert abs(middle["rings"][name]-(before["rings"][name]+value)/2) < .02
                if period == "day":
                    new_slice = next(s for s in middle["pies"] if s["name"] == "health")
                    assert abs(new_slice["value"]-.3) < .001
                    assert js("document.querySelectorAll('.expense-slice').length") == 4
                else:
                    for name, (x, height) in target["series"].items():
                        assert abs(middle["series"][name][1]-(before["series"][name][1]+height)/2) < .02
                    assert middle["reveal"] == 1  # no new left-to-right wipe
                    if period == "year":
                        assert abs(js("Number(document.querySelector('.expense-bar').getAttribute('height'))")
                                   -next(iter(middle["series"].values()))[1]) < .01
                # A third receipt while the animation runs starts at its current frame.
                report["cost"] = "$350.00 USD"
                host.draw()
                interrupted = seek(0)
                assert abs(interrupted["amount"]-middle["amount"]) < .001
                start = js("document.querySelector('.usage-receipt-layout').metricsStartedAt")
                host.draw()
                assert js("document.querySelector('.usage-receipt-layout').metricsStartedAt") == start
                final = seek(1)
                assert final["amount"] == 350
                assert final["rings"] == target["rings"]
                if period == "day":
                    assert js("document.querySelectorAll('.expense-slice').length") == 2
                report["cost"] = "$0.00 USD"
                report["chart_slices"] = []
                for item in report["categories"]:
                    item["percent"] = 0
                host.draw()
                assert seek(0)["amount"] == 350
                assert abs(seek(.5)["amount"]-43.75) < .1
                assert seek(1)["amount"] == 0
                if period == "day":
                    assert js("document.querySelectorAll('.expense-slice').length") == 0
            report = expenses.report(config, category="eat")
            host.draw()
            seek(1)
            report = changed(report)
            report["chart_slices"] = [dict(name="午饭", value=100, color="#9744ec"),
                                     dict(name="咖啡", value=150, color="#d47977")]
            host.draw()
            middle = seek(.5)
            assert {s["name"] for s in middle["pies"]} == {"午饭", "咖啡"}
            assert abs(next(s["value"] for s in middle["pies"] if s["name"] == "咖啡")-.3) < .001
            result["passed"] = True
        except Exception as error:
            import traceback
            result["error"] = traceback.format_exc()
        finally:
            window.destroy()
    webview.start(check, gui="edgechromium")
    print(json.dumps(result, ensure_ascii=False), flush=True)
    assert result.get("passed"), result


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="myn-expense-transition-") as folder:
        config = Path(folder)
        seed(config)
        classic(config)
        browser(config)
