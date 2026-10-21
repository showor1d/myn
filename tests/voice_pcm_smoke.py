"""Real SDL/pygame PCM queue; dummy driver, generated signal, no audible output."""
import json
import math
import os
from pathlib import Path
import struct
import sys
import time

os.environ["SDL_AUDIODRIVER"] = "dummy"
os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src/core")]
from tts_player import MP3Player
from tts import PCMChunk


def main():
    player = MP3Player()
    starts = []
    player.on_start = lambda: starts.append(time.perf_counter())
    started = time.perf_counter()
    try:
        for n in range(4):
            samples = b"".join(struct.pack("<h", int(3000 * math.sin(
                2 * math.pi * 440 * (n * 6000 + i) / 24000))) for i in range(6000))
            player.enqueue(PCMChunk(samples), 0)
        deadline = time.monotonic() + 3
        while player.active() and time.monotonic() < deadline:
            player.poll()
            time.sleep(.01)
        assert not player.active(), "playback stuck"
        assert len(starts) == 1, "Unexpected buffer underrun/restart"
        result = {"driver": "dummy", "buffers": 4, "start_calls": len(starts),
                  "elapsed_s": round(time.perf_counter()-started, 3),
                  "mixer": player.mixer.get_init(), "audible": False}
        (ROOT / "test-voice-latency-player-results.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result))
    finally:
        player.close()


if __name__ == "__main__":
    main()
