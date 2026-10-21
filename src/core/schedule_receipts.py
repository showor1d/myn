"""Scheduling confirmations come from tool receipts, not the model's promises.

Text matching only selects the guarded reply path; it NEVER calculates a time or
creates a task. Natural time interpretation still belongs to the model/tool call.
"""
import json
import re

MUTATIONS = {"set_reminder", "add_schedule", "update_schedule", "cancel_reminder",
             "add_alarm", "update_alarm", "delete_alarm", "skip_alarm",
             "delete_schedule", "start_timer", "timer_control",
             "add_event", "update_event", "delete_event"}
RELATED = re.compile(r"日程|日历|行程|闹钟|叫.{0,4}起床|叫醒|喊我|提醒|定时|计时|倒计时|calendar|agenda|alarm|remind|timer|wake me", re.I)
REQUEST = re.compile(r"(?:帮我|给我|请|记得|到时|明[天早]|每天|每晚).{0,35}"
                     r"(?:提醒|叫我|叫醒|喊我|闹钟|计时|定时)|"
                     r"(?:定|订|设|建|加|改|取消|删|开始).{0,12}(?:提醒|闹钟|计时|定时)|"
                     r"(?:remind me|wake me|set.{0,15}(?:alarm|timer))", re.I)
CLAIM = re.compile(r"设好|定好|订好|安排好|设置成功|已.{0,8}(?:设|定|订|安排|取消|删除|创建)|"
                   r"(?:到时|明早|明天|八点|8点).{0,15}(?:会|叫你|提醒你)|"
                   r"(?:i.ll|i will).{0,30}(?:remind|wake)|(?:alarm|reminder|timer).{0,15}(?:set|created)", re.I)
LIMIT = 'The computer must stay on and awake, MYN must be running, and audio output must be available.'
DISCUSSION = re.compile(r"昨天|昨晚|之前|当时|刚才|有没有|是不是|了吗|了么|怎么|如何|为什么|"
                        r"代码|假如|如果|教程|example|\bhow\b|\bwhy\b|did you", re.I)


def relevant(question, turns=()):
    if RELATED.search(question):
        return True
    # Brief follow-ups inherit the scheduling context.
    return len(question) < 45 and any(RELATED.search(t.get("question", "") + t.get("answer", ""))
                                     for t in list(turns)[-2:])


def snapshot(config):
    import local_actions
    import agenda
    import timekeeper
    import alarm_delivery
    result = {}
    def calendar_evidence():
        events = agenda.events(config)
        return {"events": [local_actions._event_note(agenda, event) for event in events[:60]],
                "count": len(events)}
    for name, read in (
            ("calendar", calendar_evidence),
            ("reminders", lambda: local_actions._list_reminders({}, local_actions.ActionContext(config))),
            ("schedules", lambda: json.loads(local_actions._list_schedules({}, local_actions.ActionContext(config))[0])["schedules"]),
            ("alarms", lambda: json.loads(local_actions._list_alarms({}, local_actions.ActionContext(config))[0])),
            ("delivery", lambda: alarm_delivery.recent(config))):
        try:
            value = read()
            result[name] = json.loads(value[0]) if name == "reminders" else value
        except Exception as error:
            result[name] = {"error": str(error)[:200]}
    return result


def finish(question, answer, receipts, guarded):
    if receipts:
        lines = []
        for name, data, failed in receipts:
            if failed or data.get("ok") is not True:
                kind = 'Calendar' if name in ("add_event", "update_event", "delete_event") else 'Reminder'
                lines.append('Cannot confirm this %s action succeeded: ' % kind + str(data.get("error") or 'No valid save receipt'))
            elif name in ("add_event", "update_event") and data.get("verified"):
                minutes = data.get("remind_minutes")
                if minutes is None:
                    reminder = 'No reminder set'
                elif minutes == 0:
                    reminder = 'Remind at 09:00 that day' if data.get("all_day") else 'Remind at the event time'
                else:
                    reminder = 'Remind %d minutes before%s' % (
                        minutes, ' (all-day events use 09:00 as the reference)' if data.get("all_day") else "")
                repeat = data.get("repeat")
                lines.append('%s calendar event: %s (Japan time); %s%s.' %
                             ('Saved' if name == "add_event" else 'Updated', data["said"], reminder,
                              "；" + repeat if repeat and repeat != 'Never' else ""))
                if minutes is None and guarded and REQUEST.search(question) and not DISCUSSION.search(question):
                    lines.append('Event saved without a reminder. Remind at the event time or earlier?')
            elif name == "delete_event" and data.get("verified"):
                lines.append('Calendar event deleted: %s.' % data["deleted"])
            elif name == "set_reminder" and data.get("verified"):
                lines.append('Reminder saved: %s (Japan time), %s.' % (data["due_local"], data["text"]))
            elif name in ("add_schedule", "update_schedule", "add_alarm", "update_alarm") and data.get("verified"):
                task = data["schedule"]
                lines.append('Saved %s: %s; %s; next %s (Japan time).' %
                             ('Alarm' if name in ("add_alarm", "update_alarm") else 'Scheduled task',
                              task["when"], task["what"], task["next_run"] or 'Disabled'))
                if task.get('ringtone_name'):
                    lines.append('Music alarm: ' + task['ringtone_name'] + '。')
            elif name == "start_timer":
                timer = data["timer"]
                lines.append('Timer saved: %s, %s remaining.' % (timer.get("label") or 'Timer', timer["left"]))
            elif name in ("cancel_reminder", "delete_schedule"):
                lines.append('Specified reminder cancelled.' if name == "cancel_reminder" else 'Specified scheduled task deleted.')
            elif name == "delete_alarm":
                lines.append('Specified alarm permanently deleted; it will not repeat.')
            elif name == "skip_alarm" and data.get("verified"):
                lines.append('Skipped this alarm on %s (Japan time); %s.' %
                             (data["skipped"], 'Next alarm remains at %s' % data["next_run"]
                              if data.get("next_run") else 'No other future occurrences are enabled'))
            elif name == "timer_control":
                lines.append('Timer action completed: ' + str(data.get("action", "")))
            else:
                kind = 'Calendar' if name in ("add_event", "update_event", "delete_event") else 'Reminder'
                lines.append('No verifiable save result. Query the %s list first.' % kind)
        if any(not failed and (name in ("set_reminder", "add_schedule", "add_alarm", "start_timer")
                              or (name in ("add_event", "update_event") and data.get("verified")
                                  and data.get("remind_minutes") is not None))
               for name, data, failed in receipts):
            lines.append(LIMIT)
        return "\n".join(lines)
    requested = guarded and REQUEST.search(question) and not DISCUSSION.search(question)
    if requested and CLAIM.search(answer):
        return 'No successful reminder creation or edit was recorded. Tell me the time and content again; I will verify the saved result before confirming.'
    if requested:
        # Fail closed for a direct scheduling request even if the empty promise was
        # paraphrased. Keep actual clarification questions; no task is auto-created.
        if not re.search(r"[?？]|请.{0,8}(?:告诉|提供|确认)|需要.{0,8}(?:时间|几点)", answer):
            return 'No successful reminder creation or edit was recorded. Tell me the exact reminder time and content.'
    return answer
