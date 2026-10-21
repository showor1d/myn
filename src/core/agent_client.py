"""MYN's side of background tasks: settings, and the agent_worker.py process.

The worker runs in local-agent\\.venv (Python 3.10+, `claude-agent-sdk`, installed by
setup_agent.ps1). One task at a time. Events arrive on another thread through
on_event(dict); MYN shows progress, asks the user when the worker asks, and reads
the result. The Claude key is handed to the worker in its environment only.
"""

import json
import os
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / "local-agent" / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
WORKER = Path(__file__).with_name("agent_worker.py")
SETUP_SCRIPT = ROOT / "setup_agent.ps1"
SETTINGS_NAME = "agent-settings.json"
MODELS = {"claude-opus-5-5": "Opus 5.5"}


@dataclass(frozen=True)
class AgentSettings:
    model: str = "claude-opus-5-5"
    # Per-task cap; the shared monthly budget still applies when this is 0 (unlimited).
    max_budget_usd: float = 15.0
    max_turns: int = 200  # Claude Code itself has no step limit; this only stops a runaway task
    workspace: str = ""  # Default task workspace; an empty value uses Documents/MYN Workspace.

    def validate(self):
        if self.model not in MODELS:
            raise ValueError('Invalid background task model.')
        if isinstance(self.max_budget_usd, bool) or not isinstance(self.max_budget_usd, (int, float)) \
                or not (self.max_budget_usd == 0 or 0.05 <= self.max_budget_usd <= 1000):
            raise ValueError('Task budget must be 0 (unlimited) or USD 0.05-1000.')
        if type(self.max_turns) is not int or not 3 <= self.max_turns <= 1000:
            raise ValueError('Step limit must be between 3 and 1000.')
        if not isinstance(self.workspace, str) or len(self.workspace) > 500:
            raise ValueError('Invalid working folder.')
        return self


def load_settings(config_dir):
    try:
        raw = json.loads((Path(config_dir) / SETTINGS_NAME).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw['model'] = 'claude-opus-5-5'
        return AgentSettings(**raw).validate() if isinstance(raw, dict) else AgentSettings()
    except (OSError, UnicodeError, ValueError, TypeError):
        return AgentSettings()


def save_settings(config_dir, settings):
    settings.validate()
    folder = Path(config_dir)
    folder.mkdir(parents=True, exist_ok=True)
    temporary = folder / (SETTINGS_NAME + ".tmp")
    temporary.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(str(temporary), str(folder / SETTINGS_NAME))


def default_workspace():
    return Path.home() / "Documents" / 'MYN Workspace'


def workspace_for(settings):
    path = Path(settings.workspace) if settings.workspace else default_workspace()
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def installed(python=PYTHON):
    """Whether setup_agent.ps1 has been run (the SDK is in local-agent\\.venv)."""
    python = Path(python)
    if not python.is_file():
        return False
    venv = python.parents[1]
    candidates = [venv / "Lib" / "site-packages" / "claude_agent_sdk"]
    candidates += list((venv / "lib").glob("python3*/site-packages/claude_agent_sdk")) if (venv / "lib").is_dir() else []
    return any(path.is_dir() for path in candidates)


class AgentRunner:
    """The worker process (started on first use, kept for later tasks)."""

    def __init__(self, on_event, command=None):
        self.on_event = on_event
        self.command = command or [str(PYTHON), "-u", str(WORKER)]
        self.process = None
        self.task_id = None  # the running task, or None
        self._lock = threading.Lock()

    @property
    def busy(self):
        return self.task_id is not None and self.alive

    @property
    def alive(self):
        return self.process is not None and self.process.poll() is None

    def _start(self, key):
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", ANTHROPIC_API_KEY=key)
        self.process = subprocess.Popen(
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            encoding="utf-8", errors="replace", bufsize=1, env=env, cwd=str(WORKER.parent),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        threading.Thread(target=self._read, args=(self.process,), daemon=True).start()

    def _read(self, process):
        for line in process.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("event") == "done" and message.get("task_id") == self.task_id:
                self.task_id = None
            try:
                self.on_event(message)
            except Exception:
                pass
        if self.task_id is not None:  # the worker died mid-task
            task_id, self.task_id = self.task_id, None
            self.on_event({"event": "done", "task_id": task_id, "ok": False, "result": "",
                           "error": 'Background task process exited unexpectedly.'})

    def _send(self, message):
        with self._lock:
            try:
                self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
                self.process.stdin.flush()
                return True
            except (OSError, ValueError, AttributeError):
                return False

    def run(self, task_id, prompt, key, cwd, add_dirs, settings, mode="", data_dir=None, token_budget=None):
        if self.busy:
            raise RuntimeError('A background task is already running.')
        if self.alive and getattr(self, "key", None) != key:
            self.close()  # the key changed: a fresh worker gets the new one
        if not self.alive:
            self._start(key)
            self.key = key
        self.task_id = task_id
        sent = self._send({"command": "run", "task_id": task_id, "prompt": prompt, "cwd": str(cwd),
                           "add_dirs": [str(d) for d in add_dirs], "model": settings.model,
                           "max_turns": settings.max_turns, "max_budget_usd": settings.max_budget_usd,
                           "mode": mode, "data_dir": str(data_dir) if data_dir else None,
                           "token_budget": token_budget})
        if not sent:
            self.task_id = None
            raise RuntimeError('Cannot start the background task process.')

    def answer(self, request_id, allow, message=""):
        self._send({"command": "permission", "request_id": request_id, "allow": bool(allow),
                    "message": message})

    def cancel(self):
        if self.alive:
            self._send({"command": "cancel"})

    def close(self):
        process, self.process = self.process, None
        if process is None:
            return
        try:
            process.stdin.close()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
