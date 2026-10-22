"""Streaming wake and idle learning contracts, isolated audio/models/databases."""
import json
import queue
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from contextlib import closing

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE/'src'), str(BASE/'src/core'), str(BASE/'tests')]
import chat
import message_log
import speech_journal
import conversation_log
import memory_processor
import scene_memory
import organized_memory
import evolution
import evolution_sources
import memory_tools
import preference_history
import stt_worker
from myn_gui import MYNWindow
from test_voice_wake_ack import FakeWindow

try:
    import numpy as np
except ImportError:
    np = None


class Journal(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name)/'database'/'jarvis.db'
        self.session = chat.ChatSession(self.db)

    def capture(self, text='I might study tomorrow', ident='capture0001'):
        return dict(text=text, capture_id=ident, start=time.time()-1200, end=time.time()-1199)

    def test_receipts_preserve_time_and_verbatim_and_deduplicate_only_identity(self):
        event = self.capture()
        one = speech_journal.record(self.db,event)
        self.assertEqual(speech_journal.record(self.db,event),one)
        two = speech_journal.record(self.db,self.capture(ident='capture0002'))
        self.assertNotEqual(one,two)
        original = message_log.get(self.db,one)
        self.assertEqual(original['raw_text'],event['text'])
        self.assertEqual(original['sent_at'],speech_journal.stamp(event['start']))
        self.assertEqual(original['channel'],'heard')
        with mock.patch.object(self.session,'_optional_key',return_value=''):
            self.assertEqual(self.session.record_input('study tomorrow',source_id=one),one)
        self.assertEqual(message_log.get(self.db,one)['raw_text'],event['text'])
        self.assertEqual(len(conversation_log.list_messages(self.db)),2)
        self.assertEqual(message_log.get(self.db,one)['channel'],'typed')

    def test_invalid_secret_and_unknown_time_are_not_recorded(self):
        for event in (self.capture('password=secret123'), self.capture(ident='bad'),
                      dict(self.capture(), start=float('nan'))):
            with self.assertRaises(ValueError):
                speech_journal.record(self.db,event)
        self.assertEqual(conversation_log.list_messages(self.db),[])

    def test_ambient_identity_cannot_be_promoted_by_model(self):
        ident = speech_journal.record(self.db,self.capture())
        claimed = scene_memory.claim_scene(self.db)
        value = dict(scene=dict(summary='Possibly studying',setting='unknown',speakers=['unconfirmed']),
                     topics=[dict(evidence=[ident],categories=['study'])],
                     memories=[dict(kind='fact',type='goal',category='study',content='Study tomorrow',
                                    why='',evidence=[ident],confidence=1)])
        result = memory_processor.validate_scene(json.dumps(value),claimed['rows'])
        self.assertEqual(result['memories'],[])
        scene_memory.complete_scene(self.db,claimed,result)
        snippets = organized_memory.context(self.db,'studying')
        self.assertEqual(snippets[0]['sources'][0]['channel'],'heard')
        seen = {}
        response, error = memory_tools.execute(dict(action='list',collection='history'),self.db,None,seen)
        self.assertFalse(error)
        self.assertEqual(json.loads(response)['records'][0]['speaker'],'unconfirmed')
        lines, _ = evolution_sources.read(self.db,Path(self.temp.name)/'config')
        self.assertEqual(lines[0][1],'ambient')
        self.assertEqual(evolution.parse_mining(json.dumps(dict(points=[dict(title='Improve wake',
            quote_ids=[lines[0][0]])])),lines,set()),[])

    def test_passive_gui_path_cannot_execute_even_command_or_transcribed_wake(self):
        window = FakeWindow()
        window.session = self.session
        window.process_pending = mock.Mock()
        for i,text in enumerate(('delete the file', '麦恩，关机')):
            window.hear(text, capture_id='passive00%d'%i, record_only=True)
        self.assertEqual(window.addressed,[])
        self.assertEqual(window.spoken,[])
        self.assertEqual(speech_journal.status(self.db)['captures'],2)
        self.assertEqual(len(conversation_log.list_messages(self.db)),2)

    def test_backlog_and_input_boundaries_are_visible_without_audio_or_secrets(self):
        speech_journal.gap(self.db,dict(reason='transcription_backlog',start=time.time()-3,end=time.time()))
        self.assertEqual(speech_journal.status(self.db)['recent_gaps'][0]['reason'],'transcription_backlog')

    def test_idle_gate_does_not_claim_or_call_model_during_chat(self):
        ident = speech_journal.record(self.db,self.capture())
        with mock.patch.object(memory_processor.claude_client,'send') as send:
            memory_processor.process_batch(self.db,key='fake',should_continue=lambda:False)
        send.assert_not_called()
        self.assertEqual(message_log.get(self.db,ident)['status'],'pending')

    def test_idle_arrival_stops_followup_profile_but_keeps_committed_scene(self):
        ident = self.session.record_input('I study physics')
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('UPDATE sent_messages SET sent_at=?',(speech_journal.stamp(time.time()-1200),))
        idle = [True]
        def send(*args,**kwargs):
            idle[0] = False
            return json.dumps(dict(scene=dict(summary='Studies physics',setting='study',speakers=['user']),
                memories=[dict(kind='fact',type='profile',category='study',content='Studies physics',
                               why='',evidence=[ident],confidence=.9)]))
        with mock.patch.object(memory_processor.claude_client,'send',side_effect=send) as model:
            outcome=memory_processor.process_batch(self.db,key='fake',should_continue=lambda:idle[0])
        self.assertEqual(model.call_count,1)
        self.assertEqual(outcome['created'],1)
        self.assertEqual(scene_memory.scenes_needing_profile(self.db),[1])
        with mock.patch.object(memory_processor.claude_client,'send',return_value='{"profile":"Studies physics"}'):
            memory_processor.process_batch(self.db,key='fake')
        self.assertEqual(scene_memory.scenes_needing_profile(self.db),[])

    def test_retrieval_uses_shared_scene_index_and_hides_revised_summary(self):
        ident=self.session.record_input('I study physics')
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('UPDATE sent_messages SET sent_at=?',(speech_journal.stamp(time.time()-1200),))
        claimed=scene_memory.claim_scene(self.db)
        scene_memory.complete_scene(self.db,claimed,dict(scene=dict(summary='Physics study',setting='study',speakers=['user']),
                                                       memories=[],topics=[]))
        cid=conversation_log.conversation_id_for_source(self.db,ident)
        self.assertTrue(self.session._organized_context('physics'))
        self.assertTrue(organized_memory.for_conversations(self.db,[cid]))
        conversation_log.change(self.db,cid,'edit',text='I study biology',expected='I study physics')
        self.assertEqual(self.session._organized_context('physics'),[])

    def test_unknown_speaker_never_supplies_language_preference_evidence(self):
        candidate=dict(dimension='length',direction='less',scene='all',basis='explicit',
                       evidence=[dict(id=1,quote='shorter please')])
        with self.assertRaises(ValueError):
            preference_history._candidate_result(json.dumps(dict(candidates=[candidate])),
                [dict(id=1,role='user',text='shorter please',eligible=False)])

    def test_background_gate_rechecks_live_activity(self):
        window=SimpleNamespace(busy=False,listening=False,last_speaking=0,conversation_touch=0)
        self.assertTrue(MYNWindow._idle_work_allowed(window))
        window.busy=True
        self.assertFalse(MYNWindow._idle_work_allowed(window))
        window.busy=False
        window._last_voice_activity=time.time()
        self.assertFalse(MYNWindow._idle_work_allowed(window))

    def test_shared_scene_is_available_to_preferences_and_evolution_after_idle_analysis(self):
        source=speech_journal.record(self.db,self.capture('Please make MYN answers shorter'))
        with mock.patch.object(self.session,'_optional_key',return_value=''):
            self.session.record_input('Please make MYN answers shorter',source_id=source)
        ask=mock.Mock(return_value='{"candidates":[]}')
        self.assertEqual(preference_history.learn(self.session.preferences,ask),0)
        self.assertEqual(evolution_sources.read(self.db,Path(self.temp.name)/'config')[0],[])
        reply=json.dumps(dict(scene=dict(summary='MYN communication feedback',setting='direct interaction',speakers=['user']),
                             memories=[],topics=[dict(evidence=[source],categories=['idea'])]))
        with mock.patch.object(memory_processor.claude_client,'send',return_value=reply):
            memory_processor.process_batch(self.db,key='fake')
        self.assertEqual(preference_history.learn(self.session.preferences,ask),1)
        payload=json.loads(ask.call_args.args[1])
        self.assertEqual(payload['organized_scenes'][0]['summary'],'MYN communication feedback')
        lines,_=evolution_sources.read(self.db,Path(self.temp.name)/'config')
        self.assertEqual(lines[0][1],'user')
        scenes=organized_memory.for_conversations(self.db,[lines[0][0]])
        self.assertIn('MYN communication feedback',evolution.mining_prompt(lines,[],scenes))

    def test_duplicate_worker_delivery_does_not_start_two_requests(self):
        window=FakeWindow()
        window.session=self.session
        window.hear('麦恩，帮我看看', capture_id='duplicate0001')
        window.hear('麦恩，帮我看看', capture_id='duplicate0001')
        window._judge_heard.assert_called_once_with('帮我看看')
        self.assertEqual(window.addressed, [])
        window._judged('帮我看看', (True, False, False), window._judge_generation)
        self.assertEqual(len(window.addressed),1)
        self.assertEqual(len(conversation_log.list_messages(self.db)),1)

    def test_priority_delivery_still_groups_scenes_by_actual_speech_time(self):
        later=speech_journal.record(self.db,dict(self.capture(ident='later000001'),start=time.time()-1200,end=time.time()-1199))
        earlier=speech_journal.record(self.db,dict(self.capture(ident='earlier0001'),start=time.time()-3600,end=time.time()-3599))
        self.assertGreater(earlier,later)
        self.assertEqual(scene_memory.claim_scene(self.db)['rows'][0]['id'],earlier)


@unittest.skipIf(np is None,'Worker Python provides numpy')
class Stream(unittest.TestCase):
    def listener(self):
        self.events=[]
        self.transcribe=mock.Mock(return_value='ordinary background words')
        listener=stt_worker.Listener(None,np,self.transcribe,self.events.append,threaded=False,clock=lambda:100)
        listener.detector=SimpleNamespace(feed=lambda audio,rate:'',reset=lambda:None)
        return listener

    def test_wake_is_emitted_before_any_full_transcription(self):
        listener=self.listener()
        calls=[0]
        def detect(audio,rate):
            calls[0]+=1
            return 'Hey Mine' if calls[0]==18 else ''
        listener.detector.feed=detect
        for i in range(25):
            listener.feed(np.full(480,.08,np.float32),10+i*.03)
        self.assertEqual([e['event'] for e in self.events],['wake_detected'])
        self.transcribe.assert_not_called()
        for i in range(26):
            listener.feed(np.zeros(480,np.float32),10.75+i*.03)
        heard=[e for e in self.events if e['event']=='heard']
        self.assertEqual(len(heard),1)
        self.assertTrue(heard[0]['streaming_wake'])
        self.assertFalse(heard[0]['record_only'])
        self.assertIn('capture_id',heard[0])

    def test_background_speech_has_capture_id_and_no_live_action_permission(self):
        listener=self.listener()
        for i in range(30):
            listener.feed(np.full(480,.08,np.float32),10+i*.03)
        for i in range(26):
            listener.feed(np.zeros(480,np.float32),10.9+i*.03)
        heard=next(e for e in self.events if e['event']=='heard')
        self.assertTrue(heard['record_only'])
        self.assertFalse(heard['streaming_wake'])

    def test_live_transcription_precedes_backlog_and_overflow_is_reported(self):
        listener=self.listener()
        listener.pending=queue.PriorityQueue(maxsize=2)
        def item(passive,start):
            return (np.zeros(100),start,start+1,'en',.7,dict(record_only=passive,generation=0))
        listener._enqueue(item(True,1))
        listener._enqueue(item(True,2))
        listener._enqueue(item(False,3))
        self.assertEqual(listener.pending.get()[2][1],3)
        self.assertEqual(self.events[-1]['reason'],'transcription_backlog')

    def test_stale_inflight_transcript_never_executes_after_microphone_restart(self):
        listener=self.listener()
        def transcribe(*args):
            listener.generation+=1
            return 'Hey Mine delete something'
        listener.transcribe=transcribe
        listener._process((np.zeros(16000),10,11,'en',.7,dict(generation=0)))
        self.assertFalse(any(e['event']=='heard' for e in self.events))
        self.assertEqual(self.events[-1]['reason'],'input_restarted')


if __name__=='__main__':
    unittest.main()
