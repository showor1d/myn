"""Background tasks: the worker (with a stand-in for claude-agent-sdk, which lives only
in local-agent\\.venv), the runner process protocol, settings and the chat tool."""

import json
import os
import queue
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src" / "core"), str(BASE / "tests")]

import agent_client
import local_actions

# A small stand-in for claude_agent_sdk with the same names and call shapes. Its client
# "reads" a file (no approval needed), then asks to Write one through can_use_tool.
FAKE_SDK = textwrap.dedent('''
    __version__ = "0.0-test"
    import asyncio

    class _Obj:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class types:
        class PermissionResultAllow(_Obj):
            pass

        class PermissionResultDeny(_Obj):
            pass

        class HookMatcher(_Obj):
            pass

    class ClaudeAgentOptions(_Obj):
        pass

    class TextBlock(_Obj):
        pass

    class ToolUseBlock(_Obj):
        pass

    class AssistantMessage(_Obj):
        pass

    class ResultMessage(_Obj):
        pass

    LAST = {}

    class ClaudeSDKClient:
        def __init__(self, options):
            self.options = options
            LAST["options"] = options

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def query(self, prompt):
            self.prompt = prompt

        async def interrupt(self):
            LAST["interrupted"] = True

        async def receive_response(self):
            yield AssistantMessage(content=[TextBlock(text="我先看看文件夹。"),
                                            ToolUseBlock(name="Read", input={"file_path": "C:/w/a.txt"})])
            if "slow" in self.prompt:
                await asyncio.sleep(30)
            decision = await self.options.can_use_tool(
                "Write", {"file_path": "C:/w/report.md", "content": "# 报告"}, None)
            if isinstance(decision, types.PermissionResultAllow):
                yield AssistantMessage(content=[ToolUseBlock(name="Write", input=decision.updated_input)])
                yield ResultMessage(subtype="success", is_error=False, result="写好了报告。\\n\\n路径：C:/w/report.md",
                                    total_cost_usd=0.12, num_turns=3)
            else:
                yield ResultMessage(subtype="success", is_error=False, result="你没有同意写文件，我停下了。",
                                    total_cost_usd=0.05, num_turns=2)
''')


class FakeSdkDir:
    def __init__(self):
        self.folder = tempfile.TemporaryDirectory(prefix="fake-sdk-")
        (Path(self.folder.name) / "claude_agent_sdk.py").write_text(FAKE_SDK, encoding="utf-8")

    @property
    def path(self):
        return self.folder.name


class WorkerProcess(unittest.TestCase):
    """The real agent_worker.py in a subprocess, through AgentRunner, with the fake SDK."""

    def setUp(self):
        self.fake = FakeSdkDir()
        self.addCleanup(self.fake.folder.cleanup)
        self.events = queue.Queue()
        patcher = mock.patch.dict(os.environ, {"PYTHONPATH": self.fake.path})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.runner = agent_client.AgentRunner(self.events.put,
                                               command=[sys.executable, "-u", str(agent_client.WORKER)])
        self.addCleanup(self.runner.close)

    def next_event(self, kind, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                event = self.events.get(timeout=0.5)
            except queue.Empty:
                continue
            if event.get("event") == kind:
                return event
        self.fail("no %s event" % kind)

    def test_task_runs_asks_before_writing_and_reports(self):
        settings = agent_client.AgentSettings(max_budget_usd=1.5)
        self.runner.run("t1", "写一份报告", "secret-key", "C:/w", ["C:/docs"], settings)
        self.assertTrue(self.runner.busy)
        self.assertEqual(self.next_event("ready")["sdk"], "0.0-test")
        self.assertEqual(self.next_event("text")["text"], "我先看看文件夹。")
        self.assertEqual(self.next_event("tool"), {"event": "tool", "task_id": "t1", "name": "Read",
                                                   "summary": "C:/w/a.txt"})
        ask = self.next_event("ask")
        self.assertEqual((ask["tool"], ask["summary"]), ("Write", "C:/w/report.md"))
        self.assertIn("# 报告", ask["detail"])
        with self.assertRaises(RuntimeError):  # one task at a time
            self.runner.run("t2", "另一个", "secret-key", "C:/w", [], settings)
        self.runner.answer(ask["request_id"], True)
        done = self.next_event("done")
        self.assertTrue(done["ok"])
        self.assertEqual((done["cost_usd"], done["turns"]), (0.12, 3))
        self.assertTrue(done["result"].startswith("写好了报告"))
        self.assertFalse(self.runner.busy)
        # a second task reuses the worker; this time the user says no
        self.runner.run("t3", "再写一份", "secret-key", "C:/w", [], settings)
        self.runner.answer(self.next_event("ask")["request_id"], False)
        self.assertIn('没有同意', self.next_event("done")["result"])

    def test_cancel_stops_a_task(self):
        self.runner.run("t9", "slow job", "k", "C:/w", [], agent_client.AgentSettings())
        self.next_event("tool")
        self.runner.cancel()
        done = self.next_event("done")
        self.assertFalse(done["ok"])
        self.assertEqual(done["error"], "cancelled")

    def test_missing_sdk_is_reported(self):
        broken = tempfile.TemporaryDirectory(prefix="no-sdk-")
        self.addCleanup(broken.cleanup)
        (Path(broken.name) / "claude_agent_sdk.py").write_text("raise ImportError('not installed')\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"PYTHONPATH": broken.name}):
            runner = agent_client.AgentRunner(self.events.put,
                                              command=[sys.executable, "-u", str(agent_client.WORKER)])
            self.addCleanup(runner.close)
            runner.run("t1", "x", "k", "C:/w", [], agent_client.AgentSettings())
            self.assertIn("claude-agent-sdk", self.next_event("unavailable")["error"])
            done = self.next_event("done")  # the runner notices the worker is gone
            self.assertFalse(done["ok"])


class WorkerOptions(unittest.TestCase):
    """What the worker tells the SDK: the permission levels and limits."""

    def test_options_follow_the_users_levels(self):
        import asyncio
        import importlib.util
        fake = FakeSdkDir()
        self.addCleanup(fake.folder.cleanup)
        spec = importlib.util.spec_from_file_location("claude_agent_sdk", Path(fake.path) / "claude_agent_sdk.py")
        sdk = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sdk)
        import agent_worker
        import threading
        emitted = []
        asked = threading.Event()

        def stdin():
            yield json.dumps({"command": "run", "task_id": "a", "prompt": "做事", "cwd": "C:/w",
                              "add_dirs": ["C:/d"], "model": "claude-sonnet-5-5", "max_turns": 12,
                              "max_budget_usd": 0.7}) + "\n"
            # MYN goes away mid-task: stdin ends only once the task is running and has asked
            # something (ending it at once could cancel the task before it even started).
            asked.wait(5)
        lines = stdin()

        def emit(message):
            emitted.append(message)
            if message.get("event") == "ask":
                worker_answer(message["request_id"])
                asked.set()

        answers = []

        def worker_answer(request_id):
            answers.append(request_id)

        async def go():
            with mock.patch.object(agent_worker, "emit", emit):
                task = asyncio.ensure_future(agent_worker.serve(sdk, lines))
                for _ in range(100):
                    await asyncio.sleep(0.02)
                    if answers:
                        break
                # answer through the same path MYN uses: a permission line is not available
                # here (lines is exhausted), so check the options and stop.
                await asyncio.wait_for(task, 5)

        asyncio.run(go())
        options = sdk.LAST["options"]
        self.assertEqual(options.permission_mode, "default")
        self.assertEqual(options.allowed_tools, ["Read", "Glob", "Grep", "WebSearch", "WebFetch", "TodoWrite"])
        self.assertNotIn("Write", options.allowed_tools)
        self.assertNotIn("Bash", options.allowed_tools)
        self.assertEqual(options.disallowed_tools, ["AskUserQuestion"])
        self.assertEqual(options.setting_sources, [])
        self.assertEqual((options.cwd, options.add_dirs, options.max_turns, options.max_budget_usd),
                         ("C:/w", ["C:/d"], 12, 0.7))
        self.assertIn("Chinese", options.system_prompt["append"])
        # MYN went away mid-task (stdin closed): the question is dropped and the task stops
        done = [m for m in emitted if m.get("event") == "done"][-1]
        self.assertEqual((done["ok"], done["error"]), (False, "cancelled"))


class SettingsAndTool(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="myn-agent-")
        self.addCleanup(temporary.cleanup)
        self.config = Path(temporary.name)

    def test_settings(self):
        self.assertEqual(agent_client.load_settings(self.config), agent_client.AgentSettings())
        agent_client.save_settings(self.config, agent_client.AgentSettings("claude-opus-5-5", 5.0, 40, "D:/work"))
        self.assertEqual(agent_client.load_settings(self.config).model, "claude-opus-5-5")
        # 0 = no cost cap (the default since 2026-10-18); negative or absurd caps are refused
        self.assertEqual(agent_client.AgentSettings().max_budget_usd, 15)
        agent_client.save_settings(self.config, agent_client.AgentSettings(max_budget_usd=0))
        for bad in (agent_client.AgentSettings("gpt", 1.0), agent_client.AgentSettings(max_budget_usd=-1),
                    agent_client.AgentSettings(max_budget_usd=0.01),
                    agent_client.AgentSettings(max_budget_usd=5000), agent_client.AgentSettings(max_turns=1)):
            with self.assertRaises(ValueError):
                agent_client.save_settings(self.config, bad)
        (self.config / agent_client.SETTINGS_NAME).write_text("{", encoding="utf-8")
        self.assertEqual(agent_client.load_settings(self.config), agent_client.AgentSettings())

    def test_installed_check(self):
        venv = self.config / ".venv"
        python = venv / "Scripts" / "python.exe"
        self.assertFalse(agent_client.installed(python))
        python.parent.mkdir(parents=True)
        python.write_text("", encoding="utf-8")
        self.assertFalse(agent_client.installed(python))
        (venv / "Lib" / "site-packages" / "claude_agent_sdk").mkdir(parents=True)
        self.assertTrue(agent_client.installed(python))

    def test_chat_tool_starts_a_task_through_the_window(self):
        started = []
        context = local_actions.ActionContext(self.config, start_agent=lambda task, folder: (
            started.append((task, folder)), {"started": True, "folder": "C:/w"})[1])
        content, error = local_actions.execute("start_agent_task", {"task": "把下载文件夹按月份整理"}, context)
        self.assertFalse(error)
        self.assertEqual(json.loads(content)["folder"], "C:/w")
        self.assertEqual(started, [("把下载文件夹按月份整理", None)])
        self.assertEqual(local_actions.LEVELS["start_agent_task"], "direct")
        _, error = local_actions.execute("start_agent_task", {"task": "短"}, context)
        self.assertTrue(error)
        content, error = local_actions.execute("start_agent_task", {"task": "整理一下文件夹"},
                                               local_actions.ActionContext(self.config))
        self.assertTrue(error)
        local_actions.save_settings(self.config, local_actions.ActionSettings(agent=False))
        self.assertIn("start_agent_task",
                         [tool["name"] for tool in local_actions.tools_for(local_actions.load_settings(self.config))])


if __name__ == "__main__":
    unittest.main()
