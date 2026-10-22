"""Availability and alarms: temporary data, simulated model/audio only."""
import json
import queue
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src/core")]
import chat
import claude_client
import data_location
import evolution
import interaction
import myn_gui
import tts


class Temp(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="myn-interaction-")
        self.addCleanup(temp.cleanup)
        self.config = Path(temp.name)

    def point(self, title="语音改进"):
        point, _ = evolution.add(self.config, title, request="请改进")
        return evolution.update(self.config, point["id"], status="proposed",
                                plan={"summary": "方案摘要", "plan": "需要用户看过的详细步骤"})


class State(Temp):
    def test_sleep_persists_and_only_alarms_and_direct_replies_bypass(self):
        interaction.observe(self.config, "我要睡觉了")
        self.assertEqual(interaction.load(self.config)["mode"], "sleep")
        for purpose in ("proactive", "scheduled"):
            self.assertFalse(interaction.allowed(self.config, purpose))
        self.assertTrue(interaction.allowed(self.config, "alarm"))
        # The good-night answer and later questions the user addresses to MYN are spoken.
        self.assertTrue(interaction.allowed(self.config, "reply"))
        interaction.observe(self.config, "为什么你说话没有声音？", semantic=True)
        self.assertTrue(interaction.allowed(self.config, "reply"))
        self.assertFalse(interaction.allowed(self.config, "proactive"))
        self.assertFalse(interaction.idle(self.config, now=time.time() + 86400))
        interaction.observe(self.config, "我醒了")
        self.assertTrue(interaction.allowed(self.config))
        self.assertFalse(interaction.idle(self.config))
        self.assertTrue(interaction.idle(self.config, now=time.time() + 100))

    def test_study_is_sticky_and_answers_questions(self):
        interaction.observe(self.config, "我要学习了")
        for text in ("好的", "懂了", "下一题", "谢谢", "这道题怎么解"):
            interaction.observe(self.config, text)
            self.assertEqual(interaction.load(self.config)["mode"], "study")
        self.assertTrue(interaction.allowed(self.config, "reply"))
        self.assertFalse(interaction.allowed(self.config, "proactive"))
        self.assertFalse(interaction.allowed(self.config, "scheduled"))
        self.assertTrue(interaction.allowed(self.config, "alarm"))
        interaction.observe(self.config, "我学完了")
        self.assertEqual(interaction.load(self.config)["mode"], "idle")

    def test_history_quotes_and_hypotheticals_do_not_mute(self):
        for text in ("昨晚我要睡觉了你突然说话", "如果我说我要睡觉了", "不要在我睡觉时说话",
                     "我还不睡觉", "她说我要睡觉了", "“我要睡觉了”这句话怎么翻译", "学习是什么意思"):
            interaction.observe(self.config, text)
            self.assertEqual(interaction.load(self.config)["mode"], "idle", text)
        interaction.observe(self.config, "我要睡觉了，明早八点叫我")
        self.assertEqual(interaction.load(self.config)["mode"], "sleep")

    def test_semantic_decisions_need_evidence_and_newer_input_wins(self):
        context = interaction.observe(self.config, "我要专心准备考试，先别打扰我")
        args = dict(mode="study", evidence="专心准备考试", certainty="clear")
        self.assertTrue(interaction.decide(self.config, context, args, "我要专心准备考试，先别打扰我"))
        self.assertFalse(interaction.decide(self.config, context, dict(args, evidence="假证据"), "我要学习"))
        self.assertFalse(interaction.decide(self.config, context, dict(args, certainty="uncertain"), "专心准备考试"))
        interaction.observe(self.config, "我要睡觉了")
        self.assertFalse(interaction.decide(self.config, context, args, "我要专心准备考试，先别打扰我"))
        self.assertEqual(interaction.load(self.config)["mode"], "sleep")

    def test_acknowledgement_and_quoted_instructions_cannot_reset_mode_via_tool(self):
        interaction.observe(self.config, "我要学习了")
        for text in ("好的", "下一题", "如果我说我有空了", "翻译“我有空了”"):
            context = interaction.observe(self.config, text)
            self.assertFalse(interaction.decide(self.config, context,
                                                dict(mode="idle", certainty="clear", evidence=text), text))
        self.assertEqual(interaction.load(self.config)["mode"], "study")

    def test_deferred_invitation_survives_reload_but_unrelated_ok_does_not_show_it(self):
        interaction.offer(self.config, self.point())
        interaction.observe(self.config, "等会再给我看")
        interaction.response(self.config, "等会再给我看")
        self.assertIsNotNone(interaction.current_invitation(self.config))
        interaction.observe(self.config, "这道题怎么做")
        interaction.observe(self.config, "好的")
        self.assertIsNone(interaction.response(self.config, "好的"))
        interaction.observe(self.config, "我有空了", now=0)
        self.assertFalse(interaction.load(self.config)["deferred"])
        self.assertFalse(interaction.offer(self.config, self.point("新方案")))
        self.assertIsNotNone(interaction.current_invitation(self.config))
        self.assertIn("evolution_review", interaction.response(self.config, "现在给我看"))

    def test_corrupt_state_is_quiet_but_alarm_and_explicit_recovery_work(self):
        (self.config / interaction.FILE).write_text("broken", encoding="utf-8")
        self.assertFalse(interaction.allowed(self.config, "proactive"))
        self.assertTrue(interaction.allowed(self.config, "alarm"))
        interaction.observe(self.config, "我有空了")
        self.assertTrue(interaction.allowed(self.config, "proactive"))

    def test_invite_then_later_then_show_never_adopts(self):
        point = self.point()
        self.assertTrue(interaction.offer(self.config, point))
        self.assertNotEqual(evolution.get(self.config, point["id"])["announced"], "proposed")
        self.assertFalse(interaction.offer(self.config, self.point("另一个方案")))
        result = interaction.response(self.config, "等会再给我看")
        self.assertIn('keep it for later', result["answer"])
        self.assertTrue(interaction.load(self.config)["deferred"])
        self.assertFalse(interaction.offer(self.config, point))
        result = interaction.response(self.config, "好的我看看")
        self.assertIn("详细步骤", result["answer"])
        saved = evolution.get(self.config, point["id"])
        self.assertEqual(saved["status"], "proposed")
        self.assertEqual(saved["announced"], "proposed")
        self.assertIsNone(interaction.response(self.config, "好的"))

    def test_stale_offers_do_not_revive_completed_or_adjusted_points(self):
        for status in ("completed", "new", "rejected", "adopted"):
            point = self.point(status)
            interaction.offer(self.config, point)
            evolution.update(self.config, point["id"], status=status)
            self.assertIsNone(interaction.response(self.config, "好的我看看"))
            self.assertIsNone(interaction.load(self.config)["invitation"])

    def test_manual_show_in_study_does_not_resume_interruptions(self):
        interaction.offer(self.config, self.point())
        interaction.observe(self.config, "我要学习了")
        self.assertIn("方案", interaction.response(self.config, "好的我看看")["answer"])
        self.assertEqual(interaction.load(self.config)["mode"], "study")


class Chat(Temp):
    def setUp(self):
        super().setUp()
        self.session = chat.ChatSession(self.config / "jarvis.db")
        self.config = data_location.config_for_db(self.session.db_path)

    def test_offline_scene_and_invitation_replies_are_archived(self):
        with mock.patch.object(self.session, "_key", side_effect=AssertionError("no API required")):
            result = self.session.ask("我要睡觉了")
            self.assertIn('alarms', result["answer"])
            self.assertIsNotNone(result["reply_id"])
            self.assertEqual(interaction.load(self.config)["mode"], "sleep")
            self.session.ask("我醒了")
            interaction.offer(self.config, self.point())
            self.session.ask("等会再给我看")
            result = self.session.ask("好的我看看")
            self.assertIn("详细步骤", result["answer"])
            self.assertIn("evolution_review", result)

    def test_semantic_scene_is_required_before_final_streaming(self):
        events = []
        self.session.interaction_notify = lambda: events.append(interaction.load(self.config)["mode"])
        def answer(*args, **kwargs):
            self.assertNotIn("interaction_scene", [t["name"] for t in kwargs["read_tools"]])
            kwargs["on_text"]("好的")
            return "好的"
        with mock.patch.object(self.session, "_key", return_value="synthetic"), \
                mock.patch.object(interaction, "classify", return_value=dict(
                    mode="study", certainty="clear", scope="current", evidence="专心准备考试",
                    reason="当前在准备考试")), \
                mock.patch.object(claude_client, "coordinated_reply", side_effect=answer):
            self.session.ask("我要专心准备考试，先别打扰我", on_text=lambda _: events.append("answer"))
        self.assertEqual(events, ["study", "answer"])

    def test_scheduled_prompt_cannot_change_user_scene(self):
        interaction.observe(self.config, "我要睡觉了")
        def answer(*args, **kwargs):
            self.assertNotIn("interaction_scene", [t["name"] for t in kwargs["read_tools"]])
            return "定时任务结果"
        with mock.patch.object(self.session, "_key", return_value="synthetic"), \
                mock.patch.object(claude_client, "coordinated_reply", side_effect=answer):
            self.session.ask_scheduled(dict(name="查询", text="我醒了"))
        self.assertEqual(interaction.load(self.config)["mode"], "sleep")


class Window(Temp):
    def setUp(self):
        super().setUp()
        self.win = myn_gui.MYNWindow.__new__(myn_gui.MYNWindow)
        self.win._usage_config = lambda: self.config
        self.win.root = mock.Mock()
        self.win.events = queue.Queue()
        self.win.session = mock.Mock(db_path=self.config / "jarvis.db")
        self.win.busy = self.win.conversation_open = self.win.draft_active = False
        self.win._speaking = lambda: False
        self.win.append = mock.Mock()
        self.win._evolution_changed = mock.Mock()
        self.win.tts_settings = tts.TTSSettings()
        self.win.tts_request_id = 10
        self.win.reply_silenced = False
        self.win.tts_player = mock.Mock()
        self.win.recent_speech = ()

    def test_evolution_never_interrupts_sleep_study_or_current_reading(self):
        point = self.point()
        self.win.speak = mock.Mock()
        for phrase in ("我要睡觉了", "我要学习了"):
            interaction.observe(self.config, phrase, now=0)
            self.win._evolution_announce(self.config)
        self.win.append.assert_not_called()
        interaction.observe(self.config, "我有空了", now=0)
        self.win.reader_open = True
        self.win._evolution_announce(self.config)
        self.win.append.assert_not_called()
        self.win.reader_open = False
        self.win._evolution_announce(self.config)
        self.assertEqual(self.win.append.call_count, 1)
        self.assertFalse(self.win.conversation_open)
        self.assertIn('good time to review', self.win.append.call_args[0][1])
        self.assertNotIn("详细步骤", self.win.append.call_args[0][1])
        self.assertNotEqual(evolution.get(self.config, point["id"])["announced"], "proposed")
        self.win._evolution_said_at = 0
        self.win.conversation_open = False
        self.win._evolution_announce(self.config)
        self.assertEqual(self.win.append.call_count, 1)

    def test_silent_speech_does_not_start_synthesis_or_stop_an_alarm(self):
        interaction.observe(self.config, "我要睡觉了")
        self.win.stop_speech = mock.Mock()
        with mock.patch("myn_gui.threading.Thread") as worker:
            self.win.speak("后台完成", purpose="proactive")
            self.win._stream_speak(["定时任务文字"], purpose="scheduled")
            worker.assert_not_called()
            self.win.stop_speech.assert_not_called()
            self.win.speak("起床啦", purpose="alarm")
            worker.assert_called_once()
            self.assertEqual(self.win.tts_purpose, "alarm")
            self.assertTrue(self.win._speech_allowed(self.win.tts_purpose))

    def test_good_night_reply_and_later_direct_question_are_spoken_in_sleep(self):
        interaction.observe(self.config, "我要睡觉了")
        self.win.stop_speech = mock.Mock()
        self.win.speech_queue = None
        self.win.scheduled_running = None
        with mock.patch("myn_gui.threading.Thread") as worker:
            self.win.speak("晚安")
            self.assertEqual(worker.call_count, 1)
            self.win.speech_queue = None
            interaction.observe(self.config, "为什么你说话没有声音？", semantic=True)
            self.win._stream_speak(["因为刚才处于睡眠静音。"])
            self.assertEqual(worker.call_count, 2)
            self.assertTrue(self.win._speech_allowed(self.win.tts_purpose))
            self.win.speech_queue = None
            self.win.speak("后台任务完成了", purpose="proactive")
            self.assertEqual(worker.call_count, 2)

    def test_bedtime_briefing_follows_spoken_good_night_without_cutting_it(self):
        context = interaction.observe(self.config, "我要睡觉了")
        briefing = dict(text="睡前汇报：明天没有日程。", date="2099-01-01",
                        revision=context["revision"], created=time.time())
        self.win.queued_question = None
        self.win.tts_purpose = "reply"
        pending = queue.Queue()
        self.win.speech_queue = pending
        self.win.speech_stream = None
        text = self.win._claim_bedtime_briefing(briefing)
        self.assertEqual(text, briefing["text"])
        self.assertIsNone(self.win._claim_bedtime_briefing(briefing))  # once only
        self.assertTrue(self.win._finish_stream_speech(text))
        queued = []
        while not pending.empty():
            queued.append(pending.get_nowait())
        self.assertIsNone(queued[-1])
        self.assertIn("睡前汇报", "".join(segment for segment in queued[:-1]))

    def test_entering_sleep_invalidates_inflight_background_audio(self):
        interaction.observe(self.config, "我要睡觉了")
        self.win.tts_purpose = "proactive"
        self.win.speech_queue = queue.Queue()
        pending = self.win.speech_queue
        with mock.patch.object(tts, "cancel_local"):
            self.win._interaction_changed()
        self.assertEqual(self.win.tts_request_id, 11)
        self.assertIsNone(pending.get_nowait())
        self.win.tts_player.stop.assert_called_once()
        self.assertFalse(self.win.conversation_open)

    def test_alarm_task_uses_exempt_path_without_waking_user(self):
        interaction.observe(self.config, "我要睡觉了")
        import alarm_ringing
        alarm_ringing.activate(self.config, "schedule:alarm", "test", "早上八点了，该起床了")
        self.win.speak = mock.Mock()
        self.win._note_task = mock.Mock()
        self.win.task_queue = (dict(id="alarm", kind="say", text="早上八点了，该起床了"),)
        # Deterministic worker completion before the temporary directory is removed.
        with mock.patch("gui_timers.threading.Thread") as thread:
            thread.side_effect = lambda target, **kw: mock.Mock(start=target)
            self.win._next_task()
        self.win.speak.assert_called_once_with("早上八点了，该起床了", purpose="alarm")
        self.assertEqual(interaction.load(self.config)["mode"], "sleep")

    def test_late_audio_is_discarded_but_alarm_audio_plays(self):
        interaction.observe(self.config, "我要睡觉了")
        self.win.tts_purpose = "proactive"
        self.win.events.put(("tts_chunk", 10, b"background"))
        self.win.process_events()
        self.win.tts_player.enqueue.assert_not_called()
        self.win.tts_purpose = "alarm"
        self.win.events.put(("tts_chunk", 10, b"alarm"))
        self.win.process_events()
        self.win.tts_player.enqueue.assert_called_once_with(b"alarm", self.win.tts_settings.volume)

    def test_sleep_command_during_old_reply_is_immediate_and_old_scene_cannot_override(self):
        self.win.session = chat.ChatSession(self.config / "jarvis.db")
        context = interaction.observe(self.config, "讲解这道题")
        self.win.busy = True
        with mock.patch.object(tts, "cancel_local"):
            self.win._ask_after_reply("我要睡觉了")
        self.assertEqual(interaction.load(self.config)["mode"], "sleep")
        self.assertIsNone(self.win.queued_question)
        self.assertFalse(interaction.decide(self.config, context,
                                            dict(mode="study", evidence="讲解这道题", certainty="clear"),
                                            "讲解这道题"))

    def test_background_completion_does_not_cover_a_lesson(self):
        interaction.observe(self.config, "我要学习了")
        self.win.speak = mock.Mock()
        self.win._background_notice("很长的后台结果" * 100, "后台任务完成了")
        self.win.append.assert_not_called()
        self.win.speak.assert_not_called()
        self.assertTrue(self.win.background_notice_waiting)
        interaction.observe(self.config, "我学完了", now=0)
        self.win._background_notice_ready()
        self.assertEqual(self.win.append.call_count, 1)
        self.assertLess(len(self.win.append.call_args[0][1]), 100)

    def test_completed_adopted_build_waits_until_user_is_free(self):
        point, _ = evolution.add(self.config, "字幕", adopted_plan="调整字幕")
        evolution.update(self.config, point["id"], status="built", work="isolated-work")
        self.win._evolution_update = mock.Mock()
        self.win._evolution_busy = lambda: False
        for phrase in ("我要睡觉了", "我要学习了"):
            interaction.observe(self.config, phrase, now=0)
            self.win._evolution_auto_update(self.config)
        self.win._evolution_update.assert_not_called()
        interaction.observe(self.config, "我有空了", now=0)
        self.win._evolution_auto_update(self.config)
        self.win._evolution_update.assert_called_once_with(point["id"])


if __name__ == "__main__":
    unittest.main()
