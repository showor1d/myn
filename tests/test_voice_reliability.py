"""Speech reception and proposal replies. All audio/model inputs are synthetic."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src/core")]
import chat
import claude_client
import evolution
import interaction
import local_actions
import stt
import voice_diagnostics
from gui_voice import VoiceMixin
from test_voice_wake_ack import FakeWindow

try:
    import numpy as np
except ImportError:
    np = None


class Languages(unittest.TestCase):
    def test_uncertain_short_reply_follows_recent_language_but_can_switch(self):
        state = stt.LanguageContinuity()
        self.assertEqual(state.choose(dict(zh=10, en=8, ja=5), 3, 1000), "zh")
        self.assertEqual(state.choose(dict(zh=9, en=8, ja=10), .8, 1010), "zh")
        self.assertEqual(state.choose(dict(zh=5, en=10, ja=4), .8, 1020), "en")
        self.assertEqual(state.choose(dict(zh=8, en=9, ja=10), 3, 1030), "ja")
        self.assertEqual(state.choose(dict(zh=10, en=9, ja=8), .8, 1200), "zh")

    def test_attention_does_not_start_microphone(self):
        client = stt.LocalSTTClient()
        with mock.patch.object(client, "_request") as request:
            client.attention()
        request.assert_not_called()


class Diagnostics(unittest.TestCase):
    def test_diagnostic_log_contains_metadata_only_and_rotates(self):
        with tempfile.TemporaryDirectory() as folder:
            voice_diagnostics.record(folder, "recognized", chars=3, language="zh",
                                     text="秘密内容", audio="private", error="private", transcribe_s=.5)
            path = Path(folder) / "voice-diagnostics.jsonl"
            row = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(set(row), {"at", "stage", "chars", "language", "transcribe_s"})
            path.write_text((json.dumps(row) + "\n") * 5000, encoding="utf-8")
            voice_diagnostics.record(folder, "discard", reason="echo")
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 401)
            self.assertNotIn("秘密", path.read_text(encoding="utf-8"))


class Queue(unittest.TestCase):
    def window(self):
        window = FakeWindow(busy=True)
        window.session = SimpleNamespace(_optional_key=lambda: "synthetic")
        window.root = SimpleNamespace(after=lambda *args: None)
        return window

    def test_three_busy_replies_keep_order_and_origin(self):
        window = self.window()
        window._ask_after_reply("第一句", heard=True)
        window._ask_after_reply("第二句", heard=False)
        window._ask_after_reply("第三句", heard=True)
        self.assertEqual(window.queued_question, "第一句")
        window.busy = False
        for _ in range(3):
            window._drain_voice()
        self.assertEqual(window.addressed, [("第一句", True), ("第二句", False), ("第三句", True)])
        self.assertIsNone(window.queued_question)
        window._ask_after_reply("待处理")
        window.queued_question = None
        window._drain_voice()
        self.assertEqual(len(window.addressed), 3)

    def test_pending_review_survives_reading_delay_but_is_still_judged(self):
        window = self.window()
        window.busy = False
        window.session.pending_review = lambda: {"id": "proposal", "title": "方案"}
        judged = []
        window._judge_heard = judged.append
        window.hear("不要了")
        self.assertEqual(judged, ["不要了"])
        self.assertEqual(window.addressed, [])
        # Echo rejection still happens before pending-review routing.
        window.last_speaking = time.time()
        window.recent_speech = [(time.time(), "这个方案你想要采纳吗")]
        window.hear("这个方案你想要采纳吗")
        self.assertEqual(judged, ["不要了"])

    def test_judges_are_serial_and_preserve_next_sentence(self):
        import queue
        window = self.window()
        window.events = queue.Queue()
        window.session._key = lambda: "synthetic"
        window.session.db_path = Path("unused-test-only.db")
        jobs = []
        class Thread:
            def __init__(self, target, daemon):
                jobs.append(target)
            def start(self):
                pass
        with mock.patch("gui_voice.threading.Thread", Thread):
            window._judge_heard("先不要了")
            window._judge_heard("另一个再给我看看")
            self.assertEqual(len(jobs), 1)
            with mock.patch("gui_voice.conversation_mode.judge", return_value=(True, False, False)):
                jobs[0]()
            event = window.events.get_nowait()
            self.assertEqual(event[1], "先不要了")
            # Model judgment has finished; next job starts from the next UI tick.
            window.session.db_path = None
            window._judged(event[1], event[2])
            self.assertEqual(window.addressed, [("先不要了", True)])
            self.assertEqual(window._judge_pending, ("另一个再给我看看",))
            window._start_voice_judge()
            self.assertEqual(len(jobs), 2)

    def test_turning_listening_off_invalidates_older_judgments(self):
        window = self.window()
        window._stt_job = mock.Mock()
        window._judge_pending = ("下一句",)
        window._stop_listening()
        window.always_on_active = True  # even if listening has since resumed
        window._judged("旧的话", (True, False, False), generation=0)
        self.assertEqual(window.addressed, [])
        self.assertEqual(window._judge_pending, ())


class Review(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="myn-voice-review-")
        self.addCleanup(temp.cleanup)
        self.config = Path(temp.name)
        self.session = chat.ChatSession(self.config / "jarvis.db")
        point, _ = evolution.add(self.config, "缩短回复", request="说话太啰嗦")
        self.point = evolution.update(self.config, point["id"], status="proposed",
                                      plan={"summary": "简短回复", "plan": "精简开场白"})
        self.session.remember_review(point["id"])
        self.context = self.session.pending_review()

    def choice(self, text="不要了", **kw):
        return dict(dict(action="reject", id=self.point["id"], certainty="clear",
                         scope="current", evidence=text), **kw)

    def test_only_exact_current_proposal_can_be_rejected(self):
        for change in (dict(id="other"), dict(scope="keep"), dict(certainty="uncertain"),
                       dict(evidence="unspoken"), dict(action="adopt"), dict(action="keep")):
            with self.subTest(change=change):
                self.assertIsNone(evolution.reject_review(self.config, self.context,
                                                          self.choice(**change), "不要了"))
        changed = evolution.reject_review(self.config, self.context, self.choice(), "不要了")
        self.assertEqual(changed["status"], "rejected")
        self.assertIsNone(self.session.pending_review())

    def test_changed_and_expired_reviews_cannot_be_rejected(self):
        evolution.update(self.config, self.point["id"], plan={"summary": "换了方案"})
        self.assertIsNone(self.session.pending_review())
        self.assertIsNone(evolution.reject_review(self.config, self.context, self.choice(), "不要了"))
        self.session.remember_review(self.point["id"])
        self.session._review["at"] -= 601
        self.assertIsNone(self.session.pending_review())

    def test_show_tool_returns_proposal_without_adopting(self):
        context = SimpleNamespace(config_dir=self.config)
        raw, error = local_actions._evolution_points({"action": "show", "id": self.point["id"]}, context)
        self.assertFalse(error)
        result = json.loads(raw)
        self.assertIn("精简开场白", result["review_text"])
        self.assertEqual(result["evolution_point"]["id"], self.point["id"])
        self.assertEqual(evolution.get(self.config, self.point["id"])["status"], "proposed")

    def test_semantic_rejection_saves_then_confirms_without_second_reply_call(self):
        evolution.update(self.config, self.point["id"], title="夜间不打扰")
        self.session.remember_review(self.point["id"])
        local_actions.save_settings(self.config, local_actions.ActionSettings(self_fix=True))
        self.session.interaction_notify = lambda: None
        phrase = "这个就算了吧"
        decision = dict(mode="idle", scope="keep", certainty="clear", evidence=phrase,
                        reason="回答刚展示的方案", evolution=self.choice(phrase))
        with mock.patch.object(self.session, "_key", return_value="synthetic"), \
                mock.patch.object(claude_client, "send", return_value=json.dumps(decision)) as classify, \
                mock.patch.object(claude_client, "coordinated_reply") as reply:
            result = self.session.ask(phrase, heard=True)
        reply.assert_not_called()
        self.assertEqual(classify.call_count, 1)
        self.assertIn("夜间不打扰", result["answer"])
        self.assertFalse(result.get("silent"))
        self.assertIn('declined', result["answer"])
        self.assertEqual(evolution.get(self.config, self.point["id"])["status"], "rejected")
        self.assertIsNone(self.session.pending_review())

    def test_clear_topic_switch_forgets_review_without_rejecting(self):
        local_actions.save_settings(self.config, local_actions.ActionSettings(self_fix=True))
        self.session.interaction_notify = lambda: None
        phrase = "聊聊晚饭吃什么"
        decision = dict(mode="idle", scope="current", certainty="clear", evidence=phrase,
                        reason="已经换了话题", evolution=self.choice(phrase, action="leave"))
        with mock.patch.object(self.session, "_key", return_value="synthetic"), \
                mock.patch.object(claude_client, "send", return_value=json.dumps(decision)), \
                mock.patch.object(claude_client, "coordinated_reply", return_value="想吃什么？"):
            self.session.ask(phrase)
        self.assertIsNone(self.session.pending_review())
        self.assertEqual(evolution.get(self.config, self.point["id"])["status"], "proposed")


@unittest.skipIf(np is None, "numpy is in the speech worker's Python environment")
class Reception(unittest.TestCase):
    def run_audio(self, voiced_blocks, rms, attention=False):
        import stt_worker
        events = []
        now = [1000.]
        listener = stt_worker.Listener(object(), np, lambda *_: "不要了", events.append,
                                      threaded=False, clock=lambda: now[0], levels=True)
        if attention:
            listener.attention_until = 1100.
        samples = int(listener.rate * listener.BLOCK_S)
        def feed(level, count):
            block = (np.sin(np.arange(samples) / 3.) * level * np.sqrt(2)).astype(np.float32)
            for _ in range(count):
                now[0] += listener.BLOCK_S
                listener.feed(block, now[0])
        feed(.001, 70)
        feed(rms, voiced_blocks)
        feed(.001, 40)
        return events

    def test_quiet_and_short_speech_reaches_transcriber(self):
        for blocks, rms, attention in ((10, .007, False), (5, .0045, True)):
            events = self.run_audio(blocks, rms, attention)
            heard = [e for e in events if e["event"] == "heard"]
            self.assertEqual(len(heard), 1, (blocks, rms, attention))
            self.assertEqual(heard[0]["text"], "不要了")
            self.assertIn("transcribe_s", heard[0])
            self.assertTrue(any(e.get("stage") == "recognizing" for e in events))

    def test_taps_room_noise_and_inactive_too_short_are_not_transcribed(self):
        for blocks, rms, attention in ((1, .2, True), (500, .001, True), (5, .2, False)):
            events = self.run_audio(blocks, rms, attention)
            self.assertFalse(any(e["event"] == "heard" for e in events))


if __name__ == "__main__":
    unittest.main()
