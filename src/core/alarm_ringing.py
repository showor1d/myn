"""Durable ringing occurrences; only a user acknowledgement dismisses them."""
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

FILE = "ringing-alarms.json"
INTERVAL = 30
LOCK = threading.RLock()
CLEANUP_LOCK = threading.RLock()


def _load(config):
    try:
        data = json.loads((Path(config) / FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"active": [], "seen": {}}
    if (not isinstance(data, dict) or not isinstance(data.get("active"), list)
            or not isinstance(data.get("seen"), dict)
            or any(not isinstance(row, dict) or not all(isinstance(row.get(k), str)
                       for k in ("id", "source_key", "text")) for row in data["active"])):
        raise ValueError('Active alarm records are damaged; the original file was preserved.')
    pending = data.get("completed_cleanup", [])
    if (not isinstance(pending, list) or any(not isinstance(row, dict) or
            not all(isinstance(row.get(k), str) for k in ("id", "source_key", "occurrence"))
            for row in pending)):
        raise ValueError('Completed alarm records are damaged; the original file was preserved.')
    return data


def _save(config, data):
    path = Path(config) / FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(str(temporary), str(path))


def active(config):
    with LOCK:
        return _load(config)["active"]


def activate(config, source_key, occurrence, text, ringtone_id='', ringtone_name=''):
    """Called BEFORE claiming a due task. Idempotent across interrupted claims."""
    with LOCK:
        data = _load(config)
        existing = next((r for r in data["active"] if r["source_key"] == source_key), None)
        if data["seen"].get(source_key) == occurrence:
            return existing
        data["seen"][source_key] = occurrence
        if existing is None:
            existing = dict(id=uuid.uuid4().hex, source_key=source_key, text=text,
                            occurrence=occurrence, started_at=time.time(), last_attempt=0,
                            ringtone_id=ringtone_id, ringtone_name=ringtone_name)
            data["active"].append(existing)
        _save(config, data)
        return existing


def mark_attempt(config, ring_id, now=None):
    with LOCK:
        data = _load(config)
        for row in data["active"]:
            if row["id"] == ring_id:
                row["last_attempt"] = time.time() if now is None else now
                _save(config, data)
                return True
    return False


def dismiss(config, ids, completed=False):
    with LOCK:
        data = _load(config)
        removed = [r["id"] for r in data["active"] if r["id"] in ids]
        if removed:
            if completed:
                pending = data.setdefault("completed_cleanup", [])
                for row in data["active"]:
                    if row["id"] in removed:
                        pending.append(dict(id=row["id"], source_key=row["source_key"],
                            occurrence=row.get("occurrence") or data["seen"].get(row["source_key"], "")))
            data["active"] = [r for r in data["active"] if r["id"] not in removed]
            _save(config, data)
        return removed


def cleanup_completed(config):
    """Retry durable cleanup after acknowledgement, never while holding the ring lock.

    A source rescheduled to a different occurrence or changed to repeating is retained.
    Historical dispatch evidence and deduplication markers are not deleted.
    """
    import timekeeper
    import local_actions
    from alarm_kind import is_alarm
    removed = 0
    with CLEANUP_LOCK:
        with LOCK:
            pending = list(_load(config).get("completed_cleanup", []))
        for row in pending:
            kind, _, ident = row["source_key"].partition(":")
            if kind == "schedule":
                with timekeeper._LOCK:
                    path = Path(config) / timekeeper.TASKS
                    items = timekeeper._read(path)
                    kept = [t for t in items if not (t["id"] == ident and is_alarm(t)
                        and t.get("repeat") == "once"
                        and "%s %s" % (t.get("date", ""), t.get("time", "")) == row["occurrence"])]
                    if len(kept) != len(items):
                        timekeeper._write(path, kept)
                        removed += len(items) - len(kept)
            elif kind == "reminder":
                with local_actions._LOCK:
                    items = local_actions._reminder_records(config)
                    kept = [t for t in items if not (t["id"] == ident and is_alarm(t)
                        and t["due_utc"] == row["occurrence"])]
                    if len(kept) != len(items):
                        local_actions._save_reminders(config, kept)
                        removed += len(items) - len(kept)
            with LOCK:
                data = _load(config)
                data["completed_cleanup"] = [r for r in data.get("completed_cleanup", [])
                                              if r["id"] != row["id"]]
                _save(config, data)
    return removed


def acknowledge(config, snapshot, decision, question, revision):
    """A delayed interpretation must not dismiss a new occurrence or newer turn."""
    import interaction
    if not snapshot or not isinstance(decision, dict):
        return []
    evidence = decision.get("evidence")
    if (decision.get("action") != "dismiss" or decision.get("scope") != "current"
            or decision.get("certainty") != "clear" or not decision.get("reason")
            or not isinstance(evidence, str) or not 1 <= len(evidence.strip()) <= 500
            or evidence.strip() not in question):
        return []
    with interaction.LOCK:
        if interaction.load(config)["revision"] != revision:
            return []
        removed = dismiss(config, [r["id"] for r in snapshot], completed=True)
    try:
        cleanup_completed(config)
    except (OSError, ValueError):
        pass  # acknowledgement stays saved; the window retries cleanup, including after restart
    return removed


def explicit_ack(question):
    """Whole addressed command only; also works when the network/key is unavailable."""
    plain = re.sub(r"[\s，,。.!！]", "", question).casefold()
    plain = re.sub(r"^myn", "", plain)
    return plain in {
        "关闭闹钟", "关掉闹钟", "停止闹钟", "停掉闹钟", "把闹钟关掉", "闹钟关了吧",
        "我已经起床了", "我起床了", "已经起床了", "我已经醒了", "我醒了",
        "不用再叫我了", "不用再提醒我起床了",
    }


ACK = 'This alarm has stopped. Future alarms are unchanged.'
