'Computer actions follow saved permission groups and confirmation policy. Groups cover media, system settings/power, opening URLs/apps/paths, reminders, authorized folder writes, background tasks, evolution, vision, windows and calendar. Direct actions include volume, media, settings pages, theme, lock, URLs and reminders. Program launch, file access/writes and power actions follow their confirmation policy. Permission changes always require actual fresh confirmation, even under a never-confirm policy. Chat tools offer neither file deletion nor arbitrary commands. Diagnostics log action name and success/error only, never file contents. Platform abstracts OS calls for isolated testing.'

import base64
import json
import os
import re
import subprocess
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import local_tools

SETTINGS_NAME = "actions-settings.json"
REMINDERS_NAME = "reminders.json"
MAX_WRITE_CHARS = 200000
MAX_REMINDERS = 100
CONFIRM_POLICIES = ("risky", "always", "never")
GROUPS = {"media": 'Media and volume', "system": 'System settings and power', "open": 'Open websites, programs and files',
          "reminders": 'Alarms, reminders, timers and scheduled tasks',
          "files": 'Write files (only in authorized folders)', "agent": 'Background tasks (multi-step work)',
          "self_fix": 'Evolution (adopt a plan, then test, back up and update automatically)',
          "vision": 'View screens and images (screenshots are sent to Claude)',
          "windows": 'Windows and shortcuts (switch windows, press keys, type text)',
          "calendar": "Calendar (MYN's own events)"}
_LOCK = threading.Lock()

try:
    from zoneinfo import ZoneInfo
    TOKYO = ZoneInfo("Asia/Tokyo")
except Exception:  # pragma: no cover
    TOKYO = timezone(timedelta(hours=9))


@dataclass(frozen=True)
class ActionSettings:
    media: bool = True
    system: bool = True
    open: bool = True
    reminders: bool = True
    files: bool = True
    agent: bool = True
    self_fix: bool = True
    vision: bool = True
    windows: bool = True
    calendar: bool = True
    confirm: str = "risky"  # risky: ask only for the ASK level; always; never

    def validate(self):
        if any(not isinstance(getattr(self, group), bool) for group in GROUPS) or \
                self.confirm not in CONFIRM_POLICIES:
            raise ValueError('Invalid computer action settings.')
        return self


def load_settings(config_dir):
    try:
        raw = json.loads((Path(config_dir) / SETTINGS_NAME).read_text(encoding="utf-8"))
        return ActionSettings(**raw).validate() if isinstance(raw, dict) else ActionSettings()
    except FileNotFoundError:
        return ActionSettings()
    except (OSError, UnicodeError, ValueError, TypeError):
        # A damaged file must not silently open everything: everything off.
        return ActionSettings(**{group: False for group in GROUPS}, confirm="always")


def save_settings(config_dir, settings):
    settings.validate()
    _write_json(Path(config_dir) / SETTINGS_NAME, asdict(settings))


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(str(temporary), str(path))


# ---- tool definitions -------------------------------------------------------------

def _tool(name, description, properties, required=()):
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "additionalProperties": False,
                             "properties": properties, "required": list(required)}}


SETTINGS_PAGES = {"sound": "ms-settings:sound", "display": "ms-settings:display",
                  "bluetooth": "ms-settings:bluetooth", "network": "ms-settings:network-status",
                  "wifi": "ms-settings:network-wifi", "personalization": "ms-settings:personalization",
                  "notifications": "ms-settings:notifications", "apps": "ms-settings:appsfeatures",
                  "power": "ms-settings:powersleep", "update": "ms-settings:windowsupdate",
                  "privacy": "ms-settings:privacy", "storage": "ms-settings:storagesense",
                  "mouse": "ms-settings:mousetouchpad", "keyboard": "ms-settings:keyboard",
                  "time": "ms-settings:dateandtime", "language": "ms-settings:regionlanguage"}

TOOLS = {
    "media": [
        _tool("music_control", "Play music from MYN's local Music folder, or pause/resume/skip/check it. "
              "Use this for user requests to listen to music, including natural spoken requests; "
              "do not use OS media keys to start MYN music. Play uses shuffled non-repeating cycles forever. "
              "Only report playback success after ok and playing=true. Empty folders and decode failures are errors.",
              {"action": {"type": "string", "enum": ["play", "pause", "resume", "next", "previous", "status"]}}, ["action"]),
        _tool("microphone_control", "Inspect MYN's configured microphone, actual active input and available "
              "microphones. Call inspect before switching. For switch, device must be the exact name "
              'from the list or System default microphone. Change only when the user requests it; never claim an inactive '
              "saved selection is actively recording. Does not change playback or Windows defaults.",
              {"action": {"type": "string", "enum": ["inspect", "switch"]},
               "device": {"type": "string"}}, ["action"]),
        _tool("set_volume", "Change the computer's system volume. Give level (0-100) to set it, "
              "or change (e.g. +10 / -20) to adjust it, and/or mute true/false. Returns the new volume.",
              {"level": {"type": "integer", "minimum": 0, "maximum": 100},
               "change": {"type": "integer", "minimum": -100, "maximum": 100},
               "mute": {"type": "boolean"}}),
        _tool("media_control", "Control whatever music/video is playing (media keys).",
              {"action": {"type": "string", "enum": ["play_pause", "next", "previous", "stop"]}},
              ["action"]),
    ],
    "system": [
        _tool("open_settings", "Open a Windows Settings page.",
              {"page": {"type": "string", "enum": sorted(SETTINGS_PAGES)}}, ["page"]),
        _tool("set_dark_mode", "Switch Windows (apps and system) to dark or light mode.",
              {"dark": {"type": "boolean"}}, ["dark"]),
        _tool("lock_screen", "Lock the computer (the user signs in again).", {}),
        _tool("power", "Put the computer to sleep, restart it, or shut it down "
              "(restart/shut down wait 60 s and can be cancelled with action cancel).",
              {"action": {"type": "string", "enum": ["sleep", "restart", "shutdown", "cancel"]}},
              ["action"]),
    ],
    "open": [
        _tool("open_url", "Open a web page (http/https) in the default browser.",
              {"url": {"type": "string"}}, ["url"]),
        _tool("open_app", "Start a program installed on this computer, found by its Start-menu name "
              "(e.g. 'Steam', 'Notepad', 'Blender').", {"name": {"type": "string"}}, ["name"]),
        _tool("open_path", "Open a file or folder that lies inside a folder the user granted "
              "(read or write) through computer_permissions.", {"path": {"type": "string"}}, ["path"]),
    ],
    "reminders": [
        _tool("set_reminder", "A one-off reminder: MYN will say and show the text when due. "
              "For alarms/waking the user use add_alarm instead. Give "
              "in_minutes, or at as local Asia/Tokyo time 'YYYY-MM-DD HH:MM'. For something that "
              "repeats, or for MYN to do or look up something at a time, use add_schedule; for a "
              "countdown the user wants to watch ('time five minutes'), use start_timer.",
              {"text": {"type": "string"}, "in_minutes": {"type": "number", "minimum": 0.1, "maximum": 525600},
               "at": {"type": "string"}}, ["text"]),
        _tool("list_reminders", "Read pending reminders AND recent triggered/cancelled reminders, "
              "delivery diagnostics and creation audit. An empty pending list does NOT mean a reminder "
              "was never created. Triggered does NOT prove sound was heard.", {}),
        _tool("cancel_reminder", "Cancel a reminder by its id (from list_reminders).",
              {"id": {"type": "string"}}, ["id"]),
        _tool("start_timer", "Start a countdown timer ('time five minutes', 'remind me about noodles in three minutes', '25-minute focus timer'): "
              "MYN says when the time is up. Give minutes and/or seconds (up to 24 h) and a short label "
              "if the user named it. Several timers can run at once.",
              {"minutes": {"type": "number", "minimum": 0, "maximum": 1440},
               "seconds": {"type": "number", "minimum": 0, "maximum": 86400},
               "label": {"type": "string"}}),
        _tool("list_timers", "Running and paused timers with remaining time ('how much time is left').", {}),
        _tool("timer_control", "Pause, resume, cancel a timer, or add time to it (add: minutes/seconds, "
              "negative takes time off). id from list_timers; may be left out when only one timer exists.",
              {"action": {"type": "string", "enum": ["pause", "resume", "cancel", "add"]},
               "id": {"type": "string"}, "minutes": {"type": "number"}, "seconds": {"type": "number"}},
              ["action"]),
        _tool("add_schedule", "A scheduled task MYN does by itself at a time, once or repeating. "
              "For alarms/waking the user use add_alarm instead. kind: "
              "say = MYN says text ('remind me to sleep daily at 22:30'); ask = at that time MYN answers text as a "
              "request and reads the answer ('tell me the weather and today's events daily at 08:00' - a Claude request each "
              "run, costs a little; actions needing the user's yes are not done then); action = one "
              "fixed computer action: action_name (set_volume, media_control, open_settings, "
              "set_dark_mode, lock_screen, power, open_url, open_app, open_path, focus_window, "
              "window_state, press_keys) with action_arguments exactly as that tool takes them "
              "('set volume to 20 daily at 23:00' -> set_volume {level: 20}; 'shut down at 01:00' -> power {action: "
              "shutdown}). Actions that normally ask the user are asked now, when the task is made. "
              "repeat: once (date + time; date defaults to the next such time), daily, weekdays, "
              "weekends, weekly (days: 0=Mon … 6=Sun), monthly (day_of_month), interval "
              "(every_minutes 5-1440, optionally only between from_time and to_time, e.g. 'remind me hourly "
              "to drink water from 09:00 to 21:00'). Times are local Asia/Tokyo 'HH:MM', dates 'YYYY-MM-DD'; "
              "work them out from the current time in the context. Give a short name.",
              {"name": {"type": "string"}, "kind": {"type": "string", "enum": ["say", "ask", "action"]},
               "text": {"type": "string"}, "action_name": {"type": "string"},
               "action_arguments": {"type": "object"},
               "repeat": {"type": "string", "enum": ["once", "daily", "weekdays", "weekends", "weekly",
                                                     "monthly", "interval"]},
               "time": {"type": "string"}, "date": {"type": "string"},
               "days": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 6}},
               "day_of_month": {"type": "integer", "minimum": 1, "maximum": 31},
               "every_minutes": {"type": "integer", "minimum": 5, "maximum": 1440},
               "from_time": {"type": "string"}, "to_time": {"type": "string"}},
              ["kind", "repeat"]),
        _tool("list_schedules", "The user's scheduled tasks: when, what, next run, last result, paused or "
              "not.", {}),
        _tool("update_schedule", "Change a scheduled task (id from list_schedules): only the fields given "
              "change. enabled false pauses it, true resumes it. The kind and the action itself cannot "
              "be changed (delete and add again).",
              {"id": {"type": "string"}, "enabled": {"type": "boolean"}, "name": {"type": "string"},
               "text": {"type": "string"},
               "repeat": {"type": "string", "enum": ["once", "daily", "weekdays", "weekends", "weekly",
                                                     "monthly", "interval"]},
               "time": {"type": "string"}, "date": {"type": "string"},
               "days": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 6}},
               "day_of_month": {"type": "integer", "minimum": 1, "maximum": 31},
               "every_minutes": {"type": "integer", "minimum": 5, "maximum": 1440},
               "from_time": {"type": "string"}, "to_time": {"type": "string"}}, ["id"]),
        _tool("delete_schedule", "Delete a scheduled task (id from list_schedules). The user is asked "
              "first.", {"id": {"type": "string"}}, ["id"]),
        _tool("run_schedule_now", 'Run a scheduled task once now, for example to try the morning report.',
              {"id": {"type": "string"}}, ["id"]),
    ],
    "agent": [
        _tool("task_settings", "Read or change all settings formerly in the background-task/evolution pages. "
              "No settings page exists. Interpret natural speech; only change fields explicitly requested by "
              "the user, never increase a limit yourself. Background and evolution share monthly_usd (default $50). "
              "Background jobs are level-one evolution points. All levels contribute to the monthly spend, but "
              "only levels two/three stop at the monthly cap; level one keeps working, still subject to its $15 task cap. "
              "An evolution point's cap covers all planning/build/retry costs together. Model and step limit also "
              "apply to evolution workers; workspace is for background jobs (self-updates always use a copy). Omitted fields stay unchanged. "
              "Empty workspace restores MYN's default. enabled/self_evolution_enabled toggle the respective abilities.",
              {"model": {"type": "string", "enum": ["claude-opus-5-5"]},
               "max_turns": {"type": "integer", "minimum": 3, "maximum": 1000},
               "workspace": {"type": "string"}, "enabled": {"type": "boolean"},
               "self_evolution_enabled": {"type": "boolean"},
               "agent_task_usd": {"type": "number", "minimum": 0, "maximum": 1000},
               "evolution_task_usd": {"type": "number", "minimum": 0, "maximum": 1000},
               "monthly_usd": {"type": "number", "minimum": 0, "maximum": 100000},
               "mining": {"type": "boolean"}, "default_level": {"type": "integer", "enum": [2, 3]}}),
        _tool("agent_task_status", "Read the live or latest background task progress and log, without "
              "opening a settings page. Summarize it in the user's language.", {}),
        _tool("start_agent_task", "Hand a bigger job to MYN's background worker (the Claude Agent "
              "SDK: it reads, searches the web, writes files and runs commands step by step on this "
              "computer). Use for multi-step work: organising or renaming many files, writing or "
              "fixing code, building a document or spreadsheet, research that ends in a report. Not "
              "for quick questions or single actions (use the other tools). Write the task fully "
              "and specifically (what, where, what result) in the user's language. It runs in the "
              "background: tell the user it has started; MYN reports when it is done. It runs "
              "ordinary commands directly; file writes and risky commands ask the user first.",
              {"task": {"type": "string"},
               "folder": {"type": "string", "description": "Optional: the folder to work in "
                          "(must be a folder the user granted); default is MYN's work folder."}},
              ["task"]),
        _tool("stop_agent_task", "Stop the background task that is running now.", {}),
    ],
    "vision": [
        _tool("look_at_screen", "See what is on the user's screen right now (a picture comes back). "
              "Use it when the user refers to something visible ('what does this error mean', 'look at my screen', "
              "'where is this button'), not otherwise. target: window = their active window (default; "
              "seen even behind MYN's own window), screen = the whole monitor under the mouse, all = every "
              "monitor. Describe only what is relevant to their question.",
              {"target": {"type": "string", "enum": ["window", "screen", "all"]}}),
        _tool("look_at_image", "Look at an image file (jpg/png/gif/webp/bmp) inside a folder the user "
              "granted for reading or writing.", {"path": {"type": "string"}}, ["path"]),
        _tool("look_at_clipboard", "Look at the image the user just copied (e.g. a screenshot taken "
              "with Win+Shift+S).", {}),
    ],
    "windows": [
        _tool("list_windows", "List the windows open on the computer (title, program, state), "
              "front-most first. MYN's own windows are not included.", {}),
        _tool("focus_window", "Bring a window to the front (e.g. 'switch to WeChat', 'open that Chrome window'). "
              "title: part of its title or program name, as list_windows shows.",
              {"title": {"type": "string"}}, ["title"]),
        _tool("window_state", "Minimise, maximise, restore or close a window (close asks the user "
              "first; the program may still ask to save).",
              {"title": {"type": "string"},
               "action": {"type": "string", "enum": ["minimize", "maximize", "restore", "close"]}},
              ["title", "action"]),
        _tool("press_keys", "Press a keyboard shortcut in a window: e.g. 'ctrl+s' to save, 'ctrl+c', "
              "'ctrl+v', 'ctrl+z', 'ctrl+t', 'f5', 'space', 'enter'. window: optional title to bring "
              "to the front first (otherwise the window in front). Shortcuts that close, quit or "
              "delete ask the user first. Use repeat for e.g. pressing down 3 times.",
              {"keys": {"type": "string"}, "window": {"type": "string"},
               "repeat": {"type": "integer", "minimum": 1, "maximum": 20}}, ["keys"]),
        _tool("type_text", "Type text where the cursor is in a window (window: optional title to "
              "bring to the front first). The user is always asked first and sees the text. It does "
              "not press Enter unless the text ends with a new line — sending a message is the "
              "user's call.", {"text": {"type": "string"}, "window": {"type": "string"}}, ["text"]),
    ],
    "calendar": [
        _tool("add_event", "Put something in the user's calendar (MYN keeps it itself). start: local "
              "Asia/Tokyo time 'YYYY-MM-DD HH:MM', or 'YYYY-MM-DD' for all day; end: same format or "
              "just 'HH:MM'; remind_minutes: remind that many minutes before (MYN says it aloud; "
              "for all-day events it reminds at 9:00); repeat: none/daily/weekly/monthly/yearly. "
              "Work out the date from today's date in the context; if the time is unclear, ask.",
              {"title": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"},
               "location": {"type": "string"}, "note": {"type": "string"},
               "remind_minutes": {"type": "integer", "minimum": 0, "maximum": 10080},
               "repeat": {"type": "string", "enum": ["none", "daily", "weekly", "monthly", "yearly"]}},
              ["title", "start"]),
        _tool("list_events", "The user's calendar between two dates ('YYYY-MM-DD', end included); "
              "default today through six days later. Repeating events are expanded. Shows animated "
              "day cards on the main window. Resolve dates from conversation and Tokyo today: next week "
              "means next Monday through Sunday; a specific day must have identical from_date and to_date. "
              "Use display=false for internal lookups before editing/deleting, not a requested calendar view.",
              {"from_date": {"type": "string"}, "to_date": {"type": "string"},
               "display": {"type": "boolean"}}),
        _tool("update_event", "Change an event (id from list_events): only the fields given change. "
              "For a repeating event this changes every occurrence.",
              {"id": {"type": "string"}, "title": {"type": "string"}, "start": {"type": "string"},
               "end": {"type": "string"}, "location": {"type": "string"}, "note": {"type": "string"},
               "remind_minutes": {"type": "integer", "minimum": 0, "maximum": 10080},
               "repeat": {"type": "string", "enum": ["none", "daily", "weekly", "monthly", "yearly"]}},
              ["id"]),
        _tool("delete_event", "Delete an event (id from list_events; a repeating one is deleted with "
              "all its occurrences). The user is asked first.", {"id": {"type": "string"}}, ["id"]),
    ],
    "self_fix": [
        _tool("fix_myself", "Immediate evolution: change MYN's own program because the user explicitly tells you "
              "to change or fix it now (e.g. wrong subtitles, a feature misbehaving). It becomes a normal "
              'evolution point adopted immediately: MYN works on a copy, runs its tests, then automatically '
              "backs up, updates and restarts when idle; a broken new version is rolled back. Describe the problem "
              "fully and concretely in the user's language. Tell the user it has started. For something MYN "
              "merely cannot do, or a wish/complaint without 'do it now', use note_evolution_point instead. "
              "All levels count toward the monthly dollar budget. Set level 1 when the user explicitly "
              'designates level one; level 1 has priority and bypasses the monthly cap, but keeps its task cap. Do not promise work '
              "has started unless the tool result says started=true.",
              {"problem": {"type": "string"}, "level": {"type": "integer", "enum": [1, 2, 3]}}, ["problem"]),
        _tool("note_evolution_point", 'Record an evolution point (something for MYN to learn): when the user asks for '
              "something none of your tools can do (kind missing, e.g. a feature MYN lacks), or clearly wants "
              "MYN to improve (improvement) or is unhappy with how MYN behaves (complaint). MYN will check "
              "whether it can already do it, work out a plan, and propose it to the user later; nothing is "
              "changed without the user. First check your tools (myn_status, the action tools): only record "
              "what really is missing. Tell the user briefly that you noted it. level: 1 only when the user "
              'explicitly designates level one (highest priority, counts toward spending but bypasses the monthly cap), 2 soon, '
              "3 (default) no hurry.",
              {"title": {"type": "string", "description": 'Short English title, <=20 characters, e.g. Set alarms'},
               "request": {"type": "string", "description": "the user's own words"},
               "why": {"type": "string"},
               "kind": {"type": "string", "enum": ["missing", "improvement", "complaint"]},
               "deadline": {"type": "string", "description": "User's deadline in Tokyo YYYY-MM-DD HH:MM; omit if unclear."},
               "level": {"type": "integer", "enum": [1, 2, 3]}}, ["title", "request"]),
        _tool("evolution_points", "MYN's evolution point list. list: points with their status, level, plan "
              "and this month's quota. show: retrieve one proposal to present to the user for review; "
              "include its title in the answer. The user's word on one (id from list or the context's "
              'evolution_points_waiting_for_you): adopt (accept a proposed plan, authorizing automatic backup, update '
              'and restart after tests pass), adjust (describe changes in '
              'note; MYN creates a new plan), reject (decline), level (set level 1/2/3: level 1 = do it now and '
              "counts toward monthly spend but is not blocked by its monthly cap), complete (already resolved elsewhere, removes it from active work), "
              "retry (a failed one), deadline (Tokyo YYYY-MM-DD HH:MM in note, empty clears), "
              "settings (read or change shared monthly_usd budget, per-point task_usd, mining or default_level; no id needed), "
              "update (put a built and tested point into MYN with explicit confirmation). "
              "There is no evolution settings page. Manage everything conversationally, interpreting the user's "
              "natural words in context rather than requiring fixed commands. At most one proactive proposal per "
              "Tokyo day; explicit user requests to review more are allowed. Shared monthly budget defaults to $50, slows at 90%, "
              "starts no new level-two/three work at 100%, permits about 10% overrun for their work in progress; "
              "level one keeps working after monthly exhaustion but its task cap still applies. All levels count. "
              "Only change settings when the user requests it, never to unblock a task on your own.",
              {"action": {"type": "string", "enum": ["list", "show", "adopt", "adjust", "reject", "complete", "level", "deadline", "retry", "update", "settings"]},
               "id": {"type": "string"}, "note": {"type": "string"},
               "view": {"type": "string", "enum": ["open", "completed", "rejected", "all"]},
               "offset": {"type": "integer", "minimum": 0},
               "monthly_usd": {"type": "number", "minimum": 0, "maximum": 100000},
               "task_usd": {"type": "number", "minimum": 0, "maximum": 1000},
               "mining": {"type": "boolean"},
               "default_level": {"type": "integer", "enum": [2, 3]},
               "level": {"type": "integer", "enum": [1, 2, 3]}}, ["action"]),
    ],
    "files": [
        _tool("write_file", "Create or change a text file inside a folder the user granted for "
              "writing. mode: create (fails if it exists), overwrite, or append.",
              {"path": {"type": "string"}, "content": {"type": "string"},
               "mode": {"type": "string", "enum": ["create", "overwrite", "append"]}},
              ["path", "content"]),
        _tool("make_folder", "Create a folder inside a folder the user granted for writing.",
              {"path": {"type": "string"}}, ["path"]),
    ],
}
# Dedicated alarm vocabulary; reuse only the scheduler's date/repetition schema.
_schedule_schema = next(t for t in TOOLS["reminders"] if t["name"] == "add_schedule")["input_schema"]["properties"]
_alarm_schema = {k: v for k, v in _schedule_schema.items()
                 if k not in ("kind", "action_name", "action_arguments")}
_alarm_schema['ringtone_id'] = {'type': 'string', 'description':
    'Optional Music library id from list_alarm_music. If omitted on creation use the saved default; '
    'updates preserve the current sound. Empty string explicitly selects spoken alarm. Never invent ids.'}
TOOLS["reminders"].extend([
    _tool('alarm_sound_settings', 'Read or change the default alarm sound and independent music alarm '
          'volume. Omit fields to inspect. ringtone_id comes from list_alarm_music; empty selects voice. '
          'This never creates an alarm or changes existing alarms. Volume applies to all music alarms.',
          {'ringtone_id': {'type': 'string'}, 'volume': {'type': 'number', 'minimum': 0, 'maximum': 1}}),
    _tool('list_alarm_music', 'List local Music songs available as alarm sounds. Use the returned id '
          'as ringtone_id in add_alarm/update_alarm. Ask which song when ambiguous.', {}),
    _tool("add_alarm", "Set a separate spoken or music alarm (including waking the user). Use for alarms, "
          "not start_timer/set_reminder/add_schedule. Understand intent and dates from conversation; "
          "ask if ambiguous. Asia/Tokyo time HH:MM, date YYYY-MM-DD; repeat defaults once. "
          "text is the spoken message or music-failure fallback. Optional ringtone_id selects a song "
          "from list_alarm_music; music loops until the user dismisses the alarm. "
          "Survives restart and sounds in quiet/sleep scenes.",
          _alarm_schema, ["time", "text"]),
    _tool("list_alarms", "List only alarms, including legacy wake-up reminders, paused and finished "
          "alarms. Use the returned id unchanged for update_alarm/delete_alarm.", {}),
    _tool("update_alarm", "Change/pause/resume an alarm; only supplied fields change. enabled=false "
          "pauses, true resumes. Old reminder-backed alarms support once only; do not silently duplicate.",
          dict(_alarm_schema, id={"type": "string"}, enabled={"type": "boolean"}), ["id"]),
    _tool("skip_alarm", "Skip ONE dated occurrence without changing the recurring alarm. "
          "Use for tomorrow/this morning/this time do not wake me. Inspect list_alarms first. "
          "date is required; time defaults to the alarm time. Do not disable/delete the series.",
          {"id": {"type": "string"}, "date": {"type": "string"},
           "time": {"type": "string"}}, ["id", "date"]),
    _tool("delete_alarm", "Permanently delete the alarm and all future occurrences. "
          "ONLY for explicit ongoing intent (from now on/never again/delete this recurring alarm). "
          "For one date use skip_alarm. If scope truly unclear ask once; never guess permanence.",
          {"id": {"type": "string"}, "permanent": {"type": "boolean",
           "description": "Must be true, only after user clearly requests permanent removal."}},
          ["id", "permanent"]),
])
GROUP_OF = {tool["name"]: group for group, tools in TOOLS.items() for tool in tools}
# The ASK level: starts something, changes files, or turns the computer off.
RISKY = {"open_app", "open_path", "write_file", "make_folder", "power", "delete_event", "delete_schedule"}
def needs_asking(name, arguments):
    """The ASK level, except that cancelling a shutdown is always fine. Windows: closing a
    window, shortcuts that close / quit / delete, and typing text are asked."""
    if name == "window_state":
        return arguments.get("action") == "close"
    if name == "press_keys":
        import windows_control
        try:
            return windows_control.is_risky_keys(windows_control.parse_keys(arguments.get("keys"))[0])
        except windows_control.WindowError:
            return False  # refused anyway when it runs
    if name == "type_text":
        return True
    if name == "evolution_points":  # putting new code into MYN: always the user's yes
        return arguments.get("action") == "update"
    return name in RISKY and not (name == "power" and arguments.get("action") == "cancel")


# For showing the levels (window_state / press_keys are direct except closing etc., see LABELS).
LEVELS = {name: ("ask" if name in RISKY or name == "type_text" else "direct") for name in GROUP_OF}
LABELS = {"task_settings": 'Manage tasks and budgets by voice', "agent_task_status": 'View task progress and history',
          "music_control": 'Play / control MYN music', "microphone_control": 'View / change MYN microphone',
          "add_event": 'Create calendar events', "list_events": 'View calendar events', "update_event": 'Edit calendar events', "delete_event": 'Delete calendar events',
          "add_alarm": 'Set alarms', "list_alarms": 'View alarms', "list_alarm_music": 'View music alarm sounds',
          "alarm_sound_settings": 'Default alarm sound and volume',
          "update_alarm": 'Edit / pause alarms', "delete_alarm": 'Delete alarms',
          "skip_alarm": 'Skip one alarm occurrence',
          "list_windows": 'List open windows', "focus_window": 'Switch to a window',
          "window_state": 'Minimize / maximize / restore / close a window (confirm before closing)',
          "press_keys": 'Press shortcuts (confirm close, exit and delete actions)', "type_text": 'Type text',
          "look_at_screen": 'View a screen or window', "look_at_image": 'View an image file',
          "look_at_clipboard": 'View clipboard image', "set_volume": 'Adjust volume / mute', "media_control": 'Play, pause, skip tracks',
          "open_settings": 'Open system settings', "set_dark_mode": 'Dark / light mode',
          "lock_screen": 'Lock screen', "power": 'Sleep, restart, shut down', "open_url": 'Open websites',
          "open_app": 'Launch programs', "open_path": 'Open files or folders', "set_reminder": 'Set reminders',
          "list_reminders": 'View reminders', "cancel_reminder": 'Cancel reminders',
          "start_timer": 'Start timers', "list_timers": 'View timers', "timer_control": 'Pause / resume / cancel timers',
          "add_schedule": 'Create scheduled tasks (confirm sensitive actions at creation)', "list_schedules": 'View scheduled tasks',
          "update_schedule": 'Edit / pause scheduled tasks', "delete_schedule": 'Delete scheduled tasks',
          "run_schedule_now": 'Run scheduled tasks now',
          "write_file": 'Write file', "make_folder": 'Create folders',
          "start_agent_task": 'Start background tasks (confirm file writes and risky commands)',
          "fix_myself": 'Evolution (queued by priority and budget; adopted plans update automatically after tests)', "stop_agent_task": 'Stop background tasks',
          "note_evolution_point": 'Record evolution points', "evolution_points": 'View / decide evolution points (adoption authorizes updates after testing)'}


def tools_for(settings):
    import permission_control
    # Discoverable does not mean permitted: execute rechecks persisted grants on every call.
    return [dict(tool, description=tool["description"] + (
            " Permission disabled. Ask via computer_permissions before using this tool."
            if not getattr(settings, group) and tool["name"] != "task_settings" else ""))
            for group, items in TOOLS.items() for tool in items] + permission_control.tools()


def note_for(settings, write_roots):
    management = (" Computer and web settings pages have been removed. Use computer_permissions to inspect "
                  "and request a specific missing group/folder grant, or revoke grants/change confirmation policy "
                  "on the user's request. A real confirmation is mandatory for any permission change, even under "
                  "the never policy. After approval retry the requested action; failed/declined means no permission. "
                  "Use web_settings for web settings; monthly searches are unlimited by default. "
                  " Background task and evolution settings are managed by task_settings through natural "
                  "conversation, including model, workspace, steps, both task caps, shared monthly budget, "
                  "enable switches, history mining and default level. Read before changing; preserve omitted "
                  "settings. Never tell the user to open removed pages. Only change on their current request. ")
    allowed = [GROUPS[group] for group in GROUPS if getattr(settings, group)]
    if not allowed:
        return management + "No computer action groups are currently allowed. Request the needed permission first."
    text = ("The user has allowed you to act on their Windows computer with the supplied action "
            "tools: " + "、".join(allowed) + ". Use them when asked (e.g. 'turn the volume up' -> "
            "set_volume change +10). Only say something was done after the tool returned ok; "
            "if it was declined or failed, say so plainly. Deleting files and running commands "
            "are not available.")
    if settings.media:
        text += (' Use music_control for requests to play MYN music or listen to songs. '
                 "play starts local music in shuffled non-repeating cycles; pause/resume/next/previous control it. "
                 "Never claim playback succeeded without its verified result.")
        text += (" For questions about your microphone, call microphone_control action inspect to get "
                 "configured AND actually active devices. To change it, inspect first, resolve the user's "
                 "choice to an exact listed name (ask if ambiguous), then call switch. Only confirm "
                 "active switching after applied=true and actual_device is verified; saved-only means "
                 "the microphone is currently off. Never invent a device or claim success after an error.")
    if settings.reminders:
        text += (" Alarms are separate from timers and scheduled tasks. Understand alarm/wake-up intent "
                 "from conversation, not fixed phrases: use add_alarm/list_alarms/update_alarm/delete_alarm. "
                 "Resolve cancellation scope semantically using the current request and recent context. "
                 "A dated or bounded request such as 'tomorrow morning don't wake me', 'this Friday I can sleep in', "
                 "or a contextual 'not this time' means skip_alarm for that occurrence, never enabled=false "
                 "or delete_alarm. For an explicit finite range skip each matching occurrence in that range. "
                 "Inspect list_alarms to resolve dates/times/ids; preserve recurrence and other alarms. "
                 "Only explicit ongoing intent ('stop waking me at eight from now on', 'delete this recurring alarm') permits "
                 "delete_alarm with permanent=true. update_alarm enabled=false is a whole-series pause, "
                 "only when that is what the user wants. 'I'm awake/stop ringing' stops current ringing, "
                 "not future alarms. Do not ask for fixed commands or confirmation when scope is clear. "
                 "If scope or which alarm is genuinely ambiguous, ask one concise question before changing it. "
                 "Report the actual skipped occurrence and next run from the receipt, not an invented success. "
                 "Use start_timer for elapsed-duration countdowns and add_schedule for scheduled work. "
                 "Alarms display their clock time separately, never a timer countdown. "
                 "Timers and upcoming reminders appear automatically as a read-only animation on MYN's "
                 "main window. There is no timer settings page/menu. Create, inspect, pause, resume, "
                 "change or cancel them with the supplied tools, not by telling the user to open settings.")
    if settings.calendar:
        text += (" There is no calendar settings page/menu. When the user asks about their arrangements, "
                 "always call list_events for the requested dates even if calendar_next_48h is in context, "
                 "so real saved events appear as animated day cards. Understand dates and follow-up "
                 "questions from the conversation, not fixed phrases. A week runs Monday through Sunday; "
                 "for one specific day use that same date for both bounds. Briefly summarize the result. "
                 "The user authorizes extracting their definite personal plans from addressed speech or text, "
                 "even without 'add to calendar'. Use add_event for a genuinely committed plan; inspect saved "
                 "events with display=false before adding to avoid duplicates. Never record hypothetical, "
                 "quoted, another person's, cancelled or merely tentative plans as commitments. If the date "
                 "or start time is unclear (e.g. 'next week see the dentist', 'tomorrow afternoon'), ask a "
                 "short follow-up BEFORE saving; do not invent a time or silently turn it into an all-day event. "
                 "Use date-only start only for explicit all-day/date-only plans (e.g. a birthday or 'just note "
                 "the day, no time needed'). Interpret follow-up answers with the prior conversation, preserve "
                 "the original event details, and save only once after ambiguity is resolved. Confirm the actual "
                 "saved date/time, and never say it was saved before a successful tool result.")
    if settings.files:
        text += (" Folders granted for writing: " + ("; ".join(write_roots) if write_roots else
                 "none yet (request a specific directory through computer_permissions)") + ".")
    if settings.self_fix:
        text += (' Evolution: when the user requests something none of your tools can do, explain briefly, '
                 'record an evolution point with note_evolution_point, and say MYN will develop a plan '
                 "and propose it; also record clear wishes for MYN to improve and complaints about MYN. When the "
                 'the user responds to a proposed evolution point (adopt / adjust / decline / level one / update), call '
                 "evolution_points for that point (see active_evolution_review for the exact proposal shown; "
                 "evolution_points_waiting_for_you lists other pending points). When presenting a proposal "
                 "for a decision, call evolution_points with action show and include its title. "
                 "Use its settings action to read or change the monthly dollar budget (only on the user's request), "
                 "complete for tasks solved elsewhere, and list with view completed/rejected for history. "
                 "If the user says they are ready after deferring, interaction_context.invitation identifies "
                 "the previously offered point; use show for that point, not another unsolicited proposal. "
                 "There is no evolution page to open. Do not ask the user to click buttons or repeat a fixed command.")
    return text + management


# ---- the platform (Windows); replaced in tests ---------------------------------------

_VOLUME_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @"
using System.Runtime.InteropServices;
[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioEndpointVolume {
  int f(); int g(); int h(); int i();
  int SetMasterVolumeLevelScalar(float fLevel, System.Guid pguidEventContext);
  int j();
  int GetMasterVolumeLevelScalar(out float pfLevel);
  int k(); int l(); int m(); int n();
  int SetMute([MarshalAs(UnmanagedType.Bool)] bool bMute, System.Guid pguidEventContext);
  int GetMute(out bool pbMute);
}
[Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice { int Activate(ref System.Guid id, int clsCtx, int activationParams, out IAudioEndpointVolume aev); }
[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator { int f(); int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice endpoint); }
[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class MMDeviceEnumeratorComObject { }
public class MynAudio {
  static IAudioEndpointVolume Vol() {
    var enumerator = new MMDeviceEnumeratorComObject() as IMMDeviceEnumerator;
    IMMDevice dev = null;
    Marshal.ThrowExceptionForHR(enumerator.GetDefaultAudioEndpoint(0, 1, out dev));
    IAudioEndpointVolume epv = null;
    var epvid = typeof(IAudioEndpointVolume).GUID;
    Marshal.ThrowExceptionForHR(dev.Activate(ref epvid, 23, 0, out epv));
    return epv;
  }
  public static float Get() { float v = -1; Marshal.ThrowExceptionForHR(Vol().GetMasterVolumeLevelScalar(out v)); return v; }
  public static void Set(float v) { Marshal.ThrowExceptionForHR(Vol().SetMasterVolumeLevelScalar(v, System.Guid.Empty)); }
  public static bool Muted() { bool m; Marshal.ThrowExceptionForHR(Vol().GetMute(out m)); return m; }
  public static void Mute(bool m) { Marshal.ThrowExceptionForHR(Vol().SetMute(m, System.Guid.Empty)); }
}
"@
'''


class Platform:
    """Windows: Core Audio through PowerShell, media keys through user32, os.startfile."""

    def _powershell(self, body):
        script = _VOLUME_SCRIPT + body
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-EncodedCommand", encoded], capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode != 0:
            raise OSError('PowerShell volume control failed.')
        return json.loads(result.stdout.strip().splitlines()[-1])

    def volume(self, level=None, mute=None):
        """Set level (0-100) and/or mute; returns {"level": int, "muted": bool}."""
        body = ""
        if level is not None:
            body += "[MynAudio]::Set(%0.3f)\n" % (max(0, min(100, level)) / 100.0)
        if mute is not None:
            body += "[MynAudio]::Mute($%s)\n" % ("true" if mute else "false")
        body += ("Write-Output ('{\"level\":' + [int][Math]::Round([MynAudio]::Get()*100) + "
                 "',\"muted\":' + ([MynAudio]::Muted()).ToString().ToLower() + '}')\n")
        return self._powershell(body)

    def volume_keys(self, level=None, change=None, mute=None):
        """Fallback without Core Audio: the keyboard's volume keys (2% a step)."""
        import ctypes
        user32 = ctypes.windll.user32

        def press(key, times=1):
            for _ in range(max(0, times)):
                user32.keybd_event(key, 0, 1, 0)
                user32.keybd_event(key, 0, 1 | 2, 0)
        if level is not None:
            press(0xAE, 50)              # all the way down
            press(0xAF, round(level / 2))
        elif change:
            press(0xAF if change > 0 else 0xAE, round(abs(change) / 2))
        if mute is not None:
            press(0xAD)                  # mute toggles; the state can't be read here

    def media_key(self, action):
        import ctypes
        key = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2}[action]
        user32 = ctypes.windll.user32
        user32.keybd_event(key, 0, 1, 0)      # KEYEVENTF_EXTENDEDKEY, down
        user32.keybd_event(key, 0, 1 | 2, 0)  # up

    def open(self, target):
        os.startfile(target)  # noqa: S606 - the target was checked by the caller

    def dark_mode(self, dark):
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
                             0, winreg.KEY_SET_VALUE)
        with key:
            for name in ("AppsUseLightTheme", "SystemUsesLightTheme"):
                winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, 0 if dark else 1)

    def lock(self):
        import ctypes
        if not ctypes.windll.user32.LockWorkStation():
            raise OSError('Failed to lock the screen.')

    def power(self, action):
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if action == "sleep":
            import ctypes
            # SetSuspendState(hibernate=False, force=False, wakeupEventsDisabled=False)
            if not ctypes.windll.powrprof.SetSuspendState(False, False, False):
                raise OSError('Failed to enter sleep.')
            return
        argv = {"restart": ["shutdown", "/r", "/t", "60"], "shutdown": ["shutdown", "/s", "/t", "60"],
                "cancel": ["shutdown", "/a"]}[action]
        result = subprocess.run(argv, capture_output=True, timeout=20, creationflags=flags)
        if result.returncode != 0:
            raise OSError('Power action failed.')

    def start_menu(self):
        """(name, path) of Start-menu shortcuts for this user and all users."""
        roots = [Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
                 Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "Microsoft/Windows/Start Menu/Programs"]
        found = []
        for root in roots:
            if root.is_dir():
                for path in root.rglob("*"):
                    if path.suffix.lower() in (".lnk", ".url", ".appref-ms"):
                        found.append((path.stem, str(path)))
        return found


# ---- running an action -----------------------------------------------------------------

class ActionContext:
    def __init__(self, config_dir, confirm=None, platform=None, now=None, start_agent=None,
                 start_self_fix=None, stop_agent=None, camera=None, skip_pids=(), desktop=None,
                 run_task=None, evolution_update=None, show_agenda=None, microphone_control=None, music_control=None,
                 agent_status=None):
        self.config_dir = Path(config_dir)
        self.microphone_control = microphone_control
        self.music_control = music_control
        self.agent_status = agent_status
        self.show_agenda = show_agenda
        self.evolution_update = evolution_update  # evolution_update(point_id) -> dict (the window)
        self.run_task = run_task  # run_task(task) runs a scheduled task now (the window)
        self._camera = camera
        self._desktop = desktop
        self.skip_pids = skip_pids  # MYN's own processes: their windows are not "the user's window"
        self.start_agent = start_agent  # start_agent(task, folder) -> dict, provided by the window
        self.start_self_fix = start_self_fix  # start_self_fix(problem) -> dict, provided by the window
        self.stop_agent = stop_agent  # stop_agent() asks the running background task to stop
        self.confirm = confirm          # confirm(preview) -> bool, asked on the GUI thread
        self.platform = platform or Platform()
        self.now = now or (lambda: datetime.now(timezone.utc))


    @property
    def desktop(self):
        if self._desktop is None:
            import windows_control
            self._desktop = windows_control.Desktop(self.skip_pids)
        return self._desktop

    @property
    def camera(self):
        if self._camera is None:
            import vision
            self._camera = vision.WindowsCamera([os.getpid(), *self.skip_pids])
        return self._camera


class ActionError(Exception):
    pass


def _ok(**result):
    return json.dumps(dict(ok=True, **result), ensure_ascii=False), False


def _error(message):
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False), True


def execute(name, arguments, context):
    """Run one action: (json result, is_error). Checks the group is allowed and asks first
    when the confirm policy says so."""
    if name in ("computer_permissions", "web_settings"):
        import permission_control
        try:
            result = permission_control.execute(name, arguments, context)
        except (ValueError, TypeError, OSError) as error:
            _audit(context, name, False, type(error).__name__)
            return _error(str(error))
        except Exception as error:
            _audit(context, name, False, type(error).__name__)
            return _error('Authorized action failed (%s).' % type(error).__name__)
        _audit(context, name, True, "done")
        return result
    settings = load_settings(context.config_dir)
    group = GROUP_OF.get(name)
    if group is None:
        return _error('Action not found.')
    if not getattr(settings, group) and name != "task_settings":
        _audit(context, name, False, "group_off")
        return _error('The user has not enabled "%s". Request authorization with computer_permissions first.' % GROUPS[group])
    if not isinstance(arguments, dict):
        return _error('Invalid arguments.')
    try:
        preview = _PREVIEWS[name](arguments, context)
        # An adopted evolution plan authorizes automatic application after testing; unrelated changes still require consent.
        evolving = name == "evolution_points" and arguments.get("action") == "update"
        if settings.confirm == "always" or evolving or (settings.confirm == "risky" and needs_asking(name, arguments)):
            if context.confirm is None or context.confirm(preview) is not True:
                _audit(context, name, False, "declined")
                return _error('The user did not consent: %s' % preview)
        result = _HANDLERS[name](arguments, context)
    except (ActionError, PermissionError, ValueError) as error:
        _audit(context, name, False, type(error).__name__)
        return _error(str(error) or 'Invalid action.')
    except Exception as error:  # platform failure: report, never crash the chat
        _audit(context, name, False, type(error).__name__)
        return _error('Action failed (%s).' % type(error).__name__)
    _audit(context, name, True, "done")
    return result


def _audit(context, name, ok, detail):
    record = {"at_utc": datetime.now(timezone.utc).isoformat(), "tool": name, "ok": ok, "detail": detail}
    try:
        with _LOCK:
            with (context.config_dir / local_tools.AUDIT_FILE).open("a", encoding="utf-8") as log:
                log.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


# media

def _set_volume(arguments, context):
    level, change, mute = arguments.get("level"), arguments.get("change"), arguments.get("mute")
    for value, low in ((level, 0), (change, -100)):
        if value is not None and (type(value) is not int or not low <= value <= 100):
            raise ValueError('Volume must be an integer from 0 to 100.')
    if mute is not None and not isinstance(mute, bool):
        raise ValueError('Invalid mute argument.')
    try:
        if level is None and change is None and mute is None:
            return _ok(**context.platform.volume())
        if change is not None and level is None:
            level = context.platform.volume()["level"] + change
    except (OSError, ValueError, subprocess.SubprocessError):
        context.platform.volume_keys(level, change, mute)  # approximate, but it still works
        return _ok(approximate=True, note='Adjusted with volume keys (current volume unavailable)')
    if level is not None:
        level = max(0, min(100, level))
        if mute is None and level > 0:
            mute = False  # turning it up should be heard
    try:
        return _ok(**context.platform.volume(level, mute))
    except (OSError, ValueError, subprocess.SubprocessError):
        context.platform.volume_keys(level, None, mute)
        return _ok(approximate=True, level=level)


def _media_control(arguments, context):
    action = arguments.get("action")
    if action not in ("play_pause", "next", "previous", "stop"):
        raise ValueError('Invalid media action.')
    context.platform.media_key(action)
    return _ok(action=action)


# system

def _open_settings(arguments, context):
    page = arguments.get("page")
    if page not in SETTINGS_PAGES:
        raise ValueError('Settings page not found.')
    context.platform.open(SETTINGS_PAGES[page])
    return _ok(opened=page)


def _set_dark_mode(arguments, context):
    if not isinstance(arguments.get("dark"), bool):
        raise ValueError('Invalid arguments.')
    context.platform.dark_mode(arguments["dark"])
    return _ok(dark=arguments["dark"])


def _lock_screen(arguments, context):
    context.platform.lock()
    return _ok(locked=True)


def _power(arguments, context):
    action = arguments.get("action")
    if action not in ("sleep", "restart", "shutdown", "cancel"):
        raise ValueError('Invalid power action.')
    context.platform.power(action)
    return _ok(action=action, note='Scheduled in 60 seconds. You can ask to cancel shutdown' if action in ("restart", "shutdown") else "")


# open

def _url(arguments):
    url = arguments.get("url")
    if not isinstance(url, str) or not re.match(r"^https?://[^\s\"'<>]{3,}$", url) or len(url) > 2000:
        raise ValueError('Only http and https URLs are allowed.')
    return url


def _open_url(arguments, context):
    url = _url(arguments)
    context.platform.open(url)
    return _ok(opened=url)


def _find_app(arguments, context):
    name = arguments.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        raise ValueError('Invalid program name.')
    wanted = name.strip().casefold()
    entries = context.platform.start_menu()
    exact = [entry for entry in entries if entry[0].casefold() == wanted]
    partial = sorted((entry for entry in entries if wanted in entry[0].casefold()),
                     key=lambda entry: len(entry[0]))
    if exact or partial:
        return (exact or partial)[0]
    raise ActionError('"%s" was not found in the Start menu.' % name.strip())


def _open_app(arguments, context):
    label, path = _find_app(arguments, context)
    context.platform.open(path)
    return _ok(opened=label)


def _granted_path(context, requested, roots, must_exist=True):
    if not isinstance(requested, str) or not requested.strip() or len(requested) > 1024:
        raise ValueError('Invalid path.')
    path = Path(requested.strip())
    if not path.is_absolute():
        raise PermissionError('Provide the full path.')
    resolved = path.resolve(strict=must_exist) if must_exist else path.parent.resolve() / path.name
    usable = []
    for item in roots:
        try:
            root = Path(item).resolve(strict=True)
        except OSError:
            continue
        if root.is_dir() and not local_tools.too_broad(root):
            usable.append(root)
    matching = [root for root in usable if resolved == root or root in resolved.parents]
    if not matching:
        raise PermissionError('This location is outside authorized folders. Request access to this specific folder in chat.')
    below = resolved.relative_to(max(matching, key=lambda root: len(root.parts))).parts
    private = context.config_dir.resolve()
    if resolved == private or private in resolved.parents or \
            (private.name == "config" and (resolved == private.parent or private.parent in resolved.parents)):
        raise PermissionError("MYN's own data cannot be accessed this way.")
    if any(local_tools._protected_name(part) for part in below):
        raise PermissionError('Sensitive files such as credentials and permissions cannot be accessed.')
    return resolved


def _open_path(arguments, context):
    granted = local_tools.grants(context.config_dir)
    path = _granted_path(context, arguments.get("path"), granted["read"] + granted["write"])
    if path.suffix.lower() in (".exe", ".bat", ".cmd", ".ps1", ".vbs", ".js", ".msi", ".com", ".scr", ".lnk"):
        raise PermissionError('Do not launch programs by opening executable files. Use the program name instead.')
    context.platform.open(str(path))
    return _ok(opened=str(path))


# reminders

def _reminder_records(config_dir):
    try:
        value = json.loads((Path(config_dir) / REMINDERS_NAME).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as error:
        raise ActionError('Cannot read reminders. The original file was preserved; check it before retrying.') from error
    if not isinstance(value, list) or any(not isinstance(item, dict) or
            not all(isinstance(item.get(key), str) for key in ("id", "text", "due_utc"))
            for item in value):
        raise ActionError('Reminder data is damaged; the original file was preserved.')
    return value


def reminders(config_dir):
    return [item for item in _reminder_records(config_dir) if item.get("status", "pending") == "pending"]


def _save_reminders(config_dir, items):
    _write_json(Path(config_dir) / REMINDERS_NAME, items)


def due_reminders(config_dir, now=None):
    """Claim due reminders once, retaining evidence even if audio subsequently fails."""
    now = now or datetime.now(timezone.utc)
    with _LOCK:
        items = _reminder_records(config_dir)
        due = [item for item in items if item.get("status", "pending") == "pending"
               and datetime.fromisoformat(item["due_utc"]) <= now]
        if due:
            for item in due:
                from alarm_kind import is_alarm
                if is_alarm(item):
                    import alarm_ringing
                    alarm_ringing.activate(config_dir, "reminder:" + item["id"],
                                           item["due_utc"], item["text"])
                item.update(status="triggered", triggered_utc=now.astimezone(timezone.utc).isoformat())
            _save_reminders(config_dir, items)
    return due


def _local(moment):
    return moment.astimezone(TOKYO).strftime("%Y-%m-%d %H:%M")


def _when(arguments, context):
    now = context.now()
    if arguments.get("in_minutes") is not None:
        minutes = arguments["in_minutes"]
        if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not 0.1 <= minutes <= 525600:
            raise ValueError('Invalid reminder time.')
        return now + timedelta(minutes=minutes)
    at = arguments.get("at")
    if not isinstance(at, str):
        raise ValueError('Specify a delay in minutes or an exact time.')
    try:
        moment = datetime.strptime(at.strip()[:16].replace("T", " "), "%Y-%m-%d %H:%M").replace(tzinfo=TOKYO)
    except ValueError:
        raise ValueError('Time must use YYYY-MM-DD HH:MM.') from None
    if moment <= now:
        raise ValueError('This time is in the past.')
    return moment


def _set_reminder(arguments, context):
    text = arguments.get("text")
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 300:
        raise ValueError('Reminder text must contain 1-300 characters.')
    due = _when(arguments, context)
    with _LOCK:
        items = _reminder_records(context.config_dir)
        if sum(item.get("status", "pending") == "pending" for item in items) >= MAX_REMINDERS:
            raise ActionError('Too many reminders (maximum %d).' % MAX_REMINDERS)
        item = {"id": uuid.uuid4().hex[:8], "text": text.strip(), "due_utc": due.astimezone(timezone.utc).isoformat(),
                "category": "reminder",
                "created_utc": context.now().astimezone(timezone.utc).isoformat()}
        _save_reminders(context.config_dir, items + [item])
        if item not in _reminder_records(context.config_dir):
            raise ActionError('Saved reminder could not be verified and may already exist. Check the list before creating another.')
    return _ok(id=item["id"], due_local=_local(due), text=item["text"], verified=True)


def _list_reminders(arguments, context):
    import alarm_delivery
    from alarm_kind import is_alarm
    items = sorted((r for r in reminders(context.config_dir) if not is_alarm(r)),
                   key=lambda item: item["due_utc"])
    return _ok(reminders=[{"id": item["id"], "due_local": _local(datetime.fromisoformat(item["due_utc"])),
                           "text": item["text"]} for item in items],
               history=[item for item in _reminder_records(context.config_dir)
                        if item.get("status", "pending") != "pending" and not is_alarm(item)][-20:],
               delivery=alarm_delivery.recent(context.config_dir),
               creation_audit=alarm_delivery.creation_audit(context.config_dir),
               note='Legacy versions removed reminders when due, so absence does not prove one was never set. triggered means dispatched, not audibly heard.')


def _cancel_reminder(arguments, context):
    from alarm_kind import is_alarm
    wanted = arguments.get("id")
    with _LOCK:
        items = _reminder_records(context.config_dir)
        found = [item for item in items if item["id"] == wanted and item.get("status", "pending") == "pending"]
        if not found:
            raise ActionError('Reminder not found.')
        if is_alarm(found[0]):
            raise ActionError('This is an alarm. Use the alarm list and deletion tools.')
        found[0].update(status="cancelled", cancelled_utc=context.now().astimezone(timezone.utc).isoformat())
        _save_reminders(context.config_dir, items)
    return _ok(cancelled=wanted)


# timers and scheduled tasks (timekeeper)

def _timekeeper_call(work):
    import timekeeper
    try:
        return work(timekeeper)
    except timekeeper.TimerError as error:
        raise ActionError(str(error)) from None


def _start_timer(arguments, context):
    item = _timekeeper_call(lambda tk: tk.start_timer(
        context.config_dir, arguments.get("minutes"), arguments.get("seconds"), arguments.get("label"),
        now=context.now().timestamp()))
    import timekeeper
    if not any(saved["id"] == item["id"] for saved in timekeeper.timers(context.config_dir)):
        raise ActionError('Saved timer could not be verified. Check the list before creating another.')
    return _ok(timer=timekeeper.timer_for_model(item),
               verified=True, said='Start timer %s' % timekeeper.say_duration(item["seconds"]))


def _list_timers(arguments, context):
    import timekeeper
    return _ok(timers=[timekeeper.timer_for_model(item)
                       for item in timekeeper.timers(context.config_dir, context.now().timestamp())])


def _timer_control(arguments, context):
    item = _timekeeper_call(lambda tk: tk.control_timer(
        context.config_dir, arguments.get("action"), arguments.get("id"), arguments.get("minutes"),
        arguments.get("seconds"), now=context.now().timestamp()))
    import timekeeper
    return _ok(action=arguments.get("action"), timer=timekeeper.timer_for_model(item))


def _local_now(context):
    return context.now().astimezone(TOKYO).replace(tzinfo=None, second=0, microsecond=0)


_SCHEDULE_FIELDS = ("name", "kind", "text", "repeat", "time", "date", "days", "day_of_month",
                    "every_minutes", "from_time", "to_time")


def _schedule_action(arguments, context):
    """The fixed action of a kind=action task: checked, described, and whether it asks first."""
    import timekeeper
    name = arguments.get("action_name")
    action_arguments = arguments.get("action_arguments") or {}
    if name not in timekeeper.ACTIONS or not isinstance(action_arguments, dict):
        raise ActionError('Actions available to scheduled tasks: ' + "、".join(timekeeper.ACTIONS) + "。")
    settings = load_settings(context.config_dir)
    if not getattr(settings, GROUP_OF[name]):
        raise ActionError('The user has not enabled "%s". Request authorization with computer_permissions.' % GROUPS[GROUP_OF[name]])
    preview = _PREVIEWS[name](action_arguments, context)  # also checks the arguments
    asks = settings.confirm == "always" or (settings.confirm == "risky" and needs_asking(name, action_arguments))
    return {"name": name, "arguments": action_arguments}, preview, asks


def _preview_schedule(arguments, context):
    import timekeeper
    fields = {key: arguments[key] for key in _SCHEDULE_FIELDS if key in arguments}
    try:
        if arguments.get("kind") == "action":
            fields["action"], preview, _asks = _schedule_action(arguments, context)
            task = timekeeper._clean(fields, now=_local_now(context))
            task["action_preview"] = preview
        else:
            task = timekeeper._clean(fields, now=_local_now(context))
    except (timekeeper.TimerError, ActionError):
        return 'Create scheduled task: %s' % (arguments.get("name") or arguments.get("text") or "")
    return 'Create scheduled task: %s - %s' % (timekeeper.describe_when(task), timekeeper.describe_what(task))


def _add_schedule(arguments, context, _alarm=False):
    import timekeeper
    fields = {key: arguments[key] for key in _SCHEDULE_FIELDS if key in arguments}
    fields["category"] = "alarm" if _alarm else "schedule"
    if _alarm and 'ringtone_id' in arguments:
        fields['ringtone_id'] = arguments['ringtone_id']
    preview = None
    if arguments.get("kind") == "action":
        fields["action"], preview, asks = _schedule_action(arguments, context)
        settings = load_settings(context.config_dir)
        if asks and settings.confirm == "risky":  # ("always" was already asked by execute)
            try:
                plan = timekeeper._clean(fields, now=_local_now(context))
            except timekeeper.TimerError as error:
                raise ActionError(str(error)) from None
            question = ('Scheduled task: %s - %s\n\nIt will run without asking again when due. You can delete the task at any time.'
                        % (timekeeper.describe_when(plan), preview))
            if context.confirm is None or context.confirm(question) is not True:
                raise ActionError('The user did not consent to this scheduled task: %s' % preview)
    if preview and not str(fields.get("name") or "").strip():
        fields["name"] = preview[:30]  # Use a human-readable action description rather than an internal tool name.
    task = _timekeeper_call(lambda tk: tk.add_task(context.config_dir, now=_local_now(context), **fields))
    if preview:
        _set_task_preview(context.config_dir, task["id"], preview)
        task["action_preview"] = preview
    if timekeeper.get_task(context.config_dir, task["id"]) != task:
        raise ActionError('Saved scheduled task could not be verified and may already exist. Check the list before creating another.')
    return _ok(schedule=timekeeper.task_for_model(task), verified=True)


def _set_task_preview(config_dir, task_id, preview):
    import timekeeper
    with timekeeper._LOCK:
        path = Path(config_dir) / timekeeper.TASKS
        items = timekeeper._read(path)
        for item in items:
            if item["id"] == task_id:
                item["action_preview"] = preview
        timekeeper._write(path, items)


def _list_schedules(arguments, context):
    import timekeeper
    from alarm_kind import is_alarm
    return _ok(schedules=[timekeeper.task_for_model(task) for task in timekeeper.tasks(context.config_dir)
                          if not is_alarm(task)])


def _list_alarms(arguments, context):
    import timekeeper
    import alarm_ringing
    from alarm_kind import is_alarm
    alarms = [timekeeper.task_for_model(task) for task in timekeeper.tasks(context.config_dir)
              if is_alarm(task)]
    for item in _reminder_records(context.config_dir):
        if is_alarm(item):
            alarms.append(dict(id="reminder:" + item["id"], name=item["text"],
                               when=_local(datetime.fromisoformat(item["due_utc"])),
                               what=item["text"], next_run=_local(datetime.fromisoformat(item["due_utc"])),
                               enabled=item.get("status", "pending") == "pending",
                               status=item.get("status", "pending"), legacy=True))
    return _ok(alarms=alarms, ringing=alarm_ringing.active(context.config_dir))


def _list_alarm_music(arguments, context):
    import alarm_music
    return _ok(songs=alarm_music.choices(context.config_dir))


def _alarm_sound_settings(arguments, context):
    import alarm_music
    changes = {k: arguments[k] for k in ('ringtone_id', 'volume') if k in arguments}
    data = alarm_music.configure(context.config_dir, **changes) if changes else alarm_music.settings(context.config_dir)
    return _ok(settings=data, verified=True)


def _add_alarm(arguments, context):
    return _add_schedule(dict(arguments, kind="say", repeat=arguments.get("repeat", "once")),
                         context, _alarm=True)


def _update_alarm(arguments, context):
    import timekeeper
    from alarm_kind import is_alarm
    wanted = str(arguments.get("id", ""))
    if wanted.startswith("reminder:"):
        if 'ringtone_id' in arguments:
            raise ActionError('This legacy alarm does not support music. Create a music alarm, then decide whether to cancel the old one.')
        with _LOCK:
            items = _reminder_records(context.config_dir)
            item = next((r for r in items if r["id"] == wanted[9:] and is_alarm(r)), None)
            if item is None:
                raise ActionError('Alarm not found.')
            if arguments.get("repeat", "once") != "once":
                raise ActionError('This legacy alarm cannot become recurring. Confirm cancellation before creating a recurring replacement.')
            if arguments.get("text") is not None:
                text = arguments["text"]
                if not isinstance(text, str) or not 1 <= len(text.strip()) <= 300:
                    raise ActionError('Alarm text must contain 1-300 characters.')
                item["text"] = text.strip()
            due = datetime.fromisoformat(item["due_utc"]).astimezone(TOKYO)
            if "date" in arguments or "time" in arguments:
                due = _when({"at": arguments.get("date", due.strftime("%Y-%m-%d")) + " " +
                             arguments.get("time", due.strftime("%H:%M"))}, context)
                item["due_utc"] = due.astimezone(timezone.utc).isoformat()
            enabled = arguments.get("enabled", item.get("status", "pending") == "pending")
            if enabled and due <= context.now():
                raise ActionError('Alarm time is in the past. Provide a new date and time.')
            item.update(category="alarm", status="pending" if enabled else "paused")
            _save_reminders(context.config_dir, items)
            if item not in _reminder_records(context.config_dir):
                raise ActionError('Saved alarm could not be verified. Check the list first.')
        if not enabled:
            _dismiss_ringing_source(context, wanted)
        return _ok(verified=True, schedule=dict(when=_local(due), what=item["text"],
                                               next_run=_local(due) if enabled else ""))
    task = _timekeeper_call(lambda tk: tk.get_task(context.config_dir, wanted))
    if not is_alarm(task):
        raise ActionError('This record is not an alarm. Use the scheduled task tools.')
    return _update_schedule(arguments, context, _alarm=True)


def _delete_alarm(arguments, context):
    if arguments.get("permanent") is not True:
        raise ActionError('Permanent cancellation scope is unclear. Use skip_alarm for one date; ask the user when uncertain.')
    from alarm_kind import is_alarm
    wanted = str(arguments.get("id", ""))
    if wanted.startswith("reminder:"):
        with _LOCK:
            items = _reminder_records(context.config_dir)
            item = next((r for r in items if r["id"] == wanted[9:] and is_alarm(r)), None)
            if item is None:
                raise ActionError('Alarm not found.')
            item.update(category="alarm", status="cancelled")
            _save_reminders(context.config_dir, items)
        _dismiss_ringing_source(context, wanted)
        return _ok(deleted=wanted)
    task = _timekeeper_call(lambda tk: tk.get_task(context.config_dir, wanted))
    if not is_alarm(task):
        raise ActionError('This record is not an alarm.')
    return _delete_schedule(arguments, context, _alarm=True)


def _skip_alarm(arguments, context):
    import timekeeper
    wanted = str(arguments.get("id", ""))
    day = arguments.get("date")
    if wanted.startswith("reminder:"):
        from alarm_kind import is_alarm
        with _LOCK:
            items = _reminder_records(context.config_dir)
            item = next((r for r in items if r["id"] == wanted[9:] and is_alarm(r)), None)
            if item is None:
                raise ActionError('Alarm not found.')
            due = datetime.fromisoformat(item["due_utc"]).astimezone(TOKYO)
            if (timekeeper._day(day) != due.date() or
                    timekeeper._hhmm(arguments.get("time", due.strftime("%H:%M"))) != due.time().replace(second=0, microsecond=0)):
                raise ActionError('This alarm has no occurrence at that date and time.')
            if due <= context.now():
                raise ActionError('This alarm occurrence is in the past.')
            item["status"] = "cancelled"
            _save_reminders(context.config_dir, items)
            if item not in _reminder_records(context.config_dir):
                raise ActionError('Saved alarm exception could not be verified.')
        return _ok(verified=True, skipped=due.strftime("%Y-%m-%d %H:%M"), next_run="", scope="occurrence")
    task = _timekeeper_call(lambda tk: tk.skip_alarm(context.config_dir, wanted, day,
                                                   arguments.get("time"), now=_local_now(context)))
    return _ok(verified=True, scope="occurrence",
               skipped="%s %s" % (day, arguments.get("time") or task["time"]),
               next_run=task.get("next_run", ""), schedule=timekeeper.task_for_model(task))

def _dismiss_ringing_source(context, source_key):
    import alarm_ringing
    alarm_ringing.dismiss(context.config_dir,
                          [r["id"] for r in alarm_ringing.active(context.config_dir)
                           if r["source_key"] == source_key])


def _update_schedule(arguments, context, _alarm=False):
    import timekeeper
    from alarm_kind import is_alarm
    current = _timekeeper_call(lambda tk: tk.get_task(context.config_dir, arguments.get("id")))
    if is_alarm(current) and not _alarm:
        raise ActionError('This is an alarm. Use update_alarm.')
    fields = {key: arguments[key] for key in _SCHEDULE_FIELDS + ("enabled",)
              if key in arguments and key != "kind"}
    if _alarm:
        fields["category"] = "alarm"
        if 'ringtone_id' in arguments:
            fields['ringtone_id'] = arguments['ringtone_id']
    task = _timekeeper_call(lambda tk: tk.update_task(context.config_dir, arguments.get("id"),
                                                      now=_local_now(context), **fields))
    if timekeeper.get_task(context.config_dir, task["id"]) != task:
        raise ActionError('Saved scheduled task could not be verified. Check the list before repeating the operation.')
    if _alarm and arguments.get("enabled") is False:
        _dismiss_ringing_source(context, "schedule:" + task["id"])
    return _ok(schedule=timekeeper.task_for_model(task), verified=True)


def _preview_delete_schedule(arguments, context):
    import timekeeper
    try:
        task = timekeeper.get_task(context.config_dir, arguments.get("id"))
    except timekeeper.TimerError:
        return 'Delete scheduled task %s' % arguments.get("id")
    return 'Delete scheduled task: %s - %s' % (timekeeper.describe_when(task), timekeeper.describe_what(task))


def _delete_schedule(arguments, context, _alarm=False):
    from alarm_kind import is_alarm
    current = _timekeeper_call(lambda tk: tk.get_task(context.config_dir, arguments.get("id")))
    if is_alarm(current) and not _alarm:
        raise ActionError('This is an alarm. Use delete_alarm.')
    _timekeeper_call(lambda tk: tk.delete_task(context.config_dir, arguments.get("id")))
    if _alarm:
        _dismiss_ringing_source(context, "schedule:" + arguments["id"])
    return _ok(deleted=arguments.get("id"))


def _run_schedule_now(arguments, context):
    import timekeeper
    from alarm_kind import is_alarm
    task = _timekeeper_call(lambda tk: tk.get_task(context.config_dir, arguments.get("id")))
    if is_alarm(task):
        raise ActionError('This is an alarm, not a scheduled task. Set its time with alarm tools.')
    if context.run_task is None:
        raise ActionError('Cannot run scheduled tasks immediately right now.')
    context.run_task(task)
    return _ok(started=timekeeper.task_for_model(task))


# agent

def _task_settings(arguments, context):
    from dataclasses import asdict, replace
    import agent_client
    import evolution
    config = context.config_dir
    agent = agent_client.load_settings(config)
    evo = evolution.load_settings(config)
    actions = load_settings(config)
    allowed = {"model", "max_turns", "workspace", "enabled", "self_evolution_enabled",
               "agent_task_usd", "evolution_task_usd", "monthly_usd", "mining", "default_level"}
    if set(arguments) - allowed:
        raise ValueError('Unknown task setting.')
    agent_changes = {k: arguments[k] for k in ("model", "max_turns", "workspace") if k in arguments}
    if "workspace" in agent_changes and agent_changes["workspace"]:
        folder = Path(agent_changes["workspace"]).expanduser()
        if not folder.is_absolute() or not folder.is_dir():
            raise ValueError('Provide the full path of an existing working folder.')
        agent_changes["workspace"] = str(folder.resolve())
    if "agent_task_usd" in arguments:
        agent_changes["max_budget_usd"] = arguments["agent_task_usd"]
    evo_changes = {k: arguments[k] for k in ("monthly_usd", "mining", "default_level") if k in arguments}
    if "evolution_task_usd" in arguments:
        evo_changes["task_usd"] = arguments["evolution_task_usd"]
    action_changes = {field: arguments[key] for key, field in
                      (("enabled", "agent"), ("self_evolution_enabled", "self_fix")) if key in arguments}
    new_agent = replace(agent, **agent_changes).validate()
    new_evo = replace(evo, **evo_changes).validate()
    new_actions = replace(actions, **action_changes).validate()
    # Validate everything before any write; undo partial multi-file saves on failure.
    if arguments:
        paths = [config / agent_client.SETTINGS_NAME, config / evolution.SETTINGS, config / SETTINGS_NAME]
        before = {p: p.read_bytes() if p.exists() else None for p in paths}
        try:
            if agent_changes:
                agent_client.save_settings(config, new_agent)
            if evo_changes:
                evolution.save_settings(config, new_evo)
            if action_changes:
                save_settings(config, new_actions)
            if (agent_client.load_settings(config) != new_agent or evolution.load_settings(config) != new_evo
                    or load_settings(config) != new_actions):
                raise OSError('Saved settings did not match verification.')
        except Exception:
            for path, data in before.items():
                if data is None:
                    if path.exists():
                        path.unlink()
                else:
                    path.write_bytes(data)
            raise
    return _ok(saved=bool(arguments), installed=agent_client.installed(),
               settings=dict(model=new_agent.model, max_turns=new_agent.max_turns,
                             workspace=new_agent.workspace or str(agent_client.default_workspace()),
                             agent_task_usd=new_agent.max_budget_usd, evolution_task_usd=new_evo.task_usd,
                             monthly_usd=new_evo.monthly_usd, mining=new_evo.mining,
                             default_level=new_evo.default_level, enabled=new_actions.agent,
                             self_evolution_enabled=new_actions.self_fix),
               shared_budget=evolution.quota(config), models=agent_client.MODELS)


def _agent_task_status(arguments, context):
    if context.agent_status is None:
        raise ActionError('This window cannot read background task status.')
    return _ok(**context.agent_status())


def _start_agent_task(arguments, context):
    task = arguments.get("task")
    if not isinstance(task, str) or not 5 <= len(task.strip()) <= 8000:
        raise ValueError('Task description must contain 5-8000 characters.')
    folder = arguments.get("folder")
    if folder is not None and (not isinstance(folder, str) or len(folder) > 1024):
        raise ValueError('Invalid folder.')
    if context.start_agent is None:
        raise ActionError('Background tasks are unavailable right now.')
    return _ok(**context.start_agent(task.strip(), folder or None))


def _stop_agent_task(arguments, context):
    if context.stop_agent is None:
        raise ActionError('Cannot stop background tasks right now.')
    context.stop_agent()
    return _ok(stopping=True)


def _fix_myself(arguments, context):
    problem = arguments.get("problem")
    if not isinstance(problem, str) or not 5 <= len(problem.strip()) <= 4000:
        raise ValueError('Problem description must contain 5-4000 characters.')
    if context.start_self_fix is None:
        raise ActionError('Evolution is unavailable right now.')
    level = arguments.get("level")
    if level is not None:
        if type(level) is not int or level not in (1, 2, 3):
            raise ValueError('Level must be 1, 2 or 3.')
        return _ok(**context.start_self_fix(problem.strip(), level=level))
    return _ok(**context.start_self_fix(problem.strip()))


def _point_view(point):
    import evolution
    plan = point.get("plan") or {}
    return {"id": point["id"], "title": point["title"], "level": evolution.LEVELS.get(point.get("level")),
            "status": evolution.STATUSES.get(point["status"]), "kind": evolution.KINDS.get(point.get("kind")),
            "deadline": point.get("deadline", ""), "execution": point.get("execution", "self_evolution"),
            "plan_summary": plan.get("summary", ""), "plan": plan.get("plan", "")[:1500],
            "why": point.get("why", ""), "notes": point.get("notes", []),
            "risks": plan.get("risks", ""), "files": plan.get("files", []),
            "cost_note": plan.get("cost_note", ""), "result": point.get("result", ""),
            "how_to_use": plan.get("how_to_use", ""), "reason": point.get("reason", ""),
            "said": [q.get("text", "") for q in (point.get("quotes") or [])[:3]]}


def _note_evolution_point(arguments, context):
    import evolution
    level = arguments.get("level")
    if level is not None and (type(level) is not int or level not in evolution.LEVELS):
        raise ValueError('Level must be 1, 2 or 3.')
    point, merged = evolution.add(context.config_dir, arguments.get("title"), arguments.get("kind") or "missing",
                                  request=str(arguments.get("request") or ""), why=str(arguments.get("why") or ""),
                                  level=level if level in evolution.LEVELS else None, source="chat",
                                  deadline=arguments.get("deadline", ""))
    return _ok(evolution_point=_point_view(point), merged_with_existing=merged,
               note='Recorded: MYN will check existing support, create a plan and present it. No changes will be made without adoption.')


def _evolution_points(arguments, context):
    import evolution
    action = arguments.get("action")
    config = context.config_dir
    if action == "settings":
        from dataclasses import asdict, replace
        settings = evolution.load_settings(config)
        changes = {name: arguments[name] for name in ("monthly_usd", "task_usd", "mining", "default_level") if name in arguments}
        if changes:
            evolution.save_settings(config, replace(settings, **changes))
        saved = evolution.load_settings(config)
        return _ok(settings={k: v for k, v in asdict(saved).items() if k != "monthly_tokens"},
                   quota=evolution.quota(config), saved=bool(changes))
    if action == "list":
        view = arguments.get("view", "open")
        if view not in ("open", "completed", "rejected", "all"):
            raise ValueError('Invalid evolution point query scope.')
        offset = arguments.get("offset", 0)
        if type(offset) is not int or offset < 0:
            raise ValueError('Invalid list position.')
        items = sorted(evolution.points(config), key=evolution._order) if view == "all" else evolution.visible_points(config, view)
        return _ok(points=[dict(_point_view(item), task_budget=evolution.task_budget(config, item["id"])) for item in items[offset:offset+30]], total=len(items),
                   next_offset=offset+30 if offset+30 < len(items) else None, quota=evolution.quota(config))
    point = evolution.find(config, arguments.get("id"))
    if point is None:
        raise ActionError('Evolution point not found. Use list to check its ID first.')
    if action == "show":
        text, _ = evolution.announcement(point)
        if not text:
            return _ok(evolution_point=_point_view(point), note='No plan is available to adopt yet.')
        return _ok(evolution_point=_point_view(point), review_text=text,
                   task_budget=evolution.task_budget(config, point["id"]))
    if action == "update":
        if point["status"] != "built":
            raise ActionError('Evolution point "%s" is currently "%s" and cannot be updated yet.' % (point["title"], evolution.STATUSES[point["status"]]))
        if context.evolution_update is None:
            raise ActionError('Cannot update right now.')
        return _ok(**context.evolution_update(point["id"]))
    try:
        point = evolution.decide(config, point["id"], action, note=str(arguments.get("note") or ""),
                                 level=arguments.get("level"))
    except ValueError as error:
        raise ActionError(str(error)) from None
    return _ok(evolution_point=_point_view(point))


def _preview_evolution(arguments, context):
    import evolution
    if arguments.get("action") == "settings":
        return 'View or adjust evolution budgets and conversation review settings as requested: %s' % {k: v for k, v in arguments.items() if k != "action"}
    if arguments.get("action") != "update":
        return 'View or manage evolution points as requested: %s' % arguments.get("action")
    point = evolution.find(context.config_dir, arguments.get("id")) or {}
    return 'Apply evolution point "%s" to MYN (back up, install and restart; restore automatically if startup fails)' % point.get("title", arguments.get("id"))


# files

def _write_target(arguments, context):
    return _granted_path(context, arguments.get("path"), local_tools.grants(context.config_dir)["write"],
                         must_exist=False)


def _write_file(arguments, context):
    path = _write_target(arguments, context)
    content, mode = arguments.get("content"), arguments.get("mode") or "create"
    if not isinstance(content, str) or len(content) > MAX_WRITE_CHARS:
        raise ValueError('Content is invalid or too long.')
    if mode not in ("create", "overwrite", "append"):
        raise ValueError('Invalid write mode.')
    if path.suffix.lower() in (".exe", ".bat", ".cmd", ".ps1", ".vbs", ".js", ".msi", ".com", ".scr", ".lnk", ".reg"):
        raise PermissionError('Executable and script files cannot be written.')
    if not path.parent.is_dir():
        raise ActionError('Parent folder does not exist.')
    if path.is_dir():
        raise ActionError('This is a folder.')
    if mode == "create" and path.exists():
        raise ActionError('File already exists. Specify overwrite or append.')
    with path.open("a" if mode == "append" else "w", encoding="utf-8", newline="") as output:
        output.write(content)
    return _ok(path=str(path), mode=mode, chars=len(content))


def _make_folder(arguments, context):
    path = _write_target(arguments, context)
    if path.exists():
        raise ActionError('Already exists.')
    if not path.parent.is_dir():
        raise ActionError('Parent folder does not exist.')
    path.mkdir()
    return _ok(path=str(path))


def _preview_write(arguments, context):
    content = arguments.get("content") if isinstance(arguments.get("content"), str) else ""
    shown = content[:300] + ('... (%d characters total)' % len(content) if len(content) > 300 else "")
    verb = {"overwrite": 'Overwrite', "append": 'Append to'}.get(arguments.get("mode"), 'Create')
    return '%s file: %s\n\nContent:\n%s' % (verb, _write_target(arguments, context), shown)


# vision: the picture goes back to Claude as an image next to a short JSON note

def _picture(block, info, **extra):
    note = dict(ok=True, **{key: info[key] for key in ("what", "title", "name", "width", "height") if info.get(key)})
    note.update(extra)
    return [{"type": "text", "text": json.dumps(note, ensure_ascii=False)}, block], False


def _vision_call(make):
    import vision
    try:
        return make(vision)
    except vision.VisionError as error:
        raise ActionError(str(error)) from error


def _look_at_screen(arguments, context):
    target = arguments.get("target") or "window"
    block, info = _vision_call(lambda vision: vision.look(context.camera, target))
    return _picture(block, info)


def _look_at_image(arguments, context):
    path = arguments.get("path")
    try:
        resolved = local_tools._authorized_path(context.config_dir, path)
    except PermissionError:
        resolved = _write_root_path(path, context)
    if not resolved.is_file():
        raise ActionError('This is not a file.')
    block, info = _vision_call(lambda vision: vision.from_file(context.camera, resolved))
    return _picture(block, info, path=str(resolved))


def _write_root_path(path, context):
    """An image inside a folder granted for writing (also fine to look at)."""
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise PermissionError('Provide the full path inside an authorized folder.')
    resolved = Path(path).resolve(strict=True)
    for root in local_tools.grants(context.config_dir).get("write", []):
        try:
            base = Path(root).resolve(strict=True)
        except OSError:
            continue
        if resolved == base or base in resolved.parents:
            return resolved
    raise PermissionError('This image is outside authorized folders. Request access to its specific folder in chat, or attach it with the Image button.')


def _look_at_clipboard(arguments, context):
    block, info = _vision_call(lambda vision: vision.from_clipboard(context.camera))
    return _picture(block, info)


# calendar

def _agenda_call(make):
    import agenda
    try:
        return make(agenda)
    except agenda.AgendaError as error:
        raise ActionError(str(error)) from error


def _event_fields(arguments):
    fields = {key: arguments[key] for key in ("title", "start", "end", "location", "note", "repeat")
              if key in arguments}
    if "remind_minutes" in arguments:
        fields["remind"] = arguments["remind_minutes"]
    return fields


def _event_note(agenda, event):
    occurrence = dict(event, start=agenda.parse_when(event["start"])[0],
                      end=agenda.parse_when(event["end"])[0] if event.get("end") else None)
    return {"id": event["id"], "said": agenda.describe(occurrence),
            "remind_minutes": event.get("remind"), "repeat": agenda.REPEAT_NAMES[event["repeat"]],
            "start": event["start"], "end": event.get("end", ""), "all_day": event["all_day"]}


def _verified_event(agenda, config, event):
    """Read back the saved event, including reminder settings, before confirming."""
    saved = agenda.get(config, event["id"])
    if saved != event:
        raise ActionError('Saved calendar data did not match verification. Query events first to avoid duplicates.')
    return _ok(verified=True, **_event_note(agenda, saved))


def _add_event(arguments, context):
    return _agenda_call(lambda agenda: _verified_event(agenda, context.config_dir,
                        agenda.add(context.config_dir, **_event_fields(arguments))))


def _list_events(arguments, context):
    def run(agenda):
        today = context.now().astimezone(TOKYO).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
        start = agenda.parse_when(arguments["from_date"], 'Start date')[0] if arguments.get("from_date") else today
        end = (agenda.parse_when(arguments["to_date"], 'End date')[0] if arguments.get("to_date")
               else start + timedelta(days=6)) + timedelta(days=1)
        if end <= start or (end - start).days > 400:
            raise agenda.AgendaError('Invalid date range (maximum slightly over one year).')
        found = agenda.for_model(context.config_dir, start, end)
        if context.show_agenda and arguments.get("display", True):
            import agenda_display
            context.show_agenda(agenda_display.snapshot(
                context.config_dir, start.strftime("%Y-%m-%d"),
                (end - timedelta(days=1)).strftime("%Y-%m-%d")))
        return _ok(events=found, count=len(found), from_date=start.strftime("%Y-%m-%d"),
                   to_date=(end - timedelta(days=1)).strftime("%Y-%m-%d"))
    return _agenda_call(run)


def _update_event(arguments, context):
    return _agenda_call(lambda agenda: _verified_event(agenda, context.config_dir, agenda.update(
        context.config_dir, str(arguments.get("id") or ""), **_event_fields(arguments))))


def _delete_event(arguments, context):
    def run(agenda):
        event = agenda.get(context.config_dir, str(arguments.get("id") or ""))
        agenda.delete(context.config_dir, event["id"])
        if any(item["id"] == event["id"] for item in agenda.events(context.config_dir)):
            raise ActionError('Calendar deletion could not be verified. Query events first.')
        return _ok(deleted=event["title"], verified=True)
    return _agenda_call(run)


def _preview_delete_event(arguments, context):
    try:
        import agenda
        event = agenda.get(context.config_dir, str(arguments.get("id") or ""))
        return 'Delete calendar event: %s %s%s' % (event["start"], event["title"],
                                  ' (%s; all recurrences will be deleted)' % agenda.REPEAT_NAMES[event["repeat"]]
                                  if event.get("repeat") not in (None, "none") else "")
    except Exception:
        return 'Delete calendar event %s' % arguments.get("id")


# windows and shortcuts

def _windows_call(make):
    import windows_control
    try:
        return make(windows_control)
    except windows_control.WindowError as error:
        raise ActionError(str(error)) from error


def _find_window(title, context):
    return _windows_call(lambda wc: wc.match(context.desktop.windows(), title))


def _focus(window, context):
    if not context.desktop.focus(window["id"]):
        raise ActionError('Cannot bring "%s" to the front (Windows may restrict this). Click it manually.' % window["title"])


def _list_windows(arguments, context):
    windows = context.desktop.windows()
    return _ok(windows=[{key: w[key] for key in ("title", "program", "state")} for w in windows[:40]],
               count=len(windows))


def _focus_window(arguments, context):
    window = _find_window(arguments.get("title"), context)
    _focus(window, context)
    return _ok(title=window["title"])


def _window_state(arguments, context):
    window = _find_window(arguments.get("title"), context)
    action = arguments.get("action")
    if action == "close":
        context.desktop.close(window["id"])
    elif action in ("minimize", "maximize", "restore"):
        context.desktop.show(window["id"], action)
    else:
        raise ValueError('Invalid window action.')
    return _ok(title=window["title"], action=action)


def _target(arguments, context):
    """Bring the named window to the front first, if one is named; returns its title."""
    if arguments.get("window"):
        window = _find_window(arguments["window"], context)
        _focus(window, context)
        return window["title"]
    front = context.desktop.front()
    titles = [w["title"] for w in context.desktop.windows() if w["id"] == front]
    if not titles:
        raise ActionError('MYN is the foreground window. Specify the window to act on.')
    return titles[0]


def _press_keys(arguments, context):
    name, codes = _windows_call(lambda wc: wc.parse_keys(arguments.get("keys")))
    repeat = arguments.get("repeat", 1)
    if type(repeat) is not int or not 1 <= repeat <= 20:
        raise ValueError('Repeat count must be 1-20.')
    title = _target(arguments, context)
    for _ in range(repeat):
        context.desktop.press(codes)
    return _ok(keys=name, window=title, repeat=repeat)


def _type_text(arguments, context):
    text = arguments.get("text")
    if not isinstance(text, str) or not text or len(text) > 2000:
        raise ValueError('Text to type must contain 1-2000 characters.')
    title = _target(arguments, context)
    context.desktop.type_text(text)
    return _ok(window=title, characters=len(text))


def _preview_keys(arguments, context):
    import windows_control
    try:
        name = windows_control.parse_keys(arguments.get("keys"))[0]
    except windows_control.WindowError:
        name = str(arguments.get("keys"))
    where = arguments.get("window") or 'Foreground window'
    return 'In "%s", press %s' % (where, name)


def _preview_type(arguments, context):
    text = str(arguments.get("text") or "")
    shown = text[:300] + ('... (%d characters total)' % len(text) if len(text) > 300 else "")
    return 'Type in "%s":\n\n%s' % (arguments.get("window") or 'Foreground window', shown)


def _microphone_control(arguments, context):
    action = arguments.get("action")
    if action not in ("inspect", "switch"):
        raise ActionError('Invalid microphone operation.')
    if context.microphone_control is None:
        raise ActionError("This session cannot inspect or change the active microphone. Use MYN's main window.")
    import stt
    try:
        return _ok(**context.microphone_control(action, arguments.get("device")))
    except stt.STTError as error:
        raise ActionError(str(error)) from error


def _music_control(arguments, context):
    action = arguments.get("action")
    if action not in ("play", "pause", "resume", "next", "previous", "status"):
        raise ActionError('Invalid music action.')
    if context.music_control is None:
        raise ActionError("This window cannot control MYN's music player.")
    result = context.music_control(action)
    if not result.get("ok"):
        raise ActionError(result.get("error") or 'The music player did not confirm success.')
    # All action handlers return (JSON, is_error). The browser's dict previously
    # reached the chat tool dispatcher directly and failed unpacking AFTER play.
    return json.dumps(result, ensure_ascii=False), False


_HANDLERS = {"music_control": _music_control, "microphone_control": _microphone_control,
             "alarm_sound_settings": _alarm_sound_settings,
             "list_alarm_music": _list_alarm_music,
             "add_event": _add_event, "list_events": _list_events, "update_event": _update_event,
             "add_alarm": _add_alarm, "list_alarms": _list_alarms,
             "update_alarm": _update_alarm, "delete_alarm": _delete_alarm,
             "skip_alarm": _skip_alarm,
             "delete_event": _delete_event, "list_windows": _list_windows, "focus_window": _focus_window,
             "window_state": _window_state, "press_keys": _press_keys, "type_text": _type_text,
             "look_at_screen": _look_at_screen, "look_at_image": _look_at_image,
             "look_at_clipboard": _look_at_clipboard, "set_volume": _set_volume, "media_control": _media_control,
             "open_settings": _open_settings, "set_dark_mode": _set_dark_mode,
             "lock_screen": _lock_screen, "power": _power, "open_url": _open_url,
             "open_app": _open_app, "open_path": _open_path, "set_reminder": _set_reminder,
             "list_reminders": _list_reminders, "cancel_reminder": _cancel_reminder,
             "start_timer": _start_timer, "list_timers": _list_timers, "timer_control": _timer_control,
             "add_schedule": _add_schedule, "list_schedules": _list_schedules,
             "update_schedule": _update_schedule, "delete_schedule": _delete_schedule,
             "run_schedule_now": _run_schedule_now,
             "write_file": _write_file, "make_folder": _make_folder,
             "task_settings": _task_settings, "agent_task_status": _agent_task_status,
             "start_agent_task": _start_agent_task, "fix_myself": _fix_myself,
             "stop_agent_task": _stop_agent_task, "note_evolution_point": _note_evolution_point,
             "evolution_points": _evolution_points}
_PREVIEWS = {
    "alarm_sound_settings": lambda a, c: 'View or change default alarm sound and volume: %s' % a,
    "list_alarm_music": lambda a, c: 'View available music alarm sounds',
    "music_control": lambda a, c: 'Control MYN music: %s' % a.get("action", ""),
    "microphone_control": lambda a, c: 'View MYN microphone' if a.get("action") == "inspect" else
                                     'Switch MYN microphone to: %s' % a.get("device"),
    "add_event": lambda a, c: 'Create calendar event: %s %s' % (a.get("start"), a.get("title")),
    "list_events": lambda a, c: 'View calendar events',
    "update_event": lambda a, c: 'Edit calendar event %s' % a.get("id"),
    "delete_event": _preview_delete_event,
    "list_windows": lambda a, c: 'List open windows',
    "focus_window": lambda a, c: 'Switch to window: %s' % a.get("title"),
    "window_state": lambda a, c: '%s window: %s' % ({"minimize": 'Minimize', "maximize": 'Maximize', "restore": 'Restore',
                                               "close": 'Close'}.get(a.get("action"), 'Action'), a.get("title")),
    "press_keys": _preview_keys,
    "type_text": _preview_type,
    "look_at_screen": lambda a, c: 'View screen (%s)' % {"screen": 'Entire screen', "all": 'All screens'}.get(a.get("target"), 'Current window'),
    "look_at_image": lambda a, c: 'View image: %s' % a.get("path"),
    "look_at_clipboard": lambda a, c: 'View clipboard image',
    "set_volume": lambda a, c: 'Adjust system volume',
    "media_control": lambda a, c: 'Media control: %s' % a.get("action"),
    "open_settings": lambda a, c: 'Open settings: %s' % a.get("page"),
    "set_dark_mode": lambda a, c: 'Switch to %s mode' % ('Dark' if a.get("dark") else 'Light'),
    "lock_screen": lambda a, c: 'Lock screen',
    "power": lambda a, c: {"sleep": 'Put computer to sleep', "restart": 'Restart computer in 60 seconds',
                           "shutdown": 'Shut down in 60 seconds', "cancel": 'Cancel shutdown / restart'}.get(a.get("action"), 'Power action'),
    "open_url": lambda a, c: 'Open website: %s' % _url(a),
    "open_app": lambda a, c: 'Open program: %s' % _find_app(a, c)[0],
    "open_path": lambda a, c: 'Open: %s' % a.get("path"),
    "set_reminder": lambda a, c: 'Set reminder: %s' % a.get("text"),
    "add_alarm": lambda a, c: 'Set alarm: %s %s · %s' % (a.get("date", ""), a.get("time", ""), a.get("text", "")),
    "list_alarms": lambda a, c: 'View alarms',
    "update_alarm": lambda a, c: 'Edit alarm %s' % a.get("id"),
    "delete_alarm": lambda a, c: 'Delete alarm %s' % a.get("id"),
    "skip_alarm": lambda a, c: 'Skip only this alarm on %s %s; keep other dates' % (a.get("date", ""), a.get("time", "")),
    "list_reminders": lambda a, c: 'View reminders',
    "cancel_reminder": lambda a, c: 'Cancel reminder %s' % a.get("id"),
    "start_timer": lambda a, c: 'Start timer: %s' % (a.get("label") or ""),
    "list_timers": lambda a, c: 'View timers',
    "timer_control": lambda a, c: '%s timer' % {"pause": 'Pause', "resume": "Resume", "cancel": 'Cancel',
                                               "add": 'Add time: '}.get(a.get("action"), ""),
    "add_schedule": _preview_schedule,
    "list_schedules": lambda a, c: 'View scheduled tasks',
    "update_schedule": lambda a, c: 'Edit scheduled task %s' % a.get("id"),
    "delete_schedule": _preview_delete_schedule,
    "run_schedule_now": lambda a, c: 'Run scheduled task %s now' % a.get("id"),
    "write_file": _preview_write,
    "make_folder": lambda a, c: 'Create folder: %s' % _write_target(a, c),
    "task_settings": lambda a, c: 'View or change task settings as requested: %s' % a,
    "agent_task_status": lambda a, c: 'View background task progress and history',
    "start_agent_task": lambda a, c: 'Start background task: %s' % str(a.get("task", ""))[:200],
    "fix_myself": lambda a, c: 'Evolution: %s' % str(a.get("problem", ""))[:200],
    "note_evolution_point": lambda a, c: 'Record evolution point: %s' % a.get("title"),
    "evolution_points": _preview_evolution,
    "stop_agent_task": lambda a, c: 'Stop background tasks',
}


def grant_write(config_dir, directory):
    root = Path(directory).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('The authorization target is not a folder.')
    if local_tools.too_broad(root):
        raise ValueError(local_tools.BROAD_GRANT_ERROR)
    private = Path(config_dir).resolve()
    if root == private or private in root.parents or root in private.parents:
        raise ValueError("MYN's own data folder, or any folder containing it, cannot be granted write access.")
    data = local_tools.grants(config_dir)
    if str(root) not in data["write"]:
        data["write"].append(str(root))
    _write_json(Path(config_dir) / local_tools.GRANT_FILE, data)


def revoke_write(config_dir, directory):
    data = local_tools.grants(config_dir)
    data["write"] = [item for item in data["write"] if item != directory]
    _write_json(Path(config_dir) / local_tools.GRANT_FILE, data)
