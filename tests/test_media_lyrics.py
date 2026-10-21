import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src/core")]
from media_library import MediaLibrary
import lyrics


class LyricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = MediaLibrary(self.root / "library")

    def file(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_direct_folder_pair_and_lrc_priority(self):
        folder = self.library.folders["music"]
        (folder / "Song.mp3").write_bytes(b"music")
        (folder / "song.lrc").write_text("[00:01.20]你好\n[00:04]再见", encoding="utf-8")
        (folder / "Song.txt").write_text("plain", encoding="utf-8")
        row = self.library.scan()[0]
        self.assertEqual(row["lyrics_file"], "song.lrc")
        data = self.library.lyrics(row["id"])
        self.assertEqual(data["lines"][0], {"time": 1.2, "text": "你好"})
        self.assertTrue(data["timed"])
        self.assertEqual(len(self.library.scan()), 1)
        self.assertEqual(self.library.lyrics("../../outside")["lines"], [])

    def test_batch_pair_follows_collision_name(self):
        old = self.file("old/song.mp3", b"old")
        song = self.file("new/song.mp3", b"new")
        text = self.file("new/song.lrc", b"[00:01]new lyrics")
        self.library.add([old], "music")
        result = self.library.add([text, song], "music")
        self.assertEqual(result["errors"], [])
        rows = {row["name"]: row for row in self.library.scan()}
        self.assertIsNone(rows["song.mp3"]["lyrics_file"])
        self.assertEqual(rows["song (2).mp3"]["lyrics_file"], "song (2).lrc")
        self.assertTrue(song.exists() and text.exists())

    def test_same_audio_different_lyrics_preserves_both_pairs(self):
        a = self.file("first/song.mp3", b"same")
        b = self.file("first/song.lrc", b"[00:01]first")
        c = self.file("second/song.mp3", b"same")
        d = self.file("second/song.lrc", b"[00:01]second")
        self.library.add([a,b], "music")
        self.assertEqual(self.library.add([c,d], "music")["errors"], [])
        rows = self.library.scan()
        self.assertEqual(len(rows), 2)
        self.assertEqual({self.library.lyrics(row["id"])["lines"][0]["text"] for row in rows},
                         {"first", "second"})

    def test_parser_multiple_tags_offsets_translation_and_plain(self):
        data = lyrics.parse("[ar:Artist]\n[offset:500]\n[00:02.00][00:04]hello\n[00:02]translation")
        self.assertEqual(data["lines"], [{"time":1.5,"text":"hello\ntranslation"},{"time":3.5,"text":"hello"}])
        plain = lyrics.parse("<b>plain words</b>\nSecond line", timed=False)
        self.assertFalse(plain["timed"])
        self.assertEqual(plain["lines"][0]["text"], "<b>plain words</b>")

    def test_encodings_and_size_limit(self):
        for encoding in ("utf-8-sig", "utf-16", "gb18030"):
            path = self.file("sample.lrc", "[00:02]中文歌词".encode(encoding))
            self.assertEqual(lyrics.read(path)["lines"][0]["text"], "中文歌词")
        path = self.file("large.txt", b"a" * (lyrics.LIMIT + 1))
        with self.assertRaises(ValueError):
            lyrics.read(path)

    def test_unmatched_lyrics_preserved_and_wrong_category_rejected(self):
        text = self.file("later.txt", b"lyrics")
        self.assertEqual(self.library.add([text], "music")["errors"], [])
        self.assertEqual(self.library.scan(), [])
        song = self.file("later.mp3", b"music")
        self.library.add([song], "music")
        row = self.library.scan()[0]
        self.assertEqual(row["lyrics_file"], "later.txt")
        self.assertFalse(self.library.lyrics(row["id"])["timed"])
        self.assertTrue(self.library.add([text], "video")["errors"])
