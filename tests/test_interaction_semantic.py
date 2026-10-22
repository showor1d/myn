"""Contextual scene entry/exit contracts. Model decisions are simulated, not accuracy claims."""
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
import interaction
import evolution


def verdict(mode, text, scope="current", certainty="clear"):
    return dict(mode=mode, evidence=text, scope=scope, certainty=certainty,
                reason="结合前文与当前活动判断，不依赖固定口令")


class Semantic(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="myn-semantic-scene-")
        self.addCleanup(temp.cleanup)
        self.config = Path(temp.name)
        self.session = chat.ChatSession(self.config / "jarvis.db")
        self.notifications = []
        self.session.interaction_notify = lambda: self.notifications.append(interaction.load(self.config)["mode"])
        key = mock.patch.object(self.session, "_key", return_value="synthetic")
        key.start()
        self.addCleanup(key.stop)

    def ask(self, text, result, answer="好的。"):
        def reply(*args, **kwargs):
            self.assertNotIn("interaction_scene", [item["name"] for item in kwargs["read_tools"]])
            if kwargs.get("on_text"):
                kwargs["on_text"](answer)
            return answer
        with mock.patch.object(claude_client, "send", return_value=json.dumps(result, ensure_ascii=False)) as classify, \
                mock.patch.object(claude_client, "coordinated_reply", side_effect=reply):
            response = self.session.ask(text)
        self.assertEqual(classify.call_count, 1)
        self.assertEqual(classify.call_args.kwargs["purpose"], "interaction_scene")
        self.assertEqual(classify.call_args.kwargs["usage_path"], self.config / "claude-usage.jsonl")
        self.assertFalse(classify.call_args.kwargs["retry_truncated"])
        return response, json.loads(classify.call_args.args[2])

    def test_unlisted_bedtime_expression_mutes_before_answer(self):
        text = "眼皮都撑不开了，今天就陪我到这里吧"
        self.assertIsNone(interaction.explicit_mode(text))
        seen = []
        with mock.patch.object(claude_client, "send", return_value=json.dumps(verdict("sleep", text))), \
                mock.patch.object(claude_client, "coordinated_reply", side_effect=lambda *a, **k:
                                  seen.append((interaction.allowed(self.config, "proactive"),
                                               interaction.allowed(self.config, "reply"))) or "好好休息。"):
            result = self.session.ask(text)
        self.assertEqual(seen, [(False, True)])  # muted for background speech; the good night is spoken
        self.assertEqual(result["answer"], "好好休息。")
        self.assertEqual(self.notifications, ["sleep"])
        self.assertTrue(interaction.allowed(self.config, "alarm"))

    def test_natural_waking_does_not_require_im_awake(self):
        interaction.observe(self.config, "我要睡觉了")
        text = "门铃把我吵起来了，我正坐在床边，陪我聊会儿吧"
        self.ask(text, verdict("idle", text))
        self.assertEqual(interaction.load(self.config)["mode"], "idle")
        self.assertTrue(interaction.allowed(self.config))

    def test_natural_lesson_entry_and_exit_use_recent_conversation_and_reader(self):
        first = "这个导数我算了三遍都不对，帮我看看哪步错了"
        self.ask(first, verdict("study", first), answer="先看第二步的链式法则。")
        self.session.reader_context_provider = lambda: dict(
            open=True, key="lesson", revision=7, scene="tutoring", text="链式法则的步骤")
        last = "书都收进包里了，咱们聊点周末好玩的吧"
        result, payload = self.ask(last, verdict("idle", last))
        self.assertIn(first, json.dumps(payload["recent_conversation"], ensure_ascii=False))
        self.assertIn("链式法则", json.dumps(payload["reading_panel"], ensure_ascii=False))
        self.assertEqual(payload["previous_state"]["mode"], "study")
        self.assertEqual(interaction.load(self.config)["mode"], "idle")
        self.assertTrue(result["reader_scene"]["close"])
        self.assertEqual(result["reader_scene"]["key"], "lesson")

    def test_acknowledgement_in_ongoing_lesson_keeps_study(self):
        interaction.observe(self.config, "我要学习了")
        self.ask("这步终于懂了", verdict("study", "这步终于懂了", scope="keep"))
        self.assertEqual(interaction.load(self.config)["mode"], "study")
        self.assertFalse(interaction.allowed(self.config, "proactive"))
        self.assertTrue(interaction.allowed(self.config, "reply"))

    def test_quote_and_past_prefix_do_not_block_real_present_intention(self):
        interaction.observe(self.config, "我要学习了")
        text = "昨天你说“别着急”，今天这章终于做完并收起来了，咱俩闲聊一会吧"
        self.ask(text, verdict("idle", "今天这章终于做完并收起来了，咱俩闲聊一会吧"))
        self.assertEqual(interaction.load(self.config)["mode"], "idle")

    def test_past_or_quote_alone_never_changes_current_scene(self):
        interaction.observe(self.config, "我要学习了")
        for text in ("昨晚困得连眼睛都睁不开", "把“我想上床躺下了”翻译成英语"):
            self.ask(text, verdict("sleep", text, scope="keep"))
            self.assertEqual(interaction.load(self.config)["mode"], "study")

    def test_same_short_reply_can_be_interpreted_in_context_without_literal_veto(self):
        interaction.observe(self.config, "我要学习了")
        self.ask("好了", verdict("study", "好了", scope="keep"))
        self.assertEqual(interaction.load(self.config)["mode"], "study")
        # In a different context it answers MYN asking whether the user has
        # packed everything away and wants to switch to casual conversation.
        result = self.session.record_input("好了")
        context = interaction.load(self.config)
        self.assertTrue(interaction.apply_classification(
            self.config, context, verdict("idle", "好了"), "好了"))
        self.assertEqual(interaction.load(self.config)["mode"], "idle")

    def test_fixed_phrase_is_not_an_online_override(self):
        result, _ = self.ask("晚安", verdict("idle", "晚安", scope="keep"), answer="这个词可以这样翻译。")
        self.assertEqual(result["answer"], "这个词可以这样翻译。")
        self.assertEqual(interaction.load(self.config)["mode"], "idle")

    def test_invalid_or_failed_classifier_preserves_scene_and_quiet_alarm_exception(self):
        for value in ("not json", '{"mode":"idle"}', json.dumps(verdict("idle", "捏造的依据"))):
            with mock.patch.object(claude_client, "send", return_value=value), \
                    mock.patch.object(claude_client, "coordinated_reply", return_value="文字仍可回答"):
                self.session.ask("我在忙别的事情")
            self.assertFalse(interaction.allowed(self.config, "proactive"))
            self.assertFalse(interaction.idle(self.config, now=10 ** 12))
            self.assertTrue(interaction.allowed(self.config, "alarm"))
        with mock.patch.object(claude_client, "send", side_effect=claude_client.ClaudeUnavailable("offline")), \
                mock.patch.object(claude_client, "coordinated_reply", return_value="文字仍可回答"):
            self.session.ask("先陪我到这里")
        self.assertTrue(interaction.load(self.config)["pending"])
        with mock.patch.object(self.session, "_key", return_value=None):
            self.session.ask("随口说一句不明确的话")
        self.assertTrue(interaction.load(self.config)["pending"])
        self.ask("事情忙完啦，闲聊一会", verdict("idle", "事情忙完啦，闲聊一会"))
        self.assertFalse(interaction.load(self.config)["pending"])

    def test_uncertainty_and_stale_results_cannot_wake_user(self):
        interaction.observe(self.config, "我要睡觉了")
        self.ask("嗯，好像是", verdict("idle", "嗯，好像是", certainty="uncertain"))
        self.assertEqual(interaction.load(self.config)["mode"], "sleep")
        context = interaction.observe(self.config, "旧消息", semantic=True)
        interaction.observe(self.config, "新消息", semantic=True)
        self.assertFalse(interaction.apply_classification(
            self.config, context, verdict("idle", "旧消息"), "旧消息"))
        self.assertEqual(interaction.load(self.config)["mode"], "sleep")

    def test_staying_idle_does_not_erase_a_deferred_invitation(self):
        point, _ = evolution.add(self.config, "改进字幕")
        point = evolution.update(self.config, point["id"], status="proposed", plan={"summary": "方案"})
        interaction.offer(self.config, point)
        interaction.response(self.config, "等会再给我看")
        self.ask("天气不错呀", verdict("idle", "天气不错呀", scope="keep"))
        self.assertTrue(interaction.load(self.config)["deferred"])
        self.assertIsNotNone(interaction.current_invitation(self.config))
        self.ask("手边的事情都忙完了，刚才留着的东西现在可以聊啦",
                 verdict("idle", "手边的事情都忙完了，刚才留着的东西现在可以聊啦"))
        self.assertFalse(interaction.load(self.config)["deferred"])


if __name__ == "__main__":
    unittest.main()
