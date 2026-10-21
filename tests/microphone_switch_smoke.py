"""Opt-in hardware switch on an isolated stream/config. Samples are discarded."""
import json
import sys
import tempfile
from pathlib import Path
import time
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "src"),
               str(Path(__file__).resolve().parents[1] / "src/core")]
import numpy as np
import sounddevice as sd
import stt, stt_worker, audio_inputs


def main():
    first = "Microphone (HyperX Cloud III Wireless)"
    second = "Microphone (花再 Halo PixelBar)"
    recorder = stt_worker.Recorder(sd, np)
    listener = stt_worker.Listener(recorder, np, lambda *_: "", lambda _: None, threaded=False)
    # Only count callbacks; do not retain/transcribe captured samples.
    frames = [0]
    def discard(data, count, _time, _status):
        frames[0] += count
    listener._callback = discard
    class Client:
        def audio_status(self):
            return stt_worker.audio_status(recorder, listener)
        def switch_microphone(self, name):
            return stt_worker.switch_microphone(recorder, listener, name)
    with tempfile.TemporaryDirectory(prefix="myn-real-mic-") as folder:
        stt.save_settings(folder, stt.STTSettings(device=first, always_listen=True))
        try:
            listener.start(first)
            time.sleep(.15)
            states = [audio_inputs.inspect(folder, Client())["actual_device"]]
            for name in (second, first):
                _, result = audio_inputs.switch(folder, Client(), name)
                assert result["actual_device"] == name and result["applied"], result
                states.append(result["actual_device"])
                time.sleep(.15)
            assert frames[0] > 0
            print(json.dumps(dict(passed=True, devices=states, frames_discarded=frames[0],
                                 saved_audio=False, real_settings_modified=False), ensure_ascii=False))
        finally:
            listener.stop()


if __name__ == "__main__":
    main()
