"""MYN's single-window interface. MYNWindow's larger areas live in the gui_*.py
mixins (split out in 0.46.0); this file keeps start-up, the chat flow, self-repair
and shutdown."""

from gui_common import (  # noqa: F401  (helpers re-exported: web_panels2 looks them up here)
    ChatSession,
    GREETINGS_SPOKEN,
    HolographicCore,
    MP3Player,
    Path,
    _first_paragraph,
    _source_label,
    agenda,
    agent_client,
    backup,
    claude_client,
    config_for_db,
    credentials,
    datetime,
    greeting_for_hour,
    json,
    local_actions,
    local_tools,
    memory_processor,
    os,
    preferred_font_family,
    prepare_data,
    queue,
    scrolledtext,
    self_update,
    sheets,
    storage,
    stt,
    subprocess,
    theme,
    threading,
    time,
    tk,
    tkfont,
    tokyo_display,
    ttk,
    tts,
    usage_stats,
    user_data_dir,
    web_frontend,
    web_tools,
)
import shutil
from gui_conversation import ConversationMixin
from gui_webview import WebViewMixin
from gui_frameless import FramelessMixin
from gui_timers import TimersMixin
from gui_agenda import AgendaMixin
from gui_settings import SettingsMixin
from gui_voice import VoiceMixin
from gui_reader import ReaderMixin
from gui_agent import AgentMixin
from gui_evolution import EvolutionMixin
from gui_reports import ReportsMixin
from gui_personal_data import PersonalDataMixin
from gui_interaction import InteractionMixin
import interaction
import evolution
import app_icon


class MYNWindow(ConversationMixin, WebViewMixin, FramelessMixin, TimersMixin, AgendaMixin, SettingsMixin, VoiceMixin, ReaderMixin, AgentMixin,
                EvolutionMixin, InteractionMixin, ReportsMixin, PersonalDataMixin):
    # Streaming state defaults (also valid for partially built test windows).
    draft_active = False
    speech_stream = None
    speech_queue = None
    listening = False
    voice_token = 0
    stt_jobs = None
    stt_settings = stt.STTSettings()
    on_voice_devices = None  # set while the voice settings window is open
    always_on_active = False  # the worker confirmed always-on listening
    label_token = 0
    awaiting_until = 0.0  # after a bare wake word, the next utterance is for MYN
    awaiting_after = 0.0  # ...if it began after the wake word itself ended
    last_speaking = 0.0  # wall-clock time MYN was last heard through the speakers
    reply_silenced = False  # you talked over the reply being written: don't read the rest aloud
    recent_speech = ()  # (time, sentence) MYN said lately, to tell its echo from your voice
    conversation_open = False  # the wake word opened a spoken conversation
    conversation_touch = 0.0  # wall-clock time of its last exchange

    def __init__(self, root):
        self.root = root
        app_icon.apply_tk(root)
        self.root.title("")
        self.root.configure(bg="#040910")
        self.font_family = preferred_font_family(root)
        self.default_font = tkfont.Font(root=root, family=self.font_family, size=10)
        root.option_add("*Font", self.default_font)
        # Every settings window: the main screen's dark colours and font, frameless with
        # MYN's own title strip (2026-10-17).
        theme.apply(root, self.font_family)
        theme.enable_frameless(self.font_family)
        theme.set_host(self.root, self._host_rect, self._host_hwnd, self._panels_changed)
        self._sheets = sheets.SheetHost(self._sheet_send, lambda ms, fn: self.events.put(("call", fn)))
        self.events = queue.Queue()
        self.pending_confirmation = None
        self.busy = False
        self.processing = False
        self.process_again = False
        self.session = None
        self.tts_settings = tts.TTSSettings()
        self.tts_player = MP3Player()
        self.tts_player.on_start = self._speech_playback_started
        self.tts_request_id = 0
        self.speech_links = 0
        self.reply_links = {}
        self.reply_ratings = {}
        self.draft_active = False
        self.speech_stream = None
        self.speech_queue = None

        outer = tk.Frame(root, bg="#040910")
        outer.pack(fill=tk.BOTH, expand=True)
        # The scene fills the whole window, so the core sits at its centre. The
        # conversation is drawn on the scene itself, like subtitles over space
        # (see _render_chat); the Text widget below is never shown and only holds
        # the conversation, with its tags, marks and speech links.
        self.core_canvas = tk.Canvas(outer, bg="#040910", highlightthickness=0, bd=0)
        self.core_canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self.core_animation = HolographicCore(self.core_canvas)
        self.chat_offset = 0  # how many messages the view is scrolled back
        self.chat_pending = False
        self.speech_actions = {}
        self.chat_hidden_before = 0  # messages before this have faded away
        self.chat_signature = None
        self.chat_epoch = 0
        self.chat_colors = {}
        self.transcript = tk.Text(outer, wrap=tk.WORD, height=7,
                                  state=tk.DISABLED, font=(self.font_family, 11),
                                  bg="#040910", fg="#d1e6eb",
                                  selectbackground="#1c526a",
                                  relief=tk.FLAT, bd=0, highlightthickness=0,
                                  padx=40, pady=4, spacing1=2, spacing3=4)
        self.transcript.tag_configure("myn", justify=tk.CENTER,
                                       foreground="#d1e6eb")
        self.transcript.tag_configure("you", justify=tk.CENTER,
                                       foreground="#8ed9e6")
        self.transcript.tag_configure("error", justify=tk.CENTER,
                                       foreground="#f0aaa8")
        for tag in ("myn", "you", "error"):  # speaker names recede behind the words
            self.transcript.tag_configure(tag + "_name", justify=tk.CENTER, foreground="#5d8796")
        # Keep lines to a comfortable reading column on wide windows.
        self.transcript.bind("<Configure>", lambda event: self.transcript.configure(
            padx=max(40, (event.width - 700) // 2)), add="+")
        row = tk.Frame(outer, bg="#0b1a28", padx=6, pady=6)
        self.entry = tk.Entry(row, font=(self.font_family, 11), bg="#102434",
                              fg="#e4f7f8", insertbackground="#b3edf2",
                              relief=tk.FLAT, bd=0)
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=9, ipadx=10)
        self.entry.bind("<Return>", self.send)
        self.entry.bind("<Escape>", lambda _event: self.hide_input())
        self.button = tk.Button(row, text='Send', width=9, command=self.send,
                                font=(self.font_family, 10), bg="#16677e", fg="#e8fbff",
                                activebackground="#2186a0", activeforeground="#ffffff",
                                relief=tk.FLAT, bd=0, cursor="hand2")
        self.button.pack(side=tk.LEFT, padx=(8, 0))
        # Hold to talk; Right Ctrl does the same from anywhere (see _poll_hotkey).
        self.mic_button = tk.Button(row, text="◉", width=3, font=("Segoe UI Symbol", 12),
                                    bg="#102434", fg="#b3edf2", activebackground="#16677e",
                                    activeforeground="#ffffff", relief=tk.FLAT, bd=0,
                                    cursor="hand2")
        self.mic_button.pack(side=tk.LEFT, padx=(8, 0), before=self.button)
        # A picture to send with the next message (also: paste a copied image with Ctrl+V).
        self.picture_button = tk.Button(row, text='Images', width=5, command=self.attach_image_dialog,
                                        font=(self.font_family, 10), bg="#102434", fg="#b3edf2",
                                        activebackground="#16677e", activeforeground="#ffffff",
                                        relief=tk.FLAT, bd=0, cursor="hand2")
        self.picture_button.pack(side=tk.LEFT, padx=(8, 0), before=self.mic_button)
        self.entry.bind("<Control-v>", self._paste_into_entry, add="+")
        self.mic_button.bind("<ButtonPress-1>", lambda _event: self.voice_press())
        self.mic_button.bind("<ButtonRelease-1>", lambda _event: self.voice_release())
        self.voice_label = None
        self.close_icon = self.core_canvas.create_text(
            0, 24, text="✕", font=("Segoe UI Symbol", 11), fill="#5f7c88", tags="window_control")
        self.minimize_icon = self.core_canvas.create_text(
            0, 24, text="—", font=("Segoe UI Symbol", 11), fill="#5f7c88", tags="window_control")
        self.agents_entry = self.core_canvas.create_text(
            34, 26, text="🤖", font=("Segoe UI Emoji", 20),
            fill="#9cb8c3", tags="agents_icon")
        self.usage_entry = self.core_canvas.create_text(
            34, 72, text="💵", font=("Segoe UI Emoji", 20),
            fill="#9cb8c3", tags="expenses_icon")
        self.input_entry = self.core_canvas.create_text(
            34, 72, text="⌨", font=("Segoe UI Symbol", 18),
            fill="#9cb8c3", tags="keyboard_icon")
        self.voice_label = self.core_canvas.create_text(
            0, 0, text="", font=(self.font_family, 10), fill="#8ed9e6", tags="voice_label")
        self.core_canvas.bind("<Configure>", self._position_scene_controls, add="+")
        self.core_canvas.bind("<Motion>", self._hover_scene_controls, add="+")
        self.core_canvas.bind("<Leave>", self._leave_scene_controls, add="+")
        self.core_canvas.bind("<ButtonPress-1>", self._press_scene, add="+")
        self.core_canvas.bind("<B1-Motion>", self._drag_scene, add="+")
        self.core_canvas.bind("<ButtonRelease-1>", self._release_scene, add="+")
        self.core_canvas.bind("<Double-Button-1>", self._double_click_scene, add="+")
        # F12 shows the animation's real frame rate in the corner.
        self.fps_label = self.core_canvas.create_text(
            16, 16, text="", anchor="nw", font=("Consolas", 9), fill="#4f7584", tags="fps_label")
        self.fps_visible = False
        root.bind_all("<F12>", lambda _event: self.toggle_fps())
        # The GPU window (WebView2) when available; otherwise this classic Tk window.
        self.web = None
        self.web_active = False
        if web_frontend.available():
            try:
                self.web = web_frontend.WebFrontend(self.events)
            except Exception:
                self.web = None
        if self.web is not None:
            root.withdraw()  # stays hidden; dialogs still open from it
            root.after(web_frontend.READY_TIMEOUT_MS, self._web_timeout)
        else:
            self._make_frameless()
        self.status = tk.Label(outer, text='Ready', anchor="w", font=(self.font_family, 9),
                               fg="#79a9b6", bg="#040910")
        # The input bar is hidden until opened from the keyboard icon or by voice.
        self.input_row = row
        self.input_visible = False
        self.core_canvas.bind("<Configure>", lambda _event: self._queue_chat(), add="+")
        self.core_canvas.bind("<MouseWheel>", self._scroll_chat, add="+")
        self.core_canvas.bind("<Button-4>", lambda _event: self._scroll_chat(None, 1), add="+")
        self.core_canvas.bind("<Button-5>", lambda _event: self._scroll_chat(None, -1), add="+")
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(50, self.process_events)
        root.after(60 * 60 * 1000, self.maintenance)
        try:
            db_path, mode = prepare_data()
            self.session = ChatSession(db_path)
            try:
                self.tts_settings = tts.load_settings(user_data_dir())
                self.tts_player.set_output_device(self.tts_settings.output_device)
            except tts.TTSError as error:
                self.append('Error', str(error))
            if self.tts_settings.provider == "qwen3-local":
                self._warm_local()
            try:
                self.stt_settings = stt.load_settings(user_data_dir())
            except stt.STTError as error:
                self.append('Error', str(error))
            import wake_learning
            self.wake_trial = wake_learning.Trial()
            try:
                self.wake_voice = wake_learning.active(user_data_dir())
                self.wake_profile_count = len(wake_learning.load(user_data_dir())['templates'])
            except (OSError, ValueError) as error:
                self.append('Error', str(error))
            self.session.wake_context_provider = self.wake_trial.context
            self.session.wake_feedback_notify = lambda *args: self.events.put(
                ("call", lambda: self._wake_feedback(*args)))
            stt.client.on_event = lambda message: self.events.put(("heard", message))
            if self.stt_settings.enabled and not stt.missing_parts():
                if self.stt_settings.always_listen:
                    self._start_listening()
                else:
                    self._stt_job(stt.client.warm, quiet=True)
            if os.name == "nt":
                root.after(200, self._poll_hotkey)
            root.after(20000, self._listen_watchdog)
            root.after(3000, self._check_reminders)
            root.after(2000, self._check_timekeeper)
            root.after(4000, self._presence_tick)
            self.session.confirm_action = self._confirm_action
            self.session.start_agent = self._start_agent
            self.session.agent_status = self._agent_status
            self.session.start_self_fix = self.evolve_now  # Immediate evolution: a level-one point adopted at creation.
            self.session.evolution_update = self.evolution_update_request
            self.session.microphone_control = self._microphone_control
            self.session.music_control = self._music_control
            self.session.interface_control = self._interface_control
            self.session.status_provider = self._status
            self.session.reader_context_provider = self._reader_context
            self.session.interaction_notify = self._interaction_notify
            self.session.voice_ready_notify = lambda: self.events.put(("voice_ready",))
            self.session.expense_notify = self._expense_saved
            self.session.show_agenda = lambda payload: self.events.put(("call", lambda: self._show_agenda_display(payload)))
            self.session.agenda_context_provider = self._agenda_context
            self.session.stop_agent = lambda: self.events.put(("call", self.stop_agent_task))
            self.session.run_task = self.run_task
            self.greeting = greeting_for_hour(datetime.now().hour)
            self.append("", self.greeting)
            root.after(800, self._greet_aloud)
            self._self_update_startup()
            self._dev_startup()
            self._cleanup_startup()
            self._evolution_boot()
            self._reports_boot()
            self._personal_data_boot()
            self._reanalyse_history_once()
            root.after(5000, self._daily_usage_check)
            root.after(1200, self.process_pending)
            root.after(60 * 1000, self.periodic_processing)
            if mode == "migrated":
                self.append("MYN", 'Legacy memories were preserved and migrated.')
        except Exception as error:
            self.append('Error', str(error))
            self.entry.configure(state=tk.DISABLED)
            self.button.configure(state=tk.DISABLED)
            self.status.configure(text='Cannot open data')

    recent_errors = ()  # (time, text) of the last errors shown, for myn_status
    wake_checks = ()  # (time, voice score, woke) of the last utterances compared with your wake word
    reply_links = {}  # speech link -> (reply id, shown text) of MYN's saved replies
    reply_ratings = {}  # reply id -> -1 / 0 / 1 as rated in this window

    def append(self, speaker, text, speakable=False, spoken_text=None, reply_id=None):
        if speaker == 'Error':
            self.recent_errors = (tuple(self.recent_errors) + ((time.strftime("%H:%M"), str(text)[:200]),))[-6:]
        self.transcript.configure(state=tk.NORMAL)
        tag = "error" if speaker == 'Error' else "you" if speaker == 'You' else "myn"
        prefix = f"{speaker}  " if speaker else ""
        # While a reply is streaming, other notices go above its draft.
        where = "draft_start" if self.draft_active else tk.END
        if prefix:
            self.transcript.insert(where, prefix, tag + "_name")
        self.transcript.insert(where, text, tag)
        if speakable:
            self.speech_links += 1
            link = "speech_link_%d" % self.speech_links
            self.transcript.insert(where, '  [Read aloud]', link)
            self.transcript.tag_configure(link, foreground="#8ed9e6", underline=True)
            self.transcript.tag_bind(link, "<Button-1>",
                                     lambda _event, value=spoken_text or text: self.speak(value))
            self.transcript.tag_bind(link, "<Enter>",
                                     lambda _event: self.transcript.configure(cursor="hand2"))
            self.transcript.tag_bind(link, "<Leave>",
                                     lambda _event: self.transcript.configure(cursor=""))
            self.speech_actions[link] = spoken_text or text
            if reply_id is not None:  # a saved MYN reply: it can be rated
                self.reply_links[link] = (reply_id, text)
        self.transcript.insert(where, "\n\n", (tag, "msg_sep"))  # the end of this message
        self.transcript.configure(state=tk.DISABLED)
        self.chat_offset = 0
        self._queue_chat()

    silence_gate = None  # Hold the start of a reply until it is clear it is not the silence marker.

    def _stream_text(self, delta):
        """Show the reply as it is generated; None starts a fresh model turn."""
        gate = self.silence_gate
        if gate is not None:
            if delta is None:
                gate.reset()
            else:
                delta = gate.feed(delta)
                if not delta:
                    return
        self.transcript.configure(state=tk.NORMAL)
        if delta is None:
            if self.draft_active:
                self.transcript.delete("draft_start", "end-1c")
                self.transcript.mark_unset("draft_start")
                self.draft_active = False
        else:
            self._reply_stage("first_text")
            if not self.draft_active:
                self.transcript.mark_set("draft_start", "end-1c")
                self.transcript.mark_gravity("draft_start", tk.LEFT)
                self.transcript.insert("end-1c", "MYN  ", "myn_name")
                # From now on, text inserted at the mark lands before the draft.
                self.transcript.mark_gravity("draft_start", tk.RIGHT)
                self.draft_active = True
            self.transcript.insert("end-1c", delta, "myn")
            # (No transcript.see(): the transcript is never shown, and scrolling the hidden,
            # nearly zero-width Text laid the reply out one character per line - a third of a
            # second per streamed piece, freezing MYN while a reply came in. 2026-10-17)
        self.transcript.configure(state=tk.DISABLED)
        self.chat_offset = 0
        self._queue_chat()
        if self.tts_settings.auto_read and self.speech_stream is not None:
            self._stream_speak(self.speech_stream.reset() if delta is None
                               else self.speech_stream.feed(delta))

    def _end_draft(self):
        if self.draft_active:
            self._stream_text(None)

    def _stream_speak(self, segments, purpose="reply"):
        """Queue finished sentences for synthesis in order, one worker per reply."""
        if (not segments or (self.reply_silenced and purpose != "alarm") or
                not self._speech_allowed(purpose if purpose != "reply" else
                                         "scheduled" if self.scheduled_running else "reply")):
            return
        now = time.time()
        self.recent_speech = tuple((when, said) for when, said in self.recent_speech if now - when < 90) + \
            tuple((now, segment) for segment in segments)
        if self.speech_queue is None:
            self.stop_speech()
            self.tts_purpose = purpose if purpose != "reply" else "scheduled" if self.scheduled_running else "reply"
            pending = queue.Queue()
            self.speech_queue = pending
            threading.Thread(target=self._speech_worker,
                             args=(pending, self.tts_request_id, self.tts_settings),
                             daemon=True).start()
        for segment in segments:
            self.speech_queue.put(segment)

    def _speech_worker(self, pending, request_id, settings):
        while True:
            segment = pending.get()
            if segment is None or request_id != self.tts_request_id:
                return
            try:
                if settings.provider == "qwen3-local":
                    for audio in tts.Qwen3LocalProvider().synthesize_chunks(segment, settings):
                        self.events.put(("tts_chunk", request_id, audio))
                else:
                    for audio in tts.ElevenLabsProvider().stream(
                            segment, settings, cancelled=lambda: request_id != self.tts_request_id):
                        if request_id != self.tts_request_id:
                            return
                        self.events.put(("tts_chunk", request_id, audio))
            except tts.TTSError as error:
                self.events.put(("tts_error", request_id, str(error)))
                return
            except Exception:
                self.events.put(("tts_error", request_id, 'Speech generation failed; the text reply is preserved.'))
                return

    def _finish_stream_speech(self, then=None):
        """True if this reply was already being spoken sentence by sentence.
        `then` (the bedtime briefing) is read after the reply's last sentence."""
        if self.speech_queue is None:
            return False
        self._stream_speak(self.speech_stream.finish() if self.speech_stream else [])
        if then and self.speech_queue is not None:
            self._stream_speak(tts.speech_segments(tts.speech_text(then)))
        self.speech_queue.put(None)
        self.speech_queue = None
        return True

    def open_memories(self, initial_view='Active memories'):
        # Compatibility for older menu/command events; no memory settings page remains.
        self.append("MYN", 'Ask what you said, did or planned at a particular date or time. You can also request changes or removal of specific records.',
                    speakable=True)

    # ---- fixing itself ----------------------------------------------------------------

    self_fix = None            # the self-fix in progress: {task_id, problem, work, round, ...}
    self_update_state = None   # from self_update.on_startup(): ("trying", summary) on a new version
    SELF_FIX_ROUNDS = 3

    def _self_update_startup(self):
        notice = self_update.take_notice()
        if notice:
            self.append("MYN", notice, speakable=True)
        if self.self_update_state and self.self_update_state[0] == "trying":
            self.root.after(self_update.CONFIRM_AFTER_S * 1000, self._self_update_confirm)

    def _self_update_confirm(self):
        if self.self_update_state and self.self_update_state[0] == "trying":
            summary = self_update.confirm()
            try:
                import reporting
                reporting.journal(self._usage_config(), {'at':reporting.NOW().isoformat(),
                                  'text':summary or 'Self-update passed the startup check'}, applied=True)
            except Exception:
                pass
            self.self_update_state = None
            text = 'The new version is running normally. Self-update is now active.' + (("\n\n" + summary) if summary else "")
            self.append("MYN", text, speakable=True, spoken_text='The new version is running normally. Self-update is now active.')
            self._evolution_receipts()

    def _start_self_fix(self, problem, point_id=None):
        'Self-update on a program copy. point_id identifies the evolution point being built. A tested copy for an adopted point is applied automatically when idle; see gui_evolution.'
        if not agent_client.installed():
            raise local_actions.ActionError(
                'Self-evolution requires background-task components. Run %s once in PowerShell, then restart MYN.' % agent_client.SETUP_SCRIPT)
        if self.self_fix is not None or self.evolution_run is not None or \
                (self.agent_runner is not None and self.agent_runner.busy):
            raise local_actions.ActionError('A task is already running. Wait for it to finish.')
        key = self.session._key()
        if not key:
            raise local_actions.ActionError('Configure a Claude API key first.')
        point = evolution.get(self._evolution_config(), point_id) if point_id else None
        if not (point and point.get("level") == 1) and not evolution.quota(self._evolution_config())["can_start"]:
            raise local_actions.ActionError('The monthly evolution budget is paused. Raise it or continue next month.')
        task_id = "fix%d" % int(time.time() * 1000)
        point = evolution.get(self._evolution_config(), point_id) if point_id else None
        if not evolution.task_budget(self._evolution_config(), point_id)["can_start"]:
            raise local_actions.ActionError('This evolution point reached its per-task limit. The task is preserved; raise the limit to continue.')
        saved_work = point.get("work") if point else None
        resume = bool(saved_work and Path(saved_work).is_dir() and not self_update.live_changes(saved_work))
        if saved_work and not resume:
            self._self_fix_discard({"work": saved_work})
        work = Path(saved_work) if resume else self_update.prepare(task_id)
        fix = {"task_id": task_id, "problem": problem, "work": work, "round": 1, "baseline": None,
               "key": key, "summary": "", "point": point_id,
               "purpose": evolution.purpose("build", point) if point_id else "self_fix",
               "budget_limit": evolution.load_settings(self._evolution_config()).monthly_usd,
               "task_limit": evolution.load_settings(self._evolution_config()).task_usd}
        self.self_fix = fix

        def baseline():  # what already fails before any change (e.g. Windows-only quirks)
            fix["baseline"] = self_update.run_tests(self_update.ROOT)[0]
        fix["baseline_thread"] = threading.Thread(target=baseline, daemon=True)
        fix["baseline_thread"].start()
        try:
            self._self_fix_run('Monthly budget restored. Continue this adopted proposal, preserving edits already made in the copy.' if resume else "")
        except Exception:
            self.self_fix = None
            if not resume:
                self._self_fix_discard(fix)
            raise
        self.events.put(("call", lambda: self._agent_started('Self-evolution: ' + problem.split("\n")[0][:80])))
        return {"started": True, "note": ('The adopted update will be backed up and applied automatically when idle after host verification.'
                                          if point_id else 'After host verification, ask the user whether to apply the update.')}

    def _self_fix_run(self, notes):
        fix = self.self_fix
        if self.agent_runner is None:
            self.agent_runner = agent_client.AgentRunner(lambda message: self.events.put(("agent", message)))
        config = config_for_db(self.session.db_path)
        settings = evolution.runner_settings(config, agent_client.load_settings(config), continuing=True, point_id=fix.get("point"))
        if settings is None:
            self._self_fix_quota_pause()
            return
        agents = (self_update.ROOT / "AGENTS.md").read_text(encoding="utf-8")
        prompt = self_update.prompt_for(fix["problem"], fix["work"], self_update._rules(agents), notes)
        # Commands the agent runs in the copy see a throw-away data folder, never the real one.
        data_dir = Path(str(fix["work"]) + "-data")
        data_dir.mkdir(parents=True, exist_ok=True)
        self.agent_runner.run(fix["task_id"], prompt, fix["key"], fix["work"], [], settings, mode="self_fix",
                              data_dir=data_dir)

    def _self_fix_quota_pause(self):
        fix, self.self_fix = self.self_fix, None
        if not fix:
            return
        point = evolution.get(self._evolution_config(), fix["point"])
        if point is None or point["status"] != "building":
            self._self_fix_discard(fix)
            return
        evolution.update(self._evolution_config(), fix["point"], "Budget paused; copy preserved. Raise the per-task limit or wait for next month's budget",
                         status="adopted", work=str(fix["work"]))
        self._agent_log_add("done", 'Budget paused; copy preserved. Raise the relevant limit or wait for the monthly budget to reset.')
        self._evolution_changed()

    @staticmethod
    def _self_fix_discard(fix, keep_work=False):
        if not keep_work:
            self_update.discard(fix["work"])
        shutil.rmtree(str(fix["work"]) + "-data", ignore_errors=True)

    def _self_fix_fail(self, reason, preserve_work=False):
        fix, self.self_fix = self.self_fix, None
        self._agent_log_add("done", 'No changes: ' + reason)
        self.append("MYN", 'Self-update failed: %s\n\nThe running version is unchanged.' % reason, speakable=True,
                    spoken_text='Self-update failed.' + _first_paragraph(reason, 120))
        if fix:
            if fix.get("point"):
                self._evolution_build_failed(fix, reason)
            self._self_fix_discard(fix, keep_work=preserve_work)

    def _self_fix_round_done(self, message):
        fix = self.self_fix
        if (message.get("error") in ("evolution_quota", "error_max_budget_usd") or fix.get("budget_cancelled")) and fix.get("point"):
            if message.get("error") in ("evolution_quota", "error_max_budget_usd"):
                evolution.pause_budget(self._evolution_config(), fix["point"])
            self._self_fix_quota_pause()
            return
        if not message.get("ok"):
            self._self_fix_fail({"cancelled": 'Stopped'}.get(message.get("error"), message.get("error") or 'An error occurred'))
            return
        fix["summary"] = str(message.get("result") or "").strip()
        fix["cost"] = (fix.get("cost") or 0) + (message.get("cost_usd") or 0)
        self._agent_log_add("text", fix["summary"])
        self._agent_log_add("tool", 'MYN is running tests on the copy...')
        self._set_voice_label('Testing the modified program...', clear_after=15000)

        def test():
            fix["baseline_thread"].join()
            failing, output = self_update.run_tests(fix["work"])
            self.events.put(("call", lambda: self._self_fix_tested(failing, output)))
        threading.Thread(target=test, daemon=True).start()

    def _self_fix_tested(self, failing, output):
        fix = self.self_fix
        if fix is None:
            return
        if fix.get("point"):
            point = evolution.get(self._evolution_config(), fix["point"])
            if point is None or point["status"] != "building":
                self.self_fix = None
                self._self_fix_discard(fix)
                return
        baseline = fix["baseline"] if fix["baseline"] is not None else {'(Tests unavailable)'}
        broken = self_update.new_failures(baseline, failing)
        fix["verification"] = self_update.verification_summary(baseline, failing)
        problems = self_update.check(fix["work"]) if not broken else []
        if broken or problems:
            what = ('New test failures: ' + "、".join(broken[:8])) if broken else "；".join(problems)
            self._agent_log_add("ask", 'Tests failed: ' + what)
            if fix["round"] < self.SELF_FIX_ROUNDS and not any('No changes' in p for p in problems):
                fix["round"] += 1
                try:
                    self._self_fix_run('After the last round: %s\n\nEnd of test output:\n%s' % (what, output[-3000:]))
                except Exception as error:
                    self._self_fix_fail('Could not start the next round: %s' % type(error).__name__)
                return
            self._self_fix_fail(what)
            return
        changed = self_update.diff_summary(fix["work"])
        tests_touched = any(line.startswith("tests/") for line in changed)
        self._agent_log_add("done", 'Tests passed. Changes: ' + "；".join(changed))
        if fix.get("point"):  # Keep the built evolution point until application is authorized; apply adopted plans when idle.
            self._evolution_built(fix, changed)
            return
        text = ('Proposed self-update:\n\n%s\n\nChanged files:\n%s\n\nTests: all passed%s. %s\n'
                'On approval, the current version will be backed up, the new version installed and MYN restarted. Startup failure will trigger automatic rollback.'
                % (_first_paragraph(fix["summary"], 600), "\n".join("· " + line for line in changed),
                   '(excluding %d pre-existing failures)' % len(fix["baseline"]) if fix["baseline"] else "",
                   '\nNote: tests were also changed.' if tests_touched else ""))
        def answered(yes):
            if not yes:
                self._self_fix_fail('You did not approve installing the new version')
                return
            self._set_voice_label('Installing the new version...', clear_after=20000)

            def apply():
                ok, detail = self_update.apply(fix["work"], fix["summary"])
                self.events.put(("call", lambda: self._self_fix_applied(ok, detail)))
            threading.Thread(target=apply, daemon=True).start()

        def offer_update():
            if self.self_fix is not fix:
                return
            if not self._evolution_idle():
                self.root.after(20000, offer_update)
                return
            self.speak('The change is ready and tests passed. Install the new version?', purpose="proactive")
            self._ask_user('MYN self-evolution', text, answered, yes='Install new version')
        offer_update()

    def _self_fix_applied(self, ok, detail):
        if not ok:
            self._self_fix_fail(detail, preserve_work=True)
            return
        fix, self.self_fix = self.self_fix, None
        self._self_fix_discard(fix, keep_work=True)
        self.append("MYN", 'New version installed; restarting now. Previous version backup: %s' % detail)
        self.stop_speech()
        self.root.after(1500, self._restart_now)

    def _restart_now(self):
        _relaunch()
        self.close()

    # ---- told that Claude Code finished working on MYN (2026-10-18) -------------------
    # tools\dev_finish.py (a Claude Code Stop hook) leaves self-update\dev-done.json when a
    # development task changed MYN. A running MYN restarts itself onto the new code as soon
    # When idle, restart normally so the new process can display and speak completion. If MYN was not
    # running, the script starts it and the notice is waiting.
    DEV_DONE = "dev-done.json"
    DEV_NOTICE = "dev-notice.json"
    dev_notice = None  # what to say instead of the greeting at this start

    def _dev_file(self, name):
        return self_update.state_dir(create=False) / name

    def _cleanup_startup(self):
        """Only registrations present at this boot can retire after healthy startup."""
        import development_cleanup
        try:
            batch = development_cleanup.startup_batch()
        except (OSError, ValueError):
            return  # housekeeping must never prevent the main window from opening
        if not batch:
            return
        def run():
            if self.self_fix is not None or (self.agent_runner is not None and self.agent_runner.busy):
                self.root.after(60000, run)
                return
            def work():
                development_cleanup.finish_startup(batch)
            threading.Thread(target=work, daemon=True).start()
        self.root.after((self_update.CONFIRM_AFTER_S + 1) * 1000, run)

    def _dev_startup(self):
        note = None
        for name in (self.DEV_NOTICE, self.DEV_DONE):  # DEV_DONE: written while MYN was closed
            path = self._dev_file(name)
            try:
                found = json.loads(path.read_text(encoding="utf-8"))
                path.unlink()
            except (OSError, ValueError):
                continue
            if isinstance(found, dict) and isinstance(found.get("text"), str):
                note = found
        if note:
            try:
                import reporting
                reporting.journal(self._usage_config(), note, applied=True)
            except Exception:
                pass  # report bookkeeping must not prevent startup
            spoken = str(note.get("spoken") or note["text"])[:300]
            if self._speech_allowed("proactive"):
                self.append("MYN", note["text"][:1500], speakable=True, spoken_text=spoken)
            self.dev_notice = spoken
        self.root.after(2000, self._dev_done_check)

    def _dev_done_check(self):
        try:
            self.root.after(2000, self._dev_done_check)
        except tk.TclError:
            return
        path = self._dev_file(self.DEV_DONE)
        if not path.is_file():
            return
        busy_agent = self.agent_runner is not None and self.agent_runner.busy
        if (self.busy or self.draft_active or self._speaking() or self.self_fix is not None or busy_agent
                or getattr(self, "backup_working", False) or getattr(self, 'listening', False)
                or getattr(self, 'wake_training', None) is not None):
            return  # wait until MYN is idle: never cut off a reply or a running task
        if not self._speech_allowed("proactive"):
            return  # leave the completion notice on disk until awake/free
        try:
            note = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            note = None
        if not isinstance(note, dict) or not isinstance(note.get("text"), str):
            try:
                path.unlink()
            except OSError:
                pass
            return
        if note.get("restart", True):
            try:  # the next start shows and says it
                os.replace(str(path), str(self._dev_file(self.DEV_NOTICE)))
            except OSError:
                return
            self._set_voice_label('Claude finished the update. Restarting...')
            self.stop_speech()
            self.root.after(400, self._restart_now)
            return
        try:
            path.unlink()
        except OSError:
            pass
        spoken = str(note.get("spoken") or note["text"])[:300]
        self.append("MYN", note["text"][:1500], speakable=True, spoken_text=spoken)
        self.speak(spoken, purpose="proactive")

    def _agent_started(self, task):
        self.agent_task = task
        self.agent_log = [("task", task)]
        if self._speech_allowed("proactive"):
            self.open_agent_log()

    def stop_agent_task(self):
        if self.agent_runner is not None and self.agent_runner.busy:
            self.agent_runner.cancel()
            self._set_voice_label('Stopping background task...', clear_after=3000)
        else:
            self._set_voice_label('No background task is running', clear_after=2000)

    def _agent_log_add(self, kind, text):
        if self.agent_log is None:
            self.agent_log = []
        self.agent_log.append((kind, text))
        del self.agent_log[:-400]
        refresh = getattr(self, "_agent_log_refresh", None)
        if refresh is not None:
            refresh()
        if self.sheets.is_open("agent_log"):
            self.sheets.draw()

    def _agent_event(self, message):
        kind = message.get("event")
        if kind == "done":
            self._record_agent_usage(message)
        if self.evolution_run and message.get("task_id") == self.evolution_run.get("task_id"):
            self._evolution_event(message)  # Plan an evolution point.
            return
        if kind == "text":
            self._agent_log_add("text", str(message.get("text", "")))
        elif kind == "tool":
            label = self.AGENT_TOOLS.get(message.get("name"), message.get("name") or 'Tool')
            line = "%s %s" % (label, message.get("summary") or "")
            self._agent_log_add("tool", line)
            self._set_voice_label('Task: ' + line[:40], clear_after=6000)
        elif kind == "ask" and self.self_fix and message.get("task_id") == self.self_fix.get("task_id"):
            # Fixing itself, MYN works only on its copy: anything else is refused without asking.
            self._agent_log_add("ask", '(Self-update rejects operations outside the copy) %s' % (message.get("summary") or ""))
            self.agent_runner.answer(message.get("request_id"), False,
                                     "This operation was rejected by self-update policy. Ordinary Bash and PowerShell test commands inside the copy are supported. " + str(message.get("detail") or ""))
        elif kind == "ask":
            label = self.AGENT_TOOLS.get(message.get("tool"), message.get("tool") or 'Action')
            text = 'The background task requests: %s\n\n%s' % (label, message.get("summary") or "")
            if message.get("detail"):
                text += "\n\n" + str(message["detail"])[:600]
            self._agent_log_add("ask", "%s %s" % (label, message.get("summary") or ""))
            request_id = message.get("request_id")

            def answered(allow):
                self._agent_log_add("answer", 'Allow' if allow else 'Decline')
                if self.agent_runner is not None:
                    self.agent_runner.answer(request_id, allow, "" if allow else "The user declined this action.")

            self._ask_user("MYN's background task needs your approval", text + '\n\nAllow this?', answered, yes='Allow')
        elif kind == "done" and self.self_fix and message.get("task_id") == self.self_fix.get("task_id"):
            self._self_fix_round_done(message)
        elif kind == "done":
            budget_run = getattr(self, "agent_budget_run", None)
            if budget_run and budget_run.get("task_id") == message.get("task_id"):
                config = self._evolution_config()
                point_id = budget_run.get("point_id")
                if message.get("error") == "error_max_budget_usd":
                    evolution.pause_budget(config, point_id)
                point = evolution.get(config, point_id)
                if point and point["status"] == "building":
                    evolution.update(config, point_id, 'Level-one background task completed' if message.get("ok") else 'Level-one background task incomplete',
                                     status="completed" if message.get("ok") else "failed",
                                     result=str(message.get("result") or "")[:8000], reason=str(message.get("error") or ""))
                self.agent_budget_run = None
            result = str(message.get("result") or "").strip()
            cost = message.get("cost_usd")
            note = '(approximately $%.2f)' % cost if isinstance(cost, (int, float)) else ""
            if message.get("ok"):
                self._agent_log_add("done", 'Done' + note)
                text = 'Background task completed.' + (("\n\n" + result) if result else "")
                spoken = 'Background task completed.' + _first_paragraph(result, 220)
            else:
                reason = {"cancelled": 'Stopped', "busy": 'A task is already running',
                          "error_max_budget_usd": 'Spending limit reached',
                          "error_max_turns": 'Step limit reached'}.get(message.get("error"), message.get("error") or 'An error occurred')
                self._agent_log_add("done", 'Not completed: ' + reason + note)
                text = 'Background task did not complete: %s.' % reason + (("\n\n" + result) if result else "")
                spoken = 'Background task did not complete: %s.' % reason
            self._background_notice(text + ("\n\n" + note if note else ""), spoken)
        elif kind == "unavailable":
            self.append('Error', 'Background-task components are unavailable (%s). Run %s once.'
                        % (message.get("error", ""), agent_client.SETUP_SCRIPT))

    def _record_agent_usage(self, message):
        """Background tasks count in the usage statistics too (numbers only)."""
        try:
            config = config_for_db(self.session.db_path) if self.session else Path(user_data_dir())
            fixing = self.self_fix and message.get("task_id") == self.self_fix.get("task_id")
            planning = self.evolution_run and message.get("task_id") == self.evolution_run.get("task_id")
            purpose = "agent_task"
            task_key = "agent:" + str(message.get("task_id") or "")
            background = getattr(self, "agent_budget_run", None)
            if background and background.get("task_id") == message.get("task_id") and background.get("point_id"):
                purpose = evolution.purpose("background", {"level": 1})
                task_key = "evolution:" + background["point_id"]
            if planning or (fixing and self.self_fix.get("point")):
                # All levels count in the monthly dollar budget; retain level labels for history.
                run = self.evolution_run if planning else self.self_fix
                point = evolution.get(config, run.get("point_id" if planning else "point"))
                task_key = "evolution:" + str(run.get("point_id" if planning else "point"))
                purpose = run.get("purpose") or evolution.purpose("plan" if planning else "build", point)
            elif fixing:
                purpose = "self_fix"
            usage_stats.record_agent(Path(config) / usage_stats.USAGE_NAME, message.get("model"),
                                     purpose, message.get("usage"), message.get("cost_usd"), task_key=task_key,
                                     run_id=message.get("run_id", ""), steps=message.get("usage_steps"),
                                     calls_complete=message.get("calls_complete", False))
        except Exception:
            pass

    def open_agent_log(self):
        """A small window with what the background task is doing, and a stop button."""
        if self._open_sheet("agent_log"):
            return
        if self.agent_window is not None and self.agent_window.winfo_exists():
            self.agent_window.lift()
            self._agent_log_refresh()
            return
        window = tk.Toplevel(self.root)
        window.title('Background tasks')
        window.geometry("560x420")
        self.agent_window = window
        frame = ttk.Frame(window, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        log = scrolledtext.ScrolledText(frame, wrap=tk.WORD, font=(self.font_family, 10), state=tk.DISABLED)
        log.pack(fill=tk.BOTH, expand=True)
        buttons = ttk.Frame(frame)
        buttons.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(buttons, text='Stop task', command=self.stop_agent_task).pack(side=tk.LEFT)
        status = ttk.Label(buttons, foreground=theme.MUTED)
        status.pack(side=tk.LEFT, padx=10)
        marks = {"task": 'Task: ', "text": "", "tool": "· ", "ask": '? Approval needed: ', "answer": "  → ",
                 "done": "■ "}

        def refresh():
            if not window.winfo_exists():
                return
            log.configure(state=tk.NORMAL)
            log.delete("1.0", tk.END)
            for kind, text in self.agent_log or [("text", 'No background tasks yet. For example, ask MYN to organize images in Downloads by month.')]:
                log.insert(tk.END, marks.get(kind, "") + text + "\n" + ("\n" if kind in ("task", "text", "done") else ""))
            log.configure(state=tk.DISABLED)
            log.see(tk.END)
            running = self.agent_runner is not None and self.agent_runner.busy
            status.configure(text='In progress...' if running else "")

        self._agent_log_refresh = refresh
        window.bind("<Destroy>", lambda event: event.widget is window and setattr(self, "_agent_log_refresh", None))
        refresh()

    greeting = ""
    greeted = False
    greeting_outcome = 'Not due yet'
    quiet_tts_request = None  # a speech request whose failure is not worth an error message
    _greet_waits = 0
    _greeting_request = None
    _greeting_audio_started = False
    _greeting_retries = 0
    _greeting_text = ""
    _greeting_purpose = "startup"

    def _greeting_state(self, text, reason):
        """Keep startup diagnostics without recording speech or service response bodies."""
        self.greeting_outcome = text
        import voice_diagnostics
        voice_diagnostics.record(self._usage_config(), 'startup_greeting', reason=reason)

    def _speech_playback_started(self):
        self._reply_stage("first_audio")
        if self._greeting_request == self.tts_request_id and not self._greeting_audio_started:
            self._greeting_audio_started = True
            self._greeting_state('Audio playback started', 'playback_started')

    def _start_greeting_audio(self, text, purpose="startup"):
        self._greeting_text = text
        self._greeting_purpose = purpose
        self._greeting_audio_started = False
        self.speak(text, purpose=purpose)
        self._greeting_request = self.quiet_tts_request = self.tts_request_id
        self._greeting_state('Waiting for speech audio', 'requested')

    def _greeting_failed(self, request_id, error):
        if request_id != self._greeting_request or request_id != self.tts_request_id:
            return False
        self._greeting_state('Read-aloud failed: ' + str(error)[:160], 'audio_failed')
        transient = any(reason in str(error) for reason in ('network request failed', 'temporarily unavailable'))
        if transient and not self._greeting_audio_started and self._greeting_retries < 1:
            self._greeting_retries += 1
            self._greeting_state('Retrying startup speech once', 'retry_scheduled')
            def retry():
                if (self.tts_request_id != request_id or self.busy or self._speaking() or
                        self.reply_silenced or not self.tts_settings.auto_read or
                        not self._speech_allowed(self._greeting_purpose) or
                        any(tag == 'you' for tag, _body, _link in self._messages())):
                    self._greeting_state('Retry skipped: activity or quiet scene changed', 'retry_cancelled')
                    return
                self._start_greeting_audio(self._greeting_text, self._greeting_purpose)
            self.root.after(1500, retry)
        else:
            self.append('Error', 'Startup voice could not play. ' + str(error)[:160])
        return True

    def _greet_aloud(self):
        """Say the start-up greeting in English once it can be seen (after the GPU window's
        intro, if that window is coming up)."""
        if self.greeted or not self.greeting:
            return
        if (self.web_intro or (self.web is not None and not self.web_active)) and self._greet_waits < 50:
            self._greet_waits += 1
            self.root.after(500, self._greet_aloud)  # wait for the window (intro_done also calls this)
            return
        self.greeted = True
        if not self._speech_allowed("startup"):
            self._greeting_state('Not spoken: quiet scene or media playback', 'scene_suppressed')
            self.dev_notice = None
            return
        if self.dev_notice:  # restarted after Claude Code changed MYN: say that instead
            notice, self.dev_notice = self.dev_notice, None
            if self._speech_allowed("proactive"):
                self._start_greeting_audio(notice, purpose="proactive")
                return
        # Only as the opening line: not once you have sent something or MYN is busy talking.
        reason = ('Automatic read-aloud is disabled' if not self.tts_settings.auto_read else
                  'Already speaking or replying' if self._speaking() or self.busy else
                  'Already spoke something else' if self.recent_speech else
                  'You already started a conversation' if any(tag == "you" for tag, _body, _link in self._messages()) else "")
        if reason:
            self._greeting_state('Not spoken: ' + reason, 'opening_superseded')
            return
        self._start_greeting_audio(GREETINGS_SPOKEN.get(self.greeting, self.greeting))

    def _status(self):
        """MYN's own state for the myn_status tool (called from the chat thread; reads only)."""
        import version
        config = config_for_db(self.session.db_path)
        try:
            grants = local_tools.grants(config)
        except (OSError, ValueError):
            grants = {"read": [], "write": []}
        actions = local_actions.load_settings(config)
        web = web_tools.load_settings(config)
        runner = self.agent_runner
        tts_settings, stt_settings = self.tts_settings, self.stt_settings
        try:
            eleven_key = credentials.elevenlabs_key_source(user_data_dir()) != "none"
        except Exception:
            eleven_key = None
        return {
            "version": version.VERSION,
            "main_model": claude_client.main_model(config),
            # chat (quick) or work (deep thinking) for the latest message, and why
            "last_turn_kind": dict(zip(("kind", "reason"), getattr(self.session, "last_turn_kind", ("", "")))),
            "usage_daily_update": usage_stats.daily_state(config),
            "reports": {"preparing": self._report_working,
                        "last_error": getattr(self, "report_error", "")},
            "self_evolution": self._evolution_status(),  # Evolution points, monthly budget and running work.
            "claude_code_usage": self.claude_code_sync,  # Claude Code's work on MYN, counted at start
            "codex_usage": self.codex_sync,
            "timers": self._timekeeper_status(config),
            "alarms": self._alarm_status(config),
            "sleep_estimate": self._sleep_status(),
            "interaction": interaction.load(config),
            "reader": {"open": self.reader_open, "circled_text_waiting": bool(self.reader_selection)},
            "window": 'WebView2 window' if self.web_active else 'Classic window',
            "voice_output": {"service": tts_settings.provider, "auto_read": tts_settings.auto_read,
                             "selected_device": tts_settings.output_device or 'System default output device',
                             "volume_percent": round(tts_settings.volume * 100),
                             "english_voice_set": bool(tts_settings.voice_id),
                             "chinese_voice_set": bool(tts_settings.voice_id_zh),
                             "elevenlabs_key_saved": eleven_key, "speaking_now": self._speaking(),
                             "startup_greeting": self.greeting_outcome,
                             "last_interruption": self.last_barge,
                             "instant_stop_when_you_speak": self._quick_barge()},
            "voice_input": {"enabled": stt_settings.enabled, "always_listen": stt_settings.always_listen,
                            "capture": self._microphone_status(),
                            "selected_device": stt_settings.device or 'System default microphone',
                            "device_note": 'This is the saved selection, not proof of actual use. Call microphone_control inspect to verify the active device and available devices.',
                            "listening_now": self.always_on_active, "wake_words": stt_settings.wake_words,
                            "recent_wake_checks": [{"at": at, "voice_score": score, "woke": woke}
                                                   for at, score, woke in self.wake_checks],
                            "wake_check_note": "voice_score below 1 wakes by voice; the spelling still wakes "
                                               "when it starts with the wake word or a sound-alike",
                            "voice_wake_samples": len(self.wake_voice.get("templates", [])),
                            "voice_wake_saved_samples": getattr(self, 'wake_profile_count', 0),
                            "wake_phrase_fixed": True,
                            "wake_learning": 'Learn only after a confirmed conversation and completed reply; do not learn from false wakes, echoes or uncertain speech. Features travel with personal backups',
                            "streaming_wake": getattr(self, 'streaming_wake_status', {'active': None}),
                            "recording": self._recording_status(),
                            "conversation_open": self.conversation_open},
            "background_task": {"installed": agent_client.installed(),
                                "running": bool(runner and runner.busy),
                                "task": self.agent_task[:300] if runner and runner.busy else "",
                                "latest_steps": [text[:160] for _kind, text in (self.agent_log or [])[-6:]]},
            "self_fix": ({"running": True, "problem": self.self_fix["problem"][:300],
                          "round": self.self_fix["round"]} if self.self_fix else {"running": False}),
            "web_search": {"service": web.provider, "monthly_limit": web.monthly_limit,
                           "used_this_month": web_tools.searches_this_month(config / "claude-usage.jsonl")},
            "computer_actions_allowed": [local_actions.GROUPS[g] for g in local_actions.GROUPS if getattr(actions, g)],
            "confirm_policy": actions.confirm,
            "read_folders": grants.get("read", []), "write_folders": grants.get("write", []),
            "recent_errors": [{"time": when, "text": text} for when, text in self.recent_errors],
        }

    def _check_reminders(self):
        """Say and show reminders that are due (set by MYN's set_reminder action)."""
        if self.session is not None:
            try:
                due = local_actions.due_reminders(config_for_db(self.session.db_path))
            except (OSError, ValueError, KeyError, local_actions.ActionError) as error:
                self._alarm_poll_error("reminder", error)
                due = []
            for item in due:
                from alarm_kind import is_alarm
                text = 'Reminder: ' + item["text"]
                self._deliver_alarm(text, "alarm" if is_alarm(item) else "reminder",
                                    "reminder:" + item["id"] if is_alarm(item) else item["id"])
            try:
                coming = agenda.due_reminders(config_for_db(self.session.db_path))
            except (OSError, ValueError, KeyError):
                coming = []
            for event in coming:  # calendar reminders, each occurrence once
                text = agenda.reminder_text(event)
                self._deliver_alarm(text, "agenda", event.get("id", ""))
        self.root.after(10000, self._check_reminders)

    def _consent_local(self, prompt):
        answer = {"yes": False}
        event = threading.Event()
        self.events.put(("local_consent", prompt, answer, event))
        event.wait()
        return answer["yes"]

    def process_events(self):
        try:
            while True:
                item = self.events.get_nowait()
                if item[0] in ("confirm", "local_consent", "action_confirm"):
                    _, preview, answer, event = item
                    self.pending_confirmation = event
                    title, text, yes = {
                        "action_confirm": ('MYN needs your approval', preview + '\n\nAllow this?', 'Allow'),
                        "confirm": ('Confirm changes', 'Preview (not yet applied):\n\n' + preview + '\n\nApply these changes?', 'Confirm'),
                        "local_consent": ('Local fallback model', preview, 'Allow')}[item[0]]

                    def answered(value, answer=answer, event=event):
                        answer["yes"] = bool(value)
                        event.set()
                        self.pending_confirmation = None

                    self._ask_user(title, text, answered, yes=yes)
                elif item[0] == "web_tool":
                    label = {"web_search": 'Searching the web...', "web_fetch": 'Reading webpage...',
                             "deep_thinking": 'Thinking this through...'}.get(item[1])
                    if label:
                        self._set_voice_label(label, clear_after=8000)
                elif item[0] == "agent":
                    self._agent_event(item[1])
                elif item[0] == "call":
                    item[1]()  # a background task's follow-up, run on the GUI thread
                elif item[0] == "stream":
                    self._stream_text(item[1])
                elif item[0] == "voice_listening":
                    if self.listening:
                        fallback = isinstance(item[1], dict) and item[1].get("fallback")
                        self._set_voice_label('Listening... Release to finish' + (
                            '(Selected microphone unavailable; using the system default)' if fallback else ""))
                elif item[0] == "voice_devices":
                    if self.on_voice_devices is not None:
                        self.on_voice_devices(item[1])
                elif item[0] == "voice_text":
                    self._voice_text(item[1])
                elif item[0] == "voice_enroll":
                    self._voice_enroll(item[1])
                elif item[0] == "voice_error":
                    self.listening = False
                    self._set_voice_label("")
                    self.append('Error', item[1])
                elif item[0] == "web":
                    self._web_event(item[1])
                elif item[0] == "judged":
                    self._judged(item[1], item[2], item[3] if len(item) > 3 else None,
                                 item[4] if len(item) > 4 else None)
                elif item[0] == "voice_ready":
                    if self.silence_gate is not None and self._speech_allowed():
                        self.silence_gate.voice = False
                elif item[0] == "heard":
                    self._heard(item[1])
                elif item[0] == "listen_started":
                    self.always_on_active = True
                    fallback = isinstance(item[1], dict) and item[1].get("fallback")
                    self._set_voice_label('Selected microphone unavailable; using the system default' if fallback else "",
                                          clear_after=3000 if fallback else None)
                elif item[0] == "notice":
                    self.append('Maintenance', item[1])
                elif item[0] == "processing_done":
                    self.processing = False
                    result = item[1]
                    # Understood quietly; unfinished scenes stay queued and are retried.
                    if self.process_again:
                        self.process_again = False
                        self.root.after(50, self.process_pending)
                    elif result.get("scenes") == 3 and result["failed"] == 0:
                        self.root.after(1200, self.process_pending)
                elif item[0] == "tts_audio":
                    if item[1] == self.tts_request_id and self._speech_allowed(self.tts_purpose):
                        try:
                            self.tts_player.play(item[2], self.tts_settings.volume)
                            self._alarm_audio_result(item[1], "tts_submitted", 'Audio was handed to the player; this does not prove the user heard it')
                        except tts.TTSError as error:
                            self._alarm_audio_result(item[1], "tts_failed", str(error))
                            if not self._greeting_failed(item[1], error):
                                self.append('Error', str(error))
                elif item[0] == "tts_chunk":
                    if item[1] == self.tts_request_id and self._speech_allowed(self.tts_purpose):
                        try:
                            self.tts_player.enqueue(item[2], self.tts_settings.volume)
                            self._alarm_audio_result(item[1], "tts_submitted", 'Audio was handed to the player; this does not prove the user heard it')
                        except tts.TTSError as error:
                            self._alarm_audio_result(item[1], "tts_failed", str(error))
                            if not self._greeting_failed(item[1], error):
                                self.append('Error', str(error))
                elif item[0] == "tts_warm_error":
                    if (self.tts_settings.provider == "qwen3-local" and
                            item[1] == tts.local_token()):
                        self.append('Error', item[2])
                elif item[0] == "tts_error":
                    self._alarm_audio_result(item[1], "tts_failed", item[2])
                    handled = self._greeting_failed(item[1], item[2])
                    if not handled and item[1] == self.tts_request_id and item[1] != self.quiet_tts_request:
                        self.append('Error', item[2])
                elif item[0] == "silent":
                    self._stay_silent(item[1])
                else:
                    self._end_draft()
                    if item[0] == "answer":
                        self._reply_stage("first_text")
                    bedtime_briefing = item[4] if item[0] == "answer" and len(item) > 4 and not self.reply_silenced else None
                    bedtime_text = self._claim_bedtime_briefing(bedtime_briefing) if bedtime_briefing else None
                    reply_id = item[3] if item[0] == "answer" and len(item) > 3 else None
                    if item[0] == "answer" and self.reply_silenced:
                        self.stop_speech()  # you interrupted it: shown, not read aloud
                        self.append("MYN", item[1], speakable=True, spoken_text=item[2], reply_id=reply_id)
                    elif item[0] == "answer":
                        if self._finish_stream_speech(bedtime_text):
                            self.append("MYN", item[1], speakable=True, spoken_text=item[2], reply_id=reply_id)
                        else:
                            self.stop_speech()
                            self.append("MYN", item[1], speakable=True, spoken_text=item[2], reply_id=reply_id)
                            if self.tts_settings.auto_read:
                                self.speak(item[2], opening_only=True,
                                           purpose="scheduled" if self.scheduled_running else "reply",
                                           then=bedtime_text)
                    else:
                        self._finish_stream_speech()
                        self.append('Error', item[1])
                    self.speech_stream = None
                    self.silence_gate = None
                    self.busy = False
                    self.last_answer_at = time.time()
                    if getattr(self, '_voice_turn_active', False) and self.conversation_open:
                        self.conversation_touch = self.last_answer_at
                    self._voice_turn_active = False
                    self.reply_silenced = False
                    self._finish_interface_speech()
                    self.button.configure(state=tk.NORMAL)
                    self.status.configure(text='Ready')
                    self.entry.focus_set()
                    self.root.after(50, self.process_pending)
                    self.scheduled_running = None
                    self.root.after(300, self._next_task)
                    self._timers_changed()
                    self._agenda_changed()
                    if self.queued_question:
                        self._drain_voice()
        except queue.Empty:
            pass
        try:
            self.tts_player.poll()
        except tts.TTSError as error:
            self.append('Error', str(error))
        if self._speaking():
            self.last_speaking = time.time()
        self.root.after(50, self.process_events)

    def _recording_status(self):
        try:
            import speech_journal
            return speech_journal.status(self.session.db_path)
        except Exception:
            return {'error': 'Cannot read record status'}

    def _idle_work_allowed(self):
        """Pure state check, also safe at background model-call boundaries."""
        return not (self.busy or self.listening or getattr(self, 'backup_working', False)
                    or getattr(self, '_judge_running', False)
                    or getattr(self, 'pending_confirmation', None)) and time.time() - max(
                        getattr(self, '_last_voice_activity', 0), self.last_speaking,
                        getattr(self, 'conversation_touch', 0)) >= 30

    def process_pending(self):
        if not self._idle_work_allowed() or self._speaking():
            return
        if getattr(self, "backup_working", False):
            return
        if self.processing:
            self.process_again = True
            return
        if self.session is None:
            return
        self.processing = True
        def work():
            try:
                result = memory_processor.process_batch(self.session.db_path, limit=3,
                                                        should_continue=self._idle_work_allowed)
            except Exception:
                result = {"processed": 0, "created": 0, "failed": 1}
            try:  # MYN thinks over new feedback on how it talks (queued while offline)
                if self._idle_work_allowed():
                    self.session.reflect_preferences(limit=1, should_continue=self._idle_work_allowed)
            except Exception:
                pass
            self.events.put(("processing_done", result))
        threading.Thread(target=work, daemon=True).start()

    def periodic_processing(self):
        self.process_pending()
        self.root.after(60 * 1000, self.periodic_processing)

    def close(self):
        if self.session is not None and self.always_on_active:
            try:
                import speech_journal
                speech_journal.gap(self.session.db_path, {'reason': 'session_stopped', 'end': time.time()})
            except Exception:
                pass  # shutdown still releases audio devices even if the disk is unavailable
        self._cancel_agenda_auto_close()
        if self.pending_confirmation:
            self.pending_confirmation.set()
        if self.agent_runner is not None:
            self.agent_runner.close()
        if self.self_update_state and self.self_update_state[0] == "trying":
            self_update.confirm()  # closed normally: the new version works
            self.self_update_state = None
        if self.stt_jobs is not None:
            self.stt_jobs.put(None)
        stt.client.close()
        self.stop_speech()
        tts.close_local()
        self.tts_player.close()
        self.core_animation.stop()
        self.closing = True
        if self.web is not None:
            self.web.close()
        self.root.destroy()

    def maintenance(self):
        if self.session is not None and not getattr(self, "backup_working", False):
            def work():
                try:
                    backup.ensure_daily(self.session.db_path)
                    storage.purge_expired_trash(self.session.db_path)
                except Exception:
                    self.events.put(("notice", 'Automatic backup or trash maintenance failed. Check storage permissions.'))
                try:
                    self.session.preferences.store.ensure_daily_backup()
                except Exception:
                    self.events.put(("notice", 'Automatic preference backup failed. Chat is unaffected; ask to back up conversations and preferences.'))
            threading.Thread(target=work, daemon=True).start()
        self.root.after(60 * 60 * 1000, self.maintenance)

def _fine_timers(enable):
    """Windows timers tick every ~15.6 ms by default, which caps Tk animation near
    64 fps; ask for 1 ms resolution while MYN runs (and give it back on exit)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        winmm = ctypes.windll.winmm
        (winmm.timeBeginPeriod if enable else winmm.timeEndPeriod)(1)
    except Exception:
        pass


def _relaunch():
    """Start MYN again the same way (after a self-update or a rollback)."""
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    subprocess.Popen(self_update.restart_command(), cwd=str(Path.cwd()), creationflags=flags, close_fds=True)


def main():
    app_icon.identify()
    import portable_backup
    from data_location import personal_root
    portable_backup.recover(personal_root())
    from first_run import offer
    if not offer(personal_root()):
        return
    # A self-update whose new version never came up properly is undone before anything else.
    startup = self_update.on_startup()
    if startup and startup[0] == "rolled_back":
        _relaunch()
        return
    MYNWindow.self_update_state = startup
    _fine_timers(True)
    try:
        root = tk.Tk()
        win = MYNWindow(root)
        result = portable_backup.transfer_dir(personal_root()) / "result.json"
        if result.exists():
            try:
                notice = json.loads(result.read_text(encoding="utf-8"))["text"]
                result.unlink()
                root.after(2500, lambda: win.append("MYN", notice, speakable=False))
            except (OSError, ValueError, KeyError):
                pass
        root.mainloop()
    finally:
        _fine_timers(False)


if __name__ == "__main__":
    main()
