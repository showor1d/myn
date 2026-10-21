"""Real WebView2 alarm hover/toggle round trip against a temporary config."""
import base64
import json
import sys
import tempfile
import time
import threading
import traceback
from pathlib import Path
from unittest.mock import Mock, patch
from datetime import timedelta

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src/core")]
from media_bridge import MediaBridge
from gui_timers import TimersMixin
from gui_webview import WebViewMixin
from gui_frameless import FramelessMixin
import timekeeper
import timer_display
import webview


def main():
    with tempfile.TemporaryDirectory(prefix="myn-alarm-ui-") as folder:
        config = Path(folder)
        now = timekeeper.local_now()
        when = now + timedelta(hours=1)
        alarm = timekeeper.add_task(config, now=now, category="alarm", kind="say",
            repeat="daily", time=when.strftime("%H:%M"), text="起床 <img src=x>")
        paused = timekeeper.add_task(config, now=now, category="alarm", kind="say",
            repeat="weekdays", time="09:00", text="上课", enabled=False)
        class Controller(TimersMixin, WebViewMixin, FramelessMixin):
            pass

        controller = Controller()
        controller.session = Mock(db_path=config / "jarvis.db")
        controller.web_active = True
        controller.sheets = Mock()
        controller.sheets.event.return_value = False
        controller.entry = Mock()
        controller.entry.get.return_value = ""
        controller.input_visible = False
        controller.input_row = Mock()
        controller._place_input = Mock()
        controller._queue_chat = Mock()
        result = {}
        dock_events = []

        class Api(MediaBridge):
            def emit(self, message):
                dock_events.append(message)
                if message.get('event') == 'alarm_settings':
                    threading.Thread(target=controller._music_alarm_settings, args=(message,), daemon=True).start()
                elif message.get("event", "").startswith("alarm_"):
                    controller._alarm_mini_event(message)
                elif message.get("event") in ("toggle_input", "hide_input"):
                    controller._web_event(message)

        api = Api(config / "media")
        window = webview.create_window("MYN alarm verification",
            (ROOT / "src/web/index.html").as_uri(), js_api=api, width=1040, height=720)
        controller.web = Mock()
        controller.web.js = lambda payload: window.evaluate_js(
            "MYN.receive(" + json.dumps(payload, ensure_ascii=False) + ")")

        def inspect():
            try:
                assert window.events.loaded.wait(20)
                api._attach_media(window)
                window.evaluate_js("document.body.classList.remove('intro');document.body.style.background='#172937';document.body.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true}))")
                controller._timers_changed()
                assert window.evaluate_js("!document.querySelector('#menu [data-item=usage]')")
                assert window.evaluate_js("(()=>{const d=document.getElementById('status-dock').getBoundingClientRect(),a=document.getElementById('agents-shortcut').getBoundingClientRect(),b=document.querySelector('.alarm-indicator').getBoundingClientRect();return a.left>b.left && a.bottom<=b.top})()")
                assert window.evaluate_js("(()=>{const a=document.querySelector('.alarm-indicator').getBoundingClientRect(),e=document.getElementById('expenses-shortcut').getBoundingClientRect(),m=document.getElementById('media-mini-note').getBoundingClientRect();return a.bottom<=e.top && e.bottom<=m.top && e.width>0})()")
                def check_arc(count):
                    expression = """(()=>{
                      const nodes=[...document.getElementById('status-dock').children];
                      const cx=innerWidth/2,cy=innerHeight/2-3;
                      const radius=Math.min(innerWidth*.21,innerHeight*.27,158)*1.22+42;
                      return nodes.length===COUNT && nodes.every((el,i)=>{
                        const r=el.getBoundingClientRect(),x=(r.left+r.right)/2,y=(r.top+r.bottom)/2;
                        const expected=(-36+72*i/(nodes.length-1))*Math.PI/180;
                        return Math.abs(x-(cx-radius*Math.cos(expected)))<1 &&
                          Math.abs(y-(cy+radius*Math.sin(expected)))<1 && r.left>=8 && r.bottom<innerHeight;
                      });
                    })()""".replace('COUNT',str(count))
                    end = time.monotonic() + 3
                    while time.monotonic() < end:
                        if window.evaluate_js(expression):
                            return
                        time.sleep(.05)
                    raise AssertionError(window.evaluate_js("({w:innerWidth,h:innerHeight,reading:document.body.classList.contains('reading'),nodes:[...document.getElementById('status-dock').children].map(el=>({id:el.id,rect:el.getBoundingClientRect().toJSON()}))})"))
                check_arc(6)
                window.evaluate_js("document.getElementById('expenses-shortcut').click()")
                time.sleep(.1)
                assert any(e.get('event')=='menu' and e.get('item')=='usage' for e in dock_events)
                assert window.evaluate_js("(()=>{const a=getComputedStyle(document.querySelector('.alarm-indicator')),m=getComputedStyle(document.getElementById('media-mini-note'));return ['width','height','borderRadius','padding'].every(k=>a[k]===m[k])})()")

                def wait(expression):
                    end = time.monotonic() + 10
                    while time.monotonic() < end:
                        if window.evaluate_js(expression):
                            return
                        time.sleep(.05)
                    raise AssertionError(expression)

                def enter():
                    window.evaluate_js("document.querySelector('.alarm-indicator').dispatchEvent(new PointerEvent('pointerenter'))")

                def leave():
                    window.evaluate_js("document.querySelector('.alarm-indicator').dispatchEvent(new PointerEvent('pointerleave'))")

                assert window.evaluate_js("!document.querySelector('[data-item=input]')")
                def check_keyboard():
                    assert window.evaluate_js("document.getElementById('keyboard-shortcut').getBoundingClientRect().top >= document.getElementById('media-mini-note').getBoundingClientRect().bottom")
                    window.evaluate_js("document.getElementById('keyboard-shortcut').click()")
                    wait("!document.getElementById('bar').hidden && document.getElementById('keyboard-shortcut').getAttribute('aria-pressed')==='true'")
                    assert controller.input_visible
                    wait("document.getElementById('bar').contains(document.activeElement)")
                    window.evaluate_js("document.getElementById('keyboard-shortcut').click()")
                    wait("document.getElementById('bar').hidden && document.getElementById('keyboard-shortcut').getAttribute('aria-pressed')==='false'")
                    assert not controller.input_visible
                check_keyboard()

                enter()
                time.sleep(.12)
                assert window.evaluate_js("document.getElementById('alarm-mini-box').hidden")
                wait("!document.getElementById('alarm-mini-box').hidden")
                wait("document.querySelectorAll('.alarm-mini-row').length===2")
                assert window.evaluate_js("(()=>{const p=document.getElementById('alarm-mini-box').getBoundingClientRect(),a=document.querySelector('.alarm-indicator').getBoundingClientRect();return p.left>=8 && p.right<a.left && p.bottom<=innerHeight-7})()")
                time.sleep(1.2)
                assert window.evaluate_js("!document.getElementById('alarm-mini-box').hidden")
                # Switching icons closes the other panel immediately, before the hover delay.
                window.evaluate_js("document.getElementById('media-mini-note').dispatchEvent(new PointerEvent('pointerenter'))")
                assert window.evaluate_js("document.getElementById('alarm-mini-box').hidden")
                wait("!document.getElementById('media-mini-box').hidden")
                wait("(()=>{const p=document.getElementById('media-mini-box').getBoundingClientRect(),a=document.getElementById('media-mini-note').getBoundingClientRect();return p.left>=8 && p.right<a.left && p.bottom<=innerHeight-7})()")
                enter()
                assert window.evaluate_js("document.getElementById('media-mini-box').hidden")
                wait("!document.getElementById('alarm-mini-box').hidden")
                # Cancel an unfinished hover too, so its timer cannot reopen a competing panel.
                window.evaluate_js("document.getElementById('media-mini-note').dispatchEvent(new PointerEvent('pointerenter'))")
                enter()
                time.sleep(.45)
                assert window.evaluate_js("document.getElementById('media-mini-box').hidden && !document.getElementById('alarm-mini-box').hidden")
                assert not window.evaluate_js("!!document.querySelector('#alarm-mini-box img')")
                leave()
                time.sleep(.3)
                assert window.evaluate_js("!document.getElementById('alarm-mini-box').hidden")
                window.evaluate_js("document.getElementById('alarm-mini-box').dispatchEvent(new PointerEvent('pointerenter'))")
                time.sleep(1.1)
                assert window.evaluate_js("!document.getElementById('alarm-mini-box').hidden")
                # Real GUI handler writes temp config; checked state changes only on receipt.
                window.evaluate_js("document.querySelector('.alarm-mini-toggle[aria-checked=true]').click()")
                wait("document.querySelectorAll('.alarm-mini-toggle[aria-checked=true]').length===0")
                assert not timekeeper.get_task(config, alarm["id"])["enabled"]
                assert window.evaluate_js("!document.getElementById('alarm-mini-box').hidden")
                # Icon retained while panel open, so it remains possible to turn it back on.
                window.evaluate_js("document.querySelector('.alarm-mini-toggle').click()")
                wait("document.querySelectorAll('.alarm-mini-toggle[aria-checked=true]').length===1")
                assert any(t["enabled"] for t in timekeeper.tasks(config))
                wait("(()=>{const r=document.getElementById('alarm-mini-box').getBoundingClientRect();return r.left>=8 && r.right<=innerWidth-8})()")
                time.sleep(.2)
                result['popup_geometry'] = window.evaluate_js("(()=>{const p=document.getElementById('alarm-mini-box');return {rect:p.getBoundingClientRect().toJSON(),style:p.getAttribute('style'),scrollX}})()")
                from webview.platforms.winforms import BrowserView
                from System import Func, Object
                native = BrowserView.instances[window.uid]
                task = native.webview.Invoke(Func[Object](lambda:
                    native.webview.CoreWebView2.CallDevToolsProtocolMethodAsync("Page.captureScreenshot", '{"format":"png","fromSurface":false,"captureBeyondViewport":false}')))
                if not task.Wait(10000):
                    raise TimeoutError('Alarm screenshot timed out')
                (ROOT / ".tmp-alarm-mini.png").write_bytes(base64.b64decode(json.loads(str(task.Result))["data"]))
                window.evaluate_js("document.getElementById('alarm-mini-box').dispatchEvent(new PointerEvent('pointerleave'))")
                assert window.evaluate_js("document.getElementById('alarm-mini-box').hidden")
                enter()
                wait("!document.getElementById('alarm-mini-box').hidden")
                leave()
                time.sleep(1.15)
                wait("document.getElementById('alarm-mini-box').hidden")
                # No upcoming alarm: icon absent after closing; music still present.
                # Entering the player cancels pending alarm hover, and hides both dock icons.
                enter()
                window.evaluate_js("document.getElementById('media-mini-note').click()")
                wait("!document.getElementById('media-player').hidden")
                time.sleep(.45)
                assert window.evaluate_js("document.getElementById('alarm-mini-box').hidden && getComputedStyle(document.querySelector('.alarm-indicator')).display==='none' && document.getElementById('media-mini').hidden")
                window.evaluate_js("document.getElementById('media-core-slot').click()")
                wait("document.getElementById('media-player').hidden")
                assert window.evaluate_js("getComputedStyle(document.querySelector('.alarm-indicator')).display!=='none'")
                enter()
                wait("!document.getElementById('alarm-mini-box').hidden")
                window.evaluate_js("document.getElementById('alarm-mini-box').dispatchEvent(new PointerEvent('pointerleave'))")
                for item in timekeeper.tasks(config):
                    timekeeper.update_task(config, item["id"], enabled=False)
                controller._timers_changed()
                assert window.evaluate_js("!document.querySelector('.alarm-indicator')")
                assert window.evaluate_js("document.getElementById('expenses-shortcut').getBoundingClientRect().height>0")
                assert window.evaluate_js("!document.getElementById('media-mini').hidden")
                check_keyboard()

                check_arc(5)
                window.resize(640,560)
                time.sleep(.8)
                check_arc(5)
                window.evaluate_js("document.getElementById('media-mini-note').dispatchEvent(new PointerEvent('pointerenter'))")
                wait("!document.getElementById('media-mini-box').hidden")
                assert window.evaluate_js("(()=>{const p=document.getElementById('media-mini-box').getBoundingClientRect();return p.left>=7 && p.top>=7 && p.right<=innerWidth-7 && p.bottom<=innerHeight-7})()")
                window.evaluate_js("MediaPlayer.closeMini();document.body.classList.add('reading')")
                time.sleep(2.2)
                assert window.evaluate_js("[...document.getElementById('status-dock').children].every(el=>{const r=el.getBoundingClientRect();return r.left>=8 && r.right<innerWidth*.42 && r.top>=8 && r.bottom<innerHeight})")
                window.evaluate_js("document.body.classList.remove('reading')")
                result["passed"] = True
            except Exception:
                result["error"] = traceback.format_exc()
            finally:
                api._close_media()
                window.destroy()

        with patch("gui_timers.config_for_db", return_value=config):
            webview.start(inspect, gui="edgechromium", private_mode=True)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("passed") else 1


if __name__ == "__main__":
    sys.exit(main())
