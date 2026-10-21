"""Real WebView2 DSP and UI, synthetic audio only, isolated preferences."""
import base64
import json
import math
import os
import struct
import sys
import tempfile
import time
import traceback
import wave
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE/'src'), str(BASE/'src/core')]
from media_bridge import MediaBridge


def main():
    os.environ['WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS']='--autoplay-policy=no-user-gesture-required'
    import webview
    with tempfile.TemporaryDirectory(prefix='myn-audio-console-') as folder:
        class Api(MediaBridge):
            def emit(self, value):
                pass
        api = Api(Path(folder)/'media')
        audio = api.library.folders['music']/'tone.wav'
        with wave.open(str(audio), 'wb') as stream:
            stream.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            stream.writeframes(b''.join(struct.pack('<h', int(500*math.sin(2*math.pi*440*i/24000))) for i in range(24000*20)))
        window = webview.create_window('Audio console verification', (BASE/'src/web/index.html').as_uri(),
                                      js_api=api, width=1040, height=780)
        result = {}
        def check():
            def wait(expr, seconds=15):
                end=time.monotonic()+seconds
                while time.monotonic()<end:
                    value=window.evaluate_js(expr)
                    if value: return value
                    time.sleep(.05)
                raise AssertionError(expr+'; '+str(window.evaluate_js('window._failure || document.getElementById("sheet-body").textContent')))
            try:
                assert window.events.loaded.wait(20)
                api._attach_media(window)
                window.evaluate_js("window.addEventListener('unhandledrejection',e=>window._failure=String(e.reason?.stack||e.reason));document.body.classList.remove('intro');document.body.style.background='#172937';document.dispatchEvent(new PointerEvent('pointerdown'))")
                wait("!!window.pywebview?.api?.audio_console_settings")
                window.evaluate_js("document.getElementById('equalizer-shortcut').click()")
                wait("!!document.querySelector('.audio-console select')")
                assert window.evaluate_js("document.body.classList.contains('core-sheet-open') && document.getElementById('sheet').clientWidth===innerWidth")
                assert not window.evaluate_js('AudioConsole.state().capturing || AudioConsole.state().sending')
                # Real offline filters: 60Hz should be amplified by the low shelf.
                window.evaluate_js("""(async()=>{try{
                  async function rms(boost){const c=new OfflineAudioContext(1,48000,48000),o=c.createOscillator(),eq=AudioEffects.equalizer(c);
                    o.frequency.value=40;eq.bands[0].gain.value=boost;o.connect(eq.input);eq.output.connect(c.destination);o.start();
                    const b=(await c.startRendering()).getChannelData(0);return Math.sqrt(b.slice(12000).reduce((s,x)=>s+x*x,0)/36000);}
                  const plain=await rms(0),raised=await rms(6);window._eqRatio=raised/plain;
                  const c=new OfflineAudioContext(1,96000,48000);await c.audioWorklet.addModule('voice_effect_worklet.js');
                  const o=c.createOscillator();o.frequency.value=440;
                  const w=new AudioWorkletNode(c,'myn-voice-effect',{processorOptions:{effect:'pitch',pitch:12}});
                  o.connect(w).connect(c.destination);o.start();const b=(await c.startRendering()).getChannelData(0);
                  let crossings=0;for(let i=24001;i<72000;i++)if(b[i-1]<=0&&b[i]>0)crossings++;
                  window._pitchHz=crossings;
                }catch(e){window._failure=e.stack;}})()""")
                wait('window._pitchHz || window._failure')
                assert not window.evaluate_js('window._failure'), window.evaluate_js('window._failure')
                result['eq_gain_ratio']=window.evaluate_js('window._eqRatio')
                result['pitch_hz']=window.evaluate_js('window._pitchHz')
                assert 1.5 < result['eq_gain_ratio'] < 2.1
                assert 820 < result['pitch_hz'] < 940
                # Real media element through Web Audio, local output muted for the test.
                window.evaluate_js("document.getElementById('expenses-core-slot').click();void MediaPlayer.command({action:'play',request:'test'})")
                wait("!document.getElementById('media-audio-output').paused && AudioConsole.state().context==='running'")
                wait('AudioConsole.state().musicPeak > .001')
                window.evaluate_js("document.getElementById('media-volume').value=0;document.getElementById('media-volume').dispatchEvent(new Event('input'))")
                window.evaluate_js('void AudioConsole.open()')
                wait("!!document.querySelector('.audio-console select')")
                assert not window.evaluate_js("document.getElementById('media-audio-output').paused")
                window.evaluate_js("(()=>{const r=document.querySelector('.audio-eq input');r.value=5;r.dispatchEvent(new Event('input'));r.dispatchEvent(new Event('change'));})()")
                wait("document.querySelector('.audio-status').textContent==='Saved'")
                assert api.audio_console_settings()['music_eq'][0]==5
                window.evaluate_js("document.getElementById('audio-send-toggle').click()")
                wait("document.querySelector('.audio-status').textContent.includes('No virtual microphone')")
                assert not window.evaluate_js('AudioConsole.state().sending')
                wait('expensesShift===1')
                if '--hardware' in sys.argv:
                    # Opt-in local capture check: no recording, network or monitoring.
                    window.evaluate_js("[...document.querySelectorAll('.audio-console button')].find(b=>b.textContent==='Allow microphone device access').click()")
                    wait("[...document.querySelector('[aria-label=\"Effects microphone\"]').options].some(o=>o.textContent.includes('HyperX'))")
                    window.evaluate_js("(()=>{const s=document.querySelector('[aria-label=\"Effects microphone\"]');s.value=[...s.options].find(o=>o.textContent.includes('HyperX')).value;s.dispatchEvent(new Event('change'));})()")
                    wait("AudioConsole.state().settings.input_name.includes('HyperX')")
                    window.evaluate_js("document.getElementById('audio-mic-toggle').click()")
                    wait('AudioConsole.state().capturing')
                    time.sleep(.5)
                    assert not window.evaluate_js('AudioConsole.state().monitoring || AudioConsole.state().sending')
                    window.evaluate_js("document.getElementById('audio-mic-toggle').click()")
                    wait('!AudioConsole.state().capturing')
                    result['physical_mic_open_close']=True
                # Synthetic microphone and virtual sink: exercise live routing without
                # recording the user or installing a driver. The remote element is muted.
                window.evaluate_js("""(()=>{
                  const enumerate=navigator.mediaDevices.enumerateDevices.bind(navigator.mediaDevices);
                  navigator.mediaDevices.enumerateDevices=async()=>[...await enumerate(),
                    {kind:'audioinput',deviceId:'test-mic',label:'Synthetic physical microphone'},
                    {kind:'audiooutput',deviceId:'test-cable',label:'CABLE Input (synthetic test)'}];
                  const sink=HTMLMediaElement.prototype.setSinkId;
                  HTMLMediaElement.prototype.setSinkId=function(id){if(id==='test-cable'){this.muted=true;return Promise.resolve();}return sink.call(this,id);};
                  navigator.mediaDevices.getUserMedia=async()=>{
                    const c=new AudioContext(),o=c.createOscillator(),g=c.createGain(),d=c.createMediaStreamDestination();
                    o.frequency.value=330;g.gain.value=.01;o.connect(g).connect(d);o.start();
                    window._synthetic={context:c,stream:d.stream};return d.stream;
                  };
                  void AudioConsole.open();
                })()""")
                wait("!!document.querySelector('option[value=test-mic]')")
                window.evaluate_js("(()=>{const s=document.querySelector('[aria-label=\"Effects microphone\"]');s.value='test-mic';s.dispatchEvent(new Event('change'));})()")
                wait("AudioConsole.state().settings.input_device==='test-mic'")
                window.evaluate_js("document.getElementById('audio-mic-toggle').click()")
                wait('AudioConsole.state().capturing')
                assert not window.evaluate_js('AudioConsole.state().monitoring')
                window.evaluate_js("(()=>{const s=document.querySelector('[aria-label=\"Virtual microphone output\"]');s.value='test-cable';s.dispatchEvent(new Event('change'));})()")
                wait("AudioConsole.state().settings.send_device==='test-cable'")
                window.evaluate_js("document.getElementById('audio-send-toggle').click()")
                wait('AudioConsole.state().sending && AudioConsole.state().sendPeak>.001')
                wait('AudioConsole.state().localGain<.001 && AudioConsole.state().sendGain>.49')
                window.evaluate_js("document.getElementById('audio-mic-toggle').click();document.getElementById('audio-send-toggle').click()")
                wait('!AudioConsole.state().capturing && !AudioConsole.state().sending')
                assert window.evaluate_js("_synthetic.stream.getTracks().every(t=>t.readyState==='ended')")
                window.evaluate_js('void _synthetic.context.close()')
                result['synthetic_microphone_and_mix']=True
                # Reopen a compact layout and ensure all controls remain reachable.
                window.resize(740,650)
                wait("document.querySelector('.audio-console').scrollWidth<=document.getElementById('sheet-body').clientWidth")
                window.resize(1040,780)
                wait('innerWidth > 900')
                from webview.platforms.winforms import BrowserView
                from System import Func, Object
                native=BrowserView.instances[window.uid]
                # Capture the view to avoid compositor stalls while the core animates.
                task=native.webview.Invoke(Func[Object](lambda:native.webview.CoreWebView2.CallDevToolsProtocolMethodAsync(
                    'Page.captureScreenshot','{"format":"png","fromSurface":false,"captureBeyondViewport":false}')))
                if not task.Wait(10000):
                    raise TimeoutError('Audio console screenshot timed out')
                (BASE/'.tmp-audio-console.png').write_bytes(base64.b64decode(json.loads(str(task.Result))['data']))
                window.evaluate_js("document.getElementById('expenses-core-slot').click()")
                wait("!document.body.classList.contains('core-sheet-open')")
                result['passed']=True
            except Exception:
                result['error']=traceback.format_exc()
                result['permission']=getattr(api,'_audio_permission_diagnostic',None)
            finally:
                window.destroy()
        webview.start(check, gui='edgechromium', private_mode=True)
        api._close_media()
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get('passed') else 1


if __name__=='__main__':
    raise SystemExit(main())
