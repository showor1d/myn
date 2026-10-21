"""Local personal media: atomic copies and a loopback-only, range-capable stream."""
import hashlib
import mimetypes
import os
import re
import secrets
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
import lyrics

AUDIO = {".mp3", ".wav", ".ogg", ".oga", ".flac", ".m4a", ".aac", ".opus"}
VIDEO = {".mp4", ".webm", ".m4v", ".mov", ".ogv", ".mkv", ".avi"}
CATEGORIES = {"music": ("Music", "music"), "audio": ("Audio", "music"),
              "video": ("Videos", "video"), "movie": ("Movies", "video")}


class MediaLibrary:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.folders = {category: self.root / spec[0] for category, spec in CATEGORIES.items()}
        for folder in self.folders.values():
            folder.mkdir(parents=True, exist_ok=True)
            if folder.resolve().parent != self.root:
                raise ValueError('Media folders must remain inside the media library.')
        self.lock = threading.RLock()
        self.entries = {}

    @staticmethod
    def mode(path):
        suffix = Path(path).suffix.lower()
        return "music" if suffix in AUDIO else "video" if suffix in VIDEO else None

    def scan(self):
        rows, entries = [], {}
        with self.lock:
            for mode, folder in self.folders.items():
                for path in folder.iterdir():
                    if (not path.is_file() or path.is_symlink() or
                            path.resolve().parent != folder or self.mode(path) != CATEGORIES[mode][1]):
                        continue
                    try:
                        size = path.stat().st_size
                    except OSError:
                        continue
                    ident = hashlib.sha256((mode + "/" + path.name).encode("utf-8")).hexdigest()
                    entries[ident] = path
                    rows.append(dict(id=ident, name=path.name, mode=mode, size=size))
                    if mode == "music":
                        sidecar = lyrics.match(path)
                        rows[-1]["lyrics_file"] = sidecar.name if sidecar else None
            self.entries = entries
        return sorted(rows, key=lambda item: item["name"].casefold())

    def resolve(self, ident):
        with self.lock:
            path = self.entries.get(ident)
            if path and not path.is_symlink() and path.is_file() and \
                    path.resolve().parent in self.folders.values():
                return path
        return None

    def lyrics(self, ident):
        with self.lock:
            song = self.resolve(ident)
            if song is None or song.parent != self.folders["music"]:
                return dict(file=None, timed=False, lines=[])
            path = lyrics.match(song)
            if path is None:
                return dict(file=None, timed=False, lines=[])
            try:
                return dict(file=path.name, **lyrics.read(path))
            except (OSError, ValueError) as error:
                return dict(file=path.name, timed=False, lines=[], error=str(error))

    @staticmethod
    def digest(path):
        h = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(block)
        return h.digest()

    def add(self, paths, category=None):
        if category is not None and category not in CATEGORIES:
            raise ValueError('Choose Music, Audio, Video or Movies.')
        added, errors = [], []
        with self.lock:
            sources = [Path(p) for p in paths]
            companions = {}
            for p in sources:
                if p.suffix.lower() in lyrics.EXTENSIONS and p.is_file():
                    companions.setdefault((p.parent.resolve(), lyrics.key(p.stem)), []).append(p)
            destinations, rejected = {}, set()
            # Songs first, so paired lyrics follow the actual collision-safe name.
            for value in sorted(sources, key=lambda p: p.suffix.lower() in lyrics.EXTENSIONS):
                path = Path(value)
                sidecar = path.suffix.lower() in lyrics.EXTENSIONS
                source_key = (path.parent.resolve(), lyrics.key(path.stem))
                mode = "music" if sidecar else self.mode(path)
                if not mode or not path.is_file():
                    errors.append('%s: unsupported audio or video file' % path.name)
                    continue
                if sidecar and category not in (None, "music"):
                    errors.append('%s: place lyrics in Music' % path.name)
                    continue
                if sidecar and source_key in rejected:
                    errors.append('%s: the matching song was not saved; lyrics remain at their original location' % path.name)
                    continue
                if category is not None:
                    if mode != CATEGORIES[category][1]:
                        errors.append('%s: file type does not match the category; select the corresponding audio or video category' % path.name)
                        continue
                    mode = category
                folder = self.folders[mode]
                target = folder / path.name
                if sidecar and source_key in destinations:
                    target = folder / (destinations[source_key] + path.suffix)
                temporary = None
                try:
                    number = 1
                    def lyric_conflict(candidate):
                        if sidecar or mode != "music":
                            return False
                        for companion in companions.get(source_key, []):
                            existing = folder / (candidate.stem + companion.suffix)
                            if existing.exists() and (existing.is_symlink() or
                                    self.digest(existing) != self.digest(companion)):
                                return True
                        return False
                    while target.exists() or lyric_conflict(target):
                        conflict = lyric_conflict(target)
                        if not target.exists():
                            number += 1
                            target = folder / ("%s (%d)%s" % (path.stem, number, path.suffix))
                            continue
                        if target.resolve().parent != folder or target.is_symlink():
                            raise ValueError('Invalid destination file location')
                        if not conflict and (path.resolve() == target.resolve() or (
                                path.stat().st_size == target.stat().st_size and
                                self.digest(path) == self.digest(target))):
                            break
                        if sidecar:
                            raise ValueError('Different lyrics already use this filename; do not overwrite or guess the pairing')
                        number += 1
                        target = folder / ("%s (%d)%s" % (path.stem, number, path.suffix))
                    else:
                        temporary = folder / (".import-" + secrets.token_hex(12))
                        with path.open("rb") as source, temporary.open("xb") as dest:
                            shutil.copyfileobj(source, dest, 1024 * 1024)
                            dest.flush()
                            os.fsync(dest.fileno())
                        # On Windows rename refuses an existing target; never overwrite.
                        if target.exists():
                            raise OSError('Filename already in use. Import again')
                        temporary.rename(target)
                    added.append(dict(name=target.name, mode=mode))
                    if not sidecar:
                        destinations[source_key] = target.stem
                except (OSError, ValueError):
                    errors.append('%s: save failed; check file access and disk space' % path.name)
                    if not sidecar:
                        rejected.add(source_key)
                finally:
                    if temporary is not None and temporary.exists():
                        temporary.unlink()
        return dict(added=added, errors=errors)


class MediaServer:
    def __init__(self, library):
        self.library = library
        self.token = secrets.token_hex(24)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_HEAD(self):
                self.send_media(head=True)

            def do_GET(self):
                self.send_media()

            def send_media(self, head=False):
                prefix = "/" + owner.token + "/"
                route = urlsplit(self.path).path
                path = owner.library.resolve(route[len(prefix):]) if route.startswith(prefix) else None
                if path is None:
                    self.send_error(404)
                    return
                try:
                    with path.open("rb") as stream:
                        size = os.fstat(stream.fileno()).st_size
                        start, end = 0, size-1
                        value = self.headers.get("Range")
                        if value:
                            match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
                            if not match or not any(match.groups()):
                                self.send_error(416)
                                return
                            first, last = match.groups()
                            if first:
                                start = int(first)
                                end = min(int(last), size-1) if last else size-1
                            else:
                                start = max(0, size-int(last))
                            if start > end or start >= size or (not first and int(last) == 0):
                                self.send_response(416)
                                self.send_header("Content-Range", "bytes */%d" % size)
                                self.send_header("Content-Length", "0")
                                self.end_headers()
                                return
                        self.send_response(206 if value else 200)
                        mime = {".m4a": "audio/mp4", ".m4v": "video/mp4", ".flac": "audio/flac",
                                ".opus": "audio/ogg", ".mkv": "video/x-matroska"}.get(
                                    path.suffix.lower(), mimetypes.guess_type(path.name)[0] or "application/octet-stream")
                        self.send_header("Content-Type", mime)
                        # Token-scoped local media must be CORS-readable by Web Audio.
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Accept-Ranges", "bytes")
                        self.send_header("Content-Length", str(max(0, end-start+1)))
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("X-Content-Type-Options", "nosniff")
                        if value:
                            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
                        self.end_headers()
                        if not head:
                            stream.seek(start)
                            remaining = end-start+1
                            while remaining > 0:
                                block = stream.read(min(256*1024, remaining))
                                if not block:
                                    break
                                self.wfile.write(block)
                                remaining -= len(block)
                except (OSError, ConnectionError):
                    return  # missing/unplugged file, cancelled seek or closing player

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def url(self, ident):
        return "http://127.0.0.1:%d/%s/%s" % (self.server.server_port, self.token, ident)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
