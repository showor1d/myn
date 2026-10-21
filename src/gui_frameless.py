"""The frameless main window: dragging, maximising, resizing, window buttons.

Mixin composed by MYNWindow (myn_gui.py); shares its state and event queue."""

from gui_common import (  # noqa: F401
    os,
    tk,
)


class FramelessMixin:
    # ---- frameless window ------------------------------------------------------

    DEFAULT_SIZE = (1040, 680)
    MIN_SIZE = (720, 520)
    _drag_origin = None
    _dragged = False
    _normal_geometry = None
    _hwnd = None

    def _make_frameless(self):
        """No system title bar: drag the scene to move, ✕/— to close/minimise,
        double-click to maximise, bottom-right corner to resize."""
        root = self.root
        width = min(self.DEFAULT_SIZE[0], root.winfo_screenwidth() - 40)
        height = min(self.DEFAULT_SIZE[1], root.winfo_screenheight() - 80)
        root.geometry("%dx%d+%d+%d" % (width, height, (root.winfo_screenwidth() - width) // 2,
                                       max(0, (root.winfo_screenheight() - height) // 2 - 20)))
        root.minsize(*self.MIN_SIZE)
        try:
            root.overrideredirect(True)
        except tk.TclError:
            return
        grip = tk.Canvas(root, width=16, height=16, bg="#040910", highlightthickness=0, bd=0,
                         cursor="size_nw_se" if os.name == "nt" else "bottom_right_corner")
        for step in (4, 8, 12):  # three faint diagonal ticks
            grip.create_line(16 - step, 15, 15, 16 - step, fill="#2c4a58")
        grip.place(relx=1.0, rely=1.0, anchor="se")
        grip.bind("<ButtonPress-1>", self._start_resize)
        grip.bind("<B1-Motion>", self._resize)
        self.resize_grip = grip
        if os.name == "nt":
            root.after(20, self._windows_frame)

    def _windows_frame(self):
        """Keep a taskbar button and Alt+Tab entry; Windows 11 rounded corners and a hairline."""
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = user32.GetAncestor(self.root.winfo_id(), 2) or user32.GetParent(self.root.winfo_id())
            self._hwnd = hwnd
            style = user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
            user32.SetWindowLongW(hwnd, -20, (style & ~0x00000080) | 0x00040000)  # APPWINDOW
            dwm = ctypes.windll.dwmapi
            corner = ctypes.c_int(2)  # DWMWCP_ROUND
            dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), ctypes.sizeof(corner))
            border = ctypes.c_uint(0x00463A1B)  # COLORREF 0x00BBGGRR -> #1b3a46
            dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(border), ctypes.sizeof(border))
            self.root.withdraw()
            self.root.after(10, self.root.deiconify)
        except Exception:
            pass  # older Windows: still frameless, just square

    def minimize(self):
        if os.name == "nt" and self._hwnd:
            try:
                import ctypes
                ctypes.windll.user32.ShowWindow(self._hwnd, 6)  # SW_MINIMIZE
                return
            except Exception:
                pass
        # Tk refuses to iconify an override-redirect window: lift it for the trip.
        self.root.overrideredirect(False)
        self.root.iconify()

        def restore(_event):
            self.root.unbind("<Map>")
            self.root.overrideredirect(True)
        self.root.bind("<Map>", restore)

    def toggle_maximize(self):
        root = self.root
        if self._normal_geometry:
            root.geometry(self._normal_geometry)
            self._normal_geometry = None
            return
        self._normal_geometry = root.geometry()
        left, top, right, bottom = 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()
        if os.name == "nt":
            try:
                import ctypes
                from ctypes import wintypes
                area = wintypes.RECT()
                ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(area), 0)
                left, top, right, bottom = area.left, area.top, area.right, area.bottom
            except Exception:
                pass
        root.geometry("%dx%d+%d+%d" % (right - left, bottom - top, left, top))

    def _dock_xy(self, item):
        import math
        alarm = bool((self._timer_hud_data or {}).get('alarm_upcoming'))
        entries = ['agents'] + (['alarm'] if alarm else []) + ['usage', 'input']
        w, h = self.core_canvas.winfo_width(), self.core_canvas.winfo_height()
        radius = min(w * .2, h * .25, 145) * 1.22 + 42
        index = entries.index(item) if item in entries else 1
        angle = math.radians(-36 + 72 * index / max(1, len(entries)-1))
        return (w/2 - min(radius, w/2-30) * math.cos(angle),
                h/2-3 + radius * math.sin(angle))

    def _on_scene_control(self, event):
        return any(hit(event.x, event.y) for hit in (
            self._over_agents_entry, self._over_usage_entry, self._over_input_entry,
            self._over_close_icon, self._over_minimize_icon))

    def _press_scene(self, event):
        self._dragged = False
        # Presses on dock icons or window buttons are clicks, never drags.
        self._drag_origin = None if self._on_scene_control(event) else \
            (event.x_root, event.y_root, self.root.winfo_x(), self.root.winfo_y())

    def _drag_scene(self, event):
        if self._drag_origin is None:
            return
        x0, y0, wx, wy = self._drag_origin
        dx, dy = event.x_root - x0, event.y_root - y0
        if not self._dragged and abs(dx) + abs(dy) < 4:
            return  # a click, not a drag
        self._dragged = True
        if self._normal_geometry:  # dragging a maximised window restores it first
            self._normal_geometry = None
        self.root.geometry("+%d+%d" % (wx + dx, wy + dy))

    def _release_scene(self, event):
        dragged, self._drag_origin = self._dragged, None
        self._dragged = False
        if not dragged:
            self._click_scene_controls(event)

    def _double_click_scene(self, event):
        if not self._on_scene_control(event):
            self.toggle_maximize()

    def _start_resize(self, event):
        self._resize_origin = (event.x_root, event.y_root,
                               self.root.winfo_width(), self.root.winfo_height())

    def _resize(self, event):
        x0, y0, width, height = self._resize_origin
        self._normal_geometry = None
        self.root.geometry("%dx%d" % (max(self.MIN_SIZE[0], width + event.x_root - x0),
                                      max(self.MIN_SIZE[1], height + event.y_root - y0)))

    def open_settings_menu(self):
        # Compatibility for "open settings" voice requests after removing the gear menu.
        self.open_agents_settings()

    def show_input(self):
        """Bring up the text input bar (from the keyboard icon or by voice) and focus it."""
        if not self.input_visible:
            # A slim bar floating over the bottom of the scene.
            self._place_input()
            self.input_visible = True
            self._queue_chat()  # make room for it
        if self.web_active:
            self.web.js({"cmd": "input", "visible": True, "text": self.entry.get()})
            self.web.window("show")
            return
        self.root.deiconify()
        self.entry.focus_set()

    def hide_input(self):
        if self.input_visible:
            self.input_row.place_forget()
            self.input_visible = False
            self._queue_chat()
            if self.web_active:
                self.web.js({"cmd": "input", "visible": False})

    def toggle_input(self):
        if self.input_visible:
            self.hide_input()
        else:
            self.show_input()

    usage_window = None

