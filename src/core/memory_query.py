"""Hybrid memory retrieval with Tokyo calendar windows and existing RAG."""

import json
import re
from datetime import datetime, time, timedelta, timezone
from data_location import TOKYO

import storage
from semantic_search import ask as semantic_ask

# Accept full dates or month/day dates in supported Chinese and numeric formats.
DATE_RE = re.compile(r"(?<!\d)(?:(20\d{2})\s*(?:年|[-/])\s*(\d{1,2})\s*(?:月|[-/])\s*(\d{1,2})\s*日?"
                     r"|(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?)")
ENGLISH_STOP = {"search", "find", "for", "and", "or", "the", "on", "in", "my", "me",
                "did", "do", "i", "mention", "yesterday", "today", "records", "memory",
                "memories", "events", "event", "last", "week", "both"}


def saved_window(text, now=None):
    """Return a half-open Tokyo calendar interval, or None."""
    now = (now or datetime.now(TOKYO)).astimezone(TOKYO)
    match = DATE_RE.search(text)
    if match:
        try:
            if match.group(1):
                day = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)),
                               tzinfo=TOKYO).date()
            else:
                # Without a year, the most recent such date (not one in the future).
                day = datetime(now.year, int(match.group(4)), int(match.group(5)), tzinfo=TOKYO).date()
                if day > now.date():
                    day = day.replace(year=now.year - 1)
        except ValueError:
            raise ValueError('Invalid date.') from None
        days = 1
    elif re.search(r"前天|day before yesterday", text, re.I):
        day, days = now.date() - timedelta(days=2), 1
    elif re.search(r"昨天|昨日|\byesterday\b", text, re.I):
        day, days = now.date() - timedelta(days=1), 1
    elif re.search(r"今天|今日|\btoday\b", text, re.I):
        day, days = now.date(), 1
    elif re.search(r"上周|上星期|\blast week\b", text, re.I):
        day, days = now.date() - timedelta(days=now.weekday() + 7), 7
    elif re.search(r"本周|这周|这星期|\bthis week\b", text, re.I):
        day, days = now.date() - timedelta(days=now.weekday()), 7
    else:
        return None
    start = datetime.combine(day, time.min, TOKYO)
    return start, start + timedelta(days=days)


def _filters(text):
    category = "life" if re.search(r"生活(?:类|分类|事件|记录)|life (?:events?|memories)", text, re.I) else None
    memory_type = "event" if re.search(r"事件|events?", text, re.I) else None
    return category, memory_type


def _broad_date_request(text):
    reduced = DATE_RE.sub("", text)
    reduced = re.sub(r"昨天|昨日|今天|今日|前天|上周|上星期|本周|这周|这星期|"
                     r"搜索|查找|查询|列出|请|帮我|保存的|的|生活|事件|记忆|记录|"
                     r"发生了什么|有哪些|什么|哪些", "", reduced)
    reduced = re.sub(r"\b(?:search|find|list|saved|on|for|the|of|life|events?|memories|"
                     r"day before yesterday|yesterday|today|last week|this week)\b", "", reduced,
                     flags=re.I)
    reduced = re.sub(r"[\s，。？！?!.]", "", reduced)
    return not reduced


def _keyword_hits(query, db_path):
    words = [word for word in re.findall(r"[a-z]{3,}", query.casefold())
             if word not in ENGLISH_STOP]
    hits = {}
    for word in dict.fromkeys(words[:5]):
        for row in storage.search_memories(word, db_path, limit=20):
            hits[row["id"]] = row
    return hits


def _event_at(row):
    try:
        metadata = json.loads(row.get("metadata") or "{}")
    except (TypeError, ValueError):
        return None
    value = metadata.get("event_at") or metadata.get("event_time")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.isoformat() if parsed.tzinfo is not None else None


def retrieve(query, db_path, limit=8, now=None, start_at=None, end_at=None,
             category=None, memory_type=None):
    """Return active evidence with a relevance score (not factual certainty)."""
    if not isinstance(query, str) or not query.strip() or not 1 <= len(query) <= 500:
        raise ValueError('Memory query must contain 1-500 characters.')
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError('Invalid memory result count.')
    inferred_category, inferred_type = _filters(query)
    category = category or inferred_category
    memory_type = memory_type or inferred_type
    window = (start_at, end_at) if start_at is not None and end_at is not None else saved_window(query, now)
    dated = storage.memories_in_saved_range(*window, db_path, category=category,
                                             memory_type=memory_type) if window else None
    semantic = semantic_ask(query, db_path, limit=200)
    semantic_scores = {row["id"]: row["score"] for row in semantic}
    keyword = _keyword_hits(query, db_path)
    if dated is not None:
        rows = dated if _broad_date_request(query) else [
            row for row in dated if row["id"] in semantic_scores or row["id"] in keyword]
        # A date and a broad question can identify the right record even when
        # the user's language shares no words with its original text.
        if not rows:
            rows = dated
    else:
        combined = {row["id"]: row for row in semantic}
        combined.update(keyword)
        rows = list(combined.values())
        if category:
            rows = [row for row in rows if row["category"] == category]
        if memory_type:
            rows = [row for row in rows if row["memory_type"] == memory_type]
    rows.sort(key=lambda row: (-max(semantic_scores.get(row["id"], 0),
                                     .85 if row["id"] in keyword else 0,
                                     .70 if dated is not None and _broad_date_request(query) else 0,
                                     .10 if dated is not None else 0),
                               -row["id"]))
    result = []
    for row in rows[:limit]:
        score = max(semantic_scores.get(row["id"], 0),
                    .85 if row["id"] in keyword else 0,
                    .70 if dated is not None and _broad_date_request(query) else 0,
                    .10 if dated is not None else 0)
        result.append(dict(row, memory_id=row["id"], saved_at=row["created_at"],
                           event_at=_event_at(row), confidence=round(min(1.0, score), 3),
                           confidence_kind="retrieval_relevance"))
    return result


def is_memory_request(text):
    if re.search(r"20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}.*\[(?:life|event|study|health|fitness|goal|idea|people|update|other)/",
                 text, re.I):
        return True
    if re.search(r"(?:昨天|昨日|前天|上周|今天|今日|本周).{0,24}(?:早起|起床|做了什么|发生了什么|吃了什么)", text):
        return True
    if re.match(r"^\s*(?:搜索|查找|查询|列出)", text) and \
            (saved_window(text) or re.search(r"记忆|生活事件|breakfast|lunch", text, re.I)):
        return True
    return bool(re.search(r"我.{0,16}(?:提到|说过|记录|记忆|有没有提过)|"
                          r"(?:记忆|记录).{0,12}(?:原文|昨天|今天|上周)", text, re.I))
