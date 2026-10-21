"""The GPU window (WebView2 child process) and its message protocol.

Mixin composed by MYNWindow (myn_gui.py); shares its state and event queue."""

from gui_common import (  # noqa: F401
    os,
    re,
    time,
    tk,
)


class WebViewMixin:
    # ---- the GPU window (WebView2) -------------------------------------------------

    closing = False
    web_intro = False  # the GPU window's start-up animation is playing; text is hidden

    def _send_chat(self, shown, end):
        """Hand the current exchange to the page; it lays it out and animates it."""
        names = {"you": 'You', "error": 'Error'}
        items = []
        for offset, (tag, text, link) in enumerate(shown):
            newest = offset == len(shown) - 1
            item = {"key": end - len(shown) + offset, "tag": tag, "text": text,
                    "size": "big" if newest else "small",
                    "name": names.get(tag) if newest else None, "link": link,
                    "typing": bool(newest and tag == "myn" and self.draft_active)}
            rated = self.reply_links.get(link) if newest and link else None
            if rated is not None and not self.draft_active:  # Feedback controls for MYN's saved reply: like, dislike, and teach wording.
                item["reply"] = rated[0]
                item["rating"] = self.reply_ratings.get(rated[0], 0)
            items.append(item)
        self.web.js({"cmd": "chat", "items": items, "hint": bool(self.chat_offset)})

    def _show_voice(self, text):
        self.core_canvas.itemconfigure(self.voice_label, text=text)
        if self.web_active:
            self.web.js({"cmd": "voice", "text": text})

    def _web_event(self, message):
        """Something happened in the GPU window."""
        if self.web is None:
            return  # a late message from a window already given up on
        event = message.get("event")
        if self.sheets.event(message):
            return
        if event == "ready":
            if not self.web_active and self.web is not None:
                self.web_active = True
                process = getattr(self.web, "process", None)
                if self.session is not None and getattr(process, "pid", None):
                    self.session.skip_pids = [process.pid]  # the GPU window is MYN's, not "your window"
                self.web_intro = True
                self.core_animation.stop()  # the page draws the scene now
                self.web.configure()
                self.web.js({"cmd": "input", "visible": self.input_visible, "text": self.entry.get()})
                self._timers_changed()
                self._agenda_changed()
                self.chat_signature = None
                self._queue_chat()
                self.core_mode = None
                self._watch_core_mode()
            return
        if event in ("unavailable", "closed"):
            if self.closing:
                return
            if self.web_active:
                self.close()  # the user closed the window
            else:
                self._use_tk_window()
            return
        if not self.web_active:
            return
        if event == "intro_done":
            # The greeting only now becomes visible: start its countdown from here.
            self.web_intro = False
            self.chat_signature = None
            self._queue_chat()
            self._greet_aloud()
        elif event == "close":
            self.close()
        elif event == "minimize":
            self.web.window("minimize")
        elif event == "maximize":
            self.web.window("toggle_maximize")
        elif event == "menu":
            action = {"memories": self.open_memories, "voice": self.open_voice_settings,
                      "agents": self.open_agents_settings, "personal": self.open_personal_library,
                      "about": self.open_about,
                      "usage": self.open_usage_stats,
                      "player": self.open_media_player,
                      "preferences": self.open_preferences}.get(message.get("item"))
            if action is not None:
                action()
        elif event == "agenda_close":
            self._close_agenda_display()
        elif event in ("alarm_mini_open", "alarm_mini_close", "alarm_toggle"):
            self._alarm_mini_event(message)
        elif event == "media_state":
            self.media_playing = message.get("playing") is True
        elif event == 'alarm_music_result':
            self._alarm_music_result(message)
        elif event == 'alarm_settings':
            self._music_alarm_settings(message)
        elif event == "music_result":
            pending = getattr(self, "_music_requests", {}).get(message.get("request"))
            if pending:
                pending[1].update(message)
                pending[0].set()
        elif event == "send":
            text = str(message.get("text") or "").strip()
            if text or self.attached:
                self.entry.delete(0, tk.END)
                self.entry.insert(0, text)
                self.send()
        elif event == "attach_image":
            self.attach_image_dialog()
        elif event == "paste_image":
            self.attach_clipboard_image()
        elif event == "hide_input":
            self.hide_input()
        elif event == "toggle_input":
            self.toggle_input()
        elif event == "voice_press":
            self.voice_press()
        elif event == "voice_release":
            self.voice_release()
        elif event == "speak":
            value = self.speech_actions.get(str(message.get("link") or ""))
            if value:
                self.speak(value)
        elif event == "scroll":
            self._scroll_chat(None, 1 if message.get("step", 0) > 0 else -1)
        elif event == "feedback":
            try:
                reply_id, rating = int(message.get("reply")), int(message.get("rating"))
            except (TypeError, ValueError):
                return
            if rating in (-1, 0, 1):
                self.rate_reply(reply_id, rating)
        elif event == "reader":
            self.reader_open = bool(message.get("open"))
            self.reader_key = message.get("key")
            self.reader_revision = message.get("revision")
            self.reader_text = str(message.get("text") or "")[:6000]
            if not self.reader_open:
                self.reader_selection = None
        elif event == "selection":
            text = str(message.get("text") or "").strip()[:self.MAX_CIRCLED]
            self.reader_selection = (text, time.time()) if text else None
            if text:
                self._set_voice_label('Selected %d characters. Say or type what to do with them' % len(text), clear_after=3500)
        elif event == "ask_about":
            text = str(message.get("text") or "").strip()[:self.MAX_CIRCLED]
            request = str(message.get("request") or "").strip()
            if text and request:
                self.reader_selection = (text, time.time())
                self.entry.delete(0, tk.END)
                self.entry.insert(0, request)
                self.send()
        elif event == "feedback_edit":
            try:
                self.teach_reply(int(message.get("reply")))
            except (TypeError, ValueError):
                return

    def open_media_player(self):
        if self.web_active:
            self.web.js({"cmd": "media_open"})
        else:
            self._set_voice_label('The player requires WebView2. Restart MYN to use it.', clear_after=5000)

    def _music_control(self, action):
        """Chat worker waits for actual browser playback, never just for dispatch."""
        import threading
        import uuid
        if not self.web_active or self.web is None:
            return {"ok": False, "error": 'The music player requires WebView2.'}
        if threading.current_thread() is threading.main_thread():
            return {"ok": False, "error": 'Music requests must run in a chat task.'}
        if not hasattr(self, "_music_requests"):
            self._music_requests = {}
        request = uuid.uuid4().hex
        signal, result = threading.Event(), {}
        self._music_requests[request] = (signal, result)
        try:
            self.web.js({"cmd": "music_control", "action": action, "request": request,
                         "expires": int((time.time() + 20) * 1000)})
            if not signal.wait(25):
                return {"ok": False, "error": 'The player did not respond in time. Playback is unconfirmed.'}
            return result
        finally:
            self._music_requests.pop(request, None)

    BARGE_DUCK = 0.15  # (older behaviour: MYN only got quieter)
    BARGE_RESUME_S = 4.0  # paused, but no sentence came this long after you stopped: go on
    last_barge = 'No interruptions yet'
    BARGE_MAX_S = 12.0
    ducked_at = 0.0   # when MYN stopped to listen to you (0 = not stopped)
    barge_quiet_at = 0.0  # when your voice stopped while MYN was waiting

    def _quick_barge(self):
        """Pause on raw microphone activity only for known headphone OUTPUT.

        A headset microphone says nothing about where playback goes. Default/unknown
        outputs use recognized speech for interruption, avoiding pauses on speaker echo.
        The environment override affects early pausing, never echo rejection.
        """
        forced = os.environ.get("MYN_BARGE_IN", "").strip()
        if forced in ("0", "1"):
            return forced == "1"
        device = (getattr(self.tts_settings, "output_device", "") or "").casefold()
        return bool(re.search(r"headset|headphone|耳机|耳麦|airpods|earbuds|hands-free", device))

    def _unduck(self):
        'Resume MYN after a cough, a short acknowledgement or its own echo.'
        if self.ducked_at:
            self.ducked_at = 0.0
            self.barge_quiet_at = 0.0
            try:
                self.tts_player.resume()
            except Exception:
                pass

    def _mic_level(self, message):
        """The core reacts to input sound even outside an active conversation.
        While MYN talks, your voice starting makes it quiet at once (headset)."""
        now = time.time()
        if message.get("speech") and not self.ducked_at and self._speaking() and self._quick_barge():
            # You started talking: MYN stops at once and listens (user request 2026-10-17).
            self.ducked_at, self.barge_quiet_at = now, 0.0
            try:
                self.tts_player.pause()
            except Exception:
                self.ducked_at = 0.0
        elif self.ducked_at:
            if message.get("speech"):
                self.barge_quiet_at = 0.0
            elif not self.barge_quiet_at:
                self.barge_quiet_at = now
            if (self.barge_quiet_at and now - self.barge_quiet_at > self.BARGE_RESUME_S) or \
                    now - self.ducked_at > self.BARGE_MAX_S:
                self.last_barge = 'No complete sentence after the pause; continuing'
                self._unduck()  # nothing came of it (a cough, a short noise): go on
        if not self.web_active:
            return
        try:
            level = max(0.0, min(1.0, float(message.get("level", 0))))
        except (TypeError, ValueError):
            return
        if level > 0.02 or self.last_level_sent > 0.02:
            payload = {"cmd": "level", "value": level}
            bands = message.get('bands')
            if isinstance(bands, list) and len(bands) == 128:
                payload['bands'] = bands
            self.web.js(payload)
        self.last_level_sent = level

    last_level_sent = 0.0
    core_mode = None

    def _watch_core_mode(self):
        """Tell the page what MYN is doing, so the core can show it."""
        if not self.web_active:
            return
        now = time.time()
        if self._speaking() or self.draft_active:
            mode = "speaking"
        elif self.busy:
            mode = "thinking"
        elif self._in_conversation(now) or now <= self.awaiting_until or self.listening:
            mode = "listening"
        else:
            mode = "idle"
        if mode != self.core_mode:
            self.core_mode = mode
            self.web.js({"cmd": "mode", "mode": mode})
        self.root.after(120, self._watch_core_mode)

    def _web_timeout(self):
        if self.web is not None and not self.web_active:
            self._use_tk_window()

    def _use_tk_window(self):
        """The GPU window could not start: open the classic window instead."""
        web, self.web = self.web, None
        self.web_active = False
        if web is not None:
            try:
                web.close()
            except Exception:
                pass
        if not self.core_animation.running:
            self.core_animation.running = True
            self.core_animation.canvas.after(1, self.core_animation._tick)
        self._make_frameless()
        self.root.deiconify()

    def _place_input(self):
        """The input bar: a slim bar floating over the bottom of the scene."""
        self.input_row.place(x=28, rely=1.0, y=-16, anchor="sw", relwidth=1.0, width=-56)

    def _position_scene_controls(self, event):
        self.core_canvas.coords(self.close_icon, event.width - 24, 24)
        self.core_canvas.coords(self.minimize_icon, event.width - 58, 24)
        for key, entry, _ in self._dock_controls():
            self.core_canvas.coords(entry, *self._dock_xy(key))
        self.core_canvas.coords(self.voice_label, event.width / 2,
                                event.height / 2 + self.CHAT_TOP_GAP - 16)
        if self.input_visible:
            self._place_input()
        if getattr(self, "_agenda_payload", None) and not getattr(self, "web_active", False):
            self._draw_agenda_display()

    def _dock_controls(self):
        """Visible Tk dock entries share positioning, hover and click routing."""
        return (("agents", self.agents_entry, self.open_agents_settings),
                ("usage", self.usage_entry, self.open_usage_stats),
                ("input", self.input_entry, self.toggle_input))

    def _over_close_icon(self, x, y):
        width = self.core_canvas.winfo_width()
        return width - 42 <= x <= width - 6 and 6 <= y <= 44

    def _over_minimize_icon(self, x, y):
        width = self.core_canvas.winfo_width()
        return width - 76 <= x <= width - 42 and 6 <= y <= 44

    def _over_dock_entry(self, name, x, y):
        cx, cy = self._dock_xy(name)
        return abs(x-cx) <= 18 and abs(y-cy) <= 18

    def _over_agents_entry(self, x, y):
        return self._over_dock_entry('agents', x, y)

    def _over_usage_entry(self, x, y):
        return self._over_dock_entry('usage', x, y)

    def _over_input_entry(self, x, y):
        return self._over_dock_entry('input', x, y)

    def _hover_scene_controls(self, event):
        close_hover = self._over_close_icon(event.x, event.y)
        minimize_hover = self._over_minimize_icon(event.x, event.y)
        self.core_canvas.itemconfigure(self.close_icon, fill="#f0aaa8" if close_hover else "#5f7c88")
        self.core_canvas.itemconfigure(self.minimize_icon, fill="#bfd9e0" if minimize_hover else "#5f7c88")
        hovered = close_hover or minimize_hover
        for name, entry, _ in self._dock_controls():
            hit = self._over_dock_entry(name, event.x, event.y)
            self.core_canvas.itemconfigure(entry, fill="#e1f1f4" if hit else "#9cb8c3")
            hovered = hovered or hit
        self.core_canvas.configure(cursor="hand2" if hovered else "")

    def _leave_scene_controls(self, _event):
        self.core_canvas.itemconfigure(self.close_icon, fill="#5f7c88")
        self.core_canvas.itemconfigure(self.minimize_icon, fill="#5f7c88")
        for _, entry, _ in self._dock_controls():
            self.core_canvas.itemconfigure(entry, fill="#9cb8c3")
        self.core_canvas.configure(cursor="")

    def _click_scene_controls(self, event):
        if self._over_close_icon(event.x, event.y):
            self.close()
        elif self._over_minimize_icon(event.x, event.y):
            self.minimize()
        else:
            for name, _, action in self._dock_controls():
                if self._over_dock_entry(name, event.x, event.y):
                    action()
                    break

    def toggle_fps(self):
        self.fps_visible = not self.fps_visible
        if self.fps_visible:
            self._update_fps()
        else:
            self.core_canvas.itemconfigure(self.fps_label, text="")

    def _update_fps(self):
        if not self.fps_visible:
            return
        core = self.core_animation
        self.core_canvas.itemconfigure(self.fps_label, text='%.0f fps  |  Display %d Hz' % (
            core.fps, core.refresh_hz))
        self.root.after(500, self._update_fps)
