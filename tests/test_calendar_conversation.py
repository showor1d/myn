"""Context-driven calendar UI, spoken plan follow-ups and quiet bedtime handoff."""
import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src/core")]
import agenda
import agenda_scene
import bedtime
import chat
import claude_client
import interaction
import local_actions
from gui_agenda import AgendaMixin


def verdict(mode, text, **extra):
    return dict(mode=mode, scope="current", certainty="clear", evidence=text,
                reason="当前对话与用户意图一致", **extra)


class CalendarConversation(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="myn-calendar-conversation-")
        self.addCleanup(folder.cleanup)
        self.config = Path(folder.name)

    def sleep(self):
        state = interaction.observe(self.config, "今晚先聊到这里，我撑不住了", semantic=True)
        interaction.apply_classification(self.config, state,
                                         verdict("sleep", "我撑不住了"), "我撑不住了")
        return interaction.load(self.config)["revision"]

    def test_scene_close_needs_current_evidence_and_clear_intent(self):
        context = dict(open=True, revision=3)
        close = dict(action="close", certainty="clear", evidence="聊聊电影", reason="已转到电影话题")
        self.assertTrue(agenda_scene.evaluate(context, close, "看完了，聊聊电影")["close"])
        self.assertFalse(agenda_scene.evaluate(context, close, "周三的日程再说一下")["close"])
        self.assertFalse(agenda_scene.evaluate(context, dict(close, certainty="uncertain"), "聊聊电影")["close"])
        self.assertFalse(agenda_scene.evaluate(context, dict(close, action="keep"), "聊聊电影")["close"])

    def test_old_scene_result_cannot_close_reopened_or_new_turn_view(self):
        win = AgendaMixin()
        win._agenda_revision = 4
        win._close_agenda_display = mock.Mock()
        win._usage_config = lambda: self.config
        win._apply_agenda_scene(dict(close=True, revision=3))
        win._close_agenda_display.assert_not_called()
        old = interaction.observe(self.config, "聊别的")
        interaction.observe(self.config, "还是先看周三")
        win._apply_agenda_scene(dict(close=True, revision=4, input_revision=old["revision"]))
        win._close_agenda_display.assert_not_called()
        win._apply_agenda_scene(dict(close=True, revision=4))
        win._close_agenda_display.assert_called_once()

    def test_bedtime_real_next_day_once_and_new_input_revokes_speech(self):
        revision = self.sleep()
        agenda.add(self.config, title="复诊", start="2026-10-21 09:00", location="医院")
        agenda.add(self.config, title="每周运动", start="2026-10-14 18:00", repeat="weekly")
        brief = bedtime.prepare(self.config, revision, datetime(2026, 10, 20, 23, tzinfo=agenda.TOKYO))
        self.assertIn("复诊", brief["text"])
        self.assertIn("每周运动", brief["text"])
        self.assertEqual(brief["date"], "2026-10-21")
        token = bedtime.claim(self.config, brief)
        self.assertTrue(interaction.allowed(self.config, token))
        self.assertTrue(interaction.allowed(self.config, "reply"))  # direct answers are not muted by sleep
        self.assertFalse(interaction.allowed(self.config, "proactive"))
        self.assertIsNone(bedtime.claim(self.config, brief))
        self.assertIsNone(bedtime.prepare(self.config, revision, datetime(2026, 10, 21, 1, tzinfo=agenda.TOKYO)))
        interaction.observe(self.config, "先别说了", semantic=True)
        self.assertFalse(interaction.allowed(self.config, token))
        self.assertTrue(interaction.allowed(self.config, "alarm"))

    def test_after_midnight_upcoming_day_empty_day_nap_and_next_night(self):
        revision = self.sleep()
        brief = bedtime.prepare(self.config, revision, datetime(2026, 10, 21, 1, tzinfo=agenda.TOKYO))
        self.assertEqual(brief["date"], "2026-10-21")
        self.assertIn('no events are recorded for After you wake up today', brief["text"])
        bedtime.claim(self.config, brief)
        self.assertIsNone(bedtime.prepare(self.config, revision, datetime(2026, 10, 21, 14, tzinfo=agenda.TOKYO)))
        next_brief = bedtime.prepare(self.config, revision, datetime(2026, 10, 21, 23, tzinfo=agenda.TOKYO))
        self.assertEqual(next_brief["date"], "2026-10-22")

    def test_no_late_or_stale_briefing_and_corrupt_calendar_not_empty(self):
        revision = self.sleep()
        brief = bedtime.prepare(self.config, revision, datetime(2026, 10, 20, 23, tzinfo=agenda.TOKYO))
        self.assertIsNone(bedtime.claim(self.config, brief, now=brief["created"] + 100))
        (self.config / agenda.FILE).write_text("{broken", encoding="utf-8")
        with self.assertRaises(agenda.AgendaError):
            bedtime.prepare(self.config, revision, datetime(2026, 10, 20, 23, tzinfo=agenda.TOKYO))
        interaction.observe(self.config, "新一句话", semantic=True)
        self.assertIsNone(bedtime.claim(self.config, brief))

    def test_sleep_grant_expires_and_cannot_be_forged_with_purpose_alone(self):
        revision = self.sleep()
        self.assertFalse(interaction.allowed(self.config, "bedtime:fake"))
        brief = bedtime.prepare(self.config, revision, datetime(2026, 10, 20, 23, tzinfo=agenda.TOKYO))
        token = bedtime.claim(self.config, brief)
        state = interaction.load(self.config)
        self.assertFalse(bedtime.allowed(state, token, now=time.time() + 181))
        state["mode"] = "idle"
        self.assertFalse(bedtime.allowed(state, token))

    def test_semantic_classifier_receives_calendar_and_returns_close(self):
        session = chat.ChatSession(self.config / "jarvis.db")
        session.interaction_notify = lambda: None
        session.agenda_context_provider = lambda: dict(open=True, revision=8,
                                                      from_date="2026-10-26", to_date="2026-11-01")
        question = "日程看完了，来聊聊电影吧"
        decision = verdict("idle", question, agenda=dict(action="close", certainty="clear",
                           evidence="来聊聊电影吧", reason="日程讨论结束"))
        with mock.patch.object(session, "_key", return_value="synthetic"), \
                mock.patch.object(claude_client, "send", return_value=json.dumps(decision)) as send, \
                mock.patch.object(claude_client, "coordinated_reply", return_value="你想聊什么电影？"):
            result = session.ask(question, heard=True)
        self.assertTrue(result["agenda_scene"]["close"])
        self.assertEqual(result["agenda_scene"]["revision"], 8)
        payload = json.loads(send.call_args.args[2])
        self.assertEqual(payload["reading_panel"]["agenda"]["from_date"], "2026-10-26")

    def test_bedtime_requires_explicit_semantic_brief_not_quiet_quote_or_uncertain(self):
        for flag, scope, certainty in (("quiet", "current", "clear"), ("brief", "keep", "clear"),
                                       ("brief", "current", "uncertain"), ("none", "current", "clear")):
            session = chat.ChatSession(self.config / "jarvis.db")
            session.interaction_notify = lambda: None
            decision = verdict("sleep", "该睡了", bedtime=flag)
            decision.update(scope=scope, certainty=certainty)
            with mock.patch.object(session, "_key", return_value="synthetic"), \
                    mock.patch.object(interaction, "classify", return_value=decision), \
                    mock.patch.object(claude_client, "coordinated_reply", return_value="好的"), \
                    mock.patch.object(bedtime, "prepare") as prepare:
                result = session.ask("该睡了")
            prepare.assert_not_called()
            self.assertNotIn("bedtime_briefing", result)

    def test_spoken_vague_plan_then_time_followup_saves_once_with_context(self):
        session = chat.ChatSession(self.config / "jarvis.db")
        calls = []
        def model(*args, **kwargs):
            calls.append((args, kwargs))
            if len(calls) == 1:
                return "明天下午几点去医院？"
            executor = kwargs["tool_executor"]
            listed, failed = executor("list_events", {"from_date": "2026-10-21", "to_date": "2026-10-21", "display": False})
            self.assertFalse(failed)
            saved, failed = executor("add_event", {"title": "去医院", "start": "2026-10-21 15:00"})
            self.assertFalse(failed)
            return "已记下明天下午三点去医院。"
        with mock.patch.object(session, "_key", return_value="synthetic"), \
                mock.patch.object(claude_client, "coordinated_reply", side_effect=model):
            first = session.ask("我明天下午要去医院", heard=True)
            self.assertIn("几点", first["answer"])
            self.assertEqual(agenda.events(self.config), [])
            session.ask("三点", heard=True)
        self.assertEqual(len(agenda.events(self.config)), 1)
        self.assertEqual(agenda.events(self.config)[0]["start"], "2026-10-21 15:00")
        self.assertIn("医院", str(calls[1]))
        note = local_actions.note_for(local_actions.ActionSettings(), [])
        self.assertIn("BEFORE saving", note)

    def test_bedtime_chat_reads_calendar_after_this_turn_changes(self):
        session = chat.ChatSession(self.config / "jarvis.db")
        session.interaction_notify = lambda: None
        question = "明早九点去医院，记好我就睡了"
        def model(*args, **kwargs):
            _, failed = kwargs["tool_executor"]("add_event", {"title": "去医院", "start": "2026-10-21 09:00"})
            self.assertFalse(failed)
            return "记好了。"
        prepare = bedtime.prepare
        with mock.patch.object(session, "_key", return_value="synthetic"), \
                mock.patch.object(interaction, "classify", return_value=verdict("sleep", question, bedtime="brief")), \
                mock.patch.object(claude_client, "coordinated_reply", side_effect=model), \
                mock.patch.object(bedtime, "prepare", side_effect=lambda c, r:
                                  prepare(c, r, datetime(2026, 10, 20, 23, tzinfo=agenda.TOKYO))):
            result = session.ask(question, heard=True)
        self.assertIn("09:00，去医院", result["bedtime_briefing"]["text"])
        self.assertIn('Before you sleep', result["answer"])


if __name__ == "__main__":
    unittest.main()
