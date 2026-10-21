import json
import queue
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE/'src'), str(BASE/'src/core')]
import assistant_intent
import chat
import claude_client
import local_actions
import stt
from gui_voice import VoiceMixin
from test_voice_wake_ack import FakeWindow


class NaturalCommands(unittest.TestCase):
    def test_sound_match_keeps_whole_request_and_kws_reaches_semantics(self):
        for flags in (dict(wake_voice=True, after_wake=.9), dict(streaming_wake=True)):
            win = FakeWindow()
            win.stt_settings = stt.STTSettings()
            win.hear('放首歌', **flags)
            self.assertEqual(win.addressed, [('放首歌', True)])
        win = FakeWindow()
        win.stt_settings = stt.STTSettings()
        win.hear('Hey Mine，随便来一首')
        self.assertEqual(win.addressed, [('随便来一首', False)])

    def test_background_and_echo_cannot_become_commands(self):
        win = FakeWindow()
        win._note_heard = Mock()
        win.hear('放首歌', record_only=True)
        self.assertFalse(win.addressed)
        win.last_speaking = time.time()
        win.recent_speech = [(time.time(), '放首歌')]
        win.hear('放首歌', streaming_wake=True)
        self.assertFalse(win.addressed)

    def test_only_emergency_commands_skip_semantic_router(self):
        win = VoiceMixin()
        win.interrupt_speech = Mock()
        win.stop_agent_task = Mock()
        win._set_voice_label = Mock()
        for text in ('打开输入框', '不要打开输入框', '看看今天的花销', '打开播放器',
                     '放首歌', '我想听点东西', '别停止任务', '读一下全文'):
            self.assertFalse(win._run_voice_command(text), text)
        self.assertTrue(win._run_voice_command('停'))
        self.assertTrue(win._run_voice_command('停止任务'))
        win.interrupt_speech.assert_called_once()
        win.stop_agent_task.assert_called_once()

    def test_live_natural_requests_keep_all_capabilities_and_originals(self):
        with tempfile.TemporaryDirectory() as folder:
            session = chat.ChatSession(Path(folder)/'database/jarvis.db')
            session.interface_control = Mock(return_value=dict(ok=True))
            session.music_control = Mock(return_value=dict(ok=True, playing=True))
            phrases = ('放首歌', '随便来一首', '我昨天听的那首能再放一下吗',
                       '记住明早八点叫醒我', 'show me the player', '记下 午饭花了50人民币',
                       '不要打开输入框', '解释一下打开播放器是什么意思')
            calls = []
            def model(system, question, key, **kwargs):
                names = {t['name'] for t in kwargs['read_tools']}
                self.assertTrue({'music_control','add_alarm','record_expense','memory_records',
                                 'interface_control','evolution_points','microphone_control'} <= names)
                calls.append(question)
                return '需要补充哪首歌曲？'
            with patch.object(session, '_optional_key', return_value='fake'), \
                    patch.object(session, '_rag', side_effect=AssertionError('keyword-only route')), \
                    patch.object(claude_client, 'coordinated_reply', side_effect=model):
                for phrase in phrases:
                    result = session.ask(phrase)
                    self.assertTrue(result.get('reply_id'))
            self.assertEqual(calls, list(phrases))
            import sqlite3
            with closing(sqlite3.connect(str(session.db_path))) as db:
                originals = [r[0] for r in db.execute("select raw_text from conversation_messages where role='user'")]
            self.assertEqual(originals, list(phrases))

    def test_semantic_tools_execute_once_and_keep_permissions(self):
        with tempfile.TemporaryDirectory() as folder:
            session = chat.ChatSession(Path(folder)/'database/jarvis.db')
            session.interface_control = Mock(return_value=dict(ok=True))
            session.music_control = Mock(return_value=dict(ok=True, playing=True))
            def model(*args, **kwargs):
                execute = kwargs['tool_executor']
                self.assertFalse(execute('music_control', dict(action='play'))[1])
                self.assertFalse(execute('interface_control', dict(action='input_open'))[1])
                return '已播放，并打开输入栏。'
            with patch.object(session, '_optional_key', return_value='fake'), \
                    patch.object(claude_client, 'coordinated_reply', side_effect=model):
                result = session.ask('放首歌，我接下来想打字')
            session.music_control.assert_called_once_with('play')
            session.interface_control.assert_called_once_with('input_open')
            self.assertIn('music_control', result['memory_note'])
            settings = local_actions.load_settings(Path(folder)/'config')
            from dataclasses import replace
            local_actions.save_settings(Path(folder)/'config', replace(settings, media=False))
            context = local_actions.ActionContext(Path(folder)/'config', music_control=session.music_control)
            self.assertTrue(local_actions.execute('music_control', dict(action='play'), context)[1])
            session.music_control.assert_called_once()

    def test_interface_validation_unattended_and_errors(self):
        callback = Mock(return_value=dict(ok=True))
        for args in ({'action':'delete'}, {'action':'player','path':'secret'}, None):
            self.assertTrue(assistant_intent.execute(args, callback)[1])
        self.assertTrue(assistant_intent.execute({'action':'player'}, callback, True)[1])
        callback.assert_not_called()
        callback.return_value = dict(ok=False, error='界面未响应')
        text, error = assistant_intent.execute({'action':'player'}, callback)
        self.assertTrue(error)
        self.assertIn('未响应', json.loads(text)['error'])

    def test_full_read_waits_for_reply_and_new_interruption_cancels_it(self):
        win = VoiceMixin()
        win.busy = True
        win.queued_question = None
        win._latest_reply_text = lambda: '此前回复的完整内容'
        win.stop_speech = Mock()
        win.speak = Mock()
        self.assertTrue(win._interface_control('read_all')['ok'])
        win.speak.assert_not_called()
        self.assertTrue(win.reply_silenced)
        win._finish_interface_speech()
        win.speak.assert_called_once_with('此前回复的完整内容')
        win.speak.reset_mock()
        win._interface_control('read_all')
        win.interrupt_speech()
        win._finish_interface_speech()
        win.speak.assert_not_called()

    def test_ui_callback_runs_on_gui_thread_and_expired_action_is_not_executed(self):
        win = VoiceMixin()
        win.events = queue.Queue()
        win.web_active = True
        for name in ('open_agents_settings','open_usage_stats','open_media_player',
                     'show_input','hide_input','interrupt_speech'):
            setattr(win, name, Mock())
        results = []
        thread = threading.Thread(target=lambda: results.append(win._interface_control('input_open')))
        thread.start()
        _, call = win.events.get(timeout=2)
        call()
        thread.join(2)
        self.assertTrue(results[0]['ok'])
        win.show_input.assert_called_once()
        # Expiry is checked on the GUI thread, even if its event was queued earlier.
        thread = threading.Thread(target=lambda: results.append(win._interface_control('input_close')))
        thread.start()
        _, call = win.events.get(timeout=2)
        with patch('gui_voice.time.monotonic', return_value=time.monotonic()+30):
            call()
        thread.join(2)
        win.hide_input.assert_not_called()
        self.assertFalse(results[-1]['ok'])


if __name__ == '__main__':
    unittest.main()
