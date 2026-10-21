"""Real isolated WebView2 report reader; no cloud calls or personal data."""
import json
import sys
import tempfile
import time
import traceback
from pathlib import Path

BASE=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(BASE/'src'),str(BASE/'src/core')]
from media_bridge import MediaBridge


def main():
    import webview
    result={}
    with tempfile.TemporaryDirectory(prefix='myn-report-ui-') as folder:
        events=[]
        class Api(MediaBridge):
            def emit(self,message):
                events.append(message)
        api=Api(folder)
        window=webview.create_window('MYN report verification',(BASE/'src/web/index.html').as_uri(),
                                    js_api=api,width=1040,height=720,hidden=True)
        def inspect():
            try:
                assert window.events.loaded.wait(20)
                api._attach_media(window)
                window.evaluate_js("document.body.classList.remove('intro')")
                text='每周汇报\n\n1. [功能更新 · 已启用] 播放器确认修复\n\n2. [交流偏好 · 候选] 回答简短\n\n来源：用户原话；计划不代表已完成。'
                window.evaluate_js('MYN.receive('+json.dumps(dict(cmd='reader',action='report',key='test-report',text=text))+')')
                time.sleep(.3)
                assert window.evaluate_js('!reader.hidden && readerKey === "test-report"')
                assert window.evaluate_js('readerBody.textContent.includes("候选")')
                assert window.evaluate_js('readerText')==text
                assert any(e.get('event')=='reader' and e.get('key')=='test-report' and e.get('open') for e in events)
                # Paging reuses the same reader and updates its revision/text.
                window.evaluate_js('MYN.receive('+json.dumps(dict(cmd='reader',action='report',key='page-2',text='21. 下一页依据'))+')')
                assert window.evaluate_js('readerText')=='21. 下一页依据'
                window.evaluate_js('MYN.receive({cmd:"reader",action:"close"})')
                assert window.evaluate_js('reader.hidden')
                result['passed']=True
            except Exception:
                result['error']=traceback.format_exc()
            finally:
                api._close_media()
                window.destroy()
        webview.start(inspect,gui='edgechromium',private_mode=True)
    print(json.dumps(result,ensure_ascii=False))
    if not result.get('passed'):
        raise SystemExit(1)


if __name__=='__main__':
    main()
