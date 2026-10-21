"""Resolve alarm sounds only from the portable personal Music library."""
from pathlib import Path
import json
import math
import os
import re
import threading
from media_library import MediaLibrary

LOCK = threading.RLock()
SETTINGS = 'alarm-music-settings.json'


def settings(config):
    with LOCK:
        try:
            data = json.loads((Path(config) / SETTINGS).read_text(encoding='utf-8'))
        except FileNotFoundError:
            return dict(ringtone_id='', ringtone_name='', volume=.8)
        if (not isinstance(data, dict) or not isinstance(data.get('ringtone_id'), str)
                or not re.fullmatch(r'(?:[a-f0-9]{64})?', data['ringtone_id'])
                or not isinstance(data.get('ringtone_name'), str)):
            raise ValueError('Alarm settings are damaged; the original file was preserved.')
        _volume(data.get('volume'))
        return data


def _volume(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError('Alarm volume must be 0-100%.')


def configure(config, **changes):
    with LOCK:
        data = settings(config)
        if 'ringtone_id' in changes:
            data.update(sound(config, changes['ringtone_id']))
        if 'volume' in changes:
            _volume(changes['volume'])
            data['volume'] = float(changes['volume'])
        path = Path(config) / SETTINGS
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        os.replace(str(temporary), str(path))
        if settings(config) != data:
            raise ValueError('Cannot verify saved alarm settings. Query them before trying again.')
        return data


def choices(config):
    return [dict(id=r['id'], name=r['name']) for r in
            MediaLibrary(Path(config).parent / 'media').scan() if r['mode'] == 'music']


def sound(config, ident):
    if ident == '':
        return dict(ringtone_id='', ringtone_name='')
    if not isinstance(ident, str):
        raise ValueError('Select a song from the music library.')
    row = next((r for r in choices(config) if r['id'] == ident), None)
    if row is None:
        raise ValueError('This song is not in the library. Select the alarm sound again.')
    return dict(ringtone_id=row['id'], ringtone_name=row['name'])


