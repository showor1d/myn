"""Apply persisted availability to speech and foreground notifications."""
import interaction


class InteractionMixin:
    tts_purpose = "reply"

    def _speak_bedtime_briefing(self, briefing):
        """One narrow sign-off exception; never interrupt an alarm or retry later."""
        if not self.tts_settings.auto_read or self.queued_question:
            return
        if self.tts_purpose == "alarm" and (self._speaking() or self.speech_queue is not None):
            return
        import bedtime
        try:
            token = bedtime.claim(self._usage_config(), briefing)
        except (OSError, ValueError):
            return  # Can't persist the once-only guard: do not risk a repeated wake-up.
        if token:
            self.speak(briefing["text"], purpose=token)

    def _speech_allowed(self, purpose="reply"):
        import singing
        if not singing.guard(self._usage_config()).allowed(purpose):
            return False
        if purpose == "proactive" and getattr(self, "media_playing", False):
            return False  # don't interrupt a song/video with a background proposal or restart
        return interaction.allowed(self._usage_config(), purpose)

    def _interaction_changed(self):
        try:
            self._sync_ringing()
        except (OSError, ValueError, AttributeError):
            pass
        if not self._speech_allowed(self.tts_purpose):
            self.stop_speech()
        if interaction.load(self._usage_config())["mode"] == "sleep":
            self.conversation_open = False
            self.awaiting_until = 0
        self._voice_attention()

    def _interaction_notify(self):
        # Called by a model tool in the chat worker, before final answer streaming.
        self.events.put(("call", self._interaction_changed))

    def _background_notice(self, text, spoken):
        if not self._speech_allowed("proactive"):
            # The original result remains in the background task log. Do not
            # open a long reading panel over the user's lesson.
            self.background_notice_waiting = True
            return
        self.append("MYN", text, speakable=True, spoken_text=spoken)
        if self.tts_settings.auto_read and not self.busy:
            self.speak(spoken, purpose="proactive")

    def _background_notice_ready(self):
        if getattr(self, "background_notice_waiting", False) and self._evolution_idle():
            self.background_notice_waiting = False
            self.append("MYN", 'There is progress on a background task. Ask to view it when convenient.')
