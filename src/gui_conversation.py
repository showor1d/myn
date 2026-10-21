"""The conversation drawn on the scene: subtitles, fading, scrolling back.

Mixin composed by MYNWindow (myn_gui.py); shares its state and event queue."""

from gui_common import (  # noqa: F401
    preferences_window,
    time,
    tk,
    tkfont,
)


class ConversationMixin:
    # ---- the conversation, drawn on the scene ---------------------------------

    CHAT_TOP_GAP = 168  # px below the window's centre: under the core's glow

    def _messages(self):
        """The conversation as (tag, text, speech link) per message, oldest first.

        Messages end at the separator append() tags "msg_sep" (2026-10-17: splitting at
        blank lines cut a reply with paragraphs into pieces, so a long reply never reached
        the reading panel as a whole)."""
        messages = []
        start = "1.0"
        bounds = self.transcript.tag_ranges("msg_sep")
        for index in range(0, len(bounds), 2):
            end, after = bounds[index], bounds[index + 1]
            if self.transcript.get(start, end).strip():
                messages.append(self._message(start, end))
            start = str(after)
        if self.transcript.get(start, "end-1c").strip():  # a reply that is still streaming
            messages.append(self._message(start, "end-1c"))
        return messages

    def _subtitles(self):
        """What the subtitles show: only MYN's words (and errors); what you said is
        not shown as text (user, 2026-10-17). The full conversation stays in the transcript."""
        return [message for message in self._messages() if message[0] != "you"]

    def _message(self, start, end):
        speaker_tag, body, link = None, [], None
        for kind, value, index in self.transcript.dump(start, end, text=True, tag=True):
            if kind != "text":
                continue
            tags = self.transcript.tag_names(index)
            links = [tag for tag in tags if tag.startswith("speech_link_")]
            if links:
                link = links[0]
                continue
            named = [tag for tag in tags if tag.endswith("_name")]
            if named:
                speaker_tag = speaker_tag or named[0][:-5]
                continue
            if speaker_tag is None:
                speaker_tag = next((tag for tag in tags if tag in ("you", "myn", "error")), None)
            body.append(value)
        return speaker_tag or "myn", "".join(body).strip(), link

    def _queue_chat(self):
        if not self.chat_pending:
            self.chat_pending = True
            self.root.after_idle(self._render_chat)

    def _scroll_chat(self, event, step=None):
        if step is None:
            step = 1 if event.delta > 0 else -1
        total = len(self._subtitles())
        if step > 0 and self.chat_offset == 0 and self.chat_hidden_before >= total:
            # Everything has faded: the first step up brings back the last exchange,
            # which fades again by itself.
            self.chat_hidden_before = 0
            self.chat_signature = None
        else:
            self.chat_offset = max(0, min(total - 1, self.chat_offset + step))
            if self.chat_offset == 0:
                self.chat_signature = None  # back at the newest: count down again
        self._queue_chat()

    def _render_chat(self):
        """Show only the current exchange under the core, like subtitles.

        Only MYN's latest words are shown (what you said is not shown as text,
        2026-10-17); a long reply shows its end. Once
        the reply is complete and no longer being read aloud, everything fades
        away after a few seconds (see _expire_chat). Scrolling back shows older
        exchanges and pauses the fading.
        """
        self.chat_pending = False
        canvas = self.core_canvas
        canvas.delete("chat")
        self.chat_colors = {}
        width = max(canvas.winfo_width(), 1)
        height = max(canvas.winfo_height(), 1)
        top = height / 2 + self.CHAT_TOP_GAP
        bottom = height - 22
        if self.input_visible:  # the bar floats at the bottom
            bottom -= self.input_row.winfo_reqheight() + 16
        column = min(640, width - 120)
        x = width / 2
        messages = self._subtitles()
        end = len(messages) - self.chat_offset
        start = 0 if self.chat_offset else min(self.chat_hidden_before, len(messages))
        shown = messages[max(start, end - 1):end]  # one line of subtitles: MYN's latest words
        everything = self._messages()
        if not self.chat_offset and everything and everything[-1][0] == "you":
            shown = []  # you just spoke: the previous reply makes way, nothing new to show yet
        # A new message (or a growing reply) restarts the countdown to fade out.
        signature = (len(messages), len(shown[-1][1]) if shown else 0, self.draft_active)
        if signature != self.chat_signature:
            self.chat_signature = signature
            self.chat_epoch += 1
            if shown and not self.chat_offset:
                self.root.after(self._chat_hold_ms(shown[-1][1]), self._expire_chat, self.chat_epoch)
        if self.web_active:
            self._send_chat(shown, end)
            return
        if not shown:
            return
        colors = {"you": "#8ed9e6", "myn": "#d6e9ee", "error": "#f0aaa8"}
        names = {"you": 'You', "error": 'Error'}

        def text_item(y, text, font, fill):
            shadow = canvas.create_text(x + 1, y + 1, text=text, width=column, justify=tk.CENTER,
                                        anchor="s", font=font, fill="#02060b", tags=("chat",))
            body = canvas.create_text(x, y, text=text, width=column, justify=tk.CENTER,
                                      anchor="s", font=font, fill=fill, tags=("chat",))
            self.chat_colors[shadow] = "#02060b"
            self.chat_colors[body] = fill
            return shadow, body

        # The newest message, full size; if it is too long for the space, its end.
        tag, text, link = shown[-1]
        rated = self.reply_links.get(link) if link is not None and not self.draft_active else None
        if rated is not None:
            bottom -= 18  # room for the small feedback row under the reply
        room = bottom - top - 20
        font = (self.font_family, 11)
        fill = colors.get(tag, "#d6e9ee")
        shadow, body = text_item(bottom, text, font, fill)
        box = canvas.bbox(body)
        cut = len(text)
        while cut > 20 and box[3] - box[1] > room:
            cut = int(cut * .8)
            for item in (shadow, body):
                canvas.itemconfigure(item, text="…" + text[-cut:])
            box = canvas.bbox(body)
        if link is not None:  # clicking the reply reads it aloud
            value = self.speech_actions.get(link)
            canvas.tag_bind(body, "<Button-1>", lambda _event, v=value: v and self.speak(v))
            canvas.tag_bind(body, "<Enter>", lambda _event: canvas.configure(cursor="hand2"))
            canvas.tag_bind(body, "<Leave>", lambda _event: canvas.configure(cursor=""))
        if rated is not None:
            self._draw_feedback(x, box[3] + 3, rated[0])
        y = box[1]
        if tag in names:
            name = canvas.create_text(x, y - 4, text=names[tag], anchor="s",
                                      font=(self.font_family, 9), fill="#5d8796", tags=("chat",))
            self.chat_colors[name] = "#5d8796"
            y = canvas.bbox(name)[1]
        # The message before it shrinks above it; a long one is simply left out.
        if len(shown) == 2:
            tag, text, _link = shown[0]
            small = (self.font_family, 9)
            items = text_item(y - 10, text, small, "#6f8f9b" if tag != "you" else "#5f9aa6")
            box = canvas.bbox(items[1])
            if box[3] - box[1] > 34 or box[1] < top:  # more than two lines, or no room
                for item in items:
                    canvas.delete(item)
                    self.chat_colors.pop(item, None)
        # The block hangs right under the core.
        drawn = canvas.bbox("chat")
        if drawn is not None and drawn[1] > top + 4:
            canvas.move("chat", 0, top + 4 - drawn[1])
        if self.chat_offset:
            hint = canvas.create_text(x, bottom + 12, text='Scroll down for the latest',
                                      font=(self.font_family, 9), fill="#4f7584", tags=("chat",))
            self.chat_colors[hint] = "#4f7584"
        canvas.tag_raise("chat")
        canvas.tag_raise("settings_entry")
        canvas.tag_raise("voice_label")

    FEEDBACK_MARKS = ((1, 'Like'), (-1, 'Dislike'), (None, 'Teach me'))

    def _draw_feedback(self, x, y, reply_id):
        "A quiet row under MYN's reply: Like, Dislike, Teach me. Clicking the selected rating again withdraws it. The teaching form opens only on request."
        canvas = self.core_canvas
        rating = self.reply_ratings.get(reply_id, 0)
        font = (self.font_family, 9)
        widths = [tkfont.Font(root=self.root, family=self.font_family, size=9).measure(label)
                  for _value, label in self.FEEDBACK_MARKS]
        gap = 18
        left = x - (sum(widths) + gap * (len(widths) - 1)) / 2
        for (value, label), width in zip(self.FEEDBACK_MARKS, widths):
            chosen = value is not None and value == rating
            fill = "#8ed9e6" if chosen else "#4f7584"
            item = canvas.create_text(left, y, text=label, anchor="nw", font=font, fill=fill,
                                      tags=("chat", "feedback"))
            self.chat_colors[item] = fill
            if value is None:
                canvas.tag_bind(item, "<Button-1>", lambda _event, r=reply_id: self.teach_reply(r))
            else:
                canvas.tag_bind(item, "<Button-1>", lambda _event, r=reply_id, v=value:
                                self.rate_reply(r, 0 if self.reply_ratings.get(r, 0) == v else v))
            canvas.tag_bind(item, "<Enter>", lambda _event, i=item: (canvas.configure(cursor="hand2"),
                                                                     canvas.itemconfigure(i, fill="#d6e9ee")))
            canvas.tag_bind(item, "<Leave>", lambda _event, i=item, f=fill: (canvas.configure(cursor=""),
                                                                             canvas.itemconfigure(i, fill=f)))
            left += width + gap

    def _preference_engine(self):
        return getattr(self.session, "preferences", None) if self.session is not None else None

    def rate_reply(self, reply_id, rating):
        'Like, dislike or withdraw a rating for one MYN reply (one rating per reply).'
        engine = self._preference_engine()
        if engine is None:
            return
        try:
            engine.rate(reply_id, rating)
        except Exception as error:
            self.append('Error', 'Could not save this rating: %s' % str(error)[:120])
            return
        self.reply_ratings[reply_id] = rating
        self._set_voice_label({1: 'Noted: you like this style', -1: 'Noted: you dislike this style',
                               0: 'Rating withdrawn'}[rating], clear_after=1600)
        self.chat_signature = None  # keep the exchange on screen a little longer
        self._queue_chat()

    def teach_reply(self, reply_id):
        'Teach me: rate a reply with a reason and/or a preferred wording.'
        engine = self._preference_engine()
        if engine is None:
            return
        text = next((shown for rid, shown in self.reply_links.values() if rid == reply_id), "")

        def done(rating, made):
            self.reply_ratings[reply_id] = rating
            self._set_voice_label('Noted. I will use this style in future.' if made else 'Noted', clear_after=1800)
            self.chat_signature = None
            self._queue_chat()

        if self.web_active and self.web is not None:  # inside the page, no second window
            import web_panels3
            self.sheets.open(web_panels3.TeachPanel(self, engine, reply_id, text, done))
            return
        preferences_window.FeedbackDialog(self.root, engine, reply_id, text, done, self.font_family)

    preferences_view = None

    def open_preferences(self):
        self._open_personal_view("rules")


    @staticmethod
    def _chat_hold_ms(text):
        """How long a finished message stays: a few seconds, more for longer text."""
        return int(min(20.0, 4.0 + .12 * len(text)) * 1000)

    def _expire_chat(self, epoch):
        if epoch != self.chat_epoch:
            return  # something newer is on screen
        if self.busy or self.draft_active or self.chat_offset or self._speaking() or self.web_intro:
            self.root.after(1000, self._expire_chat, epoch)  # still in use (or not yet seen)
            return
        self._fade_chat(epoch, len(self._subtitles()))

    FADE_SECONDS = .42
    FADE_RISE = 10  # px the subtitles drift upward as they dissolve

    def _fade_chat(self, epoch, count, plan=None):
        """Dissolve the subtitles into the scene behind them, then clear them.

        Time-based and redrawn every display frame: the colour eases towards the
        scene's colour under each line (fast at first, settling softly) while the
        block drifts up a little.
        """
        if epoch != self.chat_epoch:
            return
        if self.web_active:  # the page fades with a CSS transition
            if plan is None:
                self.web.js({"cmd": "fade"})
                self.root.after(int(self.FADE_SECONDS * 1000) + 60, self._fade_chat, epoch, count, True)
            else:
                self.chat_hidden_before = count
                self._queue_chat()
            return
        canvas = self.core_canvas
        now = time.perf_counter()
        if plan is None:
            cx = canvas.winfo_width() / 2
            cy = canvas.winfo_height() / 2 - 3
            space = self.core_animation.space
            items = []
            for item, fill in self.chat_colors.items():
                box = canvas.bbox(item)
                if box is None:
                    continue
                under = space.fog((box[0] + box[2]) / 2 - cx, (box[1] + box[3]) / 2 - cy)
                items.append((item, tuple(int(fill[i:i + 2], 16) for i in (1, 3, 5)), under))
            plan = {"start": now, "items": items, "risen": 0.0}
        progress = min(1.0, (now - plan["start"]) / self.FADE_SECONDS)
        if progress >= 1.0:
            self.chat_hidden_before = count
            canvas.delete("chat")
            self.chat_colors = {}
            return
        share = 1 - (1 - progress) ** 2.4  # ease out: brisk start, soft finish
        for item, start, under in plan["items"]:
            canvas.itemconfigure(item, fill="#%02x%02x%02x" % tuple(
                round(a + (b - a) * share) for a, b in zip(start, under)))
        rise = self.FADE_RISE * share
        canvas.move("chat", 0, plan["risen"] - rise)
        plan["risen"] = rise
        self.root.after(max(1, int(1000 / self.core_animation.refresh_hz)),
                        self._fade_chat, epoch, count, plan)
