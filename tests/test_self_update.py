"""MYN fixing itself: the copy, the checks that guard it, applying with a backup,
the start-up rollback when a new version fails, and the test comparison."""

import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src" / "core")]

import self_update

AGENTS = "# MYN\n\n## 0.1 第一版\n\n内容。\n\n## Permanent constraints\n\n1. 个人数据不能动。\n"
GUI = "import self_update\n\ndef main():\n    self_update.on_startup()\n    self_update.confirm()\n"


class Tree(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="myn-selfupdate-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "MYN"
        files = {"src/myn_gui.py": GUI, "src/core/tts.py": "VOICE = 'a'\n",
                 "src/core/self_update.py": "# protected\n", "src/data_location.py": "# protected\n",
                 "tests/test_tts.py": "import unittest\n", "AGENTS.md": AGENTS,
                 "personal_data/config/claude-key.dpapi": "secret"}
        for name, text in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        (self.root / "src" / "core" / "__pycache__").mkdir()
        (self.root / "src" / "core" / "__pycache__" / "tts.cpython-39.pyc").write_bytes(b"x")

    def copy(self):
        return self_update.prepare("t1", self.root)

    def test_copy_holds_the_program_only(self):
        work = self.copy()
        self.assertTrue((work / "src" / "core" / "tts.py").is_file())
        self.assertTrue((work / "AGENTS.md").is_file())
        self.assertFalse((work / "personal_data").exists())
        self.assertFalse((work / "src" / "core" / "__pycache__").exists())
        self.assertEqual(self_update.changes(work, self.root), {})
        self.assertEqual(self_update.check(work, self.root), ['No changes.'])

    def test_a_proper_fix_passes_the_checks(self):
        work = self.copy()
        (work / "src/core/tts.py").write_text("VOICE = 'b'\n", encoding="utf-8")
        (work / "tests/test_voice.py").write_text("import unittest\n", encoding="utf-8")
        (work / "AGENTS.md").write_text(AGENTS.replace("## Permanent constraints", "## 0.2 修好声音\n\n由 MYN 自我修改。\n\n## Permanent constraints"),
                                        encoding="utf-8")
        self.assertEqual(self_update.changes(work, self.root),
                         {"AGENTS.md": "changed", "src/core/tts.py": "changed", "tests/test_voice.py": "added"})
        self.assertEqual(self_update.check(work, self.root), [])
        summary = self_update.diff_summary(work, self.root)
        self.assertIn('src/core/tts.py（Modify，+1 −1）', summary)

    def test_guards(self):
        cases = {
            "src/core/self_update.py": ("# disabled\n", "Protected"),
            "src/data_location.py": ("# moved\n", "Protected"),
            "src/core/tts.py": ("def broken(:\n", "Syntax error"),
            "AGENTS.md": (AGENTS.replace("个人数据不能动", "随便"), "Permanent constraints"),
            "src/myn_gui.py": ("def main():\n    pass\n", "protections"),
        }
        for name, (text, expected) in cases.items():
            with self.subTest(name=name):
                work = self.copy()
                (work / name).write_text(text, encoding="utf-8")
                self.assertTrue(any(expected in problem for problem in self_update.check(work, self.root)),
                                self_update.check(work, self.root))
        work = self.copy()
        (work / "tests/test_tts.py").unlink()
        self.assertIn('Tests cannot be deleted: tests/test_tts.py', self_update.check(work, self.root))

    def test_apply_backs_up_and_a_failed_load_check_puts_it_back(self):
        work = self.copy()
        (work / "src/core/tts.py").write_text("VOICE = 'b'\n", encoding="utf-8")
        (work / "src/core/new_helper.py").write_text("X = 1\n", encoding="utf-8")
        ok, message = self_update.apply(work, "改了声音", self.root, checker=lambda base: (False, "ImportError"))
        self.assertFalse(ok)
        self.assertIn('was restored', message)
        self.assertEqual((self.root / "src/core/tts.py").read_text(encoding="utf-8"), "VOICE = 'a'\n")
        self.assertFalse((self.root / "src/core/new_helper.py").exists())
        self.assertFalse((self.root / self_update.STATE_DIR / self_update.PENDING).exists())
        ok, backup = self_update.apply(work, "改了声音", self.root, checker=lambda base: (True, ""))
        self.assertTrue(ok)
        self.assertEqual((self.root / "src/core/tts.py").read_text(encoding="utf-8"), "VOICE = 'b'\n")
        self.assertEqual((Path(backup) / "src/core/tts.py").read_text(encoding="utf-8"), "VOICE = 'a'\n")
        self.assertEqual((self.root / "personal_data/config/claude-key.dpapi").read_text(encoding="utf-8"), "secret")
        return backup

    def test_new_version_that_never_confirms_is_rolled_back_on_the_next_start(self):
        backup = self.test_apply_backs_up_and_a_failed_load_check_puts_it_back()
        self.assertEqual(self_update.on_startup(self.root), ("trying", "改了声音"))
        # ...it crashed, so no confirm(); the next start puts the old version back
        self.assertEqual(self_update.on_startup(self.root), ("rolled_back", "改了声音"))
        self.assertEqual((self.root / "src/core/tts.py").read_text(encoding="utf-8"), "VOICE = 'a'\n")
        self.assertFalse((self.root / "src/core/new_helper.py").exists())
        self.assertIn("restored the version from before the change", self_update.take_notice(self.root))
        self.assertEqual(self_update.take_notice(self.root), "")  # shown once
        self.assertIsNone(self_update.on_startup(self.root))
        self.assertTrue(Path(backup).is_dir())  # the backup is kept
        history = [json.loads(line) for line in
                   (self.root / self_update.STATE_DIR / self_update.HISTORY).read_text(encoding="utf-8").splitlines()]
        self.assertEqual([item["result"] for item in history], ["load_check_failed", "rolled_back"])

    def test_new_version_that_runs_is_kept(self):
        self.test_apply_backs_up_and_a_failed_load_check_puts_it_back()
        self.assertEqual(self_update.on_startup(self.root)[0], "trying")
        self.assertEqual(self_update.confirm(self.root), "改了声音")
        self.assertIsNone(self_update.on_startup(self.root))
        self.assertEqual((self.root / "src/core/tts.py").read_text(encoding="utf-8"), "VOICE = 'b'\n")

    def test_prompt_tells_the_rules(self):
        prompt = self_update.prompt_for("字幕乱码", self.root, "## Permanent constraints\n\n1. 个人数据不能动。", "测试失败：x")
        for needed in ("字幕乱码", "个人数据不能动", "测试失败：x", "Python 3.9", "src/core/self_update.py"):
            self.assertIn(needed, prompt)


class RunningTests(unittest.TestCase):
    def test_failures_are_compared_with_what_already_failed(self):
        with tempfile.TemporaryDirectory(prefix="myn-selftests-") as folder:
            base = Path(folder)
            (base / "tests").mkdir()
            (base / "tests" / "test_demo.py").write_text(textwrap.dedent('''
                import os, unittest
                class Demo(unittest.TestCase):
                    def test_ok(self):
                        self.assertTrue(True)
                    def test_windows_only(self):
                        self.assertEqual(os.name, "windows-only")
                    def test_private_data_is_not_used(self):
                        self.assertIn("myn-selftest-", os.environ["MYN_PERSONAL_DATA_DIR"])
                        self.assertNotIn("ANTHROPIC_API_KEY", os.environ)
            '''), encoding="utf-8")
            baseline, _ = self_update.run_tests(base)
            self.assertEqual(baseline, {"test_windows_only (test_demo.Demo)"} if sys.version_info < (3, 11)
                             else {next(iter(baseline))})
            (base / "tests" / "test_demo.py").write_text(
                (base / "tests" / "test_demo.py").read_text(encoding="utf-8").replace("assertTrue(True)", "assertTrue(False)"),
                encoding="utf-8")
            after, output = self_update.run_tests(base)
            self.assertEqual(len(self_update.new_failures(baseline, after)), 1)
            self.assertIn("test_ok", self_update.new_failures(baseline, after)[0])
            self.assertIn("FAILED", output)


if __name__ == "__main__":
    unittest.main()
