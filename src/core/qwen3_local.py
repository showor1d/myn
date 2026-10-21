"""Single local Qwen3-TTS worker; keeps model dependencies out of MYN's Python 3.9."""

import json
import os
import subprocess
import threading
from pathlib import Path

from worker_process import terminate


ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "local-tts"
PYTHON = RUNTIME / ".venv" / "Scripts" / "python.exe"
MODEL = RUNTIME / "model"
WORKER = Path(__file__).with_name("qwen3_tts_worker.py")
MAX_AUDIO_BYTES = 8 * 1024 * 1024


class LocalTTSClient:
    _terminate = staticmethod(terminate)

    def __init__(self):
        self.lock = threading.Lock()
        self.process = None
        self.active = None
        self.generation = 0
        self.diagnostics = None

    def _start(self):
        from tts import TTSError
        if not PYTHON.is_file() or not (MODEL / "config.json").is_file():
            raise TTSError('Local Qwen3-TTS runtime or model is missing. Check D:\\\\MYN\\\\local-tts.')
        env = os.environ.copy()
        env["HF_HUB_OFFLINE"] = "1"
        env["TRANSFORMERS_OFFLINE"] = "1"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.diagnostics = None
            self.process = subprocess.Popen(
                [str(PYTHON), "-u", str(WORKER), str(MODEL)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                cwd=str(RUNTIME), env=env, creationflags=flags,
            )
        except OSError as error:
            raise TTSError('Cannot start the local Qwen3-TTS process. Check the runtime.') from error

    @staticmethod
    def _read_exact(process, size):
        chunks = []
        while size:
            chunk = process.stdout.read(size)
            if not chunk:
                raise OSError("local worker exited")
            chunks.append(chunk)
            size -= len(chunk)
        return b"".join(chunks)

    def _write(self, process, payload):
        request = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
        process.stdin.write(request)
        process.stdin.flush()

    @staticmethod
    def _header(process):
        header = process.stdout.readline(4096)
        if not header.endswith(b"\n"):
            raise OSError("invalid local worker response")
        return json.loads(header)

    def warm(self, expected_generation):
        """Load the model before the first utterance, off the GUI thread."""
        from tts import TTSError
        if expected_generation != self.generation:
            return
        with self.lock:
            if expected_generation != self.generation:
                return
            if self.process is None or self.process.poll() is not None:
                self._start()
            process = self.process
            self.active = "warm"
            timer = threading.Timer(120, self._terminate, args=(process,))
            timer.daemon = True
            try:
                timer.start()
                self._write(process, {"command": "ready"})
                result = self._header(process)
                if not result.get("ready"):
                    self._terminate(process)
                    raise TTSError(result.get("error", 'Local Qwen3-TTS model preload failed.'))
                self.diagnostics = result
            except TTSError:
                raise
            except (OSError, ValueError, json.JSONDecodeError) as error:
                raise TTSError('Local Qwen3-TTS model preload failed.') from error
            finally:
                timer.cancel()
                self.active = None
                if process.poll() is not None and self.process is process:
                    self.process = None

    def synthesize_chunks(self, segments, speaker, speed):
        from tts import TTSError
        if not segments or sum(map(len, segments)) > 3000:
            raise TTSError('Speech text must contain 1-3000 characters.')
        generation = self.generation
        with self.lock:
            if generation != self.generation:
                raise TTSError('Local speech request cancelled.')
            if self.process is None or self.process.poll() is not None:
                self._start()
            process = self.process
            self.active = "speech"
            timer = threading.Timer(300, self._terminate, args=(process,))
            timer.daemon = True
            try:
                timer.start()
                self._write(process, {"command": "synthesize", "segments": segments,
                                      "speaker": speaker, "speed": speed})
                total_bytes = 0
                received = 0
                while True:
                    result = self._header(process)
                    if result.get("done"):
                        if received != len(segments):
                            raise OSError("missing local audio chunks")
                        break
                    if not result.get("ok"):
                        raise TTSError(result.get("error", 'Local Qwen3-TTS generation failed.'))
                    size = result.get("size")
                    if (not isinstance(size, int) or not 44 <= size <= MAX_AUDIO_BYTES or
                            result.get("index") != received):
                        raise OSError("invalid local audio header")
                    total_bytes += size
                    if total_bytes > MAX_AUDIO_BYTES:
                        self._terminate(process)
                        raise TTSError('Qwen3-TTS audio exceeded 8 MB; further reading stopped.')
                    audio = self._read_exact(process, size)
                    if not audio.startswith(b"RIFF") or audio[8:12] != b"WAVE":
                        raise OSError("invalid local audio")
                    received += 1
                    # After cancel() the rest of this request is read and dropped, so the
                    # worker (and the model loaded in it) stays in sync and alive.
                    if generation == self.generation:
                        yield audio
            except TTSError:
                raise
            except (OSError, ValueError, json.JSONDecodeError) as error:
                self._terminate(process)
                raise TTSError('Local Qwen3-TTS process stopped or returned invalid data. The text reply was preserved.') from error
            finally:
                timer.cancel()
                self.active = None
                if process.poll() is not None and self.process is process:
                    self.process = None

    def synthesize(self, text, speaker, speed):
        return list(self.synthesize_chunks([text], speaker, speed))[0]

    def cancel(self):
        """Make pending audio stale without killing the worker.

        Killing it used to force a full model reload (about 5 s) before the next
        sentence. Requests are one sentence long, so the in-flight one finishes
        quickly and is discarded; close() and the hang timer still terminate.
        """
        self.generation += 1

    def close(self):
        self.generation += 1
        process = self.process
        self.process = None
        if process is not None and process.poll() is None:
            self._terminate(process)


client = LocalTTSClient()
