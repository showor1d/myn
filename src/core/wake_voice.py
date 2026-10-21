"""Waking MYN by the sound of your own voice saying the wake word ("Hey Siri" style).

Enrollment: you say the wake word a few times; each sample becomes a template of
acoustic features (MFCCs: the shape of the sound over time). Afterwards the start
of every utterance the always-on microphone hears is compared with the templates
by dynamic time warping, which lines the two up even when you speak faster or
slower. A close enough match wakes MYN, whatever speech recognition made of the
word. The match threshold is calibrated from how alike your own samples are.

Only these numbers are kept (in personal_data/config/wake-voice.json), never audio.
Runs inside the speech worker (numpy). This is pattern matching on how you say the
word, not a security check: someone with a similar voice and accent saying the same
word could also wake MYN.
"""

RATE = 16000
FRAME = 400   # 25 ms
HOP = 160     # 10 ms
N_FFT = 512
N_MELS = 26
N_MFCC = 13
MAX_TEMPLATE_S = 2.0
# threshold = factor x typical distance between your own samples. Raised 2026-10-17
# (was 1.15 / 1.35 / 1.6) after the user found waking by voice not sensitive enough.
SENSITIVITY = {"low": 1.3, "medium": 1.55, "high": 1.85}

_banks = {}


def _mel_bank(np):
    key = id(np)
    if key not in _banks:
        def hz_to_mel(hz):
            return 2595 * np.log10(1 + hz / 700)

        def mel_to_hz(mel):
            return 700 * (10 ** (mel / 2595) - 1)
        mels = np.linspace(hz_to_mel(60), hz_to_mel(7600), N_MELS + 2)
        bins = np.floor((N_FFT + 1) * mel_to_hz(mels) / RATE).astype(int)
        bank = np.zeros((N_MELS, N_FFT // 2 + 1))
        for index in range(1, N_MELS + 1):
            left, centre, right = bins[index - 1], bins[index], bins[index + 1]
            for k in range(left, centre):
                bank[index - 1, k] = (k - left) / max(1, centre - left)
            for k in range(centre, right):
                bank[index - 1, k] = (right - k) / max(1, right - centre)
        n = np.arange(N_MELS)
        dct = np.cos(np.pi / N_MELS * (n[None, :] + .5) * np.arange(N_MFCC)[:, None])
        _banks[key] = (bank, dct)
    return _banks[key]


def _normalise(np, mfcc):
    return (mfcc - mfcc.mean(axis=0)) / (mfcc.std(axis=0) + 1e-6)


def features(np, audio, normalise=True, span=False):
    """MFCC frames (n x 13) of the voiced part of 16 kHz mono audio, mean/variance normalised.
    span=True also returns the first voiced frame (where the frames start in the audio)."""
    audio = np.asarray(audio, dtype=np.float64).ravel()
    empty = np.zeros((0, N_MFCC))
    if audio.size < FRAME * 3:
        return (empty, 0) if span else empty
    count = 1 + (audio.size - FRAME) // HOP
    index = np.arange(FRAME)[None, :] + HOP * np.arange(count)[:, None]
    energy = np.sqrt(np.mean(audio[index] ** 2, axis=1))  # loudness of the sound as heard
    emphasised = np.append(audio[0], audio[1:] - .97 * audio[:-1])
    frames = emphasised[index] * np.hanning(FRAME)
    # Speech starts where the sound rises clearly above the room's noise floor (the
    # listener keeps ~0.3 s of room sound before every utterance).
    floor = np.percentile(energy, 10)
    voiced = np.nonzero(energy > max(energy.max() * .1, min(floor * 4, energy.max() * .3), 1e-4))[0]
    if voiced.size == 0:
        return (empty, 0) if span else empty
    frames = frames[voiced[0]:voiced[-1] + 1]
    power = np.abs(np.fft.rfft(frames, N_FFT)) ** 2 / N_FFT
    bank, dct = _mel_bank(np)
    logmel = np.log(power @ bank.T + 1e-10)
    mfcc = logmel @ dct.T
    result = _normalise(np, mfcc) if normalise else mfcc
    return (result, int(voiced[0])) if span else result


def speech_end(np, audio):
    """Seconds from the start of `audio` to the end of its last clearly voiced frame."""
    audio = np.asarray(audio, dtype=np.float64).ravel()
    if audio.size < FRAME:
        return 0.0
    count = 1 + (audio.size - FRAME) // HOP
    index = np.arange(FRAME)[None, :] + HOP * np.arange(count)[:, None]
    energy = np.sqrt(np.mean(audio[index] ** 2, axis=1))
    floor = np.percentile(energy, 10)
    voiced = np.nonzero(energy > max(energy.max() * .1, min(floor * 4, energy.max() * .3), 1e-4))[0]
    return round(float((voiced[-1] * HOP + FRAME) / RATE), 2) if voiced.size else 0.0


def prefix_distance(np, template, query):
    """How well the start of `query` matches the whole `template` (lower is closer),
    and where in `query` (frames) the match ends. Dynamic time warping with a free end."""
    n, m = len(template), len(query)
    if n == 0 or m == 0:
        return float("inf"), 0
    m = min(m, int(n * 1.8) + 5)  # the wake word can't be much slower than in the samples
    cost = np.sqrt(((template[:, None, :] - query[None, :m, :]) ** 2).sum(axis=2))
    inf = float("inf")
    acc = np.full((n + 1, m + 1), inf)
    steps = np.zeros((n + 1, m + 1))
    acc[0, 0] = 0.0
    for i in range(1, n + 1):
        row, row_steps = acc[i], steps[i]
        up, diag = acc[i - 1], acc[i - 1]
        for j in range(1, m + 1):
            best, came = diag[j - 1], steps[i - 1, j - 1]
            if up[j] < best:
                best, came = up[j], steps[i - 1, j]
            if row[j - 1] < best:
                best, came = row[j - 1], row_steps[j - 1]
            row[j] = cost[i - 1, j - 1] + best
            row_steps[j] = came + 1
    last = acc[n, 1:] / np.maximum(steps[n, 1:], 1)
    end = int(np.argmin(last))
    return float(last[end]), end + 1


class WakeVoice:
    """Templates of your wake word and a calibrated threshold."""

    def __init__(self, np, templates, sensitivity="medium"):
        self.np = np
        self.templates = [np.asarray(t, dtype=np.float64) for t in templates if len(t) >= 8]
        if len(self.templates) < 3:
            raise ValueError('At least 3 clear wake-word recordings are required.')
        pairs = [prefix_distance(np, a, b)[0] for i, a in enumerate(self.templates)
                 for j, b in enumerate(self.templates) if i != j]
        self.typical = float(np.median(pairs))
        self.threshold = max(1e-6, self.typical * SENSITIVITY.get(sensitivity, SENSITIVITY["medium"]))

    def match(self, audio):
        """(matched, score, end_seconds): score is distance / threshold (below 1 wakes);
        end_seconds is where the wake word ends, from the start of `audio`."""
        np = self.np
        head = np.asarray(audio)[:int(RATE * MAX_TEMPLATE_S * 1.8)]
        raw, first = features(np, head, normalise=False, span=True)
        if len(raw) < 8:
            return False, float("inf"), 0.0
        results = []
        for template in self.templates:
            # Normalise only a window about as long as the wake word, so whatever you say
            # Words following the wake name do not change the name's feature representation.
            window = raw[:max(8, int(len(template) * 1.25))]
            results.append(prefix_distance(np, template, _normalise(np, window)))
        results.sort()
        # The best two templates must agree, so one lucky sample can't wake MYN.
        score = (results[0][0] + results[min(1, len(results) - 1)][0]) / 2 / self.threshold
        end = ((first + results[0][1] - 1) * HOP + FRAME) / RATE
        return score < 1.0, round(score, 3), round(end, 2)


def template_of(np, audio):
    """Enrollment: the features of one spoken sample, as plain lists for JSON."""
    head = np.asarray(audio)[:int(RATE * MAX_TEMPLATE_S)]
    return [[round(float(v), 4) for v in row] for row in features(np, head)]
