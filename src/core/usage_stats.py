"""What MYN (and the work on MYN) used and cost, per day, week, month and year, in USD.

Sources contain accounting metadata only, never prompts or model responses:

  * claude-usage.jsonl      - every Claude API call MYN makes (model, purpose, tokens);
                              priced with Anthropic's list prices. Background tasks
                              (Agent SDK) add a line with the cost the SDK reports.
  * other-usage.jsonl       - ElevenLabs speech (characters and cost, one line per
                              request), history imported once from ElevenLabs, and
                              entries the user adds by hand (e.g. ChatGPT).
  * claude-code-usage.jsonl - Claude Code sessions that worked on MYN; this file is
                              replaced as a whole when it is refreshed.
  * codex-usage.jsonl       - registered Codex development sessions; measured tokens,
                              with unknown cost kept explicitly unknown.

Claude Haiku 5.5 (only the quick "is this for MYN?" check) is left out of the
statistics at the user's request (2026-10-17).

Prices can be overridden in personal_data/config/usage-settings.json:
    {"prices": {"claude-sonnet-5-5": [2, 10, 2.5, 0.1]},
     "elevenlabs_per_1k": {"eleven_flash_v2_5": 0.05}}
"""

import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

USAGE_NAME = "claude-usage.jsonl"
OTHER_NAME = "other-usage.jsonl"
CLAUDE_CODE_NAME = "claude-code-usage.jsonl"
CODEX_NAME = "codex-usage.jsonl"
SETTINGS_NAME = "usage-settings.json"
PRICE_SOURCE = 'Published Anthropic, ElevenLabs and OpenAI GPT-6 Astra pricing (2026-10)'
# USD per million tokens: input, output, cache write (5 minutes), cache read.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0, 5.0, 0.20),
    "claude-sonnet-5-5": (2.0, 10.0, 2.5, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
    "claude-fable-5-1": (10.0, 50.0, 12.5, 0.25),
    "claude-fable-5": (10.0, 50.0, 12.5, 1.0),
}
FAMILIES = (("opus", "claude-opus-5-5"), ("sonnet", "claude-sonnet-5-5"),
            ("haiku", "claude-haiku-5-5"), ("fable", "claude-fable-5-1"))
WEB_SEARCH_USD = 0.01  # $10 per 1,000 searches
# ElevenLabs API, USD per 1,000 characters (pay-as-you-go list price).
ELEVEN_PER_1K = {"eleven_flash_v2_5": 0.05, "eleven_turbo_v2_5": 0.05,
                 "eleven_multilingual_v2": 0.10, "eleven_v3": 0.10}
ELEVEN_DEFAULT_PER_1K = 0.10
PERIODS = ("day", "week", "month", "year")
PERIOD_NAMES = {"day": 'Day', "week": 'Week', "month": 'Month', "year": 'Year'}
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
PURPOSES = (("chat", 'Chat'), ("work", 'Work (deep reasoning)'), ("conversation_judge", 'Conversation classification'),
            ("interaction_scene", 'Scene understanding'),
            ("memory_answer", 'Memory Q&A'), ("memory_classification", 'Memory classification'),
            ("save_classification", 'Memory classification'), ("scene_analysis", 'Scene understanding'),
            ("profile", 'Profile organization'), ("agent_task", 'Background tasks'), ("self_fix", 'Evolution'),
            ("evolution", 'Evolution'),
            ("codex_development", "Codex"),
            ("voice", 'Speech synthesis'), ("claude_code", "Claude Code"), ("chatgpt", "ChatGPT"),
            ("manual", 'Manual entry'))
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
_LOCK = threading.Lock()
_WRITE_LOCK = threading.Lock()


def model_name(model):
    text = str(model or "")
    for key, label in (("opus", "Opus"), ("sonnet", "Sonnet"), ("haiku", "Haiku"), ("fable", "Fable")):
        if key in text:
            found = re.search(r"(\d+)[-.](\d+)", text)
            return label + (" %s.%s" % found.groups() if found else "")
    return text or 'Other'


def purpose_name(purpose):
    text = str(purpose or "")
    if text.startswith("use:") and len(text) > 4:
        return text[4:44]  # a use named by the user (hand-entered records)
    for prefix, label in PURPOSES:
        if text.startswith(prefix):
            return label
    return 'Other'


@dataclass(frozen=True)
class UsageSettings:
    prices: dict = field(default_factory=dict)
    eleven: dict = field(default_factory=dict)


def _number(value, low, high):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and low <= value <= high


def load_settings(config_dir):
    try:
        raw = json.loads((Path(config_dir) / SETTINGS_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return UsageSettings()
    if not isinstance(raw, dict):
        return UsageSettings()
    prices, eleven = {}, {}
    for model, value in (raw.get("prices") or {}).items() if isinstance(raw.get("prices"), dict) else ():
        if isinstance(model, str) and isinstance(value, list) and len(value) == 4 \
                and all(_number(v, 0, 1000) for v in value):
            prices[model] = tuple(float(v) for v in value)
    for model, value in (raw.get("elevenlabs_per_1k") or {}).items() \
            if isinstance(raw.get("elevenlabs_per_1k"), dict) else ():
        if isinstance(model, str) and _number(value, 0, 100):
            eleven[model] = float(value)
    return UsageSettings(prices, eleven)


def _count(value):
    return value if type(value) is int and value >= 0 else 0


def price_of(model, prompt_tokens, overrides=None):
    """(input, output, cache write, cache read) USD per million tokens, estimated?"""
    model = str(model or "")
    if overrides and model in overrides:
        return overrides[model], False
    if model in PRICES:
        base, estimated = PRICES[model], False
    else:
        base, estimated = PRICES["claude-sonnet-5-5"], True
        for key, known in FAMILIES:
            if key in model:
                base = PRICES[known]
                break
    if "haiku-5-5" in model and prompt_tokens > 100000:
        base = (0.50, 2.50, 0.625, 0.05)  # Haiku 5.5 over 100K prompt tokens
    return base, estimated


def eleven_cost(model, characters, settings=None):
    per_1k = (settings.eleven if settings else {}).get(model) or ELEVEN_PER_1K.get(model, ELEVEN_DEFAULT_PER_1K)
    return characters * per_1k / 1000


@dataclass
class Call:
    when: datetime  # local time
    group: str      # Display labels identify the model, speech service or development source.
    purpose: str
    input: int
    output: int
    cache_write: int
    cache_read: int
    other: int      # tokens without a breakdown (e.g. ChatGPT entered by hand)
    searches: int
    cost: float
    estimated: bool
    priced: bool    # False when the cost is unknown (only tokens are known)
    task_key: str = ""  # stable evolution point identity, never task text
    details: dict = field(default_factory=dict)

    @property
    def tokens(self):
        return self.input + self.output + self.cache_write + self.cache_read + self.other


def cost_of(record, overrides=None):
    """USD for one Claude usage line, and whether the price was a guess."""
    if _number(record.get("cost_usd"), 0, 1e9):
        return float(record["cost_usd"]), False
    counts = [_count(record.get(name)) for name in TOKEN_FIELDS]
    price, estimated = price_of(record.get("model"), counts[0] + counts[2] + counts[3], overrides)
    cost = sum(count * rate for count, rate in zip(counts, price)) / 1e6
    return cost + _count(record.get("web_search_requests")) * WEB_SEARCH_USD, estimated


def _call(record, settings, tz):
    """One Call from a log line, or None for lines that cost nothing or are left out."""
    if not isinstance(record, dict):
        return None
    try:
        when = datetime.fromisoformat(record["at_utc"])
    except (ValueError, KeyError, TypeError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    source = record.get("source") or "claude"
    model = str(record.get("model") or "")
    if "haiku" in model.lower():
        return None  # left out of the statistics (user, 2026-10-17)
    counts = [_count(record.get(name)) for name in TOKEN_FIELDS]
    other = _count(record.get("other_tokens"))
    searches = _count(record.get("web_search_requests"))
    label = str(record.get("label") or "")[:40]
    # Normalize historical display labels without rewriting the user's ledger.
    label = re.sub(r"^(Codex|Claude Code|ChatGPT)[（(]开发\s*MYN[）)]$", r"\1",
                   label, flags=re.IGNORECASE)
    if source == "claude":
        if not any(counts) and not searches and not _number(record.get("cost_usd"), 0, 1e9):
            return None  # failed calls that were not billed
        cost, estimated = cost_of(record, settings.prices)
        priced, group = True, model_name(model)
    elif source == "elevenlabs":
        characters = _count(record.get("characters"))
        cost = float(record["cost_usd"]) if _number(record.get("cost_usd"), 0, 1e6) \
            else eleven_cost(model, characters, settings)
        estimated, priced, group = True, True, label or 'ElevenLabs speech'
    else:  # claude_code, codex, chatgpt, manual
        priced = _number(record.get("cost_usd"), 0, 1e9)
        cost = float(record["cost_usd"]) if priced else 0.0
        if source == "claude_code" and not priced:
            cost, _ = cost_of(record, settings.prices)
            priced = True
        estimated = bool(record.get("estimated"))
        group = label or {"claude_code": "Claude Code", "codex": "Codex", "chatgpt": "ChatGPT"}.get(source, 'Manual entry')
        if not any(counts) and not other and not priced:
            return None
    purpose = str(record.get("purpose") or ("voice" if source == "elevenlabs" else source))
    if isinstance(record.get("use"), str) and record["use"].strip():
        purpose = "use:" + record["use"].strip()
    return Call(when.astimezone(tz), group, purpose, *counts, other, searches, cost, estimated, priced,
                str(record.get("task_key") or ""),
                {key: record[key] for key in ("source", "model", "status", "run_id", "usage_steps", "calls_complete", "cost_source")
                 if key in record})


_CACHE = {}


def load_calls(config_dir, settings=None, tz=None):
    """Every counted call from all sources, oldest first. Bad lines are skipped."""
    settings = settings or load_settings(config_dir)
    paths = [Path(config_dir) / name for name in (USAGE_NAME, OTHER_NAME, CLAUDE_CODE_NAME, CODEX_NAME)]
    stamps = []
    for path in paths:
        try:
            stat = path.stat()
            stamps.append((stat.st_mtime_ns, stat.st_size))
        except OSError:
            stamps.append(None)
    key = (str(config_dir), tuple(stamps), repr(settings), str(tz))
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
    calls = []
    for path, stamp in zip(paths, stamps):
        if stamp is None:
            continue
        try:
            with path.open(encoding="utf-8", errors="replace") as log:
                for line in log:
                    try:
                        call = _call(json.loads(line), settings, tz)
                    except (ValueError, TypeError):
                        continue
                    if call is not None:
                        calls.append(call)
        except OSError:
            continue
    calls.sort(key=lambda call: call.when)
    with _LOCK:
        _CACHE.clear()
        _CACHE[key] = calls
    return calls


def bounds(period, offset=0, now=None):
    """(start, end) local datetimes of the day/week/month/year `offset` periods from now."""
    now = (now or datetime.now().astimezone())
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "day":
        start = day + timedelta(days=offset)
        return start, start + timedelta(days=1)
    if period == "week":
        start = day - timedelta(days=day.weekday()) + timedelta(weeks=offset)
        return start, start + timedelta(weeks=1)
    if period == "month":
        index = day.year * 12 + day.month - 1 + offset
        start = day.replace(year=index // 12, month=index % 12 + 1, day=1)
        index += 1
        return start, start.replace(year=index // 12, month=index % 12 + 1)
    if period == "year":
        start = day.replace(year=day.year + offset, month=1, day=1)
        return start, start.replace(year=start.year + 1)
    raise ValueError("period must be one of %s" % ", ".join(PERIODS))


def _buckets(period, start, end, now=None, first=None):
    'Return (label, start, end) chart buckets: hours, weekdays, months of the selected year, and years from the first record.'
    if period == "day":
        return [('%02d:00' % hour, start + timedelta(hours=hour), start + timedelta(hours=hour + 1))
                for hour in range(24)]
    if period == "week":
        return [(WEEKDAYS[i] + "\n%d/%d" % ((start + timedelta(days=i)).month, (start + timedelta(days=i)).day),
                 start + timedelta(days=i), start + timedelta(days=i + 1)) for i in range(7)]
    if period == "month":
        year = start.replace(month=1)
        return [('%02d' % month, year.replace(month=month),
                 year.replace(month=month + 1) if month < 12 else year.replace(year=year.year + 1))
                for month in range(1, 13)]
    last = max(start.year, (now or start).year)
    begin = min(start.year, first.year if first is not None else start.year)
    return [("%d" % year, start.replace(year=year), start.replace(year=year + 1))
            for year in range(begin, last + 1)]


def label_for(period, offset=0, now=None):
    start, end = bounds(period, offset, now)
    relative = {("day", 0): 'Today', ("day", -1): 'Yesterday', ("day", -2): 'Two days ago', ("week", 0): 'This week', ("week", -1): 'Last week',
                ("month", 0): 'This month', ("month", -1): 'previous month', ("year", 0): 'This year', ("year", -1): 'Last year'}
    if period == "day":
        text = '%04d-%02d-%02d %s' % (start.year, start.month, start.day, WEEKDAYS[start.weekday()])
    elif period == "week":
        last = end - timedelta(days=1)
        text = "%d/%d – %d/%d" % (start.month, start.day, last.month, last.day)
    elif period == "month":
        text = '%04d-%02d' % (start.year, start.month)
    else:
        text = '%d' % start.year
    name = relative.get((period, offset))
    return name + "（" + text + "）" if name else text


def _totals(calls):
    total = {"calls": len(calls), "input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "other": 0,
             "searches": 0, "cost_usd": 0.0, "estimated": False, "unpriced_tokens": 0, "unpriced": []}
    for call in calls:
        for name in ("input", "output", "cache_write", "cache_read", "other", "searches"):
            total[name] += getattr(call, name)
        total["cost_usd"] += call.cost
        total["estimated"] = total["estimated"] or call.estimated
        if not call.priced:
            total["unpriced_tokens"] += call.tokens
            if call.group not in total["unpriced"]:
                total["unpriced"].append(call.group)
    total["tokens"] = sum(total[name] for name in ("input", "output", "cache_write", "cache_read", "other"))
    return total


def _group(calls, key):
    groups = {}
    for call in calls:
        item = groups.setdefault(key(call), {"cost_usd": 0.0, "tokens": 0, "calls": 0, "priced": True,
                                             "estimated": False})
        item["cost_usd"] += call.cost
        item["tokens"] += call.tokens
        item["calls"] += 1
        item["priced"] = item["priced"] and call.priced
        item["estimated"] = item["estimated"] or (call.priced and call.estimated)
    return sorted(([name, value] for name, value in groups.items()),
                  key=lambda pair: (-pair[1]["cost_usd"], -pair[1]["tokens"]))


def report(config_dir, period="day", offset=0, now=None):
    """Everything the statistics window and the voice answer need for one period."""
    import usage_detail
    if period not in PERIODS:
        raise ValueError("period must be one of %s" % ", ".join(PERIODS))
    offset = max(-1000, min(0, int(offset)))
    now = now or datetime.now().astimezone()
    calls = load_calls(config_dir, load_settings(config_dir), now.tzinfo)
    start, end = bounds(period, offset, now)
    inside = [call for call in calls if start <= call.when < end]
    previous_start, previous_end = bounds(period, offset - 1, now)
    previous = [call for call in calls if previous_start <= call.when < previous_end]
    series = []
    first = calls[0].when if calls else None
    for label, bucket_start, bucket_end in _buckets(period, start, end, now, first):
        if first is not None and bucket_end <= first:
            continue  # nothing was recorded that early: no empty bars before the first record
        part = [call for call in calls if bucket_start <= call.when < bucket_end]
        by_group, tokens_by_group = {}, {}
        for call in part:
            by_group[call.group] = by_group.get(call.group, 0.0) + call.cost
            tokens_by_group[call.group] = tokens_by_group.get(call.group, 0) + call.tokens
        bucket = {"label": label, "cost_usd": sum(call.cost for call in part),
                  "tokens": sum(call.tokens for call in part), "calls": len(part),
                  "by_model": by_group, "tokens_by_model": tokens_by_group, "future": bucket_start > now,
                  "selected": bucket_start <= start < bucket_end and period in ("month", "year")}
        for name in ("input", "output", "cache_write", "cache_read", "other"):
            bucket[name] = sum(getattr(call, name) for call in part)
        series.append(bucket)
    return {"period": period, "offset": offset, "label": label_for(period, offset, now),
            "start": start.isoformat(), "end": end.isoformat(),
            "totals": _totals(inside), "previous": _totals(previous),
            "series": series,
            "by_model": _group(inside, lambda call: call.group),
            "by_purpose": _group(inside, lambda call: purpose_name(call.purpose)),
            "daily_entries": usage_detail.entries(inside, config_dir) if period == "day" else [],
            "first_record": first.isoformat() if first else None,
            # going back further would only show empty periods
            "has_earlier": first is not None and first < start,
            "price_source": PRICE_SOURCE}


def tokens_text(count):
    for threshold, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if count >= threshold:
            return "%.1f%s" % (count / threshold, suffix)
    return "%d" % count


def money_text(usd):
    return 'USD %.2f' % max(0.0, usd)  # always two decimals (user, 2026-10-17)


def spoken(result):
    """One or two sentences to say out loud."""
    total = result["totals"]
    name = result["label"].split("（")[0]
    if not total["calls"]:
        return '%s has no usage records or spending.' % name
    if total["cost_usd"] <= 0 and total["unpriced_tokens"] >= total["tokens"]:
        return '%s used %s tokens, all from %s, with no cost records.' % (
            name, tokens_text(total["tokens"]), "、".join(total["unpriced"]))
    text = '%s used %s tokens and cost approximately %s' % (name, tokens_text(total["tokens"]), money_text(total["cost_usd"]))
    if total["unpriced_tokens"]:
        text += ' (%s: %s tokens have no cost records and are excluded)' % (
            "、".join(total["unpriced"]), tokens_text(total["unpriced_tokens"]))
    text += ". "
    paid = [(group, value) for group, value in result["by_model"] if value["cost_usd"] > 0]
    if len(paid) > 1 and total["cost_usd"]:
        top, value = paid[0]
        share = value["cost_usd"] / total["cost_usd"] * 100
        text += ('Almost all spending was on %s.' % top if share >= 95 else 'The largest share was %s at %.1f%%.' % (top, share))
    return text


def for_model(result):
    """A compact summary for the usage_report chat tool."""
    total = result["totals"]
    return {"period": result["label"], "tokens": total["tokens"], "input_tokens": total["input"],
            "output_tokens": total["output"], "cache_write_tokens": total["cache_write"],
            "cache_read_tokens": total["cache_read"], "tokens_without_breakdown": total["other"],
            "calls": total["calls"], "web_searches": total["searches"], "cost_usd": round(total["cost_usd"], 4),
            "tokens_without_cost": total["unpriced_tokens"], "sources_without_cost": total["unpriced"],
            "previous_period_cost_usd": round(result["previous"]["cost_usd"], 4),
            "by_source": [{"source": name, "cost_usd": round(value["cost_usd"], 4), "tokens": value["tokens"],
                           "cost_known": value["priced"], "cost_is_estimate": value["estimated"]}
                          for name, value in result["by_model"]],
            "by_use": [{"use": name, "cost_usd": round(value["cost_usd"], 4), "tokens": value["tokens"]}
                       for name, value in result["by_purpose"]],
            "note": "USD, estimated from list prices; the bills are authoritative. Haiku is not counted. "
                    "Hand-entered costs are counted as the user gave them (cost_is_estimate = an estimate).",
            "say_this": spoken(result),
            **({"entries": result.get("daily_entries", [])} if result.get("period") == "day" else {})}


# Spoken questions answered on the computer without asking Claude.
_WHEN = (("今天|今日|today", "day", 0), ("前天", "day", -2), ("昨天|yesterday", "day", -1),
         ("这周|本周|这个星期|这星期|这礼拜|这个礼拜|this week", "week", 0),
         ("上周|上个星期|上星期|上礼拜|last week", "week", -1),
         ("这个月|本月|这月|this month", "month", 0), ("上个月|上月|last month", "month", -1),
         ("今年|this year", "year", 0), ("去年|last year", "year", -1))
_ABOUT = r"token|tokens|托肯|拖肯|头肯|偷肯|透肯|用量|费用|花费|开销|额度|api|claude|多少钱|花了多少"
_ASK = r"多少|几|how (?:much|many)"


def spoken_question(text):
    """(period, offset) when a short sentence asks what MYN used or cost, else None."""
    if not text or len(text) > 40:
        return None
    lowered = text.lower()
    if not re.search(_ABOUT, lowered) or not re.search(_ASK, lowered):
        return None
    # Cost questions alone may refer to shopping; also require a token or MYN cue.
    if not re.search(r"token|tokens|托肯|拖肯|头肯|偷肯|透肯|用量|费用|花费|开销|额度|api|claude|你|myn|麦恩", lowered):
        return None
    for words, period, offset in _WHEN:
        if re.search(words, lowered):
            return period, offset
    return None


# ---- writing ------------------------------------------------------------------------
def _append(path, record):
    try:
        with _WRITE_LOCK:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with Path(path).open("a", encoding="utf-8") as log:
                log.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def record_agent(usage_path, model, purpose, usage, cost_usd, task_key="", run_id="", steps=None, calls_complete=False):
    """One billed task total, with nested call evidence that never adds a second charge."""
    usage = usage if isinstance(usage, dict) else {}
    record = {"at_utc": datetime.now(timezone.utc).isoformat(), "purpose": purpose,
              "model": str(model or "agent")[:60], "status": "agent_done"}
    if task_key:
        record["task_key"] = task_key
    if run_id:
        record["run_id"] = str(run_id)[:80]
    if isinstance(steps, list):
        clean = []
        for step in steps:
            if not isinstance(step, dict):
                continue
            values = step.get("usage") if isinstance(step.get("usage"), dict) else {}
            clean.append({"id": str(step.get("id") or "")[:120],
                          "identified": step.get("identified", True) is True,
                          "at_utc": str(step.get("at_utc") or "")[:40],
                          "model": str(step.get("model") or model or "")[:80],
                          "usage": {name: _count(values.get(name)) for name in TOKEN_FIELDS},
                          "tools": [str(t)[:60] for t in step.get("tools", []) if isinstance(t, str)][:20]})
        record["usage_steps"] = clean
        record["calls_complete"] = calls_complete is True and len(clean) == len(steps)
    for name in TOKEN_FIELDS:
        record[name] = _count(usage.get(name)) or None
    server = usage.get("server_tool_use") if isinstance(usage.get("server_tool_use"), dict) else {}
    if _count(server.get("web_search_requests")):
        record["web_search_requests"] = server["web_search_requests"]
    if _number(cost_usd, 0, 1e6):
        record["cost_usd"] = round(float(cost_usd), 6)
        record["cost_source"] = "sdk"
    elif not any(record.get(name) for name in ("input_tokens", "output_tokens")):
        return
    # A retried delivery of the same worker result must not bill the task twice.
    with _LOCK:
        if run_id:
            try:
                with Path(usage_path).open(encoding="utf-8") as stream:
                    for line in stream:
                        try:
                            if json.loads(line).get("run_id") == record["run_id"]:
                                return
                        except (ValueError, AttributeError):
                            pass
            except FileNotFoundError:
                pass
        _append(usage_path, record)


def record_voice(config_dir, model, characters, when=None, history_id=None):
    """One ElevenLabs request: characters and its cost (no text)."""
    characters = _count(characters)
    if not characters:
        return False
    record = {"at_utc": (when or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
              "source": "elevenlabs", "purpose": "voice", "model": str(model or "")[:60],
              "characters": characters,
              "cost_usd": round(eleven_cost(str(model or ""), characters, load_settings(config_dir)), 6)}
    if history_id:
        record["id"] = "elevenlabs:" + str(history_id)[:80]
    return _append(Path(config_dir) / OTHER_NAME, record)


def manual_entries(config_dir):
    """Entries added by hand, newest first: [{"id", "at_utc", "label", "tokens", "cost_usd", "estimated"}]."""
    found = []
    try:
        with (Path(config_dir) / OTHER_NAME).open(encoding="utf-8", errors="replace") as log:
            for line in log:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and str(record.get("id", "")).startswith("manual:"):
                    found.append({"id": record["id"], "at_utc": record.get("at_utc"),
                                  "label": record.get("label") or 'Manual entry', "use": record.get("use") or "",
                                  "tokens": _count(record.get("other_tokens")),
                                  "cost_usd": record.get("cost_usd"),
                                  "estimated": bool(record.get("estimated"))})
    except OSError:
        pass
    return sorted(found, key=lambda item: str(item["at_utc"]), reverse=True)


def _check_cost(cost_usd):
    if cost_usd is not None and not _number(cost_usd, 0, 1e6):
        raise ValueError('Cost must be USD 0-1000000.')


def add_manual(config_dir, when, label, tokens=0, cost_usd=None, source="manual", estimated=False, use=None):
    'Add manually entered usage. Either tokens or cost is required. estimated marks a guessed cost; use describes its purpose.'
    label = str(label or "").strip()
    if not 1 <= len(label) <= 40:
        raise ValueError('Name must contain 1-40 characters.')
    tokens = int(tokens or 0)
    if not 0 <= tokens <= 10 ** 12:
        raise ValueError('Invalid token count.')
    _check_cost(cost_usd)
    if not tokens and cost_usd is None:
        raise ValueError('Enter at least a token count or cost.')
    if when.tzinfo is None:
        when = when.astimezone()
    record = {"at_utc": when.astimezone(timezone.utc).isoformat(), "source": source,
              "purpose": "chatgpt" if source == "chatgpt" else "manual",
              "label": label, "other_tokens": tokens, "cost_usd": None if cost_usd is None else float(cost_usd),
              "id": "manual:" + uuid.uuid4().hex[:12]}
    if use and str(use).strip():
        record["use"] = str(use).strip()[:40]
    if estimated and cost_usd is not None:
        record["estimated"] = True
    if not _append(Path(config_dir) / OTHER_NAME, record):
        raise OSError('Cannot write usage record.')
    return record["id"]


def _rewrite_manual(config_dir, entry_id, change):
    """Rewrite the one line with this id as change(record) (None drops it); other lines stay
    exactly as they are. False when there is no such line."""
    path = Path(config_dir) / OTHER_NAME
    with _WRITE_LOCK:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        kept, found = [], False
        for line in lines:
            try:
                record = json.loads(line)
                match = record.get("id") == entry_id
            except (ValueError, AttributeError):
                match = False
            if not match:
                kept.append(line)
                continue
            found = True
            record = change(record)
            if record is not None:
                kept.append(json.dumps(record, ensure_ascii=False) + "\n")
        if not found:
            return False
        temporary = path.with_name(OTHER_NAME + ".tmp")
        temporary.write_text("".join(kept), encoding="utf-8")
        os.replace(str(temporary), str(path))
    return True


def remove_manual(config_dir, entry_id):
    """Delete one hand-entered line; other lines stay exactly as they are."""
    if not str(entry_id).startswith("manual:"):
        raise ValueError('Only manual records can be deleted.')
    return _rewrite_manual(config_dir, entry_id, lambda record: None)


def set_manual_cost(config_dir, entry_id, cost_usd, estimated=False):
    """Fill in or change the USD cost of one hand-entered line (None = cost unknown again).
    `estimated` marks a guessed cost. Then it counts in every period's total spending."""
    if not str(entry_id).startswith("manual:"):
        raise ValueError('Only manual records can be edited.')
    _check_cost(cost_usd)

    def change(record):
        if cost_usd is None and not _count(record.get("other_tokens")):
            raise ValueError('This record has no token count, so its cost cannot be cleared.')
        record["cost_usd"] = None if cost_usd is None else float(cost_usd)
        record.pop("estimated", None)
        if estimated and cost_usd is not None:
            record["estimated"] = True
        return record

    return _rewrite_manual(config_dir, entry_id, change)


def _eleven_history(key, opener, stop_before=None, max_pages=30):
    """ElevenLabs generation history, newest first, back to stop_before (unix time)."""
    after, items = None, []
    for _ in range(max_pages):
        query = {"page_size": 1000}
        if after:
            query["start_after_history_item_id"] = after
        request = urllib.request.Request("https://api.elevenlabs.io/v1/history?" + urllib.parse.urlencode(query),
                                         headers={"xi-api-key": key, "Accept": "application/json"})
        with opener(request, timeout=30) as response:
            page = json.loads(response.read(32 * 1024 * 1024).decode("utf-8"))
        history = page.get("history") if isinstance(page, dict) else None
        if not isinstance(history, list):
            raise ValueError("history")
        items.extend(item for item in history if isinstance(item, dict))
        oldest = min((item.get("date_unix") for item in items if _number(item.get("date_unix"), 0, 1e11)),
                     default=None)
        if stop_before is not None and oldest is not None and oldest < stop_before:
            break
        if not page.get("has_more") or not page.get("last_history_item_id"):
            break
        after = page["last_history_item_id"]
    return items


def _known_voice(config_dir):
    """ElevenLabs history ids already counted, and the span (first, last unix time) of MYN's
    own lines written without an id (0.35.x)."""
    ids, first_unmarked, last_unmarked = set(), None, None
    try:
        with (Path(config_dir) / OTHER_NAME).open(encoding="utf-8", errors="replace") as log:
            for line in log:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict) or record.get("source") != "elevenlabs":
                    continue
                if str(record.get("id", "")).startswith("elevenlabs:"):
                    ids.add(record["id"][len("elevenlabs:"):])
                else:
                    try:
                        stamp = datetime.fromisoformat(record["at_utc"]).timestamp()
                    except (KeyError, TypeError, ValueError):
                        continue
                    first_unmarked = stamp if first_unmarked is None else min(first_unmarked, stamp)
                    last_unmarked = stamp if last_unmarked is None else max(last_unmarked, stamp)
    except OSError:
        pass
    return ids, (first_unmarked, last_unmarked)


def sync_elevenlabs(config_dir, key, now=None, opener=None, synced_until=None):
    """Add ElevenLabs requests from the account history that are not counted yet: speech
    made before MYN recorded it itself, from other apps, or whose local line was lost.
    MYN's own requests carry ElevenLabs' history id, so nothing is counted twice; lines
    MYN wrote before ids were kept (0.35.x) cover the time from their first one on.
    Only the text's length is used; no text is stored. Returns the number added."""
    now = now or datetime.now(timezone.utc)
    ids, (first_unmarked, last_unmarked) = _known_voice(config_dir)
    stop = (synced_until.timestamp() - 86400) if synced_until else None  # a day of overlap
    added = 0
    for item in _eleven_history(key, opener or urllib.request.urlopen, stop):
        stamp, history_id = item.get("date_unix"), str(item.get("history_item_id") or "")
        if not _number(stamp, 0, 1e11) or stamp >= now.timestamp() or not history_id or history_id in ids:
            continue
        if first_unmarked is not None and first_unmarked - 5 <= stamp <= last_unmarked + 60:
            continue  # already counted by MYN's own lines (written before ids were kept)
        text = item.get("text")
        characters = len(text) if isinstance(text, str) else \
            _count(item.get("character_count_change_to")) - _count(item.get("character_count_change_from"))
        if record_voice(config_dir, item.get("model_id"), characters,
                        datetime.fromtimestamp(stamp, timezone.utc), history_id):
            ids.add(history_id)
            added += 1
    return added


# ---- the daily update --------------------------------------------------------------
DAILY_STATE = "usage-daily.json"
DAILY_SUMMARY = "usage-daily.jsonl"


def daily_state(config_dir):
    try:
        state = json.loads((Path(config_dir) / DAILY_STATE).read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def write_daily_summaries(config_dir, now=None):
    """usage-daily.jsonl: one line per finished day (tokens, dollars, by source and use),
    rebuilt from all sources. Returns the number of days."""
    now = now or datetime.now().astimezone()
    calls = load_calls(config_dir, load_settings(config_dir), now.tzinfo)
    today = now.date()
    days = {}
    for call in calls:
        if call.when.date() >= today:
            continue
        day = days.setdefault(call.when.date().isoformat(), [])
        day.append(call)
    lines = []
    for date in sorted(days):
        part = days[date]
        total = _totals(part)
        import usage_detail
        lines.append(json.dumps({
            "entries": usage_detail.entries(part),
            "date": date, "tokens": total["tokens"], "cost_usd": round(total["cost_usd"], 2),
            "calls": total["calls"], "tokens_without_cost": total["unpriced_tokens"],
            "by_source": {name: {"tokens": value["tokens"], "cost_usd": round(value["cost_usd"], 2)}
                          for name, value in _group(part, lambda call: call.group)},
            "by_use": {name: {"tokens": value["tokens"], "cost_usd": round(value["cost_usd"], 2)}
                       for name, value in _group(part, lambda call: purpose_name(call.purpose))},
        }, ensure_ascii=False) + "\n")
    path = Path(config_dir) / DAILY_SUMMARY
    temporary = path.with_name(DAILY_SUMMARY + ".tmp")
    temporary.write_text("".join(lines), encoding="utf-8")
    os.replace(str(temporary), str(path))
    return len(lines)


def daily_update(config_dir, eleven_key=None, now=None, opener=None, force=False):
    """Once a day (local date): bring in ElevenLabs usage missing from the records and
    rebuild the daily summaries. Never raises; the outcome is kept in usage-daily.json
    (shown by myn_status and the statistics window). None when already done today."""
    now = now or datetime.now().astimezone()
    state = daily_state(config_dir)
    if state.get("last_run") == now.date().isoformat() and not force:
        return None
    if eleven_key:
        try:
            until = datetime.fromisoformat(state["elevenlabs_synced_until"]) \
                if state.get("elevenlabs_synced_until") else None
            added = sync_elevenlabs(config_dir, eleven_key, now.astimezone(timezone.utc), opener, until)
            state["elevenlabs"] = 'Synced; %d new requests' % added
            state["elevenlabs_synced_until"] = now.astimezone(timezone.utc).isoformat()
        except urllib.error.HTTPError as error:
            state["elevenlabs"] = 'Cannot read ElevenLabs history (HTTP %d%s)' % (
                error.code, ': The key may lack history access' if error.code in (401, 403) else "")
        except Exception as error:  # network, format: tried again tomorrow
            state["elevenlabs"] = 'Cannot read ElevenLabs history (%s)' % type(error).__name__
    else:
        state["elevenlabs"] = 'No ElevenLabs key; skipped'
    try:  # Claude Code's work on MYN, from its own logs (2026-10-18) — MYN's own config only
        import claude_code_usage
        import data_location
        if Path(config_dir).resolve() == data_location.user_data_dir().resolve():
            result = claude_code_usage.sync(config_dir)
            state["claude_code"] = 'Counted %d sessions, approximately USD %.2f' % (result["sessions"], result["cost_usd"])
    except Exception as error:
        state["claude_code"] = 'Could not read Claude Code records (%s)' % type(error).__name__
    try:
        import codex_usage
        import data_location
        if Path(config_dir).resolve() == data_location.user_data_dir().resolve():
            result = codex_usage.sync(config_dir)
            state["codex"] = 'Counted %d sessions, %s tokens; %s' % (
                result["sessions"], format(result["tokens"], ","), codex_usage.cost_text(result))
    except Exception as error:
        state["codex"] = 'Could not read Codex records (%s)' % type(error).__name__
    try:
        state["days"] = write_daily_summaries(config_dir, now)
        state["summary"] = "ok"
    except Exception as error:
        state["summary"] = 'Failed (%s)' % type(error).__name__
    state["last_run"] = now.date().isoformat()
    state["updated_at"] = now.isoformat(timespec="minutes")
    try:
        path = Path(config_dir) / DAILY_STATE
        temporary = path.with_name(DAILY_STATE + ".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(str(temporary), str(path))
    except OSError:
        pass
    return state
