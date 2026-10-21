'Long replies read only their opening by default, support full reading on request and allow sufficient tool rounds to finish substantive work.'

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src" / "core"), str(BASE / "tests")]

import chat
import claude_client
import stt
import tts
from test_web_tools import reply

LONG = ("以下是整理好的内容。\n\n**一、基本概念**\n- 支点：杠杆转动时绕着的那个点。\n"
        "- 力臂：支点到力的作用线的垂直距离。\n\n**二、平衡条件**\n- 合力为零。\n- 合力矩为零。")


def streamed(text, whole=False, step=3):
    gate = tts.SpokenOpening(whole)
    out = []
    for i in range(0, len(text), step):
        out += gate.feed(text[i:i + step])
    return out + gate.finish()


class Opening(unittest.TestCase):
    def test_long_reply_says_only_its_opening(self):
        self.assertEqual(streamed(LONG), ["以下是整理好的内容。"])
        self.assertEqual(tts.spoken_part(LONG), "以下是整理好的内容。")
        # the opening is said as it streams in, before the rest has arrived
        gate = tts.SpokenOpening()
        self.assertEqual(gate.feed("以下是整理好的内容。"), ["以下是整理好的内容。"])

    def test_a_list_right_after_the_opening_line(self):
        text = "杠杆的知识点有这些：\n" + "\n".join("- 第%d点：%s" % (i, "力矩等于力乘以力臂，" * 3) for i in range(1, 5))
        self.assertEqual(streamed(text), ["杠杆的知识点有这些："])

    def test_one_long_paragraph_stops_after_a_sentence_or_so(self):
        text = "日本考大学的物理分三块。" + "共通测试考基础，二次试验考推导，留学生看 EJU 物理。" * 6
        said = "".join(streamed(text))
        self.assertLess(len(said), 160)
        self.assertTrue(said.endswith("。"))

    def test_short_replies_are_read_in_full(self):
        self.assertEqual(streamed("好的，今天东京是晴天。\n\n最高气温二十二度，记得带外套。"),
                         ["好的，今天东京是晴天。", "最高气温二十二度，记得带外套。"])
        self.assertEqual(streamed("杠杆要点：\n1. 力矩 = 力 × 力臂\n2. 平衡：顺时针等于逆时针"),
                         ["杠杆要点：", "力矩 = 力 × 力臂", "平衡：顺时针等于逆时针"])  # no bare "1."

    def test_setting_all_reads_everything(self):
        self.assertGreater(len(streamed(LONG, whole=True)), 3)
        self.assertEqual(tts.spoken_part(LONG, whole=True).count("合力"), 2)

    def test_tool_turn_starts_over(self):
        gate = tts.SpokenOpening()
        gate.feed("我看一下。")
        self.assertEqual(gate.reset(), [])
        self.assertEqual(gate.feed("以下是结果。\n\n- 一\n- 二"), ["以下是结果。"])

    def test_setting_saved_and_old_files(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(tts.load_settings(folder).long_reply, "opening")
            (Path(folder) / tts.SETTINGS_NAME).write_text(json.dumps({"provider": "elevenlabs", "auto_read": True}),
                                                           encoding="utf-8")
            self.assertEqual(tts.load_settings(folder).long_reply, "opening")  # older file
        with self.assertRaises(ValueError):
            tts.TTSSettings(long_reply="half").validate()

    def test_voice(self):
        for text in ("全部读出来", "把全文念出来", "都念一遍", "念全文", "读出来"):
            self.assertEqual(stt.voice_command(text), "read_all", text)
        for text in ("读书", "念经", "把这段翻译一下", "打开全文"):
            self.assertNotEqual(stt.voice_command(text), "read_all", text)


class TellsTheModel(unittest.TestCase):
    def test_the_rule_and_many_tool_rounds(self):
        with tempfile.TemporaryDirectory(prefix="myn-opening-") as folder:
            db = Path(folder) / "data" / "jarvis.db"
            db.parent.mkdir()
            session = chat.ChatSession(db)
            payloads = []

            def opener(request, timeout):
                payloads.append(json.loads(request.data.decode("utf-8")))
                if payloads[-1].get("tool_choice", {}).get("type") == "none":
                    return reply([{"type": "text", "text": "以下是整理好的内容。\n\n- 一\n- 二"}])
                return reply([{"type": "tool_use", "id": "t%d" % len(payloads), "name": "list_reminders",
                               "input": {}}], stop="tool_use")

            with mock.patch.object(session, "_key", return_value="k"), \
                    mock.patch.object(claude_client.urllib.request, "urlopen", side_effect=opener):
                result = session.ask("把你的朗读逻辑写给我")
            self.assertTrue(result["answer"].startswith("以下是整理好的内容"))  # Do not stop at the old four-round limit.
            self.assertEqual(len(payloads), claude_client.MAX_TOOL_ROUNDS + 1)
            system = json.dumps(payloads[0]["system"], ensure_ascii=False)
            self.assertIn("ONLY the opening line", system)
            self.assertIn('read everything', system)


if __name__ == "__main__":
    unittest.main()
