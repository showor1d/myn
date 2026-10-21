"""Claude Code's work on MYN, counted in MYN's statistics from Claude Code's own logs.

2026-10-18 the user: every time Claude develops MYN, its tokens are counted in MYN.
Claude Code keeps one JSON-lines transcript per session in
%USERPROFILE%\\.claude\\projects\\<project>\\, the project folder being D:\\MYN's path with
':' and '\\' turned into '-' ("D--MYN"). Each API response carries its token counts
(several times, once per content block: counted once by its request id). MYN's own
self-fix and background tasks run in other folders and are already counted from the
Agent SDK, so only the D:\\MYN project is read.

claude-code-usage.jsonl gets one line per session, hour and model. Lines written before
this (by earlier development sessions elsewhere, no "session") are kept as they are;
lines from a session whose log Claude Code has since cleaned up are kept too. Only token
counts, models and times are read: no prompts, answers or file contents.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import usage_stats

ROOT = Path(__file__).resolve().parents[2]  # D:\MYN
LABEL = "Claude Code"
HOUR_CACHE_FACTOR, MINUTE_CACHE_FACTOR, READ_FACTOR = 2.0, 1.25, 0.1  # x input price


def project_dir(root=ROOT, home=None):
    """Claude Code's log folder for D:\\MYN."""
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path(home or Path.home()) / ".claude"))
    slug = "".join("-" if ch in ":\\/" else ch for ch in str(Path(root)))
    return base / "projects" / slug


def _cost(model, usage):
    """USD for one response, with Claude Code's 1-hour cache writes at their own price."""
    price, estimated = usage_stats.price_of(model, 0)
    inp, out = price[0], price[1]
    creation = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    hour = _n(creation.get("ephemeral_1h_input_tokens"))
    minute = _n(creation.get("ephemeral_5m_input_tokens"))
    written = _n(usage.get("cache_creation_input_tokens"))
    if hour + minute != written:  # no split given: price it all as 5-minute writes
        hour, minute = 0, written
    cost = (_n(usage.get("input_tokens")) * inp + _n(usage.get("output_tokens")) * out +
            minute * inp * MINUTE_CACHE_FACTOR + hour * inp * HOUR_CACHE_FACTOR +
            _n(usage.get("cache_read_input_tokens")) * inp * READ_FACTOR) / 1e6
    server = usage.get("server_tool_use") if isinstance(usage.get("server_tool_use"), dict) else {}
    return cost + _n(server.get("web_search_requests")) * usage_stats.WEB_SEARCH_USD, estimated


def _n(value):
    return value if type(value) is int and value >= 0 else 0


def read_logs(folder):
    """{(session, hour, model): totals} from every transcript under the project folder."""
    seen, totals = set(), {}
    for path in sorted(Path(folder).rglob("*.jsonl")) if Path(folder).is_dir() else []:
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if '"usage"' not in line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                message = item.get("message") if isinstance(item, dict) else None
                if item.get("type") != "assistant" or not isinstance(message, dict) or \
                        not isinstance(message.get("usage"), dict):
                    continue
                key = (message.get("id"), item.get("requestId"))
                if key in seen or key == (None, None):
                    continue
                seen.add(key)
                try:
                    when = datetime.fromisoformat(str(item["timestamp"]).replace("Z", "+00:00"))
                except (KeyError, ValueError):
                    continue
                usage, model = message["usage"], str(message.get("model") or "")
                if model == "<synthetic>":
                    continue
                hour = when.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
                session = str(item.get("sessionId") or path.stem)
                row = totals.setdefault((session, hour.isoformat(), model), {
                    "input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0, "web_search_requests": 0, "cost_usd": 0.0,
                    "estimated": False, "responses": 0})
                for name in usage_stats.TOKEN_FIELDS:
                    row[name] += _n(usage.get(name))
                server = usage.get("server_tool_use") if isinstance(usage.get("server_tool_use"), dict) else {}
                row["web_search_requests"] += _n(server.get("web_search_requests"))
                cost, estimated = _cost(model, usage)
                row["cost_usd"] += cost
                row["estimated"] = row["estimated"] or estimated
                row["responses"] += 1
    return totals


def sync(config_dir, folder=None):
    """Rewrite claude-code-usage.jsonl from the logs. Returns {"sessions", "tokens", "cost_usd"}
    for what the logs hold now; raises OSError only when the file can't be written."""
    folder = Path(folder) if folder else project_dir()
    totals = read_logs(folder)
    sessions = {session for session, _, _ in totals}
    path = Path(config_dir) / usage_stats.CLAUDE_CODE_NAME
    kept = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            # earlier lines without a session, and sessions whose log is gone, stay
            if isinstance(record, dict) and record.get("session") not in sessions:
                kept.append(record)
    except (OSError, UnicodeError):
        pass
    fresh = []
    for (session, hour, model), row in sorted(totals.items(), key=lambda item: item[0][1]):
        fresh.append({"at_utc": hour, "source": "claude_code", "label": LABEL, "purpose": "claude_code",
                      "model": model, "session": session, "estimated": row["estimated"],
                      "cost_usd": round(row["cost_usd"], 6),
                      **{name: row[name] for name in usage_stats.TOKEN_FIELDS},
                      "web_search_requests": row["web_search_requests"]})
    lines = sorted(kept + fresh, key=lambda record: str(record.get("at_utc")))
    Path(config_dir).mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in lines),
                         encoding="utf-8")
    os.replace(str(temporary), str(path))
    return {"sessions": len(sessions),
            "tokens": sum(sum(row[name] for name in usage_stats.TOKEN_FIELDS) for row in totals.values()),
            "cost_usd": round(sum(row["cost_usd"] for row in totals.values()), 2)}
