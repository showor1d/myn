"""Scene-level understanding: grouping, analysis validation, storage and the profile."""

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
import memory_processor
import message_log
import scene_memory
import storage


def analysis(ids, memories=None, summary="在家边做饭边打电话"):
    return json.dumps({"scene": {"summary": summary, "setting": "家里，晚上",
                                 "speakers": ["用户", "妈妈（电话）"]},
                       "memories": memories if memories is not None else [
                           {"kind": "fact", "type": "event", "category": "life",
                            "content": "晚上自己做了咖喱", "why": "", "evidence": [ids[0]],
                            "confidence": 0.95},
                           {"kind": "inference", "type": "profile", "category": "people",
                            "content": "和妈妈关系亲近，经常通电话", "why": "主动打电话并聊了很久",
                            "evidence": [ids[1], ids[2]], "confidence": 0.6}]},
                      ensure_ascii=False)


class SceneChecks(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="myn-scene-")
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / "data" / "jarvis.db"
        self.db.parent.mkdir()
        self.session = chat.ChatSession(self.db)

    def lines(self, texts, minutes_ago=(20, 19, 18), channel="typed"):
        ids = []
        for text, ago in zip(texts, minutes_ago):
            row_id = message_log.record(self.db, text, channel=channel)
            moment = (datetime.now(timezone.utc) - timedelta(minutes=ago)).isoformat()
            with closing(sqlite3.connect(str(self.db))) as connection:
                with connection:
                    connection.execute("UPDATE sent_messages SET sent_at=? WHERE id=?", (moment, row_id))
            ids.append(row_id)
        return ids

    def test_lines_close_in_time_form_one_scene_and_a_pause_splits(self):
        first = self.lines(["今天自己做咖喱", "喂妈妈", "嗯我最近挺好的"], (40, 39, 38))
        second = self.lines(["开始看电影了"], (5,))
        claimed = scene_memory.claim_scene(self.db)
        self.assertEqual([row["id"] for row in claimed["rows"]], first)
        scene_memory.fail_scene(self.db, claimed, "test")
        with closing(sqlite3.connect(str(self.db))) as connection:
            with connection:
                connection.execute("UPDATE sent_messages SET next_attempt_at=NULL")
        # The 40-minute-old scene is taken first; the newer one waits until it settles.
        self.assertEqual([row["id"] for row in scene_memory.claim_scene(self.db)["rows"]], first)
        self.assertEqual([row["id"] for row in scene_memory.claim_scene(self.db)["rows"]], second)

    def test_a_scene_still_going_on_is_not_analysed_yet(self):
        self.lines(["我们现在在聊旅行"], (1,))
        self.assertIsNone(scene_memory.claim_scene(self.db))
        later = datetime.now(timezone.utc) + timedelta(minutes=4)
        self.assertIsNotNone(scene_memory.claim_scene(self.db, now=later))

    def test_scene_analysis_stores_facts_inferences_evidence_and_updates_profile(self):
        ids = self.lines(["今天晚上自己做了咖喱", "喂妈妈，最近挺好的", "嗯下周末回家看你"])
        sent = []

        def send(model, system, message, key, **kwargs):
            sent.append((kwargs["purpose"], json.loads(message)))
            if kwargs["purpose"] == "scene_analysis":
                return "```json\n" + analysis(ids) + "\n```"
            return json.dumps({"profile": "人际关系\n- 和妈妈很亲近（推断）"}, ensure_ascii=False)

        with mock.patch.object(claude_client, "send", side_effect=send):
            outcome = memory_processor.process_batch(self.db, key="synthetic")
        self.assertEqual((outcome["processed"], outcome["created"], outcome["scenes"]), (3, 1, 1))
        self.assertEqual(outcome["summary"], "在家边做饭边打电话")
        scene_input = sent[0][1]
        self.assertEqual([line["channel"] for line in scene_input["lines"]], ["typed"] * 3)
        self.assertEqual(scene_input["lines"][0]["id"], ids[0])
        detail = scene_memory.scene(self.db, 1)
        self.assertEqual(detail["speakers"], ['用户', "妈妈（电话）"])
        self.assertEqual([line["id"] for line in detail["lines"]], ids)
        texts = [memory["raw_text"] for memory in detail["memories"]]
        self.assertEqual(texts, ["今天晚上自己做了咖喱"])
        self.assertEqual(detail['analyses'][0]['content'], '和妈妈关系亲近，经常通电话')
        metadata = json.loads(detail['analyses'][0]['metadata'])
        self.assertEqual((metadata["kind"], metadata["why"]), ("inference", "主动打电话并聊了很久"))
        self.assertEqual(metadata["evidence_message_ids"], [ids[1], ids[2]])
        self.assertEqual(metadata["evidence_quotes"][0], "喂妈妈，最近挺好的")
        self.assertEqual([message_log.get(self.db, row_id)["scene_id"] for row_id in ids], [1, 1, 1])
        self.assertEqual(scene_memory.profile(self.db)[0], "人际关系\n- 和妈妈很亲近（推断）")
        self.assertEqual(sent[1][0], "profile_update")
        self.assertIn('(Inference) 和妈妈关系亲近，经常通电话', sent[1][1]["new_findings"])
        self.assertEqual(scene_memory.recent_scenes(self.db)[0]["memory_count"], 1)
        # Memories are searchable like any other.
        self.assertIn("今天晚上自己做了咖喱", [row["raw_text"] for row in storage.recent_memories(self.db)])

    def test_invalid_analysis_stores_nothing_and_retries_later(self):
        ids = self.lines(["今天去健身房了"], (20,))
        bad = [
            analysis(ids, [{"kind": "fact", "type": "event", "category": "fitness", "content": "去了健身房",
                            "why": "", "evidence": [999], "confidence": 1}]),        # evidence outside scene
            analysis(ids, [{"kind": "inference", "type": "profile", "category": "fitness",
                            "content": "很自律", "why": "", "evidence": ids, "confidence": 0.5}]),  # no reason
            analysis(ids, [{"kind": "fact", "type": "event", "category": "sports", "content": "去了健身房",
                            "why": "", "evidence": ids, "confidence": 1}]),          # unknown category
            analysis(ids, [{"kind": "fact", "type": "event", "category": "life",
                            "content": "密码是 hunter22", "why": "", "evidence": ids, "confidence": 1}]),
            "I think the user went to the gym.",
        ]
        for reply in bad:
            with self.subTest(reply=reply[:60]), mock.patch.object(claude_client, "send", return_value=reply):
                outcome = memory_processor.process_batch(self.db, key="synthetic")
                self.assertEqual((outcome["failed"], outcome["created"]), (1, 0))
                row = message_log.get(self.db, ids[0])
                self.assertEqual((row["status"], row["last_error_code"]), ("pending", "invalid_scene_analysis"))
                with closing(sqlite3.connect(str(self.db))) as connection:
                    with connection:
                        connection.execute("UPDATE sent_messages SET next_attempt_at=NULL")
        self.assertEqual(scene_memory.recent_scenes(self.db), [])

    def test_profile_failure_keeps_the_scene_and_retries_only_the_profile(self):
        ids = self.lines(["今天晚上自己做了咖喱", "喂妈妈", "下周回家"])
        failure = claude_client.ClaudeUnavailable("down", fallback_allowed=True)
        with mock.patch.object(claude_client, "send", side_effect=[analysis(ids), failure]):
            outcome = memory_processor.process_batch(self.db, key="synthetic")
        self.assertEqual(outcome["created"], 1)
        self.assertEqual(scene_memory.profile(self.db)[0], "")
        self.assertEqual(scene_memory.scenes_needing_profile(self.db), [1])
        with mock.patch.object(claude_client, "send", return_value='{"profile": "会做饭"}') as send:
            memory_processor.process_batch(self.db, key="synthetic")
        self.assertEqual(send.call_count, 1)  # the scene was not analysed twice
        self.assertEqual(scene_memory.profile(self.db)[0], "会做饭")
        self.assertEqual(scene_memory.scenes_needing_profile(self.db), [])

    def test_empty_scene_has_no_memories_and_no_profile_call(self):
        ids = self.lines(["（电视声音）明天全国大部分地区晴"], (20,))
        with mock.patch.object(claude_client, "send", return_value=analysis(ids, [], "电视在播天气预报")) as send:
            outcome = memory_processor.process_batch(self.db, key="synthetic")
        self.assertEqual((outcome["created"], outcome["scenes"], send.call_count), (0, 1, 1))
        self.assertEqual(message_log.get(self.db, ids[0])["status"], "processed")

    def test_user_edits_are_versions_and_chat_reads_the_profile(self):
        scene_memory.save_profile(self.db, "偏好与看法\n- 喜欢咖喱", "ai")
        scene_memory.save_profile(self.db, "偏好与看法\n- 喜欢咖喱\n- 不吃香菜", "user")
        text, row = scene_memory.profile(self.db)
        self.assertEqual((text.splitlines()[-1], row["source"]), ("- 不吃香菜", "user"))
        with self.assertRaises(ValueError):
            scene_memory.save_profile(self.db, "我的密码是 abc12345", "user")
        with mock.patch.object(self.session, "_key", return_value="k"), \
                mock.patch.object(claude_client, "coordinated_reply", return_value="好的") as reply:
            self.session.ask("推荐个晚饭")
        self.assertIn("不吃香菜", reply.call_args.args[0])
        self.assertIn('(Inference)', reply.call_args.args[0])

    def test_heard_lines_are_marked_and_old_journals_gain_the_new_columns(self):
        heard = self.session.record_heard("刚才那个视频挺好笑")
        typed = self.session.record_input("你好")
        self.assertEqual(message_log.get(self.db, heard)["channel"], "heard")
        self.assertEqual(message_log.get(self.db, typed)["channel"], "typed")
        old = self.db.parent / "old.db"
        with closing(sqlite3.connect(str(old))) as connection:
            with connection:
                connection.execute("""CREATE TABLE sent_messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    raw_text TEXT NOT NULL, sent_at TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('utterance','command')),
                    status TEXT NOT NULL CHECK(status IN ('pending','processing','processed','skipped')),
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT, lease_at TEXT,
                    lease_token TEXT, processed_at TEXT, derived_memory_id INTEGER, last_error_code TEXT)""")
                connection.execute("INSERT INTO sent_messages(raw_text,sent_at,kind,status) "
                                   "VALUES('旧消息','2026-10-15T00:00:00+00:00','utterance','pending')")
        scene_memory.ensure_schema(old)
        with closing(sqlite3.connect(str(old))) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(sent_messages)")}
            self.assertTrue({"channel", "scene_id"} <= columns)
            self.assertEqual(connection.execute("SELECT raw_text FROM sent_messages").fetchone()[0], "旧消息")
        backups = list((self.db.parent.parent / "backups").glob("*pre-scene*")) + \
            list(self.db.parent.glob("**/*pre-scene*"))
        self.assertTrue(backups or any(self.db.parent.parent.rglob("*pre-scene*")))

    def test_history_is_requeued_once_for_scene_analysis_without_losing_old_links(self):
        ids = self.lines(["今天我跑步了", "我的目标是明天跑步", "删掉的话"], (60, 59, 58), channel="typed")
        with closing(sqlite3.connect(str(self.db))) as connection:
            with connection:
                # The pre-0.20 state: processed one by one, one with an old derived memory.
                cursor = connection.execute(
                    "INSERT INTO memories(content,created_at,category,raw_text,memory_type,metadata,"
                    "source_message_id) VALUES('今天我跑步了','2026-10-15T00:00:00+00:00','fitness',"
                    "'今天我跑步了','event','{}',?)", (ids[0],))
                connection.execute("UPDATE sent_messages SET status='processed',derived_memory_id=? "
                                   "WHERE id=?", (cursor.lastrowid, ids[0]))
                connection.execute("UPDATE sent_messages SET status='processed' WHERE id=?", (ids[1],))
                connection.execute("UPDATE sent_messages SET status='skipped' WHERE id=?", (ids[2],))
        old_memory = message_log.get(self.db, ids[0])["derived_memory_id"]
        self.assertEqual(scene_memory.history_needing_scenes(self.db), 2)
        self.assertEqual(scene_memory.requeue_history(self.db), 2)
        self.assertEqual(message_log.get(self.db, ids[2])["status"], "skipped")
        self.assertTrue(any(self.db.parent.parent.rglob("*pre-history-reanalysis*")))
        seen = []

        def send(model, system, message, key, **kwargs):
            seen.append(json.loads(message))
            if kwargs["purpose"] == "scene_analysis":
                return analysis(ids, [{"kind": "inference", "type": "profile", "category": "fitness",
                                       "content": "在坚持跑步", "why": "连续两天都提到跑步",
                                       "evidence": [ids[1]], "confidence": 0.7}], "跑步计划")
            return '{"profile": "日常与习惯\\n- 在坚持跑步（推断）"}'

        with mock.patch.object(claude_client, "send", side_effect=send):
            outcome = memory_processor.process_batch(self.db, key="synthetic")
        self.assertEqual((outcome["processed"], outcome["created"]), (2, 0))
        self.assertIn("今天我跑步了", seen[0]["known_memories"])  # old memory shown as known
        self.assertEqual(message_log.get(self.db, ids[0])["derived_memory_id"], old_memory)
        self.assertEqual(scene_memory.history_needing_scenes(self.db), 0)
        self.assertEqual(scene_memory.requeue_history(self.db), 0)  # nothing to redo
        self.assertEqual(len(storage.recent_memories(self.db)), 1)  # inference is separate


    def test_profile_is_a_portrait_without_times_or_quotes(self):
        bad = ("性格\n- 做事钻研，遇到问题会坚持到解决（推断）\n- 10月12日熬夜到凌晨3点修bug\n"
               "- 说过“我最讨厌半途而废的事”\n- 昨天去了超市\n- 常在深夜工作\n- 周末喜欢打游戏\n"
               "- 晚上11点睡觉")
        self.assertEqual(memory_processor.profile_issues(bad),
                         ["- 10月12日熬夜到凌晨3点修bug", "- 说过“我最讨厌半途而废的事”",
                          "- 昨天去了超市", "- 晚上11点睡觉"])
        self.assertIn("never copy", memory_processor.PROFILE_SYSTEM)
        self.assertIn("No dates", memory_processor.PROFILE_SYSTEM)
        self.assertIn('Personality', memory_processor.PROFILE_SYSTEM)

        ids = self.lines(["今天晚上自己做了咖喱", "喂妈妈，最近挺好的", "嗯下周末回家看你"])
        sent = []

        def send(model, system, message, key, **kwargs):
            sent.append((kwargs["purpose"], json.loads(message)))
            if kwargs["purpose"] == "scene_analysis":
                return analysis(ids)
            if len(sent) == 2:  # first draft still logs a time: asked once more
                return json.dumps({"profile": "性格\n- 顾家（推断）\n- 10月16日晚上做了咖喱"}, ensure_ascii=False)
            return json.dumps({"profile": "性格\n- 顾家，和妈妈很亲近（推断）\n生活习惯\n- 会自己做饭"},
                              ensure_ascii=False)

        with mock.patch.object(claude_client, "send", side_effect=send):
            memory_processor.process_batch(self.db, key="synthetic")
        self.assertNotIn("time", sent[1][1]["scene"])  # no clock time handed to the profile step
        self.assertEqual(sent[2][1]["problem_lines"], ["- 10月16日晚上做了咖喱"])
        self.assertEqual(scene_memory.profile(self.db)[0], "性格\n- 顾家，和妈妈很亲近（推断）\n生活习惯\n- 会自己做饭")

    def test_a_retry_that_still_logs_times_has_those_lines_dropped(self):
        replies = iter(['{"profile": "性格\\n- 好奇心强（推断）\\n- 昨天看了纪录片"}',
                        '{"profile": "性格\\n- 好奇心强（推断）\\n- 10:30 起床"}'])
        with mock.patch.object(claude_client, "send", side_effect=lambda *a, **k: next(replies)):
            text = memory_processor._write_profile("synthetic", "{}", None, "profile_update")
        self.assertEqual(text, "性格\n- 好奇心强（推断）")

    def test_old_profile_is_reorganised_once(self):
        old = "近况\n- 2026-10-15 22:10 说“明天要早起”\n日常与习惯\n- 经常熬夜写代码"
        scene_memory.save_profile(self.db, old, "user")
        calls = []

        def send(model, system, message, key, **kwargs):
            calls.append((kwargs["purpose"], json.loads(message)))
            return json.dumps({"profile": "生活习惯\n- 常熬夜写代码，作息偏晚（推断）"}, ensure_ascii=False)

        with mock.patch.object(claude_client, "send", side_effect=send):
            outcome = memory_processor.process_batch(self.db, key="synthetic")
            memory_processor.process_batch(self.db, key="synthetic")  # not again
        self.assertEqual([purpose for purpose, _ in calls], ["profile_reorganise"])
        self.assertEqual(calls[0][1]["current_profile"], old)
        self.assertTrue(outcome["profile"])
        text, row = scene_memory.profile(self.db)
        self.assertEqual((text, row["source"]), ("生活习惯\n- 常熬夜写代码，作息偏晚（推断）", "ai"))
        with closing(sqlite3.connect(str(self.db))) as connection:  # the old version is kept
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM user_profile_versions").fetchone()[0], 2)
        # a failure leaves the profile untouched and is retried next time
        scene_memory.save_profile(self.db, old, "user")
        (memory_processor.data_location.config_for_db(self.db) / memory_processor.PROFILE_STYLE_NAME).unlink()
        failure = claude_client.ClaudeUnavailable("down", fallback_allowed=True)
        with mock.patch.object(claude_client, "send", side_effect=failure):
            memory_processor.process_batch(self.db, key="synthetic")
        self.assertEqual(scene_memory.profile(self.db)[0], old)
        self.assertFalse(memory_processor._style_done(self.db))


if __name__ == "__main__":
    unittest.main()
