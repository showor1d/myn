"""Removed settings remain accessible through verified voice tools, on isolated data."""
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

BASE=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(BASE/'src'),str(BASE/'src/core')]
import chat
import claude_client
import personal_data_tools as personal
import scene_memory
import stt
import tts
import agent_client
import web_panels2
import sheets
from gui_personal_data import PersonalDataMixin


class PersonalVoice(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='myn-personal-voice-')
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.session=chat.ChatSession(self.root/'database/jarvis.db')
        self.store=self.session.preferences.store
        self.seen={}
        self.show=Mock()
        self.job=Mock()
        self.confirm=Mock(return_value=True)

    def call(self,action,**args):
        text,error=personal.execute(dict(action=action,**args),self.session,'修改资料并备份',self.confirm,
                                    self.seen,self.show,self.job)
        return json.loads(text),error

    def test_all_preserved_collections_read_and_display_without_mutations(self):
        self.session.record_input('昨天学了物理')
        scene_memory.save_profile(self.session.db_path,'喜欢物理','user')
        self.store.add_rule('先说结论','other','less',['all'],source='manual')
        for collection in personal.COLLECTIONS:
            result,error=self.call('list',collection=collection)
            self.assertFalse(error,result)
            self.assertTrue(result['displayed'])
            self.assertTrue(self.show.call_args[0][0])
        self.assertEqual(len(self.store.rules()),1)
        self.confirm.assert_not_called()

    def test_current_request_read_confirmation_and_changed_record_guards(self):
        ident=self.store.add_rule('少用列表','structure','less',['all'],source='manual')
        args=dict(collection='rules',id=ident,evidence='修改资料',text='先说结论')
        self.assertTrue(self.call('update_rule',**args)[1])
        self.call('get',collection='rules',id=ident)
        self.confirm.return_value=False
        self.assertTrue(self.call('update_rule',**args)[1])
        self.confirm.return_value=True
        self.assertFalse(self.call('update_rule',**args)[1])
        self.assertEqual(self.store.rule(ident)['text'],'先说结论')
        self.assertTrue(self.store.history())
        self.assertTrue(self.call('delete_rule',collection='rules',id=ident,evidence='旧消息的要求')[1])
        self.call('get',collection='rules',id=ident)
        self.confirm.side_effect=lambda text: self.store.update_rule(ident,text='并发修改') or True
        self.assertTrue(self.call('update_rule',**args)[1])
        self.assertEqual(self.store.rule(ident)['text'],'并发修改')

    def test_profile_versions_survive_edit_and_clear(self):
        scene_memory.save_profile(self.session.db_path,'旧画像','user')
        for value in ('新画像',''):
            self.call('get',collection='profile')
            result,error=self.call('save_profile',text=value,evidence='修改资料')
            self.assertFalse(error,result)
        rows=personal.read(self.session,'profile_history')['records']
        self.assertEqual([r['content'] for r in rows],['','新画像','旧画像'])

    def test_queued_backup_never_claims_it_has_finished_and_unattended_is_denied(self):
        result,error=self.call('complete_export',evidence='备份')
        self.assertFalse(error)
        self.assertTrue(result['queued'])
        self.job.assert_called_once_with('complete_export',{'file':None})
        self.assertNotIn('Completed',personal.finish([(result,error)]))
        _,error=personal.execute(dict(action='complete_import',evidence='备份'),self.session,'备份',
                                self.confirm,{},job=self.job,unattended=True)
        self.assertTrue(error)
        self.assertEqual(self.job.call_count,1)

    def test_legacy_disabled_values_are_overridden_without_erasing_configuration(self):
        configs=[(tts.SETTINGS_NAME,{'auto_read':False,'long_reply':'all','volume':.31}),
                 (stt.SETTINGS_NAME,{'enabled':False,'always_listen':False,'device':'Example microphone'}),
                 (agent_client.SETTINGS_NAME,{'model':'claude-sonnet-5-5','max_turns':77,'max_budget_usd':13,'workspace':'D:/Work'})]
        for name,value in configs:
            path=self.root/name
            path.write_text(json.dumps(value),encoding='utf-8')
            before=path.read_bytes()
            if name==tts.SETTINGS_NAME:
                current=tts.load_settings(self.root)
                self.assertEqual((current.auto_read,current.long_reply,current.volume),(True,'opening',.31))
            elif name==stt.SETTINGS_NAME:
                current=stt.load_settings(self.root)
                self.assertTrue(current.enabled and current.always_listen)
                self.assertEqual(current.device,'Example microphone')
            else:
                current=agent_client.load_settings(self.root)
                self.assertEqual((current.model,current.max_turns,current.max_budget_usd),('claude-opus-5-5',77,13))
            self.assertEqual(path.read_bytes(),before)
        with closing(self.store._connect()) as db,db:
            db.execute("UPDATE meta SET value='1' WHERE key='learning_paused'")
            db.execute("UPDATE meta SET value='0' WHERE key='apply_enabled'")
        self.assertFalse(self.store.learning_paused)
        self.assertTrue(self.store.apply_enabled)
        self.assertEqual(self.store.setting('learning_paused'),'1')
        self.assertEqual(claude_client.main_model(self.root),'claude-opus-5-5')

    def test_merged_agents_has_voice_and_key_without_removed_pages_or_switches(self):
        win=SimpleNamespace(tts_settings=tts.load_settings(self.root),stt_settings=stt.load_settings(self.root),
                            _usage_config=lambda:self.root,_stt_job=Mock())
        with patch.object(stt,'missing_parts',return_value=['test']),patch('tts_player.output_devices',return_value=[]):
            panel=web_panels2.AgentsPanel(win)
            nodes=panel.body()
        self.assertFalse(any(node['t']=='tabs' for node in nodes))
        text=json.dumps(nodes,ensure_ascii=False)
        for forbidden in ('暂停自动学习','聊天时使用这些偏好','自动朗读新回复','长回复只念开头一句','常开麦克风','read_path','main_model'):
            self.assertNotIn(forbidden,text)
        self.assertIn('Opus 5.5',text)
        self.assertEqual(set(web_panels2.PANELS),{'agents'})

    def test_restore_rechecks_busy_after_confirmation(self):
        win=PersonalDataMixin()
        win.session=self.session
        win.root=Mock()
        win._report_display=Mock()
        win._reconnect_session=Mock()
        import backup_ui
        import personal_profile
        with patch.object(backup_ui,'busy',side_effect=[False,True]), \
             patch('gui_personal_data.filedialog.askopenfilename',return_value='test.db'), \
             patch('gui_personal_data.messagebox.askyesno',return_value=True), \
             patch.object(personal_profile,'restore_backup') as restore:
            win._personal_job_when_idle('database_restore',{})
            restore.assert_not_called()
            self.assertIn('new task',win._report_display.call_args[0][0])

    def test_chat_uses_tool_and_displays_history_and_holds_backup_draft(self):
        self.session.show_report=self.show
        self.session.personal_job=self.job
        def provider(system,prompt,key,**kw):
            self.assertIsNone(kw['on_text'])
            self.assertIn('personal_data',[t['name'] for t in kw['read_tools']])
            content,error=kw['tool_executor']('personal_data',dict(action='list',collection='learning'))
            self.assertFalse(error,content)
            content,error=kw['tool_executor']('personal_data',dict(action='complete_export',evidence='备份'))
            self.assertFalse(error,content)
            return '已全部备份完成'
        with patch.object(self.session,'_key',return_value='test'),patch.object(claude_client,'coordinated_reply',side_effect=provider):
            reply=self.session.ask('查看偏好学习历史然后备份',on_text=Mock())
        self.assertIn('Select the file or confirm',reply['answer'])
        self.assertNotIn('已全部备份完成',reply['answer'])
        self.show.assert_called_once()


if __name__=='__main__': unittest.main()
