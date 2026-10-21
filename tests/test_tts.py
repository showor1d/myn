"""Speech boundaries: safe text, request contract, and stale playback."""

import io
import json
import os
import queue
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src" / "core")]

import tts
import credentials
import qwen3_local
from tts_player import MP3Player
from myn_gui import MYNWindow


class SpeechChecks(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="myn-speech-policy-")
        self.addCleanup(temp.cleanup)
        self.config = Path(temp.name)

    def test_markdown_and_code_are_not_sent_for_speech(self):
        source = "# 答复\n请看 [说明](https://example.com/path) 和 `password`。\n```python\nprint('private')\n```\n- 完成"
        cleaned = tts.speech_text(source)
        self.assertIn("答复", cleaned)
        self.assertIn('说明', cleaned)
        self.assertIn('Code block omitted', cleaned)
        self.assertNotIn("private", cleaned)
        self.assertNotIn("example.com", cleaned)
        self.assertNotIn("password", cleaned)

    def test_local_speech_splits_at_sentences_and_bounds_long_text(self):
        source = "你好，我是 MYN。今天检查语音速度。" + "没有标点" * 30 + "！最后一句。"
        segments = tts.speech_segments(source, max_chars=40)
        self.assertEqual("".join(segments), source)
        self.assertEqual(segments[0], "你好，我是 MYN。")
        self.assertTrue(all(len(part) <= 40 for part in segments))

    def test_settings_round_trip_excludes_api_key(self):
        with tempfile.TemporaryDirectory(prefix="myn-tts-") as folder:
            settings = tts.TTSSettings(auto_read=False, volume=0.25, speed=1.15,
                                       local_speaker="Serena")
            with mock.patch.dict(os.environ, {"ELEVENLABS_API_KEY": "synthetic-secret"}):
                tts.save_settings(folder, settings)
                from dataclasses import replace
                self.assertEqual(tts.load_settings(folder), replace(settings,auto_read=True,long_reply='opening'))
            saved = (Path(folder) / tts.SETTINGS_NAME).read_text(encoding="utf-8")
            self.assertNotIn("synthetic-secret", saved)
            self.assertNotIn("ELEVENLABS_API_KEY", saved)

    def test_elevenlabs_key_is_encrypted_and_environment_takes_priority(self):
        with tempfile.TemporaryDirectory(prefix="myn-tts-key-") as folder:
            with mock.patch.dict(os.environ, {"ELEVENLABS_API_KEY": ""}):
                credentials.save_elevenlabs_key(folder, "synthetic-local-key")
                encrypted = credentials.elevenlabs_key_path(folder)
                self.assertTrue(encrypted.is_file())
                self.assertNotIn(b"synthetic-local-key", encrypted.read_bytes())
                self.assertEqual(credentials.get_elevenlabs_key(folder), "synthetic-local-key")
                self.assertEqual(credentials.elevenlabs_key_source(folder), "saved")
                self.assertFalse(credentials.key_path(folder).exists())
                with mock.patch.dict(os.environ, {"ELEVENLABS_API_KEY": "synthetic-env-key"}):
                    self.assertEqual(credentials.get_elevenlabs_key(folder), "synthetic-env-key")
                    self.assertEqual(credentials.elevenlabs_key_source(folder), "environment")
                credentials.clear_elevenlabs_key(folder)
                self.assertEqual(credentials.elevenlabs_key_source(folder), "none")
                self.assertEqual(credentials.get_elevenlabs_key(folder), "")

    def test_elevenlabs_request_uses_saved_key(self):
        requests = []

        def success(request, timeout):
            requests.append(request)
            return io.BytesIO(b"ID3test-audio")

        with tempfile.TemporaryDirectory(prefix="myn-tts-key-") as folder:
            credentials.save_elevenlabs_key(folder, "synthetic-local-key")
            with mock.patch.dict(os.environ, {"ELEVENLABS_API_KEY": ""}), \
                    mock.patch.object(tts, "user_data_dir", return_value=Path(folder)):
                tts.ElevenLabsProvider(success).synthesize("你好", tts.TTSSettings())
        self.assertEqual(requests[0].get_header("Xi-api-key"), "synthetic-local-key")

    def test_elevenlabs_history_id_is_kept_so_the_daily_sync_does_not_count_twice(self):
        class Reply(io.BytesIO):
            headers = {"history-item-id": "abc123"}

        folder = tempfile.TemporaryDirectory(prefix="myn-tts-usage-")
        self.addCleanup(folder.cleanup)
        with mock.patch.dict(os.environ, {"ELEVENLABS_API_KEY": "synthetic-secret"}), \
                mock.patch.object(tts, "user_data_dir", return_value=Path(folder.name)):
            tts.ElevenLabsProvider(lambda request, timeout: Reply(b"ID3audio")).synthesize("你好", tts.TTSSettings())
        record = json.loads((Path(folder.name) / "other-usage.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(record["id"], "elevenlabs:abc123")

    def test_elevenlabs_request_and_authorization_error(self):
        requests = []

        def success(request, timeout):
            requests.append((request, timeout))
            return io.BytesIO(b"ID3test-audio")

        settings = tts.TTSSettings(speed=1.1)
        folder = tempfile.TemporaryDirectory(prefix="myn-tts-usage-")
        self.addCleanup(folder.cleanup)
        with mock.patch.dict(os.environ, {"ELEVENLABS_API_KEY": "synthetic-secret"}), \
                mock.patch.object(tts, "user_data_dir", return_value=Path(folder.name)):
            audio = tts.ElevenLabsProvider(success).synthesize("你好", settings)
            self.assertEqual(audio, b"ID3test-audio")
            # ElevenLabs bills by characters: the request is counted (numbers only, no text)
            usage = (Path(folder.name) / "other-usage.jsonl").read_text(encoding="utf-8")
            record = json.loads(usage.splitlines()[0])
            self.assertEqual((record["source"], record["characters"]), ("elevenlabs", 2))
            self.assertNotIn("id", record)  # no history id header in this reply
            self.assertAlmostEqual(record["cost_usd"], 0.0001)
            self.assertNotIn("你好", usage)
            request, timeout = requests[0]
            self.assertEqual(timeout, 30)
            self.assertIn("mp3_44100_128", request.full_url)
            self.assertEqual(json.loads(request.data)["voice_settings"]["speed"], 1.1)
            self.assertEqual(json.loads(request.data)["model_id"], "eleven_flash_v2_5")
            self.assertEqual(request.get_header("Xi-api-key"), "synthetic-secret")

            def forbidden(_request, timeout):
                raise urllib.error.HTTPError("https://api.elevenlabs.io", 401,
                                             "secret-response", {}, None)

            with self.assertRaises(tts.TTSError) as failure:
                tts.ElevenLabsProvider(forbidden).synthesize("你好", settings)
            self.assertNotIn("secret-response", str(failure.exception))

    def test_chinese_sentences_use_the_chinese_voice(self):
        self.assertTrue(tts.is_chinese("今天东京晴，17 度。"))
        self.assertTrue(tts.is_chinese("我在用 Python 写 MYN"))
        self.assertFalse(tts.is_chinese("How are you today?"))
        self.assertFalse(tts.is_chinese("The word 你好 means hello in Chinese."))
        self.assertFalse(tts.is_chinese(""))
        both = tts.TTSSettings(voice_id="EnglishVoice01", voice_id_zh="ChineseVoice01")
        self.assertEqual(tts.voice_for("晚上好，今天过得怎么样？", both), "ChineseVoice01")
        self.assertEqual(tts.voice_for("Good evening, how was your day?", both), "EnglishVoice01")
        only = tts.TTSSettings(voice_id="EnglishVoice01")
        self.assertEqual(tts.voice_for("晚上好", only), "EnglishVoice01")  # no Chinese voice set
        urls = []

        def capture(request, timeout):
            urls.append(request.full_url)
            return io.BytesIO(b"ID3audio")
        folder = tempfile.TemporaryDirectory(prefix="myn-tts-usage-")
        self.addCleanup(folder.cleanup)
        with mock.patch.dict(os.environ, {"ELEVENLABS_API_KEY": "synthetic-secret"}), \
                mock.patch.object(tts, "user_data_dir", return_value=Path(folder.name)):
            provider = tts.ElevenLabsProvider(capture)
            provider.synthesize("晚上好。", both)
            provider.synthesize("Good evening", both)
        self.assertIn("/text-to-speech/ChineseVoice01?", urls[0])
        self.assertIn("/text-to-speech/EnglishVoice01?", urls[1])

    def test_chinese_voice_setting_is_optional_and_checked(self):
        folder = Path(tempfile.mkdtemp())
        (folder / tts.SETTINGS_NAME).write_text(json.dumps({"voice_id": "EnglishVoice01"}), encoding="utf-8")
        self.assertEqual(tts.load_settings(folder).voice_id_zh, "")   # old settings still load
        tts.save_settings(folder, tts.TTSSettings(voice_id_zh="ChineseVoice01"))
        self.assertEqual(tts.load_settings(folder).voice_id_zh, "ChineseVoice01")
        with self.assertRaises(ValueError):
            tts.TTSSettings(voice_id_zh="bad id!").validate()

    def test_local_provider_uses_selected_speaker_without_cloud_fallback(self):
        settings = tts.TTSSettings(provider="qwen3-local", local_speaker="Serena", speed=1.1)
        with mock.patch.object(tts.ElevenLabsProvider, "synthesize") as cloud:
            with mock.patch.object(qwen3_local.client, "synthesize", return_value=b"RIFFxxxxWAVE") as local:
                self.assertEqual(tts.synthesize("你好", settings), b"RIFFxxxxWAVE")
                local.assert_called_once_with("你好", "Serena", 1.1)
            cloud.assert_not_called()

    def test_local_missing_runtime_reports_error_without_cloud_fallback(self):
        local = qwen3_local.LocalTTSClient()
        with mock.patch.object(qwen3_local, "PYTHON", Path("missing-python")):
            with mock.patch.object(tts.ElevenLabsProvider, "synthesize") as cloud:
                with self.assertRaisesRegex(tts.TTSError, 'runtime or model is missing'):
                    local.synthesize("你好", "Vivian", 1.0)
                cloud.assert_not_called()

    def test_cancel_keeps_the_local_worker_but_close_terminates_its_tree(self):
        local = qwen3_local.LocalTTSClient()
        process = mock.Mock(pid=12345)
        process.poll.return_value = None
        local.process = process
        local.active = "speech"
        with mock.patch.object(qwen3_local.subprocess, "run") as run, \
                mock.patch.object(qwen3_local.os, "name", "nt"):
            local.cancel()
            run.assert_not_called()  # no model reload after stopping speech
            self.assertIs(local.process, process)
            local.close()
        self.assertEqual(run.call_args.args[0], ["taskkill", "/PID", "12345", "/T", "/F"])

    def test_cancelled_sentence_is_drained_not_played(self):
        def wav(tag):
            return b"RIFF" + b"\0\0\0\0" + b"WAVE" + tag.ljust(40, b".")
        first, second = wav(b"first"), wav(b"second")
        stream = (json.dumps({"ok": True, "index": 0, "size": len(first)}).encode() + b"\n" + first +
                  json.dumps({"ok": True, "index": 1, "size": len(second)}).encode() + b"\n" + second +
                  b'{"done": true}\n')
        local = qwen3_local.LocalTTSClient()
        process = mock.Mock(pid=1, stdout=io.BytesIO(stream), stdin=io.BytesIO())
        process.poll.return_value = None
        local.process = process
        chunks = local.synthesize_chunks(["一", "二"], "Vivian", 1.0)
        self.assertEqual(next(chunks), first)
        local.cancel()
        self.assertEqual(list(chunks), [])  # second sentence read but dropped
        self.assertEqual(process.stdout.read(), b"")  # protocol stays in sync
        self.assertIs(local.process, process)

    def test_first_clause_is_spoken_on_its_own(self):
        self.assertEqual(tts.split_first(["好的，今天东京晴天，最高气温二十二度。", "记得带水。"]),
                         ["好的，今天东京晴天，", "最高气温二十二度。", "记得带水。"])
        self.assertEqual(tts.split_first(["短句。"]), ["短句。"])
        stream = tts.SpeechStream()
        self.assertEqual(stream.feed("今天东京晴天，最高"), ["今天东京晴天，"])
        self.assertEqual(stream.feed("气温二十二度，适合"), [])  # later clauses wait for sentence ends
        self.assertEqual(stream.feed("散步。"), ["最高气温二十二度，适合散步。"])

    def test_old_settings_without_model_load_with_the_fast_default(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / tts.SETTINGS_NAME).write_text(json.dumps(
                {"provider": "qwen3-local", "auto_read": True, "voice_id": "hpp4J3VqNfWAUOO0d1Us",
                 "local_speaker": "Serena", "volume": 0.8, "speed": 1.0}), encoding="utf-8")
            self.assertEqual(tts.load_settings(folder).eleven_model, "eleven_flash_v2_5")
        with self.assertRaises(ValueError):
            tts.TTSSettings(eleven_model="eleven_v9").validate()

    def test_local_speaker_must_be_known(self):
        with self.assertRaisesRegex(ValueError, 'local Qwen3 voice'):
            tts.TTSSettings(local_speaker="unknown").validate()

    def test_player_accepts_local_wav(self):
        player = MP3Player()
        player.mixer = mock.Mock()
        player.play(b"RIFFxxxxWAVEaudio", 0.4)
        self.assertEqual(player.mixer.music.load.call_args.args[1], "wav")
        player.mixer.music.set_volume.assert_called_once_with(0.4)

    def test_player_queues_sentence_audio_in_order_and_stop_clears_it(self):
        player = MP3Player()
        player.mixer = mock.Mock()
        player.mixer.music.get_busy.side_effect = [False, True, False, True]
        first = b"RIFFxxxxWAVEfirst"
        second = b"RIFFxxxxWAVEsecond"
        player.enqueue(first, 0.3)
        player.enqueue(second, 0.3)
        self.assertEqual(player.mixer.music.load.call_count, 1)
        player.poll()
        loaded = [call.args[0].getvalue() for call in player.mixer.music.load.call_args_list]
        self.assertEqual(loaded, [first, second])
        player.enqueue(first, 0.3)
        player.stop()
        self.assertFalse(player.pending)

    def test_old_audio_event_cannot_start_after_stop(self):
        window = MYNWindow.__new__(MYNWindow)
        window._usage_config = lambda: self.config
        window.events = queue.Queue()
        window.tts_request_id = 4
        window.tts_player = mock.Mock()
        window.tts_settings = tts.TTSSettings()
        window.root = mock.Mock()
        window.events.put(("tts_audio", 3, b"old"))
        window.process_events()
        window.tts_player.play.assert_not_called()
        window.events.put(("tts_audio", 4, b"new"))
        window.process_events()
        window.tts_player.play.assert_called_once_with(b"new", 0.8)
        window.stop_speech()
        self.assertEqual(window.tts_request_id, 5)
        window.tts_player.stop.assert_called_once()

    def test_stale_local_sentence_cannot_enter_player_queue(self):
        window = MYNWindow.__new__(MYNWindow)
        window._usage_config = lambda: self.config
        window.events = queue.Queue()
        window.tts_request_id = 9
        window.tts_player = mock.Mock()
        window.tts_settings = tts.TTSSettings(provider="qwen3-local")
        window.root = mock.Mock()
        window.events.put(("tts_chunk", 8, b"old"))
        window.events.put(("tts_chunk", 9, b"new"))
        window.process_events()
        window.tts_player.enqueue.assert_called_once_with(b"new", 0.8)

    def _greeting_window(self, **settings):
        window = MYNWindow.__new__(MYNWindow)
        window._usage_config = lambda: self.config
        window.events = queue.Queue()
        window.tts_request_id = 0
        window.tts_player = mock.Mock()
        window.tts_player.active.return_value = False
        window.tts_settings = tts.TTSSettings(**settings)
        window.root = mock.Mock()
        window.session = object()
        window.busy = False
        window.web = None
        window.greeting = "Good evening"
        window.greeted = False
        window._greet_waits = 0
        window.web_intro = False
        window.web_active = False
        window.recent_speech = ()
        window.speech_queue = None
        window.reply_silenced = False
        window._messages = lambda: []
        window.append = mock.Mock()
        return window

    def test_opening_greeting_is_spoken_once_in_english(self):
        window = self._greeting_window()
        with mock.patch.object(tts.ElevenLabsProvider, "stream", side_effect=lambda *a, **k: iter([b"ID3greeting"])) as synthesize, \
                mock.patch.object(tts, "cancel_local"):
            window._greet_aloud()
            event = window.events.get(timeout=5)
            self.assertEqual(event, ("tts_chunk", window.tts_request_id, b"ID3greeting"))
            self.assertEqual(synthesize.call_args.args[0], "Good evening")
            window._greet_aloud()  # only once
            self.assertEqual(synthesize.call_count, 1)
        window.events.put(event)
        window.process_events()
        window.tts_player.enqueue.assert_called_once_with(b"ID3greeting", 0.8)
        # A speech-service failure for the greeting is not shown as an error.
        window.events.put(("tts_error", window.quiet_tts_request, "未配置 ElevenLabs API Key"))
        window.process_events()
        window.append.assert_not_called()

    def test_opening_greeting_waits_for_gpu_intro_and_respects_auto_read(self):
        window = self._greeting_window()
        window.web, window.web_active, window.web_intro = mock.Mock(), True, True
        with mock.patch.object(tts.ElevenLabsProvider, "stream", side_effect=lambda *a, **k: iter([b"ID3greeting"])) as synthesize, \
                mock.patch.object(tts, "cancel_local"):
            window._greet_aloud()
            window.root.after.assert_called_once()  # tried again later, not spoken under the intro
            self.assertFalse(window.greeted)
            window.web_intro = False
            window._greet_aloud()
            window.events.get(timeout=5)
            self.assertEqual(synthesize.call_args.args[0], "Good evening")
        silent = self._greeting_window(auto_read=False)
        with mock.patch.object(tts.ElevenLabsProvider, "stream") as synthesize, mock.patch.object(tts, "cancel_local"):
            silent._greet_aloud()
            synthesize.assert_not_called()
        self.assertTrue(silent.events.empty())


class PlayerDucking(unittest.TestCase):
    def test_duck_makes_it_quiet_at_once_and_stop_resets(self):
        from tts_player import MP3Player
        player = MP3Player()
        volumes = []
        music = mock.Mock()
        music.set_volume.side_effect = volumes.append
        player.mixer = mock.Mock(music=music)
        player.set_volume(0.8)
        player.set_duck(0.15)
        self.assertAlmostEqual(volumes[-1], 0.12)
        player._play_now(b"ID3next", 0.8)  # the next sentence stays quiet too
        self.assertAlmostEqual(volumes[-1], 0.12)
        player.set_duck(1.0)
        self.assertAlmostEqual(volumes[-1], 0.8)
        player.set_duck(0.15)
        player.stop()
        self.assertEqual(player.duck, 1.0)

    def test_pause_stops_at_once_and_resume_goes_on(self):
        from tts_player import MP3Player
        player = MP3Player()
        music = mock.Mock()
        music.get_busy.return_value = False  # pygame reports "not busy" while paused
        player.mixer = mock.Mock(music=music)
        player.pending.append((b"ID3next", 0.8))
        player.pause()
        music.pause.assert_called_once()
        self.assertTrue(player.active())  # still MYN's turn: it is waiting
        player.poll()
        music.load.assert_not_called()  # the next sentence does not start while paused
        player.resume()
        music.unpause.assert_called_once()
        player.poll()
        music.load.assert_called_once()
        player.pause()
        player.stop()
        self.assertFalse(player.paused)


if __name__ == "__main__":
    unittest.main()
