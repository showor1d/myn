"""Human-approved permissions and unlimited search, using isolated configs only."""
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src/core")]
import local_actions as actions
import local_tools
import web_tools


class Permissions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="myn-permissions-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "personal_data/config"
        self.config.mkdir(parents=True)
        self.confirm = Mock(return_value=True)
        self.platform = Mock()
        self.context = actions.ActionContext(self.config, confirm=self.confirm, platform=self.platform)
        self.original = actions.ActionSettings(media=False, files=False, vision=False, confirm="never")
        actions.save_settings(self.config, self.original)

    def call(self, name="computer_permissions", **args):
        result, error = actions.execute(name, args, self.context)
        return json.loads(result), error

    def test_inspect_does_not_change_grants_or_ask(self):
        before = (self.config / actions.SETTINGS_NAME).read_bytes()
        data, error = self.call(action="inspect")
        self.assertFalse(error)
        self.assertFalse(data["settings"]["media"])
        self.assertEqual(before, (self.config / actions.SETTINGS_NAME).read_bytes())
        self.confirm.assert_not_called()
        self.assertIn("computer_permissions", {t["name"] for t in actions.tools_for(self.original)})

    def test_grant_requires_actual_approval_even_under_never_then_same_turn_action_works(self):
        self.assertTrue(self.call("media_control", action="play_pause")[1])
        self.platform.media_key.assert_not_called()
        for answer in (False, None, "yes", 1):
            self.confirm.return_value = answer
            self.assertTrue(self.call(action="group", group="media", enabled=True)[1])
            self.assertEqual(actions.load_settings(self.config), self.original)
        self.confirm.return_value = True
        self.assertFalse(self.call(action="group", group="media", enabled=True)[1])
        self.assertEqual(actions.load_settings(self.config), replace(self.original, media=True))
        self.assertFalse(self.call("media_control", action="play_pause")[1])
        self.platform.media_key.assert_called_once_with("play_pause")
        self.assertFalse(self.call(action="group", group="media", enabled=False)[1])
        self.assertTrue(self.call("media_control", action="play_pause")[1])

    def test_no_confirmation_callback_cannot_grant(self):
        self.context.confirm = None
        self.assertTrue(self.call(action="group", group="media", enabled=True)[1])
        self.assertEqual(actions.load_settings(self.config), self.original)

    def test_invalid_request_never_changes_settings(self):
        for args in ({"action":"group", "group":"unknown", "enabled":True},
                     {"action":"group", "group":"media", "enabled":"true"},
                     {"action":"group", "group":"media", "enabled":True, "approved":True},
                     {"action":"confirmation", "policy":"anything"}):
            self.assertTrue(self.call(**args)[1])
        self.confirm.assert_not_called()
        self.assertEqual(actions.load_settings(self.config), self.original)

    def test_folder_grant_and_revoke_preserve_other_roots_and_shield_private_data(self):
        folder = self.root / "documents"
        folder.mkdir()
        for access in ("read", "write"):
            for unsafe in (self.config, self.config.parent, self.root, Path(self.root.anchor)):
                self.assertTrue(self.call(action="folder", access=access, path=str(unsafe), enabled=True)[1])
            self.assertFalse(self.call(action="folder", access=access, path=str(folder), enabled=True)[1])
            self.assertIn(str(folder), local_tools.grants(self.config)[access])
            self.assertFalse(self.call(action="folder", access=access, path=str(folder), enabled=False)[1])
            self.assertNotIn(str(folder), local_tools.grants(self.config)[access])

    def test_confirmation_policy_change_requires_confirmation(self):
        self.confirm.return_value = False
        self.assertTrue(self.call(action="confirmation", policy="always")[1])
        self.assertEqual(actions.load_settings(self.config).confirm, "never")
        self.confirm.return_value = True
        self.assertFalse(self.call(action="confirmation", policy="risky")[1])
        self.assertEqual(actions.load_settings(self.config), replace(self.original, confirm="risky"))

    def test_unlimited_ignores_usage_but_retains_per_answer_guard(self):
        with patch.object(web_tools, "searches_this_month", return_value=99999999):
            data = web_tools.toolset_for(self.config)
        self.assertFalse(data.limit_reached)
        self.assertEqual(data.searches_used, 99999999)
        self.assertEqual(data.server_tools[0]["max_uses"], web_tools.MAX_SEARCHES_PER_ANSWER)

    def test_web_settings_preserve_omitted_fields_and_ask(self):
        web_tools.save_settings(self.config, web_tools.WebSettings("off", 5, False))
        self.assertFalse(self.call("web_settings")[1])
        self.confirm.assert_not_called()
        self.confirm.return_value = False
        self.assertTrue(self.call("web_settings", monthly_limit=-1)[1])
        self.assertEqual(web_tools.load_settings(self.config).monthly_limit, 5)
        self.confirm.return_value = True
        self.assertFalse(self.call("web_settings", monthly_limit=-1)[1])
        self.assertEqual(web_tools.load_settings(self.config), web_tools.WebSettings("off", -1, False))

    def test_chat_grants_then_runs_original_action_and_unattended_cannot_grant(self):
        import chat
        import claude_client
        session = chat.ChatSession(self.config / "test.db")
        session.confirm_action = self.confirm
        receipts = []
        def model(*args, **kwargs):
            names = {tool["name"] for tool in kwargs["read_tools"]}
            self.assertTrue({"computer_permissions", "media_control", "web_settings"} <= names)
            run = kwargs["tool_executor"]
            receipts.append(run("computer_permissions", {"action":"group", "group":"media", "enabled":True}))
            receipts.append(run("media_control", {"action":"play_pause"}))
            return "已执行。"
        with patch.object(session, "_optional_key", return_value="fake"), \
                patch.object(claude_client, "coordinated_reply", side_effect=model), \
                patch.object(actions, "Platform", return_value=self.platform):
            session.ask("允许控制音乐并继续播放", heard=True)
            self.assertTrue(all(not item[1] for item in receipts))
            actions.save_settings(self.config, self.original)
            session._unattended = True
            receipts.clear()
            self.confirm.reset_mock()
            session.ask("允许控制音乐并继续播放", heard=False)
            self.assertTrue(all(item[1] for item in receipts))
            self.confirm.assert_not_called()
            self.assertEqual(actions.load_settings(self.config), self.original)


if __name__ == "__main__":
    unittest.main()
