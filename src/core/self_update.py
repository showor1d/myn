'Safe self-updates: prepare a source/test/document copy, isolate its data, edit only inside it, run tests against a trusted baseline, and check protected files and permanent constraints. An adopted plan authorizes automatic application when idle. Back up the current version, swap changed files and verify loading in a fresh Python process; roll back on failure. Startup must confirm success or the next start restores the backup. Personal data, models and runtimes never enter the copy. Only a user or authorized developer may edit this protection module and other protected files.'

import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROGRAM = ("src", "tests", "AGENTS.md", "tools/dev_finish.py")
SKIP = ("__pycache__", "*.pyc", "*.pyo")
# MYN may not change these itself: the safety net, keys, where personal data lives,
# backups, and the background-task worker that does the fixing.
PROTECTED = {
    "src/core/runtime_paths.py", "src/first_run.py", "src/install_components.py", "src/component-lock.json",
    "src/core/development_cleanup.py",
    "src/core/self_test_runner.py", "tools/dev_finish.py",
    "src/core/self_update.py", "src/core/credentials.py", "src/data_location.py",
    "src/core/backup.py", "src/core/agent_worker.py", "src/core/agent_client.py",
    "src/core/personal_profile.py", "src/version.py",
    "src/core/portable_backup.py", "src/backup_ui.py", "src/backup_restore_worker.py",
    "src/core/permission_control.py",
    "src/core/memory_tools.py",
    "src/core/memory_evidence.py", "src/core/preference_history.py",
    "src/core/reporting.py", "src/core/report_sources.py",
    "src/core/personal_data_tools.py", "src/gui_personal_data.py",
}
RULES_HEADING = '## Permanent constraints'
STATE_DIR = "self-update"
PENDING = "pending.json"
HISTORY = "history.jsonl"
NOTICE = "notice.json"
# The safety net itself must stay wired into the window code.
REQUIRED_CALLS = {"src/myn_gui.py": ("self_update.on_startup(", "self_update.confirm(")}
CONFIRM_AFTER_S = 25
MAX_FILES = 40


def state_dir(root=None, create=True):
    path = Path(root or ROOT) / STATE_DIR
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def _program_files(base):
    base = Path(base)
    found = {}
    for part in PROGRAM:
        path = base / part
        if path.is_file():
            found[part] = path
        elif path.is_dir():
            for item in path.rglob("*"):
                if item.is_file() and "__pycache__" not in item.parts and item.suffix not in (".pyc", ".pyo"):
                    found[item.relative_to(base).as_posix()] = item
    return found


BASE_NAME = ".myn-base.json"  # in a copy: what each program file was when the copy was made
CONFLICT = 'These files changed after the working copy was prepared; start again: '


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare(task_id, root=None):
    """A fresh copy of the program for the agent to work on, with a record of what it was
    copied from (so a copy applied much later changes only what it changed itself)."""
    root = Path(root or ROOT)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", str(task_id)):
        raise ValueError("Invalid self-evolution task id")
    work = state_dir(root) / "work" / task_id
    import development_cleanup
    development_cleanup.cancel_reused_copy(work, root)
    if work.exists():
        shutil.rmtree(work)
    for part in PROGRAM:
        source = Path(root) / part
        if source.is_dir():
            shutil.copytree(source, work / part, ignore=shutil.ignore_patterns(*SKIP))
        elif source.is_file():
            (work / part).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, work / part)
    base = {name: _digest(path) for name, path in _program_files(work).items()}
    (work / BASE_NAME).write_text(json.dumps(base), encoding="utf-8")
    # The authoritative baseline is outside the agent's writable copy. Editing the
    # explanatory copy of this manifest must never disguise a protected-file change.
    trusted = _base_path(work)
    trusted.parent.mkdir(parents=True, exist_ok=True)
    trusted.write_text(json.dumps(base), encoding="utf-8")
    return work


def _base_path(work):
    work = Path(work)
    return work.parent / ".bases" / (work.name + ".json")


def _base(work):
    try:
        base = json.loads(_base_path(work).read_text(encoding="utf-8"))
        return base if isinstance(base, dict) and all(isinstance(k, str) and isinstance(v, str)
                                                     for k, v in base.items()) else None
    except (OSError, ValueError):
        return None


def changes(work, root=None):
    """{relative path: "added" | "changed" | "removed"}: what the copy changed.

    With the record made by prepare(), only what differs from the program as it was copied
    counts: the live program may have moved on meanwhile (a Claude Code session, another
    evolution) and those newer files must not be put back to their old versions. Without it
    (an older copy), the copy is compared with the live program."""
    root = Path(root or ROOT)
    new = _program_files(work)
    base = _base(work)
    result = {}
    if base is not None:
        for name in sorted(set(base) | set(new)):
            if name not in new:
                result[name] = "removed"
            elif name not in base:
                result[name] = "added"
            elif _digest(new[name]) != base[name]:
                result[name] = "changed"
        return result
    live = _program_files(root)
    for name in sorted(set(live) | set(new)):
        if name not in new:
            result[name] = "removed"
        elif name not in live:
            result[name] = "added"
        elif live[name].read_bytes() != new[name].read_bytes():
            result[name] = "changed"
    return result


def conflicts(work, root=None):
    """Files the copy changed that were also changed in the live program since the copy
    was made: applying would undo those newer changes, so the work must be done again."""
    root = Path(root or ROOT)
    base = _base(work)
    if base is None:
        return []
    clashes = []
    for name, kind in changes(work, root).items():
        live = Path(root) / name
        before = base.get(name)
        now = _digest(live) if live.is_file() else None
        if now != before:  # added here but also created there, or changed there since
            clashes.append(name)
    return clashes


def live_changes(work, root=None):
    """Any program drift since the tests' baseline, including untouched dependencies."""
    base = _base(work)
    if base is None:
        return ['Working copy lacks a trusted baseline']
    live = _program_files(Path(root or ROOT))
    return sorted(name for name in set(base) | set(live)
                  if base.get(name) != (_digest(live[name]) if name in live else None))


def _rules(text):
    index = text.find(RULES_HEADING)
    return text[index:] if index >= 0 else ""


def check(work, root=None):
    """Problems that forbid applying this copy ([] = fine)."""
    root = Path(root or ROOT)
    problems = []
    if _base(work) is None:
        problems.append('Working copy lacks a trusted baseline. Develop and test again.')
    found = changes(work, root)
    if not found:
        problems.append('No changes.')
    if len(found) > MAX_FILES:
        problems.append('Too many changed files (%d).' % len(found))
    for name, kind in found.items():
        if name in PROTECTED:
            problems.append('Protected file cannot be changed: %s' % name)
        elif kind == "removed" and name.startswith("tests/"):
            problems.append('Tests cannot be deleted: %s' % name)
        elif kind == "removed":
            problems.append('Program files cannot be deleted: %s' % name)
        elif not re.match(r"^(src|tests)/[\w./ -]+$|^AGENTS\.md$", name):
            problems.append('Disallowed location: %s' % name)
    live_rules = _rules((Path(root) / "AGENTS.md").read_text(encoding="utf-8"))
    new_agents = Path(work) / "AGENTS.md"
    new_rules = _rules(new_agents.read_text(encoding="utf-8")) if new_agents.is_file() else ""
    if live_rules and new_rules != live_rules:
        problems.append('Permanent constraints in AGENTS.md cannot be changed.')
    for name, calls in REQUIRED_CALLS.items():
        target = Path(work) / name
        text = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
        if any(call not in text for call in calls):
            problems.append('Self-update protections cannot be removed (%s).' % name)
    for name, kind in found.items():
        if name.endswith(".py") and kind != "removed":
            source = (Path(work) / name).read_text(encoding="utf-8", errors="replace")
            try:
                compile(source, name, "exec")
            except SyntaxError as error:
                problems.append('Syntax error: %s, line %s' % (name, error.lineno))
    return problems


def diff_summary(work, root=None, limit=60):
    """A short +/- count per changed file and a few lines of the diff, for the log."""
    root = Path(root or ROOT)
    lines = []
    for name, kind in changes(work, root).items():
        old = (Path(root) / name).read_text(encoding="utf-8", errors="replace").splitlines() if kind != "added" else []
        new = (Path(work) / name).read_text(encoding="utf-8", errors="replace").splitlines() if kind != "removed" else []
        diff = list(difflib.unified_diff(old, new, lineterm="", n=0))
        plus = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
        minus = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
        lines.append("%s（%s，+%d −%d）" % (name, {"added": 'New', "changed": 'Modify', "removed": 'Delete'}[kind], plus, minus))
    return lines[:limit]


# ---- tests --------------------------------------------------------------------------

class TestFailures(set):
    """Failure IDs plus host-collected execution evidence, including successful runs."""
    def __init__(self, values=(), report=None):
        super().__init__(values)
        self.report = report or {}


def run_tests(base, python=None, timeout=900):
    """Run the unit tests of a program copy. Returns (failing test ids, output tail).
    Personal data folders point at an empty temporary place."""
    import tempfile
    python = str(python or sys.executable).replace("pythonw.exe", "python.exe")
    with tempfile.TemporaryDirectory(prefix="myn-selftest-") as scratch:
        env = dict({k: v for k, v in os.environ.items() if not k.upper().startswith("MYN_")},
                   MYN_PERSONAL_DATA_DIR=str(Path(scratch) / "p"),
                   MYN_DATA_DIR=str(Path(scratch) / "p" / "database"),
                   MYN_LEGACY_DATA_DIR=str(Path(scratch) / "l"), MYN_CONFIG_DIR=str(Path(scratch) / "c"),
                   PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ELEVENLABS_API_KEY", None)
        report_path = Path(scratch) / "results.json"
        try:
            result = subprocess.run([python, "-B", str(Path(__file__).with_name("self_test_runner.py")),
                                     str(Path(base).resolve()), str(report_path)],
                                    cwd=str(base), env=env, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=timeout,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            output = result.stdout + result.stderr
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if not report.get("complete") or not report.get("ran"):
                return TestFailures({'(Tests unavailable)'}, report), output[-6000:]
        except subprocess.TimeoutExpired:
            return TestFailures({'(Test timeout)'}), 'Tests did not finish within %d seconds.' % timeout
        except (OSError, ValueError, AttributeError) as error:
            return TestFailures({'(Tests unavailable)'}), 'Test execution evidence unavailable: %s' % type(error).__name__
    failing = TestFailures(report.get("failures", []), report)
    if not failing and (result.returncode != 0 or not re.search(r"^OK(?:\s|$)", output, re.M)):
        failing.add('(Tests unavailable)')
    return failing, output[-6000:]


def new_failures(baseline, after):
    broken = set(after) - set(baseline)
    broken.update(item for item in set(baseline) | set(after) if item.startswith("(Test"))
    before_report, after_report = getattr(baseline, "report", {}), getattr(after, "report", {})
    if before_report and after_report:
        missing = set(before_report.get("tests", [])) - set(after_report.get("tests", []))
        skipped = (set(after_report.get("skipped", [])) - set(before_report.get("skipped", []))) & set(before_report.get("tests", []))
        broken.update('Test removed or renamed: ' + name for name in missing)
        broken.update('Existing test newly skipped: ' + name for name in skipped)
    return sorted(broken)


def verification_summary(baseline, after):
    report = getattr(after, "report", {})
    return {"executed": bool(report.get("complete")), "ran": report.get("ran", 0),
            "skipped": len(report.get("skipped", [])), "failed": len(after),
            "baseline_failed": len(baseline), "accepted": not new_failures(baseline, after),
            "live_audio_checked": False, "checked_at": datetime.now(timezone.utc).isoformat()}


# ---- applying, restarting, rolling back ---------------------------------------------------

def self_check(base=None, python=None, timeout=120):
    """A fresh Python loads MYN's modules from `base` (without opening a window)."""
    python = python or sys.executable
    base = Path(base or ROOT)
    code = ("import sys; sys.path[:0]=[r'%s', r'%s']; import compileall, myn_gui; "
            "sys.exit(0 if compileall.compile_dir(r'%s', quiet=1, legacy=False) else 1)"
            % (Path(base) / "src", Path(base) / "src" / "core", Path(base) / "src"))
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    try:
        result = subprocess.run([str(python), "-c", code], cwd=str(base), capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout, env=env,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return False, 'Startup check timed out.'
    return result.returncode == 0, (result.stdout + result.stderr)[-2000:]


def _replace(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".myn-new")
    shutil.copy2(source, temporary)
    os.replace(str(temporary), str(target))


def apply(work, summary, root=None, checker=self_check, point_id=None):
    """Back up the live program, swap in the changed files, and check MYN still loads.
    Returns (ok, message). On a failed check everything is put back."""
    root = Path(root or ROOT)
    root = Path(root)
    if _pending_path(root).is_file():
        return False, 'The previous update has not confirmed startup. Try again later.'
    clashes = conflicts(work, root)
    if clashes:
        return False, CONFLICT + "、".join(clashes[:8])
    problems = check(work, root)
    if problems:
        return False, "；".join(problems)
    found = changes(work, root)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = state_dir(root) / "backups" / stamp
    for part in PROGRAM:
        source = root / part
        if source.is_dir():
            shutil.copytree(source, backup / part, ignore=shutil.ignore_patterns(*SKIP))
        elif source.is_file():
            (backup / part).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, backup / part)
    # Recheck after the potentially slow backup; preserve edits made while copying it.
    clashes = conflicts(work, root)
    if clashes:
        return False, CONFLICT + "、".join(clashes[:8])
    pending = {"backup": str(backup), "files": list(found), "summary": summary[:2000],
               "point_id": point_id,
               "applied_hashes": {name: _digest(Path(work) / name) for name in found},
               "applied_utc": datetime.now(timezone.utc).isoformat(), "boots": 1, "phase": "applying"}
    # A crash during replacement must also be recoverable at the next launch.
    _write_pending(root, pending)
    try:
        for name in found:
            _replace(Path(work) / name, root / name)
        ok, output = checker(root)
    except Exception as error:
        ok, output = False, 'File replacement or startup check failed: %s' % type(error).__name__
    if not ok:
        try:
            restore(backup, root, pending["files"], pending["applied_hashes"])
        except OSError:
            return False, 'Update incomplete. Recovery records were retained; the next startup will restore the old version.'
        _pending_path(root).unlink()
        _log(root, {"result": "load_check_failed", "files": list(found), "summary": summary[:500]})
        return False, 'The new version could not start. The previous version was restored.\n' + output[-500:]
    pending.update(boots=0, phase="ready")
    _write_pending(root, pending)
    try:
        import development_cleanup
        development_cleanup.schedule(work, backup, root)
    except (OSError, ValueError):
        _log(root, {"result": "cleanup_registration_deferred", "point_id": point_id})
    return True, str(backup)


def restore(backup, root=None, files=None, applied_hashes=None):
    """Put a backup of the program back (files added by the change are removed)."""
    root = Path(root or ROOT)
    root, backup = Path(root), Path(backup)
    saved = _program_files(backup)
    if files is not None:
        for name in files:
            target = root / name
            # A later developer edit belongs to the user, even when an update fails.
            expected = (applied_hashes or {}).get(name)
            if expected and (not target.is_file() or _digest(target) != expected):
                continue
            if name in saved:
                _replace(saved[name], target)
            elif target.is_file() and name not in PROTECTED:
                target.unlink()
        return
    for name in _program_files(root):
        if name not in saved and name not in PROTECTED:
            (root / name).unlink()
    for name, path in saved.items():
        _replace(path, root / name)


def _pending_path(root):
    return state_dir(root, create=False) / PENDING


def _write_pending(root, data):
    path = _pending_path(root)
    temporary = path.with_name(PENDING + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(temporary), str(path))


def _log(root, record):
    record = dict(record, at_utc=datetime.now(timezone.utc).isoformat())
    with (state_dir(root) / HISTORY).open("a", encoding="utf-8") as log:
        log.write(json.dumps(record, ensure_ascii=False) + "\n")


def on_startup(root=None):
    """Called first thing when MYN starts. Returns None, or ("rolled_back", summary) when
    the previous start of a new version never confirmed (it crashed) and the backup was
    put back, or ("trying", summary) for the first start of a new version."""
    root = Path(root or ROOT)
    path = _pending_path(root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        path.unlink()
        return None
    if data.get("boots", 0) >= 1:
        restore(data["backup"], root, data.get("files"), data.get("applied_hashes"))
        path.unlink()
        _log(root, {"result": "rolled_back", "point_id": data.get("point_id"),
                    "files": data.get("files"), "summary": data.get("summary", "")[:500]})
        _notice(root, 'The previous self-update did not run correctly. I restored the version from before the change.')
        return "rolled_back", data.get("summary", "")
    data["boots"] = data.get("boots", 0) + 1
    data["started"] = time.time()
    _write_pending(root, data)
    return "trying", data.get("summary", "")


def confirm(root=None):
    """The new version has run fine for a while (or closed normally): keep it."""
    root = Path(root or ROOT)
    path = _pending_path(root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    path.unlink()
    _log(root, {"result": "kept", "point_id": data.get("point_id"),
                "files": data.get("files"), "summary": data.get("summary", "")[:500]})
    return data.get("summary", "")


def evolution_results(root=None):
    """Latest installed/rolled-back outcome for each point, for its persisted UI state."""
    found = {}
    try:
        with (state_dir(root, create=False) / HISTORY).open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                    if isinstance(row.get("point_id"), str):
                        found[row["point_id"]] = row
                except (ValueError, AttributeError):
                    pass
    except FileNotFoundError:
        pass
    try:
        row = json.loads(_pending_path(root).read_text(encoding="utf-8"))
        if row.get("point_id") and row.get("phase") == "ready":
            found[row["point_id"]] = {"result": "trying", "at_utc": row.get("applied_utc", "")}
    except (OSError, ValueError, AttributeError):
        pass
    return found


def _notice(root, text):
    (state_dir(root) / NOTICE).write_text(json.dumps({"text": text}, ensure_ascii=False), encoding="utf-8")


def take_notice(root=None):
    """A message left for the next start (after a rollback), shown once."""
    root = Path(root or ROOT)
    path = state_dir(root, create=False) / NOTICE
    if not path.is_file():
        return ""
    try:
        text = json.loads(path.read_text(encoding="utf-8")).get("text", "")
    except (OSError, ValueError, AttributeError):
        text = ""
    path.unlink()
    return text


def restart_command():
    """How to start MYN again the same way it was started."""
    return [sys.executable] + list(sys.argv)


def discard(work):
    shutil.rmtree(work, ignore_errors=True)
    try:
        _base_path(work).unlink()
    except OSError:
        pass


def prompt_for(problem, work, rules, notes=""):
    """The task given to the agent for a self-fix round."""
    return (
        "You are MYN, updating your own program to complete the user's requested changes. The working directory is a copy of MYN "
        '(src contains Python 3.9 and Tk source, tests contains unit tests, and AGENTS.md contains current documentation and rules). Edit only this copy. '
        'Run commands inside the copy to verify changes. In PowerShell use & "' + sys.executable.replace("pythonw.exe", "python.exe") +
        '" -B -m unittest discover -s tests (MYN data paths point to temporary folders, away from real data). '
        'Commands using paths outside the copy are rejected. MYN runs the full suite again after you finish.\n\n'
        'User-reported problem:\n' + problem.strip() + "\n\n" +
        (('Additional information:\n' + notes.strip() + "\n\n") if notes.strip() else "") +
        'Requirements:\n'
        '1. Read the relevant AGENTS.md sections and source, find the actual cause, and make the smallest direct fix.\n'
        '2. Remain compatible with Python 3.9; add no dependencies requiring installation.\n'
        '3. Do not edit protected files: ' + "、".join(sorted(PROTECTED)) + '. Do not delete files or tests, '
        'and do not weaken, rename or skip existing tests to make them pass. Host verification compares executed test IDs and skips. '
        'tools/dev_finish.py is included for test imports only; do not execute it from the copy.\n'
        '4. Add a focused regression test when applicable, using temporary folders under tests/ and never real data.\n'
        'Reproduce the reported failure before fixing it, inspect callers and delayed callbacks, and test opposite cases. '
        'For speech, check direct requests, weak wakes, background transcription, echoes, quiet scenes, alarms and interruptions. '
        'When a tool refuses a test command, report the exact tool and reason; do not claim that tests passed.\n'
        '5. Update existing feature documentation and verification records in AGENTS.md; do not append version entries or sections. '
        'Record actual changes and test results briefly, disclose unperformed live-device checks, and preserve permanent constraints verbatim.\n'
        'Plan adoption authorizes backup, deployment and restart after tests pass. The main program performs those steps; do not deploy from the working copy.\n'
        '6. Follow AGENTS.md permanent constraints (excerpt below).\n\n' + rules[:6000] + "\n\n"
        'Finish with two or three speakable English sentences explaining the cause and change. If the cause is unknown or cannot be fixed, say so; do not make speculative changes.')
