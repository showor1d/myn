"""Import measured Codex tokens and estimate dollars at the requested Astra rates.

Only explicitly registered development sessions are imported. The first session_meta
identifies a log: subagent logs may later contain the parent's inherited metadata.
token_count.total_token_usage is cumulative *per thread*, including repeated updates.
Input already includes cache reads/writes; output already includes reasoning tokens.
The MYN fields are disjoint, so those subsets must not be added a second time.

The desktop's local log format is observed, not a stable public API. Unknown/malformed
usage is skipped. A partial/in-flight final answer can be caught by the next sync.
Dollar estimates use the user's requested GPT-6 Astra official token prices. They
are not subscription invoices; the rate, source and assumptions travel with each row.
"""

import hashlib
import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import usage_stats

ROOT = Path(__file__).resolve().parents[2]
LABEL = "Codex"
PRICE_SOURCE = "https://developers.openai.com/api/docs/models/gpt-6-astra"
PRICE_DATE = "2026-10-19"
# USD / million tokens. Keep these separate from Claude's configurable prices.
ASTRA_RATES = {"input_tokens": 10.0, "output_tokens": 50.0,
               "cache_creation_input_tokens": 12.5, "cache_read_input_tokens": 1.0}
RAW_FIELDS = ("input_tokens", "output_tokens", "cached_input_tokens", "cache_write_input_tokens",
              "reasoning_output_tokens", "total_tokens")
_LOCK = threading.Lock()


@contextmanager
def _file_lock(config_dir):
    """MYN's daily refresh and dev_finish can run in separate processes."""
    folder = Path(config_dir)
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "codex-usage.lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _counts(value):
    if not isinstance(value, dict):
        return None
    counts = {name: value.get(name, 0) for name in RAW_FIELDS}
    if any(type(n) is not int or n < 0 for n in counts.values()):
        return None
    if any(name not in value for name in ("input_tokens", "output_tokens", "total_tokens")):
        return None
    if counts["input_tokens"] + counts["output_tokens"] != counts["total_tokens"] or \
            counts["cached_input_tokens"] + counts["cache_write_input_tokens"] > counts["input_tokens"] or \
            counts["reasoning_output_tokens"] > counts["output_tokens"]:
        return None
    return counts


def _same_root(value, root):
    try:
        return os.path.normcase(os.path.realpath(value)) == os.path.normcase(os.path.realpath(root))
    except (TypeError, ValueError, OSError):
        return False


def _key(thread, counts):
    digest = hashlib.sha256(json.dumps(counts, sort_keys=True).encode("utf-8")).hexdigest()[:20]
    return "codex:%s:%s" % (thread, digest)


def _price(row, event=None):
    """Price one imported delta; never overwrite an already recorded dollar amount.

    Cumulative totals are NOT request sizes. Only a matching last_token_usage can
    establish long-context or service-tier surcharges for this complete delta.
    Missing/aggregated request details use Standard short-context as an explicit
    estimate, including old rows whose original log has been cleaned up.
    """
    if row.get("source") != "codex" or row.get("purpose") != "codex_development" or \
            row.get("cost_usd") is not None or _counts(row.get("cumulative")) is None:
        return False
    counts = {name: row.get(name, 0) for name in ASTRA_RATES}
    if any(type(value) is not int or value < 0 for value in counts.values()):
        return False
    request = (event or {}).get("last_usage")
    raw = {"input_tokens": counts["input_tokens"] + counts["cache_creation_input_tokens"] +
                           counts["cache_read_input_tokens"],
           "output_tokens": counts["output_tokens"], "cached_input_tokens": counts["cache_read_input_tokens"],
           "cache_write_input_tokens": counts["cache_creation_input_tokens"]}
    matched = request is not None and all(request[name] == value for name, value in raw.items())
    request_input = request["input_tokens"] if matched else None
    long_context = matched and request_input > 272000
    supplied_tier = (event or {}).get("service_tier") if matched else None
    tier = {"default": "standard", "standard": "standard", "priority": "fast", "fast": "fast",
            "batch": "batch", "flex": "flex"}.get(supplied_tier, "standard")
    multiplier = {"fast": 2.0, "batch": 0.5, "flex": 0.5}.get(tier, 1.0)
    rates = {name: rate * multiplier * ((1.5 if name == "output_tokens" else 2.0) if long_context else 1.0)
             for name, rate in ASTRA_RATES.items()}
    row.update(cost_usd=round(sum(counts[name] * rates[name] for name in rates) / 1e6, 10),
               estimated=True,
               pricing={"model": "gpt-6-astra", "basis": "user_requested_model_rate",
                        "source": PRICE_SOURCE, "verified_on": PRICE_DATE,
                        "usd_per_million_tokens": rates, "scope": "tokens_only",
                        "context": ("long" if long_context else "short") if matched else "standard_assumed",
                        "request_input_tokens": request_input, "service_tier": tier,
                        "service_tier_assumed": supplied_tier not in
                            ("default", "standard", "priority", "fast", "batch", "flex")})
    return True


def cost_text(result):
    return 'Estimated $%.2f using published GPT-6 Astra pricing' % result["cost_usd"]


def _read(path, sessions, root):
    """Yield numeric snapshots from one matching log; inherited metadata is ignored."""
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        meta, model, current_root, tier = None, "", True, None
        for line in handle:
            # Do not parse message or tool-output records.
            if not any(tag in line for tag in ('"session_meta"', '"turn_context"', '"token_count"')):
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if not isinstance(item, dict) or not isinstance(item.get("payload"), dict):
                continue
            payload, kind = item["payload"], item.get("type")
            if kind == "session_meta" and meta is None:
                thread = payload.get("id")
                session = payload.get("session_id") or thread
                if not isinstance(thread, str) or not isinstance(session, str) or \
                        session not in sessions or not _same_root(payload.get("cwd"), root):
                    return
                try:
                    created = datetime.fromisoformat(str(payload.get("timestamp", "")).replace("Z", "+00:00"))
                    if created.tzinfo is None:
                        return
                except ValueError:
                    return
                meta = {"thread": thread, "session": session}
            elif kind == "turn_context" and meta is not None:
                model = str(payload.get("model") or "")[:80]
                current_root = _same_root(payload.get("cwd"), root)
                tier = payload.get("service_tier")
                tier = tier if isinstance(tier, str) else None
            elif kind == "event_msg" and payload.get("type") == "token_count" and meta is not None:
                info = payload.get("info")
                counts = _counts(info.get("total_token_usage")) if isinstance(info, dict) else None
                if counts is None:
                    continue
                try:
                    when = datetime.fromisoformat(str(item.get("timestamp", "")).replace("Z", "+00:00"))
                    if when.tzinfo is None:
                        continue
                except ValueError:
                    continue
                if when < created:
                    continue  # copied parent history is not work performed by this thread
                yield {**meta, "at_utc": when.astimezone(timezone.utc).isoformat(),
                       "model": model, "cumulative": counts, "in_workspace": current_root,
                       "last_usage": _counts(info.get("last_token_usage")), "service_tier": tier}


def sync(config_dir, session_id=None, home=None, root=ROOT):
    """Register one session (optional), refresh it and previously registered sessions.

    ``home`` is the Codex home, injectable for isolated tests. The default respects
    CODEX_HOME. Ordinary MYN refreshes only revisit sessions already in its ledger;
    dev_finish passes the current CODEX_SESSION_ID explicitly to register a new one.
    Historical/manual ChatGPT records are never modified or rediscovered implicitly.
    """
    with _LOCK:
        with _file_lock(config_dir):
            return _sync(config_dir, session_id, home, root)


def _sync(config_dir, session_id, home, root):
    path = Path(config_dir) / usage_stats.CODEX_NAME
    kept, latest, original, lines = [], {}, "", []
    try:
        original_bytes = path.read_bytes()
        original = original_bytes.decode("utf-8")
        for line in original.splitlines(keepends=True):
            try:
                record = json.loads(line)
            except ValueError:
                lines.append((line, None))
                continue
            if not isinstance(record, dict):
                lines.append((line, None))
                continue
            lines.append((line, record))
            kept.append(record)
            counts = _counts(record.get("cumulative"))
            if counts is not None and isinstance(record.get("thread"), str):
                before = latest.get(record["thread"])
                if before is None or counts["total_tokens"] > before["total_tokens"]:
                    latest[record["thread"]] = counts
    except FileNotFoundError:
        pass
    sessions = {row["session"] for row in kept if isinstance(row.get("session"), str)}
    if isinstance(session_id, str) and session_id.strip():
        sessions.add(session_id.strip())
    if not sessions:
        return {"sessions": 0, "threads": 0, "tokens": 0, "added_tokens": 0,
                "cost_usd": 0.0, "repriced_rows": 0, "estimated": True}
    base = Path(home or os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    events = []
    for name in ("sessions", "archived_sessions"):
        folder = base / name
        if folder.is_dir():
            for log in folder.rglob("*.jsonl"):
                events.extend(_read(log, sessions, root))
    # Backfill amounts in place: adding correction rows would count tokens twice.
    by_key = {_key(event["thread"], event["cumulative"]): event for event in events}
    repriced = {id(row) for row in kept if _price(row, by_key.get(row.get("id")))}
    added = []
    for event in sorted(events, key=lambda item: (item["at_utc"], item["cumulative"]["total_tokens"])):
        current = event["cumulative"]
        before = latest.get(event["thread"], dict.fromkeys(RAW_FIELDS, 0))
        delta = {name: current[name] - before[name] for name in RAW_FIELDS}
        if delta["total_tokens"] <= 0 or _counts(delta) is None:
            continue  # duplicate, incomplete, counter reset or incompatible log format
        latest[event["thread"]] = current
        # Workspace moves are excluded but advance the watermark. This also prevents a
        # later MYN turn from accidentally absorbing those other-project tokens.
        counted = delta if event["in_workspace"] else dict.fromkeys(RAW_FIELDS, 0)
        row = {"id": _key(event["thread"], current),
                      "source": "codex", "purpose": "codex_development", "label": LABEL,
                      "session": event["session"], "thread": event["thread"],
                      "at_utc": event["at_utc"], "model": event["model"],
                      "input_tokens": counted["input_tokens"] - counted["cached_input_tokens"] -
                                      counted["cache_write_input_tokens"],
                      "output_tokens": counted["output_tokens"],
                      "cache_read_input_tokens": counted["cached_input_tokens"],
                      "cache_creation_input_tokens": counted["cache_write_input_tokens"],
                      "reasoning_output_tokens": counted["reasoning_output_tokens"],
                      "reported_total_tokens": counted["total_tokens"], "cumulative": current,
                      "cost_usd": None}
        _price(row, event)
        added.append(row)
    records = kept + added
    if added or repriced:
        path.parent.mkdir(parents=True, exist_ok=True)
        if repriced:
            backup = path.with_name("codex-usage.before-astra-pricing.jsonl")
            # Exclusive creation preserves the original pre-migration bytes forever.
            try:
                with backup.open("xb") as handle:
                    handle.write(original_bytes)
            except FileExistsError:
                pass
            original = "".join(json.dumps(row, ensure_ascii=False) + "\n" if id(row) in repriced else line
                               for line, row in lines)
        temporary = path.with_name(path.name + ".tmp")
        prefix = original + ("\n" if original and not original.endswith("\n") else "")
        temporary.write_bytes((prefix + "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in added))
                              .encode("utf-8"))
        os.replace(str(temporary), str(path))
    return {"sessions": len({row.get("session") for row in records}),
            "threads": len({row.get("thread") for row in records}),
            "tokens": sum(sum(usage_stats._count(row.get(name)) for name in usage_stats.TOKEN_FIELDS)
                          for row in records),
            "added_tokens": sum(row["reported_total_tokens"] for row in added),
            "cost_usd": sum(float(row["cost_usd"]) for row in records
                            if usage_stats._number(row.get("cost_usd"), 0, 1e9)),
            "repriced_rows": len(repriced), "estimated": True}
