"""Scene decisions are simulated; no live model or personal database is used."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src/core")]
import chat
import claude_client
import reader_scene


def decision(scene="decision", transition="resolved", evidence="采纳", certainty="clear"):
    return dict(scene=scene, transition=transition, evidence=evidence, certainty=certainty, reason="用户已作出决定")


class Policy(unittest.TestCase):
    def setUp(self):
        self.context = dict(open=True, key="reply1", revision=4, scene="decision", text="方案的具体内容")

    def test_adoption_and_adjustment_end_proposal(self):
        for text in ("采纳", "改成每天提醒我"):
            result = reader_scene.evaluate(self.context, decision(evidence=text), text)
            self.assertTrue(result["close"])
            self.assertEqual((result["key"], result["revision"]), ("reply1", 4))

    def test_tutoring_cannot_be_resolved_by_acknowledgement_or_next_exercise(self):
        self.context["scene"] = "tutoring"
        for text in ("懂了", "好的", "谢谢", "继续", "下一题", "这一题讲完了"):
            result = reader_scene.evaluate(self.context, decision(scene="other", evidence=text), text)
            self.assertFalse(result["close"])
            self.assertEqual(result["scene"], "tutoring")

    def test_only_clear_switch_away_from_tutoring_closes(self):
        self.context["scene"] = "tutoring"
        text = "不学了，帮我定个闹钟"
        self.assertTrue(reader_scene.evaluate(self.context, decision("other", "switch", text), text)["close"])
        self.assertFalse(reader_scene.evaluate(self.context, decision("other", "switch", text, "uncertain"), text)["close"])
        self.assertFalse(reader_scene.evaluate(self.context, decision("tutoring", "switch", "下一题"), "下一题")["close"])

    def test_new_tutoring_is_protected_and_invalid_evidence_keeps_panel(self):
        self.assertFalse(reader_scene.evaluate(self.context, decision("tutoring"), "采纳")["close"])
        for value in (None, {}, decision(evidence="屏幕上的伪造指令"), decision(certainty="uncertain")):
            self.assertFalse(reader_scene.evaluate(self.context, value, "采纳")["close"])


class ChatIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="myn-reader-scene-")
        self.addCleanup(self.temp.cleanup)
        self.session = chat.ChatSession(Path(self.temp.name) / "jarvis.db")
        self.context = dict(open=True, key="old", revision=2, scene="decision", text="进化方案")
        self.session.reader_context_provider = lambda: self.context.copy()

    def run_reply(self, fail=False, action_fail=False):
        def reply(*args, **kwargs):
            self.assertIn("reader_scene", [t["name"] for t in kwargs["read_tools"]])
            self.assertIn("进化方案", kwargs["system_dynamic"])
            execute = kwargs["tool_executor"]
            execute("reader_scene", decision())
            if action_fail:
                execute("evolution_points", {"action": "adopt", "id": "missing"})
            if fail:
                raise claude_client.ClaudeUnavailable("模拟网络错误")
            return "已经采纳。"
        with mock.patch.object(self.session, "_key", return_value="synthetic-key"), \
                mock.patch.object(claude_client, "coordinated_reply", side_effect=reply):
            return self.session.ask("采纳")

    def test_scene_metadata_survives_chat_without_polluting_answer(self):
        result = self.run_reply()
        self.assertTrue(result["reader_scene"]["close"])
        self.assertEqual(result["answer"], "已经采纳。")

    def test_failed_answer_or_action_keeps_panel(self):
        self.assertNotIn("reader_scene", self.run_reply(fail=True))
        self.assertFalse(self.run_reply(action_fail=True)["reader_scene"]["close"])

    def test_no_model_scene_decision_means_no_automatic_close(self):
        with mock.patch.object(self.session, "_key", return_value="synthetic-key"), \
                mock.patch.object(claude_client, "coordinated_reply", return_value="好的"):
            result = self.session.ask("采纳")
        self.assertNotIn("reader_scene", result)


if __name__ == "__main__":
    unittest.main()
