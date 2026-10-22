"""Exercise actual SDL decoding/channel lifetime without using a physical speaker."""
import os
from pathlib import Path
import sys
import time

os.environ['SDL_AUDIODRIVER'] = 'dummy'
base = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(base / 'src'), str(base / 'src/core')]
from tts_player import MP3Player

player = MP3Player()
try:
    player.play_wake_chime(.6)
    channel = player.cue_channel
    assert channel.get_busy(), 'SDL did not start the cue'
    player.stop()
    assert channel.get_busy(), 'Speech interruption truncated the cue'
    deadline = time.monotonic() + 2
    while channel.get_busy() and time.monotonic() < deadline:
        time.sleep(.01)
    assert not channel.get_busy(), 'Cue did not finish'
    print('PASS: real SDL WAV decoding and independent 180 ms cue lifetime (dummy audio output).')
finally:
    player.close()
