"""Latency contracts with fake API/audio: fewer requests, early buffers, preserved guards."""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src/core")]
import chat
import claude_client
import interaction
import silence
import tts
import usage_stats
from tts_player import MP3Player


class CombinedScene(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="myn-voice-latency-")
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name)
        self.session = chat.ChatSession(self.config / "jarvis.db")
        self.session.interaction_notify = lambda: None
        self.key = mock.patch.object(self.session, "_key", return_value="synthetic")
        self.key.start()
        self.addCleanup(self.key.stop)
        self.phrase = "接着给我讲吧"
        self.decision = dict(mode="study", scope="current", certainty="clear",
                             evidence=self.phrase, reason="继续当前学习",
                             addressing=dict(to_myn=True, ends=False, elsewhere=False))

    def inspect(self):
        return self.session.inspect_voice(self.phrase, [("myn", "继续讲吗？")], [])

    def test_one_classification_and_no_mutation_until_saved(self):
        ready = []
        self.session.voice_ready_notify = lambda: ready.append("ready")
        before = interaction.load(self.config)
        with mock.patch.object(claude_client, "send", return_value=json.dumps(self.decision)) as send, \
                mock.patch.object(claude_client, "coordinated_reply", return_value="我们接着看下一步。"):
            verdict, prepared = self.inspect()
            self.assertEqual(verdict, (True, False, False))
            self.assertEqual(interaction.load(self.config), before)
            result = self.session.ask(self.phrase, heard=True, voice_preflight=prepared)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[0], claude_client.main_model(self.config))
        self.assertEqual(ready, ["ready"])
        self.assertEqual(interaction.load(self.config)["mode"], "study")
        self.assertIn("下一步", result["answer"])

    def test_expiry_reader_changes_and_new_input_reclassify(self):
        for stale in ("expired", "reader", "new_input"):
            with self.subTest(stale=stale), \
                    mock.patch.object(claude_client, "send", return_value=json.dumps(self.decision)) as send, \
                    mock.patch.object(claude_client, "coordinated_reply", return_value="下一步"):
                self.session.reader_context_provider = None
                _, prepared = self.inspect()
                if stale == "expired":
                    prepared["at"] -= 31
                elif stale == "reader":
                    self.session.reader_context_provider = lambda: {"open": True, "key": "new", "revision": 3}
                else:
                    interaction.observe(self.config, "另外一句")
                self.session.ask(self.phrase, heard=True, voice_preflight=prepared)
                self.assertEqual(send.call_count, 2)

    def test_stale_unrelated_speech_never_executes_or_changes_scene(self):
        with mock.patch.object(claude_client, "send", return_value=json.dumps(self.decision)):
            _, prepared = self.inspect()
        prepared["at"] -= 31
        decision = dict(self.decision, addressing=dict(to_myn=False, ends=False, elsewhere=True))
        ready = mock.Mock()
        self.session.voice_ready_notify = ready
        with mock.patch.object(claude_client, "send", return_value=json.dumps(decision)), \
                mock.patch.object(self.session, "_dispatch") as dispatch:
            result = self.session.ask(self.phrase, heard=True, voice_preflight=prepared)
        self.assertTrue(result["silent"])
        dispatch.assert_not_called()
        ready.assert_not_called()
        self.assertNotEqual(interaction.load(self.config)["mode"], "study")

    def test_uncertain_scene_never_releases_early_speech(self):
        self.decision["certainty"] = "uncertain"
        self.decision["scope"] = "keep"
        ready = mock.Mock()
        self.session.voice_ready_notify = ready
        with mock.patch.object(claude_client, "send", return_value=json.dumps(self.decision)), \
                mock.patch.object(claude_client, "coordinated_reply", return_value="想继续哪一步？"):
            _, prepared = self.inspect()
            self.session.ask(self.phrase, heard=True, voice_preflight=prepared)
        ready.assert_not_called()

    def test_confirmed_addressing_releases_clause_not_silence_marker(self):
        gate = silence.Gate(voice=True)
        self.assertEqual(gate.feed("先看这里，"), "")
        gate.voice = False
        self.assertEqual(gate.feed("我们"), "先看这里，我们")
        stream = tts.SpeechStream()
        self.assertEqual(stream.feed("我们先看这一步，然后"), ["我们先看这一步，"])
        gate = silence.Gate(voice=False)
        self.assertEqual(gate.feed(silence.MARK), "")
        self.assertTrue(gate.silent)


class StreamSpeech(unittest.TestCase):
    def setUp(self):
        self.key = mock.patch.object(tts.credentials, "get_elevenlabs_key", return_value="synthetic")
        self.key.start()
        self.addCleanup(self.key.stop)
        self.usage = mock.patch.object(usage_stats, "record_voice")
        self.record = self.usage.start()
        self.addCleanup(self.usage.stop)

    class Response(io.BytesIO):
        headers = {"history-item-id": "synthetic-id"}

    def test_first_chunk_arrives_before_whole_response_and_billing_once(self):
        response = self.Response(b"\x01\x00" * 30000)
        opener = mock.Mock(return_value=response)
        settings = tts.TTSSettings(voice_id_zh="abcdabcd1234")
        stream = tts.ElevenLabsProvider(opener).stream("你好，这是测试。", settings)
        first = next(stream)
        self.assertIsInstance(first, tts.PCMChunk)
        self.assertEqual(len(first.samples), 12000)
        self.assertLess(response.tell(), 60000)
        self.assertTrue(opener.call_args.args[0].full_url.endswith("abcdabcd1234/stream?output_format=pcm_24000"))
        body = json.loads(opener.call_args.args[0].data)
        self.assertEqual(body["model_id"], settings.eleven_model)
        self.assertEqual(first.samples + b"".join(c.samples for c in stream), b"\x01\x00" * 30000)
        self.assertTrue(response.closed)
        self.record.assert_called_once()

    def test_cancel_closes_response_without_queued_tail_or_new_request(self):
        response = self.Response(b"\x02\x00" * 30000)
        cancelled = [False]
        stream = tts.ElevenLabsProvider(lambda *a, **k: response).stream(
            "测试", tts.TTSSettings(), cancelled=lambda: cancelled[0])
        next(stream)
        cancelled[0] = True
        self.assertEqual(list(stream), [])
        self.assertTrue(response.closed)

    def test_empty_odd_and_failed_streams_do_not_retry_or_switch_provider(self):
        for data in (b"", b"\x01"):
            with self.subTest(data=data), self.assertRaises(tts.TTSError):
                list(tts.ElevenLabsProvider(lambda *a, **k: self.Response(data)).stream("测试", tts.TTSSettings()))
        opener = mock.Mock(side_effect=OSError("private network details"))
        with self.assertRaisesRegex(tts.TTSError, 'network request failed'):
            list(tts.ElevenLabsProvider(opener).stream("测试", tts.TTSSettings()))
        opener.assert_called_once()

    def test_player_prequeues_next_buffer_and_stop_discards_all(self):
        player = MP3Player()
        channel = SimpleNamespace(current=None, next=None, volume=0, paused=False)
        channel.get_busy = lambda: channel.current is not None
        channel.get_queue = lambda: channel.next
        channel.play = lambda sound: setattr(channel, "current", sound)
        channel.queue = lambda sound: setattr(channel, "next", sound)
        channel.set_volume = lambda v: setattr(channel, "volume", v)
        def stop():
            channel.current = channel.next = None
        channel.stop = stop
        channel.pause = lambda: setattr(channel, "paused", True)
        channel.unpause = lambda: setattr(channel, "paused", False)
        mixer = mock.Mock()
        mixer.get_init.return_value = (24000, -16, 1)
        mixer.find_channel.return_value = channel
        mixer.Sound.side_effect = lambda buffer: buffer
        player.mixer = mixer
        player.on_start = mock.Mock()
        for n in (1, 2, 3):
            player.enqueue(tts.PCMChunk(bytes([n, 0]) * 100), .8)
        self.assertEqual(channel.current, b"\x01\x00" * 100)
        self.assertEqual(channel.next, b"\x02\x00" * 100)
        self.assertEqual(len(player.pcm_pending), 1)
        player.set_duck(.25)
        self.assertAlmostEqual(channel.volume, .2)
        player.pause()
        self.assertTrue(channel.paused)
        player.resume()
        self.assertFalse(channel.paused)
        player.stop()
        self.assertIsNone(channel.current)
        self.assertFalse(player.pcm_pending)
        player.on_start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
