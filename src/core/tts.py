"""Text preparation, settings, and provider boundary for MYN speech."""

import html
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

import credentials
from data_location import user_data_dir


SETTINGS_NAME = "tts-settings.json"
DEFAULT_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"  # ElevenLabs' documented George voice.
LOCAL_SPEAKERS = ("Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric", "Ryan", "Aiden", "Ono_Anna", "Sohee")
MAX_SPEECH_CHARS = 3000
MAX_AUDIO_BYTES = 8 * 1024 * 1024
VOICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
# Flash is ElevenLabs' lowest-latency model; Multilingual v2 favours quality.
ELEVEN_MODELS = ("eleven_flash_v2_5", "eleven_multilingual_v2")
ELEVEN_MODEL_LABELS = {"eleven_flash_v2_5": 'Flash v2.5 (fastest)',
                       "eleven_multilingual_v2": 'Multilingual v2 (quality first)'}


class TTSError(Exception):
    """A user-facing TTS error without credentials or response bodies."""


@dataclass(frozen=True)
class PCMChunk:
    samples: bytes
    rate: int = 24000


@dataclass(frozen=True)
class TTSSettings:
    provider: str = "elevenlabs"
    auto_read: bool = True
    voice_id: str = DEFAULT_VOICE_ID
    voice_id_zh: str = ""  # optional native-Chinese voice for Chinese sentences
    local_speaker: str = "Vivian"
    volume: float = 0.8
    speed: float = 1.0
    eleven_model: str = "eleven_flash_v2_5"
    long_reply: str = "opening"  # long replies: "opening" = say only the opening line, "all" = read it all
    output_device: str = ""  # "" follows the system output; independent of the microphone

    def validate(self):
        if self.provider not in ("elevenlabs", "qwen3-local"):
            raise ValueError('Invalid speech provider.')
        if not isinstance(self.output_device, str) or len(self.output_device) > 200 or "\0" in self.output_device:
            raise ValueError('Invalid playback device name.')
        if not isinstance(self.auto_read, bool):
            raise ValueError('Invalid automatic reading setting.')
        if not isinstance(self.voice_id, str) or not VOICE_ID_PATTERN.fullmatch(self.voice_id):
            raise ValueError('Invalid voice ID format.')
        if not isinstance(self.voice_id_zh, str) or \
                (self.voice_id_zh and not VOICE_ID_PATTERN.fullmatch(self.voice_id_zh)):
            raise ValueError('Invalid Chinese voice ID format.')
        if self.long_reply not in ("opening", "all"):
            raise ValueError('Invalid long-reply reading mode.')
        if self.eleven_model not in ELEVEN_MODELS:
            raise ValueError('Invalid ElevenLabs model.')
        if self.local_speaker not in LOCAL_SPEAKERS:
            raise ValueError('Invalid local Qwen3 voice.')
        if isinstance(self.volume, bool) or not isinstance(self.volume, (float, int)) or not 0 <= self.volume <= 1:
            raise ValueError('Volume must be 0-100%.')
        if isinstance(self.speed, bool) or not isinstance(self.speed, (float, int)) or not 0.7 <= self.speed <= 1.2:
            raise ValueError('Speed must be 0.7-1.2.')
        return self


def load_settings(data_dir):
    path = Path(data_dir) / SETTINGS_NAME
    if not path.exists():
        return TTSSettings()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError()
        raw.update(auto_read=True, long_reply='opening')
        return TTSSettings(**raw).validate()
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        raise TTSError('Cannot read speech settings. Check tts-settings.json.') from error


def save_settings(data_dir, settings):
    from dataclasses import replace
    settings = replace(settings, auto_read=True, long_reply='opening')
    settings.validate()
    folder = Path(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / SETTINGS_NAME
    temporary = folder / (SETTINGS_NAME + ".tmp")
    try:
        temporary.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        os.replace(str(temporary), str(path))
    except OSError as error:
        raise TTSError('Cannot save speech settings. Check data folder permissions.') from error


def speech_text(value):
    """Speak prose, omitting executable code and Markdown syntax."""
    value = re.sub(r"```[\s\S]*?(?:```|$)", ' Code block omitted. ', value)
    value = re.sub(r"~~~[\s\S]*?(?:~~~|$)", ' Code block omitted. ', value)
    value = re.sub(r"(?m)^\s{4,}[^\n]+$", ' Code block omitted. ', value)
    value = re.sub(r"!\[[^\]]*\]\([^)]*\)", ' Image omitted. ', value)
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"`[^`\n]+`", ' Code ', value)
    value = re.sub(r"https?://\S+", ' Link ', value)
    value = re.sub(r"(?m)^\s*(?:#{1,6}\s*|>\s*|[-*+]\s+|\d+[.)]\s+)", "", value)
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(value)
    value = re.sub(r"[*_~|]", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def speech_segments(text, max_chars=60):
    """Split cleaned prose at sentence boundaries for early local playback."""
    if not text:
        return []
    segments = []
    for sentence in re.split(r"(?<=[。！？!?；;])\s*|(?<=\.)\s+", text):
        sentence = sentence.strip()
        while len(sentence) > max_chars:
            boundary = max(sentence.rfind(mark, max_chars // 2, max_chars)
                           for mark in ("，", ",", "、", " "))
            cut = boundary + 1 if boundary >= 0 else max_chars
            segments.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if sentence:
            segments.append(sentence)
    return segments


def split_first(segments, short=16):
    """Speak the first clause on its own: a shorter first segment starts sooner."""
    if not segments or len(segments[0]) <= short:
        return segments
    first = segments[0]
    for index in range(6, len(first) - 4):
        if first[index] in "，,、：:；;":
            return [first[:index + 1], first[index + 1:].strip()] + segments[1:]
    return segments


class SpeechStream:
    """Turn streamed reply text into speakable sentences as soon as each one ends.

    Code fences are never cut in the middle, so speech_text() can still drop them.
    The total spoken length keeps the same 3000-character cap as manual reading.
    """

    _BOUNDARY = re.compile(r"[。！？!?；;\n]|\.(?=\s)")

    def __init__(self, limit=MAX_SPEECH_CHARS):
        self.raw = ""
        self.done = 0
        self.spoken = 0
        self.limit = limit

    def _closed_fences(self, text):
        return text.count("```") % 2 == 0 and text.count("~~~") % 2 == 0

    def _take(self, final):
        pending = self.raw[self.done:]
        cut = 0
        if final:
            cut = len(pending)
        else:
            for match in self._BOUNDARY.finditer(pending):
                if self._closed_fences(self.raw[:self.done + match.end()]):
                    cut = match.end()
            if not cut and self.spoken == 0 and self._closed_fences(self.raw):
                # Nothing spoken yet: release the first clause instead of waiting
                # for the whole first sentence.
                clause = re.search(r"[，,、：:]", pending[6:])
                if clause:
                    cut = 6 + clause.end()
            if not cut and len(pending) > 200 and self._closed_fences(self.raw):
                # A very long sentence: speak up to its last comma rather than wait.
                comma = max(pending.rfind(mark) for mark in ("，", ",", "、"))
                cut = comma + 1 if comma > 0 else 0
        if not cut:
            return []
        chunk, self.done = pending[:cut], self.done + cut
        segments = []
        pieces = speech_segments(speech_text(chunk))
        if self.spoken == 0:
            pieces = split_first(pieces)
        for segment in pieces:
            if re.fullmatch(r"\s*(?:\d{1,2}|[一二三四五六七八九十]{1,2})[.)、．]?\s*", segment):
                continue  # a bare list number is not worth saying on its own
            room = self.limit - self.spoken
            if room <= 0:
                break
            segment = segment[:room]
            self.spoken += len(segment)
            segments.append(segment)
        return segments

    def feed(self, delta):
        self.raw += delta
        return self._take(final=False)

    def finish(self):
        return self._take(final=True)

    def reset(self):
        """A new model turn: speak what is left of the old one, then start over."""
        remaining = self._take(final=True)
        self.raw, self.done = "", 0
        return remaining


# ---- what is said and what is only shown (2026-10-17) --------------------------------------
# For a long reply, MYN reads a short introductory line and displays the
# opening line and the rest is only shown (the reading panel). Short replies are read in full.
LONG_REPLY_CHARS = 160   # the same rule as the reading panel (web/app.js isLong)
OPENING_MAX = 120        # an opening that runs on is cut at the next sentence end


def is_long_reply(text):
    text = text or ""
    return len(text) > LONG_REPLY_CHARS or text.count("\n") >= 4 or "```" in text


class SpokenOpening:
    """SpeechStream for replies, saying only the opening of a long one.

    The opening is spoken as it streams in (no delay). It ends at the first blank line, at a
    line that starts a list / heading / table / code, or at the first sentence end after
    OPENING_MAX characters. Whatever follows is held back: when the reply turns out short
    after all it is said at the end, otherwise it is only shown. whole=True (setting "all")
    behaves exactly like SpeechStream."""

    _ENDS = "。！？!?"

    def __init__(self, whole=False, limit=MAX_SPEECH_CHARS):
        self.inner = SpeechStream(limit)
        self.whole = whole
        self.held = ""
        self.closed = False

    @property
    def raw(self):
        return self.inner.raw + self.held

    def _opening_has_words(self):
        return len(re.sub(r"\W", "", self.inner.raw)) >= 4

    def feed(self, delta):
        if self.whole:
            return self.inner.feed(delta)
        out = []
        for char in delta:
            if self.closed:
                self.held += char
                continue
            opening = self.inner.raw
            if self._opening_has_words():
                line_start = opening.endswith("\n")
                if char == "\n" and line_start:          # a blank line
                    self.closed, self.held = True, char
                    continue
                if line_start and (char in "-*•>#|`~" or char.isdigit()):
                    self.closed, self.held = True, char  # a list / heading / table / code starts
                    continue
            out += self.inner.feed(char)
            if char in self._ENDS and len(self.inner.raw) > OPENING_MAX:
                self.closed = True                     # a long first paragraph: one sentence is enough
        return out

    def finish(self):
        if self.whole or not self.closed:
            return self.inner.finish()
        if not is_long_reply(self.raw):                # short after all: say the rest too
            held, self.held, out = self.held, "", []
            for char in held:  # as if it had streamed in: lines stay separate sentences
                out += self.inner.feed(char)
            return out + self.inner.finish()
        return self.inner.finish()

    def reset(self):
        """A new model turn (after a tool): finish the old one, start over."""
        remaining = self.finish()
        self.inner = SpeechStream(self.inner.limit)
        self.held, self.closed = "", False
        return remaining


def spoken_part(text, whole=False):
    """What MYN says of a finished reply (auto-read): see SpokenOpening."""
    gate = SpokenOpening(whole)
    return " ".join(gate.feed(text) + gate.finish()).strip()


_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN = re.compile(r"[A-Za-z]")


def is_chinese(text):
    """Mostly Chinese? One Chinese character weighs about as much as a short English word."""
    cjk = len(_CJK.findall(text or ""))
    return cjk > 0 and cjk * 3 >= len(_LATIN.findall(text or ""))


def voice_for(text, settings):
    """The ElevenLabs voice for this text: the Chinese voice for Chinese, if one is set."""
    if settings.voice_id_zh and is_chinese(text):
        return settings.voice_id_zh
    return settings.voice_id


class TTSProvider(Protocol):
    def synthesize(self, text, settings):
        """Return complete encoded audio bytes or raise TTSError."""


class ElevenLabsProvider:
    def __init__(self, opener=None):
        self.opener = opener or urllib.request.urlopen

    def stream(self, text, settings, cancelled=lambda: False):
        """Yield aligned 16-bit mono PCM while HTTP is still delivering audio."""
        settings.validate()
        if not text or len(text) > MAX_SPEECH_CHARS:
            raise TTSError('Speech text must contain 1-3000 characters.')
        key = credentials.get_elevenlabs_key(user_data_dir())
        if not key:
            raise TTSError('ElevenLabs API key is not configured. Text replies remain available.')
        if cancelled():
            return
        request = urllib.request.Request(
            "https://api.elevenlabs.io/v1/text-to-speech/" + voice_for(text, settings) +
            "/stream?output_format=pcm_24000",
            data=json.dumps({"text": text, "model_id": settings.eleven_model,
                             "voice_settings": {"speed": settings.speed}}).encode("utf-8"),
            headers={"xi-api-key": key, "Content-Type": "application/json",
                     "Accept": "application/octet-stream"}, method="POST")
        try:
            with self.opener(request, timeout=30) as response:
                try:
                    import usage_stats
                    usage_stats.record_voice(user_data_dir(), settings.eleven_model, len(text),
                                             history_id=response.headers.get("history-item-id"))
                except Exception:
                    pass
                read = getattr(response, "read1", response.read)
                pending, total = bytearray(), 0
                chunk_bytes = 12000  # 250 ms; enough for a small playback buffer
                while not cancelled():
                    part = read(8192)
                    if not part:
                        break
                    total += len(part)
                    if total > MAX_AUDIO_BYTES:
                        raise TTSError('Speech audio exceeded 8 MB; reception stopped.')
                    pending.extend(part)
                    while len(pending) >= chunk_bytes:
                        if cancelled():
                            return
                        yield PCMChunk(bytes(pending[:chunk_bytes]))
                        del pending[:chunk_bytes]
                if cancelled():
                    return
                if not total or len(pending) % 2:
                    raise TTSError('Speech audio is empty or incomplete. The text reply was preserved.')
                if pending:
                    yield PCMChunk(bytes(pending))
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                raise TTSError('ElevenLabs authentication failed. Check the API key and account permissions.') from None
            if error.code == 429:
                raise TTSError('ElevenLabs rate or quota limit reached. Try again later.') from None
            raise TTSError('ElevenLabs streaming speech request failed. The text reply was preserved.') from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise TTSError('Speech network request failed. The text reply was preserved.') from None

    def synthesize(self, text, settings):
        settings.validate()
        if not text or len(text) > MAX_SPEECH_CHARS:
            raise TTSError('Speech text must contain 1-3000 characters.')
        try:
            key = credentials.get_elevenlabs_key(user_data_dir())
        except (OSError, UnicodeError, RuntimeError) as error:
            raise TTSError('Cannot read the saved ElevenLabs API key. Configure it again in voice settings.') from error
        if not key:
            raise TTSError('ElevenLabs API key is not configured. Set it in voice settings; text replies remain available.')
        body = json.dumps({"text": text, "model_id": settings.eleven_model,
                           "voice_settings": {"speed": settings.speed}}).encode("utf-8")
        request = urllib.request.Request(
            "https://api.elevenlabs.io/v1/text-to-speech/" + voice_for(text, settings) +
            "?output_format=mp3_44100_128", data=body,
            headers={"xi-api-key": key, "Content-Type": "application/json",
                     "Accept": "audio/mpeg"}, method="POST")
        try:
            with self.opener(request, timeout=30) as response:
                audio = response.read(MAX_AUDIO_BYTES + 1)
                try:  # ElevenLabs' id for this request: the daily sync then won't count it twice
                    history_id = response.headers.get("history-item-id")
                except AttributeError:
                    history_id = None
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                message = 'ElevenLabs authentication failed. Check the API key and account permissions.'
            elif error.code == 429:
                message = 'ElevenLabs rate or quota limit reached. Try again later.'
            elif error.code in (400, 422):
                message = 'ElevenLabs rejected the speech request. Check the voice ID and settings.'
            else:
                message = 'ElevenLabs is temporarily unavailable.'
            raise TTSError(message) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise TTSError('Speech network request failed. The text reply was preserved.') from error
        if not audio or len(audio) > MAX_AUDIO_BYTES:
            raise TTSError('Speech audio is empty or exceeds 8 MB; nothing played.')
        try:  # ElevenLabs bills by characters: count them for the usage statistics
            import usage_stats
            usage_stats.record_voice(user_data_dir(), settings.eleven_model, len(text),
                                     history_id=history_id if isinstance(history_id, str) else None)
        except Exception:
            pass
        return audio


class Qwen3LocalProvider:
    """Use the isolated, persistent Python 3.12 inference worker."""

    def synthesize(self, text, settings):
        settings.validate()
        from qwen3_local import client
        return client.synthesize(text, settings.local_speaker, settings.speed)

    def synthesize_chunks(self, text, settings):
        settings.validate()
        from qwen3_local import client
        return client.synthesize_chunks(speech_segments(text), settings.local_speaker, settings.speed)


def local_token():
    from qwen3_local import client
    return client.generation


def warm_local(token):
    from qwen3_local import client
    client.warm(token)


def cancel_local():
    from qwen3_local import client
    client.cancel()


def close_local():
    from qwen3_local import client
    client.close()


PROVIDERS = {"elevenlabs": ElevenLabsProvider, "qwen3-local": Qwen3LocalProvider}


def synthesize(text, settings):
    settings.validate()
    return PROVIDERS[settings.provider]().synthesize(text, settings)
