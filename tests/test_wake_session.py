"""Wake-only sessions: entry, per-turn routing and delayed work boundaries."""
import queue
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from test_voice_wake_ack import FakeWindow
from gui_voice import VoiceMixin
import conversation_mode


class Window(FakeWindow):
    def __init__(self):
        super().__init__()
        self.root = mock.Mock()
        self.events = queue.Queue()
        self._note_heard = mock.Mock()
        self._judge_heard = mock.Mock()

    def wake(self):
        self.hear('麦恩')


class WakeSession(unittest.TestCase):
    def test_acoustic_and_streaming_candidates_cannot_open_chat(self):
        for extra in ({'wake_voice': True}, {'streaming_wake': True},
                      {'wake_voice': True, 'after_wake': .1}, {}):
            with self.subTest(extra=extra):
                win = Window()
                win.hear('I wish you could see this move.', **extra)
                self.assertFalse(win.conversation_open)
                self.assertEqual(win.addressed, [])
                win._judge_heard.assert_not_called()

    def test_overlapping_output_does_not_authorize_a_new_conversation(self):
        win = Window()
        win.last_speaking = time.time()
        win.hear('Please change the channel')
        self.assertFalse(win.conversation_open)
        self.assertEqual(win.addressed, [])

    def test_pending_proposal_does_not_authorize_voice_consent(self):
        win = Window()
        win._voice_review = lambda: {'id': 1}
        win.hear('Yes, go ahead')
        win._judge_heard.assert_not_called()
        self.assertFalse(win.conversation_open)

    def test_wake_request_is_judged_before_any_action(self):
        win = Window()
        win.hear('麦恩，停止')
        self.assertTrue(win.conversation_open)
        self.assertEqual(win.addressed, [])
        win._judge_heard.assert_called_once_with('停止')

    def test_followup_and_recent_reply_both_require_judgment(self):
        for followup in (True, False):
            win = Window()
            win.wake()
            if not followup:
                win.awaiting_until = 0
                win.last_answer_at = time.time() - 1
            win.hear('Pass me that bowl, John')
            self.assertEqual(win.addressed, [])
            win._judge_heard.assert_called_once_with('Pass me that bowl, John')

    def test_other_party_closes_and_discards_pending_work(self):
        win = Window()
        win.wake()
        old = getattr(win, '_judge_generation', 0)
        win._judge_pending = ('Delete the file',)
        win._pending_voice = (('approve', True),)
        win._judged('Hello John', (False, False, True), old)
        self.assertFalse(win.conversation_open)
        self.assertEqual(win.awaiting_until, 0)
        self.assertEqual(win._judge_pending, ())
        self.assertEqual(win._pending_voice, ())
        win._judged('Delete the file', (True, False, False), old)
        self.assertEqual(win.addressed, [])

    def test_farewell_answers_once_then_requires_new_wake(self):
        win = Window()
        win.wake()
        old = getattr(win, '_judge_generation', 0)
        win._judged('Goodbye', (True, True, False), old)
        self.assertEqual(win.addressed, [('Goodbye', True)])
        self.assertFalse(win.conversation_open)
        win._judge_heard.reset_mock()
        win.hear('Do another thing')
        win._judge_heard.assert_not_called()
        win.wake()
        win.hear('A new request')
        win._judge_heard.assert_called_once_with('A new request')

    def test_idle_is_thirty_seconds_and_unrelated_work_does_not_extend_it(self):
        self.assertEqual(conversation_mode.IDLE_SECONDS, 30)
        win = Window()
        win.wake()
        win.busy = True
        old = getattr(win, '_judge_generation', 0)
        self.assertFalse(win._in_conversation(time.time() + 31))
        win._judged('Late result', (True, False, False), old)
        self.assertEqual(win.addressed, [])

    def test_uncertain_speech_does_not_extend_session(self):
        win = Window()
        win.wake()
        touched = win.conversation_touch
        win._judged('hmm maybe', (False, False, False), getattr(win, '_judge_generation', 0))
        self.assertEqual(win.conversation_touch, touched)
        self.assertTrue(win.conversation_open)
        self.assertEqual(win.addressed, [])

    def test_stale_wake_cannot_open_chat(self):
        win = Window()
        win.hear('麦恩，打开浏览器', start=time.time()-40, end=time.time()-35)
        self.assertFalse(win.conversation_open)
        self.assertEqual(win.addressed, [])
        win._judge_heard.assert_not_called()

    def test_previous_capture_cannot_enter_a_new_session(self):
        win = Window()
        win.wake()
        win.hear('Earlier conversation', start=time.time()-10, end=time.time()-5)
        win._judge_heard.assert_not_called()

    def test_closed_session_cannot_submit_or_accept_judgment(self):
        win = Window()
        VoiceMixin._judge_heard(win, 'Do it')
        self.assertTrue(win.events.empty())
        win._judged('Do it', (True, False, False))
        self.assertEqual(win.addressed, [])

    def test_push_to_talk_is_one_request_not_hands_free_entry(self):
        win = Window()
        win._run_voice_command = mock.Mock(return_value=True)
        win._voice_text('停止')
        win._run_voice_command.assert_called_once_with('停止')
        self.assertFalse(win.conversation_open)

    def test_timer_closes_without_another_microphone_event(self):
        win = Window()
        win.wake()
        tick = win.root.after.call_args.args[1]
        with mock.patch('gui_voice.time.time', return_value=win.conversation_touch + 31):
            tick()
        self.assertFalse(win.conversation_open)
        self.assertEqual(win.awaiting_until, 0)

    def test_old_timer_and_result_cannot_touch_reopened_session(self):
        win = Window()
        win.wake()
        generation = win._judge_generation
        old_tick = win.root.after.call_args.args[1]
        win._close_voice_conversation('test')
        win.wake()
        calls = win.root.after.call_count
        old_tick()
        win._judged('stale approval', (True, False, False), generation)
        self.assertTrue(win.conversation_open)
        self.assertEqual(win.root.after.call_count, calls)
        self.assertEqual(win.addressed, [])

    def test_only_active_voice_reply_protects_waiting_time(self):
        win = Window()
        win.wake()
        win.busy = win._voice_turn_active = True
        self.assertTrue(win._in_conversation(time.time()+31))
        win.busy = False
        self.assertFalse(win._in_conversation(time.time()+31))

    def test_close_preserves_explicit_button_request_only(self):
        win = Window()
        win.wake()
        win._pending_voice = (('hands free', True), ('button request', False))
        win._close_voice_conversation('test')
        self.assertEqual(win._pending_voice, (('button request', False),))
        win._note_heard.assert_called_once_with('hands free')

    def test_judge_failure_never_executes_and_timer_still_expires(self):
        win = Window()
        win.session = SimpleNamespace(_key=mock.Mock(side_effect=RuntimeError('unavailable')))
        win.wake()
        with mock.patch('gui_voice.threading.Thread') as worker:
            worker.side_effect = lambda target, **kw: mock.Mock(start=target)
            VoiceMixin._judge_heard(win, 'Do something')
        event = win.events.get_nowait()
        win._judged(*event[1:])
        self.assertEqual(win.addressed, [])
        self.assertFalse(win._in_conversation(time.time()+31))

    def test_farewell_while_busy_clears_older_requests_but_keeps_one_reply(self):
        win = Window()
        win.session = SimpleNamespace(_optional_key=lambda: 'synthetic')
        win.wake()
        win.busy = True
        win._pending_voice = (('old request', True),)
        win._addressed = lambda text, heard: win._ask_after_reply(text, heard)
        win._judged('Goodbye', (True, True, False), win._judge_generation)
        self.assertFalse(win.conversation_open)
        self.assertEqual(win._pending_voice, (('Goodbye', True),))

    def test_active_session_still_rejects_stale_background_capture(self):
        win = Window()
        win.wake()
        win.hear('old background', end=time.time()-20)
        win._judge_heard.assert_not_called()

    def test_already_sleeping_can_wake_and_stay_open_after_scene_refresh(self):
        import tempfile
        from pathlib import Path
        from data_location import config_for_db
        from gui_interaction import InteractionMixin
        import interaction
        with tempfile.TemporaryDirectory() as folder:
            win = Window()
            win.session = SimpleNamespace(db_path=Path(folder)/'database/jarvis.db')
            config = config_for_db(win.session.db_path)
            interaction.observe(config, '我要睡觉了')
            win._usage_config = lambda: config
            win.tts_purpose = 'reply'
            win.wake()
            generation = win._judge_generation
            InteractionMixin._interaction_changed(win)
            self.assertTrue(win.conversation_open)
            self.assertEqual(win._judge_generation, generation)


if __name__ == '__main__':
    unittest.main()
