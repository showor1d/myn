"""Real WebView2 playback/seek; generated silent audio and canvas-only video."""
import base64
import json
import os
import sys
import tempfile
import time
import threading
import traceback
import wave
from pathlib import Path
BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src/core")]
from media_bridge import MediaBridge


def main():
    os.environ['WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS']='--autoplay-policy=no-user-gesture-required'
    import webview
    with tempfile.TemporaryDirectory(prefix="myn-media-ui-") as folder:
        root = Path(folder)
        source = root / "sample.wav"
        with wave.open(str(source), "wb") as stream:
            stream.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            stream.writeframes(b"\0\0" * 24000 * 15)
        lyric = root / "sample.lrc"
        lyric.write_text("[00:00]First line\n[00:02]Second line\n[00:04]Third line\n[00:06]Fourth line\n[00:08]Fifth line\n[00:10]Sixth line", encoding="utf-8")
        class Api(MediaBridge):
            def emit(self, message):
                if message.get('event') == 'alarm_settings':
                    threading.Thread(target=alarm_controller._music_alarm_settings, args=(message,), daemon=True).start()
                if message.get('event') == 'alarm_music_result':
                    alarm_results.append(message)
                if message.get('event')=='music_result':
                    music_receipt.update(message)
                    music_signal.set()
            def save_sample(self, data):
                video = root / "sample.webm"
                video.write_bytes(base64.b64decode(data.split(",", 1)[1]))
                self._drop({"dataTransfer": {"files": [{"name": video.name,
                                                       "pywebviewFullPath": str(video)}]}})
                return True
        music_receipt, music_signal = {}, threading.Event()
        alarm_controller, alarm_results = None, []
        api = Api(root / "media")
        api.media_save_settings(.2, False)
        window = webview.create_window("MYN media verification", (BASE / "src/web/index.html").as_uri(),
                                      js_api=api, width=1040, height=720, x=80, y=80,
                                      frameless=True, easy_drag=False)
        import timekeeper
        import alarm_music
        from gui_timers import TimersMixin
        from types import SimpleNamespace
        alarm_controller = TimersMixin()
        alarm_controller.session = SimpleNamespace(db_path=root / 'database' / 'jarvis.db')
        alarm_controller._timers_changed = lambda: None
        alarm_controller.web = SimpleNamespace(js=lambda payload: window.evaluate_js(
            'MYN.receive(' + json.dumps(payload) + ')'))
        result = {}
        def inspect():
            try:
                assert window.events.loaded.wait(20)
                api._attach_media(window)
                window.evaluate_js("document.body.classList.remove('intro');document.body.style.background='#172937';document.dispatchEvent(new PointerEvent('pointerdown'))")
                # Confirm a native file-drop listener is actually registered.
                from webview.dom import _dnd_state
                assert _dnd_state["num_listeners"] > 0
                assert window.evaluate_js("!document.getElementById('media-mini').hidden && !document.getElementById('media-mini-toggle').disabled")
                from webview.platforms.winforms import BrowserView
                from System import Func, Object
                native = BrowserView.instances[window.uid]
                def protocol(method, arguments):
                    if method == 'Page.captureScreenshot':
                        # Capturing the compositor surface during a live core animation
                        # can stall WebView2; the view capture still shows the actual page.
                        arguments = dict(arguments, fromSurface=False, captureBeyondViewport=False)
                    task = native.webview.Invoke(Func[Object](lambda:
                        native.webview.CoreWebView2.CallDevToolsProtocolMethodAsync(method, json.dumps(arguments))))
                    if not task.Wait(10000):
                        raise TimeoutError('WebView2 diagnostic request timed out: ' + method)
                    return json.loads(str(task.Result))
                # Exercise pywebview's decoded native-file event. Physical Explorer
                # dragging is not emulated: WebView2 CDP file drag can hang.
                def wait_js(expression, seconds=10):
                    end = time.monotonic()+seconds
                    while time.monotonic() < end:
                        value = window.evaluate_js(expression)
                        if value:
                            return value
                        time.sleep(.1)
                    raise AssertionError(expression)
                window.evaluate_js("""(() => {
                    document.getElementById('media-mini-note').click();
                    window._entrance=document.getElementById('media-player').getAnimations()[0];
                    window._entranceStart=window._entrance.effect.getKeyframes()[0].opacity;
                })()""")
                assert window.evaluate_js("!!window._entrance && !document.querySelector('#media-player h2')")
                assert window.evaluate_js("Number(window._entranceStart)===0 && window._entrance.effect.getTiming().duration===400")
                wait_js("musicShift>0 && musicShift<1 && getComputedStyle(document.getElementById('media-player')).clipPath==='none' && Number(getComputedStyle(document.getElementById('media-player')).opacity)>0 && Number(getComputedStyle(document.getElementById('media-player')).opacity)<1")
                (BASE / ".tmp-media-opening.png").write_bytes(base64.b64decode(
                    protocol("Page.captureScreenshot", dict(format="png"))["data"]))
                wait_js("musicShift===1 && document.getElementById('media-player').getAnimations().length===0")
                # Drag the actual frameless native window from the player header.
                x0, y0 = window.x, window.y
                assert window.evaluate_js("document.elementFromPoint(160,22).id==='media-drag'")
                print("checking native drag", flush=True)
                window.evaluate_js("document.getElementById('media-drag').dispatchEvent(new MouseEvent('mousedown',{clientX:160,clientY:22,bubbles:true}))")
                window.evaluate_js("window.dispatchEvent(new MouseEvent('mousemove',{screenX:%s,screenY:%s}))" % (x0+200,y0+42))
                time.sleep(.3)
                window.evaluate_js("window.dispatchEvent(new MouseEvent('mouseup'))")
                assert abs(window.x-x0)>=20 and abs(window.y-y0)>=10, (x0,y0,window.x,window.y)
                print("native drag passed", flush=True)
                # Pointer and keyboard focus no longer draw a rectangle around volume.
                window.evaluate_js("document.getElementById('media-volume').focus()")
                assert window.evaluate_js("getComputedStyle(document.getElementById('media-volume')).outlineStyle==='none'")
                window.evaluate_js("document.getElementById('media-core-slot').click();document.getElementById('media-mini-note').click();document.getElementById('media-core-slot').click()")
                assert window.evaluate_js("document.getElementById('media-player').hidden && document.getElementById('media-player').getAnimations().length===0")
                protocol("Emulation.setEmulatedMedia", {"features":[{"name":"prefers-reduced-motion","value":"reduce"}]})
                window.evaluate_js("document.getElementById('media-mini-note').click()")
                wait_js("musicShift===1")
                assert window.evaluate_js("document.getElementById('media-player').getAnimations().length===0")
                window.evaluate_js("document.getElementById('media-core-slot').click()")
                protocol("Emulation.setEmulatedMedia", {"features":[]})
                window.evaluate_js("document.getElementById('media-mini-toggle').click()")
                wait_js("document.getElementById('media-mini-error').textContent.includes('music folder is empty') && !document.getElementById('media-mini-toggle').disabled")
                assert window.evaluate_js("document.getElementById('media-audio-output').paused")
                api._drop({"dataTransfer": {"files": [{"name": source.name,
                                                      "pywebviewFullPath": str(source)},
                                                     {"name": lyric.name, "pywebviewFullPath": str(lyric)}]}})
                wait_js("!document.getElementById('media-drop').hidden")
                assert api.library.scan() == [], "Drop must wait for category selection"
                window.evaluate_js("document.querySelector('[data-category=music]').click()")
                wait_js("document.querySelectorAll('#media-library button').length===1")
                window.evaluate_js("document.getElementById('media-toggle').click()")
                wait_js("!document.getElementById('media-audio-output').paused && !document.getElementById('media-toggle').disabled")
                window.evaluate_js("document.getElementById('media-toggle').click()")
                wait_js("document.getElementById('media-audio-output').paused")
                # Browsing preserves an explicitly paused song; the mini button resumes it.
                window.evaluate_js("document.getElementById('media-audio-output').currentTime=1")
                window.evaluate_js("document.getElementById('media-audio').click();document.getElementById('media-music').click()")
                assert window.evaluate_js("document.getElementById('media-audio-output').paused && document.getElementById('media-audio-output').currentTime===1")
                window.evaluate_js("document.getElementById('media-audio').click();document.getElementById('media-music').click();document.getElementById('media-core-slot').click();document.getElementById('media-mini-toggle').click()")
                wait_js("!document.getElementById('media-audio-output').paused && !document.getElementById('media-mini-toggle').disabled")
                assert window.evaluate_js("document.getElementById('media-player').hidden")
                # The same entry used by chat/voice must really start playback.
                import local_actions
                def control(action):
                    music_signal.clear(); music_receipt.clear()
                    window.evaluate_js('MediaPlayer.command('+json.dumps(dict(action=action,request='test-play'))+')')
                    assert music_signal.wait(10), 'Missing actual browser receipt'
                    return dict(music_receipt)
                context=local_actions.ActionContext(root/'config',music_control=control)
                content,error=local_actions.execute('music_control',dict(action='play'),context)
                assert not error,content
                assert json.loads(content)['playing'],content
                wait_js("Math.abs(AudioConsole.state().localGain-.2)<.001")
                wait_js("document.getElementById('media-audio-output').currentTime>.1")
                wait_js("document.querySelector('#media-lyrics .current')?.textContent==='First line'")
                wait_js("document.body.classList.contains('music-immersive')")
                wait_js("musicShift===1 && formation===1")
                assert window.evaluate_js("""(() => {
                    const slot=document.querySelector('#media-core-slot').getBoundingClientRect();
                    const list=document.querySelector('#media-library').getBoundingClientRect();
                    const stage=document.querySelector('#media-stage').getBoundingClientRect();
                    const tabs=document.querySelector('#media-player header').getBoundingClientRect();
                    const scale=ctx.getTransform().a;
                    return slot.height===88 && slot.width===96 && slot.left===20 && slot.top===8
                        && Math.abs(scale*Math.min(W*.21,H*.27,158)*2.44-88*.84)<1
                        && tabs.left>innerWidth/2 && tabs.right<=innerWidth-16
                        && list.top>=slot.bottom && list.top<150 && list.height>innerHeight*.65 && stage.left>slot.right
                        && musicShift===1 && getComputedStyle(document.querySelector('#scene')).zIndex==='43';
                })()""")
                (BASE / ".tmp-media-header.png").write_bytes(base64.b64decode(
                    protocol("Page.captureScreenshot", dict(format="png"))["data"]))
                assert window.evaluate_js("""(() => {
                    const icons=['media-prev','media-toggle','media-next'].map(id =>
                        document.querySelector('#'+id+' svg').getBoundingClientRect());
                    const centers=icons.map(r => r.x+r.width/2);
                    return icons.every(r => r.width===24 && r.height===24 && Math.abs(r.y-icons[0].y)<.1)
                        && Math.abs((centers[1]-centers[0])-(centers[2]-centers[1]))<.1;
                })()""")
                assert window.evaluate_js("""(() => {
                    const p=document.querySelector('#media-player').getBoundingClientRect();
                    return p.x===0 && p.y===0 && p.width===innerWidth && p.height===innerHeight
                        && !document.querySelector('#media-audio-output').controls
                        && getComputedStyle(document.querySelector('#media-audio-controls')).backgroundColor==='rgba(0, 0, 0, 0)'
                        && !document.querySelector('#media-tools')
                        && !document.querySelector('#media-close')
                        && !document.querySelector('#media-add,#media-folder,#media-refresh')
                        && !document.querySelector('#media-prev').textContent.trim()
                        && !document.querySelector('#media-next').textContent.trim()
                        && document.getElementById('media-name').hidden
                        && document.querySelector('#media-prev').getBoundingClientRect().right
                           <= document.querySelector('#media-toggle').getBoundingClientRect().left
                        && document.querySelector('#media-toggle').getBoundingClientRect().right
                           <= document.querySelector('#media-next').getBoundingClientRect().left
                        && document.querySelector('#media-next').getBoundingClientRect().right
                           <= document.querySelector('#media-volume-controls').getBoundingClientRect().left
                        && document.querySelector('#media-track-nav').getBoundingClientRect().bottom
                           <= document.querySelector('#media-timeline').getBoundingClientRect().top;
                })()""")
                window.evaluate_js("document.getElementById('media-toggle').click()")
                wait_js("document.getElementById('media-audio-output').paused && document.getElementById('media-toggle').getAttribute('aria-label')==='Play'")
                assert window.evaluate_js("document.body.classList.contains('music-immersive')")
                window.evaluate_js("""(() => {
                    const seek=document.getElementById('media-seek');seek.value=2;seek.dispatchEvent(new Event('input'));
                    const volume=document.getElementById('media-volume');volume.value=.35;volume.dispatchEvent(new Event('input'));
                })()""")
                wait_js("Math.abs(AudioConsole.state().localGain-.35)<.001")
                wait_js("document.querySelector('#media-lyrics .current')?.textContent==='Second line'")
                time.sleep(.6)
                assert window.evaluate_js("""(() => {
                    const current=document.querySelector('.lyrics-line.current');
                    const other=document.querySelector('.lyrics-line:not(.current)');
                    const host=document.getElementById('media-lyrics');
                    const a=current.getBoundingClientRect(), b=host.getBoundingClientRect();
                    return parseFloat(getComputedStyle(current).fontSize)>parseFloat(getComputedStyle(other).fontSize)
                        && Number(getComputedStyle(current).fontWeight)>=700
                        && getComputedStyle(other).transform.startsWith('matrix3d')
                        && Math.abs(a.y+a.height/2-b.y-b.height/2)<5;
                })()""")
                (BASE / '.tmp-media-curved-lyrics.png').write_bytes(base64.b64decode(
                    protocol('Page.captureScreenshot', dict(format='png'))['data']))
                window.evaluate_js("document.getElementById('media-mute').click()")
                wait_js("AudioConsole.state().localGain<.001 && document.getElementById('media-volume').value==='0'")
                window.evaluate_js("document.getElementById('media-toggle').click()")
                wait_js("document.getElementById('media-audio-output').currentTime>=2")
                wait_js("document.getElementById('media-toggle').getAttribute('aria-label')==='Pause'")
                window.evaluate_js("document.getElementById('media-audio-output').currentTime=14.9")
                wait_js("document.getElementById('media-audio-output').currentTime<1 && !document.getElementById('media-audio-output').paused")
                time.sleep(.3)
                self_settings = MediaBridge(api.library.root).media_settings()
                assert self_settings == {"volume": .35, "muted": True}, self_settings
                second = root / "z-second.wav"
                second.write_bytes(source.read_bytes())
                api._import([second], "music")
                window.evaluate_js("void MediaPlayer.open()")
                wait_js("document.querySelectorAll('#media-library button').length===2")
                window.evaluate_js("document.getElementById('media-next').click()")
                wait_js("document.getElementById('media-name').textContent==='z-second.wav' && document.getElementById('media-audio-output').currentTime>.1")
                window.evaluate_js("document.getElementById('media-prev').click()")
                wait_js("document.getElementById('media-name').textContent==='sample.wav' && document.getElementById('media-audio-output').currentTime>.1")
                assert window.evaluate_js("""(() => {
                    const r=document.querySelector('#media-core-slot').getBoundingClientRect();
                    return document.elementFromPoint(r.x+r.width/2,r.y+r.height/2).id==='media-core-slot';
                })()""")
                before = window.evaluate_js("document.getElementById('media-audio-output').currentTime")
                window.evaluate_js("TimerHUD.update({items:[],count:0,alarm_upcoming:true})")
                window.evaluate_js("document.getElementById('media-core-slot').click()")
                wait_js("document.getElementById('media-player').hidden && !document.body.classList.contains('media-open')")
                wait_js("document.getElementById('media-audio-output').currentTime>%s" % (before + .2))
                wait_js("""(() => {
                    const mini=document.getElementById('media-mini');
                    const alarm=document.querySelector('.alarm-indicator');
                    return !mini.hidden && mini.getBoundingClientRect().top>alarm.getBoundingClientRect().bottom
                        && document.getElementById('media-mini-box').hidden;
                })()""")
                window.evaluate_js("document.getElementById('media-mini-note').dispatchEvent(new PointerEvent('pointerenter'))")
                time.sleep(.15)
                assert window.evaluate_js("document.getElementById('media-mini-box').hidden")
                wait_js("!document.getElementById('media-mini-box').hidden")
                time.sleep(1.1)
                assert window.evaluate_js("!document.getElementById('media-mini-box').hidden")
                window.evaluate_js("document.getElementById('media-mini-note').dispatchEvent(new PointerEvent('pointerleave'))")
                wait_js("document.getElementById('media-mini-box').hidden", seconds=2)
                window.evaluate_js("document.getElementById('media-mini-note').dispatchEvent(new PointerEvent('pointerenter'))")
                wait_js("!document.getElementById('media-mini-box').hidden")
                window.evaluate_js("document.getElementById('media-mini-box').dispatchEvent(new PointerEvent('pointerenter'))")
                time.sleep(1.1)
                assert window.evaluate_js("!document.getElementById('media-mini-box').hidden")
                window.evaluate_js("""(() => {
                    const s=document.getElementById('media-mini-seek');s.value=5;s.dispatchEvent(new Event('input'));
                })()""")
                wait_js("document.getElementById('media-audio-output').currentTime>=5")
                wait_js("document.querySelector('#media-lyrics .current')?.textContent==='Third line'")
                window.evaluate_js("document.getElementById('media-mini-toggle').click()")
                wait_js("document.getElementById('media-audio-output').paused && document.getElementById('media-mini-toggle').getAttribute('aria-label')==='Play'")
                assert window.evaluate_js("!document.getElementById('media-mini').hidden")
                window.evaluate_js("document.getElementById('media-mini-toggle').click()")
                wait_js("!document.getElementById('media-audio-output').paused")
                window.evaluate_js("document.getElementById('media-mini-next').click()")
                wait_js("document.getElementById('media-mini-name').textContent==='z-second.wav' && document.getElementById('media-audio-output').currentTime>.1")
                window.evaluate_js("document.getElementById('media-mini-prev').click()")
                wait_js("document.getElementById('media-mini-name').textContent==='sample.wav' && document.getElementById('media-audio-output').currentTime>.1")
                assert window.evaluate_js("document.getElementById('media-player').hidden")
                (BASE / ".tmp-media-mini.png").write_bytes(base64.b64decode(
                    protocol("Page.captureScreenshot", dict(format="png"))["data"]))
                window.evaluate_js("document.getElementById('media-mini-box').dispatchEvent(new PointerEvent('pointerleave'))")
                assert window.evaluate_js("document.getElementById('media-mini-box').hidden")
                window.evaluate_js("TimerHUD.update({items:[],count:0,alarm_upcoming:false})")
                wait_js("!document.getElementById('media-mini').hidden && !document.querySelector('.alarm-indicator')")
                window.evaluate_js("document.getElementById('scene').dispatchEvent(new PointerEvent('pointerdown',{bubbles:true}))")
                assert window.evaluate_js("document.getElementById('media-mini-box').hidden")
                (api.library.folders["music"] / second.name).unlink()
                window.evaluate_js("document.getElementById('media-mini-note').click()")
                wait_js("document.body.classList.contains('music-immersive') && musicShift===1")
                assert window.evaluate_js("document.getElementById('media-mini').hidden")
                (BASE / ".tmp-media-immersive.png").write_bytes(base64.b64decode(
                    protocol("Page.captureScreenshot", dict(format="png"))["data"]))
                # A second drop can be cancelled without creating a copy.
                api._drop({"dataTransfer": {"files": [{"pywebviewFullPath": str(source)}]}})
                window.evaluate_js("document.getElementById('media-drop-cancel').click()")
                wait_js("document.getElementById('media-drop').hidden")
                assert len(api.library.scan()) == 1
                # Drop directly onto Audio: the page carries the selected target
                # while the native callback supplies trusted local file paths.
                window.evaluate_js("""(() => {
                    const data=new DataTransfer();data.items.add(new File(['x'],'sample.wav'));
                    document.querySelector('[data-category=audio]').dispatchEvent(
                        new DragEvent('dragover',{bubbles:true,dataTransfer:data}));
                })()""")
                api._drop({"dataTransfer": {"files": [{"pywebviewFullPath": str(source)}]}})
                wait_js("document.getElementById('media-audio').getAttribute('aria-pressed')==='true'")
                assert window.evaluate_js("!document.body.classList.contains('music-immersive')")
                # Merely browsing any tab leaves the music source, position and playback intact.
                window.evaluate_js("window._musicSrc=document.getElementById('media-audio-output').src;document.getElementById('media-audio-output').currentTime=3")
                for category in ('video', 'movie', 'audio'):
                    window.evaluate_js("document.getElementById('media-%s').click()" % category)
                    assert window.evaluate_js("!document.getElementById('media-audio-output').paused && document.getElementById('media-audio-output').src===window._musicSrc && document.getElementById('media-audio-output').currentTime>=3")
                window.evaluate_js("document.querySelector('#media-library button').click()")
                wait_js("!document.getElementById('media-secondary-audio').paused && document.getElementById('media-secondary-audio').currentTime>.1 && document.getElementById('media-audio-output').paused")
                window.evaluate_js("window._musicPosition=document.getElementById('media-audio-output').currentTime")
                time.sleep(.3)
                assert window.evaluate_js("document.getElementById('media-audio-output').currentTime===window._musicPosition")
                assert window.evaluate_js("!document.body.classList.contains('music-immersive')")
                window.evaluate_js("document.getElementById('media-music').click()")
                wait_js("!document.getElementById('media-audio-output').paused && document.getElementById('media-secondary-audio').paused && document.getElementById('media-audio-output').currentTime>=window._musicPosition")
                assert window.evaluate_js("document.getElementById('media-audio-output').currentTime<window._musicPosition+1 && document.getElementById('media-audio-output').src===window._musicSrc && document.getElementById('media-mini-name').textContent==='sample.wav'")
                # An explicitly paused song must not auto-resume after other playback.
                window.evaluate_js("document.getElementById('media-toggle').click();document.getElementById('media-audio').click();document.querySelector('#media-library button').click()")
                wait_js("!document.getElementById('media-secondary-audio').paused")
                window.evaluate_js("document.getElementById('media-music').click()")
                assert window.evaluate_js("document.getElementById('media-audio-output').paused")
                window.evaluate_js("document.getElementById('media-toggle').click()")
                wait_js("!document.getElementById('media-audio-output').paused && document.getElementById('media-secondary-audio').paused")
                # An undecodable file cannot steal the playing song or erase its position.
                broken = root / 'broken.wav'
                broken.write_bytes(b'not a wave file')
                api._import([broken], 'audio')
                window.evaluate_js("document.getElementById('media-audio').click()")
                wait_js("document.querySelectorAll('#media-library button').length===2")
                window.evaluate_js("document.querySelector('#media-library button').click()")
                wait_js("!!document.getElementById('media-secondary-audio').error")
                assert window.evaluate_js("!document.getElementById('media-audio-output').paused && document.getElementById('media-audio-output').src===window._musicSrc")
                (api.library.folders['audio'] / broken.name).unlink()
                # Music advances its own queue while browsing Audio, without moving the tab.
                window.evaluate_js("document.getElementById('media-audio').click();document.getElementById('media-audio-output').currentTime=14.9")
                wait_js("document.getElementById('media-audio-output').currentTime<1 && !document.getElementById('media-audio-output').paused")
                assert window.evaluate_js("document.getElementById('media-audio').getAttribute('aria-pressed')==='true'")
                window.evaluate_js("document.getElementById('media-video').click()")
                assert window.evaluate_js("!document.getElementById('media-audio-output').paused")
                assert window.evaluate_js("document.getElementById('media-audio-controls').hidden")
                assert window.evaluate_js("getComputedStyle(document.getElementById('media-art')).display==='none'")
                # Generate a WebM using only a painted canvas, no camera/microphone.
                window.evaluate_js("""(() => {
                    const c=document.createElement('canvas');c.width=320;c.height=180;
                    const x=c.getContext('2d'), stream=c.captureStream(10), chunks=[];
                    const r=new MediaRecorder(stream,{mimeType:'video/webm;codecs=vp8'});
                    let frame=0;
                    const timer=setInterval(()=>{x.fillStyle='#17536a';x.fillRect(0,0,320,180);
                      x.fillStyle='#bdefff';x.font='26px sans-serif';x.fillText('MYN  '+frame++,80,100)},100);
                    r.ondataavailable=e=>chunks.push(e.data);
                    r.onstop=()=>{clearInterval(timer);stream.getTracks().forEach(t=>t.stop());
                      const reader=new FileReader();reader.onload=()=>pywebview.api.save_sample(reader.result);
                      reader.readAsDataURL(new Blob(chunks,{type:'video/webm'}));};
                    r.start();setTimeout(()=>r.stop(),2200);
                })()""")
                wait_js("!document.getElementById('media-drop').hidden")
                window.evaluate_js("document.querySelector('[data-category=movie]').click()")
                wait_js("document.querySelector('#media-library button')?.textContent.includes('sample.webm')")
                assert window.evaluate_js("document.getElementById('media-movie').getAttribute('aria-pressed')==='true'")
                window.evaluate_js("document.getElementById('media-screen').muted=true;document.querySelector('#media-library button').click()")
                wait_js("document.getElementById('media-screen').videoWidth===320 && document.getElementById('media-screen').currentTime>.1")
                assert window.evaluate_js("document.getElementById('media-audio-output').paused")
                window.evaluate_js("window._musicPosition=document.getElementById('media-audio-output').currentTime;document.getElementById('media-music').click()")
                wait_js("!document.getElementById('media-audio-output').paused && document.getElementById('media-screen').paused")
                assert window.evaluate_js("document.getElementById('media-audio-output').currentTime>=window._musicPosition && document.getElementById('media-audio-output').currentTime<window._musicPosition+1")
                window.evaluate_js("document.getElementById('media-movie').click();document.querySelector('#media-library button').click()")
                wait_js("!document.getElementById('media-screen').paused && document.getElementById('media-audio-output').paused")
                window.evaluate_js("document.getElementById('media-screen').currentTime=1")
                wait_js("document.getElementById('media-screen').currentTime>=1")
                window.evaluate_js("navigator.mediaDevices.enumerateDevices().then(ds=>window._mediaOutputs=ds.filter(d=>d.kind==='audiooutput').map(d=>({name:d.label,id:d.deviceId})))")
                wait_js("Array.isArray(window._mediaOutputs)")
                result["output_devices"] = window.evaluate_js("window._mediaOutputs")
                (BASE / ".tmp-media-player.png").write_bytes(base64.b64decode(
                    protocol("Page.captureScreenshot", dict(format="png"))["data"]))
                window.evaluate_js("document.getElementById('media-core-slot').click()")
                assert window.evaluate_js("document.getElementById('media-screen').paused && document.getElementById('media-player').hidden")
                window.evaluate_js("MYN.receive({cmd:'media_open'})")
                wait_js("!document.getElementById('media-player').hidden")
                assert len(api.library.scan()) == 3
                assert source.exists()
                window.evaluate_js("document.getElementById('media-music').click()")
                window.evaluate_js("document.querySelector('#media-library button').click()")
                wait_js("!document.getElementById('media-alarm').disabled")
                assert window.evaluate_js("document.getElementById('media-alarm').getBoundingClientRect().right <= document.getElementById('media-prev').getBoundingClientRect().left")
                window.evaluate_js("document.getElementById('media-alarm').click()")
                wait_js("document.getElementById('alarm-default-sound').textContent.includes('.wav')")
                assert not window.evaluate_js("!!document.getElementById('media-alarm-form')")
                assert timekeeper.tasks(root / 'config') == []
                chosen = alarm_music.settings(root / 'config')
                before = api.media_settings()
                assert window.evaluate_js("!document.querySelector('#media-player #alarm-volume') && !!document.querySelector('#alarm-mini-box #alarm-volume')")
                window.evaluate_js("document.getElementById('media-core-slot').click();TimerHUD.update({items:[],count:0,alarm_upcoming:true});document.querySelector('.alarm-indicator').click()")
                wait_js("!document.getElementById('alarm-mini-box').hidden && !document.getElementById('alarm-volume').disabled")
                window.evaluate_js("const av=document.getElementById('alarm-volume');av.value=.63;av.dispatchEvent(new Event('input'));av.dispatchEvent(new Event('change'))")
                end = time.monotonic() + 10
                while alarm_music.settings(root / 'config')['volume'] != .63 and time.monotonic() < end:
                    time.sleep(.1)
                assert alarm_music.settings(root / 'config')['volume'] == .63
                assert api.media_settings() == before
                window.evaluate_js("TimerHUD.closeAlarm();document.querySelector('.alarm-indicator').click()")
                wait_js("Number(document.getElementById('alarm-volume').value)===.63")
                wait_js("document.getElementById('media-player').getAnimations().length===0")
                (BASE / '.tmp-alarm-default-volume.png').write_bytes(base64.b64decode(
                    protocol('Page.captureScreenshot', dict(format='png'))['data']))
                alarm = dict(id='test-ring', track=chosen['ringtone_id'])
                window.evaluate_js('MYN.receive(' + json.dumps(dict(cmd='alarm_music', alarms=[alarm], paused=False, volume=.63)) + ')')
                wait_js("!document.getElementById('alarm-music-output').paused && document.getElementById('alarm-music-output').currentTime>.05")
                assert window.evaluate_js("document.getElementById('alarm-music-output').loop && document.getElementById('alarm-music-output').volume===.63 && document.getElementById('media-audio-output').paused")
                window.evaluate_js("document.getElementById('alarm-music-output').currentTime=14.9")
                wait_js("document.getElementById('alarm-music-output').currentTime<1 && !document.getElementById('alarm-music-output').paused")
                window.evaluate_js('MYN.receive(' + json.dumps(dict(cmd='alarm_music', alarms=[alarm], paused=True)) + ')')
                wait_js("document.getElementById('alarm-music-output').paused")
                window.evaluate_js('MYN.receive(' + json.dumps(dict(cmd='alarm_music', alarms=[alarm], paused=False, volume=.63)) + ')')
                wait_js("!document.getElementById('alarm-music-output').paused")
                window.evaluate_js("MYN.receive({cmd:'alarm_music',alarms:[],paused:false})")
                wait_js("document.getElementById('alarm-music-output').paused && !document.getElementById('alarm-music-output').getAttribute('src')")
                window.evaluate_js("MYN.receive({cmd:'alarm_music',alarms:[{id:'missing',track:'missing'}],paused:false})")
                deadline = time.monotonic() + 10
                while not any(r['id'] == 'missing' and not r['ok'] for r in alarm_results) and time.monotonic() < deadline:
                    time.sleep(.1)
                assert any(r['id'] == 'test-ring' and r['ok'] for r in alarm_results)
                assert any(r['id'] == 'missing' and not r['ok'] for r in alarm_results)
                window.evaluate_js("MYN.receive({cmd:'alarm_music',alarms:[],paused:false})")
                result["passed"] = True
            except Exception:
                result["error"] = traceback.format_exc()
                result["ui_state"] = window.evaluate_js("({body:document.body.className,shift:musicShift,playerHidden:document.getElementById('media-player').hidden,note:document.getElementById('media-mini-note').outerHTML,status:document.getElementById('media-status').textContent})")
            finally:
                api._close_media()
                window.destroy()
        webview.start(inspect, gui="edgechromium", private_mode=True)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if not result.get("passed"):
            raise SystemExit(1)


if __name__ == "__main__":
    main()

