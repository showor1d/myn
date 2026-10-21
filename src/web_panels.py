"""MYN's settings screens drawn inside the main view (see sheets.py). Each Panel describes
itself as nodes for web/app.js and does the same work as its Tk window (kept for the classic
window). Voice controls are embedded in Agents; PANELS retains the voice panel compatibility name."""


import credentials
import stt
import tts
import tts_player
from data_location import user_data_dir
from sheets import Panel



# Voice settings.

DEFAULT_MIC = 'System default microphone'
DEFAULT_OUTPUT = 'System default output device'


class VoicePanel(Panel):
    key = "voice_settings"
    title = 'Sound settings'
    width = 640

    def __init__(self, win):
        super().__init__(win)
        self.microphones = []
        self.outputs, self.output_error = [], ""
        self._load_outputs()
        self.missing = stt.missing_parts()
        if not self.missing:  # the speech worker lists the microphones (may wait for the model)
            win.on_voice_devices = self._got_microphones
            win._stt_job(stt.client.devices, result_event="voice_devices")

    def _load_outputs(self):
        try:
            self.outputs = tts_player.output_devices()
            self.output_error = ""
        except tts.TTSError as error:
            self.output_error = str(error)

    def _got_microphones(self, names):
        self.microphones = list(names)
        self.redraw()

    def closed(self):
        if getattr(self.win, "on_voice_devices", None) == self._got_microphones:
            self.win.on_voice_devices = None

    def body(self):
        win = self.win
        current, voice = win.tts_settings, win.stt_settings
        source = credentials.elevenlabs_key_source(user_data_dir())
        key_status = {"environment": 'Using the environment variable key (takes priority).', "saved": 'A key is stored encrypted on this computer.',
                      "none": 'No key configured.'}[source]
        mics = [DEFAULT_MIC] + self.microphones
        if voice.device and voice.device not in mics:
            mics.append(voice.device)
        outputs = [DEFAULT_OUTPUT] + self.outputs
        selected_output = self.v("output_device", current.output_device or DEFAULT_OUTPUT)
        if selected_output not in outputs:
            outputs.append(selected_output)
        return [
            {"t": "h", "text": 'Read aloud'},
            {"t": "row", "children": [
                {"t": "select", "id": "output_device", "label": 'Output device',
                 "value": selected_output, "options": [[name, name] for name in outputs]},
                {"t": "buttons", "items": [{"action": "refresh_outputs", "label": 'Refresh devices'}]}]},
            {"t": "p", "tone": "warn" if self.output_error else "muted",
             "text": self.output_error or 'Choose output and input devices independently, such as speakers for playback and a headset microphone for recording.'},
            {"t": "row", "children": [
                {"t": "select", "id": "provider", "label": 'Speech service', "value": self.v("provider", current.provider),
                 "options": [["elevenlabs", 'ElevenLabs (cloud)'], ["qwen3-local", 'Qwen3-TTS (local)']]},
                {"t": "select", "id": "eleven_model", "label": 'ElevenLabs model',
                 "value": self.v("eleven_model", current.eleven_model),
                 "options": [[code, tts.ELEVEN_MODEL_LABELS[code]] for code in tts.ELEVEN_MODELS]}]},
            {"t": "row", "children": [
                {"t": "field", "id": "voice_id", "label": 'ElevenLabs voice ID (English and default)',
                 "value": self.v("voice_id", current.voice_id)},
                {"t": "field", "id": "voice_id_zh", "label": 'Chinese voice ID (optional; leave blank to use the default)',
                 "value": self.v("voice_id_zh", current.voice_id_zh)}]},
            {"t": "select", "id": "local_speaker", "label": 'Qwen3 local voice', "value": self.v("local_speaker", current.local_speaker),
             "options": [[name, name] for name in tts.LOCAL_SPEAKERS]},
            {"t": "row", "children": [
                {"t": "slider", "id": "volume", "label": 'Volume (applies immediately)', "min": 0, "max": 100,
                 "value": int(round(float(self.v("volume", current.volume * 100)))), "unit": "%", "on": "volume"},
                {"t": "slider", "id": "speed", "label": 'Speed (applies to the next spoken reply)', "min": 0.7, "max": 1.2, "step": 0.05,
                 "value": self.v("speed", current.speed)}]},
            {"t": "group", "title": "ElevenLabs API Key", "children": [
                {"t": "p", "tone": "muted", "text": key_status + ' Enter keys only here. Saved keys are not displayed.'},
                {"t": "row", "children": [
                    {"t": "field", "id": "eleven_key", "kind": "password", "value": "", "placeholder": 'Paste new key'},
                    {"t": "buttons", "items": [{"action": "save_key", "label": 'Save key'},
                                               {"action": "clear_key", "label": 'Clear local key', "kind": "danger"}]}]}]},
            {"t": "h", "text": 'Listen'},
            {"t": "row", "children": [
                {"t": "select", "id": "language", "label": 'Recognition language', "value": self.v("language", voice.language),
                 "options": [[code, stt.LANGUAGE_LABELS[code]] for code in stt.LANGUAGES]},
                {"t": "select", "id": "microphone", "label": 'Microphone', "value": self.v("microphone", voice.device or DEFAULT_MIC),
                 "options": [[name, name] for name in mics]}]},
            {"t": "p", "text": 'Wake phrase: Hey, Mine | Voice features save automatically and are included in personal-data backups'},
            {"t": "buttons", "items": [{"action": "train", "label": 'Train the wake phrase with my voice...'}]},
            {"t": "p", "tone": "warn" if self.missing else "muted",
             "text": ('Not installed: %s. Run setup_voice_input.ps1.' % "、".join(self.missing)) if self.missing
             else 'Whisper large-v3-turbo runs on the local GPU. Recordings are processed in memory only.'},
            {"t": "buttons", "align": "right", "items": [{"action": "save", "label": 'Save', "kind": "primary"}]},
        ]

    def act(self, action, values):
        win = self.win
        if action == "refresh_outputs":
            self._load_outputs()
            self.redraw()
        elif action == "volume":
            try:
                win.tts_player.set_volume(float(values.get("volume", 80)) / 100)
            except Exception:
                pass
        elif action == "save_key":
            try:
                credentials.save_elevenlabs_key(user_data_dir(), values.get("eleven_key") or "")
            except (ValueError, OSError, RuntimeError):
                self.toast('Could not save the key. Check the input and data-folder permissions.', "bad")
                return
            win.stop_speech()
            self.toast('ElevenLabs key saved with encryption.', "good")
            self.redraw()
        elif action == "clear_key":
            def answered(yes):
                if not yes:
                    return
                try:
                    credentials.clear_elevenlabs_key(user_data_dir())
                except OSError:
                    self.toast('Could not clear the local key.', "bad")
                    return
                win.stop_speech()
                self.toast('Local key cleared.', "good")
                self.redraw()
            self.confirm('Clear the locally encrypted ElevenLabs key? Environment variables will remain unchanged.', answered,
                         title='Clear ElevenLabs API key', yes='Clear', danger=True)
        elif action == "train":
            win.open_wake_training(stt.DEFAULT_WAKE_WORDS)
        elif action == "save":
            try:
                updated = tts.TTSSettings(
                    provider=values.get("provider") or "elevenlabs", auto_read=True,
                    long_reply="opening",
                    voice_id=(values.get("voice_id") or "").strip(), voice_id_zh=(values.get("voice_id_zh") or "").strip(),
                    local_speaker=values.get("local_speaker") or tts.LOCAL_SPEAKERS[0],
                    volume=float(values.get("volume", 80)) / 100, speed=round(float(values.get("speed", 1.0)), 2),
                    output_device=("" if values.get("output_device", win.tts_settings.output_device) == DEFAULT_OUTPUT
                                   else values.get("output_device", win.tts_settings.output_device)),
                    eleven_model=values.get("eleven_model") or "eleven_flash_v2_5").validate()
                tts.save_settings(user_data_dir(), updated)
                chosen = values.get("microphone") or DEFAULT_MIC
                voice = stt.STTSettings(enabled=True, language=values.get("language") or "auto",
                                        device="" if chosen == DEFAULT_MIC else chosen,
                                        always_listen=True,
                                        wake_words=stt.DEFAULT_WAKE_WORDS, wake_learned='')
                stt.save_settings(user_data_dir(), voice)
            except (ValueError, KeyError, TypeError, tts.TTSError, stt.STTError) as error:
                self.toast(str(error) or 'Invalid settings.', "bad")
                return
            win._apply_voice_settings(updated, voice)
            self.toast('Sound settings saved.', "good")


class WakeTrainingPanel(Panel):
    """Say the wake word a few times; how it SOUNDS becomes voice samples."""

    key = "wake_training"
    title = 'Train wake phrase'
    width = 560

    def __init__(self, win, wake_words):
        super().__init__(win)
        self.wake_words = stt.DEFAULT_WAKE_WORDS
        self.name = 'Hey，Mine'
        self.learned = list(stt.wake_word_list(win.stt_settings.wake_learned))
        self.new_forms, self.samples, self.heard_lines = [], [], []
        win.wake_training = self.heard

    def closed(self):
        if getattr(self.win, "wake_training", None) == self.heard:
            self.win.wake_training = None

    def heard(self, text, template=None):
        name = self.name
        if template and len(template) >= 8:
            if len(self.samples) < stt.WAKE_VOICE_MAX:
                if self.win._enroll_wake_sample(template) is None:
                    return
                self.samples.append(template)
        elif template is not None:
            self.heard_lines.append('(No clear audio recorded. Please try again.)')
        form = stt.learned_form(text)
        count = 'Attempt %d' % len(self.samples) if template and len(template) >= 8 else ""
        if not text and template is None:
            self.heard_lines.append('(Could not hear clearly. Please try again.)')
        elif not text or not form:
            self.heard_lines.append('%s"%s" -> Voice recorded' % (count, name))
        elif stt.strip_wake(form, self.win.stt_settings.all_wake_words) == "" or form in self.new_forms:
            self.heard_lines.append('%s"%s" -> Recorded' % (count, name))
        else:
            self.heard_lines.append('%s"%s" -> Voice features saved' % (count, name))
        self.redraw()

    def body(self):
        saved = getattr(self.win, 'wake_profile_count', len(self.win.wake_voice["templates"]))
        if self.samples:
            progress = 'Voice samples this session: %d / %d%s' % (len(self.samples), stt.WAKE_VOICE_MIN,
                                               '  Saved automatically; %d total samples' % saved)
        else:
            progress = 'Voice wake: %s' % ('%d samples saved. Updates do not require re-recording; you can add more' % saved if saved else 'No personal features yet. You can still say Hey, Mine to wake MYN')
        return [
            {"t": "p", "text": 'Hold the button below (or right Ctrl), say "%s" naturally, then release. Repeat at least %d times (ideally 5), '
                               'from your usual position and at your normal volume; MYN can then wake to your voice.' % (self.name, stt.WAKE_VOICE_MIN)},
            {"t": "buttons", "items": [{"press": "press", "release": "release", "label": 'Hold and say "%s"' % self.name,
                                        "kind": "primary"}]},
            {"t": "h", "text": progress},
            {"t": "table", "id": "heard", "rows": [{"key": i, "cells": [line]} for i, line in enumerate(self.heard_lines)],
             "height": 170, "empty": 'Your attempts will appear here'},
            {"t": "radio", "id": "sensitivity", "label": 'Sensitivity',
             "value": self.v("sensitivity", self.win.wake_voice.get("sensitivity", "medium")),
             "options": [["low", 'Low (fewer false wakes)'], ["medium", 'Medium'], ["high", 'High (easier to wake)']]},
            {"t": "p", "tone": "muted", "text": 'Each sample saves automatically. At least three enable personal matching. Confirmed everyday wakes continue training; false wakes are excluded.'},
            {"t": "buttons", "items": [{"action": "clear", "label": 'Clear', "kind": "danger"},
                                       {"action": "back", "label": 'Back to sound settings'},
                                       {"action": "save", "label": 'Save', "kind": "primary"}]},
        ]

    def act(self, action, values):
        win = self.win
        if action == "press":
            win.voice_press()
        elif action == "release":
            win.voice_release()
        elif action == "back":
            win.open_voice_settings()
        elif action == "save":
            if win._save_wake_sensitivity(values.get("sensitivity") or "medium"):
                win.open_voice_settings()
                win.sheets.toast('Wake-phrase training saved.', "good")
        elif action == "clear":
            def answered(yes):
                if not yes:
                    return
                self.learned.clear()
                self.new_forms.clear()
                self.samples.clear()
                win._save_wake_learned([])
                win._save_wake_voice([], values.get("sensitivity") or "medium")
                self.redraw()
            self.confirm('Clear all voice-wake samples and learned spellings?', answered, yes='Clear', danger=True)


PANELS = {
}
