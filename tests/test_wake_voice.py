"""Wake by your own voice: features, matching and the speech worker's commands.

Real voices are replaced by synthetic vowel glides (two formants moving over time),
which is enough to check that the same "word" matches when said faster, slower,
lower, in noise or followed by a sentence, and that other sounds do not.
Needs numpy (present in the speech worker's Python, not MYN's own).
"""

import json
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src" / "core")]

try:
    import numpy as np
except ImportError:
    np = None

R = 16000
WAKE = [(0, 300, 2200), (.35, 700, 1300), (.55, 650, 1100), (1, 350, 1900)]
OTHER = [(0, 750, 1200), (.5, 300, 900), (1, 500, 800)]
FLAT = [(0, 400, 2000), (1, 400, 2000)]


def word(rng, path, dur=.6, f0=120, tempo=1.0, noise=.01, pre=0.0, tail=None):
    d = dur / tempo
    t = np.arange(int(R * d)) / R
    u = t / d
    f1 = np.interp(u, [p[0] for p in path], [p[1] for p in path])
    f2 = np.interp(u, [p[0] for p in path], [p[2] for p in path])
    phase = 2 * np.pi * np.cumsum(np.full_like(t, f0)) / R
    sig = np.zeros_like(t)
    for h in range(1, 40):
        amp = np.exp(-((h * f0 - f1) / 110) ** 2) + .6 * np.exp(-((h * f0 - f2) / 160) ** 2) + .02
        sig += amp * np.sin(h * phase)
    sig = sig / np.abs(sig).max() * .3 * np.minimum(1, np.minimum(u / .08, (1 - u) / .12))
    parts = ([np.zeros(int(R * pre))] if pre else []) + [sig] + ([tail] if tail is not None else [])
    out = np.concatenate(parts)
    return (out + rng.normal(0, noise, out.size)).astype(np.float32)


@unittest.skipIf(np is None, "numpy is only present in the worker's environment")
class Matching(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import wake_voice
        cls.wv = wake_voice
        cls.rng = np.random.default_rng(1)
        cls.samples = [wake_voice.template_of(np, word(cls.rng, WAKE, .6, 118 + 6 * k, tempo=1 + .06 * (k - 2)))
                       for k in range(5)]
        cls.wake = wake_voice.WakeVoice(np, cls.samples)

    def test_templates_are_plain_json_numbers(self):
        template = self.samples[0]
        self.assertEqual(json.loads(json.dumps(template)), template)
        self.assertTrue(all(len(row) == 13 for row in template))
        self.assertGreater(len(template), 30)
        self.assertEqual(self.wv.template_of(np, np.zeros(R)), [])  # silence: no sample

    def test_needs_three_clear_samples(self):
        with self.assertRaises(ValueError):
            self.wv.WakeVoice(np, self.samples[:2])
        with self.assertRaises(ValueError):
            self.wv.WakeVoice(np, self.samples[:2] + [[[0.0] * 13] * 3])

    def test_same_word_wakes_in_many_ways_of_saying_it(self):
        rng = self.rng
        tail = word(rng, OTHER, .8, 130)
        for name, audio in {
            "faster": word(rng, WAKE, .6, 125, tempo=1.15),
            "slower, lower": word(rng, WAKE, .6, 105, tempo=.88),
            "noisy": word(rng, WAKE, .6, 120, noise=.03),
            "then a sentence": word(rng, WAKE, .6, 120, tail=tail),
            "room sound first": word(rng, WAKE, .6, 121, pre=.3, tail=tail, noise=.012),
        }.items():
            matched, score, _end = self.wake.match(audio)
            self.assertTrue(matched, "%s: %s" % (name, score))

    def test_other_sounds_do_not_wake(self):
        rng = self.rng
        for name, audio in {
            "other word": word(rng, OTHER, .7, 120),
            "flat vowel": word(rng, FLAT, .6, 120),
            "wake word only later": word(rng, OTHER, .7, 120, tail=word(rng, WAKE, .6, 120)),
            "noise": rng.normal(0, .05, R).astype(np.float32),
            "silence": np.zeros(R, dtype=np.float32),
        }.items():
            matched, score, _end = self.wake.match(audio)
            self.assertFalse(matched, "%s: %s" % (name, score))

    def test_where_the_wake_word_ends(self):
        tail = word(self.rng, OTHER, .8, 130)
        audio = word(self.rng, WAKE, .6, 121, pre=.3, tail=tail, noise=.012)
        _matched, _score, end = self.wake.match(audio)
        self.assertAlmostEqual(end, .9, delta=.15)
        self.assertGreater(self.wv.speech_end(np, audio) - end, .5)  # kept talking
        bare = word(self.rng, WAKE, .6, 120, pre=.3, noise=.012)
        _matched, _score, end = self.wake.match(bare)
        self.assertLess(self.wv.speech_end(np, bare) - end, .35)

    def test_sensitivity_moves_the_threshold(self):
        low = self.wv.WakeVoice(np, self.samples, "low")
        high = self.wv.WakeVoice(np, self.samples, "high")
        self.assertLess(low.threshold, self.wake.threshold)
        self.assertLess(self.wake.threshold, high.threshold)


@unittest.skipIf(np is None, "numpy is only present in the worker's environment")
class WorkerCommands(unittest.TestCase):
    def setUp(self):
        import stt_worker
        self.worker = stt_worker
        self.rng = np.random.default_rng(2)

    class Recorder:
        def __init__(self, audio):
            self.audio = audio

        def stop(self):
            return self.audio

        def cancel(self):
            pass

    class Listener:
        active = False
        device = ""
        wake = None

    def serve(self, commands, recorder, listener):
        replies = []
        lines = [json.dumps(command).encode("utf-8") + b"\n" for command in commands]
        self.worker.serve(lines, replies.append, recorder, lambda *_: "麦恩", {"ok": True}, np, listener)
        return replies

    def test_enroll_then_set_and_clear(self):
        listener = self.Listener()
        recorder = self.Recorder(word(self.rng, WAKE, .6, 120))
        replies = self.serve([{"command": "stop", "language": "zh", "enroll": True},
                            {"command": "stop", "language": "zh"}], recorder, listener)
        self.assertEqual(replies[0]["text"], "麦恩")
        template = replies[0]["template"]
        self.assertGreater(len(template), 30)
        self.assertNotIn("template", replies[1])  # only when training
        replies = self.serve([{"command": "wake_voice", "templates": [template] * 3, "sensitivity": "high"},
                            {"command": "wake_voice", "templates": [template] * 3, "sensitivity": "max"},
                            {"command": "wake_voice", "templates": "x"},
                            {"command": "wake_voice", "templates": [template] * 2}], recorder, listener)
        self.assertTrue(replies[0]["ok"] and replies[0]["wake_voice"])
        self.assertIsNotNone(listener.wake)
        self.assertEqual([reply["ok"] for reply in replies[1:]], [False, False, False])
        replies = self.serve([{"command": "wake_voice", "templates": []}], recorder, listener)
        self.assertEqual(replies[0], {"ok": True, "wake_voice": False})
        self.assertIsNone(listener.wake)

    def test_heard_events_say_whether_your_voice_woke_it(self):
        import wake_voice
        samples = [wake_voice.template_of(np, word(self.rng, WAKE, .6, 116 + 5 * k)) for k in range(4)]
        events = []
        listener = self.worker.Listener(object(), np, lambda *_: "随便什么字", events.append, threaded=False)
        listener._process((word(self.rng, WAKE, .6, 120, pre=.3, tail=word(self.rng, OTHER, .8, 130)), 1.0, 3.0, "auto"))
        self.assertNotIn("wake_voice", events[-1])  # off until set up
        listener.wake = wake_voice.WakeVoice(np, samples)
        listener._process((word(self.rng, WAKE, .6, 120, pre=.3, tail=word(self.rng, OTHER, .8, 130)), 1.0, 3.0, "auto"))
        self.assertTrue(events[-1]["wake_voice"])
        self.assertGreater(events[-1]["after_wake"], .5)
        self.assertEqual(events[-1]["text"], "随便什么字")
        listener._process((word(self.rng, OTHER, .7, 120, pre=.3), 1.0, 3.0, "auto"))
        self.assertFalse(events[-1]["wake_voice"])

    def test_a_bare_wake_word_with_no_transcript_is_still_reported(self):
        # A short "MYn" alone often comes out of speech recognition as nothing at all.
        import wake_voice
        samples = [wake_voice.template_of(np, word(self.rng, WAKE, .6, 116 + 5 * k)) for k in range(4)]
        events = []
        listener = self.worker.Listener(object(), np, lambda *_: "", events.append, threaded=False)
        listener.wake = wake_voice.WakeVoice(np, samples)
        listener._process((word(self.rng, WAKE, .6, 120, pre=.3), 1.0, 2.0, "auto"))
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["text"], events[0]["wake_voice"]), ("", True))
        listener._process((word(self.rng, OTHER, .7, 120, pre=.3), 1.0, 2.0, "auto"))
        self.assertEqual(len(events), 1)  # nothing said and no wake word: nothing reported

    def test_streaming_candidates_also_use_personal_features_and_only_crop_keyword(self):
        import wake_voice
        samples = [wake_voice.template_of(np, word(self.rng, WAKE, .6, 116 + 5 * k)) for k in range(4)]
        events = []
        listener = self.worker.Listener(object(), np, lambda *_: 'Hey Mine, what time is it?', events.append, threaded=False)
        listener.wake = wake_voice.WakeVoice(np, samples)
        audio = word(self.rng, WAKE, .6, 120, pre=.3, tail=word(self.rng, OTHER, 1.2, 130))
        listener._process((audio, 1., 3., 'en', .48,
                           dict(generation=listener.generation, streaming_wake=True, wake_voice=True)))
        event = events[-1]
        self.assertIsInstance(event.get('voice_score'), float)
        self.assertLess(event['voice_score'], .9)
        self.assertTrue(event.get('wake_template'))
        self.assertLess(len(event['wake_template']), 95)  # command tail is not in the feature template

    def test_bootstrap_never_records_full_command_without_a_reliable_boundary(self):
        events = []
        listener = self.worker.Listener(object(), np, lambda *_: 'Hey Mine, remind me tomorrow', events.append, threaded=False)
        listener._process((word(self.rng, WAKE, .6), 1., 2., 'en'))
        self.assertNotIn('wake_template', events[-1])
        listener.transcribe = lambda *_: 'Hey Mine'
        listener._process((word(self.rng, WAKE, .6), 1., 2., 'en'))
        self.assertTrue(events[-1].get('wake_template'))


if __name__ == "__main__":
    unittest.main()
