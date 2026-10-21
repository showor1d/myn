"""Read-only day cards from the saved calendar, never from model prose."""
from datetime import datetime, timedelta

import agenda


def snapshot(config, from_date, to_date):
    start = datetime.strptime(from_date, "%Y-%m-%d")
    last = datetime.strptime(to_date, "%Y-%m-%d")
    end = last + timedelta(days=1)
    if end <= start or (end - start).days > 400:
        raise agenda.AgendaError('Invalid calendar display range.')
    events = agenda.occurrences(config, start, end)
    days = []
    day = start
    while day < end:
        following = day + timedelta(days=1)
        entries = []
        for event in events:
            finish = event.get("end") or event["start"] + (
                timedelta(days=1) if event["all_day"] else timedelta(0))
            if event["start"] >= following or (event["start"] < day and finish <= day):
                continue
            when = 'All day' if event["all_day"] else event["start"].strftime("%H:%M")
            if event["start"] < day:
                when = 'Continued from previous day'
            if event.get("end") and not event["all_day"]:
                when += " – " + (event["end"].strftime("%H:%M") if event["end"] <= following else 'Next day')
            entries.append({"id": event["id"], "title": event["title"], "time": when,
                            "location": event.get("location", ""), "note": event.get("note", "")})
        days.append({"date": day.strftime("%Y-%m-%d"), "weekday": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[day.weekday()],
                     "label": '%02d/%02d' % (day.month, day.day), "events": entries})
        day = following
    return {"cmd": "agenda_display", "from_date": from_date, "to_date": to_date, "days": days}
