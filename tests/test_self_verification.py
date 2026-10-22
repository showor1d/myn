"""Real subprocess checks for incomplete or reduced self-update verification."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src/core")]
import self_update


class Verification(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="myn-verification-")
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        (self.base / "tests").mkdir()

    def write_test(self, body="        self.assertTrue(True)"):
        (self.base / "tests/test_demo.py").write_text(
            "import unittest\nclass Demo(unittest.TestCase):\n    def test_original(self):\n" + body + "\n", encoding="utf-8")

    def test_empty_suite_cannot_approve_an_update(self):
        before, _ = self_update.run_tests(self.base)
        after, _ = self_update.run_tests(self.base)
        self.assertIn("(Tests unavailable)", self_update.new_failures(before, after))

    def test_printed_ok_without_execution_evidence_is_rejected(self):
        with mock.patch.object(self_update.subprocess, "run", return_value=SimpleNamespace(stdout="OK\n", stderr="", returncode=0)):
            failures, _ = self_update.run_tests(self.base)
        self.assertIn("(Tests unavailable)", failures)

    def test_removed_test_is_rejected_even_when_remaining_tests_pass(self):
        self.write_test()
        before, _ = self_update.run_tests(self.base)
        path = self.base / "tests/test_demo.py"
        path.write_text(path.read_text().replace("test_original", "test_replacement"))
        after, _ = self_update.run_tests(self.base)
        self.assertFalse(after)
        self.assertTrue(any("removed or renamed" in item for item in self_update.new_failures(before, after)))

    def test_turning_an_existing_test_into_a_skip_is_rejected(self):
        self.write_test()
        before, _ = self_update.run_tests(self.base)
        self.write_test('        self.skipTest("not checked")')
        after, _ = self_update.run_tests(self.base)
        self.assertFalse(after)
        self.assertTrue(any("newly skipped" in item for item in self_update.new_failures(before, after)))

    def test_successful_runs_keep_counts_and_isolate_credentials(self):
        self.write_test('        import os\n'
                        '        self.assertNotIn("ELEVENLABS_API_KEY", os.environ)\n'
                        '        self.assertNotIn("ANTHROPIC_API_KEY", os.environ)\n'
                        '        self.assertNotIn("MYN_LOCAL_LLM_URL", os.environ)')
        with mock.patch.dict(os.environ, {"ELEVENLABS_API_KEY": "fake", "ANTHROPIC_API_KEY": "fake",
                                         "MYN_LOCAL_LLM_URL": "must-not-probe"}):
            before, _ = self_update.run_tests(self.base)
            after, _ = self_update.run_tests(self.base)
        self.assertFalse(before)
        self.assertFalse(after)
        summary = self_update.verification_summary(before, after)
        self.assertEqual((summary["ran"], summary["skipped"], summary["failed"]), (1, 0, 0))
        self.assertTrue(summary["accepted"])
        self.assertTrue(summary["executed"])
        self.assertFalse(summary["live_audio_checked"])

    def test_copy_includes_but_protects_developer_test_dependency(self):
        (self.base / "tools").mkdir()
        (self.base / "tools/dev_finish.py").write_text("TEST = True\n")
        work = self_update.prepare("copy", self.base)
        self.assertEqual((work / "tools/dev_finish.py").read_text(), "TEST = True\n")
        self.assertIn("tools/dev_finish.py", self_update.PROTECTED)
        self.assertIn("src/core/self_test_runner.py", self_update.PROTECTED)

    def test_missing_baseline_is_not_excused_by_matching_failure(self):
        self.assertTrue(self_update.new_failures({"(Tests unavailable)"}, {"(Tests unavailable)"}))

    def test_timeout_and_missing_interpreter_are_rejected(self):
        with mock.patch.object(self_update.subprocess, "run", side_effect=self_update.subprocess.TimeoutExpired("python", 1)):
            failures, _ = self_update.run_tests(self.base)
        self.assertIn("(Test timeout)", failures)
        with mock.patch.object(self_update.subprocess, "run", side_effect=FileNotFoundError):
            failures, _ = self_update.run_tests(self.base)
        self.assertIn("(Tests unavailable)", failures)
