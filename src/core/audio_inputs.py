"""Inspect and change MYN's input route; never change Windows defaults or output."""
import json
import os
import subprocess
from dataclasses import replace
import stt

_lock = stt.SETTINGS_LOCK


def inventory():
    """Only enumerate hardware; do not load Whisper or start a recording."""
    try:
        result = subprocess.run([str(stt.PYTHON), str(stt.WORKER), "--audio-devices"],
            capture_output=True, text=True, encoding="utf-8", timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env=dict(os.environ, PYTHONUTF8="1"))
        data = json.loads(result.stdout)
        if result.returncode or not data.get("ok"):
            raise ValueError()
        return data
    except (OSError, ValueError, subprocess.SubprocessError):
        raise stt.STTError('Cannot read the microphone list. Check recording components and device connections.')


def inspect(config, client):
    devices = inventory()
    selected = stt.load_settings(config)
    live = client.audio_status()
    return dict(selected_device=selected.device or 'System default microphone',
                follows_system_default=not selected.device,
                default_device=devices.get("default_device"),
                available_devices=devices["devices"],
                enabled=selected.enabled, always_listen=selected.always_listen,
                actual_device=live.get("actual_device"), active=bool(live.get("active")),
                mode=live.get("mode", "inactive"))


def switch(config, client, device):
    if not isinstance(device, str) or not device or len(device) > 200:
        raise stt.STTError('Check the device list first, then provide the full microphone name. Use "System default microphone" for the default device.')
    target = "" if device.casefold() in ('system default microphone', '系统默认麦克风') else device
    with _lock:
        devices = inventory()
        if target and target not in devices["devices"]:
            raise stt.STTError('Microphone not found. Select its full name from the current device list; do not guess or switch automatically.')
        previous = stt.load_settings(config)
        live = client.audio_status()
        if live.get("mode") == "push_to_talk":
            raise stt.STTError('Recording is in progress. Release the button and finish recording before changing microphones.')
        was_active = bool(live.get("active"))
        changed = replace(previous, device=target).validate()
        try:
            if was_active:
                client.switch_microphone(target)
                verified = client.audio_status()
                if not verified.get("active") or not verified.get("actual_device") or \
                        (target and verified["actual_device"] != target):
                    raise stt.STTError('Cannot verify that the new microphone is recording.')
            else:
                verified = dict(active=False, actual_device=None, mode="inactive")
            if stt.load_settings(config) != previous:
                raise stt.STTError('Audio settings changed in another operation. Refresh and try again.')
            stt.save_settings(config, changed)
            if stt.load_settings(config) != changed:
                raise stt.STTError('Cannot verify the saved microphone settings.')
        except Exception as error:
            restored = not was_active
            config_restored = True
            try:
                if stt.load_settings(config) == changed and changed != previous:
                    stt.save_settings(config, previous)
            except Exception:
                config_restored = False
            if was_active:
                try:
                    state = client.audio_status()
                    restored = state.get("active") and state.get("actual_device") == live.get("actual_device")
                    if not restored:
                        client.switch_microphone(previous.device)
                        state = client.audio_status()
                        restored = state.get("active") and state.get("actual_device") == live.get("actual_device")
                except Exception:
                    restored = False
            raise stt.STTError(('Switch failed. The previous device was preserved or restored.' if restored else
                                'Switch failed and the previous device could not be restored. Check the device connection.') +
                               ("" if config_restored else 'Settings could not be restored. Check data folder permissions.') +
                               str(error)[:160])
        return changed, dict(selected_device=target or 'System default microphone',
            actual_device=verified.get("actual_device"), active=bool(verified.get("active")),
            saved=True, applied=was_active,
            message='Microphone switched and verified.' if was_active else
                    'Selection saved. Recording is inactive; the selection applies next time voice is used. The microphone was not enabled automatically.')
