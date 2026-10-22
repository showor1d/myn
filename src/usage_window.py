"""Read-only usage receipt and charts, using the existing usage report."""

import math
import time
import tkinter as tk
from functools import lru_cache

import theme
import usage_stats

BG = theme.BG
PANEL = theme.PANEL
LINE = theme.LINE
TEXT = theme.TEXT
MUTED = theme.MUTED
ACCENT = "#5fd4e6"
GOOD = "#7fd99a"
WARN = "#f0b36a"
MODEL_COLORS = {"Opus": "#b98cf2", "Sonnet": "#4fb8e8", "Fable": "#f2a66b", "ElevenLabs": "#6fe0b0",
                "Claude Code": "#f0b36a", "ChatGPT": "#e87f9a"}
TOKEN_KINDS = (("input", 'Input', "#4fb8e8"), ("output", 'Output', "#f2a66b"),
               ("cache_write", 'Cache write', "#b98cf2"), ("cache_read", 'Cache read', "#6fe0b0"),
               ("other", 'Uncategorized', "#e87f9a"))
EXTRA_COLORS = ("#e87f9a", "#d9d36a", "#8aa2ff", "#9fd3c7")
PURPOSE_COLOR = "#4fb8e8"


def model_color(name, index=0):
    for key, color in MODEL_COLORS.items():
        if name.startswith(key):
            return color
    return EXTRA_COLORS[index % len(EXTRA_COLORS)] if index >= 0 else "#8aa2ff"


@lru_cache(maxsize=128)
def _category_ring_pixels(percent, background):
    """Smooth Tk rings with 4x4 coverage sampling, without extra dependencies."""
    radius, half_width, center = 13, 2.5, 16
    angle = math.tau * percent / 100
    end_x, end_y = center + radius * math.sin(angle), center - radius * math.cos(angle)
    track = tuple(round(bg + (fg-bg)*46/255)
                  for bg, fg in zip(background, (163, 149, 199)))
    progress = (151, 68, 236)
    pixels = bytearray()
    for y in range(32):
        for x in range(32):
            colors = [0, 0, 0]
            for sy in range(4):
                for sx in range(4):
                    px, py = x+(sx+.5)/4, y+(sy+.5)/4
                    dx, dy = px-center, py-center
                    in_ring = abs(math.hypot(dx, dy)-radius) <= half_width
                    phase = math.atan2(dx, -dy) % math.tau
                    in_progress = percent > 0 and (
                        (in_ring and (percent >= 100 or phase <= angle)) or
                        (px-center)**2+(py-(center-radius))**2 <= half_width**2 or
                        (px-end_x)**2+(py-end_y)**2 <= half_width**2)
                    color = progress if in_progress else track if in_ring else background
                    for channel in range(3):
                        colors[channel] += color[channel]
            pixels.extend(round(value/16) for value in colors)
    return b"P6\n32 32\n255\n" + bytes(pixels)


def short_tokens(count):
    return usage_stats.tokens_text(count)


def short_usd(value):
    """Dollars, always with two decimals (user, 2026-10-17)."""
    return "$%s" % format(round(value + 0.0, 2), ",.2f")


def receipt_data(result, period):
    """One read-only receipt shared by the page and classic window."""
    total, previous = result["totals"], result["previous"]
    base = previous["cost_usd"]
    delta = (total["cost_usd"] - base) / base * 100 if base else None
    change = ("%s %.0f%%" % ("↑" if delta >= 0 else "↓", abs(delta))) if delta is not None else (
        'New' if total["cost_usd"] else "—")
    import usage_detail
    daily_rows = [{"name": row["model"], "tokens": format(row["tokens"], ","),
                   "cost": usage_detail.usd(row["cost_usd"]) if row["priced"] else 'Unpriced',
                   "detail": usage_detail.description(row), "usage_category": row["category"]}
                  for row in sorted(result.get("daily_entries", []), key=lambda r: (r["category"], r["when"]))]
    return {
        "t": "usage_receipt", "print_key": period + ":" + result["label"],
        "date": result["label"], "tokens": format(total["tokens"], ","),
        "cost": short_usd(total["cost_usd"]), "change": change,
        "comparison": 'vs.' + {"day": 'previous day', "week": 'previous week', "month": 'previous month', "year": 'previous year'}[period],
        "trend": "up" if delta is not None and delta > 0 else "down" if delta is not None and delta < 0 else "",
        "series": {key: [b[key] for b in result["series"] if not b["future"]]
                   for key in ("tokens", "cost_usd")},
        "rows": daily_rows if period == "day" else [{"name": name, "tokens": format(value["tokens"], ","),
                  "cost": short_usd(value["cost_usd"]) if value["priced"] else 'Unpriced'}
                 for name, value in result["by_model"]],
        "kinds": [[label, format(total[key], ",")] for key, label, _ in TOKEN_KINDS if total[key]],
        "calls": str(total["calls"]), "searches": str(total["searches"]),
        "note": 'Currency: USD',
        "unpriced": ('Unpriced: %s (%s tokens)' % ("、".join(total["unpriced"]),
                     format(total["unpriced_tokens"], ","))) if total["unpriced"] else "",
    }


class UsageWindow:
    def __init__(self, master, config_dir, font_family="Microsoft YaHei UI", expense_mode=False):
        self.expense_mode = expense_mode
        self.category = None
        self.config_dir = config_dir
        self.font = font_family
        self.period = "day"
        self.offset = 0
        self.result = None
        self._metrics_key, self._metrics_job = None, None
        self._metrics_fraction = 1
        self._rings_fraction = 1
        self._metrics_started = 0
        window = self.window = tk.Toplevel(master)
        window.title("Expenses" if expense_mode else 'Usage statistics')
        window.geometry("980x780")
        window.minsize(760, 600)
        window.configure(bg=BG)
        body = tk.Frame(window, bg=BG)
        body.pack(fill=tk.BOTH, expand=True)

        top = tk.Frame(body, bg=BG)
        top.pack(fill=tk.X, padx=18, pady=(14, 6))
        tk.Label(top, text="Expenses" if expense_mode else 'Usage statistics', bg=BG, fg=TEXT, font=(self.font, 15, "bold")).pack(side=tk.LEFT)
        self.period_buttons = {}
        period_area = tk.Frame(top, bg=BG)
        period_area.pack(side=tk.LEFT, padx=(22, 0))
        group = tk.Frame(period_area, bg=BG)
        group.pack(anchor="w")
        if expense_mode:
            import expenses
        for period in usage_stats.PERIODS:
            button = tk.Label(group, text=(expenses.PERIOD_LABELS if expense_mode else usage_stats.PERIOD_NAMES)[period], bg=PANEL, fg=MUTED,
                              font=(self.font, 10), padx=14, pady=4, cursor="hand2")
            button.pack(side=tk.LEFT, padx=1)
            button.bind("<Button-1>", lambda _e, p=period: self.set_period(p))
            self.period_buttons[period] = button
        nav = tk.Frame(top, bg=BG)
        nav.pack(side=tk.LEFT, padx=(18, 0))
        self.previous_button = self._link(nav, "◀", lambda: self.move(-1))
        self.previous_button.pack(side=tk.LEFT)
        self.title = tk.Label(period_area if expense_mode else nav, text="", bg=BG,
                              fg=TEXT, font=(self.font, 10 if expense_mode else 11),
                              width=0 if expense_mode else 26)
        self.title.pack(anchor="w", pady=(3, 0)) if expense_mode else self.title.pack(side=tk.LEFT)
        self.next_button = self._link(nav, "▶", lambda: self.move(1))
        self.next_button.pack(side=tk.LEFT)
        if not expense_mode:
            self._link(nav, 'Back to now', lambda: self.move(None)).pack(side=tk.LEFT, padx=(10, 0))
        main = tk.Frame(body, bg=BG)
        main.pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 4) if expense_mode else 12)
        main.grid_rowconfigure(0, weight=1)
        main.grid_columnconfigure(0, weight=1)
        main.grid_columnconfigure(1, weight=1)
        cards = tk.Frame(main, bg=BG)
        cards.grid(row=0, column=0, sticky="nsew", padx=(0, 24))
        self.cards = []
        for index in range(1 if expense_mode else 2):
            card = tk.Frame(cards, bg=PANEL, padx=22, pady=24)
            card.pack(fill=tk.X, pady=(0, 18))
            heading = tk.Frame(card, bg=PANEL) if expense_mode else card
            if expense_mode:
                heading.pack(fill=tk.X, pady=4)
            name = tk.Label(heading, bg=PANEL, fg=MUTED, font=(self.font, 11), anchor="w")
            value = tk.Label(card, bg=PANEL, fg=TEXT, font=(self.font, 25), anchor="w")
            detail = tk.Label(card, bg=PANEL, fg=MUTED, font=(self.font, 9),
                              anchor="w", justify=tk.LEFT, wraplength=260)
            if expense_mode:
                name.pack(side=tk.LEFT)
                self.expense_back = self._link(heading, "back", lambda: self.move(None))
                self.expense_back.pack(side=tk.RIGHT)
                value.pack(fill=tk.X, pady=4)
                self.expense_trend = tk.Canvas(card, bg=PANEL, height=76,
                                                borderwidth=0, highlightthickness=0)
                self.expense_trend.pack(fill=tk.X, pady=(8, 0))
                self.expense_trend.bind("<Configure>", lambda _e: self._draw_expense_trend())
            else:
                for label in (name, value, detail):
                    label.pack(fill=tk.X, pady=4)
            self.cards.append((name, value, detail))
        self.comparison = tk.Label(cards, bg=BG, fg=ACCENT, font=(self.font, 14), anchor="w")
        self.comparison.pack(fill=tk.X)
        if expense_mode:
            category_header = tk.Frame(cards, bg=BG)
            category_header.pack(fill=tk.X, pady=6)
            tk.Label(category_header, text="CATEGORY", bg=BG, fg=MUTED).pack(side=tk.LEFT)
            self.category_list = tk.Canvas(cards, bg=PANEL, borderwidth=0,
                                           highlightthickness=0, height=200, width=280,
                                           takefocus=True, cursor="hand2")
            self.category_list.pack(fill=tk.BOTH, expand=True, pady=12)
            self.category_list.bind("<Button-1>", self._category_selected)
            self.category_list.bind("<Configure>", lambda e: self._draw_categories())
            self.category_list.bind("<MouseWheel>", lambda e: self.category_list.yview_scroll(
                -1 if e.delta > 0 else 1, "units"))
            self.category_list.bind("<Down>", lambda e: self._step_category(1))
            self.category_list.bind("<Up>", lambda e: self._step_category(-1))
        self.receipt = tk.Canvas(main, bg=BG, width=370 if expense_mode else 330, height=1, highlightthickness=0, cursor="hand2")
        self.receipt.grid(row=0, column=1, sticky="ns" if expense_mode else "nsew")
        self._print_key, self._print_job, self._print_started = None, None, 0
        self._receipt_press = None
        self.receipt_viewer = None
        self.receipt.bind("<Configure>", lambda _e: self.draw_receipt())
        self.receipt.bind("<MouseWheel>", self._receipt_scroll)
        self.receipt.bind("<ButtonPress-1>", lambda e: setattr(self, "_receipt_press", (e.x, e.y)))
        self.receipt.bind("<ButtonRelease-1>", self._view_receipt)
        window.bind("<Destroy>", self._receipt_closed, add="+")

        self.refresh()
        self._tick()

    def _link(self, parent, text, command):
        label = tk.Label(parent, text=text, bg=parent.cget("bg"), fg=ACCENT, font=(self.font, 10),
                         padx=6, cursor="hand2")
        label.bind("<Button-1>", lambda _e: command())
        return label

    # ---- state -------------------------------------------------------------------
    def set_period(self, period):
        self.period, self.offset = period, 0
        self.refresh()

    def move(self, step):
        if step is not None and step < 0 and self.result is not None and not self.result.get("has_earlier"):
            return  # nothing earlier was recorded: don't page into empty periods
        self.offset = 0 if step is None else min(0, self.offset + step)
        self.refresh()

    def select_category(self, category):
        self.category = None if category == self.category else category
        self.refresh()
        if self.result and self.category:
            names = [item["name"] for item in self.result["categories"]]
            y = names.index(category)*62
            top = self.category_list.canvasy(0)
            bottom = top + self.category_list.winfo_height()
            if y < top or y+62 > bottom:
                self.category_list.yview_moveto(max(0, y if y < top else y+62-self.category_list.winfo_height()) /
                                               (62*len(names)))

    def _category_selected(self, event):
        self.category_list.focus_set()
        index = int(self.category_list.canvasy(event.y) // 62)
        if self.result and 0 <= index < len(self.result["categories"]):
            self.select_category(self.result["categories"][index]["name"])

    def _step_category(self, step):
        names = [item["name"] for item in self.result["categories"]]
        index = names.index(self.category) if self.category in names else (-1 if step > 0 else len(names))
        category = names[max(0, min(len(names)-1, index+step))]
        if category != self.category:
            self.select_category(category)
        return "break"

    def _draw_categories(self):
        if not self.result:
            return
        canvas = self.category_list
        scroll = canvas.yview()[0]
        canvas.delete("all")
        self._category_ring_images = []
        for index, item in enumerate(self.result["categories"]):
            y = index * 62
            if item["name"] == self.category:
                canvas.create_rectangle(0, y, canvas.winfo_width(), y+62, fill="#203d49", outline="")
            background = "#203d49" if item["name"] == self.category else PANEL
            percent = (self._metric_state["rings"].get(item["name"], 0)
                       if getattr(self, "_metric_from", None) else
                       max(0, min(100, item["percent"])) * self._rings_fraction)
            rgb = tuple(value // 257 for value in canvas.winfo_rgb(background))
            image = tk.PhotoImage(master=canvas, data=_category_ring_pixels(percent, rgb), format="PPM")
            self._category_ring_images.append(image)
            canvas.create_image(30, y+31, image=image)
            canvas.create_text(68, y+31, text=item["name"], fill=TEXT, anchor="w",
                               font=(self.font, 11), tags="category-name")
        canvas.configure(scrollregion=(0, 0, canvas.winfo_width(), 62*len(self.result["categories"])))
        canvas.yview_moveto(scroll)

    def _receipt_data(self):
        return self.result if self.expense_mode else receipt_data(self.result, self.period)

    def _receipt_closed(self, event):
        if event.widget is self.window and self._metrics_job:
            self.window.after_cancel(self._metrics_job)
            self._metrics_job = None
        if event.widget is self.window and self._print_job:
            self.window.after_cancel(self._print_job)
            self._print_job = None

    def _receipt_scroll(self, event):
        if time.monotonic() - self._print_started >= 2:
            self.receipt.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    def _view_receipt(self, event=None):
        if time.monotonic() - self._print_started < 2:
            return
        if event and self._receipt_press and (
                abs(event.x-self._receipt_press[0])+abs(event.y-self._receipt_press[1]) > 7):
            return
        if self.receipt_viewer is not None and self.receipt_viewer.winfo_exists():
            self.receipt_viewer.lift()
            return
        viewer = self.receipt_viewer = tk.Toplevel(self.window)
        viewer.title('Full receipt')
        viewer.geometry("720x720")
        viewer.configure(bg=BG)
        toolbar = tk.Frame(viewer, bg=BG)
        toolbar.pack(fill=tk.X)
        canvas = tk.Canvas(viewer, bg=BG, highlightthickness=0)
        canvas.pack(fill=tk.BOTH, expand=True)
        # Snapshot vector primitives keeps the complete receipt legible at any zoom.
        for ident in self.receipt.find_all():
            if "printer" in self.receipt.gettags(ident):
                continue
            kind = self.receipt.type(ident)
            fields = ("fill", "outline") if kind in ("polygon", "oval") else (
                ("outline", "start", "extent", "style", "width") if kind == "arc" else
                ("fill", "width", "dash") if kind == "line" else
                ("image", "anchor") if kind == "image" else
                ("text", "fill", "font", "anchor", "width"))
            options = {k: self.receipt.itemcget(ident, k) for k in fields}
            getattr(canvas, "create_"+kind)(*self.receipt.coords(ident), **options)
        originals = {i: canvas.coords(i) for i in canvas.find_all()}
        fonts = {i: canvas.itemcget(i, "font") for i in canvas.find_all() if canvas.type(i) == "text"}
        images = [i for i in canvas.find_all() if canvas.type(i) == "image"]
        if images:
            import receipt_art
            source = tk.PhotoImage(master=canvas, file=str(receipt_art.PATH))
            viewer._art_source = source
        box = canvas.bbox("all")
        state = {"scale": 1, "fit": True}
        def zoom(factor=None):
            from tkinter import font
            if factor is None:
                state["fit"] = True
                scale = min((canvas.winfo_width()-30)/(box[2]-box[0]),
                            (canvas.winfo_height()-30)/(box[3]-box[1]))
            else:
                state["fit"] = False
                scale = factor
            scale = max(.1, min(4, scale))
            state["scale"] = scale
            for i, coords in originals.items():
                canvas.coords(i, *[(v-(box[0] if n % 2 == 0 else box[1]))*scale+15
                                  for n, v in enumerate(coords)])
                if i in fonts:
                    f = font.Font(font=fonts[i])
                    canvas.itemconfigure(i, font=(f.actual("family"), max(1, round(f.actual("size")*scale))))
                if i in images:
                    factor = max(1, round(source.width() / (self._receipt_character.width() * scale)))
                    viewer._art_zoom = source.subsample(factor)
                    canvas.itemconfigure(i, image=viewer._art_zoom)
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.yview_moveto(0)
        for label, action in (
                ("−", lambda: zoom(state["scale"]/1.25)),
                ("+", lambda: zoom(state["scale"]*1.25)),
                ('Fit receipt', lambda: zoom()),
                ('Original size', lambda: zoom(1)),
                ('Close', viewer.destroy)):
            self._link(toolbar, label, action).pack(side=tk.LEFT, padx=4, pady=8)
        canvas.bind("<Configure>", lambda e: zoom() if state["fit"] else None)
        canvas.bind("<MouseWheel>", lambda e: canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        canvas.bind("<ButtonPress-1>", lambda e: canvas.scan_mark(e.x, e.y))
        canvas.bind("<B1-Motion>", lambda e: canvas.scan_dragto(e.x, e.y, gain=1))
        viewer.bind("<Escape>", lambda e: viewer.destroy())
        viewer.after_idle(zoom)

    def draw_receipt(self):
        if not self.result:
            return
        if self._print_job:
            self.window.after_cancel(self._print_job)
            self._print_job = None
        data = self._receipt_data()
        token_category = self.expense_mode and data.get("selected_category") == "token"
        header_extra = 20 if token_category else 0
        if self._print_key != data["print_key"]:
            self._print_key = data["print_key"]
            self._print_started = time.monotonic()
            self.receipt.yview_moveto(0)
        elapsed = time.monotonic() - self._print_started
        progress = min(1, elapsed / .85)
        c = self.receipt
        c.delete("all")
        width = max(280, c.winfo_width())
        left, right = 16, width - 16
        daily_sizes = {}
        for index, item in enumerate(data["rows"]):
            if item.get("usage_category"):
                measured = []
                for value, size, space in ((item["name"], 9, right-left-125), (item.get("detail", ""), 8, right-left-32)):
                    ident = c.create_text(0, 0, text=value, anchor="nw", width=max(80, space), font=(self.font, size))
                    box = c.bbox(ident)
                    measured.append(box[3]-box[1] if box else 16)
                    c.delete(ident)
                daily_sizes[index] = measured
        height = 340 + header_extra + len(data["rows"]) * 46 + len(data.get("kinds", [])) * 20
        height += sum(sum(sizes)+20-46 for sizes in daily_sizes.values())
        height += 28 * len({r["usage_category"] for r in data["rows"] if r.get("usage_category")})
        if self.expense_mode:
            height += 80 + (166 if data["zero"] else 0) + 28 * len(data["note"].splitlines())
            if data.get("token_total") and not token_category:
                height += 26
        c.configure(scrollregion=(0, 0, width, height + 38))
        tear = max(0, min(1, (elapsed-1)/1))
        shift = -(height + 20) * (1 - progress) + 14 * tear
        torn = [(0,5),(3,7),(5,3),(8,6),(11,4),(13,8),(16,5),(19,6),(22,2),
                (25,5),(28,9),(30,5),(34,7),(37,3),(40,6),(43,4),(46,8),(49,5),(52,7),
                (56,2),(59,5),(62,4),(65,8),(68,6),(71,9),(74,4),(77,6),(80,3),(84,7),
                (87,5),(90,8),(93,4),(96,6),(98,3),(100,5)]
        edge = [(left+(right-left)*x/100, 12+shift+(d if elapsed >= 1 else 0)) for x, d in torn]
        lengths = [math.hypot(b[0]-a[0], b[1]-a[1]) for a, b in zip(edge, edge[1:])]
        total = sum(lengths)
        cut, walked = edge[-1], 0
        for a, b, length in zip(edge, edge[1:], lengths):
            if walked + length >= tear*total:
                fraction = (tear*total-walked)/length
                cut = (a[0]+(b[0]-a[0])*fraction, a[1]+(b[1]-a[1])*fraction)
                break
            walked += length
        points = [v for point in edge for v in point] + [right, height+shift]
        for x in range(int(right), int(left) - 1, -8):
            points.extend([x, height + shift + (6 if ((int(right) - x) // 8) % 2 else 0)])
        points.extend([left, height + shift])
        c.create_polygon(points, fill="#f5f2e8", outline="")
        if tear:
            opened = [p for p in edge if p[0] < cut[0]] + [cut]
            if len(opened) > 1:
                c.create_line(*[v for x, y in opened for v in (x, y+1)], fill="#d4cdbb", width=1)
        def text(x, y, value, size=10, anchor="w", color="#30342f"):
            c.create_text(x, y + shift, text=value, anchor=anchor, fill=color,
                          font=(self.font, size), width=right-left-32 if anchor == "center" else 0)
        def row(y, name, value, size=9):
            text(left+16, y, name, size)
            text(right-16, y, value, size, "e")
        def rule(y):
            c.create_line(left+16, y+shift, right-16, y+shift, fill="#85877b", dash=(3, 3))
        text(width/2, 43, "M Y N", 23, "center")
        text(width/2, 74, data.get("heading", 'USAGE RECEIPT'), 11, "center")
        if token_category:
            text(width/2, 96, format(data.get("token_total", 0), ","), 13, "center")
        text(width/2, 101+header_extra, data["date"], 9, "center")
        rule(124+header_extra)
        y = 145+header_extra
        if self.expense_mode and data["zero"]:
            import receipt_art
            if not getattr(self, "_receipt_character", None):
                source = tk.PhotoImage(master=c, file=str(receipt_art.PATH))
                factor = max(1, math.ceil(max(source.width(), source.height()) / 112))
                self._receipt_character = source.subsample(factor)
            c.create_image(width/2, y+54+shift, image=self._receipt_character)
            y += 120
            text(width/2, y+4, 'No spending this period', 10, "center", "#73766c")
            text(width/2, y+27, 'Nicely done!', 10, "center", "#73766c")
            y += 46
        elif not data["rows"]:
            text(width/2, y, 'No usage records this period', 10, "center")
        usage_category = None
        for index, item in enumerate(data["rows"]):
            if index in daily_sizes:
                if usage_category != item["usage_category"]:
                    usage_category = item["usage_category"]
                    text(left+16, y, usage_category, 10)
                    y += 28
                name_height, detail_height = daily_sizes[index]
                c.create_text(left+16, y+shift, text=item["name"], anchor="nw", fill="#30342f",
                              width=max(80, right-left-125), font=(self.font, 9))
                text(right-16, y+8, item["cost"], 9, "e")
                c.create_text(left+16, y+name_height+6+shift, text=item.get("detail", ""), anchor="nw",
                              width=right-left-32, fill="#73766c", font=(self.font, 8))
                y += name_height+detail_height+20
                continue
            row(y, item["name"][:22], item["cost"])
            text(left+16, y+18, item.get("detail", "") if self.expense_mode else item["tokens"]+" Token",
                 8, color="#73766c")
            y += 46
        y += 22
        rule(y)
        row(y+28, 'Total / USD', data["cost"], 12)
        row(y+54, data["totals"][1][0] if self.expense_mode else "Token",
            data["totals"][1][1] if self.expense_mode else data["tokens"])
        y += 80
        if self.expense_mode:
            row(y, data["totals"][2][0], data["totals"][2][1])
            y += 26
            if data.get("token_total") and not token_category:
                row(y, "Token", format(data["token_total"], ","))
                y += 26
        for label, value in data.get("kinds", []):
            row(y, label, value, 8)
            y += 20
        rule(y+2)
        row(y+24, 'Line items' if self.expense_mode else 'Requests / web searches',
            str(data["count"]) if self.expense_mode else data["calls"]+" / "+data["searches"])
        text(width/2, y+57, data["note"], 8, "center")
        if data["unpriced"]:
            text(width/2, y+82, data["unpriced"], 8, "center")
        text(width/2, height-24, "— THANK YOU —", 9, "center")
        c.create_rectangle(0, 0, width, 14, fill=BG, outline="", tags="printer")
        # A short matching piece stays in the mouth after detachment, then fades.
        if 1.02 <= elapsed < 3.1:
            fade = max(0, min(1, (elapsed-2.05)/1.05))
            # winfo_rgb is 16-bit; blend in 8-bit display space.
            rgb = tuple(round(a+(b/257-a)*fade) for a, b in
                        zip((245, 242, 232), c.winfo_rgb(BG)))
            color = "#%02x%02x%02x" % rgb
            bottom = [(left+(right-left)*x/100, 12+d) for x, d in reversed(torn)]
            c.create_polygon(left, 12, right, 12,
                             *[v for point in bottom for v in point],
                             fill=color, outline="", tags="printer")
        c.create_line(10, 15, width-10, 15, width=10, fill="#48565d", capstyle=tk.ROUND, tags="printer")
        c.create_line(12, 16, width-12, 16, width=3, fill="#10191e", capstyle=tk.ROUND, tags="printer")
        if 0 < tear < 1:
            remaining = [cut] + [p for p in edge if p[0] > cut[0]]
            connected = [(x, y-shift-.5) for x, y in remaining] + \
                        [(x, y+.5) for x, y in reversed(remaining)]
            c.create_polygon(*[v for p in connected for v in p], fill="#f5f2e8",
                             outline="", tags="receipt_join")
            start, end = max(0, tear*total-total*.10), tear*total
            trail, walked = [], 0
            for a, b, length in zip(edge, edge[1:], lengths):
                lo, hi = max(start, walked), min(end, walked+length)
                if lo < hi:
                    for distance in (lo, hi):
                        fraction = (distance-walked)/length
                        trail.extend((a[0]+(b[0]-a[0])*fraction, a[1]+(b[1]-a[1])*fraction+1))
                walked += length
            if len(trail) >= 4:
                c.create_line(*trail, fill="#1685ac", width=5, capstyle=tk.ROUND, tags="printer")
                c.create_line(*trail, fill="#ffffff", width=2.5, capstyle=tk.ROUND, tags="printer")
        if elapsed < 3.1:
            self._print_job = self.window.after(16, self.draw_receipt)

    def _tick(self):
        """Keep the current period live while the window is open."""
        if not self.window.winfo_exists():
            return
        if self.offset == 0:
            self.refresh()
        self.window.after(30000, self._tick)

    def refresh(self, reload=True):
        if reload or self.result is None:
            try:
                if self.expense_mode:
                    import expenses
                    self.result = expenses.report(self.config_dir, self.period, self.offset, self.category)
                else:
                    self.result = usage_stats.report(self.config_dir, self.period, self.offset)
            except Exception as error:  # a broken log never breaks the window
                self.result = None
                self.title.configure(text='Could not read: %s' % type(error).__name__)
                return
        for period, button in self.period_buttons.items():
            button.configure(bg="#16677e" if period == self.period else PANEL,
                             fg="#ffffff" if period == self.period else MUTED)
        self.next_button.configure(fg=ACCENT if self.offset < 0 else LINE)
        self.previous_button.configure(fg=ACCENT if self.result.get("has_earlier") else LINE)
        self.title.configure(text=self.result["date"] if self.expense_mode else self.result["label"])
        self.fill_cards()
        receipt = self._receipt_data()
        self.comparison.configure(text="" if self.expense_mode else receipt["change"] + "  " + receipt["comparison"] + 'Cost')
        self.draw_receipt()

    # ---- cards -------------------------------------------------------------------
    def _draw_expense_trend(self):
        if not self.result:
            return
        canvas = self.expense_trend
        canvas.delete("all")
        if self.result.get("chart_kind") == "pie":
            transitioning = bool(getattr(self, "_metric_from", None))
            slices = (self._metric_state["pies"] if transitioning else self.result.get("chart_slices", []))
            height = max(88, len(slices)*13)
            canvas.configure(height=height)
            cx, cy = max(20, canvas.winfo_width())/2, height/2
            box = (cx-35, cy-35, cx+35, cy+35)
            total = (1 if slices else 0) if transitioning else sum(item["value"] for item in slices)
            start = 90
            remaining = (1 if transitioning else self._rings_fraction) * 360
            callouts = []
            if not total:
                canvas.create_oval(*box, fill="#302c43", outline="")
            for index, item in enumerate(slices):
                portion = item["value"]/total
                visible = min(portion*360, max(0, remaining))
                if visible >= 360:
                    canvas.create_oval(*box, fill=item["color"], outline="")
                elif visible > 0:
                    canvas.create_arc(*box, start=start, extent=-visible,
                                      style=tk.PIESLICE, fill=item["color"], outline=PANEL, width=1)
                middle = 0 if portion >= 1 else math.radians(start-180*portion)
                callouts.append((item, middle, 1 if math.cos(middle) >= 0 else -1))
                start -= 360*portion
                remaining -= 360*portion
            for side in (-1, 1):
                items = sorted((item for item in callouts if item[2] == side), key=lambda item: -math.sin(item[1]))
                positions = [cy-35*math.sin(item[1]) for item in items]
                for index in range(len(positions)):
                    positions[index] = max(positions[index], positions[index-1]+13 if index else 7)
                for index in reversed(range(len(positions))):
                    positions[index] = min(positions[index], positions[index+1]-13
                                           if index < len(positions)-1 else height-7)
                for index, (item, angle, _) in enumerate(items):
                    y = positions[index]
                    end_x = cx+55*side
                    canvas.create_line(cx+35*math.cos(angle), cy-35*math.sin(angle),
                                       cx+41*side, cy-35*math.sin(angle), cx+49*side, y, end_x, y,
                                       fill=item["color"], width=1, joinstyle=tk.ROUND, tags="pie-leader")
                    canvas.create_text(end_x+4*side, y, text=item["name"], anchor="w" if side > 0 else "e",
                                       fill=TEXT, font=(self.font, 8), tags="pie-label")
            return
        canvas.configure(height=76)
        values = self.result["series"]["cost_usd"]
        positions = self.result.get("series_positions", [])
        width = max(20, canvas.winfo_width())
        peak = max([1] + values)
        canvas.create_line(3, 54, width-3, 54, fill=LINE)
        coords = [(3+positions[i]*(width-6), 54-value/peak*48)
                  for i, value in enumerate(values)]
        transitioning = bool(getattr(self, "_metric_from", None))
        if transitioning:
            coords = [(3+self._metric_state["series"][label][0]*(width-6),
                       54-self._metric_state["series"][label][1])
                      for label in self.result["series_labels"]]
        bars = self.result.get("chart_kind") == "bar"
        reveal = self._metric_state["reveal"] if transitioning else self._rings_fraction
        cutoff = 3+(width-6)*reveal
        if len(coords) > 1 and not bars and reveal > 0:
            partial = [coords[0]]
            for (x0, y0), (x, y) in zip(coords, coords[1:]):
                if x <= cutoff:
                    partial.append((x, y))
                else:
                    partial.append((cutoff, y0+(y-y0)*(cutoff-x0)/(x-x0)))
                    break
            canvas.create_line(*[n for point in partial for n in point],
                               fill="#b594f0", width=2, joinstyle=tk.ROUND, tags="expense-line")
        for x, y in coords:
            if bars:
                if not transitioning:
                    y = 54-(54-y)*self._rings_fraction
                half_width = min(16, (width-6)/max(1, len(values))*.3)
                if y < 54:
                    canvas.create_rectangle(x-half_width, y, x+half_width, 54, fill="#b594f0", outline="")
            elif reveal > 0 and x <= cutoff:
                canvas.create_oval(x-2, y-2, x+2, y+2, fill="#b594f0", outline="")
        for position, label in self.result.get("trend_ticks", []):
            canvas.create_text(3+position*(width-6), 68, text=label,
                               anchor="w" if position == 0 else "e" if position == 1 else "center",
                               fill=MUTED, font=(self.font, 8))

    def _paint_metric_amount(self):
        self._metric_state = self._metric_snapshot()
        target = self.result["cost"]
        if self._metrics_fraction >= 1:
            text = target
        else:
            text = "$%s USD" % format(self._metric_state["amount"], ",.2f")
        self.cards[0][1].configure(text=text)

    def _metric_snapshot(self):
        target = self._metric_target
        before = getattr(self, "_metric_from", None)
        p = self._rings_fraction
        mix = lambda a, b, progress=p: a+(b-a)*progress
        if before:
            names = list(dict.fromkeys([s["name"] for s in before["pies"] + target["pies"]]))
            a = {s["name"]: s for s in before["pies"]}
            b = {s["name"]: s for s in target["pies"]}
            pies = [dict(b.get(n, a.get(n)), value=mix(a.get(n, {}).get("value", 0),
                                                       b.get(n, {}).get("value", 0))) for n in names]
            return dict(amount=mix(before["amount"], target["amount"], self._metrics_fraction),
                        rings={n: mix(before["rings"].get(n, 0), v) for n, v in target["rings"].items()},
                        pies=[s for s in pies if s["value"] > 0],
                        series={n: [mix(before["series"].get(n, [x, 0])[0], x),
                                    mix(before["series"].get(n, [x, 0])[1], h)]
                                for n, (x, h) in target["series"].items()},
                        reveal=mix(before["reveal"], 1))
        remaining, pies = p, []
        for s in target["pies"]:
            value = min(s["value"], max(0, remaining))
            pies.append(dict(s, value=value))
            remaining -= s["value"]
        return dict(amount=target["amount"]*self._metrics_fraction,
                    rings={n: v*p for n, v in target["rings"].items()}, pies=pies,
                    series={n: [x, h*(p if self.result["chart_kind"] == "bar" else 1)]
                            for n, (x, h) in target["series"].items()}, reveal=p)

    def _animate_metrics(self):
        self._metrics_job = None
        if not self.window.winfo_exists() or not self.result:
            return
        progress = min(1, max(0, (time.monotonic()-self._metrics_started)/1.2))
        self._metrics_fraction = 1-(1-progress)**3
        self._rings_fraction = progress
        self._paint_metric_amount()
        self._draw_categories()
        if self.result.get("chart_kind") in ("pie", "bar", "line"):
            self._draw_expense_trend()
        if progress < 1:
            self._metrics_job = self.window.after(16, self._animate_metrics)

    def fill_cards(self):
        if self.expense_mode:
            values = self.result["series"]["cost_usd"]
            peak = max([1] + values)
            pie_total = sum(s["value"] for s in self.result["chart_slices"])
            target = dict(amount=float(self.result["cost"].replace("$", "").replace(",", "").replace(" USD", "")),
                          rings={s["name"]: s["percent"] for s in self.result["categories"]},
                          pies=[dict(s, value=s["value"]/pie_total) for s in self.result["chart_slices"]],
                          series={n: [x, v/peak*48] for n, x, v in
                                  zip(self.result["series_labels"], self.result["series_positions"], values)})
            new_period = self._metrics_key != self.result["print_key"]
            if new_period or target != getattr(self, "_metric_target", None):
                if self._metrics_job:
                    self.window.after_cancel(self._metrics_job)
                self._metric_from = None if new_period else self._metric_state
                self._metric_target = target
                self._metrics_key = self.result["print_key"]
                self._metrics_fraction = 0
                self._rings_fraction = 0
                self._metrics_started = time.monotonic()
                self._metrics_job = self.window.after(16, self._animate_metrics)
            name, value, detail = self.cards[0]
            name.configure(text=self.result["metric_label"])
            self._paint_metric_amount()
            detail.configure(text="")
            self.expense_back.configure(fg=LINE if self.offset == 0 else ACCENT)
            self._draw_expense_trend()
            self._draw_categories()
            return
        total, previous = self.result["totals"], self.result["previous"]
        names = {"day": 'previous day', "week": 'previous week', "month": 'previous month', "year": 'previous year'}
        change = ""
        if previous["cost_usd"] > 0:
            percent = (total["cost_usd"] - previous["cost_usd"]) / previous["cost_usd"] * 100
            change = ("↑ %d%%" if percent >= 0 else "↓ %d%%") % abs(round(percent))
        elif total["cost_usd"] > 0:
            change = 'New'
        average = total["cost_usd"] / total["calls"] if total["calls"] else 0
        values = (
            ('Total tokens', short_tokens(total["tokens"]),
             'Input %s | Output %s\nCache write %s | read %s%s' % (
                 short_tokens(total["input"]), short_tokens(total["output"]), short_tokens(total["cache_write"]),
                 short_tokens(total["cache_read"]), ' | Uncategorized %s' % short_tokens(total["other"]) if total["other"] else "")),
            ('Cost (USD)', short_usd(total["cost_usd"]),
             ('%s has no cost record (%s tokens)' % ("、".join(total["unpriced"]), short_tokens(total["unpriced_tokens"]))
              if total["unpriced"] else "") + ('\nIncludes estimates' if total["estimated"] else "")),
            ('Request count', "%d" % total["calls"], 'Average %s per request\n%d web searches' % (short_usd(average), total["searches"])),
            ('vs. %s' % names[self.period], change or "—", "%s %s · %s token" % (
                names[self.period], short_usd(previous["cost_usd"]), short_tokens(previous["tokens"]))),
        )
        for (name, value, detail), (title, big, small) in zip(self.cards, values):
            name.configure(text=title)
            value.configure(text=big, fg=(WARN if big.startswith("↑") else GOOD if big.startswith("↓") else TEXT))
            detail.configure(text=small)

