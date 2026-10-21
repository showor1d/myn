"""Python 3.12 Qwen3-TTS inference loop. Only stdout carries framed WAV responses."""

import contextlib
import io
import json
import sys
import time


def respond(payload, audio=b""):
    sys.stdout.buffer.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
    if audio:
        sys.stdout.buffer.write(audio)
    sys.stdout.buffer.flush()


def main(model_path):
    startup_start = time.perf_counter()
    with contextlib.redirect_stdout(sys.stderr):
        import numpy as np
        import soundfile as sf
        import torch
        from qwen_tts import Qwen3TTSModel
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA is unavailable")
        load_start = time.perf_counter()
        model = Qwen3TTSModel.from_pretrained(
            model_path, device_map="cuda:0", dtype=torch.bfloat16,
            attn_implementation="sdpa",
        )
        model.model.eval()
        if not torch.cuda.is_available() or any(p.device.type != "cuda" for p in model.model.parameters()):
            raise RuntimeError("Qwen3-TTS CUDA model is unavailable")
        torch.cuda.synchronize()
        info = {"ready": True,
                "load_s": round(time.perf_counter() - load_start, 3),
                "startup_s": round(time.perf_counter() - startup_start, 3),
                "torch": torch.__version__, "cuda_runtime": torch.version.cuda,
                "gpu": torch.cuda.get_device_name(0),
                "cuda_parameters": sum(1 for _ in model.model.parameters()),
                "torch_allocated_mib": round(torch.cuda.memory_allocated() / 1048576)}
    speakers = set(model.get_supported_speakers())
    for line in sys.stdin.buffer:
        try:
            request = json.loads(line)
            if request.get("command") == "ready":
                respond(info)
                continue
            if request.get("command") != "synthesize":
                raise ValueError('Invalid local speech command.')
            segments = request["segments"]
            speaker = request["speaker"]
            speed = float(request["speed"])
            if (not isinstance(segments, list) or not segments or
                    any(not isinstance(text, str) or not text or len(text) > 3000 for text in segments) or
                    sum(map(len, segments)) > 3000):
                raise ValueError('Invalid text length.')
            if not isinstance(speaker, str) or speaker.lower() not in speakers or not 0.7 <= speed <= 1.2:
                raise ValueError('Invalid local voice or speed.')
            total_bytes = 0
            for index, text in enumerate(segments):
                with contextlib.redirect_stdout(sys.stderr), torch.inference_mode():
                    wavs, sample_rate = model.generate_custom_voice(
                        text=text, language="Auto", speaker=speaker,
                    )
                waveform = np.asarray(wavs[0], dtype=np.float32)
                if speed != 1.0:
                    import librosa
                    waveform = librosa.effects.time_stretch(waveform, rate=speed)
                output = io.BytesIO()
                sf.write(output, waveform, sample_rate, format="WAV", subtype="PCM_16")
                audio = output.getvalue()
                total_bytes += len(audio)
                if total_bytes > 8 * 1024 * 1024:
                    raise ValueError('Local audio exceeded 8 MB; further reading stopped.')
                respond({"ok": True, "index": index, "size": len(audio)}, audio)
            respond({"done": True})
        except Exception as error:
            if error.__class__.__name__ == "OutOfMemoryError":
                message = 'Qwen3-TTS ran out of GPU memory. Close GPU-heavy programs and retry.'
            elif isinstance(error, ValueError):
                message = str(error)
            else:
                message = 'Local Qwen3-TTS generation failed. Check the model and GPU runtime.'
            respond({"ok": False, "error": message})


if __name__ == "__main__":
    try:
        main(sys.argv[1])
    except Exception as error:
        message = ('Qwen3-TTS cannot use CUDA. Local speech will not fall back to CPU automatically.'
                   if "CUDA" in str(error) else 'Local Qwen3-TTS model loading failed.')
        respond({"ok": False, "error": message})
