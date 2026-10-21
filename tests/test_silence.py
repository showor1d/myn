'Suppress replies to nearby conversations so acknowledgements cannot create a self-sustaining reply loop.'

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src" / "core"), str(BASE / "tests")]

import chat
import claude_client
import conversation_mode
import silence
from test_web_tools import reply

REAL = ["我不打扰。", "（在聊显示器设置，我不打扰。）", "（在跟朋友凑人呢，我不打扰。）", "我先不出声，有事叫我。",
        "听着像在跟朋友聊画面设置，我先不出声。", "（没在跟我说话，我不出声。）",
        "好像你在和别人聊天，我先不打扰。", "这句好像不是对我说的？如果是问我，能再说具体点吗？",
        "听着像在跟朋友聊天，我先不插话，需要我的时候叫我就行。"]


def streamed(text, voice, step=2):
    gate = silence.Gate(voice)
    out = "".join(gate.feed(text[i:i + step]) for i in range(0, len(text), step))
    return out + gate.flush(), gate.silent


class Recognise(unittest.TestCase):
    def test_the_real_replies(self):
        for text in REAL:
            self.assertTrue(silence.is_silent(text, voice=True), text)
            self.assertEqual(streamed(text, True), ("", True), text)

    def test_the_mark(self):
        for voice in (False, True):
            self.assertTrue(silence.is_silent(silence.MARK, voice))
            self.assertTrue(silence.is_silent(" %s\n" % silence.MARK, voice))
            self.assertEqual(streamed(silence.MARK, voice), ("", True))

    def test_real_answers_pass(self):
        for text, voice in (("好的，今天东京是晴天。", True), ("好", True), ("〔注〕这个词有两个意思。", False),
                            ("我不打扰。", False),  # A typed request to stop interrupting may receive a real answer.
                            ("你在跟同学说的那个显示器，DyAc 开超高确实更清楚，" + "亮度可以再调低一点。" * 5, True)):
            self.assertFalse(silence.is_silent(text, voice), text)
            self.assertEqual(streamed(text, voice), (text, False), text)

    def test_typed_reply_starts_at_once(self):
        gate = silence.Gate(voice=False)
        self.assertEqual(gate.feed("好的"), '好的')
        gate = silence.Gate(voice=True)  # a heard one waits for its first sentence
        self.assertEqual(gate.feed("好的"), "")
        self.assertEqual(gate.feed("，明天见。"), "好的，明天见。")
        self.assertEqual(gate.feed("还有"), "还有")
        gate.reset()  # a new model turn after a tool
        self.assertEqual(gate.feed(silence.MARK), "")
        self.assertTrue(gate.silent)


class Singing(unittest.TestCase):
    'Singing should not trigger an acknowledgement after every line.'

    def test_listening_noises_on_heard_turns(self):
        for text in ("嗯。", "嗯，你继续唱。", "嗯，你接着唱。", "嗯，你继续唱，我听着。", "我听着呢"):
            self.assertTrue(silence.is_silent(text, voice=True), text)
            self.assertFalse(silence.is_silent(text, voice=False), text)  # typed: an answer
        for text in ("好的。", "好。", "嗯，明天八点。", "嗯，对，是这样。"):
            self.assertFalse(silence.is_silent(text, voice=True), text)

    def test_loose_wakes_are_not_certain(self):
        import stt
        words = "贾维斯, Jarvis, 麦恩"  # legacy parsing helper; active settings now fix Hey Mine
        for text in ("贾维斯，在吗", "麦恩 你好", "嘿 Jarvis 你好"):
            self.assertTrue(stt.exact_wake(text, words), text)
        for text in ("Maia, what's the weather", "马亚你好", "我昨天看了贾维斯", ""):
            self.assertFalse(stt.exact_wake(text, words), text)
        self.assertEqual(stt.strip_wake("马亚你好", words), "你好")  # still wakes MYN


class InChat(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="myn-silence-")
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name) / "data" / "jarvis.db"
        self.db.parent.mkdir()
        self.session = chat.ChatSession(self.db)

    def ask(self, text, answer, heard):
        payloads = []

        def opener(request, timeout):
            payloads.append(json.loads(request.data.decode("utf-8")))
            return reply([{"type": "text", "text": answer}])

        with mock.patch.object(self.session, "_key", return_value="k"), \
                mock.patch.object(claude_client.urllib.request, "urlopen", side_effect=opener):
            result = self.session.ask(text, heard=heard)
        return result, json.dumps(payloads[0]["system"], ensure_ascii=False)

    def answers(self):
        with closing(sqlite3.connect(str(self.db))) as db:
            return [row[0] for row in db.execute(
                "SELECT display_text FROM conversation_messages WHERE role='assistant' ORDER BY id")]

    def test_silent_answers_are_not_saved(self):
        result, system = self.ask("得呼叫网管是对的,你知道吗?", silence.MARK, heard=True)
        self.assertTrue(result["silent"])
        self.assertEqual(result["answer"], "")
        self.assertIn("always-on microphone during", system)
        self.assertIn(silence.MARK, system)
        result, _ = self.ask("色温标准。", "我不打扰。", heard=True)
        self.assertTrue(result["silent"])
        self.assertEqual(self.answers(), [])
        with closing(sqlite3.connect(str(self.db))) as db:  # what the user said is still kept
            kept = [row[0] for row in db.execute("SELECT raw_text FROM sent_messages ORDER BY id")]
        self.assertEqual(kept, ["得呼叫网管是对的,你知道吗?", "色温标准。"])

    def test_typed_and_real_answers(self):
        result, system = self.ask("别插话", "好，我不插话。", heard=False)
        self.assertFalse(result.get("silent"))
        self.assertNotIn("always-on microphone during", system)
        self.assertIn(silence.MARK, system)  # the rule is always there
        result, _ = self.ask("那八点叫我", "好，八点叫你。", heard=True)
        self.assertFalse(result.get("silent"))
        self.assertEqual(self.answers(), ["好，我不插话。", "好，八点叫你。"])


class Judge(unittest.TestCase):
    def test_the_judge_sees_the_side_talk(self):
        prompt = conversation_mode.judge_prompt([("you", "明天提醒我开会"), ("myn", "好的。")], "色温标准。",
                                                heard=["电脑是闪电模式。", "然后亮度100,暗部场景平衡。"])
        self.assertIn('Recently overheard', prompt)
        self.assertIn("暗部场景平衡", prompt)
        self.assertLess(prompt.index("Recently overheard"), prompt.index("New sentence"))
        self.assertNotIn('Recently overheard', conversation_mode.judge_prompt([], "你好"))
        seen = []
        conversation_mode.judge("k", [], "色温标准。", sender=lambda model, system, prompt, key, **kw:
                                seen.append(prompt) or '{"to_myn": false, "elsewhere": true, "ends": false}',
                                heard=["电脑是闪电模式。"])
        self.assertIn("电脑是闪电模式", seen[0])


if __name__ == "__main__":
    unittest.main()
