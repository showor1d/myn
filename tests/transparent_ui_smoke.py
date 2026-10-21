"""Real clear-glass WebView2 check; --interactive leaves an isolated UI for mouse tests."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import webview
import web_window

RESULT = {}
original_start = webview.start
backdrop = None


def inspect_window(window):
    try:
        window.events.loaded.wait(20)
        # Do not repair initialization from the test; observe production setup.
        RESULT["transparent_host"] = bool(window.transparent)
        RESULT["native_color_key_empty"] = bool(window.native.TransparencyKey.IsEmpty)
        RESULT["clear_glass"] = bool(getattr(window, "_myn_glass", False))
        from System import Action
        def read_alpha():
            RESULT["webview_alpha"] = int(window.native.webview.DefaultBackgroundColor.A)
        window.native.Invoke(Action(read_alpha))
        RESULT["page"] = window.evaluate_js("""(() => {
          document.body.classList.remove('intro');
          showReader('check', '透明阅读框\\n' + '用于检查背景透明与文字显示。'.repeat(25), false);
          
          const menuOpened = !document.getElementById('gear') && !document.getElementById('menu');
          for (const cls of ['ui-card','ui-group','ui-chart','ui-table-wrap','ui-tip']) {
            const node = document.createElement('div'); node.className = cls;
            document.getElementById('sheet-body').appendChild(node);
          }
          const selectors = ['html','body','#bar','#reader','#ask','#sheet',
            '#sheet-modal','#sheet-modal-box','#sheet-toast','.ui-card','.ui-group',
            '.ui-chart','.ui-table-wrap','.ui-tip','input','button'];
          const backgrounds = Object.fromEntries(selectors.map(s => [s,
            [...document.querySelectorAll(s)].map(e => getComputedStyle(e).backgroundColor)]));
          document.getElementById('reader-close').click();
          return {backgrounds, readerClosed: document.getElementById('reader').hidden,
            menuOpened,
            plate: getComputedStyle(document.getElementById('plate')).display,
            video: getComputedStyle(document.getElementById('bgvideo')).display,
            dimmer: getComputedStyle(document.body,'::after').backgroundImage,
            blur: getComputedStyle(document.getElementById('status-dock')).backdropFilter};
        })()""")
        assert not RESULT["transparent_host"] and RESULT["native_color_key_empty"]
        assert RESULT["clear_glass"]
        assert RESULT["webview_alpha"] == 0
        page = RESULT["page"]
        for selector in ("#reader", "#sheet", "#bar"):
            assert page["backgrounds"][selector][0].startswith("rgba(")
            assert page["backgrounds"][selector][0] != "rgba(0, 0, 0, 0)"
        assert page["plate"] == page["video"] == "none"
        assert page["blur"] == "none"
        assert page["readerClosed"] and page["menuOpened"]
        RESULT["passed"] = True
        if "--interactive" in sys.argv:
            if backdrop is not None:
                window.move(240, 140)
                window.show()
            window.set_title("MYN Glass Interaction Check")
            window.evaluate_js("""MYN.receive({cmd:'input',visible:true});
              document.addEventListener('pointerup', e => setTimeout(() => emit({event:'test_focus',
                target:e.target.id, active:document.activeElement.id}), 200));
              showReader('check2', '# 玻璃阅读框\\n背景保留玻璃质感，文字保持清晰。\\n\\n' +
                '这是独立测试窗口，不会调用模型或保存个人数据。\\n\\n'.repeat(14), false);""")
            print("READY FOR REAL MOUSE / KEYBOARD CHECK", flush=True)
            window.events.closed.wait(900)
    except Exception as error:
        RESULT["error"] = repr(error)
    finally:
        if backdrop is not None:
            backdrop.destroy()
        window.destroy()


def start(_serve, window, **kwargs):
    global backdrop
    if "--backdrop" in sys.argv:
        backdrop = webview.create_window("MYN clarity test backdrop", x=220, y=120,
            width=1080, height=720, html='''<body style="margin:0;color:white;
            background:repeating-conic-gradient(#283f77 0 25%,#be693c 0 50%) 0/80px 80px;
            font:32px Arial">BACKGROUND CLARITY 123<br>BACKGROUND CLARITY 456</body>''')
    original_start(inspect_window, window, **kwargs)


if __name__ == "__main__":
    webview.start = start
    web_window.main()
    print(json.dumps(RESULT, ensure_ascii=False))
    sys.exit(0 if RESULT.get("passed") else 1)
