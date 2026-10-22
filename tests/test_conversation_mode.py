"""Judging whether a sentence continues the spoken conversation with MYN."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "core")]

import claude_client  # noqa: E402
import conversation_mode  # noqa: E402


class ConversationModeChecks(unittest.TestCase):
    def test_prompt_lists_recent_turns_and_the_new_sentence(self):
        recent = [("you", "早"), ("myn", "早上好"), ("you", "明天提醒我开会"), ("myn", "好的，几点？")]
        prompt = conversation_mode.judge_prompt(recent, "下午 三点")
        self.assertIn('User：明天提醒我开会\nMYN：好的，几点？', prompt)
        self.assertTrue(prompt.endswith('New sentence: 下午 三点'))
        self.assertIn('(Just started)', conversation_mode.judge_prompt([], "在吗"))

    def test_long_turns_are_shortened(self):
        prompt = conversation_mode.judge_prompt([("myn", "很" * 500)], "嗯")
        self.assertIn("很" * 180 + "…" + "很" * 120, prompt)
        self.assertNotIn("很" * 301, prompt)
        prompt = conversation_mode.judge_prompt([("myn", "方案说明" * 200 + "你要采纳吗？")],
                                                "不要了", pending={"id": "one", "title": "短回复"})
        self.assertIn("你要采纳吗？", prompt)
        self.assertIn("短回复", prompt)

    def test_verdicts_are_parsed_strictly(self):
        self.assertEqual(conversation_mode.parse_verdict('{"to_myn": true, "ends": false}'), (True, False, False))
        self.assertEqual(conversation_mode.parse_verdict('好的 {"to_myn": false}'), (False, False, False))
        self.assertEqual(conversation_mode.parse_verdict('{"to_myn": false, "elsewhere": true, "ends": false}'),
                         (False, False, True))
        # "elsewhere" only means something when the sentence was not for MYN
        self.assertEqual(conversation_mode.parse_verdict('{"to_myn": true, "elsewhere": true}'), (True, False, False))
        for bad in ("", "是的", '{"to_myn": "yes"}', '{"to_myn": true, "ends": 1}', '{"to_myn": false, "elsewhere": "no"}'):
            with self.assertRaises(ValueError):
                conversation_mode.parse_verdict(bad)

    def test_judge_asks_claude_and_stays_quiet_on_failure(self):
        calls = []

        def sender(model, system, message, key, **options):
            calls.append((model, message, key, options))
            return '{"to_myn": true, "elsewhere": false, "ends": true}'
        self.assertEqual(conversation_mode.judge("k", [("myn", "还有吗？")], "没有了谢谢",
                                                 sender=sender), (True, True, False))
        self.assertEqual(calls[0][2], "k")
        self.assertEqual(calls[0][3]["purpose"], "conversation_judge")
        self.assertIn("没有了谢谢", calls[0][1])

        def offline(*_args, **_options):
            raise claude_client.ClaudeUnavailable("offline")
        self.assertEqual(conversation_mode.judge("k", [], "在吗", sender=offline), (False, False, False))
        self.assertEqual(conversation_mode.judge("k", [], "在吗", sender=lambda *a, **o: "嗯"),
                         (False, False, False))
        # quick: Haiku without thinking at low effort
        self.assertEqual(calls[0][0], claude_client.OPUS)
        self.assertEqual((calls[0][3]["effort"], calls[0][3]["thinking"]), ("low", {"type": "disabled"}))
        # Haiku not offered to this account: Sonnet without up-front thinking
        tried = []

        def no_haiku(model, *args, **options):
            tried.append((model, options["thinking"]))
            if model == claude_client.HAIKU:
                raise claude_client.ClaudeUnavailable("HTTP 404", fallback_allowed=True)
            return '{"to_myn": true}'
        self.assertEqual(conversation_mode.judge("k", [], "在吗", sender=no_haiku), (True, False, False))
        self.assertEqual(tried, [(claude_client.OPUS, {"type": "disabled"})])
        self.assertIn("elsewhere", conversation_mode.JUDGE_SYSTEM)
        self.assertEqual(conversation_mode.IDLE_SECONDS, 30)


if __name__ == "__main__":
    unittest.main()
