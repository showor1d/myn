"""Two-track persistence, migration, search, backup and privacy regressions."""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src" / "core")]

import backup
import chat
import claude_client
import conversation_log
import data_location
import local_tools
import memory_processor
import message_log
import personal_profile
import storage


class PersonalDataChecks(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="myn-personal-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / "database" / "jarvis.db"
        self.session = chat.ChatSession(self.db)

    def test_both_roles_persist_and_survive_restart(self):
        with mock.patch.object(self.session, "_dispatch",
                               return_value={"answer": "MYN synthetic reply", "sources": []}):
            result = self.session.ask("I studied astronomy today")
        rows = conversation_log.list_messages(self.db)
        self.assertEqual([row["role"] for row in rows[:2]], ["assistant", "user"])
        self.assertEqual(rows[0]["reply_to_id"], rows[1]["id"])
        self.assertEqual(rows[1]["source_message_id"], result["message_id"])
        self.assertEqual(rows[0]["session_id"], rows[1]["session_id"])
        self.assertTrue(rows[0]["created_at"].endswith("+00:00"))
        self.assertEqual(chat.ChatSession(self.db).history[-1]["answer"], "MYN synthetic reply")

    def test_command_is_archived_without_entering_classifier_queue(self):
        self.session.ask("help")
        self.assertEqual(message_log.pending_count(self.db), 0)
        rows = conversation_log.list_messages(self.db)
        self.assertEqual([row["role"] for row in rows[:2]], ["assistant", "user"])
        self.assertEqual(rows[1]["raw_text"], "help")

    def test_legacy_user_messages_backfill_once_without_fake_replies(self):
        old = self.root / "old"
        old.mkdir()
        source = old / "jarvis.db"
        storage.init_db(source)
        message_log.record(source, "legacy original message")
        with sqlite3.connect(str(source)) as db:
            db.execute("DROP TABLE conversation_messages")
            db.execute("DROP TABLE conversation_audit")
            db.commit()
        with mock.patch.dict(os.environ, {"MYN_PERSONAL_DATA_DIR": str(self.root / "personal_data"),
                                       "MYN_DATA_DIR": "", "MYN_CONFIG_DIR": ""}), \
                mock.patch.object(data_location, "legacy_candidates", return_value=[old]):
            migrated, mode = data_location.prepare_data()
            self.assertEqual(mode, "migrated")
            self.assertEqual(len(list((self.root / "personal_data" / "backups").glob(
                "pre-migration-*.db"))), 1)
            chat.ChatSession(migrated)
            rows = conversation_log.list_messages(migrated)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["role"], "user")
            self.assertIsNone(rows[0]["session_id"])
            self.assertEqual(rows[0]["origin"], "legacy-user-journal")
            self.assertEqual(data_location.prepare_data()[1], "existing")
            self.assertEqual(len(conversation_log.list_messages(migrated)), 1)
            with closing(sqlite3.connect(str(source))) as source_db:
                self.assertEqual(source_db.execute(
                    "SELECT COUNT(*) FROM sent_messages").fetchone()[0], 1)

    def test_unified_search_finds_raw_without_derived_memory(self):
        source_id = self.session.record_input("I saw a blue heron near the station")
        self.assertEqual(len(storage.recent_memories(self.db)), 0)
        hits = conversation_log.search(self.db, "blue heron", exclude_source_id=-1)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["source_message_id"], source_id)
        result, error = local_tools.execute("search_history", {"query": "blue heron"}, self.db)
        self.assertFalse(error)
        self.assertEqual(json.loads(result)["result"][0]["role"], "user")

    def test_raw_only_history_enters_grounded_claude_context(self):
        self.session.record_input("我看见蓝色火车")
        captured = []
        def answer(_system, prompt, _key, **_options):
            captured.append(json.loads(prompt))
            source = captured[-1]["memories"][0]
            return json.dumps({"answer": "你提到看见蓝色火车。",
                               "citations": [source["id"]], "grounded": True},
                              ensure_ascii=False)
        with mock.patch.object(self.session, "_key", return_value="synthetic-key"), \
                mock.patch.object(claude_client, "coordinated_reply", side_effect=answer):
            reply = self.session.ask("我以前有没有提到蓝色火车？")
        self.assertIn("蓝色火车", reply["answer"])
        self.assertEqual(reply["sources"][0]["source_type"], "conversation")
        self.assertLess(captured[0]["memories"][0]["id"], 0)

    def test_raw_history_cross_language_and_tokyo_date_boundary(self):
        source_id = self.session.record_input("I might miss breakfast")
        self.assertEqual(conversation_log.search(self.db, "我可能错过早餐")[0]["source_message_id"],
                         source_id)
        with sqlite3.connect(str(self.db)) as db:
            db.execute("UPDATE conversation_messages SET created_at=? WHERE source_message_id=?",
                       ("2026-10-15T15:00:00+00:00", source_id))
            db.commit()
        dated = conversation_log.search(self.db, "搜索2026年10月16日的记录")
        self.assertEqual(dated[0]["source_message_id"], source_id)

    def test_prior_question_and_ai_reply_are_not_personal_fact_evidence(self):
        with mock.patch.object(self.session, "_dispatch",
                               return_value={"answer": "I guessed you visited Mars", "sources": []}):
            self.session.ask("Did I visit Mars?")
        self.assertEqual(conversation_log.search(self.db, "Did I visit Mars?"), [])
        self.assertTrue(memory_processor._reference_only("搜索之前的记忆原文？"))
        self.assertTrue(memory_processor._reference_only("引用旧记忆：我看见蓝色火车"))

    def test_edit_delete_restore_and_audit_preserve_original(self):
        self.session.record_input("I prefer green tea")
        row = conversation_log.list_messages(self.db)[0]
        self.assertTrue(conversation_log.change(self.db, row["id"], "edit", text="I prefer black tea"))
        self.assertTrue(conversation_log.change(self.db, row["id"], "delete"))
        self.assertEqual(conversation_log.search(self.db, "black tea"), [])
        self.assertTrue(conversation_log.change(self.db, row["id"], "restore"))
        edited = conversation_log.get(self.db, row["id"])
        self.assertEqual(edited["raw_text"], "I prefer green tea")
        self.assertEqual(edited["display_text"], "I prefer black tea")
        self.assertEqual(message_log.get(self.db, row["source_message_id"])["status"], "skipped")
        with sqlite3.connect(str(self.db)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM conversation_audit").fetchone()[0], 3)

    def test_deleting_pending_raw_record_prevents_later_memory_extraction(self):
        source_id = self.session.record_input("I prefer green tea")
        row = conversation_log.list_messages(self.db)[0]
        self.assertTrue(conversation_log.change(self.db, row["id"], "delete"))
        self.assertEqual(message_log.get(self.db, source_id)["status"], "skipped")
        self.assertIsNone(message_log.claim_next(self.db))
        self.assertTrue(conversation_log.change(self.db, row["id"], "restore"))
        self.assertEqual(message_log.get(self.db, source_id)["status"], "pending")

    def test_export_and_verified_restore(self):
        original_id = storage.save_memory("I like tea", self.db)
        self.session.record_input("I like tea")
        old_backup = backup.backup_database(self.db, label="test")
        self.assertTrue(storage.delete_memory(original_id, self.db))
        previous = personal_profile.restore_backup(self.db, old_backup)
        self.assertTrue(previous.is_file())
        self.assertIsNone(storage.get_memory(original_id, self.db)["deleted_at"])
        manifest = personal_profile.export_profile(self.db)
        exported = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(exported["memory_count"], 1)
        self.assertTrue(Path(exported["conversations"]).is_file())
        self.assertEqual(personal_profile.status(self.db)["active_memories"], 1)
        self.assertFalse(any(path.name.endswith(".dpapi") for path in (self.root / "exports").iterdir()))

    def test_private_database_is_not_model_file_access_even_with_parent_grant(self):
        config = self.root / "config"
        config.mkdir()
        local_tools.grant_read(config, self.root)
        with mock.patch.object(data_location, "config_for_db", return_value=config):
            _, error = local_tools.execute("read_file", {"path": str(self.db)}, self.db)
        self.assertTrue(error)

    def test_credentials_cannot_be_saved_as_ordinary_memory(self):
        with self.assertRaises(ValueError):
            storage.save_memory("api key: synthetic-secret-value", self.db)
        self.assertEqual(storage.recent_memories(self.db), [])

    def test_memory_changes_have_transactional_audit(self):
        identifier = storage.save_memory("I like tea", self.db)
        self.assertTrue(storage.edit_memory(identifier, "I like coffee", self.db))
        self.assertTrue(storage.delete_memory(identifier, self.db))
        self.assertTrue(storage.restore_memory(identifier, self.db))
        with sqlite3.connect(str(self.db)) as db:
            actions = [row[0] for row in db.execute(
                "SELECT action FROM memory_audit WHERE memory_id=? ORDER BY id", (identifier,))]
        self.assertEqual(actions, ["create", "edit", "delete", "restore"])


if __name__ == "__main__":
    unittest.main()
