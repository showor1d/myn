"""Settings: panels drawn inside the main view (GPU window) and the Tk settings windows.

Mixin composed by MYNWindow (myn_gui.py); shares its state and event queue."""

from gui_common import (
    ChatSession,
    Path,
    backup,
    config_for_db,
    credentials,
    datetime,
    json,
    local_actions,
    memory_query,
    message_log,
    messagebox,
    os,
    personal_profile,
    presence,
    root_for_db,
    scene_memory,
    sheets,
    sqlite3,
    stt,
    theme,
    threading,
    timekeeper,
    tk,
    tokyo_display,
    ttk,
    tts,
    usage_stats,
    user_data_dir,
)


class SettingsMixin:
    # ---- settings drawn inside the main view (GPU window) ------------------------------------
    def _ask_user(self, title, text, then, yes='OK', no='Decline'):
        """A yes/no question: inside the page with the GPU window (no second window), else
        the classic dialog. then(bool) gets the answer; "no" is the default."""
        if self.web_active and self.web is not None:
            self.sheets.confirm(text, then, title=title, yes=yes, no=no)
            return
        then(messagebox.askyesno(title, text, parent=self.root, default=messagebox.NO))

    def _show_error(self, title, text, parent=None):
        """A problem to report: inside the open panel (GPU window), else the classic dialog."""
        if self.web_active and self.web is not None:
            if self.sheets.is_open():
                self.sheets.toast(text, "bad")
            else:
                self.append('Error', "%s：%s" % (title, text))
            return
        messagebox.showerror(title, text, parent=parent or self.root)

    @property
    def sheets(self):
        """Settings drawn inside the page (made on first use, also for windows built by tests)."""
        host = self.__dict__.get("_sheets")
        if host is None:
            host = self.__dict__["_sheets"] = sheets.SheetHost(
                self._sheet_send, lambda ms, fn: self.events.put(("call", fn)))
        return host

    def _sheet_send(self, cmd):
        if self.web_active and self.web is not None:
            self.web.js(cmd)

    def _open_sheet(self, name):
        """Open a settings screen inside the page (GPU window); False to use the Tk window."""
        if not (self.web_active and self.web is not None):
            return False
        if name in ('personal','preferences','about'):
            self._open_personal_view('profile' if name=='about' else 'overview')
            return True
        if name == 'voice_settings':
            name = 'agents'
        import web_panels
        import web_panels2
        import web_panels3
        factory = {**web_panels.PANELS, **web_panels2.PANELS, **web_panels3.PANELS}.get(name)
        if factory is None:
            return False
        if self.sheets.is_open(name):
            self.sheets.draw()
            return True
        self.sheets.open(factory(self))
        return True

    # ---- settings as floating panels inside the main window (2026-10-17) -------------------
    _web_handle = None

    def _web_hwnd(self):
        """The GPU window's handle (Windows): the top-level "MYN" window of its process."""
        if os.name != "nt" or self.web is None:
            return None
        process = getattr(self.web, "process", None)
        pid = getattr(process, "pid", None)
        if not pid:
            return None
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        if self._web_handle and user32.IsWindow(self._web_handle):
            return self._web_handle
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def each(hwnd, _param):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid and user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                if buffer.value == "MYN":
                    found.append(hwnd)
            return True

        user32.EnumWindows(each, 0)
        self._web_handle = found[0] if found else None
        return self._web_handle

    def _host_rect(self):
        """Where MYN's main window is on screen (x, y, width, height), or None (hidden or
        minimised: panels then open as ordinary windows)."""
        if self.web_active:
            hwnd = self._web_hwnd()
            if not hwnd:
                return None
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            if user32.IsIconic(hwnd):
                return None
            rect = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return None
            return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
        try:
            if self.root.state() != "normal" and self.root.state() != "zoomed":
                return None
            return (self.root.winfo_rootx(), self.root.winfo_rooty(),
                    self.root.winfo_width(), self.root.winfo_height())
        except tk.TclError:
            return None

    def _host_hwnd(self):
        if self.web_active:
            return self._web_hwnd()
        if os.name == "nt":
            try:
                return theme._hwnd(self.root)
            except Exception:
                return None
        return None

    def _panels_changed(self, count):
        """A settings panel opened or closed: the main window dims behind open panels."""
        if self.web_active and self.web is not None:
            self.web.js({"cmd": "panel", "open": count > 0})

    def _timekeeper_status(self, config):
        from alarm_kind import is_alarm
        try:
            return {"timers": [timekeeper.timer_for_model(item) for item in timekeeper.timers(config)],
                    "scheduled_tasks": [timekeeper.task_for_model(task) for task in timekeeper.tasks(config)
                                        if not is_alarm(task)][:30],
                    "waiting_to_run": [task.get("name") for task in self.task_queue]}
        except Exception as error:
            return {"error": str(error)}

    def _alarm_status(self, config):
        try:
            return json.loads(local_actions._list_alarms({}, local_actions.ActionContext(config))[0])
        except Exception as error:
            return {"error": str(error)}

    def _presence_tick(self):
        """Once a minute: MYN is running, and whether the keyboard / mouse was used (Windows'
        idle time; only the time is kept, never what was typed). For sleep_estimate."""
        if self.session is not None:
            idle = presence.windows_idle_seconds()
            config = config_for_db(self.session.db_path)
            threading.Thread(target=self._note_presence, args=(config, idle), daemon=True).start()
        self.root.after(60000, self._presence_tick)

    @staticmethod
    def _note_presence(config, idle):
        try:
            presence.note(config, idle)
        except (OSError, ValueError):
            pass

    def _check_timekeeper(self):
        """Every second: timers that ran out are said; scheduled tasks whose time came run."""
        if self.session is not None:
            config = config_for_db(self.session.db_path)
            try:
                finished = timekeeper.due_timers(config)
            except (OSError, ValueError, KeyError, TypeError) as error:
                self._alarm_poll_error("timers", error)
                finished = []
            for item in finished:
                text = timekeeper.timer_done_text(item)
                self._deliver_alarm(text, "timer", item["id"])
            try:
                due = timekeeper.due_tasks(config)
            except (OSError, ValueError, KeyError, TypeError) as error:
                self._alarm_poll_error("schedules", error)
                due = []
            for task in due:
                self._queue_task(task)
            self._repeat_ringing()
            self._timers_changed()
        self.root.after(1000, self._check_timekeeper)

    def open_usage_stats(self):
        """Open Expenses from the dock or a natural request, grouped by date/category."""
        if not getattr(self, "_expense_fx_running", False):
            self._expense_fx_running = True
            def refresh_rates():
                try:
                    import expense_rates
                    import expenses
                    expense_rates.quote(self._usage_config(), datetime.now(expenses.TOKYO).date().isoformat())
                except Exception:
                    pass  # USD remains available; other totals explicitly await conversion.
                finally:
                    self._expense_fx_running = False
                    self.events.put(("call", self._usage_updated))
            threading.Thread(target=refresh_rates, daemon=True).start()
        if self._open_sheet("usage"):
            return
        import usage_window
        if self.usage_window is not None and self.usage_window.window.winfo_exists():
            self.usage_window.window.deiconify()
            self.usage_window.window.lift()
            self.usage_window.refresh()
            return
        self.usage_window = usage_window.UsageWindow(self.root, self._usage_config(), self.font_family, expense_mode=True)

    usage_daily_running = False

    claude_code_sync = None  # outcome of counting Claude Code's work on MYN this run (myn_status)
    codex_sync = None

    def _sync_claude_code(self, config):
        import claude_code_usage
        try:
            if Path(config).resolve() != user_data_dir().resolve():
                self.claude_code_sync = "Skipped (not MYN's own data library)"
                return
            result = claude_code_usage.sync(config)
            self.claude_code_sync = 'Counted: %d sessions, %s tokens, approximately $%.2f' % (
                result["sessions"], format(result["tokens"], ","), result["cost_usd"])
        except Exception as error:
            self.claude_code_sync = 'Could not read Claude Code records (%s)' % type(error).__name__
        try:
            import codex_usage
            result = codex_usage.sync(config)
            self.codex_sync = 'Counted: %s tokens; %s' % (format(result["tokens"], ","), codex_usage.cost_text(result))
        except Exception as error:
            self.codex_sync = 'Could not read Codex records (%s)' % type(error).__name__
        self.events.put(("call", self._usage_updated))

    def _daily_usage_check(self):
        """Once a day (and right after midnight while MYN runs), MYN updates the usage
        statistics itself: ElevenLabs usage missing from the records, daily summaries."""
        try:
            self.root.after(30 * 60 * 1000, self._daily_usage_check)
        except tk.TclError:
            return
        config = self._usage_config()
        if self.claude_code_sync is None:  # once per start: Claude Code's work on MYN (its logs)
            self.claude_code_sync = 'In progress'
            threading.Thread(target=self._sync_claude_code, args=(config,), daemon=True).start()
        if self.usage_daily_running or \
                usage_stats.daily_state(config).get("last_run") == datetime.now().date().isoformat():
            return
        self.usage_daily_running = True

        def work():
            try:
                try:
                    key = credentials.get_elevenlabs_key(user_data_dir())
                except Exception:
                    key = None
                usage_stats.daily_update(config, key)
            finally:
                self.usage_daily_running = False
                self.events.put(("call", self._usage_updated))

        threading.Thread(target=work, daemon=True).start()

    def _expense_saved(self):
        """Chat/voice tools run off-thread; refresh open statistics on the GUI thread."""
        self.events.put(("call", self._usage_updated))

    def _usage_updated(self):
        window = self.usage_window
        if window is not None and window.window.winfo_exists():
            window.refresh()
        if getattr(self, "sheets", None) and self.sheets.is_open("usage"):
            self.sheets.draw()

    def _reanalyse_history_once(self):
        """0.20.1: understand pre-0.20 history as scenes, once (marker in the config folder)."""
        marker = Path(user_data_dir()) / "scene-history.json"
        if marker.exists() or self.session is None:
            return
        try:
            queued = scene_memory.requeue_history(self.session.db_path)
            marker.write_text(json.dumps({"requeued_at": datetime.now().astimezone().isoformat(),
                                          "lines": queued}) + "\n", encoding="utf-8")
        except (OSError, sqlite3.Error, ValueError):
            return  # tried again next start
        if queued:
            self.append("MYN", 'Reinterpreting %d earlier records by scene. Ask what I know about you when this finishes.' % queued)

    def reanalyse_history(self, parent=None):
        if self.session is None:
            return
        count = scene_memory.history_needing_scenes(self.session.db_path)
        if not count:
            messagebox.showinfo('About you', 'All earlier records have already been interpreted by scene.', parent=parent)
            return
        if not messagebox.askyesno('About you', 'Reinterpret %d earlier records by scene?\n'
                                   'Existing memories will be kept; only new interpretations will be added. The database will be backed up first.' % count,
                                   parent=parent, default=messagebox.NO):
            return
        try:
            scene_memory.requeue_history(self.session.db_path)
        except (OSError, sqlite3.Error, ValueError) as error:
            messagebox.showerror('About you', str(error), parent=parent)
            return
        self.process_pending()

    def open_about(self):
        self._open_personal_view("profile")


    def open_personal_library(self):
        self._open_personal_view("overview")


    def _reconnect_session(self):
        """A restored database gets fresh history/caches and every current integration callback."""
        previous = self.session
        restored = ChatSession(previous.db_path)
        for name, value in vars(previous).items():
            if callable(value) and not name.startswith("_"):
                setattr(restored, name, value)
        self.session = restored
        panel = getattr(getattr(self, "_sheets", None), "panel", None)
        if panel is not None and hasattr(panel, "store"):
            panel.engine, panel.store = restored.preferences, restored.preferences.store
        view = getattr(self, "preferences_view", None)
        if view is not None:
            view.engine, view.store = restored.preferences, restored.preferences.store

    def _build_library_frame(self, window):
        library_root = root_for_db(self.session.db_path)
        frame = ttk.Frame(window, padding=16)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text=str(library_root)).pack(anchor="w")
        info = ttk.Label(frame, justify=tk.LEFT)
        info.pack(anchor="w", pady=(10, 12))
        ttk.Label(frame, text='Restorable database backups').pack(anchor="w")
        listing = tk.Listbox(frame, height=9)
        listing.pack(fill=tk.BOTH, expand=True, pady=(5, 12))
        backups = []

        def refresh():
            nonlocal backups
            try:
                state = personal_profile.status(self.session.db_path)
                backups = personal_profile.list_backups(self.session.db_path)
            except (OSError, sqlite3.Error) as error:
                messagebox.showerror('Personal data library', str(error), parent=window)
                return
            counts = state["counts"]
            info.configure(text='Database: %.1f KiB    Active memories: %d / %d    Original conversations: %d    Backups: %d\nLatest backup: %s' %
                           (state["size_bytes"] / 1024, state["active_memories"],
                            counts["memories"], counts["conversation_messages"],
                            state["backup_count"],
                            tokyo_display(datetime.fromtimestamp(backups[0].stat().st_mtime,
                                                                  memory_query.TOKYO).isoformat())
                            if backups else 'None yet'))
            listing.delete(0, tk.END)
            for path in backups:
                listing.insert(tk.END, path.name)

        buttons = ttk.Frame(frame)
        buttons.pack(fill=tk.X)
        ttk.Button(buttons, text='Open folder', command=lambda: os.startfile(str(library_root))).pack(
            side=tk.LEFT, padx=(0, 6))

        def create_backup():
            try:
                path = backup.backup_database(self.session.db_path, label="manual")
                self.session.preferences.store.backup("manual")
                refresh()
                messagebox.showinfo('Personal data library', 'Backup created:\n' + str(path), parent=window)
            except (OSError, sqlite3.Error) as error:
                messagebox.showerror('Personal data library', str(error), parent=window)

        def export_data():
            try:
                path = personal_profile.export_profile(self.session.db_path)
                messagebox.showinfo('Personal data library', 'Export completed:\n' + str(path), parent=window)
            except (OSError, sqlite3.Error) as error:
                messagebox.showerror('Personal data library', str(error), parent=window)

        def restore():
            selection = listing.curselection()
            if not selection:
                messagebox.showinfo('Restore backup', 'Choose a backup first.', parent=window)
                return
            if self.busy or self.processing:
                messagebox.showinfo('Restore backup', 'Wait for the current reply and memory processing to finish.', parent=window)
                return
            chosen = backups[selection[0]]
            if not messagebox.askyesno('Restore backup', 'Back up the current database, then restore:\n' +
                                       str(chosen) + '\n\nContinue?', parent=window,
                                       default=messagebox.NO):
                return
            try:
                previous = personal_profile.restore_backup(self.session.db_path, chosen)
                self._reconnect_session()
                refresh()
                messagebox.showinfo('Restore backup', 'Restore completed. The previous database is saved at:\n' + str(previous),
                                    parent=window)
            except (OSError, ValueError, sqlite3.Error) as error:
                messagebox.showerror('Restore backup', str(error), parent=window)

        lower = ttk.Frame(frame)
        lower.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(lower, text='Export readable conversations and memories', command=export_data).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(lower, text='Back up conversations and preferences', command=create_backup).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(lower, text='Restore selected database only', command=restore).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(lower, text='Refresh', command=refresh).pack(side=tk.LEFT)
        refresh()

    def open_agents_settings(self):
        if self._open_sheet("agents"):
            return
        window = tk.Toplevel(self.root)
        window.title('Agents settings')
        window.geometry("560x720")
        window.resizable(False, False)
        body = theme.scroll_body(window, padding=8)
        frame = ttk.Frame(body, padding=14)
        frame.pack(fill=tk.X)
        config_dir = config_for_db(self.session.db_path) if self.session else user_data_dir()
        ttk.Label(frame, text="Opus 5.5").pack(anchor="w")
        ttk.Label(frame, text="Claude API Key").pack(anchor="w")
        ttk.Label(frame, text='Saved keys are never displayed. Enter new keys only here.',
                  foreground=theme.MUTED).pack(anchor="w", pady=(5, 8))
        key_entry = ttk.Entry(frame, show="*")
        key_entry.pack(fill=tk.X)
        key_status = ttk.Label(frame)
        key_status.pack(anchor="w", pady=(8, 0))

        def refresh_status():
            source = credentials.claude_key_source(user_data_dir())
            key_status.configure(text={"environment": 'Using the environment variable key (takes priority).',
                                       "saved": 'A key is stored encrypted on this computer.',
                                       "none": 'No key configured.'}[source])

        def save_key():
            try:
                credentials.save_key(user_data_dir(), key_entry.get())
            except (ValueError, OSError, RuntimeError):
                messagebox.showerror("Claude API Key", 'Could not save the key. Check the input and data-folder permissions.',
                                     parent=window)
                return
            key_entry.delete(0, tk.END)
            refresh_status()
            try:
                message_log.retry_pending_now(self.session.db_path)
                self.process_pending()
            except Exception:
                messagebox.showwarning("Claude API Key",
                                       'Key saved, but pending-message processing could not start. It will retry later.',
                                       parent=window)

        def clear_key():
            if not messagebox.askyesno('Clear Claude API key',
                                       'Clear the locally encrypted Claude key? Environment variables will remain unchanged.',
                                       parent=window, default=messagebox.NO):
                return
            try:
                credentials.clear_key(user_data_dir())
            except OSError:
                messagebox.showerror("Claude API Key", 'Could not clear the local key.', parent=window)
                return
            key_entry.delete(0, tk.END)
            refresh_status()

        actions = ttk.Frame(frame)
        actions.pack(fill=tk.X, pady=(14, 0))
        ttk.Button(actions, text='Save API key', command=save_key).pack(side=tk.LEFT)
        ttk.Button(actions, text='Clear local key', command=clear_key).pack(side=tk.LEFT, padx=(8, 0))
        refresh_status()

        self._voice_controls(body, window)

    def open_voice_settings(self):
        self.open_agents_settings()

    def _voice_controls(self, parent, window):
        frame = ttk.Frame(parent, padding=18)
        frame.pack(fill=tk.X)
        current = self.tts_settings
        import tts_player
        default_output = 'System default output device'
        output_device = tk.StringVar(value=current.output_device or default_output)
        ttk.Label(frame, text='Output device (independent of the microphone)').pack(anchor="w")
        output_row = ttk.Frame(frame)
        output_row.pack(fill=tk.X, pady=(2, 8))
        output_box = ttk.Combobox(output_row, textvariable=output_device, state="readonly")
        output_box.pack(side=tk.LEFT, fill=tk.X, expand=True)
        output_status = ttk.Label(frame, foreground=theme.WARN)
        output_status.pack(anchor="w")
        def refresh_outputs():
            values = [default_output]
            try:
                values += tts_player.output_devices()
                output_status.configure(text="")
            except tts.TTSError as error:
                output_status.configure(text=str(error))
            if output_device.get() not in values:
                values.append(output_device.get())
            output_box.configure(values=values)
        ttk.Button(output_row, text='Refresh devices', command=refresh_outputs).pack(side=tk.LEFT, padx=(8, 0))
        refresh_outputs()
        provider = tk.StringVar(value=current.provider)
        auto_read = tk.BooleanVar(value=current.auto_read)
        opening_only = tk.BooleanVar(value=current.long_reply != "all")
        voice_id = tk.StringVar(value=current.voice_id)
        voice_id_zh = tk.StringVar(value=current.voice_id_zh)
        eleven_model = tk.StringVar(value=tts.ELEVEN_MODEL_LABELS[current.eleven_model])
        local_speaker = tk.StringVar(value=current.local_speaker)
        volume = tk.DoubleVar(value=current.volume * 100)
        speed = tk.DoubleVar(value=current.speed)
        ttk.Label(frame, text='Speech service').pack(anchor="w")
        ttk.Combobox(frame, textvariable=provider, state="readonly",
                     values=("elevenlabs", "qwen3-local")).pack(fill=tk.X, pady=(2, 8))
        ttk.Label(frame, text='Qwen3-TTS 0.6B runs locally and preloads in the background when selected.',
                  foreground=theme.WARN).pack(anchor="w", pady=(0, 8))
        ttk.Label(frame, text='ElevenLabs voice ID (English and default)').pack(anchor="w", pady=(10, 0))
        ttk.Entry(frame, textvariable=voice_id).pack(fill=tk.X)
        ttk.Label(frame, text='Chinese voice ID (optional; leave blank to use the default voice for all languages)').pack(
            anchor="w", pady=(6, 0))
        ttk.Entry(frame, textvariable=voice_id_zh).pack(fill=tk.X)
        model_row = ttk.Frame(frame)
        model_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(model_row, text='ElevenLabs model').pack(side=tk.LEFT)
        ttk.Combobox(model_row, textvariable=eleven_model, state="readonly", width=26,
                     values=[tts.ELEVEN_MODEL_LABELS[code] for code in tts.ELEVEN_MODELS]).pack(
            side=tk.LEFT, padx=(8, 0))
        ttk.Label(frame, text='Qwen3 local voice').pack(anchor="w", pady=(10, 0))
        ttk.Combobox(frame, textvariable=local_speaker, state="readonly",
                     values=tts.LOCAL_SPEAKERS).pack(fill=tk.X)
        ttk.Label(frame, text='ElevenLabs API key (enter only here; saved keys are never displayed)').pack(
            anchor="w", pady=(10, 0))
        key_entry = ttk.Entry(frame, show="*")
        key_entry.pack(fill=tk.X)
        key_status = ttk.Label(frame)
        key_status.pack(anchor="w", pady=(3, 0))

        def refresh_key_status():
            source = credentials.elevenlabs_key_source(user_data_dir())
            label = {"environment": 'Using the environment variable key (takes priority).',
                     "saved": 'A key is stored encrypted on this computer.',
                     "none": 'No key configured.'}[source]
            key_status.configure(text=label)

        def save_key():
            try:
                credentials.save_elevenlabs_key(user_data_dir(), key_entry.get())
            except (ValueError, OSError, RuntimeError) as error:
                messagebox.showerror("ElevenLabs API Key", 'Could not save the key. Check the input and data-folder permissions.',
                                     parent=window)
                return
            key_entry.delete(0, tk.END)
            self.stop_speech()
            refresh_key_status()

        def clear_key():
            if not messagebox.askyesno('Clear ElevenLabs API key',
                                       'Clear the locally encrypted ElevenLabs key? Environment variables will remain unchanged.',
                                       parent=window, default=messagebox.NO):
                return
            try:
                credentials.clear_elevenlabs_key(user_data_dir())
            except OSError:
                messagebox.showerror("ElevenLabs API Key", 'Could not clear the local key.', parent=window)
                return
            key_entry.delete(0, tk.END)
            self.stop_speech()
            refresh_key_status()

        key_actions = ttk.Frame(frame)
        key_actions.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(key_actions, text='Save API key', command=save_key).pack(side=tk.LEFT)
        ttk.Button(key_actions, text='Clear local key', command=clear_key).pack(side=tk.LEFT, padx=(8, 0))
        refresh_key_status()
        ttk.Label(frame, text='Volume (applies immediately)').pack(anchor="w", pady=(10, 0))
        tk.Scale(frame, variable=volume, from_=0, to=100, orient=tk.HORIZONTAL,
                 resolution=1, showvalue=True).pack(fill=tk.X)
        ttk.Label(frame, text='Speed (applies to the next spoken reply)').pack(anchor="w")
        tk.Scale(frame, variable=speed, from_=0.7, to=1.2, orient=tk.HORIZONTAL,
                 resolution=0.05, showvalue=True).pack(fill=tk.X)
        ttk.Separator(frame).pack(fill=tk.X, pady=(10, 6))
        voice_enabled = tk.BooleanVar(value=self.stt_settings.enabled)
        labels = {code: stt.LANGUAGE_LABELS[code] for code in stt.LANGUAGES}
        voice_language = tk.StringVar(value=labels[self.stt_settings.language])
        language_row = ttk.Frame(frame)
        language_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(language_row, text='Recognition language').pack(side=tk.LEFT)
        ttk.Combobox(language_row, textvariable=voice_language, state="readonly", width=12,
                     values=[labels[code] for code in stt.LANGUAGES]).pack(side=tk.LEFT, padx=(8, 0))
        default_mic = 'System default microphone'
        microphone = tk.StringVar(value=self.stt_settings.device or default_mic)
        mic_row = ttk.Frame(frame)
        mic_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(mic_row, text='Microphone').pack(side=tk.LEFT)
        mic_box = ttk.Combobox(mic_row, textvariable=microphone, state="readonly",
                               values=[default_mic] + ([self.stt_settings.device]
                                                       if self.stt_settings.device else []))
        mic_box.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0))
        always_listen = tk.BooleanVar(value=self.stt_settings.always_listen)
        wake_words = tk.StringVar(value=self.stt_settings.wake_words)
        wake_row = ttk.Frame(frame)
        wake_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(wake_row, text='Wake phrase').pack(side=tk.LEFT)
        ttk.Entry(wake_row, textvariable=wake_words, state='readonly').pack(side=tk.LEFT, fill=tk.X, expand=True,
                                                          padx=(8, 0))
        ttk.Label(frame, text='Fixed to Hey, Mine. Voice features save automatically and travel with personal-data backups.',
                  foreground=theme.MUTED).pack(anchor="w", pady=(2, 0))
        ttk.Button(frame, text='Train the wake phrase with my voice...',
                   command=lambda: self.open_wake_training(wake_words.get().strip(), window)).pack(
            anchor="w", pady=(4, 0))
        missing = stt.missing_parts()

        def fill_microphones(names):
            if not mic_box.winfo_exists():
                return
            values = [default_mic] + list(names)
            if self.stt_settings.device and self.stt_settings.device not in names:
                values.append(self.stt_settings.device)  # keep the saved one visible
            mic_box.configure(values=values)

        if not missing:
            # The worker lists devices; the first call may wait for the model to load.
            self.on_voice_devices = fill_microphones
            self._stt_job(stt.client.devices, result_event="voice_devices")
        window.bind("<Destroy>", lambda event: event.widget is window and
                    setattr(self, "on_voice_devices", None))
        ttk.Label(frame, text=('Whisper large-v3-turbo runs on the local GPU. Recordings are processed in memory only.' if not missing
                               else 'Not installed: %s. Run setup_voice_input.ps1.' % "、".join(missing)),
                  foreground=theme.WARN).pack(anchor="w", pady=(4, 0))

        def save():
            try:
                updated = tts.TTSSettings(provider=provider.get(), auto_read=True,
                                          long_reply="opening",
                                          voice_id=voice_id.get().strip(),
                                          voice_id_zh=voice_id_zh.get().strip(),
                                          local_speaker=local_speaker.get(),
                                          volume=volume.get() / 100, speed=round(speed.get(), 2),
                                          output_device="" if output_device.get() == default_output else output_device.get(),
                                          eleven_model={label: code for code, label in
                                                        tts.ELEVEN_MODEL_LABELS.items()}[eleven_model.get()])
                tts.save_settings(user_data_dir(), updated)
                code = {label: code for code, label in labels.items()}[voice_language.get()]
                chosen = microphone.get()
                voice = stt.STTSettings(enabled=True, language=code,
                                        device="" if chosen == default_mic else chosen,
                                        always_listen=True,
                                        wake_words=wake_words.get().strip(),
                                        wake_learned=self.stt_settings.wake_learned)
                stt.save_settings(user_data_dir(), voice)
            except (ValueError, KeyError, tts.TTSError, stt.STTError) as error:
                messagebox.showerror('Sound settings', str(error), parent=window)
                return
            self._apply_voice_settings(updated, voice)
            window.destroy()

        ttk.Button(frame, text='Save other settings', command=save).pack(anchor="e", pady=(12, 0))

    def _apply_voice_settings(self, updated, voice):
        """Use saved voice settings now (shared by the window and the in-page panel)."""
        from dataclasses import replace
        updated = replace(updated, auto_read=True, long_reply="opening")
        voice = replace(voice, enabled=True, always_listen=True)
        previous_provider = self.tts_settings.provider
        if updated.provider != previous_provider or \
                updated.voice_id != self.tts_settings.voice_id or \
                updated.voice_id_zh != self.tts_settings.voice_id_zh or \
                updated.output_device != self.tts_settings.output_device or \
                updated.local_speaker != self.tts_settings.local_speaker:
            self.stop_speech()
        self.tts_player.set_output_device(updated.output_device)
        if previous_provider == "qwen3-local" and updated.provider != "qwen3-local":
            tts.close_local()
        self.tts_settings = updated
        previous_voice, self.stt_settings = self.stt_settings, voice
        if self._wants_listening():
            if not self.always_on_active or (previous_voice.device, previous_voice.language) != \
                    (voice.device, voice.language):
                self._start_listening()
            else:
                self._set_voice_label("")  # wake word text may have changed
        elif self.always_on_active or previous_voice.always_listen:
            self._stop_listening()
        if voice.enabled and not previous_voice.enabled and not stt.missing_parts() and \
                not voice.always_listen:
            self._stt_job(stt.client.warm, quiet=True)
        elif not voice.enabled and previous_voice.enabled:
            self.always_on_active = False
            stt.client.close()
        self.tts_player.set_volume(updated.volume)
        if updated.provider == "qwen3-local" and previous_provider != "qwen3-local":
            self._warm_local()
