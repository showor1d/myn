import json
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(BASE/'src'),str(BASE/'src/core')]
import chat
import conversation_log
import memory_evidence
import memory_processor
import memory_tools
import message_log
import scene_memory
import semantic_search
import storage


class Evidence(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(prefix='myn-evidence-')
        self.addCleanup(tmp.cleanup)
        self.db=Path(tmp.name)/'database/jarvis.db'
        self.db.parent.mkdir()
        self.session=chat.ChatSession(self.db)

    def source(self,text):
        ident=message_log.record(self.db,text)
        stamp=(datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat()
        with closing(sqlite3.connect(str(self.db))) as conn,conn:
            conn.execute('UPDATE sent_messages SET sent_at=? WHERE id=?',(stamp,ident))
        return ident

    def test_original_multi_category_plan_time_and_separate_inference(self):
        text='我打算明天八点学习数学，也想锻炼，但不确定能不能做到。'
        ident=self.source(text)
        claimed=scene_memory.claim_scene(self.db)
        payload=dict(scene=dict(summary='计划学习和运动',setting='',speakers=['用户']),
            topics=[dict(evidence=[ident],categories=['study','fitness'])],memories=[
                dict(kind='fact',type='goal',category='study',content='打算学习',why='',evidence=[ident],confidence=1,
                     temporal_kind='plan',event_at='2026-10-22T08:00:00+09:00',time_text='明天八点'),
                dict(kind='inference',type='profile',category='study',content='可能重视自律',why='提出学习计划',
                     evidence=[ident],confidence=.4)])
        result=memory_processor.validate_scene(json.dumps(payload,ensure_ascii=False),claimed['rows'])
        scene_memory.complete_scene(self.db,claimed,result)
        facts=storage.recent_memories(self.db)
        self.assertEqual(len(facts),1)
        self.assertEqual(facts[0]['raw_text'],text)
        self.assertEqual(message_log.get(self.db,ident)['raw_text'],text)
        meta=json.loads(facts[0]['metadata'])
        self.assertEqual(meta['temporal_kind'],'plan')
        self.assertNotEqual(facts[0]['created_at'],meta['event_at'])
        content,error=memory_tools.execute(dict(action='list',collection='history',category='study',
            time_basis='event',start_at='2026-10-22T00:00:00+09:00',end_at='2026-10-23T00:00:00+09:00',
            temporal_kind='plan'),self.db,None,{})
        self.assertFalse(error)
        row=json.loads(content)['records'][0]
        self.assertEqual(row['text'],text)
        self.assertIn('fitness',[a['category'] for a in row['scene_annotations']])
        self.assertEqual(len(memory_evidence.analyses(self.db)),1)
        self.assertNotIn('可能重视自律',[r['raw_text'] for r in semantic_search._active_rows(self.db)])
        content,error=memory_tools.execute(dict(action='delete',collection='analysis',id=1),self.db,lambda _:True,{})
        self.assertTrue(error)

    def test_time_requires_verbatim_evidence_and_zone(self):
        ident=self.source('我昨天学习了')
        rows={ident:message_log.get(self.db,ident)}
        base=dict(type='event',evidence=[ident],temporal_kind='reported_event')
        self.assertIsNone(memory_evidence.temporal(base,rows)['event_at'])
        for extra in [dict(event_at='2026-10-20T08:00:00+09:00'),
                      dict(event_at='2026-10-20T08:00:00',time_text='昨天'),
                      dict(time_text='明天')]:
            with self.assertRaises(ValueError):
                memory_evidence.temporal(dict(base,**extra),rows)

    def test_plan_recall_keeps_calendar_and_original_history_tools_available(self):
        with patch.object(self.session,'_general',return_value={'answer':'calendar and history'}) as general, \
                patch.object(self.session,'_rag') as memory_only:
            self.session.ask('我2026年10月22日有什么计划？')
        general.assert_called_once()
        memory_only.assert_not_called()

    def test_legacy_migration_is_additive_idempotent_and_excludes_inference(self):
        ident=self.source('我昨晚睡得晚')
        with closing(sqlite3.connect(str(self.db))) as db,db:
            for table in ('memory_evidence_meta','memory_evidence_links','memory_analyses'):
                db.execute('DROP TABLE '+table)
            old=db.execute('''INSERT INTO memories(content,raw_text,category,memory_type,metadata,created_at)
                VALUES(?,?,?,?,?,?)''',('容易焦虑','（推断）容易焦虑','health','profile',
                json.dumps(dict(kind='inference',evidence_message_ids=[ident],evidence_quotes=['我昨晚睡得晚'])),
                '2026-10-21T00:00:00+09:00')).lastrowid
        before=storage.get_memory(old,self.db)
        memory_evidence.ensure_schema(self.db)
        memory_evidence.ensure_schema(self.db)
        self.assertEqual(before,storage.get_memory(old,self.db))
        self.assertEqual(len(memory_evidence.analyses(self.db)),1)
        self.assertEqual(storage.recent_memories(self.db),[])
        self.assertEqual(storage.search_memories('焦虑',self.db),[])
        self.assertEqual(semantic_search._active_rows(self.db),[])
        self.assertTrue(any(self.db.parent.parent.rglob('*pre-memory-evidence*')))


if __name__=='__main__': unittest.main()
