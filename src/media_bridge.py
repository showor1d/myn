"""WebView media controls. Imports are selected through native dialogs or native drops."""
import json
import os
import threading
import secrets
import math
import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "core"))
from data_location import personal_root
from media_library import MediaLibrary, MediaServer, AUDIO, VIDEO, CATEGORIES


class MediaBridge:
    def __init__(self, root=None):
        self.library = MediaLibrary(root or personal_root() / "media")
        self.server = None
        self.window = None
        self.lock = threading.RLock()
        self.pending = {}
        self._audio_capture_until = 0.0
        self._output_meter = None

    def core_audio_levels(self):
        return self._output_meter.snapshot() if self._output_meter else dict(output=0., available=False)

    def audio_console_authorize_capture(self):
        """Called only by the explicit device/capture buttons, not page startup."""
        self._audio_capture_until = time.monotonic() + 20
        return True

    def _audio_capture_allowed(self, origin, source):
        page = (Path(__file__).resolve().parent / 'web/index.html').as_uri().casefold()
        # WebView2 reports the file origin, not its full document URI. Only the
        # trusted top-level page may use a recent explicit capture request.
        return (str(origin).casefold() in (page, 'file:///') and
                str(source).casefold() == page and
                time.monotonic() < self._audio_capture_until)

    def audio_console_settings(self):
        import audio_console
        with self.lock:
            return audio_console.load(self.library.root)

    def audio_console_save(self, value):
        import audio_console
        with self.lock:
            return audio_console.save(self.library.root, value)

    def media_settings(self):
        with self.lock:
            path = self.library.root / ".player-settings.json"
            if not path.exists():
                return dict(volume=1.0, muted=False)
            data = json.loads(path.read_text(encoding="utf-8"))
            return self._validate_settings(data.get("volume"), data.get("muted"))

    @staticmethod
    def _validate_settings(volume, muted):
        if (isinstance(volume, bool) or not isinstance(volume, (float, int)) or
                not math.isfinite(volume) or not 0 <= volume <= 1 or not isinstance(muted, bool)):
            raise ValueError('Invalid player volume setting.')
        return dict(volume=float(volume), muted=muted)

    def media_save_settings(self, volume, muted):
        data = self._validate_settings(volume, muted)
        with self.lock:
            target = self.library.root / ".player-settings.json"
            temporary = self.library.root / (".settings-" + secrets.token_hex(12))
            try:
                with temporary.open("x", encoding="utf-8") as stream:
                    json.dump(data, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(str(temporary), str(target))
            finally:
                if temporary.exists():
                    temporary.unlink()
        return data

    def media_list(self):
        with self.lock:
            if self.server is None:
                self.server = MediaServer(self.library)
            rows = self.library.scan()
            for row in rows:
                row["url"] = self.server.url(row["id"])
        return dict(items=rows)

    def media_lyrics(self, ident):
        return self.library.lyrics(ident)

    def media_pick(self, category="music"):
        if category not in CATEGORIES:
            raise ValueError('Choose a media category.')
        import webview
        extensions = AUDIO if CATEGORIES[category][1] == "music" else VIDEO
        files = self.window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=True,
            file_types=('Media files (%s)' % ";".join("*"+s for s in sorted(extensions)),))
        return self._import(files or [], category)

    def media_folder(self, mode):
        if mode not in self.library.folders:
            return False
        os.startfile(str(self.library.folders[mode]))
        return True

    def _import(self, paths, category=None):
        result = self.library.add(paths, category)
        result.update(self.media_list())
        return result

    def media_import(self, token, category):
        if category not in CATEGORIES:
            raise ValueError('Choose a media category.')
        with self.lock:
            paths = self.pending.pop(token, None)
        if paths is None:
            raise ValueError('This dropped-file batch was already processed. Drop the files again.')
        return self._import(paths, category)

    def media_cancel(self, token):
        with self.lock:
            self.pending.pop(token, None)
        return True

    def _drop(self, event):
        files = event.get("dataTransfer", {}).get("files", [])
        paths = [item["pywebviewFullPath"] for item in files if item.get("pywebviewFullPath")]
        if not paths:
            result = dict(errors=['No file paths received. Drop the files again from File Explorer.'], added=[])
            self.window.evaluate_js("window.MediaPlayer && MediaPlayer.imported(%s)" %
                                    json.dumps(result, ensure_ascii=False))
        else:
            token = secrets.token_hex(16)
            with self.lock:
                # One visible drop selection at a time; never retain stale paths.
                self.pending.clear()
                self.pending[token] = paths
            self.window.evaluate_js("window.MediaPlayer && MediaPlayer.dropped(%s,%s)" %
                                   (json.dumps(token), json.dumps([Path(p).name for p in paths], ensure_ascii=False)))

    def _attach_media(self, window):
        from webview.dom import DOMEventHandler
        self.window = window
        if self._output_meter is None:
            from output_meter import OutputMeter
            self._output_meter = OutputMeter().start()
        window.dom.get_element("body").events.drop += DOMEventHandler(self._drop, prevent_default=True)
        if os.name == 'nt':
            from System import Action
            from Microsoft.Web.WebView2.Core import CoreWebView2PermissionKind, CoreWebView2PermissionState
            def permission(sender, args):
                if args.PermissionKind != CoreWebView2PermissionKind.Microphone:
                    return
                allowed = self._audio_capture_allowed(args.Uri, sender.Source)
                self._audio_permission_diagnostic = dict(uri=str(args.Uri), allowed=allowed)
                args.State = (CoreWebView2PermissionState.Allow if allowed else
                              CoreWebView2PermissionState.Deny)
                if hasattr(args, 'SavesInProfile'):
                    args.SavesInProfile = False

            self._audio_permission_handler = permission
            def attach():
                window.native.webview.CoreWebView2.PermissionRequested += permission
            window.native.Invoke(Action(attach))

    def _close_media(self):
        if self._output_meter is not None:
            self._output_meter.close()
            self._output_meter = None
        if self.server is not None:
            self.server.close()
