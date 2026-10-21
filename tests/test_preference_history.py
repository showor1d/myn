import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

BASE=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(BASE/'src'),str(BASE/'src/core')]
import chat
import conversation_log
import message_log
import preference_history
import preferences


class RawLearning(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(prefix='myn-style-history-')
        self.addCleanup(tmp.cleanup)
        self.db=Path(tmp.name)/'database/jarvis.db'
        self.db.parent.mkdir()
        self.session=chat.ChatSession(self.db)
        self.engine=self.session.preferences
        self.store=self.engine.store

    def source(self,text='以后闲聊请回答简短一点',role='user'):
        ident=(conversation_log.append_user(self.db,text,'test') if role=='user' else
               conversation_log.append_assistant(self.db,text,'test'))
        with closing(sqlite3.connect(str(self.db))) as db,db:
            db.execute("UPDATE conversation_messages SET created_at='2026-01-01T00:00:00+09:00' WHERE id=?",(ident,))
        return ident

    def candidate(self,ident,quote='回答简短一点'):
        return dict(dimension='verbosity',direction='less',scene='casual',basis='explicit',
                    evidence=[dict(id=ident,quote=quote)])

    def answer(self,item):
        return json.dumps(dict(candidates=[item]),ensure_ascii=False)

    def retry_now(self):
        with closing(self.store._connect()) as db,db:
            db.execute('UPDATE raw_learning_batches SET retry_at=NULL')

    def test_learns_original_once_and_new_conversations_incrementally(self):
        ident=self.source()
        self.source('明白了，我会简洁地回答。','assistant')
        ask=Mock(return_value=self.answer(self.candidate(ident)))
        self.assertEqual(preference_history.learn(self.engine,ask),1)
        self.assertEqual(len(self.store.rules()),1)
        rule=self.store.rules()[0]
        self.assertEqual(rule['scenes'],['casual'])
        self.assertEqual(rule['source'],'inferred')
        self.assertIn('回答简短一点',rule['note'])
        self.assertEqual(preference_history.learn(preferences.Engine(self.db),ask),0)
        self.assertEqual(self.store.rules()[0]['support'],1)
        ask.assert_called_once()
        other=self.source('这次也是，回答简短一点')
        ask.return_value=self.answer(self.candidate(other))
        self.assertEqual(preference_history.learn(self.engine,ask),1)
        self.assertEqual(self.store.rules()[0]['support'],2)

    def test_failed_commit_after_rule_does_not_relearn_or_recall_model(self):
        ident=self.source()
        ask=Mock(return_value=self.answer(self.candidate(ident)))
        real=self.store.add_rule
        def interrupted(*args,**kwargs):
            real(*args,**kwargs)
            raise OSError('simulated shutdown after rule commit')
        with patch.object(self.store,'add_rule',side_effect=interrupted):
            with self.assertRaises(OSError): preference_history.learn(self.engine,ask)
        self.retry_now()
        self.assertEqual(preference_history.learn(self.engine,ask),1)
        self.assertEqual(self.store.rules()[0]['support'],1)
        ask.assert_called_once()

    def test_manual_corrections_and_learning_pause_win_over_history(self):
        ident=self.source()
        rule=self.store.add_rule('闲聊也要详细解释','verbosity','more',['casual'],source='manual')
        ask=Mock(return_value=self.answer(self.candidate(ident)))
        with self.assertRaises(ValueError):
            self.store.set_setting('learning_paused',True)
        ask.assert_not_called()
        self.assertEqual(preference_history.learn(self.engine,ask),1)
        self.assertEqual(len(self.store.rules()),1)
        self.assertEqual(self.store.rule(rule)['direction'],'more')

    def test_fabricated_assistant_evidence_and_weak_pattern_rejected(self):
        ident=self.source()
        assistant=self.source('回答简短一点','assistant')
        bad=[self.candidate(ident,'不存在的话'),self.candidate(assistant),
             dict(self.candidate(ident),basis='pattern'),dict(self.candidate(ident),dimension='permissions')]
        for item in bad:
            with self.assertRaises(ValueError):
                preference_history.learn(self.engine,Mock(return_value=self.answer(item)))
            self.assertEqual(self.store.rules(),[])
            self.retry_now()
        self.assertEqual(preference_history.learn(self.engine,Mock(return_value='{"candidates":[]}')),1)

    def test_previously_learned_online_feedback_not_reinforced(self):
        text='以后闲聊请回答简短一点'
        ident=self.source(text)
        self.store.save_feedback(reason=text,natural_text=text,channel='natural')
        ask=Mock(return_value=self.answer(self.candidate(ident)))
        self.assertEqual(preference_history.learn(self.engine,ask),1)
        ask.assert_not_called()
        self.assertEqual(self.store.rules(),[])

    def test_long_originals_are_not_truncated_to_a_style_guess(self):
        text='背景信息。'*600+'以后回答简短一点'
        ident=self.source(text)
        ask=Mock(return_value=self.answer(self.candidate(ident)))
        preference_history.learn(self.engine,ask)
        payload=json.loads(ask.call_args[0][1])
        self.assertEqual(payload['lines'][0]['text'],text)

    def test_duplicate_candidate_and_concurrent_pass_do_not_reinforce(self):
        ident=self.source()
        item=self.candidate(ident)
        ask=Mock(return_value=json.dumps(dict(candidates=[item,item]),ensure_ascii=False))
        self.engine.history_learning_lock.acquire()
        try:
            self.assertEqual(preference_history.learn(self.engine,ask),0)
            ask.assert_not_called()
        finally:
            self.engine.history_learning_lock.release()
        self.assertEqual(preference_history.learn(self.engine,ask),1)
        self.assertEqual(self.store.rules()[0]['support'],1)

    def test_unaddressed_microphone_lines_are_context_not_style_evidence(self):
        message_log.record(self.db,'回答简短一点',channel='heard')
        with closing(sqlite3.connect(str(self.db))) as db,db:
            db.execute("UPDATE conversation_messages SET created_at='2026-01-01T00:00:00+09:00'")
        ask=Mock()
        # Shared scene indexing must settle before history-learning advances its cursor.
        self.assertEqual(preference_history.learn(self.engine,ask),0)
        with closing(sqlite3.connect(str(self.db))) as db,db:
            db.execute("UPDATE sent_messages SET status='processed'")
        self.assertEqual(preference_history.learn(self.engine,ask),1)
        ask.assert_not_called()
        self.assertEqual(self.store.rules(),[])


if __name__=='__main__': unittest.main()
