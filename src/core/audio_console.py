"""Portable audio-console preferences; live capture/send state is never persisted."""
import json
import math
import os
import secrets
from pathlib import Path

FREQUENCIES = (60, 170, 350, 1000, 3500, 10000)


def defaults():
    return dict(music_eq=[0]*6, mic_eq=[0]*6, music_enabled=True,
                mic_clean=True, effect='natural', pitch=0, mic_gain=1,
                music_send=0.5, mic_send=1, monitor_gain=0.3,
                input_device='', listen_device='', send_device='',
                input_name='', listen_name='', send_name='')


def validate(value):
    if not isinstance(value, dict) or set(value) != set(defaults()):
        raise ValueError('Invalid audio setting field.')
    result = defaults()
    for key, item in value.items():
        if key.endswith('_eq'):
            if not isinstance(item, list) or len(item) != 6:
                raise ValueError('Invalid equalizer band.')
            for number in item:
                _number(number, -12, 12)
        elif key in ('music_enabled', 'mic_clean'):
            if not isinstance(item, bool):
                raise ValueError('Invalid audio toggle.')
        elif key.endswith(('_device', '_name')):
            if not isinstance(item, str) or len(item) > 512:
                raise ValueError('Invalid audio device.')
        elif key == 'effect':
            if item not in ('natural', 'pitch', 'robot', 'telephone'):
                raise ValueError('Invalid voice effect.')
        else:
            _number(item, -12 if key == 'pitch' else 0,
                    12 if key == 'pitch' else 2 if key == 'mic_gain' else 1)
        result[key] = item
    return result


def _number(value, low, high):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not math.isfinite(value) or not low <= value <= high):
        raise ValueError('Audio value is out of range.')


def load(root):
    path = Path(root) / '.audio-console.json'
    return validate(json.loads(path.read_text(encoding='utf-8'))) if path.exists() else defaults()


def save(root, value):
    data = validate(value)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / '.audio-console.json'
    temporary = root / ('.audio-console-' + secrets.token_hex(12))
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()
    return data
