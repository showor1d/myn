"""GUI-thread-only MP3/WAV playback. Synthesis lives in MYNWindow workers."""

import io
from collections import deque

from tts import TTSError, PCMChunk

_audio_inventory_ready = False


def output_devices():
    """List SDL output endpoints without opening a playback stream."""
    global _audio_inventory_ready
    try:
        from pygame._sdl2 import INIT_AUDIO, init_subsystem
        from pygame._sdl2.audio import get_audio_device_names
        if not _audio_inventory_ready:
            init_subsystem(INIT_AUDIO)
            _audio_inventory_ready = True
        return list(dict.fromkeys(get_audio_device_names(False)))
    except Exception as error:
        raise TTSError('Cannot read playback devices. Check speaker connections and refresh.') from error


class MP3Player:
    def __init__(self):
        self.mixer = None
        self.stream = None
        self.pending = deque()
        self.base_volume = 1.0
        self.duck = 1.0  # < 1 while you start talking over MYN (it gets quiet at once)
        self.paused = False  # you started talking: MYN stops mid-word and listens
        self.pcm_pending = deque()
        self.pcm_channel = None
        self.pcm_rate_state = None
        self.on_start = None
        self.output_device = ""

    def set_output_device(self, name):
        if not isinstance(name, str) or len(name) > 200 or "\0" in name:
            raise TTSError('Invalid playback device name.')
        if name != self.output_device:
            self.close()
            self.output_device = name

    def _music(self):
        global _audio_inventory_ready
        if self.mixer is None:
            try:
                from pygame import mixer
                if self.output_device or not mixer.get_init():
                    # SDL-only device enumeration makes pygame skip its channel
                    # setup unless the audio subsystem is released first.
                    mixer.quit()
                    _audio_inventory_ready = False
                if not mixer.get_init():
                    mixer.init(devicename=self.output_device) if self.output_device else mixer.init()
                if not mixer.get_init():
                    raise RuntimeError("Audio device did not initialize")
                self.mixer = mixer
            except Exception as error:
                if self.output_device:
                    raise TTSError('Cannot open playback device "%s". Connect it or select another in voice settings.' %
                                   self.output_device) from error
                raise TTSError('Cannot start audio playback. Install pygame and check audio devices.') from error
        return self.mixer.music

    def play(self, audio, volume):
        self.pending.clear()
        self.pcm_pending.clear()
        self.pcm_rate_state = None
        if self.pcm_channel is not None:
            self.pcm_channel.stop()
            self.pcm_channel = None
        self._play_now(audio, volume)

    def _play_now(self, audio, volume):
        music = self._music()
        try:
            music.stop()
            music.unload()
            self.stream = io.BytesIO(audio)
            namehint = "wav" if audio.startswith(b"RIFF") and audio[8:12] == b"WAVE" else "mp3"
            music.load(self.stream, namehint)
            self.base_volume = volume
            music.set_volume(volume * self.duck)
            music.play()
            if self.on_start:
                self.on_start()
        except Exception as error:
            self.stream = None
            raise TTSError('Audio decoding or playback failed. The text reply was preserved.') from error

    def enqueue(self, audio, volume):
        if isinstance(audio, PCMChunk):
            self.pcm_pending.append((audio, volume))
            self.poll()
            return
        self.pending.append((audio, volume))
        try:
            self.poll()
        except TTSError:
            self.pending.clear()
            raise

    def poll(self):
        if self.pcm_pending or (self.pcm_channel is not None and self.pcm_channel.get_busy()):
            self._poll_pcm()
            return
        if not self.pending or self.paused:
            return
        try:
            music = self._music()
        except TTSError:
            self.pending.clear()
            raise
        if not music.get_busy():
            audio, volume = self.pending.popleft()
            self._play_now(audio, volume)

    def _pcm_sound(self, chunk):
        import audioop
        frequency, size, channels = self.mixer.get_init()
        if size != -16 or channels not in (1, 2):
            raise TTSError('Streaming speech requires a 16-bit audio device.')
        data = chunk.samples
        if frequency != chunk.rate:
            data, self.pcm_rate_state = audioop.ratecv(
                data, 2, 1, chunk.rate, frequency, self.pcm_rate_state)
        if channels == 2:
            data = audioop.tostereo(data, 2, 1, 1)
        return self.mixer.Sound(buffer=data)

    def _poll_pcm(self):
        if self.paused:
            return
        try:
            self._music()  # initialize the same mixer used by normal audio
            if self.pcm_channel is None:
                self.pcm_channel = self.mixer.find_channel()
                if self.pcm_channel is None:
                    raise TTSError('Audio playback channel is temporarily unavailable.')
            channel = self.pcm_channel
            while self.pcm_pending:
                if channel.get_busy() and channel.get_queue() is not None:
                    break
                chunk, self.base_volume = self.pcm_pending.popleft()
                sound = self._pcm_sound(chunk)
                channel.set_volume(self.base_volume * self.duck)
                if channel.get_busy():
                    channel.queue(sound)  # SDL starts the next buffer without waiting for Tk
                else:
                    channel.play(sound)
                    if self.on_start:
                        self.on_start()
        except Exception as error:
            self.pcm_pending.clear()
            if self.pcm_channel is not None:
                self.pcm_channel.stop()
            if isinstance(error, TTSError):
                raise
            raise TTSError('Streaming audio playback failed. The text reply was preserved.') from error

    def active(self):
        """True while audio is playing, paused or queued (MYN is speaking)."""
        if self.pending or self.pcm_pending or self.paused:
            return True
        try:
            if self.pcm_channel is not None and self.pcm_channel.get_busy():
                return True
            return self.mixer is not None and bool(self.mixer.music.get_busy())
        except Exception:
            return False

    def set_volume(self, volume):
        self.base_volume = volume
        if self.mixer is not None:
            self.mixer.music.set_volume(volume * self.duck)
        if self.pcm_channel is not None:
            self.pcm_channel.set_volume(volume * self.duck)

    def set_duck(self, factor):
        """Make MYN quieter (factor < 1) right away, or back to normal (1)."""
        self.duck = max(0.0, min(1.0, factor))
        if self.pcm_channel is not None:
            self.pcm_channel.set_volume(self.base_volume * self.duck)
        if self.mixer is not None:
            try:
                self.mixer.music.set_volume(self.base_volume * self.duck)
            except Exception:
                pass

    def pause(self):
        """Stop talking at once, able to go on from the same spot (resume)."""
        if self.pcm_channel is not None:
            self.pcm_channel.pause()
        if self.mixer is not None and not self.paused:
            try:
                self.mixer.music.pause()
            except Exception:
                return
            self.paused = True

    def resume(self):
        if self.paused:
            self.paused = False
            if self.pcm_channel is not None:
                self.pcm_channel.unpause()
            if self.mixer is not None:
                try:
                    self.mixer.music.unpause()
                except Exception:
                    pass

    def stop(self):
        self.pending.clear()
        self.pcm_pending.clear()
        self.pcm_rate_state = None
        if self.pcm_channel is not None:
            self.pcm_channel.stop()
            self.pcm_channel = None
        self.duck = 1.0
        self.paused = False
        if self.mixer is not None:
            self.mixer.music.stop()
            self.mixer.music.unload()
        self.stream = None

    def close(self):
        global _audio_inventory_ready
        self.stop()
        if self.mixer is not None:
            self.mixer.quit()
            _audio_inventory_ready = False
            self.mixer = None
