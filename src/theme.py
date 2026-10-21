'A consistent dark theme for MYN windows.\n\napply(root, font) configures ttk styles and Tk defaults before windows are created. Supported Windows versions also receive a dark title bar and border. Native dialogs retain the system appearance.\n'

import sys
import tkinter as tk
from tkinter import ttk

BG = "#060f18"        # window background (the main screen is #040910; a touch lighter for panels)
PANEL = "#0c1b27"     # lists, text boxes, tabs
FIELD = "#102434"     # entry fields (as the main input bar)
LINE = "#1d3443"      # borders and separators
TEXT = "#dceff3"
MUTED = "#8aa5b0"
DIM = "#4d6b77"
ACCENT = "#5fd4e6"
BUTTON = "#123447"
BUTTON_HOVER = "#1b5a70"
PRESSED = "#16677e"
SELECT = "#16677e"
GOOD = "#7fd99a"
WARN = "#f0b36a"
BAD = "#f0aaa8"


def _colorref(color):
    red, green, blue = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
    return red | (green << 8) | (blue << 16)


def dark_title_bar(window):
    """Dark caption, border and title text for one top-level window (Windows only)."""
    if sys.platform != "win32" or getattr(window, "_myn_dark", False):
        return
    try:
        import ctypes
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id()) or window.winfo_id()
        dwm = ctypes.windll.dwmapi
        for attribute, value in ((20, 1), (19, 1),  # immersive dark mode (new id, then the old one)
                                 (35, _colorref(BG)), (34, _colorref(LINE)), (36, _colorref(TEXT))):
            data = ctypes.c_int(value)
            dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(data), ctypes.sizeof(data))
        window._myn_dark = True
    except Exception:
        pass  # an older Windows: the system title bar stays as it is


def apply(root, font_family):
    font = (font_family, 10)
    style = ttk.Style(root)
    try:
        style.theme_use("clam")  # the one built-in theme whose colours can all be set
    except tk.TclError:
        pass
    style.configure(".", background=BG, foreground=TEXT, fieldbackground=FIELD, bordercolor=LINE,
                    lightcolor=BG, darkcolor=BG, troughcolor=PANEL, focuscolor=ACCENT,
                    selectbackground=SELECT, selectforeground="#ffffff", insertcolor=TEXT,
                    arrowcolor=MUTED, font=font)
    style.map(".", foreground=[("disabled", DIM)])
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=TEXT, font=font)
    style.configure("TButton", background=BUTTON, foreground=TEXT, bordercolor=LINE, focusthickness=0,
                    padding=(12, 5), font=font, lightcolor=BUTTON, darkcolor=BUTTON)
    style.map("TButton", background=[("disabled", PANEL), ("pressed", PRESSED), ("active", BUTTON_HOVER)],
              foreground=[("disabled", DIM)], lightcolor=[("active", BUTTON_HOVER)],
              darkcolor=[("active", BUTTON_HOVER)])
    for name in ("TEntry", "TCombobox", "TSpinbox"):
        style.configure(name, fieldbackground=FIELD, foreground=TEXT, insertcolor=TEXT, bordercolor=LINE,
                        lightcolor=FIELD, darkcolor=FIELD, background=BUTTON, arrowcolor=MUTED, padding=4)
        style.map(name, fieldbackground=[("readonly", FIELD), ("disabled", PANEL)],
                  foreground=[("disabled", DIM)], bordercolor=[("focus", ACCENT)],
                  selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", TEXT)])
    for name in ("TCheckbutton", "TRadiobutton"):
        style.configure(name, background=BG, foreground=TEXT, indicatorbackground=FIELD,
                        indicatorforeground=ACCENT, indicatorcolor=FIELD, focuscolor=BG, font=font)
        style.map(name, background=[("active", BG)], indicatorcolor=[("selected", ACCENT), ("pressed", PRESSED)],
                  foreground=[("disabled", DIM)])
    style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 4, 0, 0))
    style.configure("TNotebook.Tab", background=PANEL, foreground=MUTED, padding=(16, 6), borderwidth=0,
                    lightcolor=PANEL, bordercolor=BG, font=font)
    style.map("TNotebook.Tab", background=[("selected", BUTTON_HOVER), ("active", BUTTON)],
              foreground=[("selected", "#ffffff"), ("active", TEXT)])
    style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=27,
                    bordercolor=LINE, lightcolor=PANEL, darkcolor=PANEL, font=font)
    style.map("Treeview", background=[("selected", SELECT)], foreground=[("selected", "#ffffff")])
    style.configure("Treeview.Heading", background=BG, foreground=MUTED, relief="flat", font=font,
                    bordercolor=LINE, lightcolor=BG, darkcolor=BG)
    style.map("Treeview.Heading", background=[("active", PANEL)])
    for name in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
        style.configure(name, background=BUTTON, troughcolor=BG, bordercolor=BG, arrowcolor=MUTED,
                        lightcolor=BUTTON, darkcolor=BUTTON, gripcount=0)
        style.map(name, background=[("active", BUTTON_HOVER)])
    style.configure("TLabelframe", background=BG, bordercolor=LINE, lightcolor=BG, darkcolor=BG)
    style.configure("TLabelframe.Label", background=BG, foreground=MUTED, font=font)
    style.configure("TPanedwindow", background=LINE)
    style.configure("Sash", background=LINE, sashthickness=5)
    style.configure("Horizontal.TScale", background=ACCENT, troughcolor=PANEL, bordercolor=BG,
                    lightcolor=ACCENT, darkcolor=ACCENT)
    style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=PANEL, bordercolor=BG,
                    lightcolor=ACCENT, darkcolor=ACCENT)
    style.configure("TSeparator", background=LINE)
    style.configure("Muted.TLabel", foreground=MUTED, background=BG)

    # Classic Tk widgets (text boxes, lists, plain windows) through the option database.
    for pattern, value in (
            ("*Toplevel.background", BG), ("*Frame.background", BG), ("*Label.background", BG),
            ("*Label.foreground", TEXT),
            ("*Text.background", PANEL), ("*Text.foreground", TEXT), ("*Text.insertBackground", ACCENT),
            ("*Text.selectBackground", SELECT), ("*Text.selectForeground", "#ffffff"), ("*Text.relief", "flat"),
            ("*Text.borderWidth", 0), ("*Text.highlightThickness", 1), ("*Text.highlightBackground", LINE),
            ("*Text.highlightColor", ACCENT), ("*Text.padX", 8), ("*Text.padY", 6),
            ("*Listbox.background", PANEL), ("*Listbox.foreground", TEXT),
            ("*Listbox.selectBackground", SELECT), ("*Listbox.selectForeground", "#ffffff"),
            ("*Listbox.relief", "flat"), ("*Listbox.borderWidth", 0), ("*Listbox.highlightThickness", 1),
            ("*Listbox.highlightBackground", LINE), ("*Listbox.highlightColor", ACCENT),
            ("*Listbox.activeStyle", "none"),
            ("*Entry.background", FIELD), ("*Entry.foreground", TEXT), ("*Entry.insertBackground", ACCENT),
            ("*Entry.relief", "flat"), ("*Entry.selectBackground", SELECT),
            ("*Button.background", BUTTON), ("*Button.foreground", TEXT), ("*Button.relief", "flat"),
            ("*Button.activeBackground", BUTTON_HOVER), ("*Button.activeForeground", "#ffffff"),
            ("*Checkbutton.background", BG), ("*Checkbutton.foreground", TEXT),
            ("*Checkbutton.selectColor", FIELD), ("*Checkbutton.activeBackground", BG),
            ("*Radiobutton.background", BG), ("*Radiobutton.foreground", TEXT),
            ("*Radiobutton.selectColor", FIELD), ("*Radiobutton.activeBackground", BG),
            ("*Scale.background", BG), ("*Scale.troughColor", PANEL), ("*Scale.foreground", TEXT),
            ("*Scale.highlightThickness", 0),
            ("*Canvas.highlightThickness", 0),
            ("*Menu.background", PANEL), ("*Menu.foreground", TEXT), ("*Menu.activeBackground", SELECT),
            ("*TCombobox*Listbox.background", FIELD), ("*TCombobox*Listbox.foreground", TEXT),
            ("*TCombobox*Listbox.selectBackground", SELECT), ("*TCombobox*Listbox.selectForeground", "#ffffff")):
        root.option_add(pattern, value)

    # Text boxes with a scroll bar use the themed scroll bar too.
    from tkinter import scrolledtext
    scrolledtext.Scrollbar = ttk.Scrollbar
    root.bind_class("Toplevel", "<Map>", lambda event: dark_title_bar(event.widget)
                    if isinstance(event.widget, tk.Toplevel) else None, add="+")
    return style


# Frameless windows matching the main interface.
# Every settings window gets MYN's own title strip instead of the system's: its title on the
# left, — and ✕ on the right, drag it to move, double-click to maximise, the bottom-right
# corner to resize. On Windows it keeps a taskbar button, rounded corners and a hairline.

TITLE_H = 34
_original_init = tk.Toplevel.__init__
_frameless = {"on": False, "font": ("Segoe UI", 10)}

# Floating panels inside MYN's main window.
# Keep settings in the existing window.
# Every settings window opens inside the main window's area (sized to fit, centred), is owned
# by it (no taskbar button of its own, hidden while MYN is minimised, always above it), moves
# along when MYN is moved, and closes with Esc. The main window dims behind it.
PANEL_MARGIN = (36, 28)   # px kept free around a panel inside the main window (x, y)
_host = {"rect": None, "hwnd": None, "changed": None, "root": None, "last": None, "panels": []}


def set_host(root, rect, hwnd=None, changed=None):
    """rect() -> (x, y, width, height) of MYN's main window on screen, or None;
    hwnd() -> its window handle (Windows) to own the panels; changed(count) is told how
    many panels are open."""
    first = _host["root"] is None
    _host.update(root=root, rect=rect, hwnd=hwnd, changed=changed)
    if first:
        root.after(150, _follow_host)


def host_rect():
    try:
        rect = _host["rect"]() if _host["rect"] else None
    except Exception:
        rect = None
    if not rect or rect[2] < 200 or rect[3] < 150:
        return None
    return tuple(int(v) for v in rect)


def panels():
    return [window for window in _host["panels"] if _alive(window)]


def _alive(window):
    try:
        return bool(window.winfo_exists())
    except tk.TclError:
        return False


def _panel_changed():
    _host["panels"] = panels()
    if _host["changed"]:
        try:
            _host["changed"](len(_host["panels"]))
        except Exception:
            pass


def _fit(window, width, height):
    """A size and place for a panel: inside the main window, centred over it (a dialog:
    over the panel it belongs to)."""
    rect = host_rect()
    if rect is None:
        return width, height, None
    x, y, w, h = rect
    width = max(240, min(width, w - 2 * PANEL_MARGIN[0]))
    height = max(160, min(height, h - 2 * PANEL_MARGIN[1]))
    master = getattr(window, "master", None)
    if master is not None and getattr(master, "_myn_panel", False) and _alive(master):
        cx = master.winfo_rootx() + master.winfo_width() // 2
        cy = master.winfo_rooty() + master.winfo_height() // 2
    else:
        cx, cy = x + w // 2, y + h // 2
    left = min(max(cx - width // 2, x + 8), x + w - width - 8)
    top = min(max(cy - height // 2, y + 8), y + h - height - 8)
    return width, height, (left, top)


def _follow_host():
    """Panels move along when MYN's window is moved."""
    root = _host["root"]
    try:
        rect = host_rect()
        last = _host["last"]
        if rect and last and rect[:2] != last[:2]:
            dx, dy = rect[0] - last[0], rect[1] - last[1]
            for window in panels():
                if not getattr(window, "_myn_dragged_out", False):
                    window._myn_raw_geometry("+%d+%d" % (window.winfo_x() + dx, window.winfo_y() + dy))
        if rect:
            _host["last"] = rect
    except Exception:
        pass
    try:
        root.after(120, _follow_host)
    except tk.TclError:
        pass


class ScrollBody(tk.Frame):
    """A window's content area that scrolls when the window is shorter than its content
    (a panel fitted inside MYN's main window can be). Pack the window's content into it
    instead of into the window. The scrollbar only shows when needed; lists, text boxes and
    charts inside keep their own wheel scrolling."""

    def __init__(self, window, bg=None):
        holder = tk.Frame(window, bg=bg or BG, highlightthickness=0, bd=0)
        holder.pack(fill=tk.BOTH, expand=True)
        self._canvas = canvas = tk.Canvas(holder, bg=bg or BG, highlightthickness=0, bd=0)
        self._bar = ttk.Scrollbar(holder, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=self._bar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        super().__init__(canvas, bg=bg or BG, highlightthickness=0, bd=0)
        self._item = canvas.create_window(0, 0, window=self, anchor="nw")
        self.bind("<Configure>", lambda _e: self._layout())
        canvas.bind("<Configure>", lambda _e: self._layout())
        window.bind("<MouseWheel>", self._wheel, add="+")
        window.bind("<Button-4>", lambda e: self._wheel(e, -1), add="+")
        window.bind("<Button-5>", lambda e: self._wheel(e, 1), add="+")

    def _layout(self):
        canvas = self._canvas
        width, height = canvas.winfo_width(), canvas.winfo_height()
        need = self.winfo_reqheight()
        canvas.itemconfigure(self._item, width=width, height=max(need, height))
        canvas.configure(scrollregion=(0, 0, width, max(need, height)))
        overflowing = need > height + 1
        if overflowing and not self._bar.winfo_ismapped():
            self._bar.pack(side=tk.RIGHT, fill=tk.Y)
        elif not overflowing and self._bar.winfo_ismapped():
            self._bar.pack_forget()
            canvas.yview_moveto(0)

    def _wheel(self, event, step=None):
        widget = event.widget
        if isinstance(widget, (tk.Text, tk.Listbox, ttk.Treeview)) or \
                (isinstance(widget, tk.Canvas) and widget is not self._canvas):
            return None  # it scrolls itself
        if not self._bar.winfo_ismapped():
            return None
        if step is None:
            step = -1 if event.delta > 0 else 1
        self._canvas.yview_scroll(step * 3, "units")
        return "break"


def scroll_body(window, padding=0, bg=None):
    """The frame to put a window's content in (see ScrollBody); padding like ttk: one value
    or (left, top, right, bottom)."""
    body = ScrollBody(window, bg=bg)
    pad = padding if isinstance(padding, (tuple, list)) else (padding,) * 4
    if len(pad) == 2:
        pad = (pad[0], pad[1], pad[0], pad[1])
    inner = ttk.Frame(body, padding=pad)
    inner.pack(fill=tk.BOTH, expand=True)
    return inner


def _own(window):
    """Windows: the panel belongs to MYN's main window (no taskbar button, stays above it,
    hidden while it is minimised)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        owner = None
        master = getattr(window, "master", None)
        if master is not None and getattr(master, "_myn_panel", False) and _alive(master):
            owner = _hwnd(master)
        elif _host["hwnd"]:
            owner = _host["hwnd"]()
        if owner:
            user32 = ctypes.windll.user32
            setter = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
            setter.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
            setter(_hwnd(window), -8, owner)  # GWLP_HWNDPARENT: the owner
    except Exception:
        pass


def _hwnd(window):
    import ctypes
    user32 = ctypes.windll.user32
    return user32.GetAncestor(window.winfo_id(), 2) or user32.GetParent(window.winfo_id())


def _windows_frame(window):
    """Rounded corners and a hairline border (Windows 11); a window that is not a panel inside
    MYN's main window also gets a taskbar button and Alt+Tab."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        user32, dwm = ctypes.windll.user32, ctypes.windll.dwmapi
        hwnd = _hwnd(window)
        if not getattr(window, "_myn_panel", False):
            style = user32.GetWindowLongW(hwnd, -20)
            user32.SetWindowLongW(hwnd, -20, (style & ~0x00000080) | 0x00040000)  # APPWINDOW
        else:
            _own(window)
        for attribute, value in ((33, 2), (34, _colorref(LINE))):
            data = ctypes.c_int(value)
            dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(data), ctypes.sizeof(data))
        window.withdraw()
        window.after(10, window.deiconify)
    except Exception:
        pass


class TitleBar:
    def __init__(self, window):
        self.window = window
        self.normal = None
        self.drag = None
        self.bar = bar = tk.Frame(window, bg=BG, height=TITLE_H, highlightthickness=0, bd=0)
        bar.pack(side=tk.TOP, fill=tk.X)
        bar.pack_propagate(False)
        self.label = tk.Label(bar, text="", bg=BG, fg=MUTED, font=_frameless["font"], anchor="w")
        self.label.pack(side=tk.LEFT, padx=(14, 0), fill=tk.Y)
        self.close = self._button("✕", self.close_window, "#f0aaa8")
        self.minimise = self._button("—", self.minimise_window, TEXT)
        line = tk.Frame(window, bg=LINE, height=1)
        line.pack(side=tk.TOP, fill=tk.X)
        for widget in (bar, self.label):
            widget.bind("<ButtonPress-1>", self.start_move)
            widget.bind("<B1-Motion>", self.move)
            widget.bind("<Double-Button-1>", lambda _e: self.toggle_maximise())
        self.grip = None

    def _button(self, text, command, hover):
        label = tk.Label(self.bar, text=text, bg=BG, fg="#5f7c88", font=("Segoe UI Symbol", 11),
                         width=4, cursor="hand2")
        label.pack(side=tk.RIGHT, fill=tk.Y)
        label.bind("<Enter>", lambda _e: label.configure(fg=hover))
        label.bind("<Leave>", lambda _e: label.configure(fg="#5f7c88"))
        label.bind("<ButtonRelease-1>", lambda _e: command())
        return label

    def set_title(self, text):
        self.label.configure(text=text or "")

    def mapped(self):
        """Once shown: the resize corner (if resizable) and the Windows frame."""
        window = self.window
        try:
            self.set_title(window.wm_title())
            resizable = any(window.resizable())
        except tk.TclError:
            return
        if resizable and self.grip is None:
            grip = tk.Canvas(window, width=16, height=16, bg=BG, highlightthickness=0, bd=0,
                             cursor="size_nw_se" if sys.platform == "win32" else "bottom_right_corner")
            for step in (4, 8, 12):
                grip.create_line(16 - step, 15, 15, 16 - step, fill="#2c4a58")
            grip.place(relx=1.0, rely=1.0, anchor="se")
            grip.bind("<ButtonPress-1>", self.start_resize)
            grip.bind("<B1-Motion>", self.resize)
            self.grip = grip
        if getattr(window, "_myn_panel", False) and not getattr(window, "_myn_placed", False):
            window._myn_placed = True  # never sized by its code: fit it in now
            window.update_idletasks()
            width = max(window.winfo_width(), window.winfo_reqwidth())
            height = max(window.winfo_height(), window.winfo_reqheight())
            width, height, place = _fit(window, width, height)
            if place:
                self._raw_geometry("%dx%d+%d+%d" % (width, height, place[0], place[1]))
        if not getattr(window, "_myn_framed", False):
            window._myn_framed = True
            _windows_frame(window)
            window.after(80, lambda: _focus(window))

    def _raw_geometry(self, spec=None):
        """The window's own geometry call (sizes include the title strip)."""
        return getattr(self.window, "_myn_raw_geometry", self.window.wm_geometry)(spec)

    def start_move(self, event):
        self.drag = (event.x_root - self.window.winfo_x(), event.y_root - self.window.winfo_y())

    def move(self, event):
        if self.drag and not self.normal:
            self._raw_geometry("+%d+%d" % (event.x_root - self.drag[0], event.y_root - self.drag[1]))

    def start_resize(self, event):
        self.drag = (event.x_root, event.y_root, self.window.winfo_width(), self.window.winfo_height())

    def resize(self, event):
        x, y, width, height = self.drag
        min_w, min_h = self.window.minsize()
        self._raw_geometry("%dx%d" % (max(min_w or 320, width + event.x_root - x),
                                         max(min_h or 200, height + event.y_root - y)))

    def toggle_maximise(self):
        window = self.window
        try:
            if not any(window.resizable()):
                return
        except tk.TclError:
            return
        if self.normal:
            self._raw_geometry(self.normal)
            self.normal = None
            return
        self.normal = self._raw_geometry()
        rect = host_rect() if getattr(window, "_myn_panel", False) else None
        if rect:  # a panel fills MYN's window, not the screen
            x, y, w, h = rect
            self._raw_geometry("%dx%d+%d+%d" % (w - 16, h - 16, x + 8, y + 8))
            return
        left, top, right, bottom = 0, 0, window.winfo_screenwidth(), window.winfo_screenheight()
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes
                area = wintypes.RECT()
                ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(area), 0)
                left, top, right, bottom = area.left, area.top, area.right, area.bottom
            except Exception:
                pass
        self._raw_geometry("%dx%d+%d+%d" % (right - left, bottom - top, left, top))

    def close_window(self):
        window = self.window
        try:
            handler = window.protocol("WM_DELETE_WINDOW")
            if handler:
                window.tk.call(handler)
            else:
                window.destroy()
        except tk.TclError:
            pass

    def minimise_window(self):
        window = self.window
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.user32.ShowWindow(_hwnd(window), 6)  # SW_MINIMIZE
                return
            except Exception:
                pass
        window.overrideredirect(False)
        window.iconify()

        def back(_event):
            window.unbind("<Map>", bound)
            window.overrideredirect(True)
        bound = window.bind("<Map>", back, add="+")


def _focus(window):
    try:
        window.lift()
        window.focus_force()
    except tk.TclError:
        pass


def _framed_init(self, master=None, cnf=None, **kw):
    _original_init(self, master, cnf or {}, **kw)
    if not _frameless["on"] or getattr(self, "_myn_system_frame", False):
        return
    try:
        self.overrideredirect(True)
    except tk.TclError:
        return
    if sys.platform != "win32":
        self.configure(highlightthickness=1, highlightbackground=LINE)
    self._myn_panel = host_rect() is not None  # inside MYN's main window (see set_host)
    bar = TitleBar(self)
    self._myn_title_bar = bar
    original_title, original_geometry = self.wm_title, self.wm_geometry
    if self._myn_panel:
        _host["panels"] = panels() + [self]
        _panel_changed()
        # (scheduled on the main window: callbacks of a window being destroyed are dropped)
        self.bind("<Destroy>", lambda event: _host["root"].after_idle(_panel_changed)
                  if event.widget is self and _host["root"] is not None else None, add="+")
        self.bind("<Escape>", lambda _event: bar.close_window())

    def title(text=None):
        if text is None:
            return original_title()
        result = original_title(text)
        bar.set_title(text)
        return result

    def geometry(spec=None):
        """Sizes the callers give are for their content: the title strip is added on top.
        A panel is fitted inside MYN's main window and centred there."""
        if isinstance(spec, str) and "x" in spec.split("+")[0].split("-")[0]:
            size, _, place = spec.partition("+") if "+" in spec else (spec, "", "")
            width, height = size.split("x")
            try:
                width, height = int(width), int(height) + TITLE_H + 1
            except ValueError:
                return original_geometry(spec)
            if self._myn_panel and not place:
                width, height, spot = _fit(self, width, height)
                try:
                    min_w, min_h = self.minsize()
                    self.minsize(min(min_w, width), min(min_h, height))
                except tk.TclError:
                    pass
                self._myn_placed = True
                if spot:
                    return original_geometry("%dx%d+%d+%d" % (width, height, spot[0], spot[1]))
            spec = "%dx%d%s" % (width, height, ("+" + place) if place else "")
        return original_geometry(spec)

    self._myn_raw_geometry = original_geometry
    self.title = self.wm_title = title
    self.geometry = self.wm_geometry = geometry
    self.bind("<Map>", lambda event: bar.mapped() if event.widget is self else None, add="+")


def enable_frameless(font_family):
    """From now on every new Toplevel is frameless with MYN's title strip."""
    _frameless["on"] = True
    _frameless["font"] = (font_family, 10)
    tk.Toplevel.__init__ = _framed_init
