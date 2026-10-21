"""Silent alarm playback after a fresh WebView starts, without a mouse gesture."""
import json
import sys
import tempfile
import time
import traceback
import wave
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / 'src'), str(BASE / 'src/core')]
from media_bridge import MediaBridge


def main():
    import webview
    with tempfile.TemporaryDirectory(prefix='myn-alarm-startup-') as tmp:
        class Api(MediaBridge):
            def emit(self, message):
                if message.get('event') == 'alarm_music_result':
                    reports.append(message)
        api = Api(Path(tmp) / 'media')
        path = api.library.folders['music'] / 'alarm.wav'
        with wave.open(str(path), 'wb') as out:
            out.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            out.writeframes(b'\0\0' * 24000)
        track = api.library.scan()[0]['id']
        reports, result = [], {}
        window = webview.create_window('Music alarm startup check', (BASE / 'src/web/index.html').as_uri(),
                                       js_api=api, width=740, height=560)

        def inspect():
            try:
                assert window.events.loaded.wait(20)
                window.evaluate_js('MYN.receive(' + json.dumps(dict(cmd='alarm_music',
                    alarms=[dict(id='fresh-start', track=track)], paused=False)) + ')')
                end = time.monotonic() + 10
                while not reports and time.monotonic() < end:
                    time.sleep(.1)
                assert reports and reports[-1]['ok'], reports
                assert window.evaluate_js("!document.getElementById('alarm-music-output').paused")
                window.evaluate_js("MYN.receive({cmd:'alarm_music',alarms:[],paused:false})")
                assert window.evaluate_js("document.getElementById('alarm-music-output').paused")
                result['passed'] = True
            except Exception:
                result['error'] = traceback.format_exc()
            finally:
                api._close_media()
                window.destroy()
        webview.start(inspect, gui='edgechromium', private_mode=True)
        print(json.dumps(result), flush=True)
        return 0 if result.get('passed') else 1


if __name__ == '__main__':
    sys.exit(main())
