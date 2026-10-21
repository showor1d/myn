"""Real Tk and WebView2 personal spending page; temporary synthetic purchases only."""
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
import expenses
import expense_rates
import usage_stats
import sheets
import usage_window
import web_panels3


def seed(config):
    now = datetime.now(expenses.TOKYO)
    quote = dict(date=now.date().isoformat(), source="synthetic",
                 rates=dict(USD="1.2", JPY="180", CNY="8.4"))
    (config / "expense-rates.json").write_text(json.dumps({
        "source": expense_rates.SOURCE, "rows": {quote["date"]: quote["rates"]}}), encoding="utf-8")
    usage_stats.add_manual(config, now, "Codex", 700000, 10, estimated=True)
    for i in range(14):
        currency = ("JPY", "USD", "CNY")[i % 3]
        amount = {"JPY": 1500, "USD": 10, "CNY": 70}[currency]
        expenses.add(config, amount, currency, ("eat", "study")[i % 2], "午饭" if i % 2 == 0 else "书本",
                     now.isoformat(), str(i), lambda *_: quote)


def classic(config):
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    errors = []
    root.report_callback_exception = lambda *args: errors.append(str(args))
    view = usage_window.UsageWindow(root, config, expense_mode=True)
    assert view.window.title() == "Expenses"
    assert [button.cget("text") for button in view.period_buttons.values()] == ["D", "W", "M", "Y"]
    assert view.cards[0][1].cget("text") == "$0.00 USD"
    for fraction in (0, .25, .5):
        view._rings_fraction = fraction
        view._draw_expense_trend()
        sweep = sum(abs(float(view.expense_trend.itemcget(i, "extent")))
                    for i in view.expense_trend.find_all() if view.expense_trend.type(i) == "arc")
        assert abs(sweep-360*fraction) < .01, (fraction, sweep)
    view._rings_fraction = 0
    def wait():
        end = time.monotonic() + 2.1
        while time.monotonic() < end:
            root.update()
            time.sleep(.02)
    wait()
    assert view.result["cost"] == "$150.00 USD"
    assert view.cards[0][1].cget("text") == "$150.00 USD"
    assert view.expense_back.cget("text") == "back"
    assert not view.cards[0][2].winfo_ismapped()
    assert view.expense_trend.find_all()
    assert len(view.expense_trend.find_withtag("pie-leader")) == 3
    assert len(view.expense_trend.find_withtag("pie-label")) == 3
    assert view._metrics_fraction == 1 and view._metrics_job is None
    started = view._metrics_started
    view.refresh()
    assert view._metrics_started == started and view._metrics_job is None
    assert len(view.category_list.find_withtag("category-name")) == 9
    view._category_selected(SimpleNamespace(y=93))
    assert view.category == "study"  # second by spending, not the old fixed category order
    view.select_category("token")
    assert view.cards[0][1].cget("text") == "$0.00 USD"
    wait()
    assert view.result["token_total"] == 700000
    assert view.result["cost"] == "$10.00 USD"
    assert view.cards[0][1].cget("text") == "$10.00 USD"
    texts = {view.receipt.itemcget(i, "text"): view.receipt.coords(i)
             for i in view.receipt.find_all() if view.receipt.type(i) == "text"}
    assert texts["token"][1] < texts["700,000"][1]
    view.select_category("eat")
    wait()
    assert view.result["count"] == 7
    assert [s["name"] for s in view.result["chart_slices"]] == ["午饭"]
    view.select_category("eat")
    assert view.category is None
    view.select_category("health")
    wait()
    assert view.result["zero"]
    zero_texts = {view.receipt.itemcget(i, "text"): view.receipt.coords(i)
                  for i in view.receipt.find_all() if view.receipt.type(i) == "text"}
    assert zero_texts["No spending this period"][1] < zero_texts["Nicely done!"][1]
    view._view_receipt()
    root.update()
    assert view.receipt_viewer.winfo_exists()
    view.receipt_viewer.destroy()
    view.select_category("eat")
    view.set_period("year")
    assert len(view.result["rows"]) == 1 and view.result["rows"][0]["cost"] == "$70.00 USD"
    assert "7 entries" in view.result["rows"][0]["detail"]
    view._rings_fraction = .5
    view._draw_expense_trend()
    bars = [view.expense_trend.coords(i) for i in view.expense_trend.find_all()
            if view.expense_trend.type(i) == "rectangle"]
    assert bars and abs(bars[0][3]-bars[0][1]-24) < .01, bars
    view.set_period("week")
    for fraction in (.25, .5, 1):
        view._rings_fraction = fraction
        view._draw_expense_trend()
        line = view.expense_trend.find_withtag("expense-line")[0]
        end_x = view.expense_trend.coords(line)[-2]
        assert abs(end_x-(3+(view.expense_trend.winfo_width()-6)*fraction)) < .01
    assert view._metrics_job is not None
    view.window.destroy()
    assert view._metrics_job is None
    root.update()
    root.destroy()
    assert not errors, errors
    print("Tk expense totals, categories, zero smile and viewer PASS", flush=True)


class Api:
    host = None

    def emit(self, message):
        if self.host:
            self.host.event(message)


def browser(config):
    import webview
    api, result = Api(), {}
    window = webview.create_window("MYN expense check", (BASE / "src/web/index.html").as_uri(),
                                  js_api=api, width=1040, height=820)
    def inspect():
        try:
            assert window.events.loaded.wait(20)
            window.evaluate_js("document.body.classList.remove('intro');document.body.style.background='#0b1720';document.body.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true}))")
            host = sheets.SheetHost(lambda cmd: window.evaluate_js("MYN.receive(" + json.dumps(cmd) + ")"))
            api.host = host
            panel = host.open(web_panels3.ExpensePanel(SimpleNamespace(_usage_config=lambda: config)))
            assert not host.last_error, host.last_error
            assert window.evaluate_js("document.querySelector('#sheet-title').textContent") == "Expenses"
            assert window.evaluate_js("getComputedStyle(document.getElementById('sheet-title')).display==='none'")
            assert window.evaluate_js("getComputedStyle(document.getElementById('sheet-close')).display==='none'")
            assert window.evaluate_js("!document.querySelector('#menu [data-item=usage]')")
            assert window.evaluate_js("document.getElementById('expenses-shortcut').getAttribute('aria-label')") == "Expenses"
            assert window.evaluate_js("[...document.querySelectorAll('.expense-nav button')].slice(0,4).map(b=>b.textContent)") == ["D", "W", "M", "Y"]
            assert window.evaluate_js("document.querySelector('.receipt-date').textContent") == panel._report()[0]["date"]
            def metrics():
                return window.evaluate_js("""(() => {
                  const r=document.querySelector('.usage-receipt-layout');
                  return {start:r.metricsStartedAt,
                    amount:Number(r.querySelector('.usage-number').textContent.replace(/[^0-9.-]/g,'')),
                    ring:Number(r.querySelector('.category-ring-progress').getAttribute('stroke-dasharray').split(' ')[0])};
                })()""")
            initial = metrics()
            initial_core = window.evaluate_js("expensesShift")
            assert 0 <= initial_core < 1
            assert window.evaluate_js("(()=>{const r=document.getElementById('sheet').getBoundingClientRect();return r.left===0 && r.top===0 && r.width===innerWidth && r.height===innerHeight})()")
            assert window.evaluate_js("getComputedStyle(document.getElementById('status-dock')).visibility==='hidden'")
            initial_print = window.evaluate_js("""(() => {
                const r=document.querySelector('.usage-receipt-layout');
                return {start:r.printStartedAt, phase:r.dataset.phase,
                        moving:getComputedStyle(r.querySelector('.usage-paper')).animationName};
            })()""")
            assert initial_print["phase"] == "printing" and initial_print["moving"] == "receipt-print-tear", initial_print
            initial_pie = window.evaluate_js("document.querySelector('.expense-pie-reveal').getAttribute('d')")
            assert initial["amount"] < 150 and initial["ring"] < 46, initial
            time.sleep(.25)
            middle = metrics()
            assert initial_core < window.evaluate_js("expensesShift") < 1
            assert window.evaluate_js("document.querySelector('.expense-pie-reveal').getAttribute('d')") != initial_pie
            assert initial["amount"] < middle["amount"] < 150, (initial, middle)
            assert initial["ring"] < middle["ring"] < 46.7, (initial, middle)
            host.draw()
            assert metrics()["start"] == initial["start"]  # redraw resumes instead of restarting
            assert window.evaluate_js("document.querySelector('.usage-receipt-layout').printStartedAt") == initial_print["start"]
            assert window.evaluate_js("document.querySelector('.usage-receipt-layout').dataset.phase") == "printing"
            assert window.evaluate_js("parseFloat(getComputedStyle(document.querySelector('.usage-paper')).animationDelay)") < -.1
            time.sleep(1.4)
            assert window.evaluate_js("expensesShift") == 1
            assert window.evaluate_js("(()=>{const r=document.getElementById('expenses-core-slot').getBoundingClientRect(),m=ctx.getTransform();return m.a<.3 && Math.abs(m.e/DPR+W/2*m.a-(r.left+r.width/2))<1 && Math.abs(m.f/DPR+(H/2-3)*m.a-(r.top+r.height/2))<1})()")
            assert metrics()["amount"] == 150
            host.draw()
            assert window.evaluate_js("expensesShift") == 1  # data refresh never restarts core travel
            assert metrics()["start"] == initial["start"] and metrics()["amount"] == 150
            for elapsed, expected_height in [(0, 0), (300, 36), (600, 72)]:
                box = window.evaluate_js("""(() => {
                    const r=document.querySelector('.usage-receipt-layout');
                    r.startMetrics(performance.now()-(%d || -100)); r.stopMetrics();
                    return r.querySelector('.expense-pie-reveal').getBBox().height;
                })()""" % elapsed)
                assert abs(box-expected_height) < .5, (elapsed, box)
            window.evaluate_js("(()=>{const r=document.querySelector('.usage-receipt-layout');r.startMetrics(performance.now()-1200);r.stopMetrics()})()")
            assert window.evaluate_js("document.querySelector('.expense-metric-header .expense-back').textContent") == "back"
            assert not window.evaluate_js("!!document.querySelector('.expense-layout .usage-caption')")
            assert window.evaluate_js("document.querySelectorAll('.expense-slice').length") == 3
            assert window.evaluate_js("document.querySelectorAll('.expense-pie-leader').length") == 3
            assert sorted(window.evaluate_js("[...document.querySelectorAll('.expense-pie-label')].map(n=>n.textContent)")) == ["eat", "study", "token"]
            assert not window.evaluate_js("!!document.querySelector('.expense-trend-labels')")
            panel.offset = -1
            host.draw()
            window.evaluate_js("document.querySelector('.expense-back').click()")
            time.sleep(1.4)
            assert panel.offset == 0
            assert window.evaluate_js("document.querySelectorAll('.expense-category').length") == 9
            assert window.evaluate_js("[...document.querySelectorAll('.expense-category')].slice(0,3).map(b=>b.lastChild.textContent)") == ["eat", "study", "token"]
            assert window.evaluate_js("document.querySelectorAll('.category-ring-progress').length") == 9
            assert window.evaluate_js("Number(document.querySelector('.category-ring-progress').getAttribute('stroke-dasharray').split(' ')[0])>46")
            assert not window.evaluate_js("document.querySelector('.usage-paper').hasAttribute('title')")
            shape = window.evaluate_js("""(() => {
              const p=document.querySelector('.usage-paper'), b=p.getBoundingClientRect();
              return {width:b.width, safe:document.querySelector('.receipt-brand').getBoundingClientRect().top-b.top,
                      torn:getComputedStyle(p).clipPath.startsWith('polygon(')};
            })()""")
            assert shape["width"] <= 350 and shape["safe"] >= 20 and shape["torn"], shape
            assert window.evaluate_js("document.querySelector('.usage-paper').textContent.includes('$150.00 USD')")
            assert window.evaluate_js("document.querySelector('.receipt-footnote').textContent") == "14.2857 USD = 100 CNY = 2142.8571 JPY"
            from webview.platforms.winforms import BrowserView
            from System import Func, Object
            native = BrowserView.instances[window.uid]
            def protocol(name, args):
                task = native.webview.Invoke(Func[Object](lambda:
                    native.webview.CoreWebView2.CallDevToolsProtocolMethodAsync(name, json.dumps(args))))
                return json.loads(str(task.Result))
            def capture(name):
                (BASE / name).write_bytes(base64.b64decode(protocol("Page.captureScreenshot", {"format": "png"})["data"]))
            layout = window.evaluate_js("""(() => {
                const nav=document.querySelector('.expense-nav');
                const buttons=nav.querySelector('.ui-row').getBoundingClientRect();
                const periodButton=nav.querySelector('button').getBoundingClientRect();
                const date=nav.querySelector('.ui-p').getBoundingClientRect();
                const slot=document.querySelector('.receipt-slot').getBoundingClientRect();
                const feed=document.querySelector('.receipt-feed').getBoundingClientRect();
                return {dateBelow:date.top>=buttons.bottom, slotTop:slot.top, feedGap:feed.top-slot.bottom,
                        centerGap:(slot.top+slot.bottom-periodButton.top-periodButton.bottom)/2};
            })()""")
            assert layout["dateBelow"] and -4 <= layout["feedGap"] <= 0, layout
            assert abs(layout["slotTop"]-54) <= 1, layout
            window.evaluate_js("""(() => {
                const r=document.querySelector('.usage-receipt-layout');r.startPrint();
                window.receiptPreviewAnimations=r.getAnimations({subtree:true});
                window.receiptPreviewAnimations.forEach(a=>a.pause());
            })()""")
            contours = window.evaluate_js("""(() => {
                const edge=[...document.querySelector('.receipt-tear-edge polyline').points]
                  .map(p=>[p.x/10,p.y-1]);
                const cut=document.querySelector('.receipt-stub').style.clipPath;
                const upper=[...cut.matchAll(/([\\d.]+)% ([\\d.]+)px/g)]
                  .map(m=>[Number(m[1]),Number(m[2])]).slice(-edge.length).reverse();
                return {edge,upper};
            })()""")
            assert contours["edge"] == contours["upper"], contours
            assert window.evaluate_js("""(() => {
                const edge=document.querySelector('.receipt-tear-edge polyline');
                const light=document.querySelector('.receipt-tear-light');
                return edge.getAttribute('points')===light.getAttribute('points') &&
                    getComputedStyle(document.querySelector('.receipt-slot'),'::after').content==='none';
            })()""")
            for ms, expected in [(1250, -10.5), (1500, -7), (1750, -3.5)]:
                offset = window.evaluate_js("""(() => {
                    window.receiptPreviewAnimations.forEach(a=>a.currentTime=%d);
                    const matrix=new DOMMatrixReadOnly(getComputedStyle(document.querySelector('.usage-paper')).transform);
                    return {x:matrix.m41,y:matrix.m42,rotate:matrix.m12};
                })()""" % ms)
                assert abs(offset["y"]-expected) < .1 and offset["x"] == 0 and offset["rotate"] == 0, offset
                light_offset = window.evaluate_js("parseFloat(getComputedStyle(document.querySelector('.receipt-tear-light')).strokeDashoffset)")
                assert abs(light_offset-(10-100*(ms-1000)/1000)) < .1, light_offset
                continuity = window.evaluate_js("""(() => {
                    const r=document.querySelector('.usage-receipt-layout');r.paintTear();
                    const bridge=r.querySelector('.receipt-join');
                    const points=[...bridge.querySelector('polygon').points];
                    const edge=r.querySelector('.receipt-tear-edge polyline');
                    const light=r.querySelector('.receipt-tear-light');
                    const progress=(10-parseFloat(getComputedStyle(light).strokeDashoffset))/100;
                    const p=edge.getPointAtLength(edge.getTotalLength()*progress);
                    return {shown:getComputedStyle(bridge).display!=='none',
                        left:Math.min(...points.map(p=>p.x)),right:Math.max(...points.map(p=>p.x)),
                        tip:p.x,clip:r.querySelector('.receipt-tear-edge').style.clipPath};
                })()""")
                assert continuity['shown'] and abs(continuity['left']-continuity['tip'])<.1, continuity
                assert continuity['right']==1000 and continuity['clip'].startswith('inset('), continuity
            assert window.evaluate_js("""(() => {
                const rim=getComputedStyle(document.querySelector('.receipt-tear-light-outline'));
                const core=getComputedStyle(document.querySelector('.receipt-tear-light:not(.receipt-tear-light-outline)'));
                return parseFloat(rim.strokeWidth)===5 && parseFloat(core.strokeWidth)===2.5
                  && rim.filter==='none' && core.filter==='none';
            })()""")
            for ms, name in [(900, "feeding"), (1500, "tearing"), (2250, "stub"), (3000, "faded")]:
                window.evaluate_js("window.receiptPreviewAnimations.forEach(a=>a.currentTime=%d)" % ms)
                time.sleep(.08)
                capture(".tmp-receipt-" + name + ".png")
                opacity = window.evaluate_js("Number(getComputedStyle(document.querySelector('.receipt-stub')).opacity)")
                widths = window.evaluate_js("""({
                    paper:document.querySelector('.usage-paper').offsetWidth,
                    stub:document.querySelector('.receipt-stub').offsetWidth
                })""")
                if ms == 900:
                    feeding_width = widths["paper"]
                if ms == 2250:
                    assert .5 < opacity < 1, opacity
                    assert widths["paper"] == feeding_width == widths["stub"], widths
                    assert window.evaluate_js("document.querySelector('.usage-receipt-layout').dataset.phase") == "detached"
                if ms == 3000:
                    assert opacity < .15, opacity
            window.evaluate_js("window.receiptPreviewAnimations.forEach(a=>a.finish())")
            capture(".tmp-expenses.png")
            capture(".tmp-expenses-rings.png")
            scroll = window.evaluate_js("""(() => {
                const body=document.getElementById('sheet-body'),feed=document.querySelector('.receipt-feed');
                body.scrollTop=100;feed.scrollTop=300;
                return {page:body.scrollTop,paper:feed.scrollTop,fits:body.scrollHeight<=body.clientHeight};
            })()""")
            assert scroll["page"] == 0 and scroll["paper"] > 0 and scroll["fits"], scroll
            window.evaluate_js("document.querySelector('.usage-paper').click()")
            time.sleep(.3)
            assert window.evaluate_js("document.querySelector('.receipt-viewer img').naturalHeight>1000")
            window.evaluate_js("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))")
            window.evaluate_js("document.querySelector('.expense-category').click()")
            time.sleep(1.6)
            assert panel.category == "eat", panel.category
            assert not window.evaluate_js("!!document.querySelector('.expense-category-header .category-return')")
            assert window.evaluate_js("[...document.querySelectorAll('.expense-pie-label')].map(n=>n.textContent)") == ["午饭"]
            assert window.evaluate_js("document.querySelectorAll('.receipt-entry').length") == 7
            capture(".tmp-expenses-category.png")
            window.evaluate_js("document.querySelector('.expense-category.selected').click()")
            time.sleep(.2)
            assert panel.category is None
            window.evaluate_js("[...document.querySelectorAll('.expense-category')].find(b=>b.textContent.includes('token')).click()")
            time.sleep(1.4)
            assert panel.category == "token"
            assert window.evaluate_js("document.querySelectorAll('circle.expense-slice').length") == 1
            assert window.evaluate_js("document.querySelector('.receipt-subtitle').firstChild.textContent") == "token"
            assert window.evaluate_js("document.querySelector('.receipt-heading-count').textContent") == "700,000"
            assert window.evaluate_js("document.querySelector('.usage-paper').textContent.includes('700,000')")
            assert window.evaluate_js("document.querySelector('.usage-paper').textContent.includes('$10.00 USD')")
            capture(".tmp-expenses-token.png")
            protocol("Emulation.setDeviceMetricsOverride",
                     {"width": 430, "height": 800, "deviceScaleFactor": 1, "mobile": False})
            protocol("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-reduced-motion", "value": "reduce"}]})
            original_report = panel._report
            preview = expenses.report(config)
            preview["chart_slices"] = [
                {"name": name, "value": 1, "color": expenses.CHART_COLORS[i]}
                for i, name in enumerate(expenses.CATEGORIES)]
            try:
                panel._report = lambda: (preview, None)
                host.draw()
                assert window.evaluate_js("document.querySelectorAll('.expense-pie-label').length") == 9
                assert window.evaluate_js("""(() => {
                    return [...document.querySelectorAll('.expense-pie-leader')].every(n => {
                        const p=[...n.points], side=p[3].x>150 ? 1 : -1;
                        return p.length===4 && p[0].y===p[1].y && p[2].y===p[3].y
                          && p.every(point => (point.x-150)*side>=-0.001)
                          && p.slice(1).every((point,i) => (point.x-p[i].x)*side>=0);
                    });
                })()""")
                assert window.evaluate_js("""(() => {
                    const card=document.querySelector('.usage-metric').getBoundingClientRect();
                    return [...document.querySelectorAll('.expense-pie-label')].every(n => {
                        const b=n.getBoundingClientRect();
                        return b.left>=card.left && b.right<=card.right && b.bottom<=card.bottom;
                    });
                })()""")
                capture(".tmp-expenses-pie-labels.png")
            finally:
                panel._report = original_report
            panel.category = "health"
            host.draw()
            assert metrics()["amount"] == 0
            assert window.evaluate_js("document.querySelectorAll('.expense-slice').length") == 0
            assert metrics()["ring"] > 46  # reduced motion immediately shows the final share
            assert window.evaluate_js("!!document.querySelector('.receipt-smile')")
            assert window.evaluate_js("document.querySelector('.receipt-smile').tagName") == "IMG"
            assert window.evaluate_js("document.querySelector('.receipt-smile').src.startsWith('data:image/png;base64,')")
            assert window.evaluate_js("document.querySelector('.receipt-empty').nextElementSibling.textContent") == "Nicely done!"
            assert window.evaluate_js("document.getElementById('sheet-body').scrollHeight<=document.getElementById('sheet-body').clientHeight")
            assert window.evaluate_js("document.getElementById('sheet-body').scrollWidth<=document.getElementById('sheet-body').clientWidth")
            assert window.evaluate_js("(()=>{const c=document.querySelector('.expense-category-list');c.scrollTop=100;return c.scrollTop>0})()")
            capture(".tmp-expenses-zero.png")
            window.evaluate_js("document.querySelector('.usage-paper').click()")
            time.sleep(.4)
            assert window.evaluate_js("document.querySelector('.receipt-viewer img').naturalHeight>300")
            capture(".tmp-receipt-character-view.png")
            window.evaluate_js("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))")
            for period, expected in [("week", ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]),
                                     ("month", list("JFMAMJJASOND")),
                                     ("year", [str(datetime.now(expenses.TOKYO).year)])]:
                panel.period = period
                host.draw()
                assert window.evaluate_js("[...document.querySelectorAll('.expense-trend-labels span')].map(n=>n.textContent)") == expected
                if period == "year":
                    assert window.evaluate_js("document.querySelectorAll('.expense-bar').length") == 1
                    assert not window.evaluate_js("!!document.querySelector('.expense-trend polyline')")
                else:
                    assert window.evaluate_js("!!document.querySelector('.expense-trend polyline')")
                assert window.evaluate_js("document.getElementById('sheet-body').scrollHeight<=document.getElementById('sheet-body').clientHeight")
            panel.category = None
            host.draw()
            assert window.evaluate_js("document.querySelectorAll('.receipt-entry').length") == 3
            assert window.evaluate_js("Number(document.querySelector('.expense-bar').getAttribute('height'))") > 0
            capture(".tmp-expenses-years.png")
            panel.period = "month"
            host.draw()
            assert window.evaluate_js("document.querySelectorAll('.receipt-entry').length") == 3
            capture(".tmp-expenses-months.png")
            protocol("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-reduced-motion", "value": "no-preference"}]})
            for period in ("week", "month"):
                panel.period, panel.category = period, None
                host.draw()
                for elapsed, expected_width in [(0, 0), (300, 75), (600, 150), (1200, 300)]:
                    width = window.evaluate_js("""(() => {
                        const r=document.querySelector('.usage-receipt-layout');
                        r.startMetrics(performance.now()-(%d || -100)); r.stopMetrics();
                        return Number(r.querySelector('.expense-line-reveal').getAttribute('width'));
                    })()""" % elapsed)
                    assert abs(width-expected_width) < .2, (period, elapsed, width)
            panel.period, panel.category = "year", None
            host.draw()
            for elapsed, expected_height in [(0, 0), (300, 12), (600, 24), (1200, 48)]:
                height = window.evaluate_js("""(() => {
                    const r=document.querySelector('.usage-receipt-layout');
                    r.startMetrics(performance.now()-(%d || -100)); r.stopMetrics();
                    const bar=r.querySelector('.expense-bar');
                    if (Math.abs(Number(bar.getAttribute('y'))+Number(bar.getAttribute('height'))-56)>.01)
                        throw new Error('bar baseline moved');
                    return Number(bar.getAttribute('height'));
                })()""" % elapsed)
                assert abs(height-expected_height) < .1, (elapsed, height)
            panel.category = "eat"
            host.draw()
            time.sleep(.15)
            paused = window.evaluate_js("""(() => {
                window.closedReceipt = document.querySelector('.usage-receipt-layout');
                closeSheet(false);
                return window.closedReceipt.querySelector('.usage-number').textContent;
            })()""")
            time.sleep(.3)
            assert window.evaluate_js("window.closedReceipt.querySelector('.usage-number').textContent") == paused
            # Closing returns the same live core, including a rapid reopen mid-flight.
            protocol("Emulation.setEmulatedMedia", {"features": []})
            # Observe the actual animation frame: bridge latency can outlast a fixed sleep.
            window.evaluate_js("""(() => {
                window.auditCloseAt = null;
                const deadline = performance.now() + 5000;
                function closeDuringEntry() {
                    if (document.body.classList.contains('expenses-open') &&
                        expensesShift > 0 && expensesShift < 1) {
                        window.auditCloseAt = expensesShift;
                        document.getElementById('expenses-core-slot').click();
                        return;
                    }
                    if (performance.now() < deadline) requestAnimationFrame(closeDuringEntry);
                }
                requestAnimationFrame(closeDuringEntry);
            })()""")
            host.open(web_panels3.ExpensePanel(SimpleNamespace(_usage_config=lambda: config)))
            deadline = time.monotonic() + 6
            while window.evaluate_js("window.auditCloseAt === null") and time.monotonic() < deadline:
                time.sleep(.05)
            assert window.evaluate_js("window.auditCloseAt > 0 && window.auditCloseAt < 1")
            assert window.evaluate_js("document.getElementById('sheet').hidden && !document.body.classList.contains('expenses-open')")
            time.sleep(1)
            assert window.evaluate_js("expensesShift===0 && !document.body.classList.contains('expenses-core-active')")
            assert window.evaluate_js("getComputedStyle(document.getElementById('status-dock')).visibility==='visible'")
            host.open(web_panels3.ExpensePanel(SimpleNamespace(_usage_config=lambda: config)))
            protocol("Emulation.clearDeviceMetricsOverride", {})
            window.resize(640,560)
            time.sleep(1)
            assert window.evaluate_js("(()=>{const r=document.getElementById('sheet').getBoundingClientRect();return r.left===0 && r.top===0 && r.width===innerWidth && r.height===innerHeight})()")
            capture(".tmp-expenses-fullscreen-narrow.png")
            assert window.evaluate_js("document.getElementById('sheet-body').scrollWidth<=document.getElementById('sheet-body').clientWidth && document.getElementById('sheet-body').scrollHeight<=document.getElementById('sheet-body').clientHeight"), window.evaluate_js("({w:innerWidth,h:innerHeight,rows:['#sheet-body','.expense-layout','.usage-metrics','.receipt-printer','.expense-nav'].map(s=>{const e=document.querySelector(s);return {s,rect:e.getBoundingClientRect().toJSON(),scroll:[e.scrollWidth,e.scrollHeight],client:[e.clientWidth,e.clientHeight]}})})")
            window.evaluate_js("document.getElementById('expenses-core-slot').click()")
            host.open(web_panels3.ExpensePanel(SimpleNamespace(_usage_config=lambda: config)))
            time.sleep(.25)
            window.evaluate_js("closeSheet(false);showConfirm({id:'check',title:'Check',text:'Confirmation'})")
            assert window.evaluate_js("!document.getElementById('sheet').classList.contains('usage-sheet') && !document.body.classList.contains('expenses-open')")
            assert window.evaluate_js("getComputedStyle(document.getElementById('sheet-close')).display!=='none'")
            result["passed"] = True
        except Exception as error:
            import traceback
            result["error"] = traceback.format_exc()
        finally:
            window.destroy()
    webview.start(inspect, gui="edgechromium")
    print(json.dumps(result, ensure_ascii=False), flush=True)
    assert result.get("passed"), result


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="myn-expense-ui-") as directory:
        config = Path(directory)
        seed(config)
        classic(config)
        browser(config)
