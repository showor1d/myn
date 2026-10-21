"""Local sidecar lyrics. No network, model calls or changes to source files."""
import re
import unicodedata

EXTENSIONS = {".lrc", ".txt"}
LIMIT = 1024 * 1024
STAMP = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")
META = re.compile(r"\[(?:ar|al|ti|au|by|re|ve|length|offset):[^\]]*\]", re.I)


def key(stem):
    return unicodedata.normalize("NFKC", stem).strip().casefold()


def match(song):
    candidates = [p for p in song.parent.iterdir() if p.suffix.lower() in EXTENSIONS
                  and key(p.stem) == key(song.stem) and p.is_file() and not p.is_symlink()
                  and p.resolve().parent == song.parent]
    for extension in (".lrc", ".txt"):
        found = [p for p in candidates if p.suffix.lower() == extension]
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            return None  # Do not guess between conflicting normalized names.
    return None


def read(path):
    with path.open("rb") as stream:
        data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError('Lyrics exceed 1 MB. Use a smaller text file.')
    encodings = ["utf-16"] if data.startswith((b"\xff\xfe", b"\xfe\xff")) else ["utf-8-sig", "gb18030"]
    for encoding in encodings:
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError('Unknown lyrics encoding. Save the text as UTF-8.')
    if "\0" in text:
        raise ValueError('Lyrics file is not valid text.')
    return parse(text, path.suffix.lower() == ".lrc")


def parse(text, timed=True):
    offsets = re.findall(r"\[offset:([+-]?\d+)\]", text, re.I)
    offset = int(offsets[-1]) / 1000 if offsets else 0
    groups = {}
    if timed:
        for line in text.splitlines():
            stamps = STAMP.findall(line)
            if not stamps:
                continue
            words = META.sub("", STAMP.sub("", line))
            words = re.sub(r"<\d{1,3}:\d{2}(?:[.:]\d{1,3})?>", "", words).strip()
            for minutes, seconds, fraction in stamps:
                if int(seconds) >= 60:
                    continue
                at = max(0, int(minutes)*60 + int(seconds) + (int(fraction)/10**len(fraction) if fraction else 0) - offset)
                groups.setdefault(at, [])
                if words not in groups[at]:
                    groups[at].append(words)
    if groups:
        return dict(timed=True, lines=[{"time": at, "text": "\n".join(groups[at])}
                                      for at in sorted(groups)])
    return dict(timed=False, lines=[{"time": None, "text": line}
                                   for line in META.sub("", text).strip().splitlines()])
