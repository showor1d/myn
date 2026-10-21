"""Small, bounded voice-stage log. Never stores audio, transcripts or error bodies."""
import json
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()


def record(config, stage, **fields):
    allowed = ("reason", "seconds", "wait_s", "transcribe_s", "language", "chars", "depth",
               "silence_s", "elapsed_s", "turn")
    row = {"at": round(time.time(), 3), "stage": str(stage)[:40]}
    row.update({k: v for k, v in fields.items() if k in allowed and
                isinstance(v, (str, int, float, bool))})
    # Reasons must be internal codes, never user text.
    try:
        path = Path(config) / "voice-diagnostics.jsonl"
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > 256 * 1024:
                lines = path.read_text(encoding="utf-8").splitlines()[-400:]
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    except (OSError, ValueError):
        pass
