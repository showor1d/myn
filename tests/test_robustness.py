"""0.45.2: busy-retry for Claude, atomic grant file, credential detection."""

import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src" / "core")]

import claude_client
import local_tools
import message_log


def reply(text="好的"):
    return io.BytesIO(json.dumps({"stop_reason": "end_turn",
                                  "content": [{"type": "text", "text": text}],
                                  "usage": {"input_tokens": 3, "output_tokens": 2}}).encode("utf-8"))


def busy(code, retry_after=None):
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    return urllib.error.HTTPError(claude_client.ENDPOINT, code, "busy", headers, None)


class BusyRetry(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="myn-busy-")
        self.addCleanup(temporary.cleanup)
        self.usage = Path(temporary.name) / "claude-usage.jsonl"
        self.waits = []
        patcher = mock.patch.object(claude_client, "_sleep", side_effect=self.waits.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def send(self, opener):
        return claude_client.send(claude_client.SONNET, "system", "你好", "synthetic-key",
                                  opener=opener, usage_path=self.usage)

    def test_overloaded_then_ok_is_answered(self):
        attempts = []
        def opener(request, timeout):
            attempts.append(1)
            if len(attempts) == 1:
                raise busy(529)
            return reply("在的")
        self.assertIn("在的", json.dumps(self.send(opener), ensure_ascii=False))
        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.waits, [1.5])

    def test_retry_after_is_honoured_but_capped(self):
        attempts = []
        def opener(request, timeout):
            attempts.append(1)
            if len(attempts) == 1:
                raise busy(429, "3")
            if len(attempts) == 2:
                raise busy(429, "600")
            return reply()
        self.send(opener)
        self.assertEqual(self.waits, [3.0, claude_client.BUSY_MAX_WAIT])

    def test_gives_up_after_bounded_retries(self):
        attempts = []
        def opener(request, timeout):
            attempts.append(1)
            raise busy(503)
        with self.assertRaises(claude_client.ClaudeUnavailable) as caught:
            self.send(opener)
        self.assertTrue(caught.exception.fallback_allowed)
        self.assertEqual(len(attempts), 1 + claude_client.BUSY_RETRIES)

    def test_auth_errors_are_not_retried(self):
        attempts = []
        def opener(request, timeout):
            attempts.append(1)
            raise busy(401)
        with self.assertRaises(claude_client.ClaudeUnavailable):
            self.send(opener)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(self.waits, [])


class GrantFile(unittest.TestCase):
    def test_grant_and_revoke_leave_no_temporary_file(self):
        with tempfile.TemporaryDirectory(prefix="myn-grant-") as folder:
            config = Path(folder) / "config"
            work = Path(folder) / "work"
            config.mkdir()
            work.mkdir()
            local_tools.grant_read(config, work)
            self.assertEqual(local_tools.grants(config)["read"], [str(work.resolve())])
            local_tools.revoke_read(config, str(work.resolve()))
            self.assertEqual(local_tools.grants(config)["read"], [])
            self.assertEqual(sorted(p.name for p in config.iterdir()), [local_tools.GRANT_FILE])

    def test_non_text_write_grant_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="myn-grant-") as folder:
            (Path(folder) / local_tools.GRANT_FILE).write_text(json.dumps(
                {"read": [], "write": [7], "delete": [], "execute": []}), encoding="utf-8")
            with self.assertRaises(ValueError):
                local_tools.grants(folder)


class Credentials(unittest.TestCase):
    def test_real_key_shapes_are_caught(self):
        for text in ("sk-ant-api03-AbCdEfGhIjKlMnOp", "我的key是 sk_3f9a8b7c6d5e4f3a2b1c0d9e",
                     "我的sk-ant-api03-AbCdEfGhIjKlMnOp", "ghp_abcdefghijklmnopqrstuvwxyz0123",
                     "AIzaSyA1234567890abcdefghijklmnopqrstu", "xoxb-1234567890-abcdefghij",
                     "AKIAABCDEFGHIJKLMNOP", "-----BEGIN RSA PRIVATE KEY-----",
                     "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpM",
                     "密码是 hunter22"):
            self.assertTrue(message_log.is_sensitive(text), text)

    def test_ordinary_words_are_not_keys(self):
        for text in ("今天做了 risk-assessment-report 的整理", "task-management-system 怎么设计",
                     "desk-organizer-premium 好用吗", "我在学 sklearn", "ask-me-anything 是什么意思"):
            self.assertFalse(message_log.is_sensitive(text), text)


if __name__ == "__main__":
    unittest.main()
