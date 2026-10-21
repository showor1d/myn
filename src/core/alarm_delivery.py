"""Local alarm evidence. A successful audio API call never proves the user heard it."""
import json
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

FILE = "alarm-delivery.jsonl"
_LOCK = threading.RLock()


def record(config, delivery_id, status, **fields):
    row = dict(id=delivery_id, at_utc=datetime.now(timezone.utc).isoformat(), status=status, **fields)
    with _LOCK:
        path = Path(config) / FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(row, ensure_ascii=False) + "\n")


def _recent(path, predicate, count=20):
    rows = deque(maxlen=count)
    try:
        with Path(path).open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    if isinstance(row, dict) and predicate(row):
                        rows.append(row)
                except ValueError:
                    continue
    except FileNotFoundError:
        pass
    return list(rows)


def recent(config):
    return _recent(Path(config) / FILE, lambda row: True)


def creation_audit(config):
    return _recent(Path(config) / "tool-diagnostics.jsonl",
                   lambda row: row.get("ok") is True and row.get("tool") in
                   ("set_reminder", "add_schedule", "add_alarm", "start_timer"))


