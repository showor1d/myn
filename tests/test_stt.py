"""Speech input: settings, transcript cleanup, worker protocol and process client.

No microphone, GPU or model is needed: the recorder, recogniser and worker process
are replaced by in-memory fakes. The real Whisper path is checked on the user's
computer by `stt_worker.py --self-test` (run by setup_voice_input.ps1).
"""

import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src" / "core")]

import stt

try:
    import numpy as np
except ImportError:  # MYN's own Python 3.9 has no numpy; the worker runs elsewhere.
    np = None


class SettingsAndCleanup(unittest.TestCase):
    def test_settings_default_round_trip_and_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(stt.load_settings(folder), stt.STTSettings(enabled=True, language="auto", device=""))
            stt.save_settings(folder, stt.STTSettings(device="Microphone (Realtek Audio)"))
            self.assertEqual(stt.load_settings(folder).device, "Microphone (Realtek Audio)")
            stt.save_settings(folder, stt.STTSettings(enabled=False, language="ja"))
            self.assertEqual(stt.load_settings(folder), stt.STTSettings(enabled=True, language="ja"))
            with self.assertRaises(ValueError):
                stt.save_settings(folder, stt.STTSettings(language="fr"))
            (Path(folder) / stt.SETTINGS_NAME).write_text("{bad", encoding="utf-8")
            with self.assertRaises(stt.STTError):
                stt.load_settings(folder)

    def test_silence_inventions_are_dropped_and_text_is_tidied(self):
        for invented in ("谢谢观看。", "Thank you for watching!", " ご視聴ありがとうございました ", "。。", ""):
            self.assertEqual(stt.clean_transcript(invented), "", invented)
        self.assertEqual(stt.clean_transcript(stt.ZH_PROMPT + " 明天  早上提醒我开会"), "明天 早上提醒我开会")
        self.assertEqual(stt.clean_transcript("谢谢观看这部片子之后我们聊聊"), "谢谢观看这部片子之后我们聊聊")
        self.assertEqual(stt.clean_transcript(None), "")

    def test_wake_word_only_counts_at_the_start(self):
        words = stt.DEFAULT_WAKE_WORDS
        self.assertEqual(stt.strip_wake("Hey，Mine，今天天气怎么样？", words), "今天天气怎么样？")
        self.assertEqual(stt.strip_wake("Hey Mine", words), "")
        self.assertEqual(stt.strip_wake("Hey Mine, what's up?", words), "what's up?")
        self.assertIsNone(stt.strip_wake("jarvis。", words))
        self.assertIsNone(stt.strip_wake("我昨天看了贾维斯", words))
        self.assertIsNone(stt.strip_wake("今天好累", words))
        self.assertEqual(stt.strip_wake("小明 帮我记一下", "小明"), "帮我记一下")
        self.assertEqual(stt.wake_word_list(" a，b、 ,c "), ["a", "b", "c"])

    def test_spoken_commands(self):
        cases = {"打开声音设置": "voice_settings", "open voice settings": "voice_settings",
                 "打开记忆管理": None, "打开 Agents": "agents", "打开个人资料库": "personal",
                 "打开设置": "settings", "停": "stop", "别说了！": "stop", "Stop.": "stop",
                 "停止任务": "agent_stop", "取消后台任务": "agent_stop", "看看任务进度": None,
                 "打开后台任务": None, "我今天的任务很多": None,
                 "今天天气怎么样": None, "我不想停下来学习": None,
                 "你能不能帮我打开思路想想怎么设置目标然后写一份很长的计划书": None}
        for text, action in cases.items():
            self.assertEqual(stt.voice_command(text), action, text)

    def test_always_listen_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertTrue(stt.load_settings(folder).always_listen)
            stt.save_settings(folder, stt.STTSettings(always_listen=True, wake_words="小明, MYN"))
            loaded = stt.load_settings(folder)
            self.assertEqual((loaded.always_listen, loaded.wake_words), (True, "Hey Mine"))
            with self.assertRaises(ValueError):
                stt.STTSettings(wake_words=" , ").validate()

    def test_missing_parts_names_what_setup_must_provide(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            python = root / "venv" / "Scripts" / "python.exe"
            model = root / "model"
            self.assertEqual(stt.missing_parts(python, model), ['Local speech runtime', 'Whisper recognition model'])
            python.parent.mkdir(parents=True)
            python.write_bytes(b"")
            self.assertIn('sounddevice recording component', stt.missing_parts(python, model))
            site = root / "venv" / "Lib" / "site-packages"
            site.mkdir(parents=True)
            (site / "sounddevice.py").write_text("", encoding="utf-8")
            model.mkdir()
            for name in ("config.json", "model.safetensors", "preprocessor_config.json"):
                (model / name).write_text("{}", encoding="utf-8")
            self.assertEqual(stt.missing_parts(python, model), [])


FAKE_WORKER = textwrap.dedent('''
    import json, sys
    replies = {"ready": {"ok": True, "ready": True},
               "start": {"ok": True, "listening": True},
               "devices": {"ok": True, "devices": ["Mic A", "Mic B", 7]},
               "unlisten": {"ok": True, "listening": False},
               "cancel": {"ok": True, "text": ""}}
    wake = []
    for line in sys.stdin.buffer:
        request = json.loads(line)
        command = request["command"]
        if command == "wake_voice":
            wake = request["templates"]
            reply = {"ok": True, "wake_voice": bool(wake), "templates": len(wake)}
            sys.stdout.write(json.dumps(reply) + "\\n")
            sys.stdout.flush()
            continue
        if command == "listen":
            sys.stdout.write(json.dumps({"ok": True, "listening": "continuous", "device": request["device"],
                                         "fallback": False}) + "\\n")
            sys.stdout.write(json.dumps({"event": "heard", "text": "听到了", "start": 1.0, "end": 2.0},
                                        ensure_ascii=False) + "\\n")
            sys.stdout.flush()
            continue
        if command == "stop":
            if request["language"] == "en":
                reply = {"ok": False, "error": "无法打开麦克风；测试"}
            elif request["language"] == "ja":
                sys.exit(3)  # simulated crash
            else:
                reply = {"ok": True, "text": "谢谢观看" if request["language"] == "auto" else " 明天 提醒我 ",
                         "wake_count": len(wake)}
                if request.get("enroll"):
                    reply["template"] = [[0.5] * 13] * 9
        else:
            reply = replies[command]
        sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\\n")
        sys.stdout.flush()
''')


class WakeVoiceStorage(unittest.TestCase):
    def test_round_trip_validation_and_broken_files(self):
        sample = [[0.25] * 13] * 10
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(stt.load_wake_voice(folder), {"templates": [], "sensitivity": "medium"})
            stt.save_wake_voice(folder, [sample] * 4, "low")
            self.assertEqual(stt.load_wake_voice(folder), {"templates": [sample] * 4, "sensitivity": "low"})
            with self.assertRaises(stt.STTError):
                stt.save_wake_voice(folder, [sample] * 2)  # too few to calibrate
            with self.assertRaises(stt.STTError):
                stt.save_wake_voice(folder, [sample] * 3, "max")
            stt.save_wake_voice(folder, [sample] * 11)
            self.assertEqual(len(stt.load_wake_voice(folder)["templates"]), stt.WAKE_VOICE_MAX)
            stt.save_wake_voice(folder, [])  # cleared
            self.assertEqual(stt.load_wake_voice(folder)["templates"], [])
            path = Path(folder) / stt.WAKE_VOICE_NAME
            for broken in ("{", "[]", json.dumps({"templates": [[["x"] * 13] * 10] * 3}),
                           json.dumps({"templates": [sample] * 3, "sensitivity": "max"})):
                path.write_text(broken, encoding="utf-8")
                loaded = stt.load_wake_voice(folder)
                self.assertIn(loaded["sensitivity"], stt.WAKE_SENSITIVITIES)
            self.assertEqual(len(loaded["templates"]), 8)  # recover last verified file, not corrupt input
            self.assertNotIn("audio", path.read_text(encoding="utf-8"))

    def test_rest_after_a_wake_word_recognised_by_voice(self):
        self.assertEqual(stt.voice_wake_rest("Main, what time is it"), "what time is it")
        self.assertEqual(stt.voice_wake_rest("迈恩，今天天气怎么样"), "今天天气怎么样")
        self.assertEqual(stt.voice_wake_rest("万恩把音量调大一点。"), "把音量调大一点。")  # no pause: name length
        self.assertEqual(stt.voice_wake_rest("萬恩"), "")
        self.assertEqual(stt.voice_wake_rest("万恩，"), "")
        self.assertEqual(stt.wake_syllables("MYn"), 2)
        self.assertEqual(stt.wake_syllables("贾维斯"), 3)
        self.assertEqual(stt.voice_wake_rest(""), "")


class WakeDisplay(unittest.TestCase):
    def test_wake_word_is_written_as_you_set_it(self):
        words = "麦恩, Mein, Mane"
        self.assertEqual(stt.show_wake("Maia, what's the weather today?", words), "麦恩, what's the weather today?")
        self.assertEqual(stt.show_wake("迈恩，你好", words), "麦恩，你好")
        self.assertEqual(stt.show_wake("Mein今天几点", words), "麦恩今天几点")  # learned spelling
        self.assertEqual(stt.show_wake("I told Mai En yesterday", words), "I told 麦恩 yesterday")
        self.assertEqual(stt.show_wake("MYN在吗", words), "麦恩在吗")
        # the name as the user set it ("MYn"), whatever Chinese characters it came out as
        for heard in ("万恩把音量调大一点。", "迈恩把音量调大一点。", "萬恩把音量调大一点。", "麥恩把音量调大一点。"):
            self.assertEqual(stt.show_wake(heard, "MYn"), "MYn把音量调大一点。")
            self.assertEqual(stt.strip_wake(heard, "MYn"), "把音量调大一点。")
        for text in ("Mayan ruins are old", "Maintain it", "my friend", "Hello"):
            self.assertEqual(stt.show_wake(text, words), text)
        self.assertEqual(stt.strip_wake(stt.show_wake("Maya, hi", words), words), "hi")
        self.assertEqual(stt.voice_wake_rest("Mine今天几点"), "今天几点")


class Backchannel(unittest.TestCase):
    def test_listening_noises_do_not_interrupt(self):
        for text in ("嗯", "嗯嗯。", "对对", "好的", "Yeah.", "うん", "", "，"):
            self.assertTrue(stt.is_backchannel(text), text)
        for text in ("等一下", "停", "我换个问题", "好的你先停一下", "no wait"):
            self.assertFalse(stt.is_backchannel(text), text)


class EchoChecks(unittest.TestCase):
    def test_own_voice_through_the_speakers_is_told_apart_from_yours(self):
        spoken = ["今天东京晴，最高气温二十三度。", "下午可能有阵雨，出门记得带伞。"]
        self.assertGreaterEqual(stt.echo_overlap("今天东京晴最高气温23度", spoken), 0.45)
        self.assertEqual(stt.echo_overlap("下午可能有阵雨，出门记得带伞", spoken), 1.0)
        self.assertLess(stt.echo_overlap("等一下，我想问别的", spoken), 0.25)
        self.assertLess(stt.echo_overlap("停", spoken), 0.25)
        # a question on the same topic shares words with the reply but is not an echo
        self.assertLess(stt.echo_overlap("东京明天会下雨吗", spoken), 0.25)
        self.assertLess(stt.echo_overlap("你说的天气是哪里的", spoken), 0.25)
        self.assertLess(stt.echo_overlap("Okay stop", ["It will rain later today."]), 0.25)
        self.assertEqual(stt.echo_overlap("。", spoken), 1.0)  # nothing said at all
        self.assertEqual(stt.echo_overlap("你好", []), 0.0)


class ClientChecks(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        worker = Path(folder.name) / "fake_worker.py"
        worker.write_text(FAKE_WORKER, encoding="utf-8")
        self.client = stt.LocalSTTClient(command=[sys.executable, "-u", str(worker)],
                                         check_installed=lambda: None)
        self.addCleanup(self.client.close)

    def test_round_trip_cleans_transcripts(self):
        self.assertFalse(self.client.ready)
        self.client.warm()
        self.assertTrue(self.client.ready)
        self.assertEqual(self.client.devices(), ["Mic A", "Mic B"])
        self.client.start()
        self.assertEqual(self.client.stop("zh"), "明天 提醒我")
        self.client.start()
        self.assertEqual(self.client.stop("auto"), "")  # silence invention dropped
        self.client.cancel()

    def test_worker_errors_and_crashes_become_user_errors_then_recover(self):
        with self.assertRaises(stt.STTError) as caught:
            self.client.stop("en")
        self.assertIn("无法打开麦克风", str(caught.exception))
        with self.assertRaises(stt.STTError):
            self.client.stop("ja")
        self.assertIsNone(self.client.process)
        self.assertEqual(self.client.stop("zh"), "明天 提醒我")  # restarted automatically
        with self.assertRaises(stt.STTError):
            self.client.stop("fr")

    def test_events_are_separated_from_replies_and_listening_survives_a_crash(self):
        import threading
        events, arrived = [], threading.Event()
        self.client.on_event = lambda message: (events.append(message), arrived.set())
        reply = self.client.listen("Mic A", "auto")
        self.assertEqual(reply["listening"], "continuous")
        self.assertTrue(arrived.wait(5))
        self.assertEqual(events[0]["text"], "听到了")
        self.assertEqual(self.client.devices(), ["Mic A", "Mic B"])  # replies still in order
        arrived.clear()
        with self.assertRaises(stt.STTError):
            self.client.stop("ja")  # worker crashes
        self.assertEqual(self.client.devices(), ["Mic A", "Mic B"])  # restarted...
        self.assertTrue(arrived.wait(5))  # ...and listening again before that request
        self.assertEqual(len(events), 2)
        self.client.unlisten()
        self.assertIsNone(self.client.listen_args)
        with self.assertRaises(stt.STTError):
            self.client.listen("Mic A", "fr")

    def test_wake_voice_enrollment_and_restore_after_a_crash(self):
        text, template = self.client.stop_enroll("zh")
        self.assertEqual(text, "明天 提醒我")
        self.assertEqual(len(template), 9)
        self.assertEqual(self.client.set_wake_voice([template] * 3, "high")["templates"], 3)
        with self.assertRaises(stt.STTError):
            self.client.stop("ja")  # worker crashes
        # The restarted worker gets the voice samples back before anything else.
        self.assertEqual(self.client._request({"command": "stop", "language": "zh"}, 10)["wake_count"], 3)
        self.client.set_wake_voice([], "medium")
        self.assertIsNone(self.client.wake_args)
        self.assertEqual(self.client._request({"command": "stop", "language": "zh"}, 10)["wake_count"], 0)

    def test_not_installed_is_reported_before_any_process_starts(self):
        def refuse():
            raise stt.STTError("语音输入尚未安装")
        client = stt.LocalSTTClient(command=[sys.executable, "-c", "pass"], check_installed=refuse)
        with self.assertRaises(stt.STTError):
            client.start()
        self.assertIsNone(client.process)


@unittest.skipIf(np is None, "numpy is only present in the worker's environment")
class WorkerLoopChecks(unittest.TestCase):
    def setUp(self):
        import stt_worker
        self.worker = stt_worker

    def run_loop(self, commands, recorder, transcribe):
        replies = []
        lines = [json.dumps(command).encode("utf-8") + b"\n" for command in commands]
        self.worker.serve(lines, replies.append, recorder, transcribe, {"ok": True, "ready": True}, np)
        return replies

    class FakeRecorder:
        def __init__(self, audio):
            self.audio, self.calls = audio, []

        def start(self, device=None):
            self.calls.append("start")
            return device or ""

        def stop(self):
            self.calls.append("stop")
            return self.audio

        def cancel(self):
            self.calls.append("cancel")

    def test_speech_is_transcribed_with_the_chosen_language(self):
        rate = self.worker.SAMPLE_RATE
        speech = (0.2 * np.sin(np.arange(rate) / 5)).astype(np.float32)
        recorder = self.FakeRecorder(speech)
        seen = []
        replies = self.run_loop(
            [{"command": "ready"}, {"command": "start"}, {"command": "stop", "language": "zh"}],
            recorder, lambda audio, language: seen.append((audio.size, language)) or "你好 MYN")
        self.assertEqual(replies[0]["ready"], True)
        self.assertEqual(replies[1], {"ok": True, "listening": True, "device": "", "fallback": False})
        self.assertEqual(replies[2]["text"], "你好 MYN")
        self.assertEqual(replies[2]["seconds"], 1.0)
        self.assertEqual(seen, [(rate, "zh")])

    def test_silence_and_taps_skip_the_model(self):
        rate = self.worker.SAMPLE_RATE
        for audio in (np.zeros(rate, np.float32), (0.3 * np.ones(rate // 10)).astype(np.float32)):
            replies = self.run_loop([{"command": "stop", "language": "zh"}], self.FakeRecorder(audio),
                                    lambda *_: self.fail("model must not run"))
            self.assertEqual(replies[0]["text"], "")
            self.assertTrue(replies[0]["silent"])

    def test_bad_commands_and_failures_answer_with_errors(self):
        def broken(*_):
            raise RuntimeError("boom")
        loud = (0.2 * np.ones(self.worker.SAMPLE_RATE)).astype(np.float32)
        replies = self.run_loop([{"command": "dance"}, {"command": "stop", "language": "fr"},
                                 {"command": "stop", "language": "zh"}, {"command": "cancel"}],
                                self.FakeRecorder(loud), broken)
        self.assertEqual([reply["ok"] for reply in replies], [False, False, False, True])
        self.assertIn("Invalid recognition language", replies[1]["error"])
        self.assertNotIn("boom", replies[2]["error"])

    def fake_sd(self):
        """A stand-in sounddevice: two microphones, each listed under MME and WASAPI."""
        opened = []

        class FakeStream:
            def __init__(self, device, samplerate, channels, dtype, callback, blocksize=None):
                if samplerate == 16000 and sd.refuse_16k:
                    raise RuntimeError("Invalid sample rate")
                self.callback, self.rate = callback, samplerate
                opened.append((device, samplerate))

            def start(self):
                if getattr(self.callback, "__name__", "") != "_callback" or \
                        type(getattr(self.callback, "__self__", None)).__name__ != "Recorder":
                    return  # the always-on listener is fed by tests directly
                block = np.full((self.rate, 1), 0.1, np.float32)
                for _ in range(stt.MAX_SECONDS + 5):  # longer than the cap
                    self.callback(block, self.rate, None, None)

            def stop(self):
                pass

            def close(self):
                pass

        devices = [
            {"name": "Microsoft Sound Mapper - Input", "hostapi": 0, "max_input_channels": 2, "default_samplerate": 44100.0},
            {"name": "Headset Microphone (USB Audio", "hostapi": 0, "max_input_channels": 1, "default_samplerate": 44100.0},
            {"name": "Speakers (Realtek)", "hostapi": 1, "max_input_channels": 0, "default_samplerate": 48000.0},
            {"name": "Microphone (Realtek Audio)", "hostapi": 1, "max_input_channels": 2, "default_samplerate": 48000.0},
            {"name": "Headset Microphone (USB Audio Device)", "hostapi": 1, "max_input_channels": 1, "default_samplerate": 32000.0},
        ]

        class sd:  # noqa: N801 - stands in for the sounddevice module
            refuse_16k = False
            InputStream = FakeStream

            @staticmethod
            def query_hostapis():
                return [{"name": "MME"}, {"name": "Windows WASAPI"}]

            @staticmethod
            def query_devices(device=None, kind=None):
                if device is not None:
                    return devices[device]
                if kind == "input":
                    return devices[0]
                return devices

        sd.opened = opened
        return sd

    def test_recorder_caps_length_and_resamples_when_16k_is_refused(self):
        sd = self.fake_sd()
        recorder = self.worker.Recorder(sd, np)
        self.assertEqual(recorder.start(), "")
        self.assertEqual(recorder.stop().size, 16000 * stt.MAX_SECONDS)
        sd.refuse_16k = True
        recorder.start()
        self.assertEqual(recorder.rate, 44100)  # the default device's own rate
        audio = recorder.stop()
        self.assertEqual(audio.size, 16000 * stt.MAX_SECONDS)
        self.assertAlmostEqual(float(audio.mean()), 0.1, places=5)

    def test_microphones_are_listed_once_with_full_names(self):
        recorder = self.worker.Recorder(self.fake_sd(), np)
        self.assertEqual(recorder.input_devices(),
                         ["Microphone (Realtek Audio)", "Headset Microphone (USB Audio Device)"])

    def test_chosen_microphone_is_opened_at_its_own_rate_or_falls_back(self):
        sd = self.fake_sd()
        recorder = self.worker.Recorder(sd, np)
        sd.refuse_16k = True
        used = recorder.start("Headset Microphone (USB Audio Device)")
        self.assertEqual(used, "Headset Microphone (USB Audio Device)")
        self.assertEqual(sd.opened[-1], (4, 32000))
        self.assertEqual(recorder.stop().size, 16000 * stt.MAX_SECONDS)
        self.assertEqual(recorder.start("Unplugged Mic"), "")  # system default instead
        self.assertIsNone(sd.opened[-1][0])
        recorder.cancel()

    def make_listener(self, heard):
        rate = self.worker.SAMPLE_RATE
        listener = self.worker.Listener(object(), np, lambda audio, language: "第%d句" % (len(heard) + 1),
                                        heard.append, threaded=False)
        listener.rate = rate
        block = int(rate * listener.BLOCK_S)
        clock = {"t": 1000.0}

        def play(seconds, level, tone=True):
            for _ in range(int(seconds / listener.BLOCK_S)):
                if tone:
                    samples = level * np.sin(np.arange(block) / 3.0)
                else:
                    samples = np.random.default_rng(1).normal(0, level, block)
                clock["t"] += listener.BLOCK_S
                listener.feed(samples.astype(np.float32), clock["t"])
        return listener, play, clock

    def test_misheard_wake_word_still_wakes(self):
        # Real phonetic wake transcripts with automatic language detection.
        for heard, rest in (("Maia, what's the weather today?", "what's the weather today?"),
                            ("Maen, what's the weather today?", "what's the weather today?"),
                            ("Mya, good evening.", "good evening."), ("麦恩，晚上好", "晚上好"),
                            ("迈恩 你好", "你好"), ("Hey Maya, hi", "hi"), ("Maya", "")):
            self.assertEqual(stt.strip_wake(heard, "麦恩"), rest, heard)
        for other in ("Mayan ruins are old", "Maintain it", "他叫麦恩。", "我昨天看了 Maya 的视频"):
            self.assertIsNone(stt.strip_wake(other, "麦恩"), other)
        self.assertIsNone(stt.strip_wake("Maia, hello", "贾维斯"))  # only for the configured name

    def test_wake_word_forms_learned_from_your_voice(self):
        self.assertEqual(stt.learned_form("Mein."), "Mein")
        self.assertEqual(stt.learned_form(" 麦恩！"), "麦恩")
        self.assertEqual(stt.learned_form("Mai en"), "Mai en")
        for unusable in ("", "My", "hi!", "我", "我想问一下今天的天气怎么样呢"):
            self.assertEqual(stt.learned_form(unusable), "", unusable)
        settings = stt.STTSettings(wake_words="麦恩", wake_learned="Moin, Maun")
        self.assertEqual(stt.strip_wake("Moin, turn on the light", settings.all_wake_words), "turn on the light")
        self.assertEqual(stt.strip_wake("Maun 你好", settings.all_wake_words), "你好")
        self.assertIsNone(stt.strip_wake("Moin, turn on the light", stt.STTSettings(wake_words="麦恩").all_wake_words))
        folder = Path(tempfile.mkdtemp())
        stt.save_settings(folder, settings)
        self.assertEqual(stt.load_settings(folder).wake_learned, "")  # fixed phrase, no old aliases
        self.assertNotIn("all_wake_words", (folder / stt.SETTINGS_NAME).read_text(encoding="utf-8"))
        with self.assertRaises(ValueError):
            stt.STTSettings(wake_learned="x" * 401).validate()

    def test_more_spellings_of_the_wake_word_from_real_transcripts(self):
        # 2026-10-17: these came out of real speech and did not wake MYN before
        for said, rest in (("马眼把音量调大一点。", "把音量调大一点。"), ("马尤你能放首歌吗？", "你能放首歌吗？"),
                           ("马亚给我放首歌", "给我放首歌"), ("曼尔今天天气怎么样", "今天天气怎么样"),
                           ("满羊打开设置", "打开设置"), ("马尔你现在在干嘛", "你现在在干嘛"),
                           ("Mein, what time is it?", "what time is it?"), ("嘿，马眼", "")):
            with self.subTest(said=said):
                self.assertEqual(stt.strip_wake(said, "MYn"), rest)
                self.assertTrue(stt.show_wake(said, "MYn").replace("嘿，", "").startswith("MYn"))
        # real words, and the sound-alike in the middle of a sentence, do not wake or get renamed
        for said in ("马尔代夫好玩吗", "买烟去了", "马上打开", "万安是个地方", "我昨天看了马眼", "麦当劳开门了吗"):
            with self.subTest(said=said):
                self.assertIsNone(stt.strip_wake(said, "MYn"))
                self.assertEqual(stt.show_wake(said, "MYn"), said)
        self.assertIsNone(stt.strip_wake("马眼把音量调大", "贾维斯"))  # Only for the MYN wake name.

    def test_noise_and_bare_sounds_are_dropped(self):
        # Real transcripts of game music and noise.
        for noise in ("ほほほほほほほほほほほほほほほほほほほほ", "Da da da da da da da da da da da da da d",
                      "Dun-dun-dun-dun-dun-dun.", "ダンダンダンダンダンダン",
                      "たななだったったったったったったったったった", "おぉぉぉぉぉぉぉぉぉぉぉぉ"):
            self.assertEqual(stt.clean_transcript(noise), "", noise)
        for speech in ("哈哈哈哈哈哈你太逗了", "对对对对对", "谢谢谢谢", "晚上好。", "ちょっと不要、ちょっと不要"):
            self.assertEqual(stt.clean_transcript(speech), speech)
        for sound in ("Oh", "uh", "Wow.", "Aww!", "ああ", "哦。"):
            self.assertTrue(stt.is_filler(sound), sound)
        for reply in ("嗯", "对", "好的", "yes", "Oh no.", "哦,炸了。"):  # short answers still count
            self.assertFalse(stt.is_filler(reply), reply)

    def test_listener_reports_loudness_only_when_asked(self):
        events = []
        rate = self.worker.SAMPLE_RATE
        listener = self.worker.Listener(object(), np, lambda *_: "", events.append, threaded=False, levels=True)
        listener.rate = rate
        block = int(rate * listener.BLOCK_S)
        t = 1000.0
        for level in [0.003] * 30 + [0.2] * 30:
            t += listener.BLOCK_S
            samples = level * np.sin(np.arange(block) / 3.0) if level > .01 else \
                np.random.default_rng(1).normal(0, level, block)
            listener.feed(samples.astype(np.float32), t)
        levels = [e for e in events if e["event"] == "level"]
        self.assertGreater(len(levels), 15)              # about 12 a second
        self.assertLess(max(e["level"] for e in levels[:8]), .1)   # room noise: quiet
        self.assertGreater(levels[-1]["level"], .9)      # a voice: loud
        self.assertTrue(levels[-1]["speech"])
        self.assertEqual(set(levels[0]), {"event", "level", "speech", "bands"})
        self.assertTrue(all(len(event['bands']) == 128 for event in levels))
        self.assertTrue(all(0 <= value <= 1 for event in levels for value in event['bands']))
        # Only magnitude numbers leave the worker, never PCM or a new recording stream.
        quiet = []
        silent = self.worker.Listener(object(), np, lambda *_: "", quiet.append, threaded=False)
        silent.rate = rate
        silent.feed((0.2 * np.sin(np.arange(block) / 3.0)).astype(np.float32), t)
        self.assertEqual(quiet, [])

    def test_listener_cuts_speech_into_utterances_and_ignores_noise(self):
        heard = []
        listener, play, clock = self.make_listener(heard)
        play(1.0, 0.003, tone=False)  # room noise sets the floor
        play(1.2, 0.2)                # speech
        play(1.0, 0.003, tone=False)  # pause ends the utterance
        self.assertEqual(len(heard), 1)
        self.assertEqual(heard[0]["text"], "第1句")
        self.assertAlmostEqual(heard[0]["end"] - heard[0]["start"], 1.2 + 0.3, delta=0.25)
        play(0.2, 0.2)                # a click or cough: too short
        play(1.0, 0.003, tone=False)
        self.assertEqual(len(heard), 1)
        play(stt.MAX_SECONDS + 3, 0.2)  # someone talking without pause
        self.assertEqual(len(heard), 2)  # cut at the cap, not held forever
        play(1.0, 0.003, tone=False)
        listener.stop()

    def test_listener_reports_recogniser_errors_as_events(self):
        events = []
        listener = self.worker.Listener(object(), np, lambda *_: (_ for _ in ()).throw(RuntimeError("x")),
                                        events.append, threaded=False)
        listener._process((np.ones(16000, np.float32), 1.0, 2.0, "auto"))
        self.assertEqual(events[0]["event"], "error")

    def test_short_reply_endpoint_is_faster_but_long_speech_keeps_normal_pause(self):
        for short, duration, expected in ((True, .6, 1), (False, .6, 0), (True, 3.6, 0)):
            with self.subTest(short=short, duration=duration):
                heard = []
                listener, play, clock = self.make_listener(heard)
                listener.short_reply_until = 2000 if short else 0
                play(1, .001, tone=False)
                play(duration, .2)
                play(.51, .001, tone=False)
                self.assertEqual(len(heard), expected)
                play(.24, .001, tone=False)
                self.assertEqual(len(heard), 1)
                self.assertEqual(heard[0]["silence_s"], .48 if short and duration < 3 else .7)

    def test_push_to_talk_pauses_and_resumes_listening(self):
        sd = self.fake_sd()
        recorder = self.worker.Recorder(sd, np)
        listener = self.worker.Listener(recorder, np, lambda *_: "", lambda *_: None, threaded=False)
        replies = self.run_loop_with([{"command": "listen", "device": "Microphone (Realtek Audio)",
                                       "language": "zh"},
                                      {"command": "start"}, {"command": "cancel"},
                                      {"command": "unlisten"}], recorder, listener)
        self.assertEqual(replies[0]["listening"], "continuous")
        self.assertEqual(listener.language, "zh")
        self.assertEqual([reply["ok"] for reply in replies], [True, True, True, True])
        self.assertFalse(listener.active)  # unlisten closed it
        devices = [device for device, _rate in sd.opened]
        # listen (Realtek), push-to-talk (default), listen resumed (Realtek again)
        self.assertEqual(devices, [3, None, 3])

    def run_loop_with(self, commands, recorder, listener):
        replies = []
        lines = [json.dumps(command).encode("utf-8") + b"\n" for command in commands]
        self.worker.serve(lines, replies.append, recorder, lambda *_: "", {"ok": True}, np, listener)
        return replies

    def test_worker_lists_devices_and_reports_fallback(self):
        sd = self.fake_sd()
        replies = self.run_loop([{"command": "devices"},
                                 {"command": "start", "device": "Microphone (Realtek Audio)"},
                                 {"command": "cancel"},
                                 {"command": "start", "device": "Gone"},
                                 {"command": "cancel"},
                                 {"command": "start", "device": 5}],
                                self.worker.Recorder(sd, np), lambda *_: "")
        self.assertEqual(replies[0]["devices"][0], "Microphone (Realtek Audio)")
        self.assertEqual((replies[1]["device"], replies[1]["fallback"]), ("Microphone (Realtek Audio)", False))
        self.assertEqual((replies[3]["device"], replies[3]["fallback"]), ("", True))
        self.assertFalse(replies[5]["ok"])

if __name__ == "__main__":
    unittest.main()
