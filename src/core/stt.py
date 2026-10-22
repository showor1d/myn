"""Push-to-talk speech input: settings, transcript cleanup and the local worker client.

Recording and Whisper inference run in a separate Python 3.12 process (stt_worker.py)
inside D:\\MYN\\local-tts\\.venv, so MYN's Python 3.9 process gains no dependencies.
This module itself uses only the standard library and is imported by both sides.
"""

import json
import os
import queue
import re
import subprocess
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from worker_process import terminate as _terminate

ROOT = Path(__file__).resolve().parents[2]
from runtime_paths import worker_python
PYTHON = worker_python('speech')
MODEL = ROOT / "local-stt" / "whisper-large-v3-turbo"
WORKER = Path(__file__).with_name("stt_worker.py")
SETUP_SCRIPT = ROOT / "setup_voice_input.ps1"
SETTINGS_NAME = "stt-settings.json"
SETTINGS_LOCK = threading.RLock()
LANGUAGES = ("auto", "zh", "en", "ja")
# Automatic mode compares these languages, retaining recent language for uncertain short replies.
AUTO_CANDIDATES = ("zh", "en", "ja")
LANGUAGE_LABELS = {"auto": 'Auto (Chinese / English / Japanese)', "zh": 'Chinese only', "en": "English only",
                   "ja": 'Japanese only'}
MAX_SECONDS = 30


class LanguageContinuity:
    """Hold recent language for uncertain short replies; strong evidence can switch."""
    def __init__(self):
        self.language, self.at = None, 0.0

    def choose(self, scores, seconds, now):
        ranked = sorted(scores, key=scores.get, reverse=True)
        best = ranked[0]
        margin = scores[best] - scores[ranked[1]]
        recent = self.language if now-self.at < 120 else None
        selected = recent if recent and seconds < 2.5 and margin < 2.0 else best
        if seconds >= 1.5 or margin >= 2.0 or selected == recent:
            self.language, self.at = selected, now
        return selected


# Whisper follows the style of its prompt; this keeps Mandarin output in simplified script.
ZH_PROMPT = "以下是普通话的句子，使用简体中文。"
# Whole-utterance phrases Whisper is known to invent for silence or noise
# (matched with spaces and punctuation removed).
_HALLUCINATIONS = re.compile(
    r"^(?:谢谢观看|謝謝觀看|谢谢收看|感谢观看|字幕由?amara\.org社区提供|"
    r"(?:有)?请不吝点赞订阅转发打赏支持明镜与点点栏目|明镜需要您的支持.*|"
    r"thankyouforwatching|thanksforwatching|subtitlesby.*|"
    r"ご視聴ありがとうございました|字幕.*提供)$", re.I)


# Whisper writes "Jarvis" in Chinese several ways; all count as the wake word.
DEFAULT_WAKE_WORDS = "Hey Mine"
FOLLOW_UP_SECONDS = 8  # after a bare wake word, the next utterance is addressed to MYN


def wake_word_list(text):
    text = re.sub(r'hey\s*[,，]\s*mine', 'Hey Mine', text or '', flags=re.I)
    return [word.strip() for word in re.split(r"[,，、;；\n]", text or "") if word.strip()][:40]


# Words too common to serve as a wake word on their own, however they were heard.
_TOO_COMMON = {"my", "mine", "me", "main", "man", "may", "hi", "hey", "ok", "okay", "yes", "no",
               "the", "and", "i", "you", "oh", "ah", "嗯", "啊", "哦", "我", "你", "他", "她", "好", "是", "对"}


def learned_form(transcript):
    """How a spoken wake word came out of speech recognition, usable as a wake word:
    the whole short transcript without punctuation, or "" if it can't be one."""
    form = re.sub(r"[\s,，。.!！?？、:：;；~～\-—…\"'“”‘’()（）]+", " ", transcript or "").strip()
    if not form or len(form) > 12 or form.casefold() in _TOO_COMMON:
        return ""
    if len(form) < 2 and not re.search(r"[\u4e00-\u9fff]{1}", form):
        return ""
    return form


# How speech recognition tends to write a name it doesn't know. With automatic
# Language detection may transcribe the wake name as an English phonetic equivalent:
# those count as the wake word too. (Collected from real transcripts.)
# Simplified and traditional Chinese phonetic wake spellings recognized by Whisper.
_MAIEN_CHINESE = ("麦恩", "迈恩", "卖恩", "买恩", "麦嗯", "迈嗯", "脉恩", "麦安", "万恩", "曼恩", "满恩",
                  "蛮恩", "慢恩", "埋恩", "麥恩", "邁恩", "賣恩", "買恩", "萬恩", "脈恩", "滿恩")
_MAIEN_ENGLISH = ("Maya", "Maia", "Mya", "Maen", "Mayn", "Myen", "Myan", "Mai En", "Maien", "Mayen",
                  # 2026-10-17: more English spellings of a short "MYn"
                  "Mein", "Mane", "Mayne", "Myne", "Mien", "Meyen")
SOUND_ALIKES = {
    "麦恩": ("麦恩", "迈恩", "卖恩", "买恩", "麦嗯", "脉恩", "麦安", "MYN") + _MAIEN_ENGLISH + _MAIEN_CHINESE,
    "myn": ("MYN",) + _MAIEN_ENGLISH + _MAIEN_CHINESE,
}
# Two syllables resembling MYN at the very start of speech.
# Restrict these observed phonetic variants to the start of the sentence.
# Exclude real words and place names that would cause false wakes.
_LOOSE_FIRST = "麦迈卖买埋马玛曼满万蛮慢脉麥邁賣買萬滿脈"
_LOOSE_SECOND = "恩嗯安眼亚雅尤尔因音银印伊言颜羊扬洋"
_LOOSE_WAKE = (r"[%s]\s*[%s](?!代夫|科夫|克思|克斯|他|她|斯)" % (_LOOSE_FIRST, _LOOSE_SECOND))
_LOOSE_NOT = ("买烟", "卖烟", "万安", "万言", "满眼", "马扬", "蛮安")
_GREETING = r"^\s*(?:(?:hey|hi|ok|okay|嘿|嗨|喂|哎|诶)[\s,，、!！]*)?"


def _loose_applies(wake_words):
    return any(word.casefold() in SOUND_ALIKES for word in wake_word_list(wake_words))


def _loose_match(text):
    match = re.match(_GREETING + "(" + _LOOSE_WAKE + ")", text or "", re.I)
    if match and re.sub(r"\s", "", match.group(1)) not in _LOOSE_NOT:
        return match
    return None


def _wake_forms(wake_words):
    forms = []
    for word in wake_word_list(wake_words):
        forms.append(word)
        forms.extend(SOUND_ALIKES.get(word.casefold(), ()))
    return sorted(set(forms), key=len, reverse=True)


def show_wake(text, wake_words):
    'Normalize recognized wake-word spellings to the configured first wake word, including typed, phonetic and voice-learned variants anywhere in the text.'
    words = wake_word_list(wake_words)
    if not text or not words:
        return text or ""
    name = words[0]
    forms = [form for form in _wake_forms(wake_words)
             if form != name and form.casefold() not in _TOO_COMMON]
    if not forms:
        return text
    parts = []
    for form in forms:
        pattern = r"\s*".join(re.escape(piece) for piece in form.split())
        if form[0].isascii():
            pattern = r"(?<![A-Za-z])" + pattern
        if form[-1].isascii():
            pattern += r"(?![A-Za-z])"
        parts.append(pattern)
    text = re.sub("|".join(parts), name, text, flags=re.I)
    if _loose_applies(wake_words):
        match = _loose_match(text)
        if match:  # Replace only a leading phonetic name, preserving the command that follows.
            text = text[:match.start(1)] + name + text[match.end(1):]
    return text


def _squash(text):
    return re.sub(r"[\s,，。.!！?？、:：;；~～\-—…\"'“”‘’]", "", text).casefold()


def echo_overlap(heard, spoken):
    "Return the share (0..1) of heard text contained in verbatim runs of at least four characters from MYN's recent speech. Shared topic words are insufficient. Short text must match an entire spoken segment, not a common word inside a longer explanation."
    heard = _squash(heard or "")
    said = _squash(" ".join(spoken or []))
    if not heard:
        return 1.0
    if len(heard) < 4:
        return float(any(heard == _squash(part) for part in (spoken or [])))
    if not said:
        return 0.0
    covered = [False] * len(heard)
    for i in range(len(heard) - 3):
        if heard[i:i + 4] in said:
            for j in range(i, i + 4):
                covered[j] = True
    return sum(covered) / len(heard)


def strip_wake(text, wake_words):
    'Return text after a leading wake word, or an empty string if no content follows. A short greeting may precede it. Return None for speech not addressed to MYN, including names mentioned later in a sentence.'
    if not text:
        return None
    for word in _wake_forms(wake_words):
        pattern = r"^\s*(?:(?:hey|hi|ok|okay|嘿|嗨|喂|哎|诶)[\s,，、!！]*)?" + \
            r"[\s,，、]*".join(re.escape(char) for char in word if not char.isspace())
        if word[-1:].isascii() and word[-1:].isalpha():
            pattern += r"(?![A-Za-z])"  # "Maya" wakes, "Mayan ruins" does not
        match = re.match(pattern, text, re.I)
        if match:
            rest = text[match.end():]
            return re.sub(r"^[\s,，。.!！?？、:：;；~～\-—…]+", "", rest).strip()
    if _loose_applies(wake_words):
        match = _loose_match(text)
        if match:
            return re.sub(r"^[\s,，。.!！?？、:：;；~～\-—…]+", "", text[match.end():]).strip()
    return None


def exact_wake(text, wake_words):
    'True only for a leading configured or voice-learned wake word, not loose phonetic or acoustic matches. Loose matches can occur in songs, games and nearby conversations and must be assessed as possibly not addressed to MYN.'
    if not text:
        return False
    for word in sorted(wake_word_list(wake_words), key=len, reverse=True):
        pattern = r"^\s*(?:(?:hey|hi|ok|okay|嘿|嗨|喂|哎|诶)[\s,，、!！]*)?" + \
            r"[\s,，、]*".join(re.escape(char) for char in word if not char.isspace())
        if word[-1:].isascii() and word[-1:].isalpha():
            pattern += r"(?![A-Za-z])"
        if re.match(pattern, text, re.I):
            return True
    return False


# Spoken commands handled on the computer: (action, pattern).
_VOICE_COMMANDS = (
    ("media_player", r"^(?:打开|开一下|调出)(?:一下)?(?:音乐|视频)?播放器$|^open (?:the )?(?:media |music |video )?player$"),
    ("voice_settings", r"(?:打开|开一下|调出).{0,4}(?:声音|语音|朗读)设置|open (?:the )?(?:voice|sound|audio) settings"),
    ("agents", r"(?:打开|开一下|调出).{0,4}agents?|open (?:the )?agents?"),
    ("personal", r"(?:打开|开一下|调出|看看|看一下).{0,4}(?:(?:个人)?资料库|备份)|open (?:the )?(?:personal )?(?:library|data|backups?)"),
    ("input_open", r"(?:打开|显示|调出|开一下|拿出).{0,3}(?:输入栏|输入框|键盘|打字)|open (?:the )?(?:input|keyboard|text box)"),
    ("input_close", r"(?:关闭|关掉|隐藏|收起|收掉).{0,3}(?:输入栏|输入框|键盘)|(?:close|hide) (?:the )?(?:input|keyboard|text box)"),
    ("about", r"(?:打开|开一下|调出|看看).{0,4}(?:关于我|关于你|画像|我的档案)|open (?:the )?(?:profile|about me)"),
    ("read_all", r"^(?:把)?(?:它|这个|这些|全文|刚才的|刚才那段|上面的)?(?:全部|全都|都|整段|完整地?)?(?:读|念)(?:出来|一遍|给我听)(?:吧)?$|^(?:读|念)(?:一下)?(?:全文|全部)$|^(?:全部|全都|都)(?:读|念)(?:一下)?$"),
    ("reader_close", r"^(?:关掉|关闭|收起|关上|收回去)(?:一下)?(?:这个|那个)?(?:阅读框|全文|文字框|悬浮框|框)$|^把(?:这个|那个)?(?:阅读框|全文|文字框|悬浮框|框)(?:关掉|关了|关上|收起来)$"),
    ("reader_open", r"^(?:打开|展开|显示)(?:一下)?(?:阅读框|全文|文字框|悬浮框)$"),
    ("usage", r"(?:打开|开一下|调出|看看|看一下|显示).{0,4}(?:花销|消费|支出|用量|token|托肯|费用)(?:统计|记录|图表)?|open (?:the )?(?:usage|expenses)"),
    ("preferences", r"(?:打开|开一下|调出|看看|看一下).{0,4}交流偏好|open (?:the )?(?:communication )?preferences"),
    ("agent_stop", r"^(?:停止|取消|结束|停掉|停下)(?:后台)?任务$|^stop (?:the )?task$"),
    ("agent_log", r"(?:打开|看看|看一下|显示).{0,2}(?:后台)?任务(?:记录|进度)?|show (?:the )?task"),
    ("settings", r"(?:打开|开一下|调出).{0,2}设置|open (?:the )?settings"),
    ("stop", r"^(?:停|停下|停止|安静|别说了|别讲了|闭嘴|好了别说了|stop|be quiet|shut up)$"),
)


def voice_command(text):
    """The local action a spoken sentence asks for, or None for ordinary speech."""
    if not text or len(text) > 30:
        return None
    if "进化" in text or re.search(r"\bevolution\b", text, re.I):
        return None  # conversation tools interpret it; there is no evolution settings page
    if re.fullmatch(r"(?:停止|取消|结束|停掉|停下)(?:后台)?任务|stop (?:the )?task", text, re.I):
        return "agent_stop"  # cancellation still works offline
    if any(word in text for word in ("后台", "任务设置", "预算", "限额", "工作文件夹", "任务记录", "任务进度")):
        return None  # preserve the whole natural request for settings/status tools
    for action, pattern in _VOICE_COMMANDS:
        target = _squash(text) if action == "stop" else text
        if re.search(pattern, target, re.I):
            return action
    return None


class STTError(Exception):
    """A user-facing speech-input error without audio or transcript content."""


@dataclass(frozen=True)
class STTSettings:
    enabled: bool = True
    language: str = "auto"
    device: str = ""  # microphone name; "" = Windows default input
    always_listen: bool = True  # keep the microphone open and note everything heard
    wake_words: str = DEFAULT_WAKE_WORDS  # fixed production phrase; addressing still requires context
    wake_learned: str = ""  # how your own voice saying the wake word was transcribed

    @property
    def all_wake_words(self):
        """The wake words as typed plus the forms learned from your voice."""
        return self.wake_words + ("," + self.wake_learned if self.wake_learned else "")

    def validate(self):
        if not isinstance(self.enabled, bool):
            raise ValueError('Invalid voice input toggle.')
        if self.language not in LANGUAGES:
            raise ValueError('Invalid recognition language.')
        if not isinstance(self.device, str) or len(self.device) > 200:
            raise ValueError('Invalid microphone name.')
        if not isinstance(self.always_listen, bool):
            raise ValueError('Invalid always-on microphone setting.')
        if not isinstance(self.wake_words, str) or len(self.wake_words) > 200 or \
                not wake_word_list(self.wake_words):
            raise ValueError('Enter at least one wake word, separated by commas.')
        if not isinstance(self.wake_learned, str) or len(self.wake_learned) > 400:
            raise ValueError('Invalid learned wake-word spelling.')
        return self


def load_settings(data_dir):
    path = Path(data_dir) / SETTINGS_NAME
    if not path.exists():
        return STTSettings()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError()
        raw.update(enabled=True, always_listen=True, wake_words=DEFAULT_WAKE_WORDS, wake_learned='')
        return STTSettings(**raw).validate()
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        raise STTError('Cannot read voice input settings. Check stt-settings.json.') from error


WAKE_VOICE_NAME = "wake-voice.json"
WAKE_VOICE_MIN = 3
WAKE_VOICE_MAX = 8
WAKE_SENSITIVITIES = ("low", "medium", "high")


def _valid_template(template):
    return (isinstance(template, list) and 8 <= len(template) <= 400 and
            all(isinstance(row, list) and len(row) == 13 and
                all(isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) < 1e3 for v in row)
                for row in template))


def load_wake_voice(data_dir):
    """Your wake-word voice samples: {"templates": [...], "sensitivity": ...}.
    Only acoustic numbers are stored (never audio). Missing or broken -> empty."""
    # Compatibility entry point; the GUI uses the strict loader to show corruption.
    import wake_learning
    try:
        return wake_learning.active(data_dir)
    except (OSError, ValueError, TypeError):
        return {"templates": [], "sensitivity": "medium"}


def save_wake_voice(data_dir, templates, sensitivity="medium"):
    templates = [t for t in templates if _valid_template(t)][-WAKE_VOICE_MAX:]
    if templates and len(templates) < WAKE_VOICE_MIN:
        raise STTError('At least %d clear wake-word recordings are required.' % WAKE_VOICE_MIN)
    if sensitivity not in WAKE_SENSITIVITIES:
        raise STTError('Invalid acoustic wake sensitivity.')
    import wake_learning
    try:
        with wake_learning.LOCK:
            data = wake_learning._empty()
            data.update(anchors=templates, sensitivity=sensitivity)
            wake_learning._save(data_dir, data)
    except OSError as error:
        raise STTError('Cannot save acoustic wake settings. Check data folder permissions.') from error


def wake_syllables(wake_words):
    "Count Chinese characters in the wake word's written form."
    lengths = [len(form) for form in _wake_forms(wake_words)
               if re.fullmatch(r"[\u4e00-\u9fff]{1,4}", form)]
    return max(set(lengths), key=lengths.count) if lengths else 2


def voice_wake_rest(text, syllables=2):
    'Extract speech after an acoustically recognized wake word despite transcript variation. Remove the leading name up to a pause, a leading English word, or the first syllables Chinese characters.'
    text = (text or "").strip()
    match = re.match(r"^[^，,。.!！?？、\s]{1,8}[，,。.!！?？、\s]+(.*)$", text, re.S) or \
        re.match(r"^[A-Za-z]{2,8}\s*([\u3040-\u30ff\u4e00-\u9fff].*)$", text, re.S) or \
        re.match(r"^[\u3040-\u30ff\u4e00-\u9fff]{%d}(.*)$" % max(1, syllables), text, re.S)
    return match.group(1).lstrip(" ，,。.!！?？、").strip() if match else text


def save_settings(data_dir, settings):
    with SETTINGS_LOCK:
        _save_settings(data_dir, settings)


def _save_settings(data_dir, settings):
    from dataclasses import replace
    settings = replace(settings, enabled=True, always_listen=True,
                       wake_words=DEFAULT_WAKE_WORDS, wake_learned='')
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
        raise STTError('Cannot save voice input settings. Check data folder permissions.') from error


# Music, game sounds and noise come out as one syllable over and over
# ("ほほほほ…", "Da da da da…", "Dun-dun-dun-dun", "ダンダンダン…").
_REPEATED = re.compile(r"^.{0,4}?(.{1,4}?)\1{5,}.{0,2}$", re.S)
# Sounds rather than words; on their own they say nothing to MYN or about you.
_FILLERS = re.compile(r"^(?:oh+|o+h|uh+|um+|ah+|aw+|wow+|hmm+|huh|oops|ooh+|whoa|ha(?:ha)*|"
                      r"あ+|ああ+|お+|おー+|うわ+|え+|ほ+|啊+|哦+|噢+|呃+|额+|哇+|哈+|唉+|诶+)$", re.I)


def clean_transcript(text):
    """Normalise a raw Whisper transcript; return "" for silence-style inventions
    and for noise that comes out as one syllable repeated."""
    if not isinstance(text, str):
        return ""
    text = text.replace(ZH_PROMPT, "")
    text = re.sub(r"\s+", " ", text).strip()
    bare = re.sub(r"[\s。，、！？!?,.…\-—~·\"'“”‘’()（）\[\]【】ー]", "", text)
    if not bare or _HALLUCINATIONS.match(bare) or (len(bare) >= 6 and _REPEATED.match(bare.casefold())):
        return ""
    return text[:2000]


_BACKCHANNEL = re.compile(r"^(?:嗯+|恩+|对+|是+|好+|哦+|噢+|啊+|呃+|嗯嗯|对对|好的|行|ok|okay|yeah|yes|right|uh huh|mm+|hmm+|うん|はい)$", re.I)


def is_backchannel(text):
    'A brief listening acknowledgement while MYN speaks is not an interruption.'
    bare = re.sub(r"[\s。，、！？!?,.…\-—~·\"'“”‘’ー]", "", text or "")
    return not bare or bool(_BACKCHANNEL.match(bare))


def is_social_fragment(text):
    """A standalone courtesy is not enough evidence to open a voice conversation.

    Do not remove it from transcripts: explicit wake and push-to-talk remain valid.
    """
    bare = re.sub(r"[\W_]+", "", text or "", flags=re.UNICODE).casefold()
    return bare in {"thankyou", "thanks", "thankyouverymuch", "goodnight", "bye",
                    "goodbye", "yourewelcome", "谢谢", "谢谢你", "多谢", "晚安", "再见",
                    "不客气", "ありがとう", "ありがとうございます", "おやすみ", "おやすみなさい"}


def is_filler(text):
    'An interjection alone has nothing to answer or remember.'
    bare = re.sub(r"[\s。，、！？!?,.…\-—~·\"'“”‘’ー]", "", text or "")
    return bool(bare) and bool(_FILLERS.match(bare))


def missing_parts(python=PYTHON, model=MODEL):
    """What the one-time setup still has to provide, in user-facing words."""
    missing = []
    if not Path(python).is_file():
        missing.append('Local speech runtime')
    elif not (Path(python).parents[1] / "Lib" / "site-packages" / "sounddevice.py").is_file():
        missing.append('sounddevice recording component')
    if not all((Path(model) / name).is_file()
               for name in ("config.json", "model.safetensors", "preprocessor_config.json")):
        missing.append('Whisper recognition model')
    return missing


def _discard(process):
    """Terminate a worker and release its pipes."""
    if process is None:
        return
    _terminate(process)
    try:
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass
    for pipe in (process.stdin, process.stdout):
        try:
            pipe.close()
        except (OSError, AttributeError):
            pass


class LocalSTTClient:
    """One persistent recorder/recogniser process; every request is serialised.

    A reader thread separates the worker's replies from unrequested events
    (utterances heard while always-on listening), which go to on_event.
    """

    def __init__(self, command=None, check_installed=None):
        self.lock = threading.Lock()
        self.process = None
        self.replies = None
        self.ready = False
        self.on_event = None  # called on the reader thread with each event dict
        self.listen_args = None  # restored automatically if the worker restarts
        self.wake_args = None  # wake-by-voice templates, also restored after a restart
        self.command = command or [str(PYTHON), "-u", str(WORKER), str(MODEL)]
        self.check_installed = check_installed or self._check_installed

    @staticmethod
    def _check_installed():
        missing = missing_parts()
        if missing:
            raise STTError('Voice input is not installed (missing: %s). Run %s once in PowerShell.'
                           % ("、".join(missing), SETUP_SCRIPT))

    def _start(self):
        self.check_installed()
        env = os.environ.copy()
        env["HF_HUB_OFFLINE"] = "1"
        env["TRANSFORMERS_OFFLINE"] = "1"
        try:
            self.process = subprocess.Popen(
                self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, cwd=str(Path(self.command[2]).parent), env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as error:
            raise STTError('Cannot start speech recognition. Check the local speech runtime.') from error
        self.ready = False
        self.replies = queue.Queue()
        threading.Thread(target=self._read, args=(self.process, self.replies), daemon=True).start()

    def _read(self, process, replies):
        try:
            for line in iter(lambda: process.stdout.readline(65536), b""):
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if isinstance(message, dict) and "event" in message:
                    handler = self.on_event
                    if handler is not None:
                        try:
                            handler(message)
                        except Exception:
                            pass
                else:
                    replies.put(message)
        except (OSError, ValueError):
            pass
        replies.put(None)  # the worker has gone

    def _exchange(self, process, replies, payload, timeout):
        try:
            process.stdin.write(json.dumps(payload).encode("utf-8") + b"\n")
            process.stdin.flush()
            return replies.get(timeout=timeout)
        except (OSError, ValueError, queue.Empty):
            return None

    def _request(self, payload, timeout):
        with self.lock:
            fresh = self.process is None or self.process.poll() is not None
            if fresh:
                self._start()
            process, replies = self.process, self.replies
            if fresh and self.wake_args and payload.get("command") != "wake_voice":
                self._exchange(process, replies, dict(self.wake_args, command="wake_voice"), timeout)
            if fresh and self.listen_args and payload.get("command") not in ("listen", "unlisten"):
                # A restarted worker resumes always-on listening before anything else.
                self._exchange(process, replies, dict(self.listen_args, command="listen"), timeout)
            reply = self._exchange(process, replies, payload, timeout)
            if reply is None:
                _discard(process)
                self.process = None
                raise STTError('Speech recognition stopped or timed out; it will restart automatically next time voice is used.')
            if not isinstance(reply, dict):
                raise STTError('Speech recognition returned an invalid result.')
            if not reply.get("ok"):
                raise STTError(str(reply.get("error") or 'Speech recognition failed.')[:200])
            self.ready = True
            return reply

    @property
    def alive(self):
        return self.process is not None and self.process.poll() is None

    def warm(self):
        """Load Whisper ahead of the first utterance (first load can take a while)."""
        return self._request({"command": "ready"}, 300)

    def start(self, device=""):
        """Begin push-to-talk; the reply says which microphone was used and if it fell back."""
        return self._request({"command": "start", "device": device}, 300)

    def listen(self, device="", language="auto", wake_words=None):
        """Keep the microphone open; utterances arrive through on_event."""
        if language not in LANGUAGES:
            raise STTError('Invalid recognition language.')
        self.listen_args = {"device": device, "language": language}
        if wake_words is not None:
            self.listen_args['wake_words'] = wake_words
        return self._request(dict(self.listen_args, command="listen"), 300)

    def unlisten(self):
        self.listen_args = None
        if self.alive:
            self._request({"command": "unlisten"}, 10)

    def attention(self, seconds=30, short_reply=False):
        """Temporarily accept softer short replies during an addressed conversation."""
        if self.alive:
            self._request({"command": "attention", "seconds": max(0, min(120, seconds)),
                           "short_reply": bool(short_reply)}, 10)

    def devices(self):
        """Names of the input devices the worker can open."""
        names = self._request({"command": "devices"}, 300).get("devices", [])
        return [name for name in names if isinstance(name, str)][:50]

    def audio_status(self):
        if not self.alive:
            return dict(active=False, actual_device=None, mode="inactive")
        return self._request({"command": "audio_status"}, 15)

    def switch_microphone(self, device):
        result = self._request({"command": "switch_microphone", "device": device}, 20)
        if self.listen_args is not None:
            self.listen_args = dict(self.listen_args, device=device)
        return result

    def stop(self, language):
        if language not in LANGUAGES:
            raise STTError('Invalid recognition language.')
        text = self._request({"command": "stop", "language": language}, 90).get("text", "")
        return clean_transcript(text)

    def stop_enroll(self, language):
        """Stop push-to-talk for wake-word training: (text, template of how it sounded)."""
        if language not in LANGUAGES:
            raise STTError('Invalid recognition language.')
        reply = self._request({"command": "stop", "language": language, "enroll": True}, 90)
        template = reply.get("template")
        return clean_transcript(reply.get("text", "")), template if isinstance(template, list) else []

    def set_wake_voice(self, templates, sensitivity="medium"):
        """Wake by your own voice (templates from stop_enroll); an empty list turns it off."""
        args = {"templates": list(templates or []), "sensitivity": sensitivity}
        self.wake_args = args if args["templates"] else None
        if args["templates"] or self.alive:
            return self._request(dict(args, command="wake_voice"), 60)
        return {"ok": True, "wake_voice": False}

    def cancel(self):
        if self.process is not None and self.process.poll() is None:
            self._request({"command": "cancel"}, 10)

    def close(self):
        self.listen_args = None
        process, self.process = self.process, None
        self.ready = False
        if process is not None:
            try:
                process.stdin.close()  # lets the worker exit on its own first
            except OSError:
                pass
            _discard(process)


client = LocalSTTClient()
