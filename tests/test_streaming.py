"""Streaming (SSE) replies, real multi-turn history and prompt caching, with no network."""

import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src" / "core")]

import chat
import claude_client
import conversation_log
import local_tools
import tts


def sse(*events):
    """Encode Messages API stream events the way the server frames them."""
    lines = []
    for event in events:
        lines.append("event: %s\n" % event["type"])
        lines.append("data: %s\n\n" % json.dumps(event, ensure_ascii=False))
    return io.BytesIO("".join(lines).encode("utf-8"))


def text_stream(*pieces, stop="end_turn", input_tokens=20, output_tokens=7):
    events = [{"type": "message_start", "message": {
                  "id": "msg", "type": "message", "role": "assistant", "content": [],
                  "stop_reason": None, "usage": {"input_tokens": input_tokens, "output_tokens": 1,
                                                  "cache_read_input_tokens": 5}}},
              {"type": "content_block_start", "index": 0,
               "content_block": {"type": "text", "text": ""}},
              {"type": "ping"}]
    events += [{"type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": piece}} for piece in pieces]
    events += [{"type": "content_block_stop", "index": 0},
               {"type": "message_delta", "delta": {"stop_reason": stop},
                "usage": {"output_tokens": output_tokens}},
               {"type": "message_stop"}]
    return sse(*events)


def tool_stream(name, arguments, tool_id="tool-1", preface=None, thinking=False):
    events = [{"type": "message_start", "message": {
        "id": "msg", "type": "message", "role": "assistant", "content": [],
        "stop_reason": None, "usage": {"input_tokens": 30, "output_tokens": 1}}}]
    index = 0
    if thinking:
        events += [{"type": "content_block_start", "index": index,
                    "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
                   {"type": "content_block_delta", "index": index,
                    "delta": {"type": "thinking_delta", "thinking": "need the file"}},
                   {"type": "content_block_delta", "index": index,
                    "delta": {"type": "signature_delta", "signature": "sig=="}},
                   {"type": "content_block_stop", "index": index}]
        index += 1
    if preface:
        events += [{"type": "content_block_start", "index": index,
                    "content_block": {"type": "text", "text": ""}},
                   {"type": "content_block_delta", "index": index,
                    "delta": {"type": "text_delta", "text": preface}},
                   {"type": "content_block_stop", "index": index}]
        index += 1
    raw = json.dumps(arguments, ensure_ascii=False)
    events += [{"type": "content_block_start", "index": index,
                "content_block": {"type": "tool_use", "id": tool_id, "name": name, "input": {}}},
               {"type": "content_block_delta", "index": index,
                "delta": {"type": "input_json_delta", "partial_json": raw[:5]}},
               {"type": "content_block_delta", "index": index,
                "delta": {"type": "input_json_delta", "partial_json": raw[5:]}},
               {"type": "content_block_stop", "index": index},
               {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 12}},
               {"type": "message_stop"}]
    return sse(*events)


class StreamingChecks(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="myn-stream-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / "data" / "jarvis.db"
        self.db.parent.mkdir()
        self.usage = self.db.parent / "claude-usage.jsonl"

    def test_text_deltas_arrive_in_order_and_rebuild_the_answer(self):
        payloads, deltas = [], []

        def opener(request, timeout):
            payloads.append(json.loads(request.data.decode("utf-8")))
            return text_stream("你好，", "我是 ", "MYN。")

        answer = claude_client.send(claude_client.SONNET, "sys", "hi", "k", opener=opener,
                                    on_text=deltas.append, usage_path=self.usage, purpose="chat")
        self.assertEqual(answer, "你好，我是 MYN。")
        self.assertEqual(deltas, ["你好，", "我是 ", "MYN。"])
        self.assertIs(payloads[0]["stream"], True)
        self.assertEqual(payloads[0]["system"][0]["cache_control"], {"type": "ephemeral"})
        usage = json.loads(self.usage.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual((usage["status"], usage["input_tokens"], usage["output_tokens"],
                          usage["cache_read_input_tokens"]), ("answered", 20, 7, 5))

    def test_without_callback_the_request_is_not_streamed(self):
        payloads = []

        def opener(request, timeout):
            payloads.append(json.loads(request.data.decode("utf-8")))
            return io.BytesIO(json.dumps({"stop_reason": "end_turn", "content": [
                {"type": "text", "text": "plain"}]}).encode("utf-8"))

        self.assertEqual(claude_client.send(claude_client.SONNET, "sys", "hi", "k",
                                            opener=opener), "plain")
        self.assertNotIn("stream", payloads[0])

    def test_streamed_tool_round_trip_resets_the_draft_between_turns(self):
        docs = self.root / "docs"
        docs.mkdir()
        note = docs / "note.txt"
        note.write_text("streamed proof", encoding="utf-8")
        local_tools.grant_read(self.db.parent, docs)
        payloads, deltas = [], []
        responses = [tool_stream("read_file", {"path": str(note)}, preface="我看一下。",
                                 thinking=True),
                     text_stream("文件里写着 ", "streamed proof。")]

        def opener(request, timeout):
            payloads.append(json.loads(request.data.decode("utf-8")))
            return responses.pop(0)

        answer = claude_client.send(
            claude_client.SONNET, "sys", "读文件", "k", opener=opener, on_text=deltas.append,
            read_tools=local_tools.TOOLS,
            tool_executor=lambda name, args: local_tools.execute(name, args, self.db))
        self.assertEqual(answer, "文件里写着 streamed proof。")
        self.assertEqual(deltas, ["我看一下。", None, "文件里写着 ", "streamed proof。"])
        assistant = payloads[1]["messages"][1]
        self.assertEqual(assistant["role"], "assistant")
        self.assertEqual(assistant["content"][0],
                         {"type": "thinking", "thinking": "need the file", "signature": "sig=="})
        self.assertEqual(assistant["content"][2]["input"], {"path": str(note)})
        result = payloads[1]["messages"][2]["content"][0]
        self.assertFalse(result["is_error"])
        self.assertIn("streamed proof", result["content"])

    def test_error_event_mid_stream_is_a_temporary_failure(self):
        def opener(request, timeout):
            return sse({"type": "message_start", "message": {"content": [], "usage": {}}},
                       {"type": "error", "error": {"type": "overloaded_error", "message": "x"}})

        with self.assertRaises(claude_client.ClaudeUnavailable) as caught:
            claude_client.send(claude_client.SONNET, "sys", "hi", "k", opener=opener,
                               on_text=lambda _delta: None, usage_path=self.usage)
        self.assertTrue(caught.exception.fallback_allowed)
        self.assertIn("overloaded_error", str(caught.exception))

    def test_broken_stream_is_rejected_not_half_adopted(self):
        def opener(request, timeout):
            return io.BytesIO(b"data: {not json}\n\n")

        with self.assertRaises(claude_client.ClaudeUnavailable):
            claude_client.send(claude_client.SONNET, "sys", "hi", "k", opener=opener,
                               on_text=lambda _delta: None)

    def test_streamed_max_tokens_keeps_the_draft_instead_of_restarting(self):
        calls = []

        def opener(request, timeout):
            calls.append(1)
            return text_stream("很长的回答……", stop="max_tokens")

        answer = claude_client.send(claude_client.SONNET, "sys", "hi", "k", opener=opener,
                                    on_text=lambda _delta: None, purpose="chat_sonnet")
        self.assertEqual(len(calls), 1)
        self.assertTrue(answer.startswith("很长的回答……"))
        self.assertIn('length limit', answer)

    def test_history_becomes_real_turns_with_a_cached_prefix(self):
        history = [{"question": "第一问", "answer": "第一答"},
                   {"question": "第二问", "answer": "第二答"}]
        messages = claude_client._history_messages(history)
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user", "assistant"])
        self.assertEqual(messages[0]["content"], "第一问")
        self.assertEqual(messages[-1]["content"][0],
                         {"type": "text", "text": "第二答", "cache_control": {"type": "ephemeral"}})
        long = [{"question": "q" * 11000, "answer": "a" * 11000},
                {"question": "新问", "answer": "新答"}]
        kept = claude_client._history_messages(long)
        self.assertEqual(kept[0]["content"], "新问")  # oldest turn dropped by the budget
        self.assertEqual(claude_client._history_messages([]), [])

    def test_recent_turns_keep_the_thread_but_a_pause_starts_fresh(self):
        session = chat.ChatSession(self.db)
        replies = iter(["你好呀", "刚才说的是天气"])
        with mock.patch.object(session, "_key", return_value="k"), \
                mock.patch.object(claude_client, "coordinated_reply",
                                  side_effect=lambda *a, **k: next(replies)) as reply:
            session.ask("你好")
            session.ask("我们刚才聊了什么？不用查记忆")
        history = reply.call_args.kwargs["history"]
        self.assertEqual(len(history), 1)
        self.assertRegex(history[0]["question"], '^\\[\\d{4}-\\d\\d-\\d\\d [A-Za-z]{3} \\d\\d:\\d\\d\\] 你好$')  # sent with its time
        self.assertEqual(history[0]["answer"], "你好呀")
        soon = datetime.now(timezone.utc) + timedelta(minutes=20)
        self.assertEqual(len(conversation_log.recent_turns(self.db, now=soon)), 2)
        later = datetime.now(timezone.utc) + timedelta(minutes=31)
        self.assertEqual(conversation_log.recent_turns(self.db, now=later), [])
        # A pause inside the history cuts off everything before it.
        with closing(sqlite3.connect(str(self.db))) as db:
            with db:
                old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
                first_ids = [row[0] for row in db.execute(
                    "SELECT id FROM conversation_messages ORDER BY id LIMIT 2")]
                db.executemany("UPDATE conversation_messages SET created_at=? WHERE id=?",
                               [(old, row_id) for row_id in first_ids])
        self.assertEqual([turn["question"] for turn in conversation_log.recent_turns(self.db)],
                         ["我们刚才聊了什么？不用查记忆"])
        with closing(sqlite3.connect(str(self.db))) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM conversation_messages WHERE role='assistant'").fetchone()[0], 2)

    def test_chat_session_streams_general_replies_to_the_caller(self):
        session = chat.ChatSession(self.db)
        deltas = []

        def opener(request, timeout):
            return text_stream("流式", "回答")

        with mock.patch.object(session, "_key", return_value="k"), \
                mock.patch.object(claude_client.urllib.request, "urlopen", side_effect=opener):
            result = session.ask("讲个笑话", on_text=deltas.append)
        self.assertEqual(result["answer"], "流式回答")
        self.assertEqual(deltas, ["流式", "回答"])

    def test_opus_escalation_restarts_the_draft_and_streams_opus(self):
        deltas, payloads = [], []
        responses = [tool_stream("request_opus", {"reason": "hard"}, tool_id="esc"),
                     text_stream("Opus ", "答复")]

        def opener(request, timeout):
            payloads.append(json.loads(request.data.decode("utf-8")))
            return responses.pop(0)

        answer = claude_client.coordinated_reply(
            "sys", "难题", "k", opener=opener, on_text=deltas.append,
            history=[{"question": "前一问", "answer": "前一答"}], system_dynamic="now",
            model=claude_client.SONNET)
        self.assertEqual(answer, "Opus 答复")
        self.assertEqual(deltas, [None, "Opus ", "答复"])
        self.assertEqual(payloads[1]["model"], claude_client.OPUS)
        self.assertEqual(payloads[1]["messages"][0], {"role": "user", "content": "前一问"})
        self.assertEqual(payloads[1]["messages"][-1], {"role": "user", "content": "难题"})
        self.assertIn('"sonnet_escalation_reason": "hard"', payloads[1]["system"][-1]["text"])


    def test_opus_is_the_default_main_model_and_answers_directly(self):
        payloads = []

        def opener(request, timeout):
            payloads.append(json.loads(request.data.decode("utf-8")))
            return text_stream("直接", "回答")

        with tempfile.TemporaryDirectory() as folder:
            usage = Path(folder) / "claude-usage.jsonl"
            answer = claude_client.coordinated_reply("sys", "你好", "k", opener=opener,
                                                     on_text=lambda _d: None, usage_path=usage)
            self.assertEqual(answer, "直接回答")
            self.assertEqual(len(payloads), 1)
            self.assertEqual(payloads[0]["model"], claude_client.OPUS)
            self.assertNotIn("request_opus", [tool["name"] for tool in payloads[0].get("tools", [])])
            self.assertEqual(json.loads(usage.read_text(encoding="utf-8").splitlines()[0])["purpose"], "chat_opus")
            with self.assertRaises(ValueError):
                claude_client.save_main_choice(folder, "sonnet")
            self.assertEqual(claude_client.model_for_usage(usage), claude_client.OPUS)
            (Path(folder) / claude_client.MODEL_SETTINGS).write_text("{broken", encoding="utf-8")
            self.assertEqual(claude_client.main_model(folder), claude_client.OPUS)
            with self.assertRaises(ValueError):
                claude_client.save_main_choice(folder, "haiku")


class SpeechStreamChecks(unittest.TestCase):
    def test_sentences_are_released_as_soon_as_they_end(self):
        stream = tts.SpeechStream()
        self.assertEqual(stream.feed("今天天"), [])
        self.assertEqual(stream.feed("气很好。明"), ["今天天气很好。"])
        self.assertEqual(stream.feed("天会下雨！"), ["明天会下雨！"])
        self.assertEqual(stream.finish(), [])

    def test_code_blocks_are_never_split_or_spoken(self):
        stream = tts.SpeechStream()
        self.assertEqual(stream.feed("示例如下：\n```python\nprint('a。b')\n"), ["示例如下："])
        self.assertEqual(stream.feed("x = 1\n```\n就这样。"), ['Code block omitted.', "就这样。"])

    def test_markdown_is_cleaned_and_reset_flushes_the_old_turn(self):
        stream = tts.SpeechStream()
        self.assertEqual(stream.feed("**我看一下"), [])
        self.assertEqual(stream.reset(), ["我看一下"])
        self.assertEqual(stream.feed("- 第一点\n"), ["第一点"])

    def test_long_sentence_without_full_stop_is_cut_at_a_comma(self):
        stream = tts.SpeechStream()
        released = stream.feed("甲" * 150 + "，" + "乙" * 60)
        self.assertEqual("".join(released), "甲" * 150 + "，")

    def test_total_speech_is_capped(self):
        stream = tts.SpeechStream(limit=10)
        self.assertEqual("".join(stream.feed("一二三四五六。七八九十十一。")), "一二三四五六。七八九")
        self.assertEqual(stream.feed("还有。"), [])


if __name__ == "__main__":
    unittest.main()
