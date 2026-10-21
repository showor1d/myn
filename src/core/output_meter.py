"""Windows output peaks and in-memory loopback spectrum, on one COM thread."""
import ctypes as C
import math
import os
import threading
import time
import uuid


class GUID(C.Structure):
    _fields_ = [('a', C.c_uint32), ('b', C.c_uint16), ('c', C.c_uint16), ('d', C.c_ubyte*8)]

    def __init__(self, value):
        super().__init__()
        C.memmove(C.byref(self), uuid.UUID(value).bytes_le, 16)


def _call(pointer, slot, types=(), args=()):
    table = C.cast(pointer, C.POINTER(C.POINTER(C.c_void_p))).contents
    fn = C.WINFUNCTYPE(C.c_long, C.c_void_p, *types)(table[slot])
    result = fn(pointer, *args)
    if result < 0:
        raise OSError('Audio meter HRESULT 0x%08X' % (result & 0xffffffff))
    return result


def _release(pointer):
    if pointer:
        _call(pointer, 2)


class WindowsPeaks:
    """All COM references are created, read and released on one worker thread."""
    def __init__(self):
        if os.name != 'nt':
            raise OSError('Windows audio meters are unavailable')
        self.ole = C.WinDLL('ole32')
        self.ole.CoInitializeEx.argtypes = [C.c_void_p, C.c_ulong]
        self.ole.CoInitializeEx.restype = C.c_long
        self.ole.CoCreateInstance.argtypes = [C.c_void_p, C.c_void_p, C.c_ulong, C.c_void_p, C.c_void_p]
        self.ole.CoCreateInstance.restype = C.c_long
        self.pairs = []
        self.loopbacks = []
        self.bands = []
        self.device_ids = []
        self.initialized = self.ole.CoInitializeEx(None, 0) >= 0
        if not self.initialized:
            raise OSError('Audio meter COM initialization failed')
        enumerator, collection = C.c_void_p(), C.c_void_p()
        try:
            cls = GUID('BCDE0395-E52F-467C-8E3D-C4579291692E')
            iid = GUID('A95664D2-9614-4F35-A746-DE8DB63617E6')
            hr = self.ole.CoCreateInstance(C.byref(cls), None, 23, C.byref(iid), C.byref(enumerator))
            if hr < 0:
                raise OSError('Audio device enumeration failed')
            _call(enumerator, 3, (C.c_int, C.c_ulong, C.c_void_p), (0, 1, C.byref(collection)))
            count = C.c_uint()
            _call(collection, 3, (C.c_void_p,), (C.byref(count),))
            for i in range(count.value):
                device, meter, volume = C.c_void_p(), C.c_void_p(), C.c_void_p()
                try:
                    _call(collection, 4, (C.c_uint, C.c_void_p), (i, C.byref(device)))
                    self.device_ids.append(self._device_id(device))
                    for ident, pointer in [('C02216F6-8C67-4B5B-9D00-D008E73E0064', meter),
                                           ('5CDF2C82-841E-4546-9722-0CF74078229A', volume)]:
                        interface = GUID(ident)
                        _call(device, 3, (C.c_void_p, C.c_ulong, C.c_void_p, C.c_void_p),
                              (C.byref(interface), 23, None, C.byref(pointer)))
                    self.pairs.append((meter, volume))
                    try:
                        from output_loopback import Loopback
                        loopback = Loopback(device, self.ole)
                    except (OSError, ValueError):
                        loopback = None  # Meter-only fallback never fabricates peaks.
                    self.loopbacks.append(loopback)
                except OSError:
                    _release(meter)
                    _release(volume)
                finally:
                    _release(device)
        except Exception:
            _release(collection)
            collection = None
            _release(enumerator)
            enumerator = None
            self.close()
            raise
        finally:
            _release(collection)
            _release(enumerator)

    def _device_id(self, device):
        value = C.c_void_p()
        try:
            _call(device, 5, (C.c_void_p,), (C.byref(value),))
            return C.wstring_at(value)
        finally:
            if value:
                self.ole.CoTaskMemFree.argtypes = [C.c_void_p]
                self.ole.CoTaskMemFree(value)

    def unchanged(self):
        """Discover endpoint changes without reopening healthy streams every 2 s."""
        if any(stream is None for stream in self.loopbacks):
            return False
        enumerator, collection = C.c_void_p(), C.c_void_p()
        identifiers = []
        try:
            cls, iid = GUID('BCDE0395-E52F-467C-8E3D-C4579291692E'), GUID('A95664D2-9614-4F35-A746-DE8DB63617E6')
            hr = self.ole.CoCreateInstance(C.byref(cls), None, 23, C.byref(iid), C.byref(enumerator))
            if hr < 0:
                return False
            _call(enumerator, 3, (C.c_int, C.c_ulong, C.c_void_p), (0, 1, C.byref(collection)))
            count = C.c_uint()
            _call(collection, 3, (C.c_void_p,), (C.byref(count),))
            for i in range(count.value):
                device = C.c_void_p()
                try:
                    _call(collection, 4, (C.c_uint, C.c_void_p), (i, C.byref(device)))
                    identifiers.append(self._device_id(device))
                finally:
                    _release(device)
            return set(identifiers) == set(self.device_ids)
        finally:
            _release(collection)
            _release(enumerator)

    def read(self):
        peak = 0.0
        combined = []
        for index, (meter, volume) in enumerate(self.pairs):
            value, level, muted = C.c_float(), C.c_float(), C.c_int()
            _call(meter, 3, (C.c_void_p,), (C.byref(value),))
            _call(volume, 9, (C.c_void_p,), (C.byref(level),))
            _call(volume, 15, (C.c_void_p,), (C.byref(muted),))
            # Windows meters precede endpoint attenuation: respect mute/volume.
            if not muted.value:
                peak = max(peak, value.value * level.value)
            loopback = self.loopbacks[index]
            if loopback is not None:
                try:
                    bands = loopback.read(0. if muted.value else level.value)
                    combined = [max(a, b) for a, b in zip(combined, bands)] if combined else bands
                except (OSError, ValueError):
                    loopback.close()
                    self.loopbacks[index] = None
        self.bands = combined
        return peak

    def close(self):
        for loopback in self.loopbacks:
            if loopback is not None:
                loopback.close()
        self.loopbacks = []
        self.bands = []
        for pair in self.pairs:
            for pointer in pair:
                _release(pointer)
        self.pairs = []
        if self.initialized:
            self.ole.CoUninitialize()
            self.initialized = False


class OutputMeter:
    def __init__(self, factory=WindowsPeaks, interval=.05):
        self.factory, self.interval = factory, interval
        self.stopped = threading.Event()
        self.sample = (0.0, 0.0, False)
        self.thread = threading.Thread(target=self._run, name='MYN-output-meter', daemon=True)

    def start(self):
        self.thread.start()
        return self

    def _run(self):
        reader, refresh = None, 0.0
        try:
            while not self.stopped.is_set():
                now = time.monotonic()
                try:
                    if now >= refresh:
                        refresh = now + 2
                        unchanged = getattr(reader, 'unchanged', None)
                        if not (callable(unchanged) and unchanged()):
                            if reader is not None:
                                reader.close()
                                reader = None
                            reader = self.factory()
                    if reader is not None:
                        level = reader.read()
                        level = min(1., max(0., level)) if math.isfinite(level) else 0.
                        bands = getattr(reader, 'bands', [])
                        if not isinstance(bands, (list, tuple)) or len(bands) != 128:
                            bands = []
                        else:
                            bands = [min(1., max(0., b)) if math.isfinite(b) else 0. for b in bands]
                        self.sample = (level, time.monotonic(), True, bands)
                except Exception:
                    self.sample = (0., time.monotonic(), False)
                    if reader is not None:
                        reader.close()
                        reader = None
                self.stopped.wait(self.interval)
        finally:
            if reader is not None:
                reader.close()
            self.sample = (0., time.monotonic(), False)

    def snapshot(self):
        sample = self.sample
        level, stamp, available = sample[:3]
        fresh = time.monotonic() - stamp < .5
        result = dict(output=level if fresh else 0., available=available and fresh)
        if fresh and available and len(sample) > 3 and sample[3]:
            result['bands'] = list(sample[3])
        return result

    def close(self):
        self.stopped.set()
        if self.thread.is_alive():
            self.thread.join(timeout=1)
