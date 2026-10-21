"""Background tasks run through the Claude Agent SDK.

Mixin composed by MYNWindow (myn_gui.py); shares its state and event queue."""

from gui_common import (  # noqa: F401
    Path,
    agent_client,
    config_for_db,
    local_actions,
    local_tools,
    time,
)


class AgentMixin:
    # ---- background tasks (Claude Agent SDK) ----------------------------------------

    agent_runner = None
    agent_log = None      # [(kind, text)] of the current / last task
    agent_window = None
    agent_task = ""
    agent_budget_run = None

    def _agent_status(self):
        return dict(running=bool(self.agent_runner and self.agent_runner.busy), task=self.agent_task,
                    log=[{"kind": kind, "text": text} for kind, text in (self.agent_log or [])[-80:]])

    def _agent_budget_check(self):
        run = self.agent_budget_run
        if not run or not self.agent_runner or not self.agent_runner.busy:
            return
        import evolution
        config = config_for_db(self.session.db_path)
        current = agent_client.load_settings(config)
        actions = local_actions.load_settings(config)
        cap_lowered = current.max_budget_usd > 0 and (run["task_limit"] == 0 or current.max_budget_usd < run["task_limit"])
        point = evolution.get(config, run.get("point_id"))
        if not actions.agent or cap_lowered or (point and point["status"] != "building"):
            self.agent_runner.cancel()

    AGENT_TOOLS = {"Bash": 'Run command', "Write": 'Write file', "Edit": 'Edit file', "MultiEdit": 'Edit file',
                   "NotebookEdit": 'Edit notebook', "Read": 'Read', "Glob": 'Find files', "Grep": 'Search content',
                   "WebSearch": 'Search the web', "WebFetch": 'Read webpage', "TodoWrite": 'Plan'}

    def _start_agent(self, task, folder, point_id=None):
        """Called from the chat thread by start_agent_task: start, or explain why not."""
        import evolution
        if (getattr(self, "self_fix", None) or getattr(self, "evolution_run", None)
                or getattr(self, "evolution_mining", None)
                or (self.agent_runner is not None and self.agent_runner.busy)):
            raise local_actions.ActionError('A task is already running. Wait for it to finish first.')
        if not agent_client.installed():
            raise local_actions.ActionError(
                'Background tasks are not installed. Run %s once in PowerShell, then restart MYN.' % agent_client.SETUP_SCRIPT)
        config = config_for_db(self.session.db_path)
        key = self.session._key()
        if not key:
            raise local_actions.ActionError('Configure a Claude API key first.')
        settings = agent_client.load_settings(config)
        configured = settings
        granted = local_tools.grants(config)
        roots = granted["read"] + granted["write"]
        if folder:
            context = local_actions.ActionContext(config)
            cwd = local_actions._granted_path(context, folder, roots)
            if not cwd.is_dir():
                raise local_actions.ActionError('This is not a folder.')
        else:
            cwd = agent_client.workspace_for(settings)
        if point_id is None:
            point, _ = evolution.add(config, task.split("\n")[0][:40], request=task,
                                     level=1, source="background")
            point_id = point["id"]
            evolution.update(config, point_id, 'Background task assigned to a level-one evolution task', execution="background",
                             task_prompt=task, task_folder=str(folder or ""), status="adopted", level=1)
        settings = evolution.runner_settings(config, configured, point_id=point_id, background=True)
        if settings is None:
            raise local_actions.ActionError('This level-one task has reached its per-task spending limit. Increase the limit to continue.')
        if self.agent_runner is None:
            self.agent_runner = agent_client.AgentRunner(lambda message: self.events.put(("agent", message)))
        task_id = "t%d" % int(time.time() * 1000)
        add_dirs = [str(Path(root)) for root in roots if Path(root).is_dir() and Path(root) != cwd]
        self.agent_budget_run = dict(task_id=task_id, task_limit=configured.max_budget_usd, point_id=point_id)
        evolution.update(config, point_id, 'Starting level-one background task', status="building")
        try:
            self.agent_runner.run(task_id, task, key, cwd, add_dirs, settings)
        except RuntimeError as error:
            self.agent_budget_run = None
            evolution.update(config, point_id, 'Could not start: %s' % error, status="failed")
            raise local_actions.ActionError(str(error)) from None
        self.events.put(("call", lambda: self._agent_started(task)))
        return {"started": True, "folder": str(cwd), "model": settings.model,
                "budget_usd": settings.max_budget_usd, "evolution_point": point_id, "level": 1}
