"""Small CPU-only streaming keyword detector; no microphone or cloud access.

Model: official sherpa-onnx bilingual 3M KWS, chunk-8 int8. Full ASR stays
in the existing GPU worker. This detects words, NOT the speaker's identity.
"""
from pathlib import Path
import re

MODEL = Path(__file__).resolve().parents[2] / 'local-stt' / 'kws' / 'sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20'


def keyword_tokens(words, folder=MODEL):
    from pypinyin import pinyin, Style
    lexicon = {}
    for line in (folder / 'en.phone').read_text(encoding='utf-8').splitlines():
        word, *phones = line.split()
        lexicon.setdefault(word.upper(), phones)
    tokens = {line.split()[0] for line in (folder / 'tokens.txt').read_text(encoding='utf-8').splitlines()}
    lines = []
    import stt
    for word in stt.wake_word_list(words):
        word = word.strip()
        if not word:
            continue
        # MYN is pronounced as a name, not a sequence of English letters.
        spoken = re.sub(r'\bMYN\b', '麦恩', word, flags=re.I)
        phones = []
        for part in re.findall(r'[\u4e00-\u9fff]|[A-Za-z\']+', spoken):
            if '\u4e00' <= part[0] <= '\u9fff':
                initial = pinyin(part, style=Style.INITIALS, strict=False)[0][0]
                final = pinyin(part, style=Style.FINALS_TONE, strict=False)[0][0]
                phones.extend(p for p in (initial, final) if p)
            else:
                if part.upper() not in lexicon:
                    raise ValueError('The wake word contains pronunciation unsupported by the model: ' + part)
                phones.extend(lexicon[part.upper()])
        if not phones or any(p not in tokens for p in phones):
            raise ValueError('Wake word pronunciation is unsupported by the current local model')
        lines.append(' '.join(phones) + ' @' + re.sub(r'\s+', '_', word))
    if not lines:
        raise ValueError('Wake word cannot be empty')
    return '\n'.join(lines)


class Detector:
    def __init__(self, words, folder=MODEL):
        import sherpa_onnx
        import tempfile
        suffix = 'epoch-13-avg-2-chunk-8-left-64'
        self.keywords = keyword_tokens(words, folder)
        with tempfile.TemporaryDirectory(prefix='myn-keywords-') as temporary:
            keywords = Path(temporary) / 'keywords.txt'
            keywords.write_text(self.keywords + '\n', encoding='utf-8')
            self.model = sherpa_onnx.KeywordSpotter(
                tokens=str(folder / 'tokens.txt'),
                encoder=str(folder / ('encoder-' + suffix + '.int8.onnx')),
                decoder=str(folder / ('decoder-' + suffix + '.onnx')),
                joiner=str(folder / ('joiner-' + suffix + '.int8.onnx')),
                keywords_file=str(keywords),
                num_threads=1, provider='cpu', keywords_threshold=.30,
                num_trailing_blanks=1)
        self.reset()

    def reset(self):
        self.stream = self.model.create_stream()

    def feed(self, audio, rate=16000):
        self.stream.accept_waveform(rate, audio)
        found = ''
        while self.model.is_ready(self.stream):
            self.model.decode_stream(self.stream)
            result = self.model.get_result(self.stream)
            if result:
                found = result.replace('_', ' ')
                self.model.reset_stream(self.stream)
        return found
