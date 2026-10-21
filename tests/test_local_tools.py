"""Permission and actual tool-result round-trip checks with isolated data."""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src" / "core")]

import chat
import claude_client
import local_tools
import message_log
import storage


class LocalToolChecks(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="myn-tools-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / "data" / "jarvis.db"
        self.db.parent.mkdir()
        self.session = chat.ChatSession(self.db)

    def test_file_read_needs_explicit_grant_and_never_grants_mutation(self):
        docs = self.root / "docs"
        docs.mkdir()
        (docs / "note.txt").write_text("verified local content", encoding="utf-8")
        result, error = local_tools.execute("read_file", {"path": str(docs / "note.txt")}, self.db)
        self.assertTrue(error)
        self.assertIn('outside authorized', result)
        local_tools.grant_read(self.db.parent, docs)
        result, error = local_tools.execute("read_file", {"path": str(docs / "note.txt")}, self.db)
        self.assertFalse(error)
        self.assertEqual(json.loads(result)["result"], "verified local content")
        for action in ("write_file", "delete_file", "execute_file"):
            _, error = local_tools.execute(action, {"path": str(docs / "note.txt")}, self.db)
            self.assertTrue(error)
        self.assertEqual(local_tools.grants(self.db.parent)["write"], [])
        self.assertEqual(local_tools.grants(self.db.parent)["delete"], [])
        self.assertEqual(local_tools.grants(self.db.parent)["execute"], [])
        local_tools.revoke_read(self.db.parent, str(docs.resolve()))
        _, error = local_tools.execute("read_file", {"path": str(docs / "note.txt")}, self.db)
        self.assertTrue(error)
        audit = [json.loads(line) for line in (self.db.parent / local_tools.AUDIT_FILE).read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(row["tool"] == "read_file" and row["ok"] and row["count"] == 22 for row in audit))

    def test_outside_path_and_private_data_are_denied(self):
        docs = self.root / "docs"
        docs.mkdir()
        local_tools.grant_read(self.db.parent, docs)
        outside = self.root / "outside.txt"
        outside.write_text("private", encoding="utf-8")
        _, error = local_tools.execute("read_file", {"path": str(docs / ".." / "outside.txt")}, self.db)
        self.assertTrue(error)
        local_tools.grant_read(self.db.parent, self.root)
        _, error = local_tools.execute("read_file", {"path": str(self.db)}, self.db)
        self.assertTrue(error)
        (docs / ".env").write_text("secret", encoding="utf-8")
        _, error = local_tools.execute("read_file", {"path": str(docs / ".env")}, self.db)
        self.assertTrue(error)

    def test_drive_root_and_home_grants_are_refused_and_ignored(self):
        root = Path(self.root.anchor)
        with self.assertRaises(ValueError):
            local_tools.grant_read(self.db.parent, root)
        with self.assertRaises(ValueError):
            local_tools.grant_read(self.db.parent, Path.home())
        # A legacy whole-drive grant written before this rule no longer works.
        docs = self.root / "docs"
        docs.mkdir()
        (docs / "note.txt").write_text("legacy", encoding="utf-8")
        (self.db.parent / local_tools.GRANT_FILE).write_text(json.dumps(
            {"read": [str(root)], "write": [], "delete": [], "execute": []}), encoding="utf-8")
        result, error = local_tools.execute("read_file", {"path": str(docs / "note.txt")}, self.db)
        self.assertTrue(error)
        self.assertIn('Whole-drive and home-directory access is disabled', result)

    def test_missing_grant_does_not_break_other_grants(self):
        docs = self.root / "docs"
        gone = self.root / "gone"
        docs.mkdir()
        gone.mkdir()
        (docs / "note.txt").write_text("still readable", encoding="utf-8")
        local_tools.grant_read(self.db.parent, gone)
        local_tools.grant_read(self.db.parent, docs)
        gone.rmdir()
        result, error = local_tools.execute("read_file", {"path": str(docs / "note.txt")}, self.db)
        self.assertFalse(error)
        self.assertEqual(json.loads(result)["result"], "still readable")

    def test_credential_folders_are_hidden_and_unreadable(self):
        docs = self.root / "docs"
        (docs / ".ssh").mkdir(parents=True)
        (docs / "AppData").mkdir()
        (docs / ".ssh" / "notes.txt").write_text("key material", encoding="utf-8")
        (docs / "AppData" / "config.json").write_text("{}", encoding="utf-8")
        (docs / "plain.txt").write_text("ok", encoding="utf-8")
        local_tools.grant_read(self.db.parent, docs)
        listing, error = local_tools.execute("list_files", {"path": str(docs)}, self.db)
        self.assertFalse(error)
        self.assertEqual([row["name"] for row in json.loads(listing)["result"]], ["plain.txt"])
        for target in (docs / ".ssh" / "notes.txt", docs / "AppData" / "config.json"):
            _, error = local_tools.execute("read_file", {"path": str(target)}, self.db)
            self.assertTrue(error)
        # An explicitly granted folder may itself live under AppData.
        local_tools.grant_read(self.db.parent, docs / "AppData")
        _, error = local_tools.execute("read_file", {"path": str(docs / "AppData" / "config.json")}, self.db)
        self.assertFalse(error)


    def test_existing_db_memory_and_history_are_really_read(self):
        storage.save_memory("独立记忆测试", self.db)
        old_id = message_log.record(self.db, "旧对话提到了蓝色火车")
        current_id = message_log.record(self.db, "蓝色火车是什么？")
        memory, error = local_tools.execute("search_memories", {"query": "独立记忆"}, self.db)
        self.assertFalse(error)
        self.assertIn("独立记忆测试", memory)
        history, error = local_tools.execute("search_history", {"query": "蓝色火车"}, self.db,
                                             exclude_message_id=current_id)
        self.assertFalse(error)
        self.assertIn("旧对话提到了蓝色火车", history)
        self.assertEqual([row["id"] for row in json.loads(history)["result"]], [old_id])

    def test_history_question_triggers_live_history_lookup(self):
        message_log.record(self.db, "上周聊过蓝色火车")
        payloads = []

        def opener(request, timeout):
            payloads.append(json.loads(request.data.decode("utf-8")))
            if len(payloads) == 1:
                return io.BytesIO(json.dumps({"stop_reason": "tool_use", "content": [
                    {"type": "tool_use", "id": "history-1", "name": "search_history",
                     "input": {"query": "蓝色火车"}}]}).encode("utf-8"))
            return io.BytesIO(json.dumps({"stop_reason": "end_turn", "content": [
                {"type": "text", "text": "你提过蓝色火车。"}]}, ensure_ascii=False).encode("utf-8"))

        with mock.patch.object(self.session, "_key", return_value="synthetic-key"), \
                mock.patch.object(claude_client.urllib.request, "urlopen", side_effect=opener):
            answer = self.session.ask("我之前聊天记录提到了什么？")
        self.assertIn("蓝色火车", answer["answer"])
        self.assertIn("上周聊过蓝色火车", payloads[1]["messages"][2]["content"][0]["content"])

    def test_claude_tool_use_result_reaches_model(self):
        docs = self.root / "docs"
        docs.mkdir()
        note = docs / "note.txt"
        note.write_text("proof from disk", encoding="utf-8")
        local_tools.grant_read(self.db.parent, docs)
        payloads = []

        def opener(request, timeout):
            payload = json.loads(request.data.decode("utf-8"))
            payloads.append(payload)
            if len(payloads) == 1:
                return io.BytesIO(json.dumps({"stop_reason": "tool_use", "content": [
                    {"type": "tool_use", "id": "tool-1", "name": "read_file",
                     "input": {"path": str(note)}}]}).encode("utf-8"))
            return io.BytesIO(json.dumps({"stop_reason": "end_turn", "content": [
                {"type": "text", "text": "I read proof from disk."}]}).encode("utf-8"))

        with mock.patch.object(self.session, "_key", return_value="synthetic-key"), \
                mock.patch.object(claude_client.urllib.request, "urlopen", side_effect=opener):
            answer = self.session.ask("请读一下本地文件")
        self.assertIn("proof from disk", answer["answer"])
        names = [tool["name"] for tool in payloads[0]["tools"]]
        self.assertIn("read_file", names)
        self.assertIn("web_search", names)  # official web tools by default
        self.assertIn("set_volume", names)  # allowed computer actions too
        self.assertNotIn("search_history", [tool["name"] for tool in payloads[0]["tools"]])
        self.assertEqual(payloads[1]["messages"][1]["role"], "assistant")
        tool_result = payloads[1]["messages"][2]["content"][0]
        self.assertEqual(tool_result["tool_use_id"], "tool-1")
        self.assertFalse(tool_result["is_error"])
        self.assertIn("proof from disk", tool_result["content"])

    def test_claude_tool_error_is_returned_and_visible(self):
        missing = self.root / "outside.txt"
        missing.write_text("outside", encoding="utf-8")
        payloads = []

        def opener(request, timeout):
            payloads.append(json.loads(request.data.decode("utf-8")))
            if len(payloads) == 1:
                return io.BytesIO(json.dumps({"stop_reason": "tool_use", "content": [
                    {"type": "tool_use", "id": "tool-2", "name": "read_file",
                     "input": {"path": str(missing)}}]}).encode("utf-8"))
            return io.BytesIO(json.dumps({"stop_reason": "end_turn", "content": [
                {"type": "text", "text": "无法读取。"}]}).encode("utf-8"))

        with mock.patch.object(self.session, "_key", return_value="synthetic-key"), \
                mock.patch.object(claude_client.urllib.request, "urlopen", side_effect=opener):
            answer = self.session.ask("请读这个文件")
        self.assertIn('outside authorized read-only folders', answer["answer"])
        self.assertTrue(payloads[1]["messages"][2]["content"][0]["is_error"])


if __name__ == "__main__":
    unittest.main()
