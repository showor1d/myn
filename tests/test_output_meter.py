import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'),
               str(Path(__file__).resolve().parents[1] / 'src/core')]
from output_meter import OutputMeter


class OutputMeterTests(unittest.TestCase):
    def test_reads_and_releases_on_same_worker_thread(self):
        seen = []
        class Reader:
            def __init__(self): seen.append(('open', threading.get_ident()))
            def read(self): seen.append(('read', threading.get_ident())); return .4
            def close(self): seen.append(('close', threading.get_ident()))
        meter = OutputMeter(Reader, .005).start()
        try:
            end = time.monotonic()+1
            while not meter.snapshot()['available'] and time.monotonic()<end: time.sleep(.005)
            self.assertAlmostEqual(meter.snapshot()['output'], .4)
        finally:
            meter.close()
        self.assertEqual({s[0] for s in seen}, {'open', 'read', 'close'})
        self.assertEqual(len({s[1] for s in seen}), 1)
        self.assertFalse(meter.thread.is_alive())
        self.assertEqual(meter.snapshot()['output'], 0)

    def test_stale_and_disconnected_meters_go_silent(self):
        reader = Mock()
        reader.read.side_effect = OSError('device unplugged')
        meter = OutputMeter(lambda:reader,.005).start()
        time.sleep(.03)
        self.assertFalse(meter.snapshot()['available'])
        self.assertEqual(meter.snapshot()['output'],0)
        meter.close()
        reader.close.assert_called_once()
        meter.sample=(.9,time.monotonic()-1,True)
        self.assertEqual(meter.snapshot(),dict(output=0,available=False))

    def test_microphone_visuals_outside_conversation_do_not_start_conversation(self):
        from gui_webview import WebViewMixin
        win=WebViewMixin()
        win.web_active=True; win.web=Mock(); win.ducked_at=0
        win._speaking=lambda:False
        win._in_conversation=Mock(return_value=False)
        win._mic_level(dict(level=.55,speech=False))
        win.web.js.assert_called_with(dict(cmd='level',value=.55))
        win._in_conversation.assert_not_called()

    def test_spectrum_transport_freshness_and_snapshot_isolation(self):
        meter = OutputMeter()
        bands = [.4] * 128
        meter.sample = (.3, time.monotonic(), True, bands)
        self.assertEqual(meter.snapshot()['bands'], bands)
        meter.snapshot()['bands'][0] = 1
        self.assertEqual(bands[0], .4)
        meter.sample = (.3, time.monotonic()-1, True, bands)
        self.assertNotIn('bands', meter.snapshot())

    def test_microphone_spectrum_reaches_page_without_changing_speech_controls(self):
        from gui_webview import WebViewMixin
        win = WebViewMixin()
        win.web_active=True; win.web=Mock(); win.ducked_at=0
        win._speaking=lambda:False
        bands = [.2]*128
        win._mic_level(dict(level=.4, speech=False, bands=bands))
        win.web.js.assert_called_with(dict(cmd='level', value=.4, bands=bands))


if __name__=='__main__': unittest.main()
