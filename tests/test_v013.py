"""Data-safety and routing checks for the v0.13 release."""

import os
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src" / "core")]

import chat
import backup
import claude_client
import credentials
import data_location
import memory_processor
import scene_memory
import message_log
import storage


def api_reply(blocks, stop="end_turn", input_tokens=12, output_tokens=6):
    return io.BytesIO(json.dumps({"stop_reason": stop, "content": blocks,
                                  "usage": {"input_tokens": input_tokens,
                                            "output_tokens": output_tokens,
                                            "cache_creation_input_tokens": 0,
                                            "cache_read_input_tokens": 0}},
                                 ensure_ascii=False).encode("utf-8"))


class ReleaseChecks(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="myn-v013-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / "data" / "jarvis.db"
        self.db.parent.mkdir(exist_ok=True)
        if self.db.exists():
            self.db.unlink()
        with closing(sqlite3.connect(str(self.db))) as connection:
            with connection:
                connection.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                                   "content TEXT NOT NULL, created_at TEXT NOT NULL)")
                connection.execute("INSERT INTO memories (content, created_at) VALUES (?, ?)",
                                   ("今天学习了英语", datetime.now().astimezone().isoformat()))
        # Exercise the legacy low-level handoff with an isolated mock; production is Opus-only.
        model = mock.patch.object(claude_client, 'main_choice', return_value='sonnet')
        model.start()
        self.addCleanup(model.stop)
        self.session = chat.ChatSession(self.db)

    def test_direct_source_launcher_uses_personal_data_paths(self):
        install = self.root / "source-install"
        with mock.patch.dict(os.environ, {"MYN_DATA_DIR": "", "MYN_CONFIG_DIR": "",
                                          "MYN_PERSONAL_DATA_DIR": ""}), \
                mock.patch.object(data_location, "__file__", str(install / "src/data_location.py")):
            self.assertEqual(data_location.user_data_dir(), install / "personal_data/config")
            self.assertEqual(data_location.database_dir(), install / "personal_data/database")

    def test_windows_credential_is_encrypted_and_clearable(self):
        location = self.root / "credentials"
        credentials.save_key(location, "synthetic-test-key")
        encrypted = location / "claude-key.dpapi"
        self.assertTrue(encrypted.is_file())
        self.assertNotIn(b"synthetic-test-key", encrypted.read_bytes())
        self.assertEqual(credentials.get_key(location), "synthetic-test-key")
        credentials.clear_key(location)
        self.assertFalse(encrypted.exists())

    def test_claude_key_commands_are_removed_and_secret_text_is_rejected(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            self.assertEqual(credentials.claude_key_source(self.db.parent), "none")
            credentials.save_key(self.db.parent, "synthetic-test-key")
            self.assertEqual(credentials.claude_key_source(self.db.parent), "saved")
            with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "synthetic-env-key"}):
                self.assertEqual(credentials.claude_key_source(self.db.parent), "environment")
            credentials.clear_key(self.db.parent)
            first = self.session.ask("key")
            second = self.session.ask("key clear")
            self.assertIsNotNone(first["message_id"])
            self.assertIsNotNone(second["message_id"])
            self.assertEqual([row["raw_text"] for row in message_log.list_messages(self.db)],
                             ["key clear", "key"])
            help_text = self.session.ask("help")["answer"]
            self.assertNotIn("Claude 密钥", help_text)
            self.assertNotIn("Sonnet", help_text)
            rejected = self.session.ask("key synthetic-test-key")
            self.assertIn('nothing was sent or saved', rejected["answer"])
            self.assertIsNone(rejected["message_id"])
            command_secret = self.session.ask("save sk-ant-synthetic-secret-key")
            self.assertIn('nothing was sent or saved', command_secret["answer"])
            self.assertIsNone(command_secret["message_id"])
            self.assertEqual(len(message_log.list_messages(self.db)), 2)

    def test_sonnet_category_is_used_only_when_valid(self):
        with mock.patch.object(self.session, "_key", return_value="synthetic-key"), \
                mock.patch.object(claude_client, "send", return_value='{"category":"health"}') as send:
            self.session.ask("save 测试分类内容", confirm=lambda _: True)
        self.assertEqual(storage.get_memory(2, self.db)["category"], "health")
        self.assertEqual(send.call_args.args[0], claude_client.SONNET)
        with mock.patch.object(self.session, "_key", return_value="synthetic-key"), \
                mock.patch.object(claude_client, "send", return_value='{"category":"not-allowed"}'):
            self.session.ask("save 今天学习了数学", confirm=lambda _: True)
        self.assertEqual(storage.get_memory(3, self.db)["category"], "study")

    def test_schema_migration_backup_and_explicit_writes(self):
        with closing(sqlite3.connect(str(self.db))) as connection:
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertIn("trash_managed_at", {r[1] for r in connection.execute("PRAGMA table_info(memories)")})
            self.assertEqual(connection.execute("SELECT count(*) FROM memories").fetchone()[0], 1)
        self.assertTrue(list((self.root / "backups").glob("jarvis-auto-*.db")))
        self.assertTrue(list((self.root / "data").glob("jarvis.db.pre-myn-v0.13-*.bak")))
        cancelled = self.session.ask("save 健身卧推40kg", confirm=lambda _: False)
        self.assertIn('Cancel', cancelled["answer"])
        with mock.patch.object(self.session, "_key", return_value=""):
            saved = self.session.ask("save 健身卧推40kg", confirm=lambda _: True)
        self.assertIn('saved', saved["answer"])
        rows = storage.recent_memories(self.db)
        self.assertEqual(rows[0]["category"], "fitness")
        self.assertEqual(rows[0]["memory_type"], "event")
        self.assertEqual(len(rows), 2)
        self.session.ask("delete 2", confirm=lambda _: True)
        self.assertEqual(len(storage.recent_memories(self.db)), 1)
        self.assertEqual(storage.trash_memories(self.db)[0]["id"], 2)
        self.session.ask("restore 2", confirm=lambda _: True)
        self.assertEqual(len(storage.recent_memories(self.db)), 2)

    def test_seven_day_purge_preserves_legacy_trash(self):
        self.session.ask("delete 1", confirm=lambda _: True)
        old = (datetime.now().astimezone() - timedelta(days=8)).isoformat()
        with closing(sqlite3.connect(str(self.db))) as connection:
            with connection:
                connection.execute("UPDATE memories SET trash_managed_at=? WHERE id=1", (old,))
                connection.execute("INSERT INTO memories (content, raw_text, created_at, category, "
                                   "memory_type, metadata, brain_data, deleted_at) VALUES "
                                   "('legacy', 'legacy', ?, 'other', 'event', '{}', '{}', ?)", (old, old))
        self.assertEqual(storage.purge_expired_trash(self.db), 1)
        self.assertIsNone(storage.get_memory(1, self.db))
        self.assertEqual(len(storage.trash_memories(self.db)), 1)
        self.assertTrue(list((self.root / "backups").glob("jarvis-pre-purge-*.db")))

    def test_internal_rag_excludes_trash_and_requires_citations(self):
        with mock.patch.object(self.session, "_key", return_value="dummy"), \
                mock.patch.object(claude_client, "send", return_value='{"answer":"学了英语","citations":[1],"grounded":true}'):
            result = self.session.ask("我今天学了什么？")
        self.assertEqual([row["id"] for row in result["sources"]], [1])
        self.session.ask("delete 1", confirm=lambda _: True)
        with mock.patch.object(self.session, "_general", return_value={"answer": "从原始消息查找", "sources": []}) as general:
            result = self.session.ask("我今天学了什么？")
        self.assertEqual(result["answer"], "从原始消息查找")
        general.assert_called_once()

    def test_local_requires_claude_failure_and_consent(self):
        failure = claude_client.ClaudeUnavailable("temporary", fallback_allowed=True)
        with mock.patch.object(self.session, "_key", return_value="dummy"), \
                mock.patch.object(claude_client, "send", side_effect=failure), \
                mock.patch.object(chat.local_llm, "configuration", return_value=chat.local_llm.Config("http://127.0.0.1:11434/api/chat", "test-model")), \
                mock.patch.object(chat, "_local_rag", return_value=("本地回答", [])) as local:
            no = self.session.ask("我今天学了什么？", consent_local=lambda _: False)
            self.assertEqual(no["answer"], "temporary")
            local.assert_not_called()
            yes = self.session.ask("我今天学了什么？", consent_local=lambda _: True)
            self.assertEqual(yes["answer"], "本地回答")
            local.assert_called_once()
        with mock.patch.object(chat, "_local_rag") as local:
            self.session.ask("stats")
            local.assert_not_called()
        self.assertEqual(len(storage.recent_memories(self.db)), 1)

    def test_unconfigured_local_interface_never_prompts_or_connects(self):
        failure = claude_client.ClaudeUnavailable("temporary", fallback_allowed=True)
        consent = mock.Mock(return_value=True)
        with mock.patch.dict(os.environ, {"MYN_LOCAL_LLM_URL": "", "MYN_LOCAL_LLM_MODEL": ""}), \
                mock.patch.object(self.session, "_key", return_value="dummy"), \
                mock.patch.object(claude_client, "send", side_effect=failure), \
                mock.patch.object(chat.local_llm._transport, "open") as transport:
            result = self.session.ask("我今天学了什么？", consent_local=consent)
        self.assertEqual(result["answer"], "temporary")
        self.assertTrue(result["unavailable"])
        consent.assert_not_called()
        transport.assert_not_called()

    def test_local_service_failure_preserves_memories_and_reports_unavailable(self):
        failure = claude_client.ClaudeUnavailable("temporary", fallback_allowed=True)
        with mock.patch.object(self.session, "_key", return_value="dummy"), \
                mock.patch.object(claude_client, "send", side_effect=failure), \
                mock.patch.object(chat.local_llm, "configuration", return_value=chat.local_llm.Config(
                    "http://127.0.0.1:11434/api/chat", "test-model")), \
                mock.patch.object(chat.local_llm._transport, "open", side_effect=OSError("offline")):
            result = self.session.ask("我今天学了什么？", consent_local=lambda _: True)
        self.assertTrue(result["unavailable"])
        self.assertIn('Local model service is unavailable', result["answer"])
        self.assertFalse(result["sources"])
        self.assertEqual(len(storage.recent_memories(self.db)), 1)

    def test_permanent_claude_failure_does_not_offer_local_model(self):
        consent = mock.Mock(return_value=True)
        failure = claude_client.ClaudeUnavailable("bad credentials", fallback_allowed=False)
        with mock.patch.object(self.session, "_key", return_value="dummy"), \
                mock.patch.object(claude_client, "send", side_effect=failure), \
                mock.patch.object(chat.local_llm, "configuration") as configured, \
                mock.patch.object(chat.local_llm._transport, "open") as transport:
            result = self.session.ask("我今天学了什么？", consent_local=consent)
        self.assertTrue(result["unavailable"])
        configured.assert_not_called()
        consent.assert_not_called()
        transport.assert_not_called()

    def test_edit_type_category_and_legacy_commands(self):
        with mock.patch.object(self.session, "_key", return_value=""):
            self.assertIn('changed', self.session.ask("edit 1 今天复习了数学",
                                                     confirm=lambda _: True)["answer"])
        self.assertIn('Type', self.session.ask("type 1 profile", confirm=lambda _: True)["answer"])
        self.assertIn('Category', self.session.ask("category 1 goal", confirm=lambda _: True)["answer"])
        row = storage.get_memory(1, self.db)
        self.assertEqual((row["raw_text"], row["memory_type"], row["category"]),
                         ("今天复习了数学", "profile", "goal"))
        category_view = self.session.ask("category goal")["answer"]
        self.assertIn("今天复习了数学", category_view)
        self.assertNotIn("#1", category_view)
        self.assertIn("今天复习了数学", self.session.ask("month")["answer"])
        self.assertIn('disabled', self.session.ask("stats")["answer"])
        self.assertIn('disabled', self.session.ask("search 数学")["answer"])

    def test_claude_request_contract_and_citation_rejection(self):
        def opener(request, timeout):
            payload = json.loads(request.data.decode("utf-8"))
            self.assertEqual(payload["model"], claude_client.SONNET)
            self.assertNotIn("tools", payload)
            self.assertEqual(request.full_url, claude_client.ENDPOINT)
            self.assertEqual(request.get_header("Authorization"), "Bearer dummy-test-key")
            return io.BytesIO(json.dumps({"stop_reason": "end_turn", "content": [
                {"type": "text", "text": "好的"}]}).encode("utf-8"))
        self.assertEqual(claude_client.send(claude_client.SONNET, "system", "hello",
                                            "dummy-test-key", opener=opener), '好的')
        self.assertEqual(claude_client.DECISION_AUTHORITY, "opus")
        with mock.patch.object(self.session, "_key", return_value="dummy"), \
                mock.patch.object(claude_client, "send", return_value='{"answer":"错误","citations":[999],"grounded":true}'):
            self.assertEqual(self.session.ask("我今天学了什么？")["answer"], chat.INVALID_ANSWER)

    def test_all_local_memory_actions_without_any_key(self):
        original = "今天学习了数学；完整原文必须保留。"
        self.assertFalse((self.db.parent / "claude-key.dpapi").exists())
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}), \
                mock.patch.object(claude_client, "send") as send:
            cancelled = self.session.ask("记住" + original, confirm=lambda _: False)
            self.assertIn('Cancel', cancelled["answer"])
            self.assertEqual(len(storage.recent_memories(self.db)), 1)
            saved = self.session.ask("记住" + original, confirm=lambda _: True)
            self.assertIn('saved', saved["answer"])
            self.assertEqual(storage.get_memory(2, self.db)["raw_text"], original)
            self.assertEqual(storage.get_memory(2, self.db)["category"], "study")
            for command in ("recent", "today", "week", "month", "category study"):
                self.assertIn(original, self.session.ask(command)["answer"])
            self.assertIn("changed", self.session.ask("edit 2 今天复习了数学",
                                                      confirm=lambda _: True)["answer"])
            self.assertEqual(storage.get_memory(2, self.db)["raw_text"], "今天复习了数学")
            self.assertIn("Category changed", self.session.ask("type 2 goal",
                                                        confirm=lambda _: True)["answer"])
            self.assertEqual(storage.get_memory(2, self.db)["category"], "goal")
            self.assertIn("Type changed", self.session.ask("type 2 profile",
                                                        confirm=lambda _: True)["answer"])
            self.assertIn("Moved to trash", self.session.ask("delete 2", confirm=lambda _: True)["answer"])
            self.assertNotIn("今天复习了数学", self.session.ask("recent")["answer"].split("Extracted memories:")[-1])
            trash_view = self.session.ask("trash")["answer"]
            self.assertIn("今天复习了数学", trash_view)
            self.assertNotIn("#2", trash_view)
            self.assertIn('Restored', self.session.ask("restore 2", confirm=lambda _: True)["answer"])
            self.assertIn("今天复习了数学", self.session.ask("recent")["answer"])
            send.assert_not_called()

    def test_plain_statement_and_local_syntax_never_need_claude(self):
        with mock.patch.object(credentials, "get_key", side_effect=RuntimeError("bad credential")), \
                mock.patch.object(claude_client, "send") as send:
            plain = self.session.ask("今天练习了跑步", confirm=lambda _: True)
            self.assertIsNotNone(plain["message_id"])
            self.assertEqual(message_log.get(self.db, plain["message_id"])["raw_text"],
                             "今天练习了跑步")
            self.assertIn('saved', self.session.ask("save: 明天继续锻炼",
                                                   confirm=lambda _: True)["answer"])
            self.assertIn('saved', self.session.ask("请记住：周五开会",
                                                   confirm=lambda _: True)["answer"])
            recorded = self.session.ask("记录了今天的读书进度", confirm=lambda _: True)
            self.assertIsNotNone(recorded["message_id"])
            self.assertIn("Usage", self.session.ask("recent something")["answer"])
            self.assertIn("Tell me", self.session.ask("delete abc")["answer"])
            self.assertIn("Tell me", self.session.ask("show abc")["answer"])
            send.assert_not_called()
        self.assertEqual(message_log.get(self.db, recorded["message_id"])["raw_text"],
                         "记录了今天的读书进度")

    def test_keyless_chat_prompt_only_for_ai_and_keyed_chat_still_routes(self):
        with mock.patch.object(credentials, "get_key", return_value=""):
            self.assertIn('API key', self.session.ask("你好")["answer"])
        with mock.patch.object(credentials, "get_key", return_value="synthetic-test-key"), \
                mock.patch.object(claude_client, "send", return_value="你好，测试成功。") as send:
            self.assertEqual(self.session.ask("你好")["answer"], "你好，测试成功。")
            self.assertEqual(send.call_args.args[3], "synthetic-test-key")
            self.assertEqual(send.call_args.args[0], claude_client.SONNET)

    def test_deep_is_plain_text_and_sonnet_decides_complex_requests(self):
        help_text = self.session.ask("help")["answer"]
        self.assertNotIn("deep <", help_text)
        with mock.patch.object(credentials, "get_key", return_value=""):
            plain = self.session.ask("deep 你好")
        self.assertIsNotNone(plain["message_id"])
        self.assertEqual(message_log.get(self.db, plain["message_id"])["raw_text"], "deep 你好")
        with mock.patch.object(credentials, "get_key", return_value="synthetic-test-key"), \
                mock.patch.object(claude_client, "send", return_value="复杂问题测试回复") as send:
            reply = self.session.ask("请比较这两种方案的风险和成本，并给出建议")
        self.assertEqual(reply["answer"], "复杂问题测试回复")
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[0], claude_client.SONNET)
        evidence = storage.get_memory(1, self.db)
        answer = '{"answer":"记忆问答测试回复","citations":[1],"grounded":true}'
        with mock.patch.object(credentials, "get_key", return_value="synthetic-test-key"), \
                mock.patch.object(chat, "internal_retrieve", return_value=[evidence]), \
                mock.patch.object(claude_client, "send", side_effect=[
                    claude_client.OpusRequest("Need more reliable synthesis"), answer]) as send:
            result = self.session.ask("请详细比较我之前的学习记录，分析原因并给出建议？")
        self.assertEqual(result["answer"], "记忆问答测试回复")
        self.assertEqual([call.args[0] for call in send.call_args_list],
                         [claude_client.SONNET, claude_client.OPUS])

    def test_sonnet_can_answer_in_one_call_and_records_token_usage(self):
        requests = []
        def opener(request, timeout):
            requests.append(json.loads(request.data.decode("utf-8")))
            return api_reply([{"type": "text", "text": "你好，我来回答。"}],
                             input_tokens=24, output_tokens=9)
        with mock.patch.object(self.session, "_key", return_value="synthetic-key"), \
                mock.patch.object(claude_client.urllib.request, "urlopen", side_effect=opener):
            result = self.session.ask("你好")
        self.assertEqual(result["answer"], "你好，我来回答。")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["model"], claude_client.SONNET)
        self.assertEqual(requests[0]["tools"][0]["name"], "request_opus")
        self.assertEqual(message_log.get(self.db, result["message_id"])["raw_text"], "你好")
        records = [json.loads(line) for line in
                   (self.db.parent / "claude-usage.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(row["model"], row["status"], row["input_tokens"], row["output_tokens"])
                          for row in records], [(claude_client.SONNET, "answered", 24, 9)])
        self.assertNotIn("你好", (self.db.parent / "claude-usage.jsonl").read_text(encoding="utf-8"))

    def test_sonnet_requests_one_opus_answer_with_context_and_memory(self):
        payloads = []
        responses = [
            api_reply([{"type": "text", "text": "先前答复"}]),
            api_reply([{"type": "tool_use", "id": "tool-1", "name": "request_opus",
                        "input": {"reason": "Need careful reasoning"}}], "tool_use", 31, 8),
            api_reply([{"type": "text", "text": "Opus 综合答复"}], input_tokens=43, output_tokens=12),
            api_reply([{"type": "tool_use", "id": "tool-2", "name": "request_opus",
                        "input": {"reason": "Memory comparison needs care"}}], "tool_use"),
            api_reply([{"type": "text", "text":
                        '{"answer":"今天学了英语","citations":[1],"grounded":true}'}]),
        ]
        def opener(request, timeout):
            payloads.append(json.loads(request.data.decode("utf-8")))
            return responses.pop(0)
        with mock.patch.object(self.session, "_key", return_value="synthetic-key"), \
                mock.patch.object(claude_client.urllib.request, "urlopen", side_effect=opener):
            first = self.session.ask("你好")
            second = self.session.ask("请解释这个方案的两难取舍")
            third = self.session.ask("我今天学了什么？")
        self.assertEqual(second["answer"], "Opus 综合答复")
        self.assertEqual(third["answer"], "今天学了英语")
        self.assertEqual([row["id"] for row in third["sources"]], [1])
        self.assertEqual([payload["model"] for payload in payloads],
                         [claude_client.SONNET, claude_client.SONNET, claude_client.OPUS,
                          claude_client.SONNET, claude_client.OPUS])
        # A greeting starts quick chat without memory tools but can escalate to work.
        first_tools = [tool["name"] for tool in payloads[0]["tools"]]
        self.assertNotIn("search_memories", first_tools)
        self.assertIn("think_deeper", first_tools)
        self.assertEqual(payloads[0]["output_config"], {"effort": "low"})
        # Weighing a plan is work (task_kind): every read tool, deeper effort for Opus too.
        work_tools = [tool["name"] for tool in payloads[2]["tools"]]
        self.assertIn("list_files", work_tools)
        self.assertIn("search_memories", work_tools)
        self.assertNotIn("think_deeper", work_tools)
        self.assertEqual(payloads[2]["output_config"], {"effort": "high"})
        # Opus continues the same real conversation: earlier turn, then this request.
        second_opus = payloads[2]["messages"]
        self.assertEqual(second_opus[0]["role"], "user")
        self.assertRegex(second_opus[0]["content"], '^\\[\\d{4}-\\d\\d-\\d\\d [A-Za-z]{3} \\d\\d:\\d\\d\\] 你好$')
        self.assertEqual(second_opus[1]["content"][0]["text"], "先前答复")
        self.assertEqual(second_opus[-1], {"role": "user", "content": "请解释这个方案的两难取舍"})
        self.assertIn("Need careful reasoning", payloads[2]["system"][-1]["text"])
        rag_opus = json.loads(payloads[4]["messages"][-1]["content"])
        self.assertEqual(rag_opus["memories"][0]["id"], 1)
        self.assertEqual(rag_opus["recent_chat"][-1]["answer"], "Opus 综合答复")
        self.assertEqual([message_log.get(self.db, row["message_id"])["raw_text"]
                          for row in (first, second, third)],
                         ["你好", "请解释这个方案的两难取舍", "我今天学了什么？"])
        self.assertEqual(len(message_log.list_messages(self.db)), 3)
        usage = [json.loads(line) for line in
                 (self.db.parent / "claude-usage.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(usage), 5)
        self.assertEqual(usage[1]["status"], "request_opus")
        self.assertEqual(usage[2]["model"], claude_client.OPUS)

    def test_opus_failure_stops_without_recursion_or_duplicate_save(self):
        calls = []
        def opener(request, timeout):
            calls.append(json.loads(request.data.decode("utf-8"))["model"])
            if len(calls) == 1:
                return api_reply([{"type": "tool_use", "id": "tool-3",
                                   "name": "request_opus", "input": {"reason": "Need precision"}}],
                                 "tool_use")
            raise claude_client.urllib.error.HTTPError(request.full_url, 503, "unavailable", {}, None)
        waits = []
        with mock.patch.object(self.session, "_key", return_value="synthetic-key"), \
                mock.patch.object(claude_client, "_sleep", side_effect=waits.append), \
                mock.patch.object(claude_client.urllib.request, "urlopen", side_effect=opener):
            result = self.session.ask("请解释这个方案的权衡")
        self.assertIn('temporarily unavailable', result["answer"])
        # A 503 is retried a bounded number of times, then Opus gives up (no recursion).
        self.assertEqual(calls, [claude_client.SONNET] + [claude_client.OPUS] * (1 + claude_client.BUSY_RETRIES))
        self.assertEqual(len(waits), claude_client.BUSY_RETRIES)
        self.assertEqual(self.session.history, [])
        self.assertEqual(message_log.get(self.db, result["message_id"])["raw_text"],
                         "请解释这个方案的权衡")
        self.assertEqual(message_log.pending_count(self.db), 1)
        usage = [json.loads(line) for line in
                 (self.db.parent / "claude-usage.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["status"] for row in usage],
                         ["request_opus"] + ["http_503"] * (1 + claude_client.BUSY_RETRIES))
        self.assertIsNone(usage[1]["input_tokens"])

    def test_invalid_sonnet_tool_request_never_calls_opus(self):
        calls = []
        def opener(request, timeout):
            calls.append(1)
            return api_reply([{"type": "tool_use", "id": "one", "name": "request_opus",
                               "input": {"reason": "uncertain"}},
                              {"type": "tool_use", "id": "two", "name": "request_opus",
                               "input": {"reason": "again"}}], "tool_use")
        with mock.patch.object(self.session, "_key", return_value="synthetic-key"), \
                mock.patch.object(claude_client.urllib.request, "urlopen", side_effect=opener):
            result = self.session.ask("你好")
        self.assertIn('Invalid Claude response', result["answer"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(message_log.get(self.db, result["message_id"])["raw_text"], "你好")

    def test_configured_local_key_reaches_claude_messages_client(self):
        credentials.save_key(self.db.parent, "synthetic-dpapi-key")
        seen = []
        def opener(request, timeout):
            seen.append((request.full_url, request.get_header("Authorization"),
                         json.loads(request.data.decode("utf-8"))["model"]))
            return io.BytesIO(json.dumps({"stop_reason": "end_turn", "content": [
                {"type": "text", "text": "配置后聊天可用"}]}).encode("utf-8"))
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}), \
                mock.patch.object(claude_client.urllib.request, "urlopen", side_effect=opener):
            result = self.session.ask("你好")
        self.assertEqual(result["answer"], "配置后聊天可用")
        self.assertEqual(seen, [(claude_client.ENDPOINT, "Bearer synthetic-dpapi-key",
                                 claude_client.SONNET)])

    def test_classification_failure_cannot_drop_memory(self):
        with mock.patch.object(self.session, "_key", return_value="synthetic-test-key"), \
                mock.patch.object(claude_client, "send", side_effect=RuntimeError("classifier failed")):
            self.assertIn('saved', self.session.ask("记住今天学习了地理",
                                                   confirm=lambda _: True)["answer"])
        row = storage.get_memory(2, self.db)
        self.assertEqual(row["raw_text"], "今天学习了地理")
        self.assertEqual(row["category"], "study")

    def test_every_ordinary_message_is_durable_and_commands_skip_classifier_queue(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}), \
                mock.patch.object(claude_client, "send") as send:
            messages = ["  今天我跑步了  ", "你好", "我的目标是明天跑步"]
            ids = [self.session.ask(text)["message_id"] for text in messages]
            self.assertEqual([message_log.get(self.db, n)["raw_text"] for n in ids], messages)
            self.assertEqual([message_log.get(self.db, n)["status"] for n in ids],
                             ["pending"] * 3)
            self.assertEqual(len(storage.recent_memories(self.db)), 1)
            listed = self.session.ask("recent")
            self.assertIn('Original conversations', listed["answer"])
            self.assertIsNone(listed["message_id"])
            self.assertIn("今天我跑步了", self.session.ask("today")["answer"])
            self.assertIn("今天我跑步了", self.session.ask("week")["answer"])
            self.assertIn("今天我跑步了", self.session.ask("month")["answer"])
            self.assertNotIn("M#", listed["answer"])
            help_text = self.session.ask("help")["answer"]
            self.assertIn("Local commands remain in conversation history", help_text)
            self.assertNotIn("chat <", help_text)
            self.assertNotIn("ask <", help_text)
            self.assertNotIn("<编号>", help_text)
            self.assertEqual(len(message_log.list_messages(self.db)), 3)
            send.assert_not_called()
        restarted = chat.ChatSession(self.db)
        self.assertEqual([message_log.get(restarted.db_path, n)["raw_text"] for n in ids], messages)
        # The three lines form one scene, analysed together (0.20).
        replies = [json.dumps({"scene": {"summary": "跑步与目标", "setting": "日常", "speakers": ["用户"]},
                               "memories": [
                                   {"kind": "fact", "type": "event", "category": "fitness",
                                    "content": "今天跑了步", "why": "", "evidence": [ids[0]],
                                    "confidence": 0.95},
                                   {"kind": "fact", "type": "goal", "category": "goal",
                                    "content": "计划明天再跑步", "why": "", "evidence": [ids[2]],
                                    "confidence": 0.9}]}, ensure_ascii=False),
                   json.dumps({"profile": "日常与习惯\n- 在跑步"}, ensure_ascii=False)]
        with mock.patch.object(scene_memory, "SCENE_SETTLE", timedelta(0)), \
                mock.patch.object(claude_client, "send", side_effect=replies) as send:
            result = memory_processor.process_batch(self.db, limit=3, key="synthetic")
            self.assertEqual((result["processed"], result["created"], result["failed"]), (3, 2, 0))
            self.assertEqual(send.call_count, 2)  # one scene analysis + one profile update
        self.assertEqual([message_log.get(self.db, n)["status"] for n in ids],
                         ["processed"] * 3)
        self.assertEqual([message_log.get(self.db, n)["raw_text"] for n in ids], messages)
        self.assertEqual(message_log.pending_count(self.db), 0)
        derived = [storage.get_memory(message_log.get(self.db, n)["derived_memory_id"], self.db)
                   for n in (ids[0], ids[2])]
        self.assertEqual([row["memory_type"] for row in derived], ["event", "goal"])
        self.assertIsNone(message_log.get(self.db, ids[1])["derived_memory_id"])
        self.assertEqual(scene_memory.profile(self.db)[0], "日常与习惯\n- 在跑步")
        with mock.patch.object(scene_memory, "SCENE_SETTLE", timedelta(0)), \
                mock.patch.object(claude_client, "send") as send:
            self.assertEqual(memory_processor.process_batch(self.db, limit=3, key="synthetic")
                             ["processed"], 0)
            send.assert_not_called()
        self.assertEqual(len(storage.recent_memories(self.db)), 3)

    def test_failed_ai_processing_retries_without_duplicate_memory(self):
        sent = self.session.ask("今天复习了历史")
        message_id = sent["message_id"]
        failure = claude_client.ClaudeUnavailable("network", fallback_allowed=True)
        settle = mock.patch.object(scene_memory, "SCENE_SETTLE", timedelta(0))
        settle.start()
        self.addCleanup(settle.stop)
        with mock.patch.object(claude_client, "send", side_effect=failure):
            self.assertEqual(memory_processor.process_batch(self.db, key="synthetic")["failed"], 1)
        self.assertEqual(message_log.get(self.db, message_id)["status"], "pending")
        self.assertEqual(message_log.get(self.db, message_id)["attempts"], 1)
        with closing(sqlite3.connect(str(self.db))) as connection:
            with connection:
                connection.execute("UPDATE sent_messages SET next_attempt_at='2000-01-01' WHERE id=?",
                                   (message_id,))
        restarted = chat.ChatSession(self.db)
        reply = json.dumps({"scene": {"summary": "复习", "setting": "", "speakers": ["用户"]},
                            "memories": [{"kind": "fact", "type": "event", "category": "study",
                                          "content": "今天复习了历史", "why": "",
                                          "evidence": [message_id], "confidence": 1}]},
                           ensure_ascii=False)
        with mock.patch.object(claude_client, "send", side_effect=[reply, '{"profile": "在复习历史"}']):
            self.assertEqual(memory_processor.process_batch(restarted.db_path, key="synthetic")
                             ["created"], 1)
        with mock.patch.object(claude_client, "send") as send:
            memory_processor.process_batch(restarted.db_path, key="synthetic")
            send.assert_not_called()  # nothing left to do: no duplicate memory
        with closing(sqlite3.connect(str(self.db))) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM memories WHERE content=?",
                                                ("今天复习了历史",)).fetchone()[0], 1)
        self.assertEqual(message_log.get(self.db, message_id)["raw_text"], "今天复习了历史")

    def test_sensitive_input_rejected_and_journal_failure_does_not_block_command(self):
        discussion = self.session.ask("请解释 API Key 的用途")
        self.assertIsNotNone(discussion["message_id"])
        before = message_log.pending_count(self.db)
        rejected = self.session.ask("我的密码是 abc123")
        self.assertIsNone(rejected["message_id"])
        self.assertIn('nothing was sent or saved', rejected["answer"])
        self.assertEqual(message_log.pending_count(self.db), before)
        with mock.patch.object(message_log, "record", side_effect=OSError("disk full")):
            failed = self.session.ask("今天做了笔记")
            command = self.session.ask("delete 1", confirm=lambda _: True)
        self.assertIsNone(failed["message_id"])
        self.assertIn("Could not save the original message", failed["answer"])
        self.assertIsNone(command["message_id"])
        self.assertIn("Moved to trash", command["answer"])
        self.assertIsNotNone(storage.get_memory(1, self.db)["deleted_at"])

    def test_backup_includes_raw_log_and_interrupted_claim_resumes(self):
        sent = self.session.ask("今天我读完了一本书")
        message_id = sent["message_id"]
        saved = backup.backup_database(self.db, label="test-journal")
        with closing(sqlite3.connect(str(saved))) as copy:
            self.assertEqual(copy.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(copy.execute("SELECT raw_text FROM sent_messages WHERE id=?",
                                          (message_id,)).fetchone()[0], "今天我读完了一本书")
        claimed = message_log.claim_next(self.db)
        self.assertEqual(claimed["id"], message_id)
        with closing(sqlite3.connect(str(self.db))) as connection:
            with connection:
                connection.execute("UPDATE sent_messages SET lease_at='2000-01-01' WHERE id=?",
                                   (message_id,))
        chat.ChatSession(self.db)
        reply = json.dumps({"scene": {"summary": "读书", "setting": "", "speakers": ["用户"]},
                            "memories": [{"kind": "fact", "type": "event", "category": "life",
                                          "content": "今天读完了一本书", "why": "",
                                          "evidence": [message_id], "confidence": 1}]},
                           ensure_ascii=False)
        with mock.patch.object(scene_memory, "SCENE_SETTLE", timedelta(0)), \
                mock.patch.object(claude_client, "send", side_effect=[reply, '{"profile": "爱读书"}']):
            self.assertEqual(memory_processor.process_batch(self.db, key="synthetic")["created"], 1)
        self.assertEqual(message_log.get(self.db, message_id)["status"], "processed")


if __name__ == "__main__":
    unittest.main()
