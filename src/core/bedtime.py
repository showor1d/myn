"""Once per night, a short real-calendar briefing at the user's bedtime sign-off.

No polling, fixed-hour alarm or restart catch-up. Speech uses a short-lived,
revision-bound permission, never the unconditional alarm exemption.
"""
import time
import uuid
from datetime import datetime, timedelta

import agenda
import agenda_display
import interaction


def prepare(config, revision, now=None):
    local = now or datetime.now(agenda.TOKYO)
    if 6 <= local.hour < 18:
        return None  # Don't turn an afternoon nap into a nightly briefing.
    target = local.date() + timedelta(days=1) if local.hour >= 18 else local.date()
    key = str(target)
    state = interaction.load(config)
    if (state["revision"] != revision or state["mode"] != "sleep" or state.get("pending")
            or state.get("bedtime_date") == key):
        return None
    # After midnight, "tomorrow's morning" means the upcoming day after this sleep.
    lead = 'Tomorrow' if local.hour >= 18 else 'After you wake up today'
    entries = agenda_display.snapshot(config, key, key)["days"][0]["events"]
    if not entries:
        text = 'Before you sleep: no events are recorded for %s. Good night.' % lead
    else:
        parts = []
        for entry in entries[:12]:
            parts.append("%s，%s%s" % (entry["time"], entry["title"][:100],
                         ', at ' + entry["location"][:60] if entry["location"] else ""))
        text = 'Before you sleep: %s has %d events: %s.' % (lead, len(entries), "；".join(parts))
        if len(entries) > 12:
            text += 'There are %d more. You can check the full calendar after waking up.' % (len(entries) - 12)
        text += 'Good night.'
    return {"text": text, "date": key, "revision": revision, "created": time.time()}


def claim(config, briefing, now=None):
    """Atomically consume today's briefing only immediately before UI speech."""
    stamp = time.time() if now is None else now
    with interaction.LOCK:
        state = interaction.load(config)
        if (state["revision"] != briefing["revision"] or state["mode"] != "sleep"
                or state.get("pending") or state.get("bedtime_date") == briefing["date"]
                or not 0 <= stamp - briefing["created"] <= 90
                or stamp - state["last_input"] > 120):
            return None
        token = "bedtime:" + uuid.uuid4().hex
        state["bedtime_date"] = briefing["date"]
        state["bedtime_speech"] = {"token": token, "revision": state["revision"], "expires": stamp + 180}
        interaction._save(config, state)
        return token


def allowed(state, purpose, now=None):
    grant = state.get("bedtime_speech") or {}
    return bool(grant.get("token") == purpose and grant.get("revision") == state["revision"]
                and state["mode"] == "sleep" and not state.get("pending")
                and (time.time() if now is None else now) < grant.get("expires", 0))
