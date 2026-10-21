"""Protect the Claude key with Windows DPAPI for the current Windows user."""

import ctypes
import os
import tempfile
from ctypes import wintypes
from pathlib import Path


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _crypt(data, protect):
    if os.name != "nt":
        raise RuntimeError("Windows credential protection is unavailable")
    source_buffer = ctypes.create_string_buffer(data)
    source = DATA_BLOB(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)))
    target = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(source), None, None, None, None,
                                      0x01, ctypes.byref(target))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None,
                                        0x01, ctypes.byref(target))
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        free = ctypes.windll.kernel32.LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(ctypes.cast(target.pbData, ctypes.c_void_p))


def key_path(data_dir):
    return Path(data_dir) / "claude-key.dpapi"


def claude_key_source(data_dir):
    """Report the active Claude credential source without exposing its value."""
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return "environment"
    return "saved" if key_path(data_dir).is_file() else "none"


def save_key(data_dir, key):
    key = key.strip()
    if not key or "\n" in key or len(key) > 1024:
        raise ValueError('Invalid key format')
    path = key_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    encrypted = _crypt(key.encode("utf-8"), True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("xb") as stream:
        stream.write(encrypted)
    temporary.replace(path)


def get_key(data_dir):
    from_env = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if from_env:
        return from_env
    path = key_path(data_dir)
    if not path.is_file():
        return ""
    return _crypt(path.read_bytes(), False).decode("utf-8")


def clear_key(data_dir):
    path = key_path(data_dir)
    if path.exists():
        path.unlink()


def elevenlabs_key_path(data_dir):
    return Path(data_dir) / "elevenlabs-key.dpapi"


def elevenlabs_key_source(data_dir):
    """Report only where the effective key comes from, never its value."""
    if os.environ.get("ELEVENLABS_API_KEY", "").strip():
        return "environment"
    return "saved" if elevenlabs_key_path(data_dir).is_file() else "none"


def save_elevenlabs_key(data_dir, key):
    if not isinstance(key, str):
        raise ValueError('Invalid key format')
    key = key.strip()
    if not key or "\n" in key or "\r" in key or len(key) > 1024:
        raise ValueError('Invalid key format')
    path = elevenlabs_key_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    encrypted = _crypt(key.encode("utf-8"), True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix="elevenlabs-key-",
                                         suffix=".tmp", dir=str(path.parent),
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encrypted)
        os.replace(str(temporary), str(path))
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def get_elevenlabs_key(data_dir):
    from_env = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if from_env:
        return from_env
    path = elevenlabs_key_path(data_dir)
    if not path.is_file():
        return ""
    return _crypt(path.read_bytes(), False).decode("utf-8")


def clear_elevenlabs_key(data_dir):
    path = elevenlabs_key_path(data_dir)
    if path.exists():
        path.unlink()
