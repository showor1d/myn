"""WASAPI shared render loopback, owned entirely by the meter thread.

Only the latest FFT window stays in memory. No driver, file, network, playback,
or microphone capture. Interfaces and packet buffers are released on that thread.
"""
import ctypes as C
import struct
import time

from audio_spectrum import Spectrum, SIZE


def decode_pcm(raw, channels, bits, floating):
    """Keep channels separate: anti-phase stereo must not cancel visualization."""
    if floating and bits == 32:
        values = [v[0] for v in struct.iter_unpack('<f', raw)]
    elif not floating and bits in (16, 32):
        fmt = '<h' if bits == 16 else '<i'
        divisor = float(2 ** (bits - 1))
        values = [v[0] / divisor for v in struct.iter_unpack(fmt, raw)]
    elif not floating and bits == 24:
        values = [int.from_bytes(raw[i:i+3], 'little', signed=True) / 8388608.
                  for i in range(0, len(raw), 3)]
    else:
        raise ValueError('Unsupported loopback PCM format')
    # Analyze the strongest channel of each packet instead of averaging phases.
    tracks = [values[i::channels] for i in range(channels)]
    return max(tracks, key=lambda track: sum(x*x for x in track), default=[])


class Loopback:
    def __init__(self, device, ole):
        from output_meter import GUID, _call
        self.client, self.capture = C.c_void_p(), C.c_void_p()
        self.started = False
        self.spectrum = Spectrum()
        self.last_packet = 0.
        fmt = C.c_void_p()
        try:
            iid = GUID('1CB9AD4C-DBFA-4C32-B178-C2F568A703B2')
            _call(device, 3, (C.c_void_p, C.c_ulong, C.c_void_p, C.c_void_p),
                  (C.byref(iid), 23, None, C.byref(self.client)))
            _call(self.client, 8, (C.c_void_p,), (C.byref(fmt),))
            tag, channels, rate, _, align, bits, extra = struct.unpack('<HHIIHHH', C.string_at(fmt, 18))
            if tag == 0xfffe and extra >= 22:
                subtype = C.string_at(fmt.value + 24, 16)
                if subtype not in (bytes(GUID('00000001-0000-0010-8000-00aa00389b71')),
                                   bytes(GUID('00000003-0000-0010-8000-00aa00389b71'))):
                    raise ValueError('Unsupported loopback subtype')
                tag = int.from_bytes(subtype[:4], 'little')
            if (tag not in (1, 3) or bits not in (16, 24, 32) or
                    (tag == 3 and bits != 32) or not 1 <= channels <= 32 or
                    align != channels * bits // 8 or not 8000 <= rate <= 384000):
                raise ValueError('Unsupported loopback format')
            self.channels, self.rate, self.align = channels, rate, align
            self.bits, self.floating = bits, tag == 3
            # Shared mode + LOOPBACK, 200 ms buffer, polling without callbacks.
            _call(self.client, 3, (C.c_int, C.c_ulong, C.c_longlong, C.c_longlong, C.c_void_p, C.c_void_p),
                  (0, 0x20000, 2000000, 0, fmt, None))
            iid = GUID('C8ADBD64-E71E-48A0-A4DE-185C395CD317')
            _call(self.client, 14, (C.c_void_p, C.c_void_p), (C.byref(iid), C.byref(self.capture)))
            _call(self.client, 10)
            self.started = True
        except Exception:
            self.close()
            raise
        finally:
            if fmt:
                ole.CoTaskMemFree.argtypes = [C.c_void_p]
                ole.CoTaskMemFree(fmt)

    def read(self, gain):
        from output_meter import _call
        for _ in range(64):  # Bound each polling pass even on a misbehaving device.
            available = C.c_uint()
            _call(self.capture, 5, (C.c_void_p,), (C.byref(available),))
            if not available.value:
                break
            data, frames, flags = C.c_void_p(), C.c_uint(), C.c_ulong()
            _call(self.capture, 3, (C.c_void_p,) * 5,
                  (C.byref(data), C.byref(frames), C.byref(flags), None, None))
            try:
                if not frames.value:
                    break
                if flags.value & 1:  # discontinuity: do not join unrelated samples
                    self.spectrum.clear()
                if flags.value & 2 or not data:
                    self.spectrum.clear()
                else:
                    kept = min(frames.value, SIZE)
                    raw = C.string_at(data.value + (frames.value-kept)*self.align, kept*self.align)
                    self.spectrum.feed(decode_pcm(raw, self.channels, self.bits, self.floating), self.rate)
                self.last_packet = time.monotonic()
            finally:
                _call(self.capture, 4, (C.c_uint,), (frames.value,))
        if time.monotonic() - self.last_packet > .15:
            self.spectrum.clear()
        return self.spectrum.bands(gain)

    def close(self):
        from output_meter import _call, _release
        if self.started:
            try:
                _call(self.client, 11)
            except OSError:
                pass
            self.started = False
        _release(self.capture)
        _release(self.client)
        self.capture, self.client = C.c_void_p(), C.c_void_p()
        self.spectrum.clear()
