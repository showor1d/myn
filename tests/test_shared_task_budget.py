"""Conversation settings and the priority-one/shared-month/lifetime-task budget boundary."""
import json
import queue
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / 'src'), str(BASE / 'src/core')]
import agent_client
import evolution
import local_actions
import usage_stats
import stt


class SharedBudget(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='myn-shared-budget-')
        self.addCleanup(temp.cleanup)
        self.config = Path(temp.name)

    def spend(self, cost, point=None, purpose='evolution_build'):
        usage_stats.record_agent(self.config / usage_stats.USAGE_NAME, 'claude-opus-5-5',
                                 purpose, {'input_tokens': 100}, cost,
                                 task_key='evolution:' + point['id'] if point else '')

    def tool(self, **args):
        text, error = local_actions.execute('task_settings', args, local_actions.ActionContext(self.config))
        self.assertFalse(error, text)
        return json.loads(text)

    def test_settings_readback_preserves_model_workspace_steps_and_all_voice_fields(self):
        current = agent_client.AgentSettings('claude-opus-5-5', 10, 200, str(self.config))
        agent_client.save_settings(self.config, current)
        result = self.tool(agent_task_usd=15, evolution_task_usd=15, monthly_usd=50)
        self.assertEqual(agent_client.load_settings(self.config), replace(current, max_budget_usd=15))
        self.assertEqual((result['settings']['monthly_usd'], result['settings']['evolution_task_usd']), (50, 15))
        changed = self.tool(model='claude-opus-5-5', max_turns=80, workspace='', mining=False,
                            default_level=2, enabled=False, self_evolution_enabled=False)
        self.assertEqual(changed['settings']['max_turns'], 80)
        self.assertFalse(changed['settings']['mining'])
        # Settings stay available even when both execution groups are disabled.
        self.assertIn('task_settings', [t['name'] for t in local_actions.tools_for(local_actions.load_settings(self.config))])
        self.tool(enabled=True, self_evolution_enabled=True)
        self.assertTrue(local_actions.load_settings(self.config).self_fix)
        self.assertFalse(self.tool()['saved'])
        for text in ('看看后台任务设置', '后台任务改用Sonnet', '任务每次最多十五美元', '看看任务进度', '把月预算调到五十美元'):
            self.assertIsNone(stt.voice_command(text))

    def test_settings_validate_all_before_write_and_rollback_failed_multi_save(self):
        self.tool(monthly_usd=50)
        original = evolution.load_settings(self.config)
        for args in ({'monthly_usd': 99, 'max_turns': 1}, {'evolution_task_usd': True}, {'workspace': 'relative'}):
            _, failed = local_actions.execute('task_settings', args, local_actions.ActionContext(self.config))
            self.assertTrue(failed)
            self.assertEqual(evolution.load_settings(self.config), original)
        old_agent = agent_client.load_settings(self.config)
        with patch.object(evolution, 'save_settings', side_effect=OSError('disk failed')):
            _, failed = local_actions.execute('task_settings', {'monthly_usd': 60, 'agent_task_usd': 20},
                                               local_actions.ActionContext(self.config))
        self.assertTrue(failed)
        self.assertEqual(agent_client.load_settings(self.config), old_agent)

    def test_first_level_counts_but_runs_after_month_exhaustion(self):
        low, _ = evolution.add(self.config, '二级字幕改进', level=2)
        first, _ = evolution.add(self.config, '一级即时修复', level=1)
        self.spend(49, purpose='agent_task')  # historical background spend also counts
        self.spend(2, first, 'evolution_plan_l1')
        q = evolution.quota(self.config)
        self.assertEqual(q['used'], 51)
        self.assertFalse(q['can_start'])
        self.assertEqual(evolution.next_work(self.config)[0]['id'], first['id'])
        settings = agent_client.AgentSettings()
        self.assertEqual(evolution.runner_settings(self.config, settings, point_id=first['id']).max_budget_usd, 13)
        self.assertIsNone(evolution.runner_settings(self.config, settings, point_id=low['id']))
        evolution.pause_budget(self.config)  # an ordinary month stop must not stop priority one
        self.assertIsNotNone(evolution.runner_settings(self.config, settings, point_id=first['id']))
        self.spend(13, first, 'evolution_build_l1')
        self.assertIsNone(evolution.runner_settings(self.config, settings, point_id=first['id']))
        self.assertEqual(evolution.next_work(self.config), (None, None))

    def test_task_cap_spans_plan_build_retry_restart_and_month_rollover(self):
        first, _ = evolution.add(self.config, '测试进化任务', level=1)
        self.spend(3, first, 'evolution_plan_l1')
        self.spend(7, first, 'evolution_build_l1')
        self.assertEqual(evolution.runner_settings(self.config, agent_client.AgentSettings(), point_id=first['id']).max_budget_usd, 5)
        evolution.update(self.config, first['id'], status='failed')
        evolution.decide(self.config, first['id'], 'retry')
        evolution.recover(self.config)
        self.assertEqual(evolution.task_budget(self.config, first['id'])['used_usd'], 10)
        self.assertEqual(evolution.quota(self.config, datetime.now(timezone.utc) + timedelta(days=40))['used'], 0)
        self.assertEqual(evolution.task_budget(self.config, first['id'])['used_usd'], 10)
        evolution.pause_budget(self.config, first['id'])  # SDK may stop just below the configured cap
        self.assertFalse(evolution.task_budget(self.config, first['id'])['can_start'])
        self.assertFalse(evolution.quota(self.config)['budget_blocked'])
        self.tool(monthly_usd=500)
        self.assertFalse(evolution.task_budget(self.config, first['id'])['can_start'])
        self.tool(evolution_task_usd=20)
        self.assertEqual(evolution.runner_settings(self.config, agent_client.AgentSettings(), point_id=first['id']).max_budget_usd, 10)

    def window(self):
        import myn_gui
        win = myn_gui.MYNWindow.__new__(myn_gui.MYNWindow)
        win.session = SimpleNamespace(db_path=self.config / 'test.db', _key=lambda: 'fake-key')
        win._usage_config = lambda: self.config
        win.events = queue.Queue()
        win.agent_runner = Mock(busy=False)
        win._agent_log_add = Mock()
        win._background_notice = Mock()
        win.agent_budget_run = None
        agent_client.save_settings(self.config, agent_client.AgentSettings(workspace=str(self.config)))
        return win

    def test_background_is_level_one_point_actual_cost_completion_and_retry_limit(self):
        win = self.window()
        self.spend(60)
        with patch.object(agent_client, 'installed', return_value=True):
            receipt = win._start_agent('整理测试资料', '')
        point_id = receipt['evolution_point']
        point = evolution.get(self.config, point_id)
        self.assertEqual((point['level'], point['execution'], point['status']), (1, 'background', 'building'))
        self.assertEqual(win.agent_runner.run.call_args.args[5].max_budget_usd, 15)
        task_id = win.agent_budget_run['task_id']
        win._agent_event(dict(event='done', task_id=task_id, ok=False, error='error_max_budget_usd',
                             result='已完成一部分', model='claude-opus-5-5', cost_usd=15, usage={}))
        self.assertEqual(evolution.task_budget(self.config, point_id)['used_usd'], 15)
        self.assertEqual(evolution.get(self.config, point_id)['status'], 'failed')
        evolution.decide(self.config, point_id, 'retry')
        self.assertEqual(evolution.next_work(self.config), (None, None))
        self.tool(agent_task_usd=20)
        self.assertEqual(evolution.next_work(self.config)[1], 'background')
        with patch.object(agent_client, 'installed', return_value=True):
            win._start_agent('整理测试资料', '', point_id=point_id)
        self.assertEqual(win.agent_runner.run.call_args.args[5].max_budget_usd, 5)
        win._agent_event(dict(event='done', task_id=win.agent_budget_run['task_id'], ok=True,
                             result='整理完成', model='claude-opus-5-5', cost_usd=2, usage={}))
        self.assertEqual(evolution.get(self.config, point_id)['status'], 'completed')
        self.assertEqual(evolution.quota(self.config)['used'], 77)

    def test_interrupted_background_requires_explicit_retry_not_automatic_replay(self):
        point, _ = evolution.add(self.config, '整理工作文件', level=1, source='background')
        evolution.update(self.config, point['id'], execution='background', status='building')
        evolution.recover(self.config)
        self.assertEqual(evolution.get(self.config, point['id'])['status'], 'failed')
        self.assertEqual(evolution.next_work(self.config), (None, None))
        evolution.decide(self.config, point['id'], 'retry')
        self.assertEqual(evolution.next_work(self.config)[1], 'background')

    def test_spoken_settings_use_real_chat_tools_and_unattended_cannot_change_them(self):
        import chat
        import claude_client
        session = chat.ChatSession(self.config / 'test.db')
        receipts = []
        def model(*args, **kwargs):
            receipts.append(kwargs['tool_executor']('task_settings', {'monthly_usd': 55, 'max_turns': 90}))
            return '已设置。'
        with patch.object(session, '_optional_key', return_value='fake'), patch.object(claude_client, 'coordinated_reply', side_effect=model):
            session.ask('共享月预算改成55美元，后台任务最多90步', heard=True)
            self.assertFalse(receipts[-1][1])
            self.assertEqual(agent_client.load_settings(self.config).max_turns, 90)
            session._unattended = True
            session.ask('把月预算调高', heard=False)
            self.assertTrue(receipts[-1][1])
        self.assertEqual(evolution.load_settings(self.config).monthly_usd, 55)


if __name__ == '__main__':
    unittest.main()
