"""Real WebView2 and Tk merged settings, isolated and without cloud or microphone access."""
import json
import base64
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path
from unittest.mock import Mock, patch

BASE=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(BASE/'src'),str(BASE/'src/core')]


def main():
    with tempfile.TemporaryDirectory(prefix='myn-agents-merged-') as folder:
        config=Path(folder)/'config'
        database=Path(folder)/'database/jarvis.db'
        database.parent.mkdir()
        os.environ.update(MYN_PERSONAL_DATA_DIR=folder,MYN_CONFIG_DIR=str(config),MYN_DATA_DIR=str(database.parent))
        import myn_gui
        import sheets
        import stt
        import tts
        import webview
        import tkinter as tk
        from settings_navigation import AGENTS_SECTIONS
        assert AGENTS_SECTIONS==()
        win=myn_gui.MYNWindow.__new__(myn_gui.MYNWindow)
        win.session=myn_gui.ChatSession(database)
        win.web=Mock()
        win.web_active=False
        win.tts_settings=tts.load_settings(config)
        win.stt_settings=stt.load_settings(config)
        win._stt_job=Mock()
        win._report_display=Mock()
        win.font_family='Segoe UI'
        win.root=tk.Tk()
        win.root.withdraw()
        result={}
        forbidden=['Pause automatic learning','Apply preferences in chat','Read new replies automatically','Read only the opening of long replies','Always-on microphone']
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        with patch('stt.missing_parts',return_value=['test']),patch('tts_player.output_devices',return_value=[]):
            assert win._interface_control('agents')['ok']
            win.root.update()
            widgets=list(descendants(win.root))
            notebooks=[w for w in widgets if w.winfo_class()=='TNotebook']
            assert not notebooks
            texts=[str(w.cget('text')) for w in widgets if 'text' in w.keys()]
            assert not any(t in texts for t in forbidden+['Backups','About you','Default model'])
            assert 'Opus 5.5' in texts
            win.open_about()
            assert 'About you' in win._report_display.call_args[0][0]
            win.open_personal_library()
            assert 'Data overview' in win._report_display.call_args[0][0]
        win.root.destroy()
        win.web_active=True
        class Api:
            def emit(self,message):
                if message.get('event') in ('sheet','sheet_closed','menu'):
                    win._web_event(message)
        window=webview.create_window('Agents verification',(BASE/'src/web/index.html').as_uri(),
                                    js_api=Api(),width=1040,height=780)
        win._sheets=sheets.SheetHost(lambda cmd:window.evaluate_js('MYN.receive('+json.dumps(cmd,ensure_ascii=False)+')'))
        def inspect():
            try:
                assert window.events.loaded.wait(20)
                window.evaluate_js("document.body.classList.remove('intro');document.body.style.background='#172937';document.dispatchEvent(new PointerEvent('pointerdown'))")
                window.evaluate_js("document.getElementById('agents-shortcut').click()")
                def wait(expr):
                    end=time.monotonic()+8
                    while time.monotonic()<end:
                        if window.evaluate_js(expr): return
                        time.sleep(.05)
                    raise AssertionError(expr)
                wait("document.getElementById('sheet-title').textContent==='Agents'")
                wait("expensesShift>0")
                assert window.evaluate_js("expensesShift<1")
                assert window.evaluate_js("(()=>{const r=document.getElementById('sheet').getBoundingClientRect();return r.x===0 && r.y===0 && r.width===innerWidth && r.height===innerHeight})()")
                assert window.evaluate_js("getComputedStyle(document.getElementById('sheet-title')).display==='none' && getComputedStyle(document.getElementById('sheet-close')).display==='none'")
                assert window.evaluate_js("!document.getElementById('gear') && !document.getElementById('menu')")
                text=window.evaluate_js("document.getElementById('sheet-body').textContent")
                assert not any(t in text for t in forbidden)
                assert 'Microphone' in text and 'Output device' in text
                assert 'Hey, Mine' in text and 'save automatically' in text
                assert not window.evaluate_js("!!document.querySelector('input[data-id=wake_words]')")
                assert not window.evaluate_js("!!document.querySelector('[data-id=read_path],[data-id=main_model],[data-id=auto_read],[data-id=always_listen]')")
                assert not window.evaluate_js("!!document.querySelector('[data-id=agents_tab]')")
                wait("document.getElementById('sheet-body').textContent.includes('Opus 5.5')")
                assert not win.sheets.last_error,win.sheets.last_error
                wait("expensesShift===1")
                assert window.evaluate_js("(()=>{const r=document.getElementById('expenses-core-slot').getBoundingClientRect();return r.width===96 && r.height===88 && r.x===20 && r.y===8 && ctx.getTransform().a<.3})()")
                assert window.evaluate_js("(()=>{const e=document.getElementById('sheet-body');e.scrollTop=e.scrollHeight;return e.scrollTop>0 && e.scrollWidth<=e.clientWidth})()")
                window.evaluate_js("document.getElementById('sheet-body').scrollTop=0")
                from webview.platforms.winforms import BrowserView
                from System import Func, Object
                native=BrowserView.instances[window.uid]
                task=native.webview.Invoke(Func[Object](lambda: native.webview.CoreWebView2.CallDevToolsProtocolMethodAsync('Page.captureScreenshot','{"format":"png"}')))
                (BASE/'.tmp-agents-fullscreen.png').write_bytes(base64.b64decode(json.loads(str(task.Result))['data']))
                win.sheets.draw()
                assert window.evaluate_js("expensesShift===1")
                window.evaluate_js("document.getElementById('expenses-core-slot').click()")
                wait("document.getElementById('sheet').hidden && !document.body.classList.contains('core-sheet-open')")
                wait("expensesShift===0 && !document.body.classList.contains('expenses-core-active')")
                window.resize(640,560)
                window.evaluate_js("document.getElementById('agents-shortcut').click()")
                wait("expensesShift===1")
                assert window.evaluate_js("(()=>{const r=document.getElementById('expenses-core-slot').getBoundingClientRect(),b=document.getElementById('sheet-body');return r.width===80 && r.height===74 && b.scrollWidth<=b.clientWidth && b.clientHeight>300})()")
                window.evaluate_js("document.getElementById('expenses-core-slot').click()")
                result['passed']=True
            except Exception:
                result['error']=traceback.format_exc()
            finally:
                window.destroy()
        with patch('stt.missing_parts',return_value=['test']),patch('tts_player.output_devices',return_value=[]):
            webview.start(inspect,gui='edgechromium',private_mode=True)
        print(json.dumps(result,ensure_ascii=False))
        return 0 if result.get('passed') else 1

if __name__=='__main__': raise SystemExit(main())
