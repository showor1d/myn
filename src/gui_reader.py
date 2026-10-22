"""The reader panel for long replies, circled text, and pictures for MYN to look at.

Mixin composed by MYNWindow (myn_gui.py); shares its state and event queue."""

from gui_common import (
    Path,
    _source_label,
    config_for_db,
    filedialog,
    os,
    threading,
    time,
    tk,
    tokyo_display,
    user_data_dir,
    vision,
)


class ReaderMixin:
    # ---- the reader (long replies in a panel; text circled there) -----------------------
    reader_open = False
    reader_selection = None   # (circled text, when)
    reader_key = None
    reader_revision = None
    reader_text = ""
    reader_scene = "other"
    MAX_CIRCLED = 4000
    CIRCLED_FRESH_S = 600     # circled text is used by questions within 10 minutes

    def _reader_context(self):
        if not self.web_active:
            return None  # the classic window has subtitles, not this reading panel
        return {"open": self.reader_open, "key": self.reader_key, "revision": self.reader_revision,
                "scene": self.reader_scene, "text": self.reader_text[:6000]}

    def _apply_reader_scene(self, decision):
        self.reader_scene = decision.get("scene", self.reader_scene)
        if decision.get("close") and self.web_active and self.web is not None:
            self.web.js({"cmd": "reader", "action": "close", "key": decision.get("key"),
                         "revision": decision.get("revision")})

    def _latest_reply_text(self):
        "MYN's latest displayed reply, used by the read-aloud request."
        for link in sorted(self.speech_actions, key=lambda name: int(name.rsplit("_", 1)[-1]), reverse=True):
            if link in self.reply_links:
                return self.reply_links[link][1]
        return ""

    def _take_circled(self):
        """The circled text, once, for the question being sent now (then it's used up)."""
        selection, self.reader_selection = self.reader_selection, None
        if not selection or time.time() - selection[1] > self.CIRCLED_FRESH_S:
            return ""
        if self.web_active:
            self.web.js({"cmd": "selection_used"})
        return selection[0]

    def _usage_config(self):
        return Path(config_for_db(self.session.db_path) if self.session else user_data_dir())

    # ---- pictures for MYN to look at --------------------------------------------------
    attached = ()  # (name, image block) to send with the next message
    MAX_ATTACHED = 4

    def _camera(self):
        """MYN's eyes; its own windows (this process and the GPU window) are not "your window"."""
        pids = [os.getpid()]
        process = getattr(self.web, "process", None) if self.web is not None else None
        if process is not None and getattr(process, "pid", None):
            pids.append(process.pid)
        return vision.WindowsCamera(pids)

    last_image_folder = ""

    def attach_image_dialog(self):
        if self.web_active and self.web is not None:  # choose inside the page, no second window
            import web_panels3
            self.sheets.open(web_panels3.ImagePickPanel(self, self.last_image_folder))
            return
        path = filedialog.askopenfilename(
            parent=self.root, title='Choose images to show MYN',
            filetypes=[('Images', "*.png *.jpg *.jpeg *.gif *.webp *.bmp"), ('All files', "*.*")])
        if path:
            self._attach(lambda camera: vision.from_file(camera, path), Path(path).name)

    def attach_clipboard_image(self):
        self._attach(vision.from_clipboard, 'Clipboard image')

    def _paste_into_entry(self, _event=None):
        try:
            self.root.clipboard_get()
            return None  # text: paste as usual
        except tk.TclError:
            self.attach_clipboard_image()  # no text: maybe a copied picture
            return "break"

    def _attach(self, make, name):
        if len(self.attached) >= self.MAX_ATTACHED:
            self._set_voice_label('Attach at most %d images at a time' % self.MAX_ATTACHED, clear_after=2500)
            return
        self._set_voice_label('Reading images...')
        camera = self._camera()

        def work():
            try:
                block, _info = make(camera)
            except (vision.VisionError, OSError, ValueError) as error:
                message = str(error) or type(error).__name__
                self.events.put(("call", lambda: self._set_voice_label('Could not read the image: %s' % message,
                                                                       clear_after=3500)))
                return
            self.events.put(("call", lambda: self._attached(name, block)))

        threading.Thread(target=work, daemon=True).start()

    def _attached(self, name, block):
        self.attached = list(self.attached) + [(name, block)]
        self._show_attached()
        self.show_input()

    def _show_attached(self):
        count = len(self.attached)
        self.picture_button.configure(text='Images: %d' % count if count else 'Images')
        self._set_voice_label('%d images attached; they will be sent with your message' % count if count else "",
                              clear_after=4000 if count else None)
        if self.web_active:
            self.web.js({"cmd": "attached", "count": count})

    def _confirm(self, preview):
        answer = {"yes": False}
        event = threading.Event()
        self.events.put(("confirm", preview, answer, event))
        event.wait()
        return answer["yes"]

    def _ask(self, question, message_id, heard=False, prepared=None):
        try:
            result = self.session.ask(question, confirm=self._confirm, heard=heard,
                                      consent_local=self._consent_local,
                                      recorded_id=message_id if message_id is not None else False,
                                      on_text=lambda delta: self.events.put(("stream", delta)),
                                      on_tool=lambda name: self.events.put(("web_tool", name)),
                                      voice_preflight=prepared)
            if result.get("silent"):
                self.events.put(("silent", question))
                return
            if result.get("reader_scene") and not result.get("unavailable"):
                decision = result["reader_scene"]
                self.events.put(("call", lambda: self._apply_reader_scene(decision)))
            if result.get("evolution_review"):
                def awaiting_decision():
                    self.reader_scene = "decision"
                    self._voice_attention()
                self.events.put(("call", awaiting_decision))
            if result.get("agenda_scene"):
                agenda_decision = result["agenda_scene"]
                self.events.put(("call", lambda: self._apply_agenda_scene(agenda_decision)))
            text = result["answer"]
            sources = result.get("sources") or []
            if sources:
                text += '\n\nEvidence: ' + ", ".join(
                    ('Original conversation' if row.get("source_type") == "conversation" else 'Long-term memory') +
                    f" {tokyo_display(row['created_at'])}: {row['raw_text'][:140]}"
                    for row in sources)
            web = result.get("web_sources") or []
            if web:  # shown, not read aloud
                text += '\n\nSources: ' + "；".join(_source_label(item) for item in web)
            rateable = result.get("reply_id") if not result.get("unavailable") else None
            self.events.put(("answer", text, result.get("spoken_text", result["answer"]), rateable,
                             result.get("bedtime_briefing")))
            if result.get("persistence_error"):
                self.events.put(("notice", result["persistence_error"]))
        except Exception as error:
            self.events.put(("error", str(error)))

    def _stay_silent(self, question):
        "The model classified the sentence as not addressed to MYN: show and say nothing, then end the spoken conversation so a friend's next sentence is not treated as a reply."
        self._end_draft()
        self.stop_speech()
        try:  # the sentence wasn't for MYN: it leaves the conversation on screen too
            self.transcript.configure(state=tk.NORMAL)
            self.transcript.delete("turn_start", "turn_end")
            self.transcript.configure(state=tk.DISABLED)
            self._queue_chat()
        except tk.TclError:
            pass
        self.speech_stream = None
        self.silence_gate = None
        self.busy = False
        self.reply_silenced = False
        self._voice_turn_active = False
        self._close_voice_conversation('not_addressed')
        now = time.time()
        self.recent_heard = [(when, said) for when, said in self.recent_heard if now - when < 180][-5:] + \
            [(now, question)]
        self.button.configure(state=tk.NORMAL)
        self.status.configure(text='Ready')
        self._set_voice_label('Not addressed to me; archived only', clear_after=2500)
        self.root.after(50, self.process_pending)
        self.scheduled_running = None
        self.root.after(300, self._next_task)
        if self.queued_question:
            self._drain_voice()

    def _confirm_action(self, preview):
        """Asked from the chat thread before an ASK-level action; waits for the answer."""
        answer = {"yes": False}
        event = threading.Event()
        self.events.put(("action_confirm", preview, answer, event))
        event.wait()
        return answer["yes"]
