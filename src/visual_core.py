"""Animated scene for MYN's window: a dark fog void and a living neural core.

The core is a slowly turning, round ball of softly glowing neurons joined by
fibres that arc around it like lines on a globe and writhe gently, with free dendrites curling outward and bright signals travelling along
the connections. Connections fade out and grow back over time, so the network
keeps rebuilding itself. It is an original design in the spirit of an organic,
blue machine mind; it uses no film assets.

Performance: every canvas item is created once. Each frame only moves items and
changes a colour when its visible step changes, and all of that is sent to Tk as
one Tcl script (TclBatch). The module never reads user data.
"""

import math
import os
import random
import time
import tkinter as tk
from collections import deque
from pathlib import Path

BACKGROUND = (4, 9, 16)
BACKGROUND_HEX = "#040910"
DEEP_BLUE = (30, 85, 157)
ELECTRIC = (29, 147, 232)
CYAN = (56, 215, 246)
ICE = (190, 247, 255)
FRAME_MS = 30  # used only when the display refresh rate cannot be read
HIDDEN_FRAME_MS = 220
ASSETS = Path(__file__).resolve().parent / "assets"
SPACE_PLATE = ASSETS / "cosmos_v3.png"
SPACE_FOG = ASSETS / "cosmos_v3_fog.png"  # the plate blurred, 1/16 size

NEURONS = 64
FOG_CELL = 16  # px between samples of the fog plate's colour
FIBRE_SAMPLES = 7
SPARKS = 16


def display_refresh_rate():
    """The primary display's refresh rate in Hz (60 if it cannot be read)."""
    if os.name == "nt":
        try:
            import ctypes
            user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
            hdc = user32.GetDC(0)
            try:
                hertz = gdi32.GetDeviceCaps(hdc, 116)  # VREFRESH
            finally:
                user32.ReleaseDC(0, hdc)
            if 24 <= hertz <= 1000:
                return hertz
        except Exception:
            pass
    return 60


_COLORS = {}


def color(accent, strength, under=BACKGROUND):
    """Simulate transparency against what lies underneath, cached per 1/256 step."""
    step = int(max(0.0, min(1.0, strength)) * 256)
    key = (accent, step, under)
    value = _COLORS.get(key)
    if value is None:
        strength = step / 256
        value = _COLORS[key] = "#%02x%02x%02x" % tuple(
            round(base + (bright - base) * strength)
            for base, bright in zip(under, accent))
    return value


class TclBatch:
    """Collects one frame's canvas commands and runs them as a single Tcl script.

    Crossing from Python into Tcl once per command was most of the frame time;
    one script per frame keeps the same commands and order at a fraction of it.
    """

    def __init__(self, canvas):
        self.canvas = canvas
        self.path = str(canvas)
        self.script = []

    @staticmethod
    def _xy(values):
        return " ".join("%.1f" % value for value in values)

    def coords(self, item, *coords):
        self.script.append("%s coords %s %s" % (self.path, item, self._xy(coords)))

    def itemconfigure(self, item, **options):
        self.script.append("%s itemconfigure %s %s" % (self.path, item, " ".join(
            "-%s %s" % (name, value if value != "" else "{}") for name, value in options.items())))

    def flush(self):
        if self.script:
            self.canvas.tk.eval("\n".join(self.script))
            self.script = []


class _Item:
    """A persistent canvas item that only sends changes."""

    __slots__ = ("id", "fill", "state")

    def __init__(self, item_id):
        self.id, self.fill, self.state = item_id, None, "normal"

    def show(self, batch, visible):
        state = "normal" if visible else "hidden"
        if state != self.state:
            batch.itemconfigure(self.id, state=state)
            self.state = state
        return visible

    def paint(self, batch, fill):
        if fill != self.fill:
            batch.itemconfigure(self.id, fill=fill)
            self.fill = fill


class DeepSpaceScene:
    """Deep space: a nebula plate, and stars that keep flying past the viewer.

    The stars live in 3D. Each one starts far away near the core (the vanishing
    point), comes closer, brightens and stretches into a short streak as it
    speeds past the edge of the window, then starts again far away, so the view
    keeps travelling through space.
    """

    STARS = 150
    SPEED = .085  # depth units per second; a star takes about 11 s to arrive

    def __init__(self, canvas):
        self.canvas = canvas
        self.image = None
        self.plate_item = None
        self.fog_grid = None
        try:
            self.image = tk.PhotoImage(master=canvas, file=str(SPACE_PLATE))
            self.plate_item = canvas.create_image(0, 0, image=self.image,
                                                  anchor=tk.CENTER, tags="space_plate")
            self.fog_grid = self._load_fog()
            self.fog_origin = (self.image.width() / 2, self.image.height() / 2 - 3)
        except (OSError, tk.TclError, ValueError):
            self.image = None  # the stars and the core still work without it
        self.rng = random.Random(19771214)
        self.stars = []
        for _ in range(self.STARS):
            item = _Item(canvas.create_line(0, 0, 0, 0, fill=BACKGROUND_HEX, width=1,
                                            capstyle=tk.ROUND, tags="space_star"))
            star = {"item": item}
            self._respawn(star, self.rng.uniform(.06, 1.0))
            self.stars.append(star)
        self.last_time = None

    def _respawn(self, star, depth=1.0):
        angle = self.rng.random() * math.tau
        spread = .12 + 2.4 * math.sqrt(self.rng.random())
        star["x"], star["y"] = math.cos(angle) * spread, math.sin(angle) * spread
        star["z"] = depth
        star["tone"] = self.rng.choice((ICE, ICE, ICE, (170, 205, 255), (255, 236, 214)))
        star["power"] = .55 + .45 * self.rng.random()

    def _load_fog(self):
        """The plate's colour, blurred, every FOG_CELL px (from the small companion image)."""
        small = tk.PhotoImage(master=self.canvas, file=str(SPACE_FOG))
        tk_ = self.canvas.tk
        rows = tk_.splitlist(tk_.call(small, "data"))
        grid = [tuple(tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))
                      for value in tk_.splitlist(row)) for row in rows]
        return grid

    def fog(self, dx, dy):
        """The fog colour at an offset from the core's centre."""
        grid = self.fog_grid
        if grid is None:
            return BACKGROUND
        origin_x, origin_y = self.fog_origin
        row = int((origin_y + dy) / FOG_CELL)
        column = int((origin_x + dx) / FOG_CELL)
        if 0 <= row < len(grid) and 0 <= column < len(grid[0]):
            return grid[row][column]
        return BACKGROUND

    def draw(self, time_s, width, height, batch):
        dt = 0.0 if self.last_time is None else max(0.0, min(.25, time_s - self.last_time))
        self.last_time = time_s
        if self.plate_item is not None:
            batch.coords(self.plate_item, width / 2 + 4 * math.sin(time_s * .05),
                         height / 2 + 3 * math.cos(time_s * .04))
        cx, cy = width / 2, height / 2 - 3
        focal = 120.0
        # The journey surges gently, like a ship easing on and off the throttle.
        speed = self.SPEED * (1 + .35 * math.sin(time_s * .06))
        for star in self.stars:
            star["z"] -= dt * speed
            z = star["z"]
            x = cx + star["x"] / z * focal
            y = cy + star["y"] / z * focal
            if z < .035 or not (-40 < x < width + 40 and -40 < y < height + 40):
                self._respawn(star)
                star["item"].show(batch, False)
                continue
            # The tail is where the star was a moment ago: longer as it gets near.
            tail = min(1.0, z + .9 * speed * .2)
            tx = cx + star["x"] / tail * focal
            ty = cy + star["y"] / tail * focal
            length = math.hypot(x - tx, y - ty)
            if length > 26:  # keep streaks short, like motion blur, not bars
                tx, ty = x + (tx - x) * 26 / length, y + (ty - y) * 26 / length
            near = 1 - z
            strength = star["power"] * (near ** 1.6) * min(1.0, (1 - z) / .12) * .85
            item = star["item"]
            under = self.fog(x - cx, y - cy)
            # Passing dust is only seen against dark space, never over a lit planet.
            if not item.show(batch, strength > .03 and under[0] + under[1] + under[2] < 150):
                continue
            batch.coords(item.id, tx, ty, x, y)
            tone = star["tone"]
            strength = min(1.0, strength)
            item.paint(batch, "#%02x%02x%02x" % tuple(
                int(base + (bright - base) * strength) for base, bright in zip(under, tone)))


def _cluster(rng):
    """Neuron positions in a round ball: an evenly spaced outer shell (a golden-angle
    spiral, lightly jittered) around a looser interior."""
    shell = int(NEURONS * .55)
    points = []
    for index in range(shell):
        y = 1 - 2 * (index + .5) / shell
        ring = math.sqrt(1 - y * y)
        angle = index * 2.39996323 + rng.uniform(-.15, .15)
        radius = rng.uniform(.9, 1.0)
        points.append((math.cos(angle) * ring * radius, y * radius, math.sin(angle) * ring * radius))
    while len(points) < NEURONS:
        x, y, z = rng.uniform(-1, 1), rng.uniform(-1, 1), rng.uniform(-1, 1)
        length = math.sqrt(x * x + y * y + z * z)
        if not .05 < length <= 1:
            continue
        radius = .15 + .6 * rng.random() ** .6
        point = (x / length * radius, y / length * radius, z / length * radius)
        if all(sum((a - b) ** 2 for a, b in zip(point, other)) > .2 ** 2 for other in points):
            points.append(point)
    return points


def _wiring(points, rng):
    """Connect each neuron to its nearest neighbours plus a few long-range fibres."""
    edges = set()
    for index, point in enumerate(points):
        nearest = sorted(range(len(points)), key=lambda other: sum(
            (a - b) ** 2 for a, b in zip(point, points[other])))[1:4]
        for other in nearest[:2 + (index % 2)]:
            edges.add(tuple(sorted((index, other))))
    return sorted(edges)


def _bezier_weights(samples):
    weights = []
    for index in range(samples):
        t = index / (samples - 1)
        u = 1 - t
        weights.append((u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t))
    return weights


BEZIER = _bezier_weights(FIBRE_SAMPLES)
TENDRIL = _bezier_weights(7)


def _clamp(value):
    return 0.0 if value < 0 else 1.0 if value > 1 else value


class HolographicCore:
    """The neural core: neurons, fibres, dendrites, travelling signals and a heart.

    Glows are layered solid shapes rather than alpha images: Tk blends alpha
    images in software on every redraw, which made the animation stutter.
    Every brightness changes continuously, so nothing pops in or out.
    """

    def __init__(self, canvas):
        self.canvas = canvas
        self.space = DeepSpaceScene(canvas)
        rng = random.Random(20261016)
        self.points = _cluster(rng)
        self.edges = _wiring(self.points, rng)
        self.phases = [rng.random() * math.tau for _ in self.points]

        def line(width):
            return _Item(canvas.create_line(0, 0, 0, 0, fill=BACKGROUND_HEX, width=width,
                                            capstyle=tk.ROUND, joinstyle=tk.ROUND,
                                            smooth=False, tags="core_frame"))

        def dot():
            return _Item(canvas.create_oval(0, 0, 0, 0, outline="", fill=BACKGROUND_HEX,
                                            tags="core_frame"))

        # Back to front: neuron halos, fibres, dendrites, neuron bodies, signals, heart.
        self.halos = [dot() for _ in self.points]
        self.fibres = []
        for a, b in self.edges:
            self.fibres.append({
                "a": a, "b": b, "glow": line(3.0), "core": line(1.0),
                "phase": rng.random() * math.tau, "period": rng.uniform(9, 18),
                "bend": (rng.uniform(-.16, .16), rng.uniform(-.16, .16), rng.uniform(-.3, .3)),
                "base": .55 + .45 * rng.random(), "dormant": rng.random() < .35,
                "strength": 0.0,
            })
        self.tendrils = []
        for index, (px, py, pz) in enumerate(self.points):
            if px * px + py * py + pz * pz < .55 ** 2:
                continue  # dendrites only on the outer shell: a soft round edge
            for _branch in range(1 + (index % 2)):
                # Point mostly outward so the outline stays a soft round edge.
                dx, dy, dz = (px + rng.gauss(0, .55), py + rng.gauss(0, .55), pz + rng.gauss(0, .55))
                length = math.sqrt(dx * dx + dy * dy + dz * dz) or 1.0
                self.tendrils.append({
                    "n": index, "dir": (dx / length, dy / length, dz / length),
                    "length": rng.uniform(.10, .22), "curl": rng.uniform(-1, 1),
                    "phase": rng.random() * math.tau,
                    "item": line(1.0), "fork": line(1.0) if rng.random() < .4 else None,
                })
        self.mids = [dot() for _ in self.points]
        self.bodies = [dot() for _ in self.points]
        self.sparks = []
        for _index in range(SPARKS):
            self.sparks.append({"halo": dot(), "core": dot(),
                                "fibre": rng.randrange(len(self.fibres)),
                                "t": rng.random(), "speed": rng.uniform(.25, .5),
                                "forward": rng.random() < .5})
        self.heart_rings = [dot() for _ in range(4)]
        self.rng = rng
        self.last_time = None
        self.started = time.monotonic()
        self.frames = 0
        self.running = True
        # Paced to the monitor: one frame per refresh, and no extra wait at all
        # when a frame takes longer than that (all motion is time-based).
        self.refresh_hz = display_refresh_rate()
        self.interval = 1 / self.refresh_hz
        self.next_due = time.perf_counter()
        self.frame_times = deque(maxlen=120)
        self.canvas.after(1, self._tick)

    @property
    def fps(self):
        """Frames actually drawn per second over the last couple of seconds."""
        if len(self.frame_times) < 2:
            return 0.0
        span = self.frame_times[-1] - self.frame_times[0]
        return (len(self.frame_times) - 1) / span if span > 0 else 0.0

    def stop(self):
        self.running = False

    # ---- geometry ---------------------------------------------------------------

    @staticmethod
    def _view(time_s):
        yaw = time_s * .07
        pitch = -.20 + .06 * math.sin(time_s * .11)
        return math.cos(yaw), math.sin(yaw), math.cos(pitch), math.sin(pitch)

    @staticmethod
    def _turn(x, y, z, view):
        cy, sy, cp, sp = view
        x, z = x * cy + z * sy, -x * sy + z * cy
        return x, y * cp - z * sp, y * sp + z * cp

    def _neuron(self, index, time_s):
        """A neuron's living position: it drifts gently around its home."""
        x, y, z = self.points[index]
        phase = self.phases[index]
        return (x + .035 * math.sin(time_s * .27 + phase),
                y + .030 * math.cos(time_s * .23 + phase * 1.3),
                z + .035 * math.sin(time_s * .19 + phase * .7))

    @staticmethod
    def _disc(batch, item, x, y, r):
        batch.coords(item.id, x - r, y - r, x + r, y + r)

    # ---- frame ------------------------------------------------------------------

    def draw(self, time_s=None):
        """Draw a frame; an explicit time allows deterministic motion checks."""
        if time_s is None:
            time_s = time.monotonic() - self.started
        dt = 0.0 if self.last_time is None else max(0.0, min(.25, time_s - self.last_time))
        self.last_time = time_s
        width = max(self.canvas.winfo_width(), 1)
        height = max(self.canvas.winfo_height(), 1)
        cx, cy = width / 2, height / 2 - 3
        breath = 1 + .05 * math.sin(time_s * .8)
        radius = min(width * .2, height * .25, 145) * (1 + .012 * math.sin(time_s * .8))
        view = self._view(time_s)
        batch = TclBatch(self.canvas)
        self.space.draw(time_s, width, height, batch)

        # Project every neuron once: screen position and depth (-1 far .. 1 near).
        turned, projected = [], []
        for index in range(len(self.points)):
            x, y, z = self._turn(*self._neuron(index, time_s), view)
            turned.append((x, y, z))
            scale = radius * (1 + .14 * z)
            projected.append((cx + x * scale, cy - y * scale, z))

        def on_sphere(a, b, share, swell):
            """A point between two neurons, pushed out to their distance from the
            centre, so fibres arc around the ball like lines on a globe."""
            x = a[0] + (b[0] - a[0]) * share
            y = a[1] + (b[1] - a[1]) * share
            z = a[2] + (b[2] - a[2]) * share
            length = math.sqrt(x * x + y * y + z * z) or 1.0
            reach = (math.sqrt(a[0] ** 2 + a[1] ** 2 + a[2] ** 2) * (1 - share)
                     + math.sqrt(b[0] ** 2 + b[1] ** 2 + b[2] ** 2) * share) * swell / length
            x, y, z = x * reach, y * reach, z * reach
            scale = radius * (1 + .14 * z)
            return cx + x * scale, cy - y * scale

        space = self.space

        def fog(x, y):
            return space.fog(x - cx, y - cy)

        # Neuron halos (furthest layer): dim, soft and larger when near.
        pulses = []
        for index, (x, y, z) in enumerate(projected):
            pulse = .5 + .5 * math.sin(time_s * 1.1 + self.phases[index] * 3)
            pulses.append(pulse)
            near = (z + 1) / 2
            self._disc(batch, self.halos[index], x, y, (4.0 + 4.0 * near) * (1 + .15 * pulse))
            self.halos[index].paint(batch, color(ELECTRIC, (.07 + .10 * near + .04 * pulse) * breath, fog(x, y)))

        # Curved fibres: cubic curves whose middle bows out and writhes over time.
        paths = []
        for fibre in self.fibres:
            ax, ay, az = projected[fibre["a"]]
            bx, by, bz = projected[fibre["b"]]
            bend_x, bend_y, _bend_z = fibre["bend"]
            # Control points slightly beyond the arc (a cubic curve undershoots it),
            # breathing in and out a little so the fibres writhe.
            c1x, c1y = on_sphere(turned[fibre["a"]], turned[fibre["b"]], .33,
                                 1.10 + bend_x * .5 + .05 * math.sin(time_s * .3 + fibre["phase"]))
            c2x, c2y = on_sphere(turned[fibre["a"]], turned[fibre["b"]], .67,
                                 1.10 + bend_y * .5 + .05 * math.cos(time_s * .25 + fibre["phase"]))
            path = []
            for w0, w1, w2, w3 in BEZIER:
                path.append(w0 * ax + w1 * c1x + w2 * c2x + w3 * bx)
                path.append(w0 * ay + w1 * c1y + w2 * c2y + w3 * by)
            paths.append(path)
            # Activity: each connection slowly fades out now and then and grows back.
            cycle = .5 + .5 * math.sin(time_s * math.tau / fibre["period"] + fibre["phase"])
            cycle = cycle * cycle * (3 - 2 * cycle)  # ease, so fades start and end softly
            life = cycle if fibre["dormant"] else .4 + .6 * cycle
            depth = .45 + .55 * _clamp(((az + bz) / 2 + 1) / 2)
            strength = fibre["base"] * life * depth * breath
            visible = strength > .04
            fibre["glow"].show(batch, visible)
            fibre["core"].show(batch, visible)
            if visible:
                under = fog(path[FIBRE_SAMPLES - 1], path[FIBRE_SAMPLES])
                batch.coords(fibre["glow"].id, *path)
                batch.coords(fibre["core"].id, *path)
                fibre["glow"].paint(batch, color(DEEP_BLUE, .38 * strength, under))
                fibre["core"].paint(batch, color(ELECTRIC, .58 * strength, under))
            fibre["strength"] = strength if visible else 0.0

        # Dendrites: short curling branches with open ends, some of them forked.
        for tendril in self.tendrils:
            nx, ny, nz = projected[tendril["n"]]
            dx, dy, dz = self._turn(*tendril["dir"], view)
            reach = tendril["length"] * radius * (1 + .10 * math.sin(time_s * .35 + tendril["phase"]))
            curl = tendril["curl"] * (1 + .3 * math.sin(time_s * .4 + tendril["phase"]))
            ex, ey = nx + dx * reach, ny - dy * reach
            px, py = -dy * reach * curl * .6, -dx * reach * curl * .6  # sideways, for the curl
            c1x, c1y = nx + dx * reach * .35 + px * .4, ny - dy * reach * .35 + py * .4
            c2x, c2y = nx + dx * reach * .75 + px, ny - dy * reach * .75 + py
            path = []
            for w0, w1, w2, w3 in TENDRIL:
                path.append(w0 * nx + w1 * c1x + w2 * c2x + w3 * ex)
                path.append(w0 * ny + w1 * c1y + w2 * c2y + w3 * ey)
            depth = .45 + .55 * _clamp((nz + dz * .3 + 1) / 2)
            strength = (.40 + .20 * math.sin(time_s * .6 + tendril["phase"])) * depth * breath
            under = fog(nx, ny)
            if tendril["item"].show(batch, strength > .04):
                batch.coords(tendril["item"].id, *path)
                tendril["item"].paint(batch, color(ELECTRIC, .42 * strength, under))
            fork = tendril["fork"]
            if fork is not None and fork.show(batch, strength > .04):
                mx, my = path[6], path[7]  # branch off near the middle
                fx, fy = mx + (ex - nx) * .45 + py * .5, my + (ey - ny) * .45 - px * .5
                batch.coords(fork.id, mx, my, (mx + fx) / 2 + px * .2, (my + fy) / 2 + py * .2, fx, fy)
                fork.paint(batch, color(ELECTRIC, .32 * strength, under))

        # Neuron centres: a soft middle ring and a small bright body.
        for index, (x, y, z) in enumerate(projected):
            near, pulse = (z + 1) / 2, pulses[index]
            under = fog(x, y)
            self._disc(batch, self.mids[index], x, y, (2.0 + 1.6 * near) * (1 + .15 * pulse))
            self.mids[index].paint(batch, color(CYAN, .20 + .22 * near + .08 * pulse, under))
            self._disc(batch, self.bodies[index], x, y, .9 + 1.0 * near)
            self.bodies[index].paint(batch, color(ICE, .32 + .30 * near + .12 * pulse, under))

        # Signals glide along active fibres, fading in and out, then jump to another.
        for spark in self.sparks:
            if spark["t"] >= 1 or self.fibres[spark["fibre"]]["strength"] < .15:
                spark["fibre"] = self.rng.randrange(len(self.fibres))
                spark["t"] = 0.0
                spark["forward"] = self.rng.random() < .5
            spark["t"] += dt * spark["speed"]
            progress = min(spark["t"], 1.0)
            t = progress if spark["forward"] else 1 - progress
            path = paths[spark["fibre"]]
            position = t * (FIBRE_SAMPLES - 1)
            index = min(int(position), FIBRE_SAMPLES - 2)
            frac = position - index
            x = path[2 * index] + (path[2 * index + 2] - path[2 * index]) * frac
            y = path[2 * index + 1] + (path[2 * index + 3] - path[2 * index + 1]) * frac
            fade = math.sin(math.pi * progress)
            under = fog(x, y)
            self._disc(batch, spark["halo"], x, y, 3.6)
            self._disc(batch, spark["core"], x, y, 1.4)
            spark["halo"].paint(batch, color(ELECTRIC, .30 * fade, under))
            spark["core"].paint(batch, color(ICE, .70 * fade, under))

        # The heart of the cluster: a few soft rings that breathe.
        hx = cx + 3 * math.sin(time_s * .23)
        hy = cy + 2 * math.cos(time_s * .19)
        under = fog(hx, hy)
        for ring, (size, accent, strength) in zip(self.heart_rings, (
                (11, DEEP_BLUE, .22), (6.5, ELECTRIC, .30), (3.6, CYAN, .50), (1.8, ICE, .75))):
            self._disc(batch, ring, hx, hy, size * breath)
            ring.paint(batch, color(accent, strength * (.9 + .1 * breath), under))
        batch.flush()
        self.frames += 1

    def _tick(self):
        if not self.running:
            return
        try:
            if not self.canvas.winfo_viewable():
                self.frame_times.clear()
                self.last_time = None
                self.next_due = time.perf_counter()
                self.canvas.after(HIDDEN_FRAME_MS, self._tick)
                return
            self.draw()
            now = time.perf_counter()
            self.frame_times.append(now)
            self.next_due += self.interval
            if self.next_due < now:
                self.next_due = now  # behind: draw the next frame straight away
            delay = max(1, int((self.next_due - now) * 1000))
            # Schedule the next frame from an idle callback: Tk repaints the window
            # in its idle pass, which never comes while a timer is always due. This
            # way every frame is shown, and input is handled, even on a slow machine.
            self.canvas.after_idle(lambda: self.running and self.canvas.after(delay, self._tick))
        except Exception:
            self.running = False
