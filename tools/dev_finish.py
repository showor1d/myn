"""Run when development finishes on D:\\MYN (Claude Code Stop hook or manual Codex run).

2026-10-18 the user: count Claude's development tokens in MYN every time, and after each
task reopen MYN and have it say "Task completed / Changes completed".

1. Count Claude Code's work and the current registered Codex session in MYN statistics.
2. If MYN's files changed since the last run (or since the running MYN started), leave
   self-update\\dev-done.json with a short note taken from Claude's last message.
3. MYN running: it restarts itself onto the new code as soon as it is idle (or, when only
   tests/docs changed, just says it). A MYN too old to watch for the note is asked to
   close normally and started again. MYN not running: it is started.

Reads Claude Code's hook input (JSON on stdin) for the transcript; never fails the hook.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "core")]
STATE = ROOT / "self-update" / "dev-state.json"
DONE = ROOT / "self-update" / "dev-done.json"
PYTHONW = Path(r"C:\Program Files\Python39\pythonw.exe")
GUI = ROOT / "src" / "myn_gui.py"
WATCHED = ("src", "tests")
SKIP_PARTS = {"__pycache__", ".pytest_cache"}


def log(message):
    print("[dev_finish] " + message)


def newest(paths):
    stamp = 0.0
    for base in paths:
        base = ROOT / base
        files = [base] if base.is_file() else base.rglob("*") if base.is_dir() else []
        for path in files:
            if path.is_file() and not SKIP_PARTS & set(path.parts):
                stamp = max(stamp, path.stat().st_mtime)
    return stamp


def myn_processes():
    """[(pid, start time as epoch)] of the user's MYN (pythonw running myn_gui.py)."""
    script = ("Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | "
              "Where-Object { $_.CommandLine -like '*myn_gui.py*' } | ForEach-Object { "
              "'{0} {1}' -f $_.ProcessId, $_.CreationDate.ToUniversalTime().ToString('o') }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True,
                             text=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit():
            try:
                found.append((int(parts[0]), datetime.fromisoformat(parts[1].replace("Z", "+00:00")[:26] +
                                                                  "+00:00").timestamp()))
            except ValueError:
                found.append((int(parts[0]), 0.0))
    return found


def last_message(hook):
    """Claude's final words this turn (plain text), for the note."""
    text = hook.get("last_assistant_message")
    if isinstance(text, str) and text.strip():
        return text
    path = hook.get("transcript_path")
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, TypeError):
        return ""
    for line in reversed(lines[-400:]):
        try:
            item = json.loads(line)
        except ValueError:
            continue
        message = item.get("message") if isinstance(item, dict) else None
        if item.get("type") != "assistant" or not isinstance(message, dict):
            continue
        parts = [block.get("text", "") for block in message.get("content") or []
                 if isinstance(block, dict) and block.get("type") == "text"]
        if any(part.strip() for part in parts):
            return "\n".join(parts)
    return ""


def plain(text):
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*_`#>|]", "", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def note_for(message, program_changed):
    head = "Changes completed." if program_changed else "Task completed."
    body = plain(message)
    # Callers may already include the completion heading; add it only once.
    body = re.sub(r"^(?:(?:(?:Changes|Task) completed|(?:修改|任务)已完成)(?:[。.!！]+|\s+|$)\s*)+", "", body)
    first = body.split("\n\n")[0].strip() if body else ""
    first = first[:300] + ("…" if len(first) > 300 else "")
    sentence = re.split(r"(?<=[。！？!?])", first)[0][:90] if first else ""
    return {"text": head + (("\n\n" + first) if first else ""),
            "spoken": head + sentence,
            "restart": program_changed, "at": datetime.now(timezone.utc).isoformat()}


def start_myn():
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    env = {k: v for k, v in os.environ.items() if not k.startswith("MYN_")}  # the real data, as the shortcut does
    subprocess.Popen([str(PYTHONW), str(GUI)], cwd=str(ROOT), creationflags=flags, close_fds=True, env=env)


def read_hook(stream):
    """Claude Code's hook input. It arrives as UTF-8; Python 3.9 on Windows would decode
    stdin as GBK and lose every Chinese character, so the bytes are decoded here."""
    raw = getattr(stream, "buffer", stream).read()
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    try:
        hook = json.loads(text.lstrip("﻿") or "{}")
    except ValueError:
        return {}
    return hook if isinstance(hook, dict) else {}


def main():
    for stream in (sys.stdout, sys.stderr):  # Claude Code reads the output as UTF-8 too
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        hook = read_hook(sys.stdin)
    except OSError:
        hook = {}

    try:  # 1. count Claude Code's work on MYN
        import claude_code_usage
        import data_location
        result = claude_code_usage.sync(data_location.user_data_dir())
        log("Claude Code usage: %d sessions, %s tokens, approximately USD %.2f" % (
            result["sessions"], format(result["tokens"], ","), result["cost_usd"]))
    except Exception as error:
        log("Usage sync failed: %s: %s" % (type(error).__name__, error))

    try:
        import codex_usage
        import data_location
        result = codex_usage.sync(data_location.user_data_dir(),
                                  session_id=os.environ.get("CODEX_SESSION_ID") or os.environ.get("CODEX_THREAD_ID"))
        log("Codex usage: %d sessions, %s tokens (%s new); %s" % (
            result["sessions"], format(result["tokens"], ","), format(result["added_tokens"], ","),
            codex_usage.cost_text(result)))
    except Exception as error:
        log("Codex Usage sync failed: %s: %s" % (type(error).__name__, error))

    # 2. did this task change MYN?
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    previous = float(state.get("checked_at") or 0)
    changed_any = max(newest(WATCHED), newest(["AGENTS.md"])) > previous
    running = myn_processes()
    src_stamp = newest(["src"])
    started = min((start for _, start in running), default=0.0)
    program_changed = src_stamp > (started if running else previous)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"checked_at": time.time()}), encoding="utf-8")
    if not (changed_any or (running and program_changed)):
        log("No MYN files changed this turn; no notification needed.")
        return
    note = note_for(last_message(hook), program_changed)
    try:
        import reporting
        reporting.journal(data_location.user_data_dir(), note)
    except Exception as error:
        log('Update report was not recorded: %s' % type(error).__name__)
    temporary = DONE.with_name(DONE.name + ".tmp")
    temporary.write_text(json.dumps(note, ensure_ascii=False), encoding="utf-8")
    os.replace(str(temporary), str(DONE))

    # 3. MYN picks it up (restarting itself when the program changed), or is started
    if not running:
        start_myn()
        log("MYN was not running. Started; completion notice: %s" % note["spoken"])
        return
    deadline = time.time() + 12
    while time.time() < deadline and DONE.exists():
        time.sleep(1)
    if not DONE.exists():
        log("MYN received the notification%s." % ("; restarting to the new version" if program_changed else ""))
        return
    # Still there: an older MYN without the watcher (or it is busy). Only an older one that
    # needs the new code is asked to close normally (like clicking ✕) and started again.
    if not program_changed:
        log("MYN is busy; it will announce completion when idle.")
        return
    pids = [pid for pid, start in running if start < (ROOT / "src" / "myn_gui.py").stat().st_mtime]
    if not pids:
        log("MYN is busy; it will restart when idle.")
        return
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    deadline = time.time() + 20
    while time.time() < deadline and any(pid in {p for p, _ in myn_processes()} for pid in pids):
        time.sleep(1)
    if any(pid in {p for p, _ in myn_processes()} for pid in pids):
        log("The old MYN did not close normally. Close and reopen it manually; the notification is saved.")
        return
    start_myn()
    log("The old MYN closed normally and restarted. Completion notice: %s" % note["spoken"])


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # a hook must never get in the way
        log("Error: %s: %s" % (type(error).__name__, error))
    sys.exit(0)
