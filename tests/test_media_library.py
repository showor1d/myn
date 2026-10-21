import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src/core")]
from media_library import MediaLibrary, MediaServer
from media_bridge import MediaBridge
import stt


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="myn-media-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = MediaLibrary(self.root / "library")

    def file(self, name, data=b"0123456789"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_import_routes_without_moving_and_persists(self):
        audio, video = self.file("一首歌.mp3"), self.file("视频.mp4")
        result = self.library.add([audio, video])
        self.assertEqual(result["errors"], [])
        self.assertEqual([x["mode"] for x in result["added"]], ["music", "video"])
        self.assertTrue(audio.exists() and video.exists())
        fresh = MediaLibrary(self.library.root)
        rows = fresh.scan()
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(fresh.resolve(row["id"]).read_bytes(), audio.read_bytes())

    def test_repeat_and_conflicting_names_preserve_every_file(self):
        one = self.file("a/song.wav", b"first")
        two = self.file("b/song.wav", b"other")
        self.library.add([one, one, two, two])
        self.assertEqual(len(self.library.scan()), 2)
        self.assertEqual((self.library.folders["music"] / "song.wav").read_bytes(), b"first")
        self.assertEqual((self.library.folders["music"] / "song (2).wav").read_bytes(), b"other")

    def test_partial_copy_never_appears_and_other_files_survive(self):
        path = self.file("sound.wav")
        with patch("media_library.shutil.copyfileobj", side_effect=OSError("disk full")):
            result = self.library.add([path])
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(list(self.library.folders["music"].iterdir()), [])
        self.assertTrue(path.exists())
        result = self.library.add([self.file("no.exe"), path])
        self.assertEqual(len(result["added"]), 1)
        self.assertEqual(len(result["errors"]), 1)

    def test_manual_files_appear_refresh_and_stale_ids_fail_closed(self):
        path = self.library.folders["video"] / "manual.webm"
        path.write_bytes(b"test")
        row = self.library.scan()[0]
        self.assertEqual(self.library.resolve(row["id"]), path)
        self.assertIsNone(self.library.resolve("../../outside"))
        path.unlink()
        self.assertIsNone(self.library.resolve(row["id"]))
        self.assertEqual(self.library.scan(), [])

    def test_http_seek_head_suffix_and_no_arbitrary_paths(self):
        self.library.add([self.file("clip.mp4")])
        row = self.library.scan()[0]
        server = MediaServer(self.library)
        self.addCleanup(server.close)
        url = server.url(row["id"])
        def read(headers=None, method="GET", target=url):
            return urllib.request.urlopen(urllib.request.Request(target, headers=headers or {}, method=method))
        with read() as response:
            self.assertEqual(response.read(), b"0123456789")
            self.assertEqual(response.headers["Accept-Ranges"], "bytes")
        with read({"Range": "bytes=2-5"}) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), b"2345")
            self.assertEqual(response.headers["Content-Range"], "bytes 2-5/10")
        with read({"Range": "bytes=-3"}) as response:
            self.assertEqual(response.read(), b"789")
        with read(method="HEAD") as response:
            self.assertEqual(response.read(), b"")
            self.assertEqual(response.headers["Content-Length"], "10")
        for headers, target, code in [
                ({"Range": "bytes=99-"}, url, 416),
                ({}, server.url("../file.mp4"), 404),
                ({}, url.replace(server.token, "wrong"), 404)]:
            with self.assertRaises(urllib.error.HTTPError) as error:
                read(headers, target=target)
            self.assertEqual(error.exception.code, code)

    def test_drop_native_paths_import_and_plain_names_do_not(self):
        api = MediaBridge(self.root / "bridge")
        self.addCleanup(api._close_media)
        messages = []
        class Window:
            def evaluate_js(self, script):
                messages.append(script)
        api.window = Window()
        path = self.file("drop.flac")
        api._drop({"dataTransfer": {"files": [{"name": path.name, "pywebviewFullPath": str(path)}]}})
        self.assertEqual(len(api.library.scan()), 0)
        token = next(iter(api.pending))
        api.media_import(token, "audio")
        self.assertEqual(len(api.library.scan()), 1)
        self.assertEqual(api.library.scan()[0]["mode"], "audio")
        with self.assertRaises(ValueError):
            api.media_import(token, "music")
        api._drop({"dataTransfer": {"files": [{"name": "private.wav"}]}})
        self.assertIn('No file paths received', messages[-1])
        self.assertEqual(stt.voice_command("打开播放器"), "media_player")

    def test_four_categories_validate_types_and_survive_restart(self):
        sound, movie = self.file("track.wav"), self.file("film.mp4")
        for category, path in [("music", sound), ("audio", sound), ("video", movie), ("movie", movie)]:
            self.assertEqual(self.library.add([path], category)["errors"], [])
        fresh = MediaLibrary(self.library.root)
        self.assertEqual({r["mode"] for r in fresh.scan()}, {"music", "audio", "video", "movie"})
        for row in fresh.scan():
            self.assertIsNotNone(fresh.resolve(row["id"]))
        self.assertTrue(self.library.add([movie], "music")["errors"])
        self.assertTrue(self.library.add([sound], "movie")["errors"])
        with self.assertRaises(ValueError):
            self.library.add([sound], "../../outside")
        self.assertEqual(len(self.library.scan()), 4)

    def test_drop_cancel_and_new_drop_never_import_old_paths(self):
        api = MediaBridge(self.root / "bridge")
        self.addCleanup(api._close_media)
        class Window:
            def evaluate_js(self, script):
                pass
        api.window = Window()
        event = {"dataTransfer": {"files": [{"pywebviewFullPath": str(self.file("one.wav"))}]}}
        api._drop(event)
        first = next(iter(api.pending))
        api.media_cancel(first)
        self.assertEqual(api.library.scan(), [])
        with self.assertRaises(ValueError):
            api.media_import(first, "music")
        api._drop(event)
        old = next(iter(api.pending))
        api._drop(event)
        with self.assertRaises(ValueError):
            api.media_import(old, "music")
        self.assertEqual(api.library.scan(), [])

    def test_playing_defers_proactive_notices_but_not_alarms_or_replies(self):
        from gui_interaction import InteractionMixin
        class Window(InteractionMixin):
            media_playing = True
            def _usage_config(self):
                return self.root
        window = Window()
        window.root = self.root
        with patch("gui_interaction.interaction.allowed", return_value=True):
            self.assertFalse(window._speech_allowed("proactive"))
            self.assertTrue(window._speech_allowed("alarm"))
            self.assertTrue(window._speech_allowed("reply"))
            window.media_playing = False
            self.assertTrue(window._speech_allowed("proactive"))


if __name__ == "__main__":
    unittest.main()
