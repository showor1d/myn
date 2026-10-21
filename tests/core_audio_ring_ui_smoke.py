"""Real WebView2 geometry and loudness animation with isolated, controlled signals."""
import base64
import json
import sys
import tempfile
import time
import traceback
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE/'src'), str(BASE/'src/core')]
from media_bridge import MediaBridge


def main():
    import webview
    with tempfile.TemporaryDirectory(prefix='myn-core-ring-') as folder:
        class Api(MediaBridge):
            output = 0
            def emit(self, value): pass
            def core_audio_levels(self):
                return dict(output=self.output, available=True, bands=self.bands)
            bands = [0.] * 128
        api = Api(Path(folder)/'media')
        window = webview.create_window('Core audio verification', (BASE/'src/web/index.html').as_uri(),
                                      js_api=api, width=1040, height=780)
        result = {}
        def check():
            def wait(expr, seconds=12):
                print('WAIT',expr,flush=True)
                end=time.monotonic()+seconds
                while time.monotonic()<end:
                    value=window.evaluate_js(expr)
                    if value: return value
                    time.sleep(.05)
                raise AssertionError(expr)
            def capture(name):
                data=window.evaluate_js('canvas.toDataURL()')
                (BASE/name).write_bytes(base64.b64decode(data.split(',',1)[1]))
            def mini(slot):
                state=window.evaluate_js('CoreAudioRing.state()')
                rect=window.evaluate_js(f'(()=>{{const r=document.getElementById("{slot}").getBoundingClientRect();return {{x:r.x+r.width/2,y:r.y+r.height/2,w:r.width,h:r.height}};}})()')
                assert state['scale']<.3, state
                assert abs(state['x']-rect['x'])<2 and abs(state['y']-rect['y'])<2, (state,rect)
                # Larger waves may extend beyond the core slot; controls remain
                # usable because the visual canvas never becomes a hit target.
                assert rect['h']/2 < state['radius'] < rect['h']*.75, (state,rect)
                assert state['level']>.3
                return {k:state[k] for k in ('x','y','radius','scale','level')}
            try:
                assert window.events.loaded.wait(20)
                wait('!!window.CoreAudioRing && !!window.pywebview?.api?.core_audio_levels')
                window.evaluate_js("intro.done=true;document.body.classList.remove('intro');document.body.style.background='#172937'")
                wait('CoreAudioRing.state().scale===1')
                time.sleep(5)
                window.evaluate_js("window._micSignal=setInterval(()=>MYN.receive({cmd:'level',value:.4}),50)")
                wait('CoreAudioRing.state().level>.6')
                result['microphone_idle']=True
                window.evaluate_js('clearInterval(_micSignal)')
                wait('CoreAudioRing.state().level===0')
                api.output=.3
                # Real FFT of a reproducible multi-tone PCM frame, not visual sine waves.
                from audio_spectrum import Spectrum, SIZE
                import math
                spectrum = Spectrum()
                spectrum.feed([sum(.012*math.sin(2*math.pi*f*i/48000)
                                   for f in (120, 180, 270, 410, 640, 900, 1300, 2000, 3300, 5100, 7600))
                               for i in range(SIZE)], 48000)
                api.bands = spectrum.bands()
                wait('CoreAudioRing.state().level>.6')
                wait('Math.max(...CoreAudioRing.state().bands)>.4')
                capture('.tmp-core-ring-main.png')
                result['system_playback_idle']=True
                for key in ('usage','agents','audio_console'):
                    window.evaluate_js('showSheet('+json.dumps(dict(key=key,body=[]))+');true')
                    wait('expensesShift===1')
                    result[key]=mini('expenses-core-slot')
                    if key=='audio_console': capture('.tmp-core-ring-mini.png')
                    window.evaluate_js("document.getElementById('expenses-core-slot').click()")
                    wait('expensesShift===0')
                window.evaluate_js('void MediaPlayer.open()')
                wait('musicShift===1')
                result['music']=mini('media-core-slot')
                window.evaluate_js("document.getElementById('media-core-slot').click()")
                wait('musicShift===0')
                api.output=0
                wait('CoreAudioRing.state().level===0')
                result['silence_settles']=True
                result['passed']=True
            except Exception:
                result['error']=traceback.format_exc()
            finally:
                window.destroy()
        webview.start(check,gui='edgechromium',private_mode=True)
        api._close_media()
        print(json.dumps(result,ensure_ascii=False))
        return 0 if result.get('passed') else 1


if __name__=='__main__': raise SystemExit(main())
