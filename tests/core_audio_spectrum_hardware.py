"""Explicit hardware smoke: short quiet test tones through real Windows output."""
import json
import math
import struct
import sys
import tempfile
import time
import wave
import winsound
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/core'))
from output_meter import OutputMeter
from audio_spectrum import BANDS


def main():
    result = {}
    meter = OutputMeter().start()
    try:
        with tempfile.TemporaryDirectory(prefix='myn-spectrum-tone-') as folder:
            for hz in (440, 3200):
                path = Path(folder) / ('tone-%s.wav' % hz)
                rate = 48000
                with wave.open(str(path), 'wb') as stream:
                    stream.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
                    stream.writeframes(b''.join(struct.pack('<h', int(2400 * math.sin(2*math.pi*hz*i/rate)))
                                               for i in range(rate*2)))
                winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
                start = time.monotonic()
                frames = []
                while time.monotonic() - start < 1.7:
                    frame = meter.snapshot()
                    if time.monotonic() - start > .3 and frame.get('bands') and frame['output'] > .001:
                        frames.append(frame)
                    time.sleep(.05)
                winsound.PlaySound(None, 0)
                assert len(frames) > 10, ('No real output spectrum', hz, len(frames))
                average = [sum(frame['bands'][i] for frame in frames) / len(frames) for i in range(BANDS)]
                peak = max(range(BANDS), key=average.__getitem__)
                observed_hz = 40 * 400 ** ((peak + .5) / BANDS)
                assert abs(math.log(observed_hz / hz)) < .14, (hz, observed_hz)
                result[str(hz)] = dict(peak_band=peak, observed_hz=round(observed_hz),
                                       frames=len(frames), amplitude=round(max(average), 4))
                time.sleep(.3)
            time.sleep(.5)
            final = meter.snapshot()
            assert final['output'] < .001 and max(final.get('bands', [0])) < .01, final
            result['silence'] = True
    finally:
        winsound.PlaySound(None, 0)
        meter.close()
    assert not meter.thread.is_alive()
    result['released'] = True
    print(json.dumps(result))


if __name__ == '__main__':
    main()
