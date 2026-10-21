"""Voice memory management without changing the scene extraction policy."""
import json
import sys
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

BASE=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(BASE/'src'),str(BASE/'src/core')]
import chat
import claude_client
import conversation_log as history
import memory_tools
import storage


class MemoryTools(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='myn-memory-voice-')
        self.addCleanup(self.tmp.cleanup)
        self.db=Path(self.tmp.name)/'database/jarvis.db'
        self.db.parent.mkdir()
        self.session=chat.ChatSession(self.db)
        self.confirm=Mock(return_value=True)
        self.seen={}

    def run_tool(self, action, collection='history', **kwargs):
        text,error=memory_tools.execute(dict(action=action,collection=collection,**kwargs),self.db,self.confirm,self.seen)
        return json.loads(text),error

    def test_exact_time_order_pagination_and_role_originals(self):
        ids=[]
        for i in range(45):
            ident=history.append_user(self.db,'record %d'%i,'test')
            ids.append(ident)
            with closing(sqlite3.connect(str(self.db))) as conn,conn:
                conn.execute('UPDATE conversation_messages SET created_at=? WHERE id=?',
                    ('2026-10-20T23:00:%02d+00:00'%i,ident))
        args=dict(start_at='2026-10-21T08:00:00+09:00',end_at='2026-10-21T08:01:00+09:00')
        collected=[]
        for offset in (0,20,40):
            data,error=self.run_tool('list',offset=offset,**args)
            self.assertFalse(error)
            collected.extend(row['id'] for row in data['records'])
            self.assertEqual(data['more'],offset<40)
        self.assertEqual(collected,ids)
        self.assertFalse(self.run_tool('list',start_at='2026-10-21T09:00:00+09:00',end_at='2026-10-21T10:00:00+09:00')[0]['records'])
        self.assertTrue(self.run_tool('list',start_at='2026-10-21T08:00:00',end_at=args['end_at'])[1])
        data,error=self.run_tool('get',id=ids[-1])
        self.assertFalse(error)
        self.assertEqual(data['record']['original_text'],'record 44')
        self.assertEqual(data['record']['role'],'user')
        self.confirm.assert_not_called()

    def test_mutation_requires_read_approval_and_keeps_original(self):
        ident=history.append_user(self.db,'original words','test')
        self.assertTrue(self.run_tool('edit',id=ident,text='corrected words')[1])
        self.run_tool('get',id=ident)
        self.confirm.return_value=False
        self.assertTrue(self.run_tool('edit',id=ident,text='corrected words')[1])
        self.assertEqual(history.get(self.db,ident)['display_text'],'original words')
        self.confirm.return_value=True
        self.assertFalse(self.run_tool('edit',id=ident,text='corrected words')[1])
        record=self.run_tool('get',id=ident)[0]['record']
        self.assertTrue(record['revised'])
        self.assertEqual((record['original_text'],record['text']),('original words','corrected words'))
        self.assertEqual(self.run_tool('list',query='original words')[0]['records'][0]['id'],ident)
        self.assertFalse(self.run_tool('delete',id=ident)[1])
        self.assertEqual(self.run_tool('list')[0]['records'],[])
        self.assertEqual(self.run_tool('list',deleted=True)[0]['records'][0]['id'],ident)
        self.assertFalse(self.run_tool('restore',id=ident)[1])
        self.assertEqual(history.get(self.db,ident)['raw_text'],'original words')

    def test_changed_while_waiting_is_not_overwritten(self):
        ident=history.append_user(self.db,'original','test')
        self.run_tool('get',id=ident)
        def confirm(_):
            history.change(self.db,ident,'edit',expected='original',text='other edit')
            return True
        self.confirm.side_effect=confirm
        self.assertTrue(self.run_tool('edit',id=ident,text='my edit')[1])
        self.assertEqual(history.get(self.db,ident)['display_text'],'other edit')

    def test_derived_memory_filters_and_original_management_functions(self):
        ident=storage.save_memory('study tomorrow',self.db)
        with closing(sqlite3.connect(str(self.db))) as conn,conn:
            conn.execute('UPDATE memories SET category=?,memory_type=?,metadata=? WHERE id=?',
                ('study','goal',json.dumps({'kind':'inference','scene_id':3,'evidence_quotes':['maybe study tomorrow']}),ident))
        data,error=self.run_tool('list','memories',category='study',memory_type='goal')
        self.assertFalse(error)
        self.assertEqual(data['records'],[])
        self.assertEqual(self.run_tool('get','memories',id=ident)[0]['record']['kind'],'inference')
        self.assertFalse(self.run_tool('category','memories',id=ident,category='health')[1])
        self.assertEqual(self.run_tool('get','memories',id=ident)[0]['record']['evidence_quotes'],['maybe study tomorrow'])
        for action,kwargs in [('category',{'category':'life'}),('type',{'memory_type':'event'}),('edit',{'text':'corrected fact'}),('delete',{}),('restore',{})]:
            self.run_tool('get','memories',id=ident)
            self.assertFalse(self.run_tool(action,'memories',id=ident,**kwargs)[1],action)
        row=storage.get_memory(ident,self.db)
        self.assertEqual(row['raw_text'],'corrected fact')
        self.assertIsNone(row['deleted_at'])

    def test_spoken_request_exposes_recall_tools_without_a_menu_and_blocks_unattended_edits(self):
        ident=history.append_user(self.db,'buy a book next Sunday','test')
        self.session.confirm_action=self.confirm
        receipts=[]
        def model(*args,**kwargs):
            self.assertIn('memory_records',{t['name'] for t in kwargs['read_tools']})
            run=kwargs['tool_executor']
            receipts.append(run('memory_records',dict(action='get',collection='history',id=ident)))
            receipts.append(run('memory_records',dict(action='delete',collection='history',id=ident)))
            return 'done'
        with patch.object(self.session,'_optional_key',return_value='fake'),patch.object(claude_client,'coordinated_reply',side_effect=model):
            self.session._unattended=True
            self.session.ask('find my old note',heard=False)
            self.assertFalse(receipts[0][1])
            self.assertTrue(receipts[1][1])
            self.confirm.assert_not_called()
            receipts.clear()
            self.session._unattended=False
            self.session.ask('Please remove that previous note',heard=True)
            self.assertFalse(any(error for _,error in receipts))
            self.confirm.assert_called_once()

    def test_memory_answer_can_page_beyond_initial_retrieval_and_cite_original(self):
        first=history.append_user(self.db,'earlier record','test')
        target=history.append_user(self.db,'the precise original words','test')
        rows=[dict(history.get(self.db,first),confidence=1,confidence_kind='retrieval_relevance')]
        def model(*args,**kwargs):
            self.assertEqual(kwargs['read_tools'][0]['name'],'memory_records')
            data,error=kwargs['tool_executor']('memory_records',dict(action='get',collection='history',id=target))
            self.assertFalse(error)
            record=json.loads(data)['record']
            return json.dumps(dict(answer=record['original_text'],citations=[record['citation_id']],grounded=True))
        with patch.object(self.session,'_optional_key',return_value='fake'), \
                patch.object(chat,'internal_retrieve',return_value=[]), \
                patch.object(history,'search',return_value=rows), \
                patch.object(claude_client,'coordinated_reply',side_effect=model):
            result=self.session._rag('What did I say at that time?')
        self.assertEqual(result['answer'],'the precise original words')
        self.assertEqual(result['sources'][0]['id'],-target)


if __name__=='__main__':
    unittest.main()
