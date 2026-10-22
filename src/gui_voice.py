"""Speech input: push-to-talk and the always-on microphone, wake words, barge-in.

Mixin composed by MYNWindow (myn_gui.py); shares its state and event queue."""

from gui_common import (
    config_for_db,
    conversation_mode,
    message_log,
    messagebox,
    queue,
    silence,
    stt,
    theme,
    threading,
    time,
    tk,
    ttk,
    tts,
    user_data_dir,
)
import interaction


class VoiceMixin:
    @property
    def queued_question(self):
        pending = getattr(self, "_pending_voice", ())
        return pending[0][0] if pending else None

    @queued_question.setter
    def queued_question(self, text):
        self._pending_voice = ((text, True),) if text else ()

    def _voice_trace(self, stage, **fields):
        if getattr(self.session, "db_path", None) is not None:
            import voice_diagnostics
            voice_diagnostics.record(config_for_db(self.session.db_path), stage, **fields)

    def _voice_review(self):
        try:
            callback = getattr(self.session, "pending_review", None)
            return callback() if callback else None
        except (OSError, ValueError):
            return None

    def _voice_attention(self):
        if self._wants_listening() and self._in_conversation():
            short = bool(self._voice_review()) and getattr(self, "reader_scene", "") != "tutoring"
            if short and getattr(self.session, "db_path", None):
                short = interaction.load(config_for_db(self.session.db_path))["mode"] == "idle"
            self._stt_job(stt.client.attention, conversation_mode.IDLE_SECONDS, short, quiet=True)

    def _reply_stage(self, stage):
        marker = getattr(self, "_reply_latency", None)
        if not marker or stage in marker:
            return
        if stage == "first_audio" and getattr(self, "tts_purpose", "reply") != "reply":
            return
        marker[stage] = True
        self._voice_trace(stage, turn=marker["turn"], elapsed_s=round(time.time()-marker["end"], 3))

    def _drain_voice(self):
        pending = getattr(self, "_pending_voice", ())
        if not pending or self.busy:
            return
        packet, self._pending_voice = pending[0], pending[1:]
        self._next_voice_preflight = packet[2] if len(packet) > 2 else None
        try:
            self._addressed(packet[0], heard=packet[1])
        finally:
            self._next_voice_preflight = None
        if not self.busy and self.queued_question:
            self.root.after(50, self._drain_voice)

    # ---- push-to-talk speech input -------------------------------------------------

    def _idle_voice_text(self):
        """Nothing is shown while simply listening; the scene stays clean."""
        return ""

    def _set_voice_label(self, text, clear_after=None):
        """Show a voice status line; temporary ones fall back to the idle line."""
        if self.voice_label is None:
            return
        self.label_token += 1
        self._show_voice(text or self._idle_voice_text())
        if clear_after:
            token = self.label_token

            def restore():
                if token == self.label_token and not self.listening:
                    self._show_voice(self._idle_voice_text())
            self.root.after(clear_after, restore)

    # ---- always-on microphone ----------------------------------------------------

    def _microphone_control(self, action, device=None):
        """Called on the chat thread. Hardware/config commit precedes the tool receipt."""
        import audio_inputs
        if action == "inspect":
            return audio_inputs.inspect(user_data_dir(), stt.client)
        if action != "switch":
            raise stt.STTError('Invalid microphone operation.')
        if self.listening:
            raise stt.STTError('Push-to-talk recording is active. Finish recording before switching.')
        updated, result = audio_inputs.switch(user_data_dir(), stt.client, device)
        def refresh():
            # Use the latest disk state in case the user saved a newer choice meanwhile.
            self.stt_settings = stt.load_settings(user_data_dir())
            if self.sheets.is_open("voice_settings"):
                self.sheets.panel.values["microphone"] = self.stt_settings.device or 'System default microphone'
                self.sheets.draw()
        self.events.put(("call", refresh))
        return result

    def _microphone_status(self):
        try:
            return stt.client.audio_status()
        except stt.STTError as error:
            return dict(active=None, actual_device=None, error=str(error),
                        note='Could not read the active device. The saved selection is not proof of the actual recording device.')

    def _wants_listening(self):
        return self.stt_settings.enabled and self.stt_settings.always_listen and \
            not stt.missing_parts()

    def _start_listening(self):
        if self.wake_voice["templates"] and stt.client.wake_args is None:
            # Wake by your own voice: the worker compares each utterance with your samples.
            self._stt_job(stt.client.set_wake_voice, self.wake_voice["templates"],
                          self.wake_voice["sensitivity"], quiet=True)
        self._stt_job(stt.client.listen, self.stt_settings.device, self.stt_settings.language,
                      self.stt_settings.wake_words,
                      result_event="listen_started")

    def _stop_listening(self):
        self.always_on_active = False
        self._close_voice_conversation('microphone_stopped')
        self._stt_job(stt.client.unlisten, quiet=True)
        self._set_voice_label("")

    def _listen_watchdog(self):
        """Bring always-on listening back if the speech worker stopped (e.g. a crash)."""
        if self._wants_listening() and not stt.client.alive and not self.listening:
            self.always_on_active = False
            self._stt_job(stt.client.listen, self.stt_settings.device, self.stt_settings.language,
                          self.stt_settings.wake_words,
                          quiet=True, result_event="listen_started")
        if self._in_conversation():
            self._voice_attention()
        self._finish_wake_learning()
        self.root.after(20000, self._listen_watchdog)

    def _heard(self, message):
        """One utterance from the always-on microphone."""
        if message.get('event') == 'vocal_activity':
            import singing
            try:
                at = float(message.get('end', 0))
                if (self.session is not None and self.always_on_active and
                        message.get('kind') == 'sustained_pitch' and 0 <= time.time()-at < 3):
                    vocal_guard = singing.guard(config_for_db(self.session.db_path))
                    before = vocal_guard.snapshot()
                    vocal_guard.signal(at)
                    if vocal_guard.snapshot() != before:
                        self._interaction_changed()
            except (ValueError, TypeError):
                pass
            return
        if message.get('event') == 'wake_status':
            self.streaming_wake_status = message
            if not message.get('active'):
                self._set_voice_label('Dedicated wake detection unavailable; continuing with the original speech wake detector', clear_after=5000)
            return
        if message.get('event') == 'recording_gap':
            if self.session is not None:
                import speech_journal
                speech_journal.gap(self.session.db_path, message)
            return
        if message.get('event') == 'wake_detected':
            self._last_voice_activity = time.time()
            # The UI confirms a wake only after transcription and echo checks.
            self._voice_trace('wake_candidate')
            return
        if message.get("event") == "voice_stage":
            self._voice_trace(message.get("stage", "unknown"),
                              **{k: v for k, v in message.items() if k in ("reason", "seconds", "wait_s")})
            if message.get("stage") == "recognizing" and self._in_conversation():
                self._set_voice_label('Heard you. Transcribing...', clear_after=2500)
            return
        if message.get("event") == "level":
            if message.get('speech'):
                self._last_voice_activity = time.time()
            self._mic_level(message)
            return
        if message.get("event") == "error":
            self._voice_trace("discard", reason="recognizer_error")
            self._set_voice_label('Always-on recognition error: %s' % message.get("error", ""), clear_after=4000)
            return
        if message.get("event") == "heard" and self.ducked_at:
            self.last_barge = 'Heard a sentence; checking intent'
        self._unduck()  # a whole utterance arrived: below it either stops MYN or MYN goes on
        text = str(message.get("text") or "").strip()
        if message.get('capture_id') in getattr(self, '_seen_captures', ()):
            return
        voice_woke = message.get("wake_voice") is True
        if message.get("event") == "heard" and "voice_score" in message:  # for myn_status
            self.wake_checks = (tuple(self.wake_checks) + ((time.strftime("%H:%M:%S"), message.get("voice_score"),
                                                            voice_woke),))[-8:]
        if (not text and not voice_woke) or self.session is None or not self.always_on_active or self.listening:
            return
        self._voice_trace("recognized", chars=len(text),
                          **{k: v for k, v in message.items() if k in ("transcribe_s", "wait_s", "language", "silence_s")})
        # Check the original transcript before fuzzy display normalization.
        # A sound-alike or acoustic match alone must not open a session.
        certain = stt.exact_wake(text, self.stt_settings.all_wake_words)
        self._voice_trace("wake_evidence", exact_wake=certain, acoustic_wake=voice_woke,
                          streaming_wake=bool(message.get('streaming_wake')),
                          record_only=bool(message.get('record_only')),
                          capture_id=str(message.get('capture_id') or '')[:80])
        # Normalize recognized wake words to their configured spelling.
        text = stt.show_wake(text, self.stt_settings.all_wake_words)
        try:
            started = float(message.get("start", 0))
        except (TypeError, ValueError):
            started = 0.0
        now = time.time()
        over_myn = bool(self.last_speaking and started <= self.last_speaking + 0.5)
        echo_window = bool(self.last_speaking and
                           started <= self.last_speaking + self.ECHO_TAIL_S)
        echo = 0.0
        if echo_window and text:
            # Echo filtering is independent of early-pause policy and microphone name.
            # Compare only speech submitted for reading, not unspoken reader text.
            spoken = [said for _when, said in self.recent_speech]
            echo = stt.echo_overlap(text, spoken)
            if echo >= self.ECHO_SHARE:
                self._voice_trace("discard", reason="echo")
                self.last_barge = "Not interrupted: likely MYN's own echo (%.0f%%)" % (echo * 100)
                return  # MYN's own voice coming back through the speakers
        rest = stt.strip_wake(text, self.stt_settings.all_wake_words)
        # Acoustic candidates are retained only after echo filtering. New speech
        # invalidates a previous success until its meaning is judged again.
        trial = getattr(self, 'wake_trial', None)
        try:
            fresh = 0 <= now - float(message.get('end', 0)) <= 15
        except (TypeError, ValueError):
            fresh = False
        if trial and fresh:
            if rest is not None or voice_woke:
                trial.begin(message)
            trial.observe(rest if rest else text)
        # Save exactly once BEFORE interpretation; no tools or preference updates here.
        if message.get('capture_id') and message.get('text') and self.session is not None:
            try:
                import speech_journal
                source_id = speech_journal.record(self.session.db_path, message, self.session.session_id)
                self._seen_captures = (getattr(self, '_seen_captures', ()) + (message['capture_id'],))[-2000:]
                records = getattr(self, '_speech_records', {})
                records[text] = records.get(text, []) + [source_id]
                self._speech_records = dict(list(records.items())[-150:])
            except message_log.SensitiveInputError:
                return
            except Exception:
                self._set_voice_label('Could not save the heard words. Request not executed', clear_after=4000)
                return
        if stt.is_filler(text) and not voice_woke:
            self._voice_trace('discard', reason='filler')
            if message.get('capture_id'):
                self._note_heard(text)
            return
        # Only a fresh, explicitly transcribed wake phrase opens hands-free chat.
        # Acoustic/streaming detectors are candidates; neither grants permission.
        active = self._in_conversation(now)
        if not fresh or (not certain and not active):
            self._voice_trace('discard', reason='stale_capture' if not fresh else 'wake_required')
            self._note_heard(text)
            return
        if not certain and float(message.get('end', 0)) < getattr(self, '_conversation_started', 0) - .1:
            self._voice_trace('discard', reason='previous_session_capture')
            self._note_heard(text)
            return
        if not certain:
            rest = None
            if now <= self.awaiting_until and (self._is_wake_ack(text) or stt.is_backchannel(text)):
                return
            if over_myn and (echo > self.BARGE_IN_SHARE or stt.is_backchannel(text)):
                self._voice_trace('discard', reason='backchannel_or_partial_echo')
                return
        origin_key = rest if rest else text
        if rest and rest != text and text in getattr(self, '_speech_records', {}):
            self._speech_records.setdefault(rest, []).append(self._speech_records[text].pop())
            if not self._speech_records[text]:
                self._speech_records.pop(text)
        ended = float(message.get('end', 0))
        origins = getattr(self, '_voice_origins', {})
        origins = {k: [t for t in v if now-t < 180] for k, v in origins.items()}
        origins[origin_key] = (origins.get(origin_key, []) + [ended])[-20:]
        self._voice_origins = dict(list(origins.items())[-40:])
        import singing
        singing_active = (getattr(self.session, 'db_path', None) is not None and
                          singing.guard(config_for_db(self.session.db_path)).snapshot()['active'])
        if certain:
            self._open_voice_conversation(started, now)
            self._voice_attention()
            if not singing_active:
                self.interrupt_speech()
                self._wake_ack()
                if self.web_active:
                    self.web.js({'cmd': 'wake'})
            if not rest and not singing_active:
                self._take_speech_record(text)
                self.awaiting_after = ended
                self.awaiting_until = now + stt.FOLLOW_UP_SECONDS
                self.awaiting_certain = True
                self._set_voice_label('Listening...', clear_after=stt.FOLLOW_UP_SECONDS * 1000)
                return
        self.awaiting_until = 0.0
        self._set_voice_label('Heard you. Checking who you are speaking to...', clear_after=4000)
        self._judge_heard(rest if rest else text)

    # Older spoken acknowledgements remain recognizable as echoes.
    WAKE_ACKS = ("I'm here.", "Yes?", "I'm listening.")
    wake_ack_request = None  # the speech request of the last wake reply

    def _wake_ack(self):
        """Confirm an exact wake locally; never synthesize speech or delay the next sentence."""
        now = time.monotonic()
        if now - getattr(self, '_wake_chime_at', float('-inf')) < 1.5:
            return
        self._wake_chime_at = now
        player = getattr(self, 'tts_player', None)
        if player is None:
            return
        try:
            player.set_output_device(self.tts_settings.output_device)
            player.play_wake_chime(self.tts_settings.volume)
        except tts.TTSError:
            self._voice_trace('wake_feedback', reason='playback_unavailable')

    def _is_wake_ack(self, text):
        def bare(said):
            return "".join(char for char in said if char.isalnum())
        # Recognize older spoken acknowledgements as echoes as well.
        known = self.WAKE_ACKS + ("我在。", "在呢。", "嗯，我在听。")
        return bool(bare(text)) and bare(text) in {bare(said) for said in known}

    awaiting_certain = True  # the bare wake word before the awaited sentence was the exact one
    last_answer_at = 0.0  # when MYN's last answer finished appearing

    def _last_reply_end(self):
        """When MYN last finished answering: done speaking, or (not read aloud) shown."""
        return max(self.last_speaking, self.last_answer_at)

    ECHO_SHARE = 0.45  # this much of what was heard is what MYN just said: its own echo
    BARGE_IN_SHARE = 0.25  # talking over MYN counts only when clearly not its echo
    ECHO_TAIL_S = 1.2  # room/recording tail after output ends; not an input mute window

    def _close_voice_conversation(self, reason):
        """End a session and invalidate work that has not been executed yet."""
        self.conversation_open = False
        self.awaiting_until = 0.0
        self.awaiting_certain = False
        self._judge_generation = getattr(self, '_judge_generation', 0) + 1
        pending = getattr(self, '_judge_pending', ())
        queued = getattr(self, '_pending_voice', ())
        self._judge_pending = ()
        self._judge_running = False
        # Explicit push-to-talk requests remain deliberate individual requests.
        self._pending_voice = tuple(packet for packet in queued if not packet[1])
        for text in pending:
            self._note_heard(text)
        for packet in queued:
            if packet[1]:
                self._note_heard(packet[0])
        self._voice_trace('conversation_closed', reason=reason)

    def _open_voice_conversation(self, started, now):
        self._close_voice_conversation('new_wake')
        self.conversation_open = True
        self.conversation_touch = now
        self._conversation_started = started
        if getattr(self.session, 'db_path', None) is not None:
            self._voice_scene_mode = interaction.load(config_for_db(self.session.db_path))['mode']
        generation = self._judge_generation
        def tick():
            if generation == self._judge_generation and self._in_conversation():
                self.root.after(1000, tick)
        self.root.after(1000, tick)

    def _in_conversation(self, now=None):
        """Keep a woken session while exchanging words, then close after 30 seconds."""
        if not self.conversation_open:
            return False
        now = time.time() if now is None else now
        if (self.busy and getattr(self, '_voice_turn_active', False)) or now - max(
                self.conversation_touch, self.last_speaking) <= conversation_mode.IDLE_SECONDS:
            return True
        self._close_voice_conversation('idle_timeout')
        self._set_voice_label('Voice chat ended. Say Hey, Mine to start again', clear_after=2500)
        return False

    def _judge_heard(self, text):
        """Ask, in the background, whether this sentence continues the conversation."""
        if not self._in_conversation():
            self._note_heard(text)
            return
        if len(getattr(self, "_judge_pending", ())) >= 20:
            self._voice_trace("discard", reason="judge_queue_full")
            self._note_heard(text)
            self._set_voice_label('Many voice messages are pending. Please wait before speaking again', clear_after=4000)
            return
        self._judge_pending = getattr(self, "_judge_pending", ()) + (text,)
        if getattr(self, "_judge_running", False):
            return
        self._start_voice_judge()

    def _start_voice_judge(self):
        if not self._in_conversation() or getattr(self, '_judge_running', False):
            return
        pending = getattr(self, "_judge_pending", ())
        if not pending:
            self._judge_running = False
            return
        text, self._judge_pending = pending[0], pending[1:]
        self._judge_running = True
        generation = getattr(self, "_judge_generation", 0)
        recent = [(tag, body) for tag, body, _link in self._messages()[-conversation_mode.RECENT_TURNS:]
                  if tag in ("you", "myn")]
        session = self.session
        now = time.time()
        aside = [said for when, said in self.recent_heard if now - when < 180]
        review = self._voice_review()

        def work():
            verdict = (False, False, False)
            prepared = None
            try:
                key = session._key()
                if key:
                    inspector = getattr(session, "inspect_voice", None)
                    if callable(inspector):
                        verdict, prepared = inspector(text, recent, aside)
                    else:
                        usage = config_for_db(session.db_path) / "claude-usage.jsonl"
                        options = {"heard": aside}
                        if review:
                            options["pending"] = review
                        verdict = conversation_mode.judge(key, recent, text, usage, **options)
            except Exception:
                pass  # treated as not for MYN; the sentence is still kept
            finally:
                self.events.put(("judged", text, verdict, generation, prepared))
        threading.Thread(target=work, daemon=True).start()

    def _judged(self, text, verdict, generation=None, prepared=None):
        if generation is not None and generation != getattr(self, "_judge_generation", 0):
            return
        if not self._in_conversation():
            self._note_heard(text)
            return
        self._judge_running = False
        to_myn, ends, elsewhere = verdict
        self._voice_trace("addressing", reason="to_myn" if to_myn else
                          "elsewhere" if elsewhere else "uncertain")
        if to_myn:
            if ends:
                # Clear older work first; this accepted farewell may still be answered once.
                self._close_voice_conversation('farewell')
            self.conversation_touch = time.time()
            self._next_voice_preflight = prepared
            try:
                self._addressed(text, heard=True)
            finally:
                self._next_voice_preflight = None
        else:
            vocal = (prepared or {}).get('decision', {}).get('vocal') or {}
            if (vocal.get('activity') in ('singing', 'humming') and vocal.get('certainty') == 'clear'
                    and isinstance(vocal.get('evidence'), str) and vocal['evidence'].strip()
                    and vocal['evidence'] in text):
                import speech_journal
                for source_id in getattr(self, '_speech_records', {}).get(text, [])[:1]:
                    try:
                        speech_journal.performance(self.session.db_path, source_id)
                    except Exception:
                        self._voice_trace('discard', reason='performance_annotation_failed')
            # Clearly meant for someone else (or a video): the conversation is over
            # until the wake word is heard again. Unclear bits just don't get a reply.
            if elsewhere or ends:
                self._close_voice_conversation('other_party' if elsewhere else 'farewell')
                self.interrupt_speech()
            self._note_heard(text)
        if self.conversation_open:
            generation = getattr(self, '_judge_generation', 0)
            self.root.after(0, lambda: self._start_voice_judge()
                            if generation == getattr(self, '_judge_generation', 0) else None)

    def _note_heard(self, text):
        """Keep what was heard (no reply) and let the memory queue classify it."""
        origins = getattr(self, "_voice_origins", {}).get(text, [])
        if origins:
            origins.pop(0)
        now = time.time()
        self.recent_heard = [(when, said) for when, said in self.recent_heard if now - when < 180][-5:] + \
            [(now, text)]
        try:
            if self._take_speech_record(text) is None:
                self.session.record_heard(text)
        except message_log.SensitiveInputError:
            self._set_voice_label('Possible key or password detected and discarded', clear_after=2500)
            return
        except Exception:
            self._set_voice_label('Could not save the heard words; check disk space', clear_after=3000)
            return
        # Saved quietly: only a failure to keep it is worth interrupting for.
        self.process_pending()

    recent_heard = ()  # [(when, text)] sentences heard lately that were not for MYN
    send_heard = False  # the next send() is a sentence the microphone heard without the wake word

    def _take_speech_record(self, text):
        records = getattr(self, '_speech_records', {})
        pending = records.get(text, [])
        if not pending:
            return None
        ident = pending.pop(0)
        if not pending:
            records.pop(text, None)
        return ident

    def _addressed(self, text, heard=False):
        """Speech meant for MYN: a local command, or a message that gets a reply.
        heard: an automatically captured utterance already judged to address MYN.
        The reply path retains its own final silence guard."""
        self.conversation_touch = time.time()
        self._voice_attention()
        self._voice_trace("addressed", chars=len(text))
        if self._run_voice_command(text):
            self._take_speech_record(text)
            return
        if self.busy:
            self._ask_after_reply(text, heard=heard)
            return
        if str(self.entry.cget("state")) == tk.DISABLED:
            self.entry.insert(tk.END, text)
            self.show_input()
            self._set_voice_label('Added to the input field', clear_after=2500)
            return
        self._set_voice_label('Heard you. Processing...', clear_after=3000)
        self.entry.delete(0, tk.END)
        self.entry.insert(0, text)
        self.send_heard = heard
        self._sending_voice = True
        try:
            self.send()
        finally:
            self.send_heard = False
            self._sending_voice = False

    def _run_voice_command(self, text):
        # Only emergency cancellation bypasses semantic understanding.
        action = stt.voice_command(text)
        if action == "stop":
            self.interrupt_speech()
            self.queued_question = None
            self._set_voice_label('OK', clear_after=1200)
            return True
        if action == "agent_stop":
            self.stop_agent_task()
            return True
        return False

    def _interface_control(self, action):
        """Run a whitelisted UI action on Tk's thread; expired requests never run."""
        import assistant_intent
        if action not in assistant_intent.ACTIONS:
            return dict(ok=False, error='Unknown interface operation.')
        signal, result = threading.Event(), {}
        expires = time.monotonic() + 10

        def perform():
            try:
                if signal.is_set() or time.monotonic() >= expires:
                    return
                if action in ('player', 'equalizer', 'reader_open', 'reader_close') and not self.web_active:
                    raise ValueError('This interface requires the WebView2 window, which is not open.')
                if action == 'read_all':
                    text = self._latest_reply_text()
                    if not text:
                        raise ValueError('There is no reply to read aloud yet.')
                    if self.busy:
                        self.interrupt_speech()
                        self._interface_read_text = text
                    else:
                        self.speak(text)
                elif action == 'equalizer':
                    self.web.js(dict(cmd='audio_console'))
                elif action.startswith('reader_'):
                    self.web.js(dict(cmd='reader', action=action.split('_')[1]))
                else:
                    {'agents': self.open_agents_settings, 'expenses': self.open_usage_stats,
                     'player': self.open_media_player, 'input_open': self.show_input,
                     'input_close': self.hide_input, 'stop_speaking': self.interrupt_speech}[action]()
                result.update(ok=True, action=action, dispatched=True)
            except Exception as error:
                result.update(ok=False, error=str(error)[:200])
            finally:
                signal.set()

        if threading.current_thread() is threading.main_thread():
            perform()
        else:
            self.events.put(('call', perform))
            if not signal.wait(10):
                signal.set()
                return dict(ok=False, error='The interface did not respond in time. Success is unconfirmed.')
        return result or dict(ok=False, error='Interface request expired.')

    def _finish_interface_speech(self):
        text = getattr(self, '_interface_read_text', None)
        self._interface_read_text = None
        if text and not self.queued_question:
            self.speak(text)

    def _speaking(self):
        try:
            playing = self.tts_player.active()
        except Exception:
            playing = False
        return playing or self.speech_queue is not None

    def _stt_job(self, action, *args, quiet=False, result_event=None):
        """Run speech-worker calls one at a time, in order, off the GUI thread."""
        if self.stt_jobs is None:
            self.stt_jobs = queue.Queue()

            def worker():
                while True:
                    job = self.stt_jobs.get()
                    if job is None:
                        return
                    call, call_args, call_quiet, event = job
                    try:
                        value = call(*call_args)
                        if event:
                            self.events.put((event, value))
                    except stt.STTError as error:
                        if not call_quiet:
                            self.events.put(("voice_error", str(error)))
                    except Exception:
                        if not call_quiet:
                            self.events.put(("voice_error", 'Speech recognition failed.'))

            threading.Thread(target=worker, daemon=True).start()
        self.stt_jobs.put((action, args, quiet, result_event))

    def voice_press(self):
        if self.listening or self.session is None:
            return
        if not self.stt_settings.enabled:
            self._set_voice_label('Voice input is unavailable. Check sound devices in Agents.', clear_after=2500)
            return
        missing = stt.missing_parts()
        if missing:
            self.append('Error', 'Voice input is not installed (missing: %s). Run %s once in PowerShell, then restart MYN.'
                        % ("、".join(missing), stt.SETUP_SCRIPT))
            return
        self.listening = True
        self.voice_token += 1
        self.interrupt_speech()  # talking over MYN interrupts its speech, like a person would
        # Recording starts only once the worker answers; until then, ask the user to wait.
        self._set_voice_label('Preparing the recognition model. Please wait...')
        self._stt_job(stt.client.start, self.stt_settings.device, result_event="voice_listening")
        token = self.voice_token
        self.root.after(stt.MAX_SECONDS * 1000,
                        lambda: self.listening and token == self.voice_token and self.voice_release())

    def voice_release(self):
        if not self.listening:
            return
        self.listening = False
        self._set_voice_label('Transcribing...')
        if self.wake_training is not None:
            # Training the wake word: also keep how this sample sounded.
            self._stt_job(stt.client.stop_enroll, self.stt_settings.language, result_event="voice_enroll")
            return
        self._stt_job(stt.client.stop, self.stt_settings.language, result_event="voice_text")

    wake_training = None  # set while the wake-word training window is collecting samples
    wake_voice = {"templates": [], "sensitivity": "medium"}  # your wake-word voice samples

    def _voice_enroll(self, result):
        text, template = result
        if self.wake_training is not None:
            self.wake_training(text or "", template)
        self._set_voice_label("")

    def _voice_text(self, text):
        if self.wake_training is not None:
            self.wake_training(text or "")
            self._set_voice_label("")
            return
        if not text:
            self._set_voice_label('I could not hear clearly. Please try again', clear_after=1800)
            return
        self._set_voice_label("")
        # Holding the button authorizes one request, not a hands-free session.
        certain = stt.exact_wake(text, self.stt_settings.all_wake_words)
        text = stt.show_wake(text, self.stt_settings.all_wake_words)
        if certain:
            self._open_voice_conversation(time.time(), time.time())
        # Push-to-talk is always addressed to MYN; a leading wake word is just dropped.
        rest = stt.strip_wake(text, self.stt_settings.all_wake_words)
        text = rest if rest else text
        if self._run_voice_command(text):
            return
        if self.busy:
            self._ask_after_reply(text, heard=False)
            return
        if str(self.entry.cget("state")) == tk.DISABLED:
            self.entry.insert(tk.END, text)
            self.show_input()
            return
        self.entry.delete(0, tk.END)
        self.entry.insert(0, text)
        self.send()

    def open_wake_training(self, wake_words, parent=None):
        """Collect wake-word voice samples; a recognized wake phrase is still required."""
        wake_words = stt.DEFAULT_WAKE_WORDS
        name = 'Hey，Mine'
        if self.web_active and self.web is not None:
            import web_panels
            self.sheets.open(web_panels.WakeTrainingPanel(self, wake_words))
            return None
        window = tk.Toplevel(parent or self.root)
        window.title('Train wake phrase')
        window.geometry("460x500")
        window.resizable(False, False)
        frame = ttk.Frame(window, padding=16)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text='Hold the button below (or right Ctrl), say "%s" naturally, then release.\n'
                              'Repeat at least %d times (ideally 5), from your usual position and at your normal volume. '
                              'MYN still requires a recognized wake phrase to start voice chat.' % (name, stt.WAKE_VOICE_MIN),
                  wraplength=420, justify=tk.LEFT).pack(anchor="w")
        hold = tk.Button(frame, text='Hold and say "%s"' % name, font=(self.font_family, 11),
                         bg="#16677e", fg="#e8fbff", activebackground="#2186a0", relief=tk.FLAT,
                         padx=16, pady=8, cursor="hand2")
        hold.pack(pady=10)
        hold.bind("<ButtonPress-1>", lambda _event: self.voice_press())
        hold.bind("<ButtonRelease-1>", lambda _event: self.voice_release())
        progress = ttk.Label(frame, font=(self.font_family, 10, "bold"))
        progress.pack(anchor="w")
        heard_list = tk.Listbox(frame, height=6, font=(self.font_family, 10))
        heard_list.pack(fill=tk.X, pady=(2, 8))
        sensitivity = tk.StringVar(value=self.wake_voice.get("sensitivity", "medium"))
        row = ttk.Frame(frame)
        row.pack(anchor="w", pady=(0, 6))
        ttk.Label(row, text='Sensitivity: ').pack(side=tk.LEFT)
        for value, label in (("low", 'Low (fewer false wakes)'), ("medium", 'Medium'), ("high", 'High (easier to wake)')):
            ttk.Radiobutton(row, text=label, value=value, variable=sensitivity).pack(side=tk.LEFT, padx=(0, 6))
        learned = [form for form in stt.wake_word_list(self.stt_settings.wake_learned)]
        known = ttk.Label(frame, wraplength=420, justify=tk.LEFT, foreground=theme.MUTED)
        known.pack(anchor="w")
        new_forms = []
        samples = []

        def show_known():
            saved = getattr(self, 'wake_profile_count', len(self.wake_voice["templates"]))
            if samples:
                progress.configure(text='Voice samples this session: %d / %d%s' % (
                    len(samples), stt.WAKE_VOICE_MIN,
                    '  Saved automatically; %d total samples' % saved))
            else:
                progress.configure(text='Voice wake: %s' % ('%d samples saved. Updates do not require re-recording; you can add more' % saved
                                                      if saved else 'Not configured yet'))
            known.configure(text='Fixed wake phrase: Hey, Mine. Features are included in full personal-data backups.')

        def heard(text, template=None):
            if template and len(template) >= 8:
                if len(samples) < stt.WAKE_VOICE_MAX:
                    if self._enroll_wake_sample(template) is None:
                        return
                    samples.append(template)
            elif template is not None:
                heard_list.insert(tk.END, '(No clear audio recorded. Please try again.)')
            # The list shows the wake word as you set it, never how recognition spelled it.
            form = stt.learned_form(text)
            count = 'Attempt %d' % len(samples) if template and len(template) >= 8 else ""
            if not text and template is None:
                heard_list.insert(tk.END, '(Could not hear clearly. Please try again.)')
            elif not text or not form:
                heard_list.insert(tk.END, '%s"%s" -> Voice recorded' % (count, name))
            elif stt.strip_wake(form, self.stt_settings.all_wake_words) == "" or form in new_forms:
                heard_list.insert(tk.END, '%s"%s" -> Recorded' % (count, name))
            else:
                heard_list.insert(tk.END, '%s"%s" -> Voice features saved' % (count, name))
            heard_list.see(tk.END)
            show_known()

        def save():
            if not self._save_wake_sensitivity(sensitivity.get()):
                return
            window.destroy()

        def clear():
            if messagebox.askyesno('Train wake phrase', 'Clear all voice-wake samples and learned spellings?', parent=window):
                learned.clear()
                new_forms.clear()
                samples.clear()
                self._save_wake_learned([])
                self._save_wake_voice([], sensitivity.get(), window)
                show_known()

        buttons = ttk.Frame(frame)
        buttons.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Button(buttons, text='Save', command=save).pack(side=tk.RIGHT)
        ttk.Button(buttons, text='Clear', command=clear).pack(side=tk.LEFT)
        show_known()
        self.wake_training = heard
        window.bind("<Destroy>", lambda event: event.widget is window and setattr(self, "wake_training", None))
        return window

    def _save_wake_voice(self, templates, sensitivity, parent=None):
        """Store the voice samples and hand them to the speech worker straight away."""
        try:
            stt.save_wake_voice(user_data_dir(), templates, sensitivity)
        except stt.STTError as error:
            self._show_error('Train wake phrase', str(error), parent)
            return False
        self.wake_voice = {"templates": list(templates), "sensitivity": sensitivity}
        self.wake_profile_count = len(templates)
        if not templates and getattr(self, 'wake_trial', None):
            self.wake_trial.clear()
        if templates or stt.client.wake_args is not None:
            self._stt_job(stt.client.set_wake_voice, list(templates), sensitivity)
        return True

    def _reload_wake_profile(self):
        import wake_learning
        self.wake_voice = wake_learning.active(user_data_dir())
        self.wake_profile_count = len(wake_learning.load(user_data_dir())['templates'])
        self._stt_job(stt.client.set_wake_voice, self.wake_voice['templates'],
                      self.wake_voice['sensitivity'], quiet=True)

    def _save_wake_sensitivity(self, value):
        import wake_learning
        try:
            wake_learning.sensitivity(user_data_dir(), value)
            self._reload_wake_profile()
            return True
        except (ValueError, OSError, stt.STTError) as error:
            self._show_error('Train wake phrase', str(error))
            return False

    def _enroll_wake_sample(self, template):
        import wake_learning
        try:
            data = wake_learning.enroll(user_data_dir(), template)
            self._reload_wake_profile()
            return len(data['templates'])
        except (ValueError, OSError, stt.STTError) as error:
            self._show_error('Train wake phrase', str(error))
            return None

    def _wake_feedback(self, context, decision, text, replied):
        trial = getattr(self, 'wake_trial', None)
        if not trial:
            return
        try:
            if trial.feedback(context, decision, text, replied, user_data_dir()):
                self._reload_wake_profile()
            self.root.after(31000, self._finish_wake_learning)
            if trial.pending and trial.pending.get('rejected'):
                self._close_voice_conversation('wake_rejected')
        except (ValueError, OSError, stt.STTError):
            self._set_voice_label('Could not save wake features. Existing samples were preserved', clear_after=3000)

    def _finish_wake_learning(self):
        trial = getattr(self, 'wake_trial', None)
        if trial:
            try:
                if trial.finish(user_data_dir()):
                    self._reload_wake_profile()
            except (ValueError, OSError, stt.STTError):
                self._set_voice_label('Could not save wake features. Existing samples were preserved', clear_after=3000)

    def _save_wake_learned(self, forms):
        import dataclasses
        text = ", ".join(forms)
        while len(text) > 400 and forms:  # keep the newest forms within the limit
            forms = forms[1:]
            text = ", ".join(forms)
        updated = dataclasses.replace(self.stt_settings, wake_words=stt.DEFAULT_WAKE_WORDS, wake_learned='')
        try:
            stt.save_settings(user_data_dir(), updated)
        except (ValueError, stt.STTError) as error:
            self._show_error('Train wake phrase', str(error))
            return
        self.stt_settings = updated

    def _poll_hotkey(self):
        """Hold Right Ctrl anywhere to talk. Reads only that one key's state."""
        try:
            import ctypes
            down = bool(ctypes.windll.user32.GetAsyncKeyState(0xA3) & 0x8000)  # VK_RCONTROL
        except (AttributeError, OSError):
            return
        if down:
            self._hotkey_held = getattr(self, "_hotkey_held", 0) + 1
            # About 0.2 s of holding starts listening, so a quick Ctrl shortcut does not.
            if self._hotkey_held == 5 and not self.listening:
                self.voice_press()
        else:
            if getattr(self, "_hotkey_held", 0) >= 5 and self.listening:
                self.voice_release()
            self._hotkey_held = 0
        self.root.after(40, self._poll_hotkey)

    def interrupt_speech(self):
        """You talked over MYN: stop now, and don't read out the rest of a reply still
        being written (its text still appears)."""
        self._interface_read_text = None
        self.stop_speech()
        if self.busy:
            self.reply_silenced = True

    def _ask_after_reply(self, text, heard=True):
        """Asked while a reply is still being written: answered as soon as it is done."""
        self.interrupt_speech()
        scene = interaction.standalone_mode(text)
        if scene in ("sleep", "study") and not self.session._optional_key():
            # Do not wait for a slow model before honoring "I'm going to bed".
            # Recording increments the scene revision, rejecting stale tool calls
            # from the older answer that is still being generated.
            try:
                self.session.record_input(text)
                self._interaction_changed()
                self.append('You', text)
                self.append("MYN", interaction.ACK[scene])
                self.queued_question = None
            except Exception:
                self._set_voice_label('Could not save the scene. Please try again later', clear_after=4000)
            return
        if len(getattr(self, "_pending_voice", ())) >= 20:
            self._voice_trace("discard", reason="reply_queue_full")
            self._note_heard(text)
            self._set_voice_label('This sentence was archived only. Many voice requests are pending; please retry later', clear_after=4000)
            return
        prepared = getattr(self, "_next_voice_preflight", None)
        packet = (text, heard, prepared) if prepared else (text, heard)
        self._pending_voice = getattr(self, "_pending_voice", ()) + (packet,)
        self._voice_trace("queued", depth=len(self._pending_voice))
        self._set_voice_label('Received. Answering now', clear_after=4000)

    def stop_speech(self):
        self._alarm_audio_result(self.tts_request_id, "tts_interrupted", 'The voice request was ended by a later message or action')
        if self.speech_queue is not None:
            self.speech_queue.put(None)
            self.speech_queue = None
        self.tts_request_id += 1
        tts.cancel_local()
        self.tts_player.stop()

    def _warm_local(self):
        token = tts.local_token()
        def work():
            try:
                tts.warm_local(token)
            except tts.TTSError as error:
                self.events.put(("tts_warm_error", token, str(error)))
        threading.Thread(target=work, daemon=True).start()

    def _reply_speech(self):
        """How a new reply is said as it streams: only the opening of a long one (setting)."""
        return tts.SpokenOpening(whole=self.tts_settings.long_reply == "all")

    def speak(self, text, opening_only=False, purpose="reply", then=None):
        if not self._speech_allowed(purpose):
            return  # suppressed background speech must not cancel a ringing alarm
        self.stop_speech()
        if opening_only and self.tts_settings.long_reply != "all":
            text = tts.spoken_part(text) or text
        cleaned = tts.speech_text(text)
        if not cleaned:
            self.append('Error', 'This reply has no text to read aloud.')
            return
        if len(cleaned) > tts.MAX_SPEECH_CHARS:
            cleaned = cleaned[:tts.MAX_SPEECH_CHARS].rstrip()
            self.append("MYN", 'Only the first 3,000 characters will be read this time.')
        # Same sentence-by-sentence path as streamed replies: the first clause starts
        # playing early, and stopping never has to kill the local model.
        self._stream_speak(tts.split_first(tts.speech_segments(cleaned)), purpose=purpose)
        if then and self.speech_queue is not None:  # bedtime briefing after the good-night reply
            self._stream_speak(tts.speech_segments(tts.speech_text(then)), purpose=purpose)
        if self.speech_queue is not None:  # no more sentences for this request
            self.speech_queue.put(None)
            self.speech_queue = None

    def send(self, _event=None):
        self._last_voice_activity = time.time()
        if getattr(self, "backup_working", False):
            self._set_voice_label('Backing up or restoring. Please wait...')
            return
        if self.busy or self.session is None:
            return
        raw = self.entry.get()
        question = raw.strip()
        if not question and not self.attached:
            return
        circled = self._take_circled()
        if circled:  # text circled in the reader goes with this question
            raw = '%s\n\n(Text I selected in the reader:\n"%s")' % (raw.strip(), circled)
            question = raw
        images = [block for _name, block in self.attached]
        if images:  # pictures go with this message; the saved text notes them
            names = "、".join(name for name, _block in self.attached)
            raw = (raw.strip() or 'Look at this image') + '(Attached image: %s)' % names
            question = raw
            self.attached = []
            self._show_attached()
        try:
            recorded = self._take_speech_record(raw) if getattr(self, '_sending_voice', False) else None
            message_id = self.session.record_input(raw, source_id=recorded) if recorded else self.session.record_input(raw)
        except message_log.SensitiveInputError as error:
            self.entry.delete(0, tk.END)
            self.append("MYN", str(error))
            return
        except Exception:
            self.append('Error', 'Could not save the original message. Request not executed; check the disk and data-folder permissions.')
            return
        self.entry.delete(0, tk.END)
        self.stop_speech()
        self._interaction_changed()
        self.transcript.mark_set("turn_start", "end-1c")
        self.transcript.mark_gravity("turn_start", tk.LEFT)
        self.append('You', raw)
        self.transcript.mark_set("turn_end", "end-1c")
        self.transcript.mark_gravity("turn_end", tk.LEFT)
        parts = question.split(None, 1)
        command = parts[0].casefold()
        if getattr(self.session, 'interface_control', None) is None and (
                command in ("show", "edit", "type", "delete", "restore") or
                command == "category" and (len(parts) == 1 or parts[1].split()[0].isdecimal())):
            self.open_memories()
            return
        if images:
            self.session.pending_images = images
        heard = bool(self.send_heard)
        self.busy = True
        self._voice_turn_active = bool(getattr(self, '_sending_voice', False))
        import uuid
        origins = getattr(self, "_voice_origins", {}).get(raw, [])
        self._reply_latency = ({"end": origins.pop(0), "turn": uuid.uuid4().hex[:12]}
                               if origins else None)
        self.reply_silenced = False
        self.speech_stream = self._reply_speech()
        self.silence_gate = silence.Gate(voice=heard)
        self.button.configure(state=tk.DISABLED)
        self.status.configure(text='Replying...')
        prepared = getattr(self, "_next_voice_preflight", None)
        threading.Thread(target=self._ask, args=(raw, message_id, heard, prepared), daemon=True).start()
