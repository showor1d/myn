'Desktop window actions: list, focus, minimize, maximize, restore, close, press shortcuts and type at the cursor. Only named actions are available, not arbitrary clicking. Calls use user32 through ctypes. Exclude MYN windows from listing and targeting. Tests substitute Desktop.'

import os
import time

VK = {"enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20,
      "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23,
      "pageup": 0x21, "pagedown": 0x22, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
      "shift": 0x10, "ctrl": 0x11, "control": 0x11, "alt": 0x12, "win": 0x5B,
      "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75, "f7": 0x76,
      "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
      "playpause": 0xB3, "nexttrack": 0xB0, "prevtrack": 0xB1, "volumeup": 0xAF,
      "volumedown": 0xAE, "volumemute": 0xAD}
PUNCTUATION = {";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF, "`": 0xC0,
               "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE}
EXTENDED = {0x2E, 0x2D, 0x24, 0x23, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28, 0x5B}
# Shortcuts that close, delete or quit something: asked first (user's levels).
RISKY_KEYS = {"alt+f4", "ctrl+w", "ctrl+shift+w", "ctrl+q", "ctrl+f4", "delete", "shift+delete",
              "ctrl+delete", "ctrl+shift+delete", "ctrl+alt+delete", "ctrl+shift+esc", "win+l",
              "ctrl+d", "ctrl+backspace", "ctrl+enter", "alt+enter"}
MAX_TYPE = 2000


class WindowError(Exception):
    """A user-facing reason (Chinese)."""


def parse_keys(combo):
    """'Ctrl + Shift + T' -> ('ctrl+shift+t', [0x11, 0x10, 0x54]). Modifiers first."""
    parts = [piece.strip().casefold() for piece in str(combo or "").replace("＋", "+").split("+")]
    parts = [{"control": "ctrl", "cmd": "win", "super": "win", "windows": "win", "return": "enter",
              "escape": "esc", "del": "delete"}.get(part, part) for part in parts if part]
    if not parts or len(parts) > 4:
        raise WindowError('Invalid shortcut.')
    codes = []
    for part in parts:
        if part in VK:
            codes.append(VK[part])
        elif len(part) == 1 and part.isascii() and part.isalnum():
            codes.append(ord(part.upper()))
        elif part in PUNCTUATION:
            codes.append(PUNCTUATION[part])
        else:
            raise WindowError('Unknown key: %s' % part)
    modifiers = [p for p in parts if p in ("ctrl", "shift", "alt", "win")]
    rest = [p for p in parts if p not in ("ctrl", "shift", "alt", "win")]
    order = {"ctrl": 0, "shift": 1, "alt": 2, "win": 3}
    name = "+".join(sorted(modifiers, key=order.get) + rest)
    return name, codes


def is_risky_keys(name):
    return name in RISKY_KEYS


def match(windows, title):
    """The window whose title best fits `title` (case-insensitive part of the title)."""
    wanted = str(title or "").strip().casefold()
    if not wanted:
        raise WindowError('Specify a window.')
    exact = [w for w in windows if w["title"].casefold() == wanted]
    if exact:
        return exact[0]
    found = [w for w in windows if wanted in w["title"].casefold() or wanted in w.get("program", "").casefold()]
    if not found:
        raise WindowError('No window title contains "%s".' % title)
    return found[0]  # windows come front-most first


class Desktop:
    """The real Windows desktop (user32)."""

    def __init__(self, skip_pids=()):
        import ctypes
        from ctypes import wintypes
        self.ctypes, self.wintypes = ctypes, wintypes
        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32
        self.skip = {int(pid) for pid in skip_pids if pid} | {os.getpid()}
        handle = ctypes.c_void_p
        u = self.user32
        for function, restype, argtypes in (
                (u.IsWindowVisible, wintypes.BOOL, [handle]), (u.IsIconic, wintypes.BOOL, [handle]),
                (u.IsZoomed, wintypes.BOOL, [handle]),
                (u.GetWindowTextLengthW, ctypes.c_int, [handle]),
                (u.GetWindowTextW, ctypes.c_int, [handle, wintypes.LPWSTR, ctypes.c_int]),
                (u.GetWindowThreadProcessId, wintypes.DWORD, [handle, ctypes.POINTER(wintypes.DWORD)]),
                (u.GetWindowLongW, ctypes.c_long, [handle, ctypes.c_int]),
                (u.GetForegroundWindow, handle, []), (u.SetForegroundWindow, wintypes.BOOL, [handle]),
                (u.ShowWindow, wintypes.BOOL, [handle, ctypes.c_int]),
                (u.BringWindowToTop, wintypes.BOOL, [handle]),
                (u.PostMessageW, wintypes.BOOL, [handle, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]),
                (u.GetWindow, handle, [handle, ctypes.c_uint])):
            function.restype, function.argtypes = restype, argtypes
        k = self.kernel32
        k.OpenProcess.restype, k.OpenProcess.argtypes = handle, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.QueryFullProcessImageNameW.restype = wintypes.BOOL
        k.QueryFullProcessImageNameW.argtypes = [handle, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        k.CloseHandle.restype, k.CloseHandle.argtypes = wintypes.BOOL, [handle]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class _U(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("u", _U)]

        self.KEYBDINPUT, self.INPUT = KEYBDINPUT, INPUT

    def _program(self, pid):
        try:
            kernel = self.kernel32
            process = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not process:
                return ""
            try:
                size = self.wintypes.DWORD(260)
                buffer = self.ctypes.create_unicode_buffer(260)
                if kernel.QueryFullProcessImageNameW(process, 0, buffer, self.ctypes.byref(size)):
                    return os.path.splitext(os.path.basename(buffer.value))[0]
            finally:
                kernel.CloseHandle(process)
        except Exception:
            pass
        return ""

    def windows(self):
        """Normal windows, front-most first: [{"id", "title", "program", "state"}]."""
        u, found = self.user32, []
        window = u.GetWindow(u.GetForegroundWindow() or None, 0)  # GW_HWNDFIRST
        seen = 0
        while window and seen < 2000:
            seen += 1
            if u.IsWindowVisible(window) and not (u.GetWindowLongW(window, -20) & 0x80):  # no tool windows
                length = u.GetWindowTextLengthW(window)
                if length:
                    buffer = self.ctypes.create_unicode_buffer(length + 1)
                    u.GetWindowTextW(window, buffer, length + 1)
                    pid = self.wintypes.DWORD()
                    u.GetWindowThreadProcessId(window, self.ctypes.byref(pid))
                    title = buffer.value
                    if pid.value not in self.skip and title not in ("Program Manager",):
                        found.append({"id": int(window), "title": title, "program": self._program(pid.value),
                                      "state": "minimized" if u.IsIconic(window) else
                                      "maximized" if u.IsZoomed(window) else "normal"})
            window = u.GetWindow(window, 2)  # GW_HWNDNEXT
        return found

    def _send_keys(self, codes, up):
        items = []
        for code in (reversed(codes) if up else codes):
            item = self.INPUT(type=1)
            item.u.ki = self.KEYBDINPUT(code, 0, (0x0001 if code in EXTENDED else 0) | (0x0002 if up else 0), 0, 0)
            items.append(item)
        array = (self.INPUT * len(items))(*items)
        self.user32.SendInput(len(items), array, self.ctypes.sizeof(self.INPUT))

    def focus(self, window_id):
        u = self.user32
        if u.IsIconic(window_id):
            u.ShowWindow(window_id, 9)  # SW_RESTORE
        # Windows only lets the front program hand over the focus; a tap of Alt makes it allowed.
        self._send_keys([0x12], up=False)
        self._send_keys([0x12], up=True)
        u.BringWindowToTop(window_id)
        ok = u.SetForegroundWindow(window_id)
        time.sleep(0.15)
        return bool(ok) and int(u.GetForegroundWindow() or 0) == int(window_id)

    def show(self, window_id, action):
        command = {"minimize": 6, "maximize": 3, "restore": 9}[action]
        self.user32.ShowWindow(window_id, command)

    def close(self, window_id):
        self.user32.PostMessageW(window_id, 0x0010, None, None)  # WM_CLOSE: the program may ask to save

    def front(self):
        window = self.user32.GetForegroundWindow()
        return int(window) if window else 0

    def press(self, codes):
        self._send_keys(codes, up=False)
        time.sleep(0.03)
        self._send_keys(codes, up=True)

    def type_text(self, text):
        for char in text:
            if char == "\n":
                self.press([0x0D])
                continue
            units = char.encode("utf-16-le")
            for k in range(0, len(units), 2):
                unit = int.from_bytes(units[k:k + 2], "little")
                items = []
                for up in (False, True):
                    item = self.INPUT(type=1)
                    item.u.ki = self.KEYBDINPUT(0, unit, 0x0004 | (0x0002 if up else 0), 0, 0)  # KEYEVENTF_UNICODE
                    items.append(item)
                array = (self.INPUT * 2)(*items)
                self.user32.SendInput(2, array, self.ctypes.sizeof(self.INPUT))
            time.sleep(0.003)
