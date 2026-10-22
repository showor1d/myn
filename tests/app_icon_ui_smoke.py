"""Check the supplied icon in real Tk and WebView2 windows without user data."""
import sys
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / 'src'), str(BASE / 'src/core')]
import app_icon


def main():
    import tkinter as tk
    import ctypes
    from ctypes import wintypes
    assert app_icon.ICON.is_file()
    app_icon.identify()
    root = tk.Tk()
    root.withdraw()
    try:
        app_icon.apply_tk(root)
        root.geometry('64x64+0+0')
        root.deiconify()
        root.update()
        user = ctypes.windll.user32
        user.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user.SendMessageW.restype = wintypes.LPARAM
        hwnd = int(root.tk.call('wm', 'frame', root._w), 16)
        assert user.SendMessageW(hwnd, 0x7F, 1, 0), 'Tk icon handle is missing'
        print('PASS: real Tk window has a native application icon.')
    finally:
        root.destroy()

    import webview
    import web_window
    original_start = webview.start
    result = []

    def inspect(window):
        try:
            assert window.events.loaded.wait(20), 'WebView2 did not load'
            from System.Drawing import Icon
            expected = Icon(str(app_icon.ICON))
            expected_bitmap = expected.ToBitmap()
            actual = window.native.Icon.ToBitmap()
            try:
                assert actual.Size == expected_bitmap.Size
                assert all(actual.GetPixel(x, y).ToArgb() == expected_bitmap.GetPixel(x, y).ToArgb()
                           for x in range(actual.Width) for y in range(actual.Height))
            finally:
                actual.Dispose()
                expected_bitmap.Dispose()
                expected.Dispose()
            result.append(True)
            print('PASS: actual WebView2 startup loads the supplied icon pixels.')
        finally:
            window.destroy()

    with mock.patch.object(webview, 'start', lambda _serve, window, **kw: original_start(inspect, window, **kw)):
        assert web_window.main() == 0
    assert result == [True]


if __name__ == '__main__':
    main()
