"""Opt-in real input/output routing check. No recorded audio is saved or transcribed."""
import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

BASE = Path(__file__).resolve().parents[1]


def capture(name):
    import sounddevice as sd
    apis = sd.query_hostapis()
    device = next(i for i, d in enumerate(sd.query_devices())
                  if d["name"] == name and d["max_input_channels"] > 0
                  and "WASAPI" in apis[d["hostapi"]]["name"])
    frames_seen = [0]
    def discard(_data, frames, _time, _status):
        frames_seen[0] += frames  # count delivery, never retain the sample buffer
    with sd.InputStream(device=device, channels=1,
                        samplerate=int(sd.query_devices(device)["default_samplerate"]),
                        callback=discard):
        print(json.dumps({"ready": True}), flush=True)
        sys.stdin.readline()
    print(json.dumps({"input_frames": frames_seen[0]}), flush=True)


def check(speaker, microphone):
    sys.path[:0] = [str(BASE / "src"), str(BASE / "src/core")]
    from tts import PCMChunk
    from tts_player import MP3Player, output_devices
    assert speaker in output_devices()
    env = dict(os.environ, PYTHONUTF8="1", PYGAME_HIDE_SUPPORT_PROMPT="1")
    worker = subprocess.Popen(
        [str(BASE / "local-tts/.venv/Scripts/python.exe"), __file__, "--capture", microphone],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", env=env, creationflags=subprocess.CREATE_NO_WINDOW)
    player = MP3Player()
    ready = queue.Queue()
    threading.Thread(target=lambda: ready.put(worker.stdout.readline()), daemon=True).start()
    try:
        assert json.loads(ready.get(timeout=15))["ready"]
        player.set_output_device(speaker)
        starts = []
        player.on_start = lambda: starts.append(time.monotonic())
        player.enqueue(PCMChunk(b"\0\0" * 12000), 0)
        deadline = time.monotonic() + 5
        while player.active() and time.monotonic() < deadline:
            player.poll()
            time.sleep(.01)
        assert starts and not player.active()
        output, errors = worker.communicate("\n", timeout=5)
        assert worker.returncode == 0, errors
        received = json.loads(output)["input_frames"]
        assert received > 0
        print(json.dumps(dict(output=speaker, input=microphone, input_frames=received,
                              silent_output_seconds=.5, audio_saved=False, passed=True),
                         ensure_ascii=False), flush=True)
    finally:
        player.close()
        if worker.poll() is None:
            worker.terminate()
            worker.communicate(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture")
    parser.add_argument("--speaker")
    parser.add_argument("--microphone")
    args = parser.parse_args()
    capture(args.capture) if args.capture else check(args.speaker, args.microphone)
