"""Read-only main-window timer summary. Never creates, fires or cancels tasks."""
from datetime import datetime
import time
import local_actions
import timekeeper
from alarm_kind import is_alarm


def snapshot(config, now=None):
    now = time.time() if now is None else now
    items = []
    alarm_upcoming = False
    for item in timekeeper.timers(config, now=now):
        items.append(dict(id="timer:" + item["id"], label=item.get("label") or 'Timer',
                          kind="countdown", paused=item.get("state") == "paused",
                          remaining=item["left"], total=item["seconds"], when=""))
    for item in local_actions.reminders(config):
        due = datetime.fromisoformat(item["due_utc"]).timestamp()
        if is_alarm(item):
            alarm_upcoming = alarm_upcoming or 0 <= due - now <= 24 * 3600
            continue
        local = datetime.fromtimestamp(due, timekeeper.TOKYO)
        items.append(dict(id="reminder:" + item["id"], label=item["text"], kind="scheduled",
                          paused=False, remaining=max(0, due - now), total=0,
                          when='%02d/%02d %02d:%02d' % (local.month, local.day, local.hour, local.minute)))
    for task in timekeeper.tasks(config):
        if not task.get("enabled", True) or not task.get("next_run"):
            continue
        due = datetime.strptime(task["next_run"], "%Y-%m-%d %H:%M").replace(tzinfo=timekeeper.TOKYO).timestamp()
        if is_alarm(task):
            alarm_upcoming = alarm_upcoming or 0 <= due - now <= 24 * 3600
            continue
        items.append(dict(id="schedule:" + task["id"], label=task["name"], kind="scheduled",
                          paused=False, remaining=max(0, due - now), total=0,
                          when=task["next_run"][5:]))
    items.sort(key=lambda i: (i["paused"], i["remaining"], i["id"]))
    return dict(cmd="timers", items=items[:3], count=len(items),
                alarm_upcoming=alarm_upcoming)
