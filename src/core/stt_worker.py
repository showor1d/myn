"""Python 3.12 speech worker: push-to-talk and always-on listening, Whisper on CUDA.

Protocol: one JSON command per stdin line, one JSON reply per stdout line.
  ready                       -> {"ok": true, ...load info}
  devices                     -> {"ok": true, "devices": [...]}
  audio_status                -> actual active stream's device name, or inactive
  switch_microphone {device}  -> verified continuous input change, rollback on failure
  start {device}              -> begin push-to-talk recording (pauses always-on listening)
  stop {language, enroll}     -> stop recording and transcribe -> {"ok": true, "text": ...}
                                 (enroll: also the wake-word template of the sample)
  cancel                      -> stop recording and discard
  listen {device, language, wake_words} -> shared capture, streaming KWS and priority ASR
  unlisten                    -> stop always-on listening
  wake_voice {templates, sensitivity} -> wake by your own voice (templates: [] turns it off)
Events (unrequested lines): {"event": "heard", "text": ..., "start": t, "end": t}
  wake_detected -> candidate before ASR (never permission to execute an action)
  wake_status / recording_gap -> detector readiness / bounded queue discontinuity
  heard includes capture_id, record_only and streaming_wake for receipt/routing
(plus "wake_voice", "voice_score", "wake_end", "after_wake" when wake-by-voice is on)
with wall-clock seconds, so MYN can drop what it heard while it was speaking.
Only protocol lines go to stdout; library output is redirected to stderr. Audio
never leaves memory and is never written to disk.

Run with --self-test <model> to check the GPU, the model and the microphone once.
"""

import contextlib
import json
import sys
import threading
import time
import queue
import uuid
import itertools
import math

import stt
import wake_voice

SAMPLE_RATE = 16000
MIN_SECONDS = 0.3
SILENCE_RMS = 0.004

_OUT = None


def _claim_stdout():
    """Keep the real stdout for protocol replies; route everything else to stderr."""
    global _OUT
    _OUT = sys.stdout.buffer
    sys.stdout = sys.stderr


_OUT_LOCK = threading.Lock()


def respond(payload):
    """Write one protocol line; replies and listener events come from two threads."""
    line = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
    with _OUT_LOCK:
        _OUT.write(line)
        _OUT.flush()


def to_16k(np, audio, rate):
    if rate == SAMPLE_RATE or not audio.size:
        return audio
    target = int(audio.size * SAMPLE_RATE / rate)
    return np.interp(np.linspace(0, audio.size - 1, target),
                     np.arange(audio.size), audio).astype(np.float32)


def is_silent(np, audio):
    return audio.size < SAMPLE_RATE * MIN_SECONDS or \
        float(np.sqrt(np.mean(np.square(audio, dtype=np.float64)))) < SILENCE_RMS


class Recorder:
    """Collects mono float32 microphone samples in memory, capped at stt.MAX_SECONDS."""

    def __init__(self, sd, np):
        self.sd, self.np = sd, np
        self.lock = threading.Lock()
        self.stream = None
        self.chunks = []
        self.frames = 0
        self.rate = SAMPLE_RATE

    def _callback(self, indata, frames, _time, _status):
        with self.lock:
            if self.frames < self.rate * stt.MAX_SECONDS:
                self.chunks.append(indata[:, 0].copy())
                self.frames += frames

    def _host_api(self):
        """Windows lists every microphone once per audio API; WASAPI has full names."""
        try:
            for index, api in enumerate(self.sd.query_hostapis()):
                if "WASAPI" in api["name"]:
                    return index
        except Exception:
            pass
        return None

    def input_devices(self):
        api = self._host_api()
        names = []
        for device in self.sd.query_devices():
            if device["max_input_channels"] > 0 and (api is None or device["hostapi"] == api) \
                    and device["name"] not in names:
                names.append(device["name"])
        return names

    def _resolve(self, name):
        """Device index for a saved microphone name; None means the system default."""
        if not name:
            return None
        api = self._host_api()
        for index, device in enumerate(self.sd.query_devices()):
            if device["name"] == name and device["max_input_channels"] > 0 and \
                    (api is None or device["hostapi"] == api):
                return index
        raise LookupError(name)

    def open_stream(self, device_name, callback, block_seconds=None):
        """Open (not start) an input stream; returns (stream, rate, microphone used)."""
        try:
            device = self._resolve(device_name)
            used = device_name or ""
        except LookupError:
            device, used = None, ""  # unplugged or renamed: fall back to the default

        def open_at(rate):
            extra = {"blocksize": int(rate * block_seconds)} if block_seconds else {}
            return self.sd.InputStream(device=device, samplerate=rate, channels=1,
                                       dtype="float32", callback=callback, **extra)
        try:
            return open_at(SAMPLE_RATE), SAMPLE_RATE, used
        except Exception:
            # Some devices (e.g. WASAPI shared mode) refuse 16 kHz; record at the
            # device's own rate and resample later.
            info = (self.sd.query_devices(device) if device is not None
                    else self.sd.query_devices(kind="input"))
            rate = int(info["default_samplerate"])
            return open_at(rate), rate, used

    def start(self, device_name=None):
        """Start recording; returns the microphone actually used ("" = system default)."""
        self.cancel()
        with self.lock:
            self.chunks, self.frames = [], 0
        self.stream, self.rate, used = self.open_stream(device_name, self._callback)
        self.stream.start()
        return used

    def _close(self):
        stream, self.stream = self.stream, None
        if stream is not None:
            stream.stop()
            stream.close()

    def stop(self):
        np = self.np
        self._close()
        with self.lock:
            audio = np.concatenate(self.chunks) if self.chunks else np.zeros(0, np.float32)
            self.chunks, self.frames = [], 0
        return to_16k(np, audio[: self.rate * stt.MAX_SECONDS], self.rate)

    def cancel(self):
        self._close()
        with self.lock:
            self.chunks, self.frames = [], 0


class Listener:
    """Always-on microphone that cuts speech into utterances and transcribes each one.

    Utterances are found with an energy gate that follows the room's noise floor:
    speech starts after ~120 ms above three times the floor (90 ms / 2.2x while
    awaiting a reply), needs 240 ms of voiced audio (120 ms while attentive), and ends after 0.7 s
    of quiet (or at stt.MAX_SECONDS). Audio is processed on a separate thread so
    the sound card's callback never waits for the GPU.
    """

    BLOCK_S = 0.03
    START_BLOCKS = 4
    END_SILENCE_S = 0.7  # a pause this long ends what you said (shorter = quicker replies)
    PRE_ROLL_BLOCKS = 10
    MIN_SPEECH_S = 0.24  # actual above-threshold audio, excluding pre-roll and trailing silence
    MIN_THRESHOLD = 0.006

    LEVEL_EVERY_S = 0.05  # PCM-derived visual spectrum, up to 20 reports/second

    def __init__(self, recorder, np, transcribe, emit, threaded=True, clock=time.time, levels=False):
        self.recorder, self.np, self.transcribe, self.emit = recorder, np, transcribe, emit
        self.levels = levels
        self.level_peak = 0.0
        self.level_time = 0.0
        from audio_spectrum import Spectrum
        self.visual_spectrum = Spectrum(np)
        self.clock = clock
        self.language = "auto"
        self.device = ""
        self.stream = None
        self.rate = SAMPLE_RATE
        self.threaded = threaded
        self.pending = None
        self.wake = None  # wake_voice.WakeVoice when wake-by-voice is set up
        self.detector = None
        self.wake_at = 0.0
        self.wake_keyword = ''
        self.generation = 0
        self.sequence = itertools.count()
        self.capture_pending = None
        self.capture_lock = threading.RLock()
        self.capture_dropped_at = None
        self.attention_until = 0.0
        self.short_reply_until = 0.0
        from singing import PitchHint
        self.pitch_hint = PitchHint(np)
        self._reset(full=True)
        if threaded:
            import queue
            self.pending = queue.PriorityQueue(maxsize=120)
            self.capture_pending = queue.Queue(maxsize=200)
            threading.Thread(target=self._work, daemon=True).start()
            threading.Thread(target=self._capture_work, daemon=True).start()

    @property
    def active(self):
        return self.stream is not None

    def _reset(self, full=False):
        from collections import deque
        if full:
            self.floor = 0.005
            self.visual_spectrum.clear()
            self.level_peak, self.level_time = 0.0, 0.0
            from singing import PitchHint
            self.pitch_hint = PitchHint(self.np)
        self.loud = 0
        self.in_speech = False
        self.chunks = []
        self.quiet = 0.0
        self.voiced = 0.0
        self.duration = 0.0
        self.threshold = self.MIN_THRESHOLD
        self.start_at = 0.0
        self.preroll = deque(maxlen=self.PRE_ROLL_BLOCKS)

    def start(self, device_name=None):
        self.stop()
        self._reset(full=True)
        self.device = device_name or ""
        stream, self.rate, used = self.recorder.open_stream(
            device_name, self._callback, block_seconds=self.BLOCK_S)
        try:
            stream.start()
        except Exception:
            stream.close()
            raise
        self.stream = stream
        return used

    def stop(self):
        self.generation += 1
        stream, self.stream = self.stream, None
        if stream is not None:
            stream.stop()
            stream.close()
        with self.capture_lock:
            if self.in_speech:
                self.emit({'event': 'recording_gap', 'reason': 'input_restarted',
                           'start': self.start_at, 'end': self.clock()})
            self._reset()
            self.visual_spectrum.clear()
            if self.detector is not None:
                self.detector.reset()
            self.wake_at = 0.0

    def _callback(self, indata, frames, _time, _status):
        if self.capture_pending is None:
            self.feed(indata[:, 0].copy(), self.clock())
            return
        try:
            self.capture_pending.put_nowait((self.generation, indata[:, 0].copy(), self.clock()))
        except queue.Full:
            self.capture_dropped_at = self.clock()

    def _capture_work(self):
        while True:
            generation, block, now = self.capture_pending.get()
            if generation != self.generation:
                continue
            try:
                with self.capture_lock:
                    if generation == self.generation:
                        if self.capture_dropped_at:
                            self.emit({'event': 'recording_gap', 'reason': 'capture_overflow', 'end': self.capture_dropped_at})
                            self.capture_dropped_at = None
                            self._reset()
                            if self.detector is not None:
                                self.detector.reset()
                        self.feed(block, now)
            except Exception:
                self.emit({'event': 'recording_gap', 'reason': 'capture_processing', 'end': now})
                self._reset()

    def feed(self, block, now):
        """Advance the speech detector by one block of samples (also used by tests)."""
        np = self.np
        if self.levels and self.pitch_hint.feed(block, self.rate):
            self.emit({'event': 'vocal_activity', 'kind': 'sustained_pitch', 'end': now})
        if self.detector is not None:
            try:
                keyword = self.detector.feed(block, self.rate)
                if keyword and now - self.wake_at > 1.5:
                    self.wake_at, self.wake_keyword = now, keyword
                    self.attention_until = max(self.attention_until, now + 15)
                    self.emit({'event': 'wake_detected', 'end': now, 'keyword': keyword})
            except Exception:
                self.detector = None
                self.emit({'event': 'wake_status', 'active': False, 'reason': 'detector_failed'})
        seconds = block.size / self.rate
        rms = float(np.sqrt(np.mean(np.square(block, dtype=np.float64)))) if block.size else 0.0
        if self.levels:
            self.visual_spectrum.feed(block, self.rate)
            self._report_level(rms, seconds)
        if not self.in_speech:
            self.preroll.append(block)
            attentive = now < self.attention_until
            threshold = max(0.0035 if attentive else self.MIN_THRESHOLD,
                            self.floor * (2.2 if attentive else 3))
            if rms > threshold:
                self.loud += 1
            else:
                self.loud = 0
                self.floor = 0.95 * self.floor + 0.05 * rms
            if self.loud >= (3 if attentive else self.START_BLOCKS):
                self.in_speech, self.threshold = True, threshold
                self.voiced = self.loud * seconds
                self.minimum_voiced = 0.12 if attentive else self.MIN_SPEECH_S
                self.chunks = list(self.preroll)
                self.duration = sum(chunk.size for chunk in self.chunks) / self.rate
                self.start_at = now - self.duration
                self.utterance_attentive = now < self.attention_until
                self.quiet = 0.0
            return
        self.chunks.append(block)
        self.duration += seconds
        if rms >= self.threshold * 0.7:
            self.voiced += seconds
        self.quiet = self.quiet + seconds if rms < self.threshold * 0.7 else 0.0
        silence_s = 0.48 if now < self.short_reply_until and self.duration - self.quiet < 3 else self.END_SILENCE_S
        if self.quiet >= silence_s or self.duration >= stt.MAX_SECONDS:
            if self.voiced + 1e-6 >= self.minimum_voiced:
                audio = to_16k(np, np.concatenate(self.chunks), self.rate)
                woke = self.start_at <= self.wake_at <= now
                metadata = dict(capture_id=uuid.uuid4().hex, generation=self.generation,
                                record_only=bool(self.detector is not None and not woke and
                                                 not self.utterance_attentive),
                                streaming_wake=woke, wake_keyword=self.wake_keyword if woke else '')
                if woke:
                    metadata.update(wake_voice=True, wake_end=max(0, self.wake_at-self.start_at-.16),
                                    after_wake=max(0, now-self.quiet-self.wake_at+.16))
                item = (audio, self.start_at, now - self.quiet, self.language, silence_s, metadata)
                if self.threaded:
                    self._enqueue(item)
                else:
                    self._process(item)
            elif self.levels:
                self.emit({"event": "voice_stage", "stage": "discard",
                           "reason": "too_short", "seconds": round(self.voiced, 3)})
            self._reset()

    def _report_level(self, rms, seconds):
        """Loudness above the room's noise floor, 0..1, so the window can react to a voice.
        Only loudness and 128 FFT magnitudes leave the worker, never PCM."""
        level = min(1.0, max(0.0, (rms - self.floor * 2) / 0.08))
        self.level_peak = max(self.level_peak, level)
        self.level_time += seconds
        if self.level_time >= self.LEVEL_EVERY_S:
            try:
                bands = self.visual_spectrum.bands() if self.level_peak > .0015 else [0.] * 128
            except Exception:
                bands = []  # A visualization failure must not interrupt recognition.
            self.emit({"event": "level", "level": round(self.level_peak, 3),
                       "speech": self.in_speech, "bands": bands})
            self.level_peak, self.level_time = 0.0, 0.0

    def _work(self):
        while True:
            _priority, _sequence, item = self.pending.get()
            if item[5]['generation'] != self.generation:
                self.emit({'event': 'recording_gap', 'reason': 'input_restarted',
                           'start': item[1], 'end': item[2]})
                continue
            self._process(item)

    def _enqueue(self, item):
        entry = (1 if item[5]['record_only'] else 0, next(self.sequence), item)
        try:
            self.pending.put_nowait(entry)
        except queue.Full:
            # A live request must never sit behind a full passive backlog.
            with self.pending.mutex:
                if entry[0] == 0 and any(p[0] == 1 for p in self.pending.queue):
                    import heapq
                    dropped = max((p for p in self.pending.queue if p[0] == 1), key=lambda p: p[1])
                    self.pending.queue.remove(dropped)
                    heapq.heappush(self.pending.queue, entry)
                    heapq.heapify(self.pending.queue)
                else:
                    dropped = entry
            self.emit({'event': 'recording_gap', 'reason': 'transcription_backlog',
                       'start': dropped[2][1], 'end': dropped[2][2]})

    def _process(self, item):
        audio, start, end, language = item[:4]
        silence_s = item[4] if len(item) > 4 else self.END_SILENCE_S
        metadata = item[5] if len(item) > 5 else {}
        began = time.perf_counter()
        wait_s = max(0, self.clock()-end-silence_s)
        if self.levels:
            self.emit({"event": "voice_stage", "stage": "recognizing", "wait_s": round(wait_s, 3)})
        try:
            with contextlib.redirect_stdout(sys.stderr):
                text = stt.clean_transcript(self.transcribe(audio, language))
        except Exception as error:
            self.emit({"event": "error", "error": describe(error)})
            self.emit({'event': 'recording_gap', 'reason': 'transcription_failed', 'start': start, 'end': end})
            return
        event = {"event": "heard", "text": text, "start": round(start, 3), "end": round(end, 3)}
        event.update({k: v for k, v in metadata.items() if k != 'generation'})
        wake = self.wake
        if wake is not None:
            try:
                matched, score, wake_end = wake.match(audio)
                # after_wake: how long you kept talking after the wake word (0 = bare wake word)
                event['voice_score'] = score if math.isfinite(score) else None
                if matched:
                    event.update(wake_voice=True, record_only=False, wake_end=wake_end,
                                 after_wake=round(max(0.0, wake_voice.speech_end(self.np, audio) - wake_end), 2))
                elif not event.get('streaming_wake'):
                    event['wake_voice'] = False
            except Exception:
                pass  # a failed comparison never loses what was said
        # Learn only a clean, bounded keyword, never an entire command/sentence.
        # With no calibrated templates, bootstrap from an isolated exact keyword.
        bare = stt.strip_wake(text, stt.DEFAULT_WAKE_WORDS) == ''
        if stt.exact_wake(text, stt.DEFAULT_WAKE_WORDS):
            event['record_only'] = False
        score = event.get('voice_score')
        if bare or (event.get('wake_voice') and isinstance(score, (int, float)) and score < .9):
            stop = wake_voice.speech_end(self.np, audio) if bare else event.get('wake_end', 0)
            clip = audio[:int(stop * SAMPLE_RATE)]
            if .25 <= stop <= 2.3 and clip.size and float(self.np.mean(self.np.abs(clip) >= .98)) < .01:
                template = wake_voice.template_of(self.np, clip)
                if stt._valid_template(template):
                    event['wake_template'] = template
        # A short "MYn" said alone often comes out as nothing at all (or as a dropped
        # hallucination): if your voice matched the wake word, still report it.
        if not text and event.get("wake_voice") is not True:
            if self.levels:
                self.emit({"event": "voice_stage", "stage": "discard", "reason": "empty_transcript"})
            return
        event["transcribe_s"] = round(time.perf_counter()-began, 3)
        event["wait_s"] = round(wait_s, 3)
        event["silence_s"] = silence_s
        event["language"] = getattr(self.transcribe, "state", {}).get("detected") or language
        if metadata and metadata['generation'] != self.generation:
            self.emit({'event': 'recording_gap', 'reason': 'input_restarted', 'start': start, 'end': end})
            return
        self.emit(event)


def load_whisper(model_path):
    """Use CUDA when available; a fresh system can use CPU until its driver is ready."""
    started = time.perf_counter()
    import torch
    from transformers import WhisperForConditionalGeneration, WhisperProcessor
    accelerated = torch.cuda.is_available()
    device = 'cuda:0' if accelerated else 'cpu'
    dtype = torch.float16 if accelerated else torch.float32
    processor = WhisperProcessor.from_pretrained(model_path)
    model = WhisperForConditionalGeneration.from_pretrained(
        model_path, dtype=dtype, attn_implementation="sdpa").to(device).eval()
    zh_prompt = processor.get_prompt_ids(stt.ZH_PROMPT, return_tensors="pt").to(device)
    state = {"prompt": True, "detected": None}
    continuity = stt.LanguageContinuity()
    # "auto" chooses only among the languages MYN's user speaks, which is far more
    # reliable on short utterances than Whisper's open 99-language detection.
    language_tokens = {code: processor.tokenizer.convert_tokens_to_ids("<|%s|>" % code)
                       for code in stt.AUTO_CANDIDATES}
    start_token = torch.tensor([[model.generation_config.decoder_start_token_id]], device=device)

    def detect(features):
        with torch.inference_mode():
            logits = model(input_features=features, decoder_input_ids=start_token).logits[0, -1]
        return {code: float(logits[token]) for code, token in language_tokens.items()}

    def generate(features, **kwargs):
        with torch.inference_mode():
            ids = model.generate(features, task="transcribe", max_new_tokens=384, **kwargs)
        return processor.batch_decode(ids, skip_special_tokens=True)[0]

    gpu_lock = threading.Lock()

    def transcribe(audio, language):
        with gpu_lock:
            return transcribe_unlocked(audio, language)

    def transcribe_unlocked(audio, language):
        features = processor(audio, sampling_rate=SAMPLE_RATE,
                             return_tensors="pt").input_features.to(device, dtype=dtype)
        if language == "auto":
            try:
                scores = detect(features)
                language = continuity.choose(scores, audio.size/SAMPLE_RATE, time.time())
            except Exception:
                language = None  # fall back to Whisper's own detection
        state["detected"] = language
        kwargs = {"language": language} if language else {}
        # Short utterances have little context. Compare several acoustic paths
        # rather than hard-replacing homophones with a fixed command dictionary.
        if audio.size / SAMPLE_RATE <= 5:
            kwargs['num_beams'] = 3
        if language == "zh" and state["prompt"]:
            try:
                return generate(features, prompt_ids=zh_prompt, **kwargs)
            except (TypeError, ValueError):
                state["prompt"] = False  # this transformers build rejects prompts; go without
        return generate(features, **kwargs)

    # One silent pass so the first real utterance does not pay for kernel warm-up.
    import numpy as np
    transcribe(np.zeros(SAMPLE_RATE, np.float32), "zh")
    if accelerated:
        torch.cuda.synchronize()
    transcribe.state = state
    info = {"ok": True, "ready": True, "load_s": round(time.perf_counter() - started, 3),
            "gpu": torch.cuda.get_device_name(0) if accelerated else 'CPU (slower)',
            "torch": torch.__version__,
            "torch_allocated_mib": round(torch.cuda.memory_allocated() / 1048576) if accelerated else 0}
    return transcribe, info


def _device_arg(request):
    wanted = request.get("device") or ""
    if not isinstance(wanted, str) or len(wanted) > 200:
        raise ValueError('Invalid microphone name.')
    return wanted


def audio_status(recorder, listener):
    stream = getattr(recorder, "stream", None)
    mode = "push_to_talk"
    if stream is None:
        stream = getattr(listener, "stream", None)
        mode = "continuous"
    if stream is None or not getattr(stream, "active", False):
        return dict(active=False, actual_device=None, mode="inactive")
    # Read the stream's real input index, including a resolved system default.
    name = recorder.sd.query_devices(stream.device)["name"]
    return dict(active=True, actual_device=name, mode=mode)


def switch_microphone(recorder, listener, wanted):
    if getattr(recorder, "stream", None) is not None:
        raise ValueError('Hold-to-record is active; the microphone cannot be changed yet.')
    if listener is None or not listener.active:
        raise ValueError('Continuous microphone listening is inactive. Refresh status and retry.')
    if wanted and wanted not in recorder.input_devices():
        raise ValueError('Selected microphone unavailable; no switch made.')
    previous = listener.device
    try:
        used = listener.start(wanted or None)
        if wanted and used != wanted:
            raise ValueError('Selected microphone unavailable; automatic fallback refused.')
        state = audio_status(recorder, listener)
        if not state["active"] or (wanted and state["actual_device"] != wanted):
            raise ValueError('The new microphone could not be opened.')
        return state
    except Exception as error:
        listener.stop()
        try:
            restored = listener.start(previous or None)
            if previous and restored != previous:
                listener.stop()
                raise ValueError()
        except Exception:
            raise ValueError('Switch failed and the previous microphone could not be restored. Check its connection.') from error
        raise ValueError('Switch failed; the previous microphone was restored.') from error


def serve(lines, reply, recorder, transcribe, info, np, listener=None):
    """The command loop, independent of the real microphone and model (unit-tested)."""
    paused = False  # always-on listening is paused while push-to-talk records

    def resume():
        nonlocal paused
        if paused and listener is not None:
            paused = False
            listener.start(listener.device or None)

    for line in lines:
        try:
            request = json.loads(line)
            command = request.get("command") if isinstance(request, dict) else None
            if command == "ready":
                reply(info)
            elif command == "devices":
                reply({"ok": True, "devices": recorder.input_devices()})
            elif command == "audio_status":
                reply(dict(ok=True, **audio_status(recorder, listener)))
            elif command == "switch_microphone":
                reply(dict(ok=True, **switch_microphone(recorder, listener, _device_arg(request))))
            elif command == "listen":
                if listener is None:
                    raise ValueError('This speech process does not support always-on listening.')
                wanted = _device_arg(request)
                language = request.get("language")
                if language not in stt.LANGUAGES:
                    raise ValueError('Invalid recognition language.')
                recorder.cancel()
                paused = False
                listener.language = language
                words = request.get('wake_words')
                if words is not None:
                    listener.stop()
                    try:
                        import streaming_wake
                        listener.detector = streaming_wake.Detector(words)
                        listener.emit({'event': 'wake_status', 'active': True, 'engine': 'sherpa-onnx'})
                    except Exception as error:
                        listener.detector = None
                        listener.emit({'event': 'wake_status', 'active': False,
                                       'reason': str(error)[:160]})
                used = listener.start(wanted or None)
                if words is not None:
                    listener.emit({'event': 'recording_gap', 'reason': 'session_started', 'end': listener.clock()})
                reply({"ok": True, "listening": "continuous", "device": used,
                       "fallback": bool(wanted) and used != wanted})
            elif command == "unlisten":
                paused = False
                if listener is not None:
                    listener.stop()
                reply({"ok": True, "listening": False})
            elif command == "attention":
                seconds = request.get("seconds", 0)
                if listener is None or not isinstance(seconds, (int, float)) or not 0 <= seconds <= 120:
                    raise ValueError('Invalid listening duration')
                listener.attention_until = listener.clock() + seconds
                listener.short_reply_until = listener.attention_until if request.get("short_reply") is True else 0
                reply({"ok": True})
            elif command == "start":
                wanted = _device_arg(request)
                if listener is not None and listener.active:
                    listener.stop()  # one stream at a time; resumed after stop/cancel
                    paused = True
                used = recorder.start(wanted or None)
                reply({"ok": True, "listening": True, "device": used,
                       "fallback": bool(wanted) and used != wanted})
            elif command == "cancel":
                recorder.cancel()
                resume()
                reply({"ok": True, "text": ""})
            elif command == "stop":
                language = request.get("language")
                if language not in stt.LANGUAGES:
                    raise ValueError('Invalid recognition language.')
                audio = recorder.stop()
                resume()
                seconds = round(audio.size / SAMPLE_RATE, 2)
                if is_silent(np, audio):
                    reply({"ok": True, "text": "", "seconds": seconds, "silent": True})
                    continue
                began = time.perf_counter()
                with contextlib.redirect_stdout(sys.stderr):
                    text = transcribe(audio, language)
                result = {"ok": True, "text": stt.clean_transcript(text), "seconds": seconds,
                          "transcribe_s": round(time.perf_counter() - began, 3)}
                if request.get("enroll"):
                    result["template"] = wake_voice.template_of(np, audio)
                reply(result)
            elif command == "wake_voice":
                if listener is None:
                    raise ValueError('This speech process does not support always-on listening.')
                templates = request.get("templates") or []
                sensitivity = request.get("sensitivity") or "medium"
                if not isinstance(templates, list) or len(templates) > 12 or \
                        sensitivity not in wake_voice.SENSITIVITY:
                    raise ValueError('Invalid acoustic wake settings.')
                if not templates:
                    listener.wake = None
                    reply({"ok": True, "wake_voice": False})
                    continue
                listener.wake = wake_voice.WakeVoice(np, templates, sensitivity)
                reply({"ok": True, "wake_voice": True, "templates": len(listener.wake.templates),
                       "threshold": round(listener.wake.threshold, 3)})
            else:
                raise ValueError('Invalid speech recognition command.')
        except Exception as error:
            reply({"ok": False, "error": describe(error)})


def describe(error):
    name = error.__class__.__name__
    if name == "OutOfMemoryError":
        return 'Speech recognition ran out of GPU memory. Close GPU-heavy programs and retry.'
    if name == "PortAudioError" or "PortAudio" in str(error):
        return 'Cannot open the microphone. Check Windows default input device and microphone permissions.'
    if isinstance(error, ValueError):
        return str(error)[:200]
    return 'Speech recognition failed. Check the model and GPU runtime.'


def self_test(model_path):
    """Print a JSON report: model on CUDA, a real transcription pass, default microphone."""
    report = {"ok": False}
    try:
        import numpy as np
        transcribe, info = load_whisper(model_path)
        report.update(info)
        tone = (0.1 * np.sin(2 * np.pi * 220 * np.arange(SAMPLE_RATE * 2) / SAMPLE_RATE)).astype(np.float32)
        began = time.perf_counter()
        report["zh_text"] = stt.clean_transcript(transcribe(tone, "zh"))
        report["auto_text"] = stt.clean_transcript(transcribe(tone, "auto"))
        report["auto_detected"] = transcribe.state["detected"]
        report["transcribe_s"] = round(time.perf_counter() - began, 3)
        import sounddevice as sd
        device = sd.query_devices(kind="input")
        report["microphone"] = device["name"]
        recorder = Recorder(sd, np)
        report["microphones"] = recorder.input_devices()
        recorder.start()
        time.sleep(0.5)
        audio = recorder.stop()
        report["microphone_samples"] = int(audio.size)
        report["ok"] = audio.size > 0
    except Exception as error:
        report["error"] = describe(error)
        report["detail"] = "%s: %s" % (type(error).__name__, str(error)[:300])
    respond(report)
    return 0 if report["ok"] else 1


def main(model_path):
    with contextlib.redirect_stdout(sys.stderr):
        import numpy as np
        import sounddevice as sd
        transcribe, info = load_whisper(model_path)
    recorder = Recorder(sd, np)
    listener = Listener(recorder, np, transcribe, respond, levels=True)
    serve(sys.stdin.buffer, respond, recorder, transcribe, info, np, listener)


if __name__ == "__main__":
    _claim_stdout()
    if len(sys.argv) == 2 and sys.argv[1] == "--audio-devices":
        try:
            import sounddevice as sd
            try:
                default = sd.query_devices(kind="input")["name"]
            except Exception:
                default = None
            respond(dict(ok=True, devices=Recorder(sd, None).input_devices(), default_device=default))
            sys.exit(0)
        except Exception:
            respond(dict(ok=False, error='Cannot enumerate microphones.'))
            sys.exit(1)
    if len(sys.argv) == 3 and sys.argv[1] == "--self-test":
        sys.exit(self_test(sys.argv[2]))
    try:
        main(sys.argv[1])
    except Exception as failure:
        message = ('Speech recognition cannot use CUDA and will not fall back to CPU automatically.' if "CUDA" in str(failure)
                   else describe(failure))
        # The client reads one reply per request; keep answering with the load error.
        for _line in sys.stdin.buffer:
            respond({"ok": False, "error": message})
