"""Real Tk widgets/chat event loop; isolated database, no network or audible output."""
import os
import sys
import tempfile
import time
from contextlib import ExitStack, closing
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src/core")]


def main():
    with tempfile.TemporaryDirectory(prefix="myn-interaction-ui-") as folder:
        config = Path(folder) / "config"
        database = Path(folder) / "database" / "jarvis.db"
        database.parent.mkdir()
        os.environ.update(MYN_PERSONAL_DATA_DIR=folder, MYN_CONFIG_DIR=str(config),
                          MYN_DATA_DIR=str(database.parent))
        import tkinter as tk
        import myn_gui
        import interaction
        import evolution
        import tts

        myn_gui.ChatSession(database)
        player = mock.Mock()
        player.active.return_value = False
        spoken = []
        def synth(text, settings):
            spoken.append(text)
            return b"test-audio"

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(myn_gui, "prepare_data", return_value=(database, "current")))
            stack.enter_context(mock.patch.object(myn_gui, "MP3Player", return_value=player))
            stack.enter_context(mock.patch.object(myn_gui.web_frontend, "available", return_value=False))
            stack.enter_context(mock.patch.object(myn_gui.stt, "missing_parts", return_value=["test"]))
            stack.enter_context(mock.patch.object(tts, "synthesize", side_effect=synth))
            stack.enter_context(mock.patch.object(tts.ElevenLabsProvider, "stream",
                                                 side_effect=lambda text, settings, **kw: iter([synth(text, settings)])))
            stack.enter_context(mock.patch.object(myn_gui.ChatSession, "_key", return_value=None))
            for method in ("_make_frameless", "_self_update_startup", "_dev_startup", "_evolution_boot",
                           "_reanalyse_history_once", "_daily_usage_check", "process_pending", "_poll_hotkey"):
                stack.enter_context(mock.patch.object(myn_gui.MYNWindow, method,
                                                       lambda self, *a, **kw: None))
            root = tk.Tk()
            root.withdraw()
            win = myn_gui.MYNWindow(root)
            assert str(win.entry.cget("state")) != tk.DISABLED, win.transcript.get("1.0", "end")
            win.web = mock.Mock()  # transport only; the real bridge and widgets run below
            win.web_active = True
            win.greeted = True
            errors = []
            root.report_callback_exception = lambda *error: errors.append(error)

            def pump(predicate=lambda: True):
                deadline = time.monotonic() + 5
                while True:
                    root.update()
                    if predicate():
                        break
                    if time.monotonic() >= deadline:
                        raise AssertionError("UI did not settle: " + win.transcript.get("1.0", "end"))
                    time.sleep(.02)
                assert not errors, errors

            def send(text):
                # The actual web bridge writes the real Tk input and calls send().
                win._web_event({"event": "send", "text": text})
                pump(lambda: not win.busy)

            try:
                # Reproduce the idle-mode false courtesy capture before any real turn.
                import sqlite3
                win.always_on_active = True
                now = time.time()
                with mock.patch.object(win, '_addressed') as addressed, \
                        mock.patch.object(win, '_judge_heard') as judged:
                    win._heard(dict(event='heard', text='Thank you.', start=now - 2.5,
                                    end=now - .1, capture_id='courtesyincident0001',
                                    wake_voice=True, after_wake=.96, language='en'))
                    addressed.assert_not_called()
                    judged.assert_not_called()
                assert not win.conversation_open and not spoken
                with closing(sqlite3.connect(str(database))) as db:
                    assert db.execute('SELECT addressed FROM speech_captures WHERE capture_id=?',
                                      ('courtesyincident0001',)).fetchone() == (0,)
                print('PASS: idle false courtesy capture stays unconfirmed with no conversation, model judgment or audio.')
                with mock.patch.object(win.session, '_key', return_value='synthetic'), \
                        mock.patch.object(win.session, 'inspect_voice', return_value=((False, False, True), None)) as inspect, \
                        mock.patch.object(win, '_addressed') as addressed:
                    win._heard(dict(event='heard', text='I wish you could see this move.',
                                    start=time.time()-.3, end=time.time(), wake_voice=True))
                    inspect.assert_not_called()
                    win._heard(dict(event='heard', text='Hey Mine', start=time.time()-.3, end=time.time()))
                    assert win.conversation_open
                    generation = win._judge_generation
                    win._heard(dict(event='heard', text='John, pass me the bowl', start=time.time(), end=time.time()))
                    pump(lambda: not win.conversation_open)
                    inspect.assert_called_once()
                    addressed.assert_not_called()
                    win.events.put(('judged', 'obsolete approval', (True, False, False), generation, None))
                    pump(lambda: win.events.empty())
                    addressed.assert_not_called()
                    assert not win.awaiting_until and not win._judge_pending
                print('PASS: acoustic candidate stays quiet; exact wake opens; other-party judgment closes; late result cannot act.')
                send("我要睡觉了")
                assert interaction.load(config)["mode"] == "sleep", win.transcript.get("1.0", "end")
                pump(lambda: bool(spoken))
                assert win._speech_allowed("reply")
                assert not win._speech_allowed("proactive")
                import json
                import claude_client
                sleep_question = 'Can you answer quietly?'
                sleep_decision = dict(mode='sleep', scope='keep', certainty='clear', evidence=sleep_question,
                                      reason='Direct question after explicit wake',
                                      addressing=dict(to_myn=True, ends=False, elsewhere=False))
                with mock.patch.object(win.session, '_key', return_value='synthetic'), \
                        mock.patch.object(claude_client, 'send', return_value=json.dumps(sleep_decision)) as classify, \
                        mock.patch.object(claude_client, 'coordinated_reply', return_value='Yes, I can answer quietly.'):
                    win._heard(dict(event='heard', text='Hey Mine', start=time.time()-.3, end=time.time()))
                    win._heard(dict(event='heard', text=sleep_question, start=time.time(), end=time.time()))
                    pump(lambda: not win.busy and 'Yes, I can answer quietly.' in win.transcript.get('1.0', 'end'))
                    pump(lambda: any('answer quietly' in text for text in spoken))
                    assert win.conversation_open and interaction.load(config)['mode'] == 'sleep'
                    assert classify.call_count == 1, 'Preflight judgment should be reused'
                win._close_voice_conversation('smoke_complete')
                print('PASS: exact wake during sleep survives scene refresh, speaks the direct answer and reuses one judgment.')
                sleep_reply_count = len(spoken)
                point, _ = evolution.add(config, "夜间不打扰", request="睡觉不要打扰")
                evolution.update(config, point["id"], status="proposed",
                                 plan={"summary": "用场景保护", "plan": "这里是完整方案"})
                win._evolution_announce(config)
                assert len(spoken) == sleep_reply_count
                import alarm_ringing
                ring = alarm_ringing.activate(config, "schedule:isolated-alarm", "test", "早上八点，该起床了")
                win._queue_task({"id": "isolated-alarm", "kind": "say", "text": "早上八点，该起床了"})
                pump(lambda: any("起床" in text for text in spoken))
                assert "起床" in "".join(spoken)
                assert interaction.load(config)["mode"] == "sleep"
                pump(lambda: win._ring_waiting_since == 0)
                alarm_ringing.mark_attempt(config, ring["id"], now=0)
                win._ring_not_before = 0
                before_repeat = len(spoken)
                win.busy = False
                win._repeat_ringing()
                pump(lambda: len(spoken) > before_repeat)
                win.always_on_active = True
                win.conversation_open = False
                win._heard({"event": "heard", "text": "关闭闹钟", "start": time.time(), "end": time.time()})
                assert alarm_ringing.active(config), 'Background words must not dismiss an alarm'
                win._voice_text("关闭闹钟")  # deliberate one-shot microphone-button input
                pump(lambda: not win.busy and not alarm_ringing.active(config))
                win.always_on_active = False
                assert not alarm_ringing.active(config)
                assert interaction.load(config)["mode"] == "sleep"
                print("PASS: persistent alarm repeats, then an offline spoken-equivalent command dismisses only this ringing.")
                import agenda
                import agenda_display
                sample = agenda.add(config, title="隔离日程检查", start="2026-10-28 10:00")
                win.session.show_agenda(agenda_display.snapshot(config, "2026-10-26", "2026-11-01"))
                pump(lambda: win._agenda_payload is not None)
                assert len(win._agenda_payload["days"]) == 7
                agenda.update(config, sample["id"], title="修改后的日程")
                win._agenda_changed()
                assert win._agenda_payload["days"][2]["events"][0]["title"] == "修改后的日程"
                win.web_active = False
                win._draw_agenda_display()
                assert win.core_canvas.find_withtag("agenda_display")
                win._scroll_agenda(10)
                win.web_active = True
                win._web_event({"event": "agenda_close"})
                assert win._agenda_payload is None
                assert not win.core_canvas.find_withtag("agenda_display")
                print("PASS: saved agenda callback on GUI thread, refresh after edit, classic main-canvas fallback, close.")
                send("我醒了")
                send("我要学习了")
                count = len(spoken)
                win._evolution_announce(config)
                assert len(spoken) == count
                send("我学完了")
                interaction.observe(config, "我有空了", now=0)
                win.conversation_open = False
                win._evolution_announce(config)
                assert "good time to review" in win.transcript.get("1.0", "end")
                assert "完整方案" not in win.transcript.get("1.0", "end")
                send("等会再给我看")
                assert interaction.load(config)["deferred"]
                send("好的我看看")
                assert "完整方案" in win.transcript.get("1.0", "end")
                assert evolution.get(config, point["id"])["status"] == "proposed"
                assert win.reader_scene == "decision"
                assert win.session.pending_review()["id"] == point["id"]
                import json
                import claude_client
                import local_actions
                import conversation_mode
                local_actions.save_settings(config, local_actions.ActionSettings(self_fix=True))
                phrase = "这个方案就算了吧"
                refusal = dict(mode="idle", scope="keep", certainty="clear", evidence=phrase,
                               reason="回答当前进化方案",
                               addressing=dict(to_myn=True, ends=False, elsewhere=False),
                               evolution=dict(action="reject", id=point["id"], scope="current",
                                              certainty="clear", evidence=phrase))
                with mock.patch.object(win.session, "_key", return_value="synthetic"), \
                        mock.patch.object(conversation_mode, "judge", return_value=(True, False, False)), \
                        mock.patch.object(claude_client, "send", return_value=json.dumps(refusal)), \
                        mock.patch.object(claude_client, "coordinated_reply") as general:
                    win.always_on_active = True
                    win.awaiting_until = 0
                    win.conversation_open = False
                    win._heard({"event": "heard", "text": phrase,
                                "start": time.time() - .2, "end": time.time() - .1})
                    assert evolution.get(config, point['id'])['status'] == 'proposed'
                    win._heard(dict(event='heard', text='Hey Mine', start=time.time()-.2, end=time.time()))
                    win._heard({"event": "heard", "text": phrase,
                                "start": time.time() - .1, "end": time.time()})
                    pump(lambda: not win.busy and evolution.get(config, point["id"])["status"] == "rejected")
                    general.assert_not_called()
                win.always_on_active = False
                assert win.session.pending_review() is None
                pump(lambda: "has been declined" in win.transcript.get("1.0", "end"))
                pump(lambda: any("has been declined" in text for text in spoken))
                print("PASS: pending proposal voice reply -> serial intent judgment -> verified rejection -> one UI reply.")
                # The online path always interprets context before generating
                # the reply, even though these phrases aren't local shortcuts.
                def classify(model, system, prompt, key, **kwargs):
                    import json
                    assert kwargs["purpose"] == "interaction_scene"
                    data = json.loads(prompt)
                    text = data["current_user_message"]
                    mode = {"眼皮撑不住了，今天陪我到这里吧": "sleep",
                            "被门铃吵醒了，坐起来陪我聊会儿": "idle",
                            "帮我看这道积分哪步算错了": "study",
                            "书收进包里了，聊聊周末的事吧": "idle"}[text]
                    return json.dumps(dict(mode=mode, scope="current", certainty="clear",
                                           evidence=text, reason="结合前文和当前活动"))
                with mock.patch.object(win.session, "_key", return_value="synthetic"), \
                        mock.patch.object(myn_gui.claude_client, "send", side_effect=classify), \
                        mock.patch.object(myn_gui.claude_client, "coordinated_reply", return_value="收到。"):
                    for text, mode in (("眼皮撑不住了，今天陪我到这里吧", "sleep"),
                                       ("被门铃吵醒了，坐起来陪我聊会儿", "idle"),
                                       ("帮我看这道积分哪步算错了", "study"),
                                       ("书收进包里了，聊聊周末的事吧", "idle")):
                        send(text)
                        assert interaction.load(config)["mode"] == mode
                        assert not interaction.load(config)["pending"]
                # Create through the real chat executor, then fire the saved reminder
                # while sleeping. Only the model/audio transport are simulated.
                import json
                import local_actions
                import alarm_delivery
                from datetime import datetime, timedelta, timezone
                def plan_alarm(*args, **kwargs):
                    assert kwargs["on_text"] is None
                    receipt, failed = kwargs["tool_executor"](
                        "set_reminder", dict(text="隔离测试该起床了", in_minutes=5))
                    assert not failed, receipt
                    return "已经设为九点了"  # wrong generated time must never be spoken/shown
                with mock.patch.object(win.session, "_key", return_value="synthetic"), \
                        mock.patch.object(interaction, "classify", return_value=dict(
                            mode="idle", scope="keep", certainty="clear", reason="继续当前场景")), \
                        mock.patch.object(myn_gui.claude_client, "coordinated_reply", side_effect=plan_alarm):
                    send("五分钟后提醒我起床")
                assert "已经设为九点了" not in win.transcript.get("1.0", "end")
                assert "Reminder saved" in win.transcript.get("1.0", "end")
                interaction.observe(config, "我要睡觉了")
                due = local_actions.due_reminders(config, datetime.now(timezone.utc) + timedelta(minutes=6))
                assert len(due) == 1
                with mock.patch.object(local_actions, "due_reminders", return_value=due):
                    win._check_reminders()
                pump(lambda: any(r["status"] == "tts_submitted" and r.get("id") in {
                    d["id"] for d in alarm_delivery.recent(config)
                    if d.get("source_id") == due[0]["id"]}
                    for r in alarm_delivery.recent(config)))
                assert not local_actions.reminders(config)
                assert interaction.load(config)["mode"] == "sleep"
                print("PASS: real input/web bridge, persisted sleep/study, exempt alarm, brief invitation, defer/show.")
                print("PASS: context-classified natural sleep/study entry and exit before replies (simulated model).")
                print("PASS: actual reminder tool/save/receipt/GUI dispatch/audio queue while sleeping; generated wrong time blocked.")
                # Context-driven calendar close, then one narrowly permitted bedtime voice.
                import bedtime
                win._show_agenda_display(agenda_display.snapshot(config, "2026-10-26", "2026-11-01"))
                switch = "日程看完了，聊聊电影吧"
                decision = dict(mode="idle", scope="current", certainty="clear", evidence=switch,
                                reason="明确换话题", agenda=dict(action="close", certainty="clear",
                                evidence="聊聊电影吧", reason="日程讨论已结束"), bedtime="none")
                with mock.patch.object(win.session, "_key", return_value="synthetic"), \
                        mock.patch.object(interaction, "classify", return_value=decision), \
                        mock.patch.object(myn_gui.claude_client, "coordinated_reply", return_value="聊哪部电影？"):
                    send(switch)
                assert win._agenda_payload is None
                agenda.add(config, title="睡前汇报检查", start="2026-10-21 09:00")
                phrase = "眼皮快撑不住了，今晚就先到这里吧"
                decision = dict(mode="sleep", scope="current", certainty="clear", evidence=phrase,
                                reason="当前自然睡前告别", bedtime="brief")
                original_prepare = bedtime.prepare
                with mock.patch.object(win.session, "_key", return_value="synthetic"), \
                        mock.patch.object(interaction, "classify", return_value=decision), \
                        mock.patch.object(myn_gui.claude_client, "coordinated_reply", return_value="晚安。"), \
                        mock.patch.object(bedtime, "prepare", side_effect=lambda c, r:
                                          original_prepare(c, r, datetime(2026, 10, 20, 23, tzinfo=agenda.TOKYO))):
                    send(phrase)
                    pump(lambda: "Before you sleep" in "".join(spoken))
                    joined = "".join(spoken)
                    assert "晚安" in joined and joined.rindex("晚安") < joined.index("Before you sleep")
                    assert win.tts_purpose == "reply"
                    assert not win._speech_allowed("proactive")
                    assert win._speech_allowed("alarm")
                    before = win.transcript.get("1.0", "end").count("Before you sleep")
                    count = "".join(spoken).count("Before you sleep")
                    send(phrase)
                    assert win.transcript.get("1.0", "end").count("Before you sleep") == before
                    assert "".join(spoken).count("Before you sleep") == count
                print("PASS: semantic calendar close; spoken good night then bedtime briefing once; background speech stays quiet in sleep.")
                import singing
                interaction.observe(config, '我醒了')
                win.always_on_active = True
                win.tts_purpose = 'reply'
                old_request = win.tts_request_id
                win._heard(dict(event='vocal_activity', kind='sustained_pitch', end=time.time()))
                assert not win._speech_allowed('reply')
                before_audio = player.enqueue.call_count
                win.events.put(('tts_chunk', old_request, b'old-reply'))
                win.events.put(('tts_chunk', win.tts_request_id, b'late-reply'))
                pump(lambda: win.events.empty())
                assert player.enqueue.call_count == before_audio
                before_spoken = len(spoken)
                win.speak('不客气，你继续唱')
                win.speak('进化方案已经完成', purpose='proactive')
                pump()
                assert len(spoken) == before_spoken
                win.tts_purpose = 'alarm'
                win.events.put(('tts_chunk', win.tts_request_id, b'alarm'))
                pump(lambda: player.enqueue.call_count > before_audio)
                g = singing.guard(config)
                words = '歇一会儿，陪我聊聊吧'
                g.decide(dict(addressing=dict(to_myn=True), vocal=dict(activity='speech',
                         intent='request', certainty='clear', evidence=words, resume_conversation=True)),
                         words, g.snapshot())
                assert win._speech_allowed('reply')
                print('PASS: singing blocks old/new queued speech and proactive announcements; alarms pass; natural return restores replies.')
                print("All data temporary; synthesis/player simulated; no paid API or actual loudspeaker test.")
            finally:
                win.close()


if __name__ == "__main__":
    main()
