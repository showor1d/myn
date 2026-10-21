"""MYN's eyes: screen / window / clipboard pictures and image files reach Claude as images.
Windows' own screenshot code is replaced by a fake camera here."""

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src" / "core"), str(BASE / "tests")]

import chat
import claude_client
import local_actions
import local_tools
import vision
from test_web_tools import reply

JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-bytes" * 10
PNG = b"\x89PNG\r\n\x1a\n" + b"png-bytes" * 10


class FakeCamera:
    def __init__(self):
        self.calls = []

    def capture(self, target):
        self.calls.append(("capture", target))
        return JPEG, {"what": target, "title": "记事本 - 报错.txt" if target == "window" else "",
                      "width": 2560, "height": 1440, "sent": [1568, 882]}

    def clipboard(self):
        self.calls.append(("clipboard",))
        return JPEG, {"what": "clipboard", "width": 800, "height": 600}

    def shrink(self, path):
        self.calls.append(("shrink", Path(path).name))
        return JPEG, {"what": "file", "width": 6000, "height": 4000}


class Pictures(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="myn-vision-")
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.camera = FakeCamera()

    def test_screen_and_window(self):
        block, info = vision.look(self.camera, "window")
        self.assertEqual(block["type"], "image")
        self.assertEqual(block["source"]["media_type"], "image/jpeg")
        self.assertEqual(base64.b64decode(block["source"]["data"]), JPEG)
        self.assertEqual(info["title"], "记事本 - 报错.txt")
        vision.look(self.camera, "screen")
        self.assertEqual(self.camera.calls, [("capture", "window"), ("capture", "screen")])
        with self.assertRaises(vision.VisionError):
            vision.look(self.camera, "desktop")

    def test_files_small_ones_as_they_are_big_ones_shrunk(self):
        small = self.folder / "图.png"
        small.write_bytes(PNG)
        block, info = vision.from_file(self.camera, small)
        self.assertEqual(block["source"]["media_type"], "image/png")
        self.assertEqual(self.camera.calls, [])  # sent as it is
        big = self.folder / "photo.jpg"
        big.write_bytes(JPEG + b"x" * vision.SEND_AS_IS_BYTES)
        block, info = vision.from_file(self.camera, big)
        self.assertEqual(self.camera.calls, [("shrink", "photo.jpg")])
        self.assertEqual(info["name"], "photo.jpg")
        bmp = self.folder / "old.bmp"
        bmp.write_bytes(b"BM" + b"0" * 50)
        vision.from_file(self.camera, bmp)  # bmp is always converted
        self.assertEqual(self.camera.calls[-1], ("shrink", "old.bmp"))
        fake = self.folder / "not-really.png"
        fake.write_bytes(b"hello")  # not an image inside: converted (or refused by Windows)
        vision.from_file(self.camera, fake)
        self.assertEqual(self.camera.calls[-1], ("shrink", "not-really.png"))
        with self.assertRaises(vision.VisionError):
            vision.from_file(self.camera, self.folder / "notes.txt")

    def test_windows_script_skips_myn_and_reads_behind_it(self):
        camera = vision.WindowsCamera([111, 222])
        scripts = []

        def run(body, timeout=25):
            scripts.append(body)
            return JPEG, {"what": "window"}

        with mock.patch.object(camera, "_run", side_effect=run):
            camera.capture("window")
            camera.capture("all")
        self.assertIn("@(111,222)", scripts[0])
        self.assertIn("PrintWindow($h, $dc, 2)", scripts[0])  # drawn even when covered by MYN
        self.assertIn("VirtualScreen", scripts[1])


class Tools(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="myn-vision-tools-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.config = self.root / "personal_data" / "config"
        self.config.mkdir(parents=True)
        self.camera = FakeCamera()
        self.context = local_actions.ActionContext(self.config, confirm=lambda preview: False,
                                                   camera=self.camera)

    def test_looking_is_direct_and_returns_a_picture(self):
        content, is_error = local_actions.execute("look_at_screen", {}, self.context)
        self.assertFalse(is_error)
        self.assertEqual([part["type"] for part in content], ["text", "image"])
        note = json.loads(content[0]["text"])
        self.assertEqual((note["what"], note["title"]), ("window", "记事本 - 报错.txt"))
        content, _ = local_actions.execute("look_at_clipboard", {}, self.context)
        self.assertEqual(content[1]["type"], "image")
        self.assertEqual(local_actions.LEVELS["look_at_screen"], "direct")

    def test_image_files_only_inside_granted_folders(self):
        pictures = self.root / "Pictures"
        pictures.mkdir()
        (pictures / "cat.png").write_bytes(PNG)
        content, is_error = local_actions.execute("look_at_image", {"path": str(pictures / "cat.png")}, self.context)
        self.assertTrue(is_error)
        self.assertIn('authorized', json.loads(content)["error"])
        local_tools.grant_read(self.config, pictures)
        content, is_error = local_actions.execute("look_at_image", {"path": str(pictures / "cat.png")}, self.context)
        self.assertFalse(is_error)
        self.assertEqual(content[1]["source"]["media_type"], "image/png")

    def test_turned_off_means_no_looking(self):
        local_actions.save_settings(self.config, local_actions.ActionSettings(vision=False))
        self.assertIn("look_at_screen", [tool["name"] for tool in
                                            local_actions.tools_for(local_actions.load_settings(self.config))])
        content, is_error = local_actions.execute("look_at_screen", {}, self.context)
        self.assertTrue(is_error)
        self.assertIn('View screens', json.loads(content)["error"])

    def test_a_failed_screenshot_is_reported_not_raised(self):
        class Broken(FakeCamera):
            def capture(self, target):
                raise vision.VisionError("截图超时。")
        context = local_actions.ActionContext(self.config, camera=Broken())
        content, is_error = local_actions.execute("look_at_screen", {"target": "screen"}, context)
        self.assertTrue(is_error)
        self.assertEqual(json.loads(content)["error"], '截图超时。')


class InChat(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="myn-vision-chat-")
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name) / "data" / "jarvis.db"
        self.db.parent.mkdir()
        self.session = chat.ChatSession(self.db)
        self.session.camera = FakeCamera()

    def test_myn_looks_at_the_screen_when_asked(self):
        payloads = []

        def opener(request, timeout):
            payloads.append(json.loads(request.data.decode("utf-8")))
            if len(payloads) == 1:
                return reply([{"type": "tool_use", "id": "toolu_l", "name": "look_at_screen", "input": {}}],
                             stop="tool_use")
            return reply([{"type": "text", "text": "这是一个找不到文件的报错。"}])

        with mock.patch.object(self.session, "_key", return_value="k"), \
                mock.patch.object(claude_client.urllib.request, "urlopen", side_effect=opener):
            answer = self.session.ask("我屏幕上这个报错是什么意思")["answer"]
        self.assertEqual(answer, "这是一个找不到文件的报错。")
        self.assertIn("look_at_screen", [tool["name"] for tool in payloads[0]["tools"]])
        result = payloads[1]["messages"][-1]["content"][0]
        self.assertEqual(result["type"], "tool_result")
        self.assertEqual([part["type"] for part in result["content"]], ["text", "image"])
        self.assertEqual(base64.b64decode(result["content"][1]["source"]["data"]), JPEG)

    def test_a_picture_sent_with_the_message(self):
        payloads = []

        def opener(request, timeout):
            payloads.append(json.loads(request.data.decode("utf-8")))
            return reply([{"type": "text", "text": "图里是一只猫。"}])

        self.session.pending_images = [vision.image_block(PNG, "image/png")]
        with mock.patch.object(self.session, "_key", return_value="k"), \
                mock.patch.object(claude_client.urllib.request, "urlopen", side_effect=opener):
            # even a question that looks like one about memories goes to the chat that can see
            answer = self.session.ask("我昨天拍的这张是什么（附图片：cat.png）")["answer"]
        self.assertEqual(answer, "图里是一只猫。")
        user = payloads[0]["messages"][-1]["content"]
        self.assertEqual([part["type"] for part in user], ["image", "text"])
        self.assertIn("附图片：cat.png", user[1]["text"])
        self.assertEqual(self.session.pending_images, [])
        with mock.patch.object(self.session, "_key", return_value="k"), \
                mock.patch.object(claude_client.urllib.request, "urlopen", side_effect=opener):
            self.session.ask("谢谢")
        self.assertIsInstance(payloads[-1]["messages"][-1]["content"], str)  # one turn only


if __name__ == "__main__":
    unittest.main()
