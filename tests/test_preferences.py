"""Communication-preference learning: feedback, rules, scenes, context, and the chat flow.

Everything runs in temporary folders with a fake Messages API; the user's real data is
never touched. Acceptance checks 1–10 of the 2026-10-17 request are marked "A1"…"A10".
"""

import io
import json
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src" / "core")]

import chat  # noqa: E402
import claude_client  # noqa: E402
import conversation_log  # noqa: E402
import preferences  # noqa: E402
import storage  # noqa: E402


def reply(text):
    """A complete (non-streamed) Messages API answer."""
    return io.BytesIO(json.dumps({"content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                                  "usage": {"input_tokens": 10, "output_tokens": 5}}).encode("utf-8"))


class FakeAPI:
    """Records every request; answers with the queued texts (or a default)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.payloads = []

    def __call__(self, request, timeout):
        self.payloads.append(json.loads(request.data.decode("utf-8")))
        return reply(self.answers.pop(0) if self.answers else "好的。")

    @property
    def last_system(self):
        return "\n".join(block["text"] for block in self.payloads[-1]["system"])


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="myn-prefs-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / "data" / "jarvis.db"
        self.db.parent.mkdir()
        self.session = chat.ChatSession(self.db)
        self.api = FakeAPI()

    def ask(self, text, key="k", api=None):
        with mock.patch.object(self.session, "_key", return_value=key), \
                mock.patch.object(claude_client.urllib.request, "urlopen", side_effect=api or self.api):
            return self.session.ask(text)

    @property
    def store(self):
        return self.session.preferences.store

    def active_texts(self):
        return [rule["text"] for rule in self.store.rules() if rule["status"] == "active"]


class DetectionChecks(unittest.TestCase):
    def test_corrections_praise_and_ordinary_talk_are_told_apart(self):
        cases = {
            "你这样说话太机械了。": (-1, True, "tone"),
            "以后不要每次都夸我": (-1, True, "praise"),
            "不要解释这么多，直接告诉我结果": (-1, True, "verbosity"),
        }
        for text, (polarity, explicit, dimension) in cases.items():
            found = preferences.detect_feedback(text)
            self.assertIsNotNone(found, text)
            self.assertEqual(found["polarity"], polarity, text)
            self.assertEqual(found["explicit"], explicit, text)
            self.assertEqual(found["dims"][0][0], dimension, text)
        liked = preferences.detect_feedback("刚才那种说法我很喜欢，以后就这样说")
        self.assertEqual(liked["polarity"], 1)
        self.assertTrue(liked["keep_style"])
        for ordinary in ("今天天气怎么样", "以后我想去北京工作", "我不喜欢下雨", "帮我写一个排序函数", "你好"):
            self.assertIsNone(preferences.detect_feedback(ordinary), ordinary)

    def test_unclear_feedback_makes_no_rule_and_facts_are_not_style(self):
        unclear = preferences.detect_feedback("你这样回答不太好")
        self.assertTrue(unclear["ambiguous"])
        self.assertFalse(unclear["explicit"])
        content = preferences.detect_feedback("你说的不对，答案是 42")
        self.assertEqual(content["about"], "content")
        self.assertFalse(content["explicit"])

    def test_an_example_of_the_wanted_answer_is_picked_up(self):
        found = preferences.detect_feedback(
            "我觉得你的回答太繁琐了我不喜欢，我想简短一点比如：“可以实现，主要需要以下三个模块……”")
        self.assertEqual(found["ideal"], "可以实现，主要需要以下三个模块……")
        self.assertIn(("verbosity", "less"), found["dims"])

    def test_scenes(self):
        self.assertEqual(preferences.classify_scene("这个 python 函数报错了怎么办"), "coding")
        self.assertEqual(preferences.classify_scene("能给我讲讲导数的定义吗"), "study")
        self.assertEqual(preferences.classify_scene("我今天好累，心情很差"), "emotional")
        self.assertEqual(preferences.classify_scene("东京现在几点？"), "quick")
        self.assertEqual(preferences.classify_scene("帮我把音量调大"), "task")
        self.assertEqual(preferences.classify_scene("哈哈今天吃了拉面"), "casual")
        self.assertEqual(preferences.classify_scene("为什么分布式系统需要共识算法，各方案利弊是什么"), "complex")
        self.assertEqual(preferences.classify_scene("那第二步呢？", previous="study"), "study")

    def test_one_off_requests_and_talk_about_others_are_not_lasting_rules(self):
        # Requests for this one answer: followed now, not a lasting rule.
        for one_off in ("用列表列出东京的景点", "直接告诉我结果是多少", "不要用markdown格式写这篇文章",
                        "你可以用列表列出来吗"):
            found = preferences.detect_feedback(one_off)
            self.assertIsNotNone(found, one_off)
            self.assertTrue(found["one_off"], one_off)
            self.assertFalse(found["explicit"], one_off)
        # About someone or something else.
        for other in ("他说话太机械了", "我同事说话太啰嗦了", "这部电影太长了", "老师讲得太详细了我听不懂"):
            self.assertIsNone(preferences.detect_feedback(other, recently_rated=True), other)
        # Ordinary requests are answered, not treated as an unclear complaint.
        for request in ("你回答一下这个问题", "你可以详细讲讲吗", "以后不要再推荐这家店了"):
            self.assertIsNone(preferences.detect_feedback(request), request)

    def test_more_ways_of_saying_how_to_talk(self):
        cases = {
            "别这么正式": ("tone", "less"), "说话随意一点": ("tone", "less"),
            "你说话能不能别那么客气": ("tone", "less"), "不要总是问我还需要什么": ("closing", "less"),
            "别叫我主人": ("other", "less"), "以后不要叫我老板": ("other", "less"),
        }
        for text, dim in cases.items():
            found = preferences.detect_feedback(text)
            self.assertIsNotNone(found, text)
            self.assertTrue(found["explicit"], text)
            self.assertIn(dim, found["dims"], text)
        self.assertEqual(preferences.rule_text_from("别叫我主人", [("other", "less")]), "别叫我主人")
        keep = preferences.detect_feedback("保持这种风格")
        self.assertTrue(keep["keep_style"])
        again = preferences.detect_feedback("你怎么又这样")
        self.assertTrue(again["ambiguous"])
        self.assertFalse(again["explicit"])
        self.assertEqual(preferences.classify_scene("我今天太累了不想说话"), "emotional")

    def test_reply_features(self):
        features = dict(preferences.reply_features("这是一个非常棒的问题！下面我将从五个方面详细介绍……"))
        self.assertIn("praise", features)
        self.assertIn("opener", features)
        self.assertEqual(preferences.reply_features("可以实现，主要需要以下三个模块。"), [])


class ChatFlowChecks(Base):
    def test_A1_a_rating_with_reason_survives_a_restart(self):
        self.api.answers = ["这是一个非常棒的问题！下面我将从五个方面详细介绍……"]
        result = self.ask("怎么做一个待办事项应用？")
        feedback, _made = self.session.preferences.rate(result["reply_id"], -1, reason="太啰嗦了")
        self.assertEqual(feedback["rating"], -1)
        # "Restart": a brand-new session and store read the same folder.
        reopened = chat.ChatSession(self.db).preferences.store.feedback(reply_id=result["reply_id"])
        self.assertEqual(reopened["rating"], -1)
        self.assertEqual(reopened["reason"], "太啰嗦了")
        self.assertEqual(reopened["question"], "怎么做一个待办事项应用？")
        self.assertIn("非常棒的问题", reopened["reply"])
        self.assertEqual(reopened["model"], claude_client.OPUS)
        self.assertEqual(reopened["provider"], "anthropic")
        self.assertIn(reopened["scene"], preferences.SCENES)
        self.assertEqual(reopened["reply_id"], result["reply_id"])
        self.assertTrue((self.db.parent / "preferences" / "feedback.db").is_file())

    def test_A2_said_in_words_and_applied_to_the_next_related_reply(self):
        self.api.answers = ["好问题！东京今天晴。"]
        self.ask("东京今天天气怎么样？")
        self.ask("以后不要每次都夸我")
        self.assertIn("不要每次都夸我", self.active_texts())
        rule = next(rule for rule in self.store.rules() if rule["text"] == "不要每次都夸我")
        self.assertEqual(rule["source"], "user")
        self.assertEqual(rule["scenes"], ["all"])  # praise is not scene-dependent
        # The reply to the correction itself already knows about it...
        self.assertIn("不要每次都夸我", self.api.last_system)
        self.assertIn("Acknowledge it", self.api.last_system)
        # ...and so does the next answer in another scene.
        self.ask("python 里怎么读文件？")
        self.assertIn("不要每次都夸我", self.api.last_system)
        self.assertNotIn("Acknowledge it", self.api.last_system)

    def test_A3_an_ideal_answer_is_kept_and_found_for_similar_questions(self):
        self.api.answers = ["这是一个非常棒的问题！下面我将从五个方面详细介绍……"]
        self.ask("能不能做一个能语音控制的笔记应用？")
        self.ask("我觉得你的回答太繁琐了我不喜欢，我想简短一点比如：“可以实现，主要需要以下三个模块……”")
        examples = self.store.examples()
        self.assertEqual(len(examples), 1)
        self.assertEqual(examples[0]["kind"], "correction")
        self.assertEqual(examples[0]["ideal"], "可以实现，主要需要以下三个模块……")
        # The explicit part takes effect; what the contrast suggests becomes candidates.
        self.assertTrue(any("brief" in text for text in self.active_texts()))
        candidates = {rule["dimension"] for rule in self.store.rules() if rule["status"] == "candidate"}
        self.assertTrue({"praise", "opener"} <= candidates)
        self.ask("能不能做一个能语音控制的日程应用？")
        self.assertIn("可以实现，主要需要以下三个模块", self.api.last_system)
        self.assertIn("Avoid mechanical praise", self.api.last_system)  # tried (candidate), scene-bound

    def test_A4_rating_the_same_reply_again_replaces_it(self):
        result = self.ask("讲个笑话")
        engine = self.session.preferences
        for rating in (-1, -1, -1, 1, 1):
            engine.rate(result["reply_id"], rating)
        rows = self.store.feedback_list()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rating"], 1)
        evaluation = preferences.evaluate(self.store)
        self.assertEqual((evaluation["recent"]["likes"], evaluation["recent"]["dislikes"]), (1, 0))
        self.assertEqual(len(self.store.events(rows[0]["id"])), 5)  # the raw clicks are all kept
        engine.rate(result["reply_id"], 0)  # take it back
        self.assertEqual(engine.rating_of(result["reply_id"]), 0)
        self.assertEqual(preferences.evaluate(self.store)["recent"]["likes"], 0)

    def test_A5_different_scenes_get_different_rules(self):
        self.api.answers = ["一大段解释……"]
        self.ask("东京现在几点？")  # quick
        self.ask("不要解释这么多，直接告诉我结果")
        short = [rule for rule in self.store.rules() if rule["dimension"] == "verbosity"]
        self.assertEqual(short[0]["scenes"], ["quick"])
        self.ask("学数学的时候你要详细一点，分步骤讲")
        detailed = [rule for rule in self.store.rules()
                    if rule["direction"] == "more" and rule["dimension"] in ("verbosity", "steps")]
        self.assertTrue(detailed and all(rule["scenes"] == ["study"] for rule in detailed))
        self.ask("能给我讲讲导数的定义吗")
        study = self.api.last_system
        self.assertIn('Study', study)
        self.assertNotIn("不要解释这么多", study)
        self.assertIn("detailed", study)
        self.ask("纽约现在几点？")
        quick = self.api.last_system
        self.assertIn("不要解释这么多", quick)
        self.assertNotIn("分步骤讲", quick)

    def test_A6_another_model_reads_the_same_preferences(self):
        self.ask("你好")
        self.ask("以后说话自然一点，别太机械")
        self.assertEqual(self.api.payloads[-1]["model"], claude_client.OPUS)
        self.assertIn("别太机械", self.api.last_system)
        with self.assertRaises(ValueError):
            claude_client.save_main_choice(self.db.parent, "sonnet")
        self.ask("今天想吃什么好")
        self.assertEqual(self.api.payloads[-1]["model"], claude_client.OPUS)
        self.assertIn("别太机械", self.api.last_system)
        # The configured local fallback gets the very same text.
        engine = self.session.preferences
        turn = engine.prepare_turn("今天想吃什么好")
        captured = {}

        def ollama(request, timeout):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return io.BytesIO(json.dumps({"done": True, "message": {"content": json.dumps(
                {"answer": "a", "citations": [1], "grounded": True})}}).encode("utf-8"))

        with mock.patch.object(chat.local_llm._transport, "open", side_effect=ollama):
            chat._local_rag(chat.local_llm.Config("http://127.0.0.1:11434/api/chat", "test-model"), "q", [{"id": 1, "raw_text": "x", "created_at": "2026-10-17T00:00:00+00:00"}],
                           turn["context"])
        self.assertIn("别太机械", captured["body"]["messages"][0]["content"])
        self.assertEqual(len(self.store.rules()), 1)  # nothing was reset by switching

    def test_A7_a_deleted_rule_stops_at_once_even_when_cached(self):
        rule_id = self.store.add_rule("回答别用表情符号", "emoji", "less", ["all"], source="manual")
        engine = self.session.preferences
        self.assertIn("回答别用表情符号", engine.build_context("casual", "hi")[0])
        self.assertIn("回答别用表情符号", engine.build_context("casual", "hi")[0])  # cached
        self.store.delete_rule(rule_id)
        self.assertEqual(engine.build_context("casual", "hi")[0], "")
        self.ask("你好呀")
        self.assertNotIn("回答别用表情符号", self.api.last_system)
        # A second store instance (another thread / window) sees it too.
        other = preferences.Engine(self.db)
        self.assertEqual(other.build_context("casual", "hi")[0], "")

    def test_A8_a_broken_preference_database_does_not_stop_chatting(self):
        folder = self.db.parent / "preferences"
        folder.mkdir(exist_ok=True)
        (folder / "feedback.db").write_bytes(b"this is not a database" * 200)
        session = chat.ChatSession(self.db)
        with mock.patch.object(session, "_key", return_value="k"), \
                mock.patch.object(claude_client.urllib.request, "urlopen", side_effect=self.api):
            result = session.ask("以后不要每次都夸我")
            self.assertEqual(result["answer"], "好的。")
            self.assertIsNotNone(result["reply_id"])
            result = session.ask("今天星期几？")
        self.assertEqual(result["answer"], "好的。")
        self.assertIsNotNone(session.preferences.last_error)
        self.assertIn("error", session.preferences.summary())
        with self.assertRaises(preferences.PreferenceError):
            session.preferences.rate(result["reply_id"], 1)
        # The damaged file is left alone (no silent reset).
        self.assertEqual((folder / "feedback.db").read_bytes()[:10], b"this is no")

    def test_A9_without_the_api_feedback_is_still_saved(self):
        self.ask("给我讲讲黑洞")
        result = self.ask("以后不要解释这么多", key="")  # no key at all
        self.assertIn(chat.OFFLINE_FEEDBACK_NOTE, result["answer"])
        self.assertTrue(any("不要解释这么多" in text for text in self.active_texts()))

        def offline(request, timeout):
            raise urllib.error.URLError("offline")

        result = self.ask("你这样说话太机械了", api=offline)  # network down
        self.assertIn(chat.OFFLINE_FEEDBACK_NOTE, result["answer"])
        feedback = self.store.feedback_list()
        self.assertGreaterEqual(len(feedback), 2)
        # Reflection can't run now: the feedback stays queued for later.
        pending_before = self.store.pending_count()

        def failing(_system, _prompt):
            raise claude_client.ClaudeUnavailable("offline", fallback_allowed=True)

        self.assertEqual(self.session.preferences.reflect_pending(failing), 0)
        self.assertEqual(self.store.pending_count(), pending_before)

    def test_A10_existing_data_is_untouched(self):
        storage.save_memory("今天学习了数学", self.db, parsed={
            "should_save": True, "category": "study", "memory_type": "event", "metadata": {},
            "summary": "", "tags": [], "reason": "", "source": "rule", "fallback_reason": "",
            "suggested_replaces_id": None})
        self.ask("你好")
        with closing(sqlite3.connect(str(self.db))) as db:
            before = {table: db.execute("SELECT * FROM %s ORDER BY id" % table).fetchall()
                      for table in ("memories", "conversation_messages", "sent_messages")}
        reply_id = conversation_log.latest_assistant(self.db)["id"]
        self.session.preferences.rate(reply_id, -1, reason="太长了", ideal="你好！")
        self.store.add_rule("少用列表", "structure", "less", ["all"], source="manual")
        self.store.export()
        with closing(sqlite3.connect(str(self.db))) as db:
            after = {table: db.execute("SELECT * FROM %s ORDER BY id" % table).fetchall()
                     for table in ("memories", "conversation_messages", "sent_messages")}
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(before, after)
        self.assertFalse({"feedback", "rules", "examples"} & tables)  # kept in their own file


    def test_one_off_requests_in_chat_only_build_up_slowly_and_others_talk_is_ignored(self):
        self.ask("用列表列出东京的景点")
        self.assertEqual(self.active_texts(), [])  # this answer only: no lasting rule
        self.assertEqual(self.store.feedback_list(), [])  # and it is not feedback on a reply
        guess = [rule for rule in self.store.rules() if rule["dimension"] == "structure"]
        self.assertEqual(len(guess), 1)
        self.assertEqual((guess[0]["source"], guess[0]["status"]), ("inferred", "candidate"))
        self.assertNotIn(preferences.ALL, guess[0]["scenes"])
        self.assertNotIn("用分点", self.api.last_system)  # a weak guess is not even tried yet
        for _ in range(3):  # asked the same way again and again: it becomes a habit for that scene
            self.ask("用列表列出大阪的景点")
        rule = self.store.rule(guess[0]["id"])
        self.assertEqual(rule["status"], "active")
        self.ask("用列表列出京都的景点")
        self.assertIn(rule["text"], self.api.last_system)
        # Talking about someone else never becomes a rule about MYN.
        before = len(self.store.rules())
        self.ask("我同事说话太啰嗦了")
        self.ask("他说话太机械了")
        self.assertEqual(len(self.store.rules()), before)

    def test_how_to_address_the_user_is_kept_in_their_words(self):
        self.ask("你好")
        self.ask("别叫我主人")
        self.ask("以后不要叫我老板")
        self.ask("今天天气怎么样？")
        self.assertIn("别叫我主人", self.api.last_system)
        self.assertIn("不要叫我老板", self.api.last_system)
        self.assertEqual(self.store.conflicts(), [])


class LearningChecks(Base):
    def test_one_downvote_without_words_changes_nothing_everywhere(self):
        self.api.answers = ["好问题！" + "很长的解释。" * 100]
        result = self.ask("东京现在几点？")
        self.session.preferences.rate(result["reply_id"], -1)
        rules = self.store.rules()
        self.assertTrue(rules)
        self.assertTrue(all(rule["status"] == "candidate" and rule["scenes"] == ["quick"] for rule in rules))
        self.assertEqual(self.session.preferences.build_context("quick", "x")[0], "")  # not even tried yet
        self.assertEqual(self.session.preferences.build_context("study", "x")[0], "")

    def test_repeated_signals_promote_a_candidate_and_bad_ones_retire_it(self):
        store = self.store
        rule_id = store.add_rule("少用小标题", "structure", "less", ["coding"], source="inferred", confidence=0.4)
        self.assertEqual(store.rule(rule_id)["status"], "candidate")
        for _ in range(3):
            store.add_rule("少用小标题", "structure", "less", ["coding"], source="inferred", confidence=0.4)
        promoted = store.rule(rule_id)
        self.assertEqual(promoted["status"], "active")
        self.assertGreaterEqual(promoted["support"], 2)
        other = store.add_rule("多用比喻", "other", "more", ["casual"], source="inferred", confidence=0.3)
        store.adjust_rule(other, False)
        self.assertEqual(store.rule(other)["status"], "retired")

    def test_similar_rules_merge_and_the_users_words_win(self):
        store = self.store
        guess = store.add_rule("不要无意义的开场白", "opener", "less", ["casual"], source="inferred")
        same = store.add_rule("别来那么多开场白，直接说", "opener", "less", ["casual"], source="user")
        self.assertEqual(guess, same)
        rule = store.rule(same)
        self.assertEqual((rule["source"], rule["status"], rule["text"]), ("user", "active", "别来那么多开场白，直接说"))

    def test_conflicts_supersede_and_can_be_undone(self):
        store = self.store
        short = store.add_rule("回答简短", "verbosity", "less", ["casual"], source="user")
        long_id = store.add_rule("回答详细一些", "verbosity", "more", ["casual"], source="user")
        self.assertEqual(store.rule(short)["status"], "paused")
        self.assertEqual(store.rule(long_id)["status"], "active")
        # Different scopes are not a conflict: the specific scene wins there.
        store.add_rule("回答简短", "verbosity", "less", ["all"], source="manual")
        self.assertEqual(store.conflicts(), [])
        context = self.session.preferences.build_context("casual", "")[0]
        self.assertIn("回答详细一些", context)
        self.assertNotIn("回答简短", context)
        self.assertIn("回答简短", self.session.preferences.build_context("coding", "")[0])
        # Both specific and opposite (made by hand): reported.
        store.update_rule(short, status="active")
        self.assertEqual(len(store.conflicts()), 1)
        # Undo the edit from the learning history.
        event = next(item for item in store.history() if item["rule_id"] == short and item["action"] == "edit")
        self.assertTrue(store.undo_history(event["id"]))
        self.assertEqual(store.rule(short)["status"], "paused")

    def test_learning_and_applying_preferences_cannot_be_disabled(self):
        with self.assertRaises(ValueError):
            self.store.set_setting("learning_paused", True)
        self.ask("你好")
        self.ask("以后不要每次都夸我")
        self.assertTrue(self.store.rules())
        self.assertEqual(len(self.store.feedback_list()), 1)
        with self.assertRaises(ValueError):
            self.store.set_setting("apply_enabled", False)
        self.store.add_rule("少用列表", "structure", "less", ["all"], source="manual")
        self.ask("随便聊聊")
        self.assertIn("少用列表", self.api.last_system)

    def test_unclear_feedback_asks_instead_of_making_a_rule(self):
        self.ask("你好")
        self.ask("你这样回答不太好")
        self.assertEqual(self.store.rules(), [])
        self.assertIn("Ask one short question", self.api.last_system)

    def test_revoking_withdraws_what_was_learned_from_it(self):
        self.api.answers = ["好问题！下面我将详细介绍……"]
        result = self.ask("怎么学英语？")
        engine = self.session.preferences
        feedback, _ = engine.rate(result["reply_id"], -1, ideal="多听多说，每天坚持。")
        self.assertTrue(self.store.examples())
        self.assertTrue(self.store.rules())
        self.store.revoke_feedback(feedback["id"])
        self.assertEqual(self.store.examples(), [])
        self.assertTrue(all(rule["status"] == "retired" for rule in self.store.rules()))
        self.store.delete_feedback(feedback["id"])
        self.assertEqual(self.store.feedback_list(), [])
        self.assertEqual(self.store.events(feedback["id"]), [])

    def test_reflection_with_the_model(self):
        self.api.answers = ["这是一个很好的问题！答案是 4。"]
        result = self.ask("2+2 等于几？")
        self.session.preferences.rate(result["reply_id"], -1, reason="别这样")
        answer = json.dumps({"about": "style", "style_issues": ["开头夸奖多余"], "keep": [],
                             "violated_rule_ids": [999], "unhelpful_rule_ids": [], "better": "直接说 4。",
                             "candidate_rules": [{"text": "简单问题直接给答案", "dimension": "directness",
                                                  "direction": "more", "confidence": 0.9}]}, ensure_ascii=False)
        prompts = []

        def ask(system, prompt):
            prompts.append(json.loads(prompt))
            return answer

        self.assertEqual(self.session.preferences.reflect_pending(ask), 1)
        self.assertEqual(prompts[0]["rating"], -1)
        self.assertIn("4", prompts[0]["myn_reply"])
        feedback = self.store.feedback(reply_id=result["reply_id"])
        self.assertEqual(feedback["analysis_status"], "done")
        analysis = json.loads(feedback["analysis"])
        self.assertEqual(analysis["violated_rule_ids"], [])  # unknown ids are dropped
        rule = next(rule for rule in self.store.rules() if rule["text"] == "简单问题直接给答案")
        self.assertEqual((rule["status"], rule["scenes"]), ("candidate", ["quick"]))
        self.assertLessEqual(rule["confidence"], 0.6)
        self.assertEqual(self.store.pending_count(), 0)

    def test_reflection_about_facts_makes_no_style_rule_and_bad_output_is_retried(self):
        result = self.ask("日本首都是哪里？")
        self.session.preferences.rate(result["reply_id"], -1, reason="不对")
        self.assertEqual(self.session.preferences.reflect_pending(lambda s, p: "not json"), 0)
        self.assertEqual(self.store.pending_count(), 1)
        with self.store._connect() as db:
            db.execute("UPDATE feedback SET next_attempt_at=NULL")
            db.commit()
        content = json.dumps({"about": "content", "style_issues": [], "keep": [], "violated_rule_ids": [],
                              "unhelpful_rule_ids": [], "better": "", "candidate_rules": [
                                  {"text": "多附和用户", "dimension": "other", "direction": "more"}]})
        self.assertEqual(self.session.preferences.reflect_pending(lambda s, p: content), 1)
        self.assertEqual(self.store.rules(), [])

    def test_keys_never_become_examples(self):
        result = self.ask("你好")
        self.session.preferences.rate(result["reply_id"], -1, ideal="我的 api key 是 sk-abcdefghijklmnop123")
        self.assertEqual(self.store.examples(), [])
        feedback = self.store.feedback(reply_id=result["reply_id"])
        self.assertNotIn("sk-abcdefghijklmnop123", feedback["ideal"])
        files = "".join(path.read_text(encoding="utf-8") for path in (self.db.parent / "preferences").glob("*.json*"))
        self.assertNotIn("sk-abcdefghijklmnop123", files)


class StorageChecks(Base):
    def test_readable_copies_export_import_and_backups(self):
        store = self.store
        self.ask("你好")
        self.ask("以后不要每次都夸我")
        reply_id = conversation_log.latest_assistant(self.db)["id"]
        self.session.preferences.rate(reply_id, 1)
        folder = self.db.parent / "preferences"
        profile = json.loads((folder / "style_profile.json").read_text(encoding="utf-8"))
        self.assertEqual(profile["schema_version"], preferences.SCHEMA_VERSION)
        self.assertIn("不要每次都夸我", profile["scenes"]["coding"]["rules"])
        self.assertTrue((folder / "preferred_examples.jsonl").read_text(encoding="utf-8").strip())
        history = [json.loads(line) for line in
                   (folder / "style_history.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(history[0]["action"], "create")
        exported = store.export()
        self.assertTrue(exported.parent.name == "exports" and exported.is_file())
        # Importing into the same store adds nothing twice.
        self.assertEqual(store.import_file(exported), {"rules": 0, "feedback": 0, "examples": 0})
        # A fresh install gets everything back.
        fresh_db = self.root / "other" / "jarvis.db"
        fresh_db.parent.mkdir()
        fresh = preferences.Engine(fresh_db).store
        added = fresh.import_file(exported)
        self.assertEqual(added["rules"], 1)
        self.assertEqual(added["feedback"], len(store.feedback_list()))
        self.assertEqual(added["examples"], 1)
        self.assertEqual(fresh.import_file(exported), {"rules": 0, "feedback": 0, "examples": 0})
        self.assertTrue(any(path.name.startswith("feedback-pre-import-") for path in fresh.list_backups()))
        bad = self.root / "bad.json"
        bad.write_text(json.dumps({"format": "something-else"}), encoding="utf-8")
        with self.assertRaises(ValueError):
            fresh.import_file(bad)
        bad.write_text(json.dumps({"format": preferences.FORMAT, "schema_version": 1,
                                   "rules": [{"uid": "x", "text": "", "source": "user", "status": "active"}]}),
                       encoding="utf-8")
        with self.assertRaises(ValueError):
            fresh.import_file(bad)
        self.assertEqual(len(fresh.rules()), 1)  # a rejected import changes nothing

    def test_backup_restore_keeps_the_replaced_file(self):
        store = self.store
        store.add_rule("少用列表", "structure", "less", ["all"], source="manual")
        backup = store.backup("manual")
        self.assertTrue(backup.is_file())
        self.assertIsNone(store.ensure_daily_backup() and None)
        store.add_rule("不用表情", "emoji", "less", ["all"], source="manual")
        kept = store.restore(backup)
        self.assertTrue(kept.is_file())
        self.assertEqual([rule["text"] for rule in store.rules()], ["少用列表"])
        with self.assertRaises(ValueError):
            store.restore(self.db)

    def test_schema_version_from_the_future_is_refused_untouched(self):
        store = self.store
        store.add_rule("少用列表", "structure", "less", ["all"], source="manual")
        with closing(sqlite3.connect(str(store.path))) as db:
            with db:
                db.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
        later = preferences.Store(store.folder)
        with self.assertRaises(preferences.PreferenceError):
            later.rules()

    def test_evaluation_reports_trend_issues_and_rule_effects(self):
        store = self.store
        rule_id = store.add_rule("回答简短", "verbosity", "less", ["all"], source="user")
        engine = self.session.preferences
        for index in range(6):
            self.api.answers = ["好问题！" + "长" * 600]
            result = self.ask("问题 %d 是什么？" % index)
            engine.rate(result["reply_id"], -1, reason="太长了")
        evaluation = preferences.evaluate(store)
        self.assertEqual(evaluation["recent"]["dislikes"], 6)
        self.assertEqual(evaluation["recurring_issues"][0]["dimension"], "verbosity")
        effect = next(item for item in evaluation["rules"] if item["id"] == rule_id)
        self.assertEqual(effect["applied"], 6)
        self.assertEqual(effect["verdict"], 'No positive effect')
        self.assertEqual(len(evaluation["weeks"]), 6)

    def test_myn_can_say_what_it_learned(self):
        self.store.add_rule("少用列表", "structure", "less", ["coding"], source="manual")
        described = self.session.preferences.describe()
        self.assertEqual(described["rules"][0]["text"], "少用列表")
        self.assertEqual(described["rules"][0]["source"], 'Added manually by you')
        self.assertIn("communication_preferences", [tool["name"] for tool in [chat.PREFERENCES_TOOL]])


if __name__ == "__main__":
    unittest.main()
