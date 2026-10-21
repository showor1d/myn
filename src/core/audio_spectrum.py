"""Short, in-memory FFT frames for visualization; no audio storage or inference."""
import cmath
import math
from functools import lru_cache

SIZE = 2048
BANDS = 128
WINDOW = tuple(.5 - .5 * math.cos(2 * math.pi * i / (SIZE - 1)) for i in range(SIZE))


@lru_cache(maxsize=8)
def band_ranges(rate):
    edges = [40 * (16000 / 40) ** (i / BANDS) for i in range(BANDS + 1)]
    return tuple((max(1, int(lo * SIZE / rate)),
                  max(2, int(hi * SIZE / rate) + 1)) for lo, hi in zip(edges, edges[1:]))


@lru_cache(maxsize=12)
def _twiddles(length):
    return tuple(cmath.exp(-2j * math.pi * k / length) for k in range(length // 2))


def _fft(values):
    """Radix-2 fallback for the dependency-free GUI Python environment."""
    values = list(values)
    j = 0
    for i in range(1, SIZE):
        bit = SIZE >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j ^= bit
        if i < j:
            values[i], values[j] = values[j], values[i]
    length = 2
    while length <= SIZE:
        half, rotations = length // 2, _twiddles(length)
        for start in range(0, SIZE, length):
            for k, rotation in enumerate(rotations):
                a, b = values[start + k], values[start + k + half] * rotation
                values[start + k], values[start + k + half] = a + b, a - b
        length *= 2
    return values[:SIZE // 2 + 1]


class Spectrum:
    def __init__(self, np=None):
        self.np = np
        self.samples = []
        self.rate = 0

    def clear(self):
        self.samples = []

    def feed(self, samples, rate):
        if rate != self.rate:
            self.clear()
            self.rate = rate
        self.samples = (self.samples + list(samples[-SIZE:]))[-SIZE:]

    def bands(self, gain=1.):
        if not self.samples or not self.rate or gain <= 0:
            return [0.] * BANDS
        raw = [0.] * (SIZE - len(self.samples)) + self.samples
        raw = [float(x) if math.isfinite(x) else 0. for x in raw]
        if max(map(abs, raw)) * gain < .0001:
            return [0.] * BANDS
        mean = sum(raw) / SIZE
        data = [(x - mean) * w for x, w in zip(raw, WINDOW)]
        fft = self.np.fft.rfft(data) if self.np is not None else _fft(data)
        magnitudes = [abs(x) * 4 / SIZE * gain for x in fft]
        result = []
        for lo, hi in band_ranges(self.rate):
            peak = max(magnitudes[lo:min(hi, len(magnitudes))], default=0.)
            # Fixed dB mapping, never normalize each frame to full scale.
            db = 20 * math.log10(max(1e-9, peak))
            result.append(round(min(1., max(0., (db + 70) / 64)), 4))
        return result
