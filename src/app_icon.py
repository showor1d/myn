"""Shared desktop identity and artwork for the Tk and WebView2 windows."""
import os
from pathlib import Path

ICON = Path(__file__).resolve().parent / 'assets' / 'myn-symbol.ico'


def identify():
    if os.name == 'nt':
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('MYN.Desktop')


def apply_tk(root):
    """Use the same icon for this window and its later Tk dialogs."""
    if os.name == 'nt' and ICON.is_file():
        import tkinter as tk
        try:
            root.iconbitmap(default=str(ICON))
            root.iconbitmap(str(ICON))
        except tk.TclError:
            pass  # Missing/corrupt artwork must not prevent opening the app.


def apply_web(window):
    """Called on the WinForms UI thread before the native window is shown."""
    if os.name == 'nt' and ICON.is_file():
        from System.Drawing import Icon
        icon = Icon(str(ICON))
        window.native.Icon = icon
        window._myn_icon = icon
        window.events.closed += lambda: icon.Dispose()
