import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1]/'src'),
               str(Path(__file__).resolve().parents[1]/'src/core')]
import singing
import interaction


def decision(words, activity='speech', intent='request', resume=False):
    return dict(mode='idle', scope='keep', certainty='clear', evidence=words, reason='current context',
                addressing=dict(to_myn=True, ends=False, elsewhere=False),
                vocal=dict(activity=activity,intent=intent,certainty='clear',
                           evidence=words,resume_conversation=resume))


class SingingGuardTests(unittest.TestCase):
    def test_lyrics_cannot_become_thanks_or_actions(self):
        g=singing.Guard()
        result=g.decide(decision('谢谢你陪着我','singing','none'), '谢谢你陪着我',g.snapshot())
        self.assertFalse(result['addressing']['to_myn'])
        self.assertFalse(g.allowed('reply'))
        self.assertFalse(g.allowed('proactive'))
        self.assertTrue(g.allowed('alarm'))

    def test_direct_request_during_music_does_not_end_quiet_scene(self):
        g=singing.Guard();g.signal(10)
        result=g.decide(decision('伴奏小一点'), '伴奏小一点',g.snapshot())
        self.assertTrue(result['addressing']['to_myn'])
        self.assertTrue(g.allowed('reply'))
        self.assertFalse(g.allowed('proactive'))
        self.assertTrue(g.snapshot()['active'])
        # Renewed singing revokes the exception, including late generated audio.
        g.signal(15)
        self.assertFalse(g.allowed('reply'))

    def test_natural_return_to_conversation_and_no_timer_based_exit(self):
        g=singing.Guard();g.signal(10)
        self.assertTrue(g.snapshot()['active'])
        g.decide(decision('歇一会儿，陪我聊聊今天的事情',resume=True),
                 '歇一会儿，陪我聊聊今天的事情',g.snapshot())
        self.assertFalse(g.snapshot()['active'])
        self.assertTrue(g.allowed('reply'))
        g.signal(10.5)
        self.assertFalse(g.snapshot()['active'])  # same background music is not a new interruption
        g.signal(15)
        self.assertTrue(g.snapshot()['active'])

    def test_uncertain_missing_or_stale_verdict_keeps_quiet(self):
        g=singing.Guard();g.signal(10)
        snapshot=g.snapshot();g.signal(20)
        result=g.decide(decision('放下一首'), '放下一首',snapshot)
        self.assertFalse(result['addressing']['to_myn'])
        for vocal in (None, {}, dict(activity='speech',intent='request',certainty='uncertain',evidence='好'),
                      dict(activity='speech',intent='request',certainty='clear',evidence='invented')):
            result=g.decide(dict(addressing=dict(to_myn=True),vocal=vocal),'好',g.snapshot())
            self.assertFalse(result['addressing']['to_myn'])
            self.assertFalse(g.allowed('reply'))

    def test_real_classifier_boundary_and_alarm_exception(self):
        with tempfile.TemporaryDirectory() as folder:
            g=singing.guard(folder);g.signal(time.time())
            with patch('claude_client.send',return_value=json.dumps(decision('你好呀','singing','none'))) as send:
                result=interaction.classify(folder,interaction.load(folder),'你好呀',[],None,'test',addressing={})
            self.assertFalse(result['addressing']['to_myn'])
            payload=json.loads(send.call_args.args[2])
            self.assertTrue(payload['vocal_context']['active'])
            self.assertFalse(interaction.allowed(folder,'reply'))
            self.assertTrue(interaction.allowed(folder,'alarm'))
            self.assertFalse(interaction.idle(folder))
            self.assertFalse(interaction.offer(folder,{}))

    def test_live_hint_stops_existing_speech_but_not_alarm(self):
        from gui_interaction import InteractionMixin
        from gui_voice import VoiceMixin
        class Window(VoiceMixin,InteractionMixin): pass
        with tempfile.TemporaryDirectory() as folder:
            w=Window();w.session=SimpleNamespace(db_path=Path(folder)/'database/jarvis.db')
            from data_location import config_for_db
            w._usage_config=lambda:config_for_db(w.session.db_path)
            w.always_on_active=True;w._sync_ringing=Mock();w._voice_attention=Mock()
            w.stop_speech=Mock();w.tts_purpose='reply'
            w._heard(dict(event='vocal_activity',kind='sustained_pitch',end=time.time()))
            w.stop_speech.assert_called_once()
            self.assertFalse(w._speech_allowed('reply'))
            self.assertTrue(w._speech_allowed('alarm'))
            w.stop_speech.reset_mock();w.tts_purpose='alarm'
            w._heard(dict(event='vocal_activity',kind='sustained_pitch',end=time.time()))
            w.stop_speech.assert_not_called()

    def test_candidate_wake_and_follow_on_must_be_judged_while_singing(self):
        from test_voice_wake_ack import FakeWindow
        with tempfile.TemporaryDirectory() as folder:
            w=FakeWindow();w.session=SimpleNamespace(db_path=Path(folder)/'database/jarvis.db')
            from data_location import config_for_db
            singing.guard(config_for_db(w.session.db_path)).signal(time.time())
            w._judge_heard=Mock()
            now=time.time()
            w._heard(dict(event='heard',text='麦恩',start=now-1,end=now))
            w._judge_heard.assert_called_once()
            self.assertFalse(w.spoken)
            self.assertFalse(w.addressed)


class AcousticTests(unittest.TestCase):
    def test_existing_listener_emits_hint_and_still_records_utterance(self):
        try: import numpy as np
        except ImportError: self.skipTest('numpy in speech environment')
        from stt_worker import Listener
        events=[]
        listener=Listener(None,np,lambda audio,language:'谢谢你陪着我',events.append,threaded=False,levels=True)
        sr=listener.rate;t=np.arange(sr*3)/sr
        signal=np.concatenate((.1*np.sin(2*np.pi*220*t),np.zeros(sr)))
        for i in range(0,len(signal),480): listener.feed(signal[i:i+480],100+i/sr)
        self.assertTrue(any(e['event']=='vocal_activity' for e in events))
        self.assertTrue(any(e['event']=='level' for e in events))
        heard=[e for e in events if e['event']=='heard']
        self.assertEqual(len(heard),1)
        self.assertEqual(heard[0]['text'],'谢谢你陪着我')

    def test_sustained_notes_but_not_noise_silence_or_short_syllables(self):
        try: import numpy as np
        except ImportError: self.skipTest('numpy in speech environment')
        hint=singing.PitchHint(np);sr=16000;t=np.arange(sr*3)/sr
        voice=.06*np.sin(2*np.pi*220*t)+.025*np.sin(2*np.pi*440*t)
        self.assertTrue(hint.analyze(voice,sr))
        self.assertFalse(hint.analyze(np.zeros_like(voice),sr))
        self.assertFalse(hint.analyze(np.random.default_rng(4).normal(0,.06,len(t)),sr))
        syllables=voice*((t%.45)<.22)
        self.assertFalse(hint.analyze(syllables,sr))
        fired=[]
        for i in range(0,len(voice),480): fired.append(hint.feed(voice[i:i+480],sr))
        self.assertTrue(any(fired))
        self.assertLessEqual(hint.samples,sr*2.5)


class ChatBoundaryTests(unittest.TestCase):
    def test_sung_thanks_cannot_reach_reply_tools_or_preference_learning(self):
        import chat
        import message_log
        import conversation_log
        import speech_journal
        from data_location import config_for_db
        with tempfile.TemporaryDirectory() as folder:
            db=Path(folder)/'database/jarvis.db';session=chat.ChatSession(db)
            session.interaction_notify=Mock()
            config=config_for_db(db)
            line='谢谢你陪着我'
            capture=dict(capture_id='singingtest001',text=line,start=time.time()-2,end=time.time())
            source=speech_journal.record(db,capture)
            with patch.object(session,'_key',return_value='synthetic'), \
                    patch('claude_client.send',return_value=json.dumps(decision(line,'singing','none'))), \
                    patch.object(session,'_dispatch') as dispatch, \
                    patch.object(session.preferences,'prepare_turn') as learn:
                session.record_input(line,source_id=source)
                result=session.ask(line,heard=True,recorded_id=source)
            self.assertTrue(result['silent']);dispatch.assert_not_called();learn.assert_not_called()
            saved=message_log.get(db,source)
            self.assertEqual(saved['raw_text'],line)
            self.assertEqual(saved['channel'],'heard')
            self.assertEqual(saved['status'],'skipped')
            self.assertFalse(any(r['role']=='assistant' for r in conversation_log.list_messages(db)))
            self.assertFalse(interaction.allowed(config,'reply'))

    def test_clear_request_runs_but_classifier_failure_does_not(self):
        import chat
        from data_location import config_for_db
        with tempfile.TemporaryDirectory() as folder:
            session=chat.ChatSession(Path(folder)/'database/jarvis.db')
            session.interaction_notify=Mock()
            config=config_for_db(session.db_path)
            singing.guard(config).signal(time.time())
            line='伴奏稍微调低一点'
            with patch.object(session,'_key',return_value='synthetic'), \
                    patch('claude_client.send',return_value=json.dumps(decision(line))), \
                    patch.object(session,'_dispatch',return_value={'answer':'已调低','sources':[]}) as dispatch:
                result=session.ask(line,heard=True)
                dispatch.assert_called_once()
                self.assertEqual(result['answer'],'已调低')
            with patch.object(session,'_key',return_value='synthetic'), \
                    patch('claude_client.send',side_effect=ValueError('unavailable')), \
                    patch.object(session,'_dispatch') as dispatch:
                result=session.ask('又一句歌词',heard=True)
                self.assertTrue(result['silent']);dispatch.assert_not_called()


if __name__=='__main__': unittest.main()
