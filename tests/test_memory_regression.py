"""Regressions for Tokyo dates, bilingual memory recall and Claude tool blocks."""

import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src" / "core")]

import chat
import claude_client
import conversation_log
import local_tools
import memory_query
import semantic_search
import storage


class MemoryRegressions(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="myn-memory-regression-")
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / "data" / "jarvis.db"
        self.db.parent.mkdir()
        storage.init_db(self.db)
        storage.save_memory("i get up late, might miss both breakfast and lunch", self.db)
        storage.save_memory("MYN program update", self.db)
        with closing(sqlite3.connect(str(self.db))) as connection:
            with connection:
                connection.execute("UPDATE memories SET id=28,created_at=?,category='life',memory_type='event' WHERE id=1",
                                   ("2026-10-15T11:59:40+09:00",))
                connection.execute("UPDATE memories SET id=22,created_at=?,category='update',memory_type='event' WHERE id=2",
                                   ("2026-10-14T18:00:00+09:00",))
        self.now = datetime(2026, 10, 16, 0, 30, tzinfo=memory_query.TOKYO)

    def test_1_structured_saved_date_life_event_returns_id_28(self):
        rows = memory_query.retrieve("搜索 2026年10月15日的生活事件。", self.db,
                                     now=self.now)
        self.assertEqual([row["memory_id"] for row in rows], [28])
        self.assertEqual(rows[0]["saved_at"], "2026-10-15T11:59:40+09:00")
        self.assertIsNone(rows[0]["event_at"])
        self.assertEqual(rows[0]["raw_text"],
                         "i get up late, might miss both breakfast and lunch")
        self.assertGreater(rows[0]["confidence"], 0)

    def test_2_keyword_and_cross_language_recall_id_28(self):
        for query in ("搜索 breakfast 和 lunch。", "我可能错过早餐了吗？"):
            with self.subTest(query=query):
                rows = memory_query.retrieve(query, self.db, now=self.now)
                self.assertEqual(rows[0]["memory_id"], 28)

    def test_yesterday_wake_question_finds_original_english_memory(self):
        self.assertEqual(semantic_search.ask("早起", self.db)[0]["id"], 28)
        rows = memory_query.retrieve("我昨天早上早起了吗", self.db, now=self.now)
        self.assertEqual([row["memory_id"] for row in rows], [28])
        self.assertIn(28, [row["memory_id"] for row in memory_query.retrieve(
            "我昨天早上做了什么", self.db, now=self.now)])
        self.assertEqual(chat._time_context("昨天", self.now)["saved_time_start"],
                         "2026-10-15T00:00:00+09:00")

    def test_pasted_old_record_is_not_a_new_event_today(self):
        quote = ("2026-10-15T11:59:40+09:00  [life/event]  "
                 "i get up late, might miss both breakfast and lunch,那这句话呢")
        quoted_id = conversation_log.append_user(self.db, quote, "test-session")
        question_id = conversation_log.append_user(self.db, "我今天早上早起了吗", "test-session")
        with closing(sqlite3.connect(str(self.db))) as connection:
            with connection:
                connection.execute("UPDATE conversation_messages SET created_at=? WHERE id IN (?,?)",
                                   ("2026-10-16T10:00:00+09:00", quoted_id, question_id))
        with mock.patch.object(memory_query, "saved_window",
                               side_effect=lambda text, now=None: self._pinned_window(text)):
            self.assertEqual(conversation_log.search(self.db, "我今天早上早起了吗"), [])
            self.assertEqual(memory_query.retrieve("我今天早上早起了吗", self.db), [])
            self.assertEqual([row["memory_id"] for row in memory_query.retrieve(quote, self.db)], [28])

    def _pinned_window(self, text):
        return self._saved_window(text, self.now)

    _saved_window = staticmethod(memory_query.saved_window)

    def test_quoted_record_routes_to_verified_memory_and_corrects_model_date(self):
        quote = ("2026-10-15T11:59:40+09:00  [life/event]  "
                 "i get up late, might miss both breakfast and lunch,那这句话呢")
        incorrect = json.dumps({"answer": "这是今天（10月15日）的情况，我没有记忆检索工具。",
                                "citations": [28], "grounded": True}, ensure_ascii=False)
        with mock.patch.object(chat.ChatSession, "_key", return_value="synthetic-key"), \
                mock.patch.object(claude_client, "coordinated_reply", return_value=incorrect) as reply:
            result = chat.ChatSession(self.db).ask(quote, recorded_id=False)
        self.assertEqual([row["memory_id"] for row in result["sources"]], [28])
        self.assertIn("might miss", result["answer"])
        self.assertNotIn("今天（10月15日）", result["answer"])
        self.assertNotIn("没有记忆检索工具", result["answer"])
        self.assertEqual(json.loads(reply.call_args.args[1])["time_context"]["timezone"], "Asia/Tokyo")

    def test_general_chat_gets_current_tokyo_time_and_recent_access_tools(self):
        with mock.patch.object(chat.ChatSession, "_key", return_value="synthetic-key"), \
                mock.patch.object(claude_client, "coordinated_reply", return_value="可以查询历史记录。") as reply:
            chat.ChatSession(self.db).ask("你能看到指令recent里的内容吗", recorded_id=False)
        self.assertEqual(reply.call_args.args[1], "你能看到指令recent里的内容吗")
        dynamic = reply.call_args.kwargs["system_dynamic"]
        prompt = json.loads(dynamic[dynamic.index("{"):])
        self.assertEqual(prompt["time_context"]["timezone"], "Asia/Tokyo")
        self.assertIn("now_local", prompt["time_context"])
        self.assertIn("search_history", prompt["available_read_tools"])

    def test_search_phrases_route_into_grounded_answer_context(self):
        answer = json.dumps({"answer": "找到符合条件的记忆。", "citations": [28],
                             "grounded": True}, ensure_ascii=False)
        with mock.patch.object(chat.ChatSession, "_key", return_value="synthetic-key"), \
                mock.patch.object(claude_client, "coordinated_reply", return_value=answer) as reply:
            session = chat.ChatSession(self.db)
            for question in ("搜索 2026年10月15日的生活事件。", "搜索 breakfast 和 lunch。"):
                with self.subTest(question=question):
                    result = session.ask(question)
                    self.assertEqual([row["memory_id"] for row in result["sources"]], [28])
                    prompt = json.loads(reply.call_args.args[1])
                    self.assertEqual(prompt["memories"][0]["memory_id"], 28)

    def test_tokyo_day_boundary_and_last_week(self):
        start, end = memory_query.saved_window("昨天", self.now)
        self.assertEqual(start.isoformat(), "2026-10-15T00:00:00+09:00")
        self.assertEqual(end.isoformat(), "2026-10-16T00:00:00+09:00")
        storage.save_memory("another life event", self.db)
        with closing(sqlite3.connect(str(self.db))) as connection:
            with connection:
                connection.execute("UPDATE memories SET created_at=?,category='life',memory_type='event' WHERE raw_text=?",
                                   ("2026-10-15T15:30:00+00:00", "another life event"))
        ids = [row["id"] for row in storage.memories_in_saved_range(
            start, end, self.db, category="life", memory_type="event")]
        self.assertEqual(ids, [28])
        week_start, week_end = memory_query.saved_window("上周", self.now)
        self.assertEqual(week_start.weekday(), 0)
        self.assertEqual((week_end - week_start).days, 7)

    def test_3_yesterday_question_uses_evidence_and_claude_thinking_text(self):
        original_window = memory_query.saved_window
        def pinned_window(text, now=None):
            return original_window(text, self.now)

        def opener(request, timeout):
            return io.BytesIO(json.dumps({"stop_reason": "end_turn", "content": [
                {"type": "thinking", "thinking": "synthetic", "signature": "sig"},
                {"type": "text", "text": json.dumps({"answer": "原文说可能错过早餐和午餐；不能确定实际有没有吃。",
                                                     "citations": [28], "grounded": True},
                                                    ensure_ascii=False)}]}, ensure_ascii=False).encode("utf-8"))

        with mock.patch.object(memory_query, "saved_window", side_effect=pinned_window), \
                mock.patch.object(chat.ChatSession, "_key", return_value="synthetic-key"), \
                mock.patch.object(claude_client.urllib.request, "urlopen", side_effect=opener):
            session = chat.ChatSession(self.db)
            result = session.ask("昨天我有没有提到可能错过早餐？请根据记忆原文回答。")
        # Claude's grounded answer is kept as written (no blanket "uncertain" rewrite).
        self.assertEqual(result["answer"], "原文说可能错过早餐和午餐；不能确定实际有没有吃。")
        self.assertEqual([row["memory_id"] for row in result["sources"]], [28])

    def test_stated_clause_is_a_fact_and_only_might_stays_uncertain(self):
        row = memory_query.retrieve("搜索 breakfast 和 lunch。", self.db, now=self.now)[0]
        claude = json.dumps({"answer": "你 10月15日起晚了，当时说可能会错过早餐和午餐。",
                             "citations": [28], "grounded": True}, ensure_ascii=False)
        answer, _ = chat._json_answer(claude, [row], trust_model=True)
        self.assertEqual(answer, "你 10月15日起晚了，当时说可能会错过早餐和午餐。")
        summary = chat._grounded_summary(row)
        self.assertIn('10/15 at 11:59', summary)
        self.assertIn('explicitly says "i get up late"', summary)
        self.assertIn('"might miss both breakfast and lunch" was only a possibility', summary)
        self.assertNotIn("无法判断", summary)

    def test_month_day_question_is_personal_and_dated(self):
        start, end = memory_query.saved_window("我10月15日起晚了吗？", self.now)
        self.assertEqual(start.isoformat(), "2026-10-15T00:00:00+09:00")
        self.assertEqual((end - start).days, 1)
        start, _ = memory_query.saved_window("12月31号我做了什么", self.now)
        self.assertEqual(start.year, 2025)  # not a future date
        self.assertTrue(chat._personal("我10月15日起晚了吗？"))
        self.assertIn(28, [row["memory_id"] for row in
                           memory_query.retrieve("我10月15日起晚了吗？", self.db, now=self.now)])

    def test_system_prompt_reads_stated_clauses_as_facts(self):
        with mock.patch.object(chat.ChatSession, "_key", return_value="synthetic-key"), \
                mock.patch.object(claude_client, "coordinated_reply",
                                  return_value=json.dumps({"answer": "起晚了。", "citations": [28],
                                                           "grounded": True})) as reply:
            with mock.patch.object(memory_query, "saved_window",
                                   side_effect=lambda text, now=None: self._pinned_window(text)):
                chat.ChatSession(self.db).ask("我10月15日起晚了吗？")
        system = reply.call_args.args[0]
        self.assertIn("getting up late is stated as fact", system)
        self.assertIn("saved_at is speech time, not event time", system)

    def test_thinking_and_parallel_tools_are_returned_verbatim(self):
        seen = []
        thinking = {"type": "thinking", "thinking": "synthetic", "signature": "sig"}

        def opener(request, timeout):
            payload = json.loads(request.data.decode("utf-8"))
            seen.append(payload)
            if len(seen) == 1:
                return io.BytesIO(json.dumps({"stop_reason": "tool_use", "content": [
                    thinking, {"type": "tool_use", "id": "a", "name": "search_memories",
                               "input": {"query": "breakfast"}},
                    {"type": "tool_use", "id": "b", "name": "search_memories",
                               "input": {"query": "lunch"}}]}).encode("utf-8"))
            return io.BytesIO(json.dumps({"stop_reason": "end_turn", "content": [
                thinking, {"type": "text", "text": "grounded reply"}]}).encode("utf-8"))

        answer = claude_client.send(claude_client.SONNET, "system", "question", "synthetic-key",
                                    opener=opener, read_tools=[local_tools.TOOLS[0]],
                                    tool_executor=lambda name, args: ('{"ok":true}', False),
                                    usage_path=self.db.parent / "claude-usage.jsonl")
        self.assertEqual(answer, "grounded reply")
        self.assertEqual(seen[1]["messages"][1]["content"][0], thinking)
        self.assertEqual([block["tool_use_id"] for block in seen[1]["messages"][2]["content"]],
                         ["a", "b"])
        protocol = (self.db.parent / "claude-protocol.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("breakfast", protocol)
        self.assertIn("tool_result_sent", protocol)

    def test_truncated_response_gets_one_bounded_retry(self):
        calls = []
        def opener(request, timeout):
            calls.append(json.loads(request.data.decode("utf-8"))["max_tokens"])
            stop, text = ("max_tokens", "partial") if len(calls) == 1 else ("end_turn", "complete")
            return io.BytesIO(json.dumps({"stop_reason": stop, "content": [
                {"type": "text", "text": text}]}).encode("utf-8"))
        answer = claude_client.send(claude_client.SONNET, "system", "question", "synthetic-key",
                                    opener=opener, max_tokens=100,
                                    usage_path=self.db.parent / "claude-usage.jsonl")
        self.assertEqual(answer, "complete")
        self.assertEqual(calls, [100, 200])
        self.assertIn("max_tokens", (self.db.parent / "claude-protocol.jsonl").read_text(encoding="utf-8"))

    def test_memory_json_format_gets_one_safe_retry(self):
        valid = json.dumps({"answer": "记忆原文表示可能性。", "citations": [28],
                            "grounded": True}, ensure_ascii=False)
        with mock.patch.object(chat.ChatSession, "_key", return_value="synthetic-key"), \
                mock.patch.object(claude_client, "coordinated_reply", return_value="prose answer"), \
                mock.patch.object(claude_client, "send", return_value=valid) as retry:
            result = chat.ChatSession(self.db).ask("搜索 2026年10月15日的生活事件。")
        self.assertEqual([row["memory_id"] for row in result["sources"]], [28])
        retry.assert_called_once()
        self.assertIn("memory_json_retry", (self.db.parent / "claude-protocol.jsonl").read_text(encoding="utf-8"))

    def test_unknown_tool_is_rejected_with_safe_diagnostic(self):
        def opener(request, timeout):
            return io.BytesIO(json.dumps({"stop_reason": "tool_use", "content": [
                {"type": "tool_use", "id": "x", "name": "delete_file",
                 "input": {"path": "secret"}}]}).encode("utf-8"))
        with self.assertRaises(claude_client.ClaudeUnavailable) as caught:
            claude_client.send(claude_client.SONNET, "system", "question", "synthetic-key",
                               opener=opener, read_tools=[local_tools.TOOLS[0]],
                               tool_executor=lambda name, args: ('{}', False),
                               usage_path=self.db.parent / "claude-usage.jsonl")
        self.assertIn("unknown tool", str(caught.exception))
        diagnostic = (self.db.parent / "claude-protocol.jsonl").read_text(encoding="utf-8")
        self.assertIn("unrecognized", diagnostic)
        self.assertNotIn("secret", diagnostic)

    def test_local_fallback_gets_the_same_retrieved_evidence(self):
        original_window = memory_query.saved_window
        def pinned_window(text, now=None):
            return original_window(text, self.now)
        captured = []

        def local_response(request, timeout):
            payload = json.loads(request.data.decode("utf-8"))
            captured.append(payload)
            content = json.dumps({"answer": "原文表示可能错过，无法确定实际是否吃了。",
                                  "citations": [28], "grounded": True}, ensure_ascii=False)
            return io.BytesIO(json.dumps({"done": True, "message": {"content": content}},
                                        ensure_ascii=False).encode("utf-8"))

        failure = claude_client.ClaudeUnavailable("temporary", fallback_allowed=True)
        with mock.patch.object(memory_query, "saved_window", side_effect=pinned_window), \
                mock.patch.object(chat.ChatSession, "_key", return_value="synthetic-key"), \
                mock.patch.object(claude_client, "coordinated_reply", side_effect=failure), \
                mock.patch.object(chat.local_llm, "configuration", return_value=chat.local_llm.Config("http://127.0.0.1:11434/api/chat", "test-model")), \
                mock.patch.object(chat.local_llm._transport, "open", side_effect=local_response):
            session = chat.ChatSession(self.db)
            result = session.ask("昨天我有没有提到可能错过早餐？", consent_local=lambda _: True)
        self.assertEqual([row["memory_id"] for row in result["sources"]], [28])
        evidence = json.loads(captured[0]["messages"][1]["content"])["memories"]
        self.assertEqual(evidence[0]["memory_id"], 28)
        self.assertEqual(evidence[0]["event_at"], None)

    def test_uncertain_source_cannot_become_definite_claim(self):
        row = memory_query.retrieve("搜索 breakfast 和 lunch。", self.db, now=self.now)[0]
        wrong = json.dumps({"answer": "确定没有吃早餐。", "citations": [28],
                            "grounded": True}, ensure_ascii=False)
        answer, sources = chat._json_answer(wrong, [row])
        self.assertIn("might miss", answer)
        self.assertNotIn("确定没有吃", answer)
        self.assertEqual(sources[0]["memory_id"], 28)


if __name__ == "__main__":
    unittest.main()
