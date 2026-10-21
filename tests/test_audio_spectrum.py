import math
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/core'))
from audio_spectrum import Spectrum, SIZE, BANDS, band_ranges
from output_loopback import decode_pcm


def tone(hz, amplitude=.1, rate=48000):
    return [amplitude * math.sin(2*math.pi*hz*i/rate) for i in range(SIZE)]


class SpectrumTests(unittest.TestCase):
    def test_frequency_changes_move_peak_and_gain_changes_height(self):
        analyzer = Spectrum()
        peaks = []
        for hz in (220, 880, 3200):
            analyzer.feed(tone(hz), 48000)
            bands = analyzer.bands()
            peak = max(range(BANDS), key=bands.__getitem__)
            lo, hi = band_ranges(48000)[peak]
            self.assertLessEqual(lo*48000/SIZE, hz+48000/SIZE)
            self.assertGreaterEqual(hi*48000/SIZE, hz-48000/SIZE)
            peaks.append(peak)
            self.assertGreater(max(bands), max(analyzer.bands(.1)))
        self.assertEqual(peaks, sorted(set(peaks)))

    def test_silence_dc_nan_mute_and_device_rate_change(self):
        analyzer = Spectrum()
        for samples in ([0.]*SIZE, [.2]*SIZE, [float('nan')]*SIZE):
            analyzer.feed(samples, 48000)
            self.assertEqual(analyzer.bands(), [0.]*BANDS)
        analyzer.feed(tone(440), 48000)
        self.assertGreater(max(analyzer.bands()), .5)
        self.assertEqual(analyzer.bands(0), [0.]*BANDS)
        analyzer.feed([0.]*80, 16000)
        self.assertEqual(analyzer.bands(), [0.]*BANDS)
        self.assertEqual(len(analyzer.samples), 80)

    def test_opposite_stereo_and_pcm_formats(self):
        raw = struct.pack('<ffff', .25, -.25, -.5, .5)
        self.assertEqual(decode_pcm(raw, 2, 32, True), [.25, -.5])
        self.assertEqual(decode_pcm(struct.pack('<hh', 8192, -16384), 1, 16, False), [.25, -.5])
        self.assertEqual(decode_pcm(bytes.fromhex('0000200000c0'), 1, 24, False), [.25, -.5])
        self.assertEqual(decode_pcm(struct.pack('<ii', 2**29, -2**30), 1, 32, False), [.25, -.5])

    def test_numpy_and_standard_library_agree(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest('NumPy installed in speech environment only')
        a, b = Spectrum(), Spectrum(np)
        for analyzer in (a, b):
            analyzer.feed(tone(330, rate=16000), 16000)
        for x, y in zip(a.bands(), b.bands()):
            self.assertAlmostEqual(x, y, places=4)

    def test_listener_forwards_spectrum_without_second_recording_stream(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest('NumPy installed in speech environment only')
        from stt_worker import Listener
        from unittest.mock import Mock
        events = []
        recorder = Mock()
        listener = Listener(recorder, np, lambda *a, **k: '', events.append, threaded=False, levels=True)
        listener.feed(np.array(tone(440, .1, 16000)), 10)
        packet = next(item for item in events if item['event'] == 'level')
        self.assertEqual(len(packet['bands']), 128)
        self.assertGreater(max(packet['bands']), .5)
        recorder.open_stream.assert_not_called()


if __name__ == '__main__':
    unittest.main()
