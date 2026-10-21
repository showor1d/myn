import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src/core")]
import stt
import stt_worker
import audio_inputs
import local_actions


class Client:
    def __init__(self, active=True):
        self.device, self.active = "Headset mic", active
        self.changes = []
    def audio_status(self):
        return dict(active=self.active, actual_device=self.device if self.active else None,
                    mode="continuous" if self.active else "inactive")
    def switch_microphone(self, name):
        self.changes.append(name)
        self.device = name or "Default mic"


class Microphones(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="myn-microphone-control-")
        self.addCleanup(temp.cleanup)
        self.config = Path(temp.name)
        self.settings = stt.STTSettings(device="Headset mic", language="ja", wake_words=stt.DEFAULT_WAKE_WORDS,
                                       always_listen=True)
        stt.save_settings(self.config, self.settings)
        self.client = Client()
        self.inventory = patch.object(audio_inputs, "inventory", return_value=dict(
            devices=["Headset mic", "Speaker mic"], default_device="Default mic"))
        self.inventory.start()
        self.addCleanup(self.inventory.stop)

    def test_inspect_reports_actual_fallback_separately_from_selection(self):
        self.client.device = "Default mic"
        result = audio_inputs.inspect(self.config, self.client)
        self.assertEqual(result["selected_device"], "Headset mic")
        self.assertEqual(result["actual_device"], "Default mic")
        self.assertEqual(len(result["available_devices"]), 2)
        self.assertEqual(self.client.changes, [])

    def test_active_switch_verified_persisted_and_preserves_other_settings(self):
        updated, result = audio_inputs.switch(self.config, self.client, "Speaker mic")
        self.assertTrue(result["applied"] and result["active"])
        self.assertEqual(result["actual_device"], "Speaker mic")
        self.assertEqual(stt.load_settings(self.config), updated)
        self.assertEqual(updated.language, "ja")
        self.assertEqual(updated.wake_words, stt.DEFAULT_WAKE_WORDS)
        self.assertTrue(updated.always_listen)

    def test_disabled_input_only_saves_selection(self):
        self.client.active = False
        stt.save_settings(self.config, stt.STTSettings(enabled=False, device="Headset mic"))
        updated, result = audio_inputs.switch(self.config, self.client, "Speaker mic")
        self.assertFalse(result["applied"] or result["active"])
        self.assertTrue(updated.enabled and updated.always_listen)
        self.assertIsNone(result["actual_device"])
        self.assertEqual(self.client.changes, [])

    def test_unknown_or_ambiguous_names_never_change_anything(self):
        for name in ("mic", "missing", None, "", "speaker MIC"):
            with self.assertRaises(stt.STTError):
                audio_inputs.switch(self.config, self.client, name)
        self.assertEqual(self.client.changes, [])
        self.assertEqual(stt.load_settings(self.config), self.settings)

    def test_save_failure_restores_live_device(self):
        with patch.object(stt, "save_settings", side_effect=stt.STTError("disk")):
            with self.assertRaises(stt.STTError):
                audio_inputs.switch(self.config, self.client, "Speaker mic")
        self.assertEqual(self.client.device, "Headset mic")
        self.assertEqual(stt.load_settings(self.config), self.settings)

    def test_unverified_device_cannot_report_success(self):
        self.client.switch_microphone = Mock()  # reports nothing, old device still active
        with self.assertRaises(stt.STTError):
            audio_inputs.switch(self.config, self.client, "Speaker mic")
        self.assertEqual(stt.load_settings(self.config), self.settings)

    def test_default_and_push_to_talk(self):
        updated, result = audio_inputs.switch(self.config, self.client, "系统默认麦克风")
        self.assertEqual(updated.device, "")
        self.assertEqual(result["actual_device"], "Default mic")
        self.client.audio_status = lambda: dict(active=True, mode="push_to_talk")
        with self.assertRaises(stt.STTError):
            audio_inputs.switch(self.config, self.client, "Speaker mic")

    def test_worker_resolves_actual_stream_index_and_rolls_back(self):
        recorder = SimpleNamespace(stream=None, sd=SimpleNamespace(
            query_devices=lambda i: dict(name={1:"Headset mic", 2:"Speaker mic"}[i])),
            input_devices=lambda: ["Headset mic", "Speaker mic"])
        class Listener:
            device = "Headset mic"
            stream = SimpleNamespace(device=1, active=True)
            active = True
            def start(self, name):
                self.device = name
                if name == "Speaker mic":
                    raise RuntimeError("unplugged")
                self.stream = SimpleNamespace(device=1, active=True)
                return name
            def stop(self):
                self.stream = None
        listener = Listener()
        self.assertEqual(stt_worker.audio_status(recorder, listener)["actual_device"], "Headset mic")
        with self.assertRaisesRegex(ValueError, 'restored'):
            stt_worker.switch_microphone(recorder, listener, "Speaker mic")
        self.assertEqual(stt_worker.audio_status(recorder, listener)["actual_device"], "Headset mic")

    def test_model_tool_honors_permission_and_propagates_failures(self):
        callback = Mock(return_value=dict(active=True, actual_device="Headset mic"))
        context = local_actions.ActionContext(self.config, microphone_control=callback)
        text, error = local_actions.execute("microphone_control", {"action":"inspect"}, context)
        self.assertFalse(error)
        self.assertEqual(json.loads(text)["actual_device"], "Headset mic")
        callback.side_effect = stt.STTError("unavailable")
        text, error = local_actions.execute("microphone_control", {"action":"switch","device":"missing"}, context)
        self.assertTrue(error)
        self.assertFalse(json.loads(text)["ok"])
        local_actions.save_settings(self.config, local_actions.ActionSettings(media=False))
        callback.reset_mock()
        self.assertTrue(local_actions.execute("microphone_control", {"action":"inspect"}, context)[1])
        callback.assert_not_called()

    def test_chat_can_inspect_then_switch_with_verified_receipts(self):
        import chat
        import claude_client
        from test_local_actions import reply
        session = chat.ChatSession(self.config / "jarvis.db")
        def control(action, device):
            return (audio_inputs.inspect(self.config, self.client) if action == "inspect"
                    else audio_inputs.switch(self.config, self.client, device)[1])
        session.microphone_control = control
        requests = []
        def opener(request, timeout):
            requests.append(json.loads(request.data))
            if len(requests) <= 2:
                args = {"action": "inspect"} if len(requests) == 1 else {
                    "action": "switch", "device": "Speaker mic"}
                return reply([dict(type="tool_use", id="mic_%s" % len(requests),
                                   name="microphone_control", input=args)], stop="tool_use")
            return reply([dict(type="text", text="已切换到 Speaker mic。")])
        with patch.object(session, "_key", return_value="synthetic"), \
                patch.object(claude_client.urllib.request, "urlopen", side_effect=opener):
            result = session.ask("把麦克风切换到 Speaker mic")
        self.assertIn("已切换", result["answer"])
        self.assertIn("microphone_control", [t["name"] for t in requests[0]["tools"]])
        receipt = json.loads(requests[-1]["messages"][-1]["content"][0]["content"])
        self.assertTrue(receipt["ok"] and receipt["applied"])
        self.assertEqual(receipt["actual_device"], "Speaker mic")
        self.assertEqual(stt.load_settings(self.config).device, "Speaker mic")


if __name__ == "__main__":
    unittest.main()
