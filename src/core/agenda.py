'MYN calendar: events are stored in config/calendar.json with a .bak backup. Times use Asia/Tokyo and YYYY-MM-DD HH:MM; all-day dates use YYYY-MM-DD. Daily, weekly, monthly and yearly repeats are supported. Each occurrence is reminded once, tracked in calendar-reminded.json.'

import calendar as _calendar
import json
import os
import threading
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    TOKYO = ZoneInfo("Asia/Tokyo")
except Exception:  # pragma: no cover - Windows without tzdata
    TOKYO = timezone(timedelta(hours=9))

FILE = "calendar.json"
REMINDED = "calendar-reminded.json"
REPEATS = ("none", "daily", "weekly", "monthly", "yearly")
REPEAT_NAMES = {"none": 'Never', "daily": 'Daily', "weekly": 'Weekly', "monthly": 'Monthly', "yearly": 'Yearly'}
MAX_EVENTS = 2000
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_LOCK = threading.Lock()


class AgendaError(ValueError):
    """A user-facing reason (Chinese)."""


def _now():
    return datetime.now(TOKYO).replace(tzinfo=None, second=0, microsecond=0)


def parse_when(text, field='Time'):
    """'2026-10-20 15:00' -> (datetime, False); '2026-10-20' -> (datetime at 00:00, True)."""
    text = str(text or "").strip().replace("T", " ").replace("/", "-")
    for pattern, all_day in (("%Y-%m-%d %H:%M", False), ("%Y-%m-%d %H:%M:%S", False), ("%Y-%m-%d", True)):
        try:
            return datetime.strptime(text, pattern).replace(second=0), all_day
        except ValueError:
            continue
    raise AgendaError('%s must use YYYY-MM-DD HH:MM (YYYY-MM-DD for all-day events).' % field)


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if path.exists():
        try:
            path.with_name(path.name + ".bak").write_bytes(path.read_bytes())
        except OSError:
            pass
    os.replace(str(temporary), str(path))


def events(config_dir):
    """All stored events (as saved), sorted by start."""
    try:
        value = json.loads((Path(config_dir) / FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError, UnicodeError) as error:
        raise AgendaError('Cannot read the calendar; the previous version is in calendar.json.bak.') from error
    items = [item for item in value if isinstance(item, dict) and isinstance(item.get("id"), str)
             and isinstance(item.get("title"), str) and isinstance(item.get("start"), str)] \
        if isinstance(value, list) else []
    return sorted(items, key=lambda item: item["start"])


def _clean(fields, current=None):
    """Validate an event's fields (new, or changes to `current`) -> the complete event."""
    event = dict(current or {})
    for key in ("title", "start", "end", "location", "note", "remind", "repeat", "all_day"):
        if key in fields:
            event[key] = fields[key]
    title = str(event.get("title") or "").strip()
    if not 1 <= len(title) <= 200:
        raise AgendaError('Event title must contain 1-200 characters.')
    start, all_day = parse_when(event.get("start"), 'Start time')
    if event.get("all_day") is True:
        all_day = True
    end_text = event.get("end")
    end = None
    if end_text not in (None, ""):
        end_raw = str(end_text).strip()
        if len(end_raw) <= 5 and ":" in end_raw:  # just a time: same day as the start
            end_raw = start.strftime("%Y-%m-%d ") + end_raw
        end, _ = parse_when(end_raw, 'End time')
        if end < start:
            raise AgendaError('End time cannot precede start time.')
    remind = event.get("remind")
    if remind in ("", None):
        remind = None
    elif type(remind) is not int or not 0 <= remind <= 10080:
        raise AgendaError('Reminder lead time must be 0-10080 minutes (up to one week).')
    repeat = event.get("repeat") or "none"
    if repeat not in REPEATS:
        raise AgendaError('Repeat must be never, daily, weekly, monthly or yearly.')
    return {"id": event.get("id") or uuid.uuid4().hex[:8], "title": title,
            "start": start.strftime("%Y-%m-%d") if all_day else start.strftime("%Y-%m-%d %H:%M"),
            "end": (end.strftime("%Y-%m-%d") if all_day else end.strftime("%Y-%m-%d %H:%M")) if end else "",
            "all_day": all_day, "location": str(event.get("location") or "").strip()[:200],
            "note": str(event.get("note") or "").strip()[:2000], "remind": remind, "repeat": repeat,
            "created": event.get("created") or _now().strftime("%Y-%m-%d %H:%M")}


def add(config_dir, **fields):
    with _LOCK:
        items = events(config_dir)
        if len(items) >= MAX_EVENTS:
            raise AgendaError('Too many events (maximum %d).' % MAX_EVENTS)
        event = _clean(fields)
        _write(Path(config_dir) / FILE, items + [event])
    return event


def update(config_dir, event_id, **fields):
    with _LOCK:
        items = events(config_dir)
        found = [item for item in items if item["id"] == event_id]
        if not found:
            raise AgendaError('Event not found. Check the event list first.')
        event = _clean(fields, found[0])
        _write(Path(config_dir) / FILE, [event if item["id"] == event_id else item for item in items])
    return event


def delete(config_dir, event_id):
    with _LOCK:
        items = events(config_dir)
        kept = [item for item in items if item["id"] != event_id]
        if len(kept) == len(items):
            raise AgendaError('Event not found.')
        _write(Path(config_dir) / FILE, kept)
    return True


def get(config_dir, event_id):
    for item in events(config_dir):
        if item["id"] == event_id:
            return item
    raise AgendaError('Event not found.')


def _shift(moment, repeat, steps):
    if repeat == "daily":
        return moment + timedelta(days=steps)
    if repeat == "weekly":
        return moment + timedelta(weeks=steps)
    months = steps if repeat == "monthly" else 12 * steps
    index = moment.year * 12 + moment.month - 1 + months
    year, month = divmod(index, 12)
    day = min(moment.day, _calendar.monthrange(year, month + 1)[1])  # 31st -> last day
    return moment.replace(year=year, month=month + 1, day=day)


def occurrences(config_dir, start, end):
    """Every event happening from `start` up to (not including) `end`, repeats expanded,
    sorted: [{"id", "title", "start": datetime, "end": datetime|None, "all_day", ...}]."""
    found = []
    for item in events(config_dir):
        first, all_day = parse_when(item["start"])
        all_day = all_day or bool(item.get("all_day"))
        length = None
        if item.get("end"):
            length = parse_when(item["end"])[0] - first
        repeat = item.get("repeat") or "none"
        step, moment = 0, first
        if repeat != "none" and first < start - timedelta(days=1):
            # jump close to the window instead of walking from the first occurrence
            span = {"daily": 1, "weekly": 7, "monthly": 28, "yearly": 365}[repeat]
            step = max(0, int((start - first).days / span) - 2)
            moment = _shift(first, repeat, step)
        while moment < end and step < 5000:
            finish = moment + length if length is not None else None
            reaches = (finish or moment + (timedelta(days=1) if all_day else timedelta(0))) > start
            if moment >= start or reaches:
                found.append(dict(item, start=moment, end=finish, all_day=all_day))
            if repeat == "none":
                break
            step += 1
            moment = _shift(first, repeat, step)
    return sorted(found, key=lambda event: (event["start"], event["title"]))


def describe(event, with_date=True):
    'Format an event for display and speech, including date, time, title and location.'
    start = event["start"]
    day = '%02d/%02d %s' % (start.month, start.day, WEEKDAYS[start.weekday()]) if with_date else ""
    if event.get("all_day"):
        when = (day + ' All day').strip()
    else:
        when = (day + " " + start.strftime("%H:%M")).strip()
        if event.get("end"):
            when += "–" + event["end"].strftime("%H:%M")
    text = when + " " + event["title"]
    if event.get("location"):
        text += "（%s）" % event["location"]
    return text


def for_model(config_dir, start, end, limit=60):
    return [{"id": event["id"], "title": event["title"],
             "start": event["start"].strftime("%Y-%m-%d") if event["all_day"] else event["start"].strftime("%Y-%m-%d %H:%M"),
             "end": (event["end"].strftime("%Y-%m-%d %H:%M") if event.get("end") else ""),
             "all_day": event["all_day"], "location": event.get("location", ""), "note": event.get("note", ""),
             "remind_minutes": event.get("remind"), "repeat": REPEAT_NAMES.get(event.get("repeat"), 'Never'),
             "said": describe(event)}
            for event in occurrences(config_dir, start, end)[:limit]]


def upcoming(config_dir, now=None, hours=48, limit=12):
    """A short agenda for the chat's context (data, not instructions)."""
    now = now or _now()
    day_start = now.replace(hour=0, minute=0)
    try:
        return [describe(event) for event in occurrences(config_dir, day_start, now + timedelta(hours=hours))
                if event["all_day"] or (event.get("end") or event["start"]) >= now][:limit]
    except (AgendaError, OSError):
        return []


def due_reminders(config_dir, now=None):
    """Occurrences whose reminder time has come and that were not reminded yet."""
    now = now or _now()
    path = Path(config_dir) / REMINDED
    try:
        done = set(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        done = set()
    due = []
    try:
        candidates = occurrences(config_dir, now - timedelta(hours=1), now + timedelta(days=8))
    except AgendaError:
        return []
    for event in candidates:
        if event.get("remind") is None:
            continue
        moment = event["start"] if not event["all_day"] else event["start"].replace(hour=9)
        key = "%s@%s" % (event["id"], moment.strftime("%Y-%m-%dT%H:%M"))
        at = moment - timedelta(minutes=event["remind"])
        if at <= now <= moment + timedelta(minutes=30) and key not in done:
            due.append(dict(event, minutes_left=max(0, int((moment - now).total_seconds() // 60))))
            done.add(key)
    if due:
        keep = sorted(done)[-500:]
        try:
            _write(path, keep)
        except OSError:
            pass
    return due


def reminder_text(event):
    minutes = event.get("minutes_left", 0)
    if event["all_day"]:
        lead = 'Today'
    elif minutes <= 0:
        lead = 'Now'
    elif minutes < 60:
        lead = 'In %d minutes' % minutes
    elif minutes % 60 == 0 and minutes < 1440:
        lead = 'In %d hours' % (minutes // 60)
    else:
        lead = event["start"].strftime('%m/%d %H:%M').lstrip("0")
    text = 'Event reminder: %s, %s' % (lead, event["title"])
    if event.get("location"):
        text += ', at %s' % event["location"]
    return text + "。"


def month_days(year, month):
    """Weeks of (date or None) for a month grid, Monday first."""
    weeks, row = [], [None] * date(year, month, 1).weekday()
    for day in range(1, _calendar.monthrange(year, month)[1] + 1):
        row.append(date(year, month, day))
        if len(row) == 7:
            weeks.append(row)
            row = []
    if row:
        weeks.append(row + [None] * (7 - len(row)))
    return weeks


def day_range(day):
    start = datetime.combine(day, time(0, 0))
    return start, start + timedelta(days=1)
