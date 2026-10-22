"""Resolve bundled interpreters without depending on a system Python install."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def worker_python(kind, root=None):
    root = Path(root or ROOT)
    bundled = root / 'runtime' / kind / 'python.exe'
    if bundled.is_file():
        return bundled
    legacy = 'local-agent' if kind == 'agent' else 'local-tts'
    return root / legacy / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def activate(root=None):
    """Expose private tools to child processes, without changing Windows settings."""
    root = Path(root or ROOT)
    git = root / 'runtime' / 'git'
    bash = git / 'bin' / 'bash.exe'
    if bash.is_file():
        os.environ['CLAUDE_CODE_GIT_BASH_PATH'] = str(bash)
        os.environ['PATH'] = str(git / 'cmd') + os.pathsep + os.environ.get('PATH', '')

