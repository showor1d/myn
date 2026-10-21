"""Timers and scheduled tasks firing in the window.

Mixin composed by MYNWindow (myn_gui.py); shares its state and event queue."""

from gui_common import (  # noqa: F401
    _source_label,
    config_for_db,
    json,
    local_actions,
    os,
    presence,
    threading,
    time,
    timekeeper,
    tk,
)


class TimersMixin:
    # ---- timers and scheduled tasks ----------------------------------------------------
    task_queue = ()        # scheduled tasks waiting for MYN to be free
    scheduled_running = None  # the "ask" task whose reply is being made
    alarm_delivery_request = None
    alarm_poll_errors = None
    _ring_speech_id = None
    _ring_speech_request = None
    _ring_waiting_since = 0
    _ring_not_before = 0
    _timer_hud_data = None
    _timer_hud_at = 0
    _timer_hud_after = None
    _timer_hud_notice = (0, "")

    def _music_alarm_settings(self, message):
        import alarm_music
        request = message.get('request')
        if not isinstance(request, str) or not 1 <= len(request) <= 80:
            return
        try:
            config = config_for_db(self.session.db_path)
            changes = {k: message[k] for k in ('ringtone_id', 'volume') if k in message}
            data = alarm_music.configure(config, **changes) if changes else alarm_music.settings(config)
            result = dict(ok=True, settings=data)
        except (OSError, ValueError, TypeError) as error:
            result = dict(ok=False, error=str(error))
        self.web.js(dict(cmd='alarm_settings_result', request=request, **result))

    def _timers_changed(self):
        """Refresh only the main-window animation; no settings page exists."""
        if self.session is None:
            return
        import timer_display
        try:
            payload = timer_display.snapshot(config_for_db(self.session.db_path))
        except (OSError, ValueError, KeyError, local_actions.ActionError) as error:
            self._alarm_poll_error("display", error)
            return
        self._timer_hud_data, self._timer_hud_at = payload, time.time()
        if getattr(self, "web_active", False):
            self.web.js(payload)
            if getattr(self, "_alarm_mini_visible", False):
                self._alarm_mini_list()
        elif self._timer_hud_after is None and hasattr(self, "core_canvas"):
            self._timer_hud_tick()

    def _alarm_mini_list(self, request=None, error=""):
        context = local_actions.ActionContext(config_for_db(self.session.db_path),
                                               confirm=lambda _: True)
        try:
            raw, failed = local_actions.execute("list_alarms", {}, context)
            data = json.loads(raw)
            self.web.js(dict(cmd="alarm_list", alarms=data.get("alarms", []),
                             error=error or (data.get("error", "") if failed else ""),
                             request=request))
        except Exception:
            self.web.js(dict(cmd="alarm_list", error='Could not read the alarm list. Please retry.', request=request))

    def _alarm_mini_event(self, message):
        if self.session is None:
            return
        event = message.get("event")
        if event == "alarm_mini_close":
            self._alarm_mini_visible = False
        elif event == "alarm_mini_open":
            self._alarm_mini_visible = True
            self._alarm_mini_list()
        elif event == "alarm_toggle":
            error = ""
            if not isinstance(message.get("id"), str) or type(message.get("enabled")) is not bool:
                error = 'Invalid alarm toggle parameters.'
            else:
                # Clicking the switch explicitly authorizes this reversible series pause/resume.
                context = local_actions.ActionContext(config_for_db(self.session.db_path),
                                                       confirm=lambda _: True)
                raw, failed = local_actions.execute("update_alarm",
                    dict(id=message["id"], enabled=message["enabled"]), context)
                if failed:
                    error = json.loads(raw).get("error", 'Alarm toggle was not saved.')
            self._timers_changed()
            self._alarm_mini_list(message.get("request"), error)

    def _timer_hud_tick(self):
        """Classic-window counterpart; no background rectangle or editable page."""
        self._timer_hud_after = None
        if getattr(self, "closing", False) or getattr(self, "web_active", False):
            return
        canvas = self.core_canvas
        try:
            canvas.delete("timer_hud")
            payload = self._timer_hud_data or {"items": [], "count": 0}
            items = payload["items"]
            alarm_upcoming = payload.get("alarm_upcoming", False)
            if alarm_upcoming:
                ax, ay = self._dock_xy('alarm')
                dy = ay - 76
                canvas.create_line(24, 84, 27, 80, 27, 73, 29, 69, 34, 67, 39, 69,
                                   41, 73, 41, 80, 44, 84, 24, 84,
                                   fill="#bcf5ff", width=2, tags=("timer_hud","alarm_dock"))
                canvas.create_line(31, 88, 37, 88, fill="#bcf5ff", width=2, tags=("timer_hud","alarm_dock"))
                canvas.move('alarm_dock',ax-34,dy)
            if hasattr(self, 'agents_entry'):
                canvas.coords(self.agents_entry, *self._dock_xy('agents'))
            if hasattr(self, "input_entry"):
                canvas.coords(self.input_entry, *self._dock_xy('input'))
            if hasattr(self, "usage_entry"):
                canvas.coords(self.usage_entry, *self._dock_xy('usage'))
            timer_top = 54
            for index, item in enumerate(items):
                y = timer_top + index * 62
                left = max(0, item["remaining"] - (0 if item["paused"] else time.time() - self._timer_hud_at))
                color = "#8caab7" if item["paused"] else "#8de8fa"
                fraction = min(1, left / item["total"]) if item["total"] else .24
                start = 90 if item["kind"] == "countdown" else -(time.time() * 35) % 360
                canvas.create_oval(22, y, 58, y + 36, outline="#354d5b", tags="timer_hud")
                canvas.create_arc(22, y, 58, y + 36, style=tk.ARC, start=start,
                                  extent=-max(.1, fraction * 359.9), outline=color, width=2, tags="timer_hud")
                text = item["label"][:22] + "\n" + ('Paused: ' if item["paused"] else "") + timekeeper.clock(left)
                canvas.create_text(69, y + 18, text=text, anchor="w", fill=color,
                                   font=(self.font_family, 10), tags="timer_hud")
            y = timer_top + len(items) * 62
            if payload["count"] > len(items):
                canvas.create_text(22, y, text='%d more schedules; ask me for details' % (payload["count"] - len(items)),
                                   anchor="nw", fill="#bedbe5", tags="timer_hud")
                y += 20
            if self._timer_hud_notice[0] > time.time():
                canvas.create_text(22, y, text='Time is up: ' + self._timer_hud_notice[1], width=230,
                                   anchor="nw", fill="#bcf5ff", tags="timer_hud")
            canvas.tag_raise("timer_hud")
            if items or self._timer_hud_notice[0] > time.time():
                self._timer_hud_after = self.root.after(100, self._timer_hud_tick)
        except tk.TclError:
            pass  # window closed

    def _alarm_poll_error(self, source, error):
        errors = self.alarm_poll_errors = dict(self.alarm_poll_errors or {})
        detail = str(error)
        if errors.get(source) != detail:
            errors[source] = detail
            self.append('Error', 'Reminder check failed: ' + detail[:200])

    def _alarm_record(self, config, delivery_id, status, **fields):
        import alarm_delivery
        try:
            alarm_delivery.record(config, delivery_id, status, **fields)
        except OSError:
            self.events.put(("call", lambda: self.append('Error', 'Could not save the alarm execution record. Speech playback will still be attempted.')))

    def _alarm_audio_result(self, request_id, status, detail=""):
        if request_id == self._ring_speech_request:
            if status in ("tts_submitted", "tts_failed", "tts_interrupted"):
                self._ring_waiting_since = 0
                self._ring_not_before = time.time() + 30
        delivery = self.alarm_delivery_request
        if delivery and delivery[0] == request_id:
            self._alarm_record(delivery[1], delivery[2], status, detail=detail[:200])
            if status in ("tts_failed", "tts_interrupted"):
                self.alarm_delivery_request = None

    def _deliver_alarm(self, text, source, source_id, repeat=False):
        """Speak an alarm even in quiet scenes; no additional electronic tone."""
        import uuid
        config = config_for_db(self.session.db_path)
        ringing = None
        if source == "alarm":
            import alarm_ringing
            ringing = next((r for r in alarm_ringing.active(config) if r["source_key"] == source_id), None)
            if ringing is None:
                return  # already acknowledged after dispatch, never resurrect it
            if not alarm_ringing.mark_attempt(config, ringing["id"]):
                return
        delivery_id = uuid.uuid4().hex
        self._alarm_record(config, delivery_id, "dispatched", source=source,
                           source_id=source_id, text=text)
        if not repeat:
            self.append("MYN", text, speakable=True)

        if source != "alarm" and getattr(self, "web_active", False):
            self.web.js({"cmd": "timer_finished", "text": text[:80]})
        if source != "alarm":
            self._timer_hud_notice = (time.time() + 4, text[:45])
        self._timers_changed()
        if (ringing and ringing.get('ringtone_id') and getattr(self, 'web_active', False)
                and ringing['id'] not in getattr(self, '_alarm_music_failed', set())):
            self._sync_alarm_music()
            self._alarm_record(config, delivery_id, 'music_requested')
            return
        try:
            self.speak(text, purpose="alarm")
            if ringing:
                self._ring_speech_id = ringing["id"]
                self._ring_speech_request = self.tts_request_id
                self._ring_waiting_since = time.time()
                self._ring_not_before = time.time() + 30
            self.alarm_delivery_request = (self.tts_request_id, config, delivery_id)
            self._alarm_record(config, delivery_id, "tts_requested")
        except Exception as error:
            self._alarm_record(config, delivery_id, "tts_failed", detail=str(error)[:200])
            self.append('Error', 'Alarm speech could not start. Check the speech service and audio output; the text reminder is preserved.')

    def _sync_ringing(self):
        """Stop acknowledged audio; ordinary interruptions never clear persistent state."""
        import alarm_ringing
        active = alarm_ringing.active(config_for_db(self.session.db_path))
        self._sync_alarm_music(active)
        if self._ring_speech_id and self._ring_speech_id not in {r["id"] for r in active}:
            if self._ring_speech_request == self.tts_request_id:
                self.stop_speech()  # invalidates queued/late synthesis too
            self._ring_speech_id = None
            self._ring_speech_request = None
            self._ring_waiting_since = 0
        try:
            if alarm_ringing.cleanup_completed(config_for_db(self.session.db_path)):
                self._timers_changed()
        except (OSError, ValueError) as error:
            self._alarm_poll_error("completed_cleanup", error)
        return active

    def _sync_alarm_music(self, active=None):
        if not getattr(self, 'web_active', False):
            return
        import alarm_ringing
        if active is None:
            active = alarm_ringing.active(config_for_db(self.session.db_path))
        failed = getattr(self, '_alarm_music_failed', set())
        rows = [dict(id=r['id'], track=r['ringtone_id']) for r in active
                if r.get('ringtone_id') and r['id'] not in failed]
        import alarm_music
        volume = getattr(self, '_alarm_music_volume', .8)
        try:
            volume = alarm_music.settings(config_for_db(self.session.db_path))['volume']
            self._alarm_music_volume = volume
        except (OSError, ValueError) as error:
            self._alarm_poll_error('music_volume', error)
            # A damaged setting must never prevent an acknowledged alarm stopping.
        paused = bool(self.busy or getattr(self, 'listening', False)
                      or getattr(self, 'ducked_at', 0) or self._speaking())
        # Also send an empty snapshot after acknowledgement, before source cleanup.
        self.web.js(dict(cmd='alarm_music', alarms=rows, paused=paused, volume=volume))

    def _alarm_music_result(self, message):
        import alarm_ringing
        active = alarm_ringing.active(config_for_db(self.session.db_path))
        row = next((r for r in active if r['id'] == message.get('id') and r.get('ringtone_id')), None)
        if row is None:
            return  # stale failures never resurrect acknowledged occurrences
        self._alarm_record(config_for_db(self.session.db_path), row['id'],
                           'music_' + ('playing' if message.get('ok') is True else 'failed'))
        if message.get('ok') is not True:
            self._alarm_music_failed = set(getattr(self, '_alarm_music_failed', set())) | {row['id']}
            self._sync_alarm_music(active)
            self._deliver_alarm(row['text'], 'alarm', row['source_key'], repeat=True)

    def _repeat_ringing(self):
        import alarm_ringing
        try:
            active = self._sync_ringing()
            now = time.time()
            speaking = self._speaking()
            if speaking and self._ring_speech_request == self.tts_request_id:
                self._ring_not_before = now + alarm_ringing.INTERVAL
            if (not active or self.busy or getattr(self, "listening", False)
                    or getattr(self, "ducked_at", 0) or speaking or now < self._ring_not_before
                    or (self._ring_waiting_since and now - self._ring_waiting_since < 120)):
                return
            due = sorted((r for r in active if now - r.get("last_attempt", 0) >= alarm_ringing.INTERVAL),
                         key=lambda r: r.get("last_attempt", 0))
            if due:
                row = due[0]
                self._deliver_alarm(row["text"], "alarm", row["source_key"], repeat=True)
        except (OSError, ValueError, KeyError) as error:
            self._alarm_poll_error("ringing", error)

    def run_task(self, task):
        'Run a scheduled task at its due time or on an explicit run-now request. Safe from any thread.'
        self.events.put(("call", lambda: self._queue_task(task)))

    def _queue_task(self, task):
        if task["kind"] == "say":
            from alarm_kind import is_alarm
            self._deliver_alarm(task["text"], "alarm" if is_alarm(task) else "schedule",
                                "schedule:" + task["id"] if is_alarm(task) else task["id"])
            self._note_task(config_for_db(self.session.db_path), task, 'Reminder dispatched; check the execution record for audio status')
            return
        self.task_queue = tuple(self.task_queue) + (task,)
        self._next_task()

    def _next_task(self):
        if getattr(self, "backup_working", False):
            self.root.after(1000, self._next_task)
            return
        if not self.task_queue or self.busy or self.session is None:
            return
        task, self.task_queue = self.task_queue[0], tuple(self.task_queue[1:])
        config = config_for_db(self.session.db_path)
        if task["kind"] == "say":
            from alarm_kind import is_alarm
            self._deliver_alarm(task["text"], "alarm" if is_alarm(task) else "schedule",
                                "schedule:" + task["id"] if is_alarm(task) else task["id"])
            self._note_task(config, task, 'Reminder dispatched; check the execution record for audio status')
            self.root.after(200, self._next_task)
        elif task["kind"] == "action":
            threading.Thread(target=self._task_action, args=(config, task), daemon=True).start()
            self.root.after(200, self._next_task)
        else:
            self.busy = True
            self.reply_silenced = False
            self.scheduled_running = task
            self.speech_stream = self._reply_speech()
            self.button.configure(state=tk.DISABLED)
            self.status.configure(text='Running scheduled task...')
            threading.Thread(target=self._ask_scheduled, args=(config, task), daemon=True).start()

    def _note_task(self, config, task, result):
        try:
            timekeeper.note_result(config, task["id"], time.strftime("%m-%d %H:%M ") + result)
        except (OSError, ValueError):
            pass
        self.events.put(("call", self._timers_changed))

    def _task_action(self, config, task):
        """A kind=action task: the one fixed action, agreed to when the task was made."""
        action = task["action"]
        context = local_actions.ActionContext(config, confirm=lambda _preview: True,
                                              camera=self._camera(), skip_pids=self._skip_pids())
        try:
            content, is_error = local_actions.execute(action["name"], dict(action.get("arguments") or {}), context)
            result = json.loads(content) if content.strip().startswith("{") else {}
        except Exception as error:  # never let a task take MYN down
            is_error, result = True, {"error": str(error) or type(error).__name__}
        name = task.get("name") or 'Scheduled task'
        if is_error:
            reason = str(result.get("error") or 'Not completed')[:120]
            self._note_task(config, task, 'Not completed: ' + reason)
            self.events.put(("call", lambda: self.append('Error', 'Scheduled task "%s" could not complete: %s' % (name, reason))))
        else:
            self._note_task(config, task, 'Completed')
            self.events.put(("call", lambda: self._set_voice_label('Scheduled task: %s' % name, clear_after=3500)))

    def _ask_scheduled(self, config, task):
        try:
            result = self.session.ask_scheduled(
                task, on_text=lambda delta: self.events.put(("stream", delta)),
                on_tool=lambda name: self.events.put(("web_tool", name)))
            text = result["answer"]
            web = result.get("web_sources") or []
            if web:
                text += '\n\nSources: ' + "；".join(_source_label(item) for item in web)
            self._note_task(config, task, 'Answered' if not result.get("unavailable") else 'Could not answer')
            self.events.put(("answer", text, result["answer"], None))
        except Exception as error:
            self._note_task(config, task, 'Error: %s' % (str(error)[:80] or type(error).__name__))
            self.events.put(("error", 'Scheduled task "%s" failed: %s' % (task.get("name", ""), error)))

    def _skip_pids(self):
        pids = [os.getpid()]
        process = getattr(self.web, "process", None) if self.web is not None else None
        if process is not None and getattr(process, "pid", None):
            pids.append(process.pid)
        return pids

    def _sleep_status(self):
        try:
            return presence.sleep_estimate(self.session.db_path, config_for_db(self.session.db_path))
        except Exception as error:
            return {"error": str(error)}
