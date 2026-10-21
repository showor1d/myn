import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

BASE=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(BASE/'src'),str(BASE/'src/core')]
import chat
import claude_client
import conversation_log
import data_location
import preferences
import reporting as r
import report_sources as sources
from gui_reports import ReportsMixin


class Reporting(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(prefix='myn-reports-')
        self.addCleanup(tmp.cleanup)
        self.db=Path(tmp.name)/'database/jarvis.db'
        self.db.parent.mkdir()
        self.session=chat.ChatSession(self.db)
        self.config=data_location.config_for_db(self.db)
        self.now=datetime(2026,10,21,20,0,tzinfo=sources.TOKYO)

    def call(self, action, **args):
        text,error=r.execute(dict(action=action,**args),self.config,self.db,question='订阅 暂停 恢复 取消 等会 不要',now=self.now)
        return json.loads(text),error

    def sub(self, **args):
        fields=dict(action='subscribe',evidence='订阅',topics=['updates'],title='每周总结',
                    schedule=dict(repeat='weekly',time='21:00',days=[5]),lookback_days=7)
        fields.update(args)
        result,error=self.call(**fields)
        self.assertFalse(error,result)
        return result['subscription']

    def raw(self,text,at,role='user',deleted=None):
        with closing(sqlite3.connect(str(self.db))) as db,db:
            return db.execute('INSERT INTO conversation_messages(role,raw_text,display_text,created_at,deleted_at) VALUES(?,?,?,?,?)',
                              (role,text,text,at,deleted)).lastrowid

    def test_explicit_schedule_persistence_dedup_and_pause_resume_cancel(self):
        sub=self.sub()
        self.assertEqual(self.sub()['id'],sub['id'])
        self.assertIn('2026-10-24',sub['schedule']['next_run'])
        ident=sub['id']
        self.assertTrue(self.call('pause',id=ident,evidence='暂停')[0]['verified'])
        r.due(self.config,self.db,self.now+timedelta(days=10))
        self.assertEqual(self.call('list_reports')[0]['reports'],[])
        self.assertTrue(self.call('resume',id=ident,evidence='恢复')[0]['subscription']['enabled'])
        self.assertTrue(self.call('cancel',id=ident,evidence='取消')[0]['verified'])
        self.assertEqual(r.subscriptions(self.config),[])

    def test_missing_time_and_unattended_mutation_rejected(self):
        result,error=self.call('subscribe',schedule={'repeat':'weekly','days':[5]},evidence='订阅')
        self.assertTrue(error)
        self.assertEqual(r.subscriptions(self.config),[])
        text,error=r.execute(dict(action='subscribe',schedule={'repeat':'daily','time':'21:00'},evidence='订阅'),
                             self.config,self.db,question='订阅',unattended=True)
        self.assertTrue(error)
        for args in ({'action':'pause','evidence':'不存在'}, {'action':'subscribe','schedule':{'repeat':'interval','time':'21:00'},'evidence':'订阅'}):
            self.assertTrue(self.call(**args)[1])

    def test_due_newest_only_durable_once_and_fixed_range(self):
        sub=self.sub()
        late=datetime(2026,11,16,10,tzinfo=sources.TOKYO)
        r.due(self.config,self.db,late)
        r.due(self.config,self.db,late)
        reports=self.call('list_reports')[0]['reports']
        self.assertEqual(len(reports),1)
        report=r.get_report(self.config,reports[0]['id'])
        self.assertEqual(report['end_at'],'2026-11-14T21:00:00+09:00')
        self.assertEqual(report['start_at'],'2026-11-07T21:00:00+09:00')
        self.assertEqual(r.subscriptions(self.config)[0]['schedule']['next_run'],'2026-11-21T21:00')

    def test_pause_during_collection_cannot_publish_stale_report(self):
        sub=self.sub()
        original=r.make_report
        def collect(*args,**kwargs):
            self.call('pause',id=sub['id'],evidence='暂停')
            return original(*args,**kwargs)
        with patch.object(r,'make_report',side_effect=collect):
            r.due(self.config,self.db,self.now+timedelta(days=10))
        self.assertEqual(self.call('list_reports')[0]['reports'],[])
        self.assertFalse(r.subscriptions(self.config)[0]['enabled'])

    def test_collection_failure_preserves_due_for_retry(self):
        sub=self.sub()
        with patch.object(r,'make_report',side_effect=OSError('disk temporarily unavailable')):
            with self.assertRaises(OSError):
                r.due(self.config,self.db,self.now+timedelta(days=5))
        self.assertEqual(r.subscriptions(self.config)[0]['schedule']['next_run'],sub['schedule']['next_run'])

    def test_invitation_defer_show_and_restart_do_not_repeat(self):
        self.sub()
        later=self.now+timedelta(days=5)
        r.due(self.config,self.db,later)
        first=r.offer(self.config,later)
        self.assertIsNotNone(first)
        self.assertIsNone(r.offer(self.config,later+timedelta(days=1)))
        r.transition(self.config,first['id'],'deferred',later,minutes=120)
        self.assertIsNone(r.offer(self.config,later+timedelta(minutes=119)))
        self.assertEqual(r.offer(self.config,later+timedelta(minutes=120))['id'],first['id'])
        r.transition(self.config,first['id'],'shown',later+timedelta(minutes=121))
        self.assertIsNone(r.offer(self.config,later+timedelta(days=1)))

    def test_once_schedule_disables_after_preparing(self):
        self.sub(schedule={'repeat':'once','date':'2026-10-22','time':'12:00'})
        r.due(self.config,self.db,self.now+timedelta(days=2))
        self.assertFalse(r.subscriptions(self.config)[0]['enabled'])
        self.assertEqual(len(self.call('list_reports')[0]['reports']),1)

    def test_source_time_boundaries_originals_and_missing_data(self):
        self.raw('边界之前','2026-10-19T14:59:59+00:00')
        ident=self.raw('我计划明天学习数学','2026-10-19T15:00:00+00:00')
        self.raw('AI 猜测','2026-10-20T02:00:00+00:00','assistant')
        self.raw('已删除','2026-10-20T02:00:00+00:00',deleted='2026-10-20')
        self.raw('结束不包含','2026-10-20T15:00:00+00:00')
        report=r.make_report(self.config,self.db,dict(topics=['life'],start_at='2026-10-20',end_at='2026-10-21'),self.now)
        self.assertEqual(len(report['items']),1)
        self.assertEqual(report['items'][0]['source_id'],str(ident))
        self.assertEqual(report['items'][0]['status'],'user_statement')
        self.assertIn('Plans do not prove completion',''.join(report['coverage']))
        with patch.object(sources,'rows',side_effect=sqlite3.OperationalError('locked')):
            broken=r.make_report(self.config,self.db,dict(topics=['life']),self.now)
        self.assertIn('do not treat this as zero',''.join(broken['coverage']))

    def test_study_uses_source_message_id_not_conversation_row_id(self):
        import message_log
        import memory_evidence
        self.raw('无关对话','2026-10-20T00:00:00+09:00')
        source=message_log.record(self.db,'明天计划学习数学')
        # Source and conversation IDs deliberately differ because both roles are logged.
        ident=conversation_log.conversation_id_for_source(self.db,source)
        self.assertNotEqual(ident,source)
        with closing(sqlite3.connect(str(self.db))) as db,db:
            db.execute('UPDATE conversation_messages SET created_at=? WHERE id=?',('2026-10-20T00:00:00+09:00',ident))
            db.execute("INSERT INTO memory_evidence_links(scene_id,message_id,category,temporal_kind,event_at,time_text) VALUES(?,?,?,?,?,?)",
                       (101,source,'study','plan','','明天'))
        report=r.make_report(self.config,self.db,dict(topics=['study']),self.now)
        self.assertEqual([i['source_id'] for i in report['items']],[str(ident)])
        self.assertEqual(report['items'][0]['detail']['annotations'][0]['temporal_kind'],'plan')

    def test_spending_uses_usd_units_without_counting_token_twice(self):
        from types import SimpleNamespace
        import expenses
        import usage_stats
        call=SimpleNamespace(when=self.now-timedelta(hours=1),group='token',tokens=1000,cost=.125,priced=True)
        expense=dict(id='one',description='午餐',occurred_at=self.now.isoformat(),amount_minor=1000,currency='JPY',usd=670)
        with patch.object(expenses,'records',return_value=[expense]),patch.object(usage_stats,'load_calls',return_value=[call]):
            report=r.make_report(self.config,self.db,dict(topics=['expenses'],end_at=(self.now+timedelta(seconds=1)).isoformat()),self.now)
        self.assertEqual(report['totals']['cost_usd'],'6.82')
        self.assertEqual(report['totals']['tokens'],1000)
        self.assertEqual(len(report['items']),2)

    def test_report_followup_does_not_accept_an_old_evolution_offer(self):
        import interaction
        with patch.object(r,'active',return_value=True),patch.object(interaction,'response') as old_offer, \
                patch.object(self.session,'_general',return_value={'answer':'展示汇报','sources':[]}):
            result=self.session._dispatch('好的我看看',None,None)
        old_offer.assert_not_called()
        self.assertEqual(result['answer'],'展示汇报')

    def test_cancel_pending_report_and_no_false_success(self):
        sub=self.sub()
        r.due(self.config,self.db,self.now+timedelta(days=4))
        self.call('cancel',id=sub['id'],evidence='取消')
        self.assertIsNone(r.offer(self.config,self.now+timedelta(days=5)))
        self.assertIn('did not complete',r.finish('已经设置好了',[('subscribe',{'error':'磁盘写入失败'},True)],True))

    def test_developer_journal_distinguishes_prepared_and_startup_dates(self):
        note={'at':'2026-10-20T12:00:00+09:00','text':'修复音乐播放确认'}
        r.journal(self.config,note)
        with patch.object(r,'NOW',return_value=self.now):
            r.journal(self.config,note,applied=True)
            r.journal(self.config,note,applied=True)
        report=r.make_report(self.config,self.db,dict(topics=['updates'],start_at='2026-10-20',end_at='2026-10-21'),self.now)
        self.assertEqual([i['status'] for i in report['items']],['prepared'])
        report=r.make_report(self.config,self.db,dict(topics=['updates'],start_at='2026-10-21',end_at='2026-10-22'),self.now)
        self.assertEqual([i['status'] for i in report['items']],['applied'])

    def test_preferences_candidate_and_changed_rule_preserve_evidence(self):
        store=preferences.Store(preferences.folder_for_db(self.db))
        store.rules()
        old={'text':'少用列表','status':'candidate'}
        new={'text':'先说结论','status':'active'}
        with closing(store._connect()) as db,db:
            db.execute('INSERT INTO rule_history(rule_id,action,actor,at,before,after,note) VALUES(?,?,?,?,?,?,?)',
                       ('sample','update','user',self.now.isoformat(),json.dumps(old),json.dumps(new),'用户要求先说结论'))
        report=r.make_report(self.config,self.db,dict(topics=['preferences'],end_at=(self.now+timedelta(seconds=1)).isoformat()),self.now)
        self.assertEqual(len(report['items']),1)
        item=report['items'][0]
        self.assertEqual(item['detail']['before'],old)
        self.assertEqual(item['detail']['after'],new)
        self.assertTrue(item['title'].startswith('Modify'))

    def test_pagination_evidence_and_since_last_shown(self):
        for index in range(25):
            self.raw('学习原话 %d' % index,'2026-10-20T00:00:00+09:00')
        value,error=self.call('create',topics=['life'])
        self.assertFalse(error,value)
        self.assertEqual(value['next_offset'],20)
        self.assertEqual(len(value['items']),20)
        second,error=self.call('show',id=value['id'],offset=20)
        self.assertEqual(len(second['items']),5)
        detail,error=self.call('evidence',id=value['id'],number=25)
        self.assertEqual(detail['item']['detail']['original_text'],detail['item']['title'])
        later=r.make_report(self.config,self.db,dict(topics=['life'],since_last=True),self.now+timedelta(days=1))
        self.assertEqual(later['start_at'],self.now.isoformat())
        with self.assertRaisesRegex(ValueError,'these topics'):
            r.make_report(self.config,self.db,dict(topics=['updates'],since_last=True),self.now+timedelta(days=1))

    def test_gui_quiet_gate_and_single_brief_invitation(self):
        window=ReportsMixin()
        window._evolution_idle=Mock(return_value=False)
        window._speech_allowed=Mock(return_value=True)
        window.append=Mock(); window.speak=Mock()
        with patch.object(r,'offer',return_value={'title':'每周总结'}) as offered:
            window._report_announce(self.config)
            offered.assert_not_called()
            window._evolution_idle.return_value=True
            window._speech_allowed.return_value=False
            window._report_announce(self.config)
            offered.assert_not_called()
            window._speech_allowed.return_value=True
            window._report_announce(self.config)
            window.speak.assert_called_once()
            self.assertTrue(window.conversation_open)
            self.assertIn('good time to view',window.speak.call_args[0][0])

    def test_chat_tool_and_verified_subscription_receipt(self):
        question='每周六晚上九点向我汇报过去一周的更新'
        def model(*args,**kwargs):
            self.assertIn('myn_reports',[tool['name'] for tool in kwargs['read_tools']])
            text,error=kwargs['tool_executor']('myn_reports',dict(action='subscribe',topics=['updates'],
                schedule={'repeat':'weekly','days':[5],'time':'21:00'},lookback_days=7,evidence=question))
            self.assertFalse(error,text)
            return '随口说一个未核实的时间'
        with patch.object(self.session,'_optional_key',return_value='synthetic'),patch.object(claude_client,'coordinated_reply',side_effect=model):
            answer=self.session.ask(question,heard=False)['answer']
        self.assertIn('21:00',answer)
        self.assertIn('Saved subscription',answer)
        self.assertEqual(len(r.subscriptions(self.config)),1)

    def test_no_settings_ui_and_report_reader_flush(self):
        window=ReportsMixin(); window.root=Mock(); window.busy=True; window.draft_active=False
        window.web_active=True; window.web=Mock()
        window._report_display('汇报内容')
        window._report_flush()
        window.web.js.assert_not_called()
        window.busy=False
        window._report_flush()
        self.assertEqual(window.web.js.call_args[0][0]['action'],'report')
        self.assertEqual(window.web.js.call_args[0][0]['text'],'汇报内容')


if __name__=='__main__':
    unittest.main()
