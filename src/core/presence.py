'Estimate presence and likely sleep from message/speech timestamps and keyboard/mouse activity, never typed content. Sample Windows idle time once per minute and retain active spans for 30 days in presence.json. Track when MYN was running so unobserved periods are not mistaken for sleep. Ignore microphone noise. sleep_estimate interprets a silence of at least three hours with likelihood and reasons for conversational answers.'

import json
import os
import re
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

FILE = "presence.json"
KEEP_DAYS = 30
MERGE_GAP = timedelta(minutes=3)      # activity this close together is one span
MIN_SLEEP = timedelta(hours=3)        # a silence this long may be sleep
MAX_SLEEP = timedelta(hours=20)
SLEEP_CUES = re.compile(r"睡|困|晚安|休息了|躺下|good ?night|sleep|bed\b|累死|好累", re.I)
NOISE = re.compile(r"^(?:you|yeah|yes|no|ok|okay|oh|ah|uh|um|hmm|mm|mm-hmm|uh-huh|wow|thank you\.?|"
                   r"thanks|bye|嗯+|啊+|哦+|噢+|呃+|额+|唉+|哈+|好|对|是|喂|うん|はい|ああ)[.。!！?？,，…\s]*$", re.I)
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_LOCK = threading.Lock()


def _utc(value):
    stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _tokyo():
    import memory_query
    return memory_query.TOKYO


def local(moment, seconds=False):
    moment = _utc(moment).astimezone(_tokyo())
    return moment.strftime("%Y-%m-%d ") + WEEKDAYS[moment.weekday()] + \
        moment.strftime(" %H:%M:%S" if seconds else " %H:%M")


def say_span(delta):
    minutes = max(0, int(round(delta.total_seconds() / 60)))
    hours, minutes = divmod(minutes, 60)
    return ('%d hours %d minutes' % (hours, minutes)) if hours else ('%d minutes' % minutes)


def meaningful(text):
    """Something the user really said, not microphone noise."""
    text = str(text or "").strip()
    if len(re.sub(r"[\W_]", "", text)) <= 1:
        return False
    return not NOISE.match(text)


# ---- keyboard / mouse and MYN running ----------------------------------------------------

def _read(config_dir):
    try:
        value = json.loads((Path(config_dir) / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return {"input": [], "running": []}
    return {"input": [s for s in value.get("input", []) if isinstance(s, list) and len(s) == 2],
            "running": [s for s in value.get("running", []) if isinstance(s, list) and len(s) == 2]}


def _write(config_dir, value):
    path = Path(config_dir) / FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(str(temporary), str(path))


def _extend(spans, start, end, gap=MERGE_GAP):
    if spans and _utc(spans[-1][1]) + gap >= start:
        if end > _utc(spans[-1][1]):
            spans[-1][1] = end.isoformat()
    else:
        spans.append([start.isoformat(), end.isoformat()])


def note(config_dir, idle_seconds=None, now=None):
    """Once a minute from the window: MYN is running; and if the keyboard or mouse was used
    in the last minute (Windows' idle time), the user was at the computer."""
    now = now or datetime.now(timezone.utc)
    with _LOCK:
        value = _read(config_dir)
        _extend(value["running"], now - timedelta(seconds=70), now, gap=timedelta(minutes=5))
        if idle_seconds is not None and idle_seconds < 75:
            moment = now - timedelta(seconds=idle_seconds)
            _extend(value["input"], moment, moment)
        cutoff = now - timedelta(days=KEEP_DAYS)
        for key in ("input", "running"):
            value[key] = [span for span in value[key] if _utc(span[1]) >= cutoff][-5000:]
        _write(config_dir, value)


def windows_idle_seconds():
    """Seconds since the last keyboard or mouse input on this computer (Windows), or None."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        ticks = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
        return ((ticks - info.dwTime) & 0xFFFFFFFF) / 1000.0
    except Exception:
        return None


# ---- the estimate --------------------------------------------------------------------------

def _messages(db_path, since, until):
    try:
        with closing(sqlite3.connect(str(db_path))) as db:
            rows = db.execute("""SELECT created_at, display_text FROM conversation_messages
                WHERE role='user' AND deleted_at IS NULL AND origin!='ambient-unconfirmed'
                AND julianday(created_at)>=julianday(?)
                ORDER BY id""", (since.isoformat(),)).fetchall()
    except sqlite3.Error:
        return []
    found = []
    for created, text in rows:
        try:
            moment = _utc(created)
            if moment <= until:
                found.append((moment, text))
        except (TypeError, ValueError):
            continue
    return found


def _covered(spans, start, end):
    """How much of [start, end) MYN was running (0..1); None if nothing is known."""
    if not spans:
        return None
    total = 0.0
    for a, b in spans:
        a, b = max(_utc(a), start), min(_utc(b), end)
        if b > a:
            total += (b - a).total_seconds()
    return total / max(1.0, (end - start).total_seconds())


def sleep_estimate(db_path, config_dir, now=None, hours_back=48):
    'Return the latest sleep-like silence with approximate sleep/wake times, duration, likelihood, reasons and last words. Return None when no qualifying silence exists in the last two days.'
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=hours_back)
    value = _read(config_dir)
    events = [(moment, "message", text) for moment, text in _messages(db_path, since, now) if meaningful(text)]
    for a, b in value["input"]:
        for moment in (_utc(a), _utc(b)):
            if since <= moment <= now:
                events.append((moment, "input", ""))
    events.sort(key=lambda event: event[0])
    if not events:
        return None
    candidates = [(events[i][0], events[i + 1][0], i) for i in range(len(events) - 1)
                  if MIN_SLEEP <= events[i + 1][0] - events[i][0] <= MAX_SLEEP]
    ongoing = now - events[-1][0] >= MIN_SLEEP
    if ongoing:
        candidates.append((events[-1][0], None, len(events) - 1))
    if not candidates:
        return None
    start, end, index = candidates[-1]
    stop = end or now
    why, score = [], 0
    length = stop - start
    why.append('No activity for %s afterward' % say_span(length))
    last_words = next((text for moment, kind, text in reversed(events[:index + 1]) if kind == "message"), "")
    said_at = next((moment for moment, kind, _ in reversed(events[:index + 1]) if kind == "message"), None)
    if said_at and start - said_at <= timedelta(minutes=45) and SLEEP_CUES.search(last_words or ""):
        score += 2
        why.append('Last words mentioned sleep or tiredness: "%s"' % last_words[:30])
    hour = start.astimezone(_tokyo()).hour
    if hour >= 21 or hour < 10:
        score += 1
        why.append('Quiet began around %d:00, at night or early morning' % hour)
    if timedelta(hours=4) <= length <= timedelta(hours=13):
        score += 1
        why.append('The quiet interval resembles a sleep period')
    covered = _covered(value["running"], start, stop)
    if covered is not None and covered >= .8:
        quiet_input = not any(kind == "input" and start < moment < stop for moment, kind, _ in events)
        if quiet_input:
            score += 1
            why.append('MYN was running, and there was no keyboard or mouse activity')
    elif covered is not None and covered < .3:
        why.append('MYN was mostly off; inferred from chat only')
    else:
        why.append('Keyboard and mouse activity were not tracked then; inferred from chat only')
    likelihood = 'Very likely' if score >= 3 else 'Possible' if score >= 2 else 'Uncertain'
    result = {"fell_asleep_around": local(start, seconds=True), "woke_up_around": local(end, seconds=True) if end else None,
              "slept_about": say_span(length), "still_away_now": end is None, "likelihood": likelihood,
              "why": why, "last_words_before": (last_words or "")[:60]}
    return result
