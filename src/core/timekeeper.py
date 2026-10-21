'Countdown timers persist their end times in timers.json and support pause, resume, extension and cancellation. Scheduled tasks in scheduled-tasks.json run once or recur daily, on weekdays/weekends, selected weekdays, a monthly date, or at intervals with optional time windows. say speaks text; ask makes a Claude request without performing actions requiring consent; action performs a fixed permitted computer action. Confirm sensitive actions at task creation. Times use Asia/Tokyo. Runs more than GRACE minutes late are recorded as missed; recurring tasks continue from now.'

import json
import math
import os
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    TOKYO = ZoneInfo("Asia/Tokyo")
except Exception:  # pragma: no cover - Windows without tzdata
    from datetime import timezone
    TOKYO = timezone(timedelta(hours=9))

TIMERS = "timers.json"
TASKS = "scheduled-tasks.json"
MAX_TIMERS = 20
MAX_TIMER_SECONDS = 24 * 3600
MAX_TASKS = 200
GRACE_MINUTES = 15
KINDS = {"say": 'Say something', "ask": 'Ask MYN', "action": 'Perform an action'}
REPEATS = {"once": 'Once', "daily": 'Daily', "weekdays": 'Weekdays', "weekends": 'Weekends',
           "weekly": 'Weekly', "monthly": 'Monthly', "interval": 'At intervals'}
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
# Actions a scheduled task may do (fixed, no pictures, no background tasks, no files).
ACTIONS = ("set_volume", "media_control", "open_settings", "set_dark_mode", "lock_screen", "power",
           "open_url", "open_app", "open_path", "focus_window", "window_state", "press_keys")
_LOCK = threading.RLock()


class TimerError(ValueError):
    """A user-facing reason (Chinese)."""


def local_now():
    return datetime.now(TOKYO).replace(tzinfo=None, microsecond=0)


def _read(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError, UnicodeError) as error:
        raise TimerError('Cannot read timer or scheduled task records; the original file was preserved.') from error
    if not isinstance(value, list) or any(not isinstance(item, dict) or
                                        not isinstance(item.get("id"), str) for item in value):
        raise TimerError('Timer or scheduled task data is damaged; the original file was preserved.')
    return value


def _write(path, items):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(str(temporary), str(path))


def say_duration(seconds):
    'Format seconds as hours, minutes and seconds for display and speech.'
    seconds = max(0, int(round(seconds)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if hours:
        parts.append('%d hours' % hours)
    if minutes:
        parts.append('%d minutes' % minutes)
    if secs or not parts:
        parts.append('%d seconds' % secs)
    return " ".join(parts)


def clock(seconds):
    """Remaining time as 05:03 or 1:05:03."""
    seconds = max(0, int(math.ceil(seconds)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return "%d:%02d:%02d" % (hours, minutes, secs) if hours else "%02d:%02d" % (minutes, secs)


# ---- countdown timers ----------------------------------------------------------------

def _seconds(minutes=None, seconds=None):
    total = 0.0
    for value, factor in ((minutes, 60), (seconds, 1)):
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise TimerError('Invalid duration.')
        total += value * factor
    if not 1 <= total <= MAX_TIMER_SECONDS:
        raise TimerError('Timer duration must be between 1 second and 24 hours.')
    return round(total)


def timers(config_dir, now=None):
    """Every timer with what is left: [{"id", "label", "seconds", "left", "state", ...}]."""
    now = time.time() if now is None else now
    found = []
    for item in _read(Path(config_dir) / TIMERS):
        try:
            left = float(item["left"]) if item.get("state") == "paused" else float(item["ends_at"]) - now
            found.append(dict(item, left=max(0.0, left)))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(found, key=lambda item: (item.get("state") == "paused", item["left"]))


def start_timer(config_dir, minutes=None, seconds=None, label="", now=None):
    now = time.time() if now is None else now
    total = _seconds(minutes, seconds)
    label = str(label or "").strip()[:60]
    with _LOCK:
        items = _read(Path(config_dir) / TIMERS)
        if len(items) >= MAX_TIMERS:
            raise TimerError('Too many timers (maximum %d).' % MAX_TIMERS)
        item = {"id": uuid.uuid4().hex[:6], "label": label, "seconds": total, "state": "running",
                "ends_at": now + total, "started_at": now}
        _write(Path(config_dir) / TIMERS, items + [item])
    return dict(item, left=float(total))


def _pick(items, timer_id):
    if timer_id:
        found = [item for item in items if item["id"] == timer_id]
        if not found:
            raise TimerError('Timer not found.')
        return found[0]
    if len(items) == 1:
        return items[0]
    raise TimerError('No timers.' if not items else 'Several timers exist. Specify which one.')


def control_timer(config_dir, action, timer_id=None, minutes=None, seconds=None, now=None):
    """pause / resume / cancel / add (minutes or seconds; negative takes time off)."""
    now = time.time() if now is None else now
    with _LOCK:
        path = Path(config_dir) / TIMERS
        items = _read(path)
        item = _pick(items, timer_id)
        if action == "cancel":
            items = [other for other in items if other is not item]
        elif action == "pause":
            if item.get("state") == "paused":
                raise TimerError('This timer is already paused.')
            item.update(state="paused", left=max(0.0, float(item["ends_at"]) - now))
            item.pop("ends_at", None)
        elif action == "resume":
            if item.get("state") != "paused":
                raise TimerError('This timer is not paused.')
            item.update(state="running", ends_at=now + float(item.get("left", 0)))
            item.pop("left", None)
        elif action == "add":
            change = 0.0
            for value, factor in ((minutes, 60), (seconds, 1)):
                if value is not None:
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        raise TimerError('Invalid duration.')
                    change += value * factor
            if not change:
                raise TimerError('How much time should be added?')
            if item.get("state") == "paused":
                item["left"] = min(MAX_TIMER_SECONDS, max(1.0, float(item["left"]) + change))
            else:
                item["ends_at"] = min(now + MAX_TIMER_SECONDS, max(now + 1, float(item["ends_at"]) + change))
            item["seconds"] = max(1, int(item.get("seconds", 0) + change))
        else:
            raise TimerError('Unknown timer action.')
        _write(path, items)
    if action == "cancel":
        return dict(item, left=0.0, state="cancelled")
    return [entry for entry in timers(config_dir, now) if entry["id"] == item["id"]][0]


def due_timers(config_dir, now=None):
    """Remove and return the timers that have run out (the window says them)."""
    now = time.time() if now is None else now
    with _LOCK:
        path = Path(config_dir) / TIMERS
        items = _read(path)
        due = [item for item in items if item.get("state") == "running" and float(item.get("ends_at", 0)) <= now]
        if due:
            _write(path, [item for item in items if item not in due])
    return due


def timer_name(item):
    return item.get("label") or '%s timer' % say_duration(item.get("seconds", 0))


def timer_done_text(item):
    label = item.get("label")
    length = say_duration(item.get("seconds", 0))
    return 'Time is up: %s (%s).' % (label, length) if label else 'Time is up for %s.' % length


def timer_for_model(item):
    return {"id": item["id"], "label": item.get("label", ""), "length": say_duration(item.get("seconds", 0)),
            "left": say_duration(item["left"]), "state": 'Paused' if item.get("state") == "paused" else 'Running'}


# ---- scheduled tasks -------------------------------------------------------------------

def _hhmm(text, field='Time'):
    try:
        return datetime.strptime(str(text).strip(), "%H:%M").time()
    except (TypeError, ValueError):
        raise TimerError('%s must use HH:MM (24-hour time).' % field) from None


def _day(text):
    try:
        return datetime.strptime(str(text).strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise TimerError('Date must use YYYY-MM-DD.') from None


def _month_day(year, month, wanted):
    import calendar
    return date(year, month, min(wanted, calendar.monthrange(year, month)[1]))


def next_after(task, after):
    """Next occurrence, honoring durable one-occurrence exceptions."""
    skipped = set(task.get("skipped_runs", ()))
    for _ in range(len(skipped) + 1):
        moment = _next_regular(task, after)
        if moment is None or moment.strftime("%Y-%m-%d %H:%M") not in skipped:
            return moment
        after = moment
    return None


def _next_regular(task, after):
    """The first time the task should run strictly after `after` (naive local), or None."""
    repeat = task["repeat"]
    at = _hhmm(task["time"])
    if repeat == "once":
        moment = datetime.combine(_day(task["date"]), at)
        return moment if moment > after else None
    start = _day(task["date"]) if task.get("date") else after.date()
    if repeat == "interval":
        every = timedelta(minutes=task["every_minutes"])
        if task.get("from_time"):
            begin, end = _hhmm(task["from_time"]), _hhmm(task.get("to_time") or "23:59")
            day = max(after.date(), start)
            for _ in range(400):
                moment = datetime.combine(day, begin)
                last = datetime.combine(day, end)
                if moment <= after:
                    steps = int((after - moment) // every) + 1
                    moment += steps * every
                if moment <= last and moment > after:
                    return moment
                day += timedelta(days=1)
            return None
        anchor = datetime.combine(start, at)
        if anchor > after:
            return anchor
        steps = int((after - anchor) // every) + 1
        return anchor + steps * every
    day = max(after.date(), start)
    for _ in range(400):
        moment = datetime.combine(day, at)
        if moment > after:
            weekday = day.weekday()
            if repeat == "daily" or (repeat == "weekdays" and weekday < 5) or \
                    (repeat == "weekends" and weekday >= 5) or \
                    (repeat == "weekly" and weekday in task.get("days", ())) or \
                    (repeat == "monthly" and day == _month_day(day.year, day.month, task["day_of_month"])):
                return moment
        day += timedelta(days=1)
    return None


def describe_when(task):
    repeat, at = task["repeat"], task["time"]
    if repeat == "once":
        moment = datetime.combine(_day(task["date"]), _hhmm(at))
        return '%02d/%02d %s %s (once)' % (moment.month, moment.day, WEEKDAYS[moment.weekday()], at)
    if repeat == "weekly":
        return 'Every %s at %s' % ("、".join(WEEKDAYS[d] for d in sorted(task.get("days", ()))), at)
    if repeat == "monthly":
        return 'Day %d of each month at %s' % (task["day_of_month"], at)
    if repeat == "interval":
        every = task["every_minutes"]
        span = '%d hours' % (every // 60) if every % 60 == 0 else '%d minutes' % every
        window = "（%s–%s）" % (task["from_time"], task.get("to_time") or "23:59") if task.get("from_time") else ""
        return 'Every %s%s' % (span, window)
    return "%s %s" % (REPEATS[repeat], at)


def describe_what(task):
    if task["kind"] == "action":
        return 'Action: ' + (task.get("action_preview") or task["action"]["name"])
    return ('Say: ' if task["kind"] == "say" else 'Ask: ') + task["text"]


def tasks(config_dir):
    return sorted(_read(Path(config_dir) / TASKS), key=lambda t: (not t.get("enabled", True), t.get("next_run") or "9"))


def get_task(config_dir, task_id):
    for task in _read(Path(config_dir) / TASKS):
        if task["id"] == task_id:
            return task
    raise TimerError('Scheduled task not found. Check the list first.')


def _clean(fields, current=None, now=None):
    """Validate a task (new, or changes to `current`) -> the complete task."""
    now = now or local_now()
    task = dict(current or {})
    for key in ("name", "kind", "text", "action", "repeat", "time", "date", "days", "day_of_month",
                "every_minutes", "from_time", "to_time", "enabled", "category", "ringtone_id", "ringtone_name"):
        if key in fields and fields[key] is not None:
            task[key] = fields[key]
    kind = task.get("kind")
    if task.get('ringtone_id') and (task.get('category') != 'alarm' or kind != 'say'):
        raise TimerError('Music sounds are available only for alarms.')
    if task.get("category") == "alarm" and kind != "say":
        raise TimerError('Alarms can use voice or music, but cannot execute background tasks or computer actions.')
    if kind not in KINDS:
        raise TimerError('Scheduled task type must be say, ask or action.')
    if kind in ("say", "ask"):
        text = str(task.get("text") or "").strip()
        if not 1 <= len(text) <= (300 if kind == "say" else 1500):
            raise TimerError('Spoken text or question cannot be empty or too long.')
        task["text"] = text
        task.pop("action", None)
    else:
        action = task.get("action")
        if not isinstance(action, dict) or action.get("name") not in ACTIONS or \
                not isinstance(action.get("arguments", {}), dict):
            raise TimerError('Actions available to scheduled tasks: ' + "、".join(ACTIONS) + "。")
        task["action"] = {"name": action["name"], "arguments": dict(action.get("arguments") or {})}
        task.pop("text", None)
    repeat = task.get("repeat") or "once"
    if repeat not in REPEATS:
        raise TimerError('Recurrence must be once, daily, weekdays, weekends, weekly, monthly or interval.')
    task["repeat"] = repeat
    if repeat == "interval":
        every = task.get("every_minutes")
        if type(every) is not int or not 5 <= every <= 1440:
            raise TimerError('Interval must be 5-1440 minutes. Use a timer for shorter durations.')
        for key in ("from_time", "to_time"):
            if task.get(key):
                task[key] = _hhmm(task[key], 'Time window').strftime("%H:%M")
            else:
                task.pop(key, None)
        if task.get("to_time") and not task.get("from_time"):
            raise TimerError('An end time also requires a start time.')
        if not task.get("time"):
            first = now + timedelta(minutes=every)
            task.setdefault("date", first.strftime("%Y-%m-%d"))
            task["time"] = first.strftime("%H:%M")
    if not task.get("time"):
        raise TimerError('What time? (HH:MM)')
    task["time"] = _hhmm(task["time"]).strftime("%H:%M")
    if repeat == "once":
        if not task.get("date"):
            today = datetime.combine(now.date(), _hhmm(task["time"]))
            task["date"] = (today if today > now else today + timedelta(days=1)).strftime("%Y-%m-%d")
    if task.get("date"):
        task["date"] = _day(task["date"]).strftime("%Y-%m-%d")
    else:
        task.pop("date", None)
    if repeat == "weekly":
        days = task.get("days")
        if not isinstance(days, list) or not days or \
                any(type(d) is not int or not 0 <= d <= 6 for d in days):
            raise TimerError('Which weekdays? (0=Monday ... 6=Sunday)')
        task["days"] = sorted(set(days))
    else:
        task.pop("days", None)
    if repeat == "monthly":
        wanted = task.get("day_of_month")
        if type(wanted) is not int or not 1 <= wanted <= 31:
            raise TimerError('Which day of the month? (1-31)')
    else:
        task.pop("day_of_month", None)
    if repeat != "interval":
        task.pop("every_minutes", None)
        task.pop("from_time", None)
        task.pop("to_time", None)
    name = str(task.get("name") or "").strip()[:60]
    task["name"] = name or (task.get("text", "")[:20] if kind != "action" else task["action"]["name"])
    task["enabled"] = bool(task.get("enabled", True))
    task.setdefault("id", uuid.uuid4().hex[:6])
    task.setdefault("created", now.strftime("%Y-%m-%d %H:%M"))
    nxt = next_after(task, now)
    if nxt is None and task["enabled"]:
        if repeat == "once":
            raise TimerError('This time is in the past.')
        raise TimerError('Cannot calculate the next run time.')
    task["next_run"] = nxt.strftime("%Y-%m-%d %H:%M") if task["enabled"] and nxt else ""
    if nxt and task["enabled"]:
        task.pop("finished", None)
    return task


def add_task(config_dir, now=None, **fields):
    if 'ringtone_id' in fields:
        import alarm_music
        try:
            fields.update(alarm_music.sound(config_dir, fields['ringtone_id']))
        except ValueError as error:
            raise TimerError(str(error)) from None
    elif fields.get('category') == 'alarm':
        import alarm_music
        default = alarm_music.settings(config_dir)
        fields.update(ringtone_id=default['ringtone_id'], ringtone_name=default['ringtone_name'])
    with _LOCK:
        items = _read(Path(config_dir) / TASKS)
        if len(items) >= MAX_TASKS:
            raise TimerError('Too many scheduled tasks (maximum %d).' % MAX_TASKS)
        task = _clean(fields, now=now)
        _write(Path(config_dir) / TASKS, items + [task])
    return task


def update_task(config_dir, task_id, now=None, **fields):
    if 'ringtone_id' in fields:
        import alarm_music
        try:
            fields.update(alarm_music.sound(config_dir, fields['ringtone_id']))
        except ValueError as error:
            raise TimerError(str(error)) from None
    with _LOCK:
        items = _read(Path(config_dir) / TASKS)
        found = [item for item in items if item["id"] == task_id]
        if not found:
            raise TimerError('Scheduled task not found. Check the list first.')
        current = dict(found[0])
        if fields.get("kind") and fields["kind"] != current["kind"]:
            raise TimerError('Scheduled task type cannot be changed. Delete it and create another.')
        if "action" in fields and fields["action"] is not None:
            raise TimerError('Scheduled task actions cannot be edited directly. Delete and recreate the task with confirmation.')
        task = _clean(fields, current, now=now)
        _write(Path(config_dir) / TASKS, [task if item["id"] == task_id else item for item in items])
    return task


def delete_task(config_dir, task_id):
    with _LOCK:
        items = _read(Path(config_dir) / TASKS)
        kept = [item for item in items if item["id"] != task_id]
        if len(kept) == len(items):
            raise TimerError('Scheduled task not found.')
        _write(Path(config_dir) / TASKS, kept)
    return True


def skip_alarm(config_dir, task_id, day, at=None, now=None):
    """Persist one exact occurrence exception; never change the recurrence rule."""
    from alarm_kind import is_alarm
    now = now or local_now()
    with _LOCK:
        path = Path(config_dir) / TASKS
        items = _read(path)
        task = _pick(items, task_id)
        if not is_alarm(task):
            raise TimerError('This record is not an alarm.')
        moment = datetime.combine(_day(day), _hhmm(at or task["time"]))
        stamp = moment.strftime("%Y-%m-%d %H:%M")
        if moment <= now:
            raise TimerError('This alarm time is in the past. For an actively ringing alarm, stop the current occurrence.')
        if _next_regular(task, moment - timedelta(seconds=1)) != moment:
            raise TimerError('No alarm occurs at this date and time; recurrence was not changed.')
        skipped = set(task.get("skipped_runs", ()))
        skipped.add(stamp)
        task["skipped_runs"] = sorted(skipped)
        if task.get("next_run") == stamp:
            nxt = next_after(task, moment)
            task["next_run"] = nxt.strftime("%Y-%m-%d %H:%M") if nxt else ""
            if nxt is None:
                task.update(enabled=False, finished=True)
        task["last_result"] = 'Skipped %s; other dates unchanged' % stamp
        _write(path, items)
        if get_task(config_dir, task_id) != task:
            raise TimerError('Saved alarm exception could not be verified. Check the list first.')
        return dict(task)


def due_tasks(config_dir, now=None):
    """Tasks whose time has come: returned to run now; each moves on to its next time
    (a one-off task is then switched off). Runs more than GRACE minutes late are not
    returned but noted as missed."""
    now = now or local_now()
    due = []
    with _LOCK:
        path = Path(config_dir) / TASKS
        items = _read(path)
        changed = False
        for task in items:
            if not task.get("enabled", True) or not task.get("next_run"):
                continue
            try:
                when = datetime.strptime(task["next_run"], "%Y-%m-%d %H:%M")
            except (TypeError, ValueError):
                continue
            if when > now:
                continue
            changed = True
            if task["next_run"] in task.get("skipped_runs", ()):
                task["last_result"] = 'Skipped %s' % task["next_run"]
            elif now - when > timedelta(minutes=GRACE_MINUTES):
                task["last_result"] = 'Missed %s (checked over 15 minutes late; this does not establish shutdown, sleep or a program pause)' % task["next_run"]
                task["missed"] = task.get("missed", 0) + 1
            else:
                from alarm_kind import is_alarm
                if is_alarm(task):
                    import alarm_ringing
                    alarm_ringing.activate(config_dir, "schedule:" + task["id"],
                                           task["next_run"], task["text"],
                                           task.get('ringtone_id', ''), task.get('ringtone_name', ''))
                due.append(dict(task, late_minutes=int((now - when).total_seconds() // 60)))
                task["last_run"] = now.strftime("%Y-%m-%d %H:%M")
                task["runs"] = task.get("runs", 0) + 1
                task["last_result"] = 'Dispatched when due; execution or audio playback is not yet confirmed'
            try:
                nxt = next_after(task, now)
            except TimerError:
                nxt = None
            if nxt is None:
                task["enabled"] = False
                task["next_run"] = ""
                task["finished"] = True
            else:
                task["next_run"] = nxt.strftime("%Y-%m-%d %H:%M")
        if changed:
            _write(path, items)
    return due


def note_result(config_dir, task_id, result):
    """What happened the last time (shown in the list and to the model)."""
    with _LOCK:
        path = Path(config_dir) / TASKS
        items = _read(path)
        for task in items:
            if task["id"] == task_id:
                task["last_result"] = str(result)[:200]
                _write(path, items)
                return


def task_for_model(task):
    return {"id": task["id"], "name": task["name"], "kind": KINDS[task["kind"]], "when": describe_when(task),
            "what": describe_what(task), "enabled": task.get("enabled", True),
            "next_run": task.get("next_run") or "", "last_run": task.get("last_run", ""),
            "last_result": task.get("last_result", ""), "finished": bool(task.get("finished")),
            "repeat": task["repeat"], "time": task["time"],
            "ringtone_id": task.get('ringtone_id', ''), "ringtone_name": task.get('ringtone_name', ''),
            "skipped_runs": list(task.get("skipped_runs", ()))}
