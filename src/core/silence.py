'Suppress replies to speech not addressed to MYN. Return MARK or recognize a reply that only steps aside, acknowledges singing or offers no substantive response. Such replies are neither displayed, spoken nor saved, and the conversation closes until the wake word. This prevents accidental acknowledgement loops with nearby conversations or lyrics.'

import re

MARK = "〔不回应〕"
_STEP_ASIDE = re.compile(
    r"不打扰|不插话|不插嘴|不出声|不吭声|先不说话|不吵你|不打断|安静(?:待着|一下)|"
    r"不是(?:在)?(?:跟|对|和|问)我说?|没在(?:跟|对|和)我说|"
    r"(?:在|正在)(?:跟|和)(?:别人|朋友|同学|室友|家人|队友)")
# Suppress replies consisting only of listening acknowledgements or encouragement to continue singing.
_LISTENING = re.compile(r"^(?:嗯+|哦+|噢+|嗯嗯|en|mm+|hmm+)?(?:你)?(?:继续|接着)?(?:唱|说|玩)?(?:吧|呀)?(?:我(?:在)?听着(?:呢)?)?$", re.I)
SENTENCE_END = re.compile(r"[。！？!?\n]")
HOLD_LIMIT = 60  # a voice answer longer than this is a real answer


def _bare(text):
    return re.sub(r"[\s（）()【】\[\]〔〕。，、,.!！?？…~～\"“”'‘’]", "", str(text or ""))


def is_silent(answer, voice=False):
    """True when the answer means "that wasn't for me": MARK (with nothing else of
    substance), or - for a sentence MYN heard - a short reply that only steps aside."""
    text = str(answer or "").strip()
    if not text:
        return False
    if MARK in text and len(_bare(text.replace(MARK, ""))) <= 12:
        return True
    if text.startswith(MARK):
        return True
    if not voice or len(text) > HOLD_LIMIT:
        return False
    return bool(_STEP_ASIDE.search(text) or _LISTENING.match(_bare(text)))


class Gate:
    """Holds the start of a streamed reply until it is clear it isn't a silent one, so a
    silent answer is never shown or read aloud. feed() returns what may be shown now."""

    def __init__(self, voice=False):
        self.voice = voice
        self.reset()

    def reset(self):
        self.held = ""
        self.open = False
        self.silent = False

    def _undecided(self):
        bare = self.held.lstrip()
        if not bare:
            return True
        if bare.startswith(MARK):
            return True  # silent; decided at once in feed()
        if MARK.startswith(bare[:len(MARK)]):
            return True  # still could be the mark
        if self.voice and len(bare) <= HOLD_LIMIT:
            # The first sentence decides; a reply beginning with a promise to stay quiet waits for more.
            return not SENTENCE_END.search(bare) or is_silent(bare, True)
        return False

    def feed(self, delta):
        if self.open:
            return delta
        if self.silent:
            return ""
        self.held += delta
        if self.held.lstrip().startswith(MARK):
            self.silent = True
            self.held = ""
            return ""
        if self._undecided():
            return ""
        self.open = True
        held, self.held = self.held, ""
        return held

    def flush(self):
        """End of the reply: what was still held (a short real answer), unless silent."""
        if self.open or self.silent:
            return ""
        held, self.held = self.held, ""
        if is_silent(held, self.voice):
            self.silent = True
            return ""
        return held
