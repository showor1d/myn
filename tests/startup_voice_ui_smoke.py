"""Actual Tk startup/event loop with one simulated transient speech-service failure."""
import os
from pathlib import Path
import sys
import tempfile
import time
from contextlib import ExitStack
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / 'src'), str(BASE / 'src/core')]

with tempfile.TemporaryDirectory(prefix='myn-startup-ui-') as folder:
    root_path = Path(folder)
    config, database = root_path / 'config', root_path / 'database/jarvis.db'
    database.parent.mkdir()
    os.environ.update(MYN_PERSONAL_DATA_DIR=folder, MYN_CONFIG_DIR=str(config),
                      MYN_DATA_DIR=str(database.parent), MYN_LEGACY_DATA_DIR=str(root_path / 'legacy'))
    import tkinter as tk
    import myn_gui
    import tts
    import interaction
    myn_gui.ChatSession(database)
    interaction.observe(config, 'Unresolved input from the previous run', semantic=True)
    original_state = (config / interaction.FILE).read_bytes()
    player = mock.Mock()
    player.active.return_value = False
    attempts = []
    def stream(text, settings, **kwargs):
        attempts.append(text)
        if len(attempts) == 1:
            raise tts.TTSError('Speech network request failed. The text reply was preserved.')
        return iter([b'synthetic-greeting'])
    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(myn_gui, 'prepare_data', return_value=(database, 'current')))
        stack.enter_context(mock.patch.object(myn_gui, 'MP3Player', return_value=player))
        stack.enter_context(mock.patch.object(myn_gui.web_frontend, 'available', return_value=False))
        stack.enter_context(mock.patch.object(myn_gui.stt, 'missing_parts', return_value=['test']))
        stack.enter_context(mock.patch.object(myn_gui.ChatSession, '_key', return_value=None))
        stack.enter_context(mock.patch.object(tts.ElevenLabsProvider, 'stream', side_effect=stream))
        for method in ('_make_frameless', '_self_update_startup', '_dev_startup', '_evolution_boot',
                       '_reanalyse_history_once', '_daily_usage_check', 'process_pending', '_poll_hotkey'):
            stack.enter_context(mock.patch.object(myn_gui.MYNWindow, method, lambda self, *a, **kw: None))
        root = tk.Tk()
        root.withdraw()
        errors = []
        root.report_callback_exception = lambda *error: errors.append(error)
        win = myn_gui.MYNWindow(root)
        player.enqueue.side_effect = lambda *args: player.on_start()
        try:
            deadline = time.monotonic() + 8
            while not win._greeting_audio_started and time.monotonic() < deadline:
                root.update()
                time.sleep(.02)
            assert not errors, errors
            assert len(attempts) == 2 and attempts[0] == attempts[1], attempts
            assert win.greeting_outcome == 'Audio playback started', win.greeting_outcome
            text = (config / 'voice-diagnostics.jsonl').read_text(encoding='utf-8')
            assert 'retry_scheduled' in text and 'playback_started' in text
            assert attempts[0] not in text
            assert (config / interaction.FILE).read_bytes() == original_state
            assert not interaction.allowed(config, 'proactive')
            print('PASS: real Tk startup speaks with pending idle state, retries once, preserves background silence and records player start.')
        finally:
            win.close()
    print('Speech service and playback simulated; all data temporary.')
