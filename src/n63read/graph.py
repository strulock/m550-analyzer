"""
n63read.graph - one channel in its own window, for a closer look.

GraphWindow follows the app's timed history of a channel and redraws a few
times a second. The time axis shows a chosen window (or everything) in
seconds, minutes or hours; the value axis fits what's shown or takes a
fixed range; the unit converts where a conversion makes sense (°C/°F,
hPa/bar/psi, km/h/mph...). Hovering reads off the nearest point. Pause
holds the view while the data keeps coming.

The unit and axis logic has no Tk in it, so it is tested on its own.
"""

import math
import tkinter as tk
from tkinter import ttk

# Unit families: unit -> factor to the family's base unit. Temperature is
# not a factor, so it has its own conversion.
_FAMILIES = [
    {"hPa": 1.0, "mbar": 1.0, "bar": 1000.0, "kPa": 10.0, "MPa": 10000.0,
     "psi": 68.9475729},
    {"V": 1.0, "mV": 0.001},
    {"A": 1.0, "mA": 0.001},
    {"m": 1.0, "mm": 0.001, "cm": 0.01, "km": 1000.0, "in": 0.0254, "ft": 0.3048,
     "mi": 1609.344},
    {"km/h": 1.0, "mph": 1.609344, "m/s": 3.6},
    {"kg/h": 1.0, "g/s": 3.6, "lb/min": 27.2155422},
    {"Nm": 1.0, "lb-ft": 1.35581795},
    {"s": 1.0, "ms": 0.001, "µs": 1e-6, "min": 60.0, "h": 3600.0},
    {"kWh": 3.6e6, "Ws": 1.0, "J": 1.0, "kJ": 1000.0},
]
_TEMPERATURE = ("°C", "°F", "K")
_ALIASES = {"degC": "°C", "Grad C": "°C", "1/min": "rpm", "us": "µs"}


def canonical(unit):
    unit = (unit or "").strip()
    return _ALIASES.get(unit, unit)


def units_for(unit):
    """The units a value in `unit` can be shown in, its own first."""
    unit = canonical(unit)
    if unit in _TEMPERATURE:
        return [unit] + [u for u in _TEMPERATURE if u != unit]
    for family in _FAMILIES:
        if unit in family:
            return [unit] + [u for u in family if u != unit]
    return [unit] if unit else [""]


def converter(src, dst):
    """A function taking a value in `src` to `dst`."""
    src, dst = canonical(src), canonical(dst)
    if src == dst:
        return lambda v: v
    if src in _TEMPERATURE and dst in _TEMPERATURE:
        to_c = {"°C": lambda v: v, "°F": lambda v: (v - 32) * 5 / 9,
                "K": lambda v: v - 273.15}[src]
        from_c = {"°C": lambda v: v, "°F": lambda v: v * 9 / 5 + 32,
                  "K": lambda v: v + 273.15}[dst]
        return lambda v: from_c(to_c(v))
    for family in _FAMILIES:
        if src in family and dst in family:
            k = family[src] / family[dst]
            return lambda v: v * k
    raise ValueError("can't convert %s to %s" % (src, dst))


TIME_WINDOWS = [("30 s", 30), ("1 min", 60), ("2 min", 120), ("5 min", 300),
                ("15 min", 900), ("1 h", 3600), ("All", None)]
TIME_UNITS = {"s": 1.0, "min": 60.0, "h": 3600.0}


def nice_ticks(lo, hi, count=5):
    """Round tick values covering lo..hi, about `count` of them."""
    if hi <= lo:
        hi = lo + 1.0
    raw = (hi - lo) / max(1, count)
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    first = math.ceil(lo / step - 1e-9) * step
    ticks, t = [], first
    while t <= hi + step * 1e-9:
        ticks.append(round(t, 12))
        t += step
    return ticks


def visible(points, window, now=None):
    """The (t, v) points inside the last `window` seconds (None: all)."""
    if not points:
        return []
    if window is None:
        return list(points)
    end = points[-1][0] if now is None else now
    return [p for p in points if p[0] >= end - window]


def value_range(values, fixed=None):
    """(lo, hi) for the value axis: `fixed` if given, else the data with a
    little room, never empty."""
    if fixed is not None:
        lo, hi = fixed
        return (lo, hi) if hi > lo else (lo, lo + 1.0)
    if not values:
        return 0.0, 1.0
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return lo - 1.0, hi + 1.0
    room = (hi - lo) * 0.05
    return lo - room, hi + room


def stats(values):
    if not values:
        return None
    return {"min": min(values), "max": max(values), "mean": sum(values) / len(values),
            "last": values[-1], "n": len(values)}


class GraphWindow(tk.Toplevel):
    """One channel, live, with its own axes."""

    REFRESH_MS = 250
    PAD_L, PAD_R, PAD_T, PAD_B = 64, 16, 12, 34

    def __init__(self, master, name, title, unit, history, color="#2d7dd2"):
        super().__init__(master)
        self.name, self.unit, self.history, self.color = name, canonical(unit), history, color
        self.title("%s  -  %s" % (title, name) if title and title != name else name)
        self.geometry("820x460")
        self.minsize(520, 320)
        self.paused = None                  # the points frozen by Pause
        self.hover = None

        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(fill="x")
        ttk.Label(bar, text="Time").pack(side="left")
        self.window_var = tk.StringVar(value="2 min")
        ttk.Combobox(bar, textvariable=self.window_var, width=7, state="readonly",
                     values=[w for w, _ in TIME_WINDOWS]).pack(side="left", padx=(4, 2))
        self.tunit_var = tk.StringVar(value="s")
        ttk.Combobox(bar, textvariable=self.tunit_var, width=4, state="readonly",
                     values=list(TIME_UNITS)).pack(side="left", padx=(0, 12))

        ttk.Label(bar, text="Unit").pack(side="left")
        self.unit_var = tk.StringVar(value=self.unit)
        choices = units_for(self.unit)
        ttk.Combobox(bar, textvariable=self.unit_var, width=7, state="readonly"
                     if len(choices) > 1 else "disabled",
                     values=choices).pack(side="left", padx=(4, 12))

        ttk.Label(bar, text="Scale").pack(side="left")
        self.auto_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Auto", variable=self.auto_var,
                        command=self._auto_changed).pack(side="left", padx=(4, 2))
        self.min_var, self.max_var = tk.StringVar(), tk.StringVar()
        self.min_entry = ttk.Entry(bar, textvariable=self.min_var, width=8, state="disabled")
        self.min_entry.pack(side="left")
        ttk.Label(bar, text="to").pack(side="left", padx=2)
        self.max_entry = ttk.Entry(bar, textvariable=self.max_var, width=8, state="disabled")
        self.max_entry.pack(side="left", padx=(0, 12))

        self.pause_btn = ttk.Button(bar, text="Pause", command=self._toggle_pause)
        self.pause_btn.pack(side="right")

        self.canvas = tk.Canvas(self, bg="#fbfbfb", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=8)
        self.stats_label = ttk.Label(self, padding=(8, 4), foreground="#555")
        self.stats_label.pack(fill="x")
        self.canvas.bind("<Motion>", self._motion)
        self.canvas.bind("<Leave>", lambda e: self._set_hover(None))
        self._job = self.after(10, self._tick)
        self.protocol("WM_DELETE_WINDOW", self.close)

    # -- controls

    def _auto_changed(self):
        state = "disabled" if self.auto_var.get() else "normal"
        if not self.auto_var.get() and not self.min_var.get():
            lo, hi = self.last_range if hasattr(self, "last_range") else (0.0, 1.0)
            self.min_var.set("%.4g" % lo)
            self.max_var.set("%.4g" % hi)
        self.min_entry.config(state=state)
        self.max_entry.config(state=state)

    def _toggle_pause(self):
        if self.paused is None:
            self.paused = list(self.history)
            self.pause_btn.config(text="Resume")
        else:
            self.paused = None
            self.pause_btn.config(text="Pause")

    def _set_hover(self, x):
        self.hover = x
        self.draw()

    def _motion(self, event):
        self._set_hover(event.x)

    def close(self):
        if self._job:
            self.after_cancel(self._job)
            self._job = None
        self.destroy()

    def _tick(self):
        self.draw()
        self._job = self.after(self.REFRESH_MS, self._tick)

    # -- drawing

    def settings(self):
        """(window s, time unit factor, value unit, fixed range or None)."""
        window = dict(TIME_WINDOWS).get(self.window_var.get(), 120)
        fixed = None
        if not self.auto_var.get():
            try:
                fixed = (float(self.min_var.get()), float(self.max_var.get()))
            except ValueError:
                fixed = None
        return window, TIME_UNITS.get(self.tunit_var.get(), 1.0), self.unit_var.get(), fixed

    def draw(self):
        c = self.canvas
        c.delete("all")
        w, h = max(c.winfo_width(), 200), max(c.winfo_height(), 120)
        L, R, T, B = self.PAD_L, w - self.PAD_R, self.PAD_T, h - self.PAD_B
        window, tfactor, unit, fixed = self.settings()
        try:
            conv = converter(self.unit, unit)
        except ValueError:
            conv, unit = (lambda v: v), self.unit
        points = [(t, conv(v)) for t, v in visible(self.paused or list(self.history), window)]
        if len(points) < 2:
            c.create_text(w // 2, h // 2, text="waiting for data", fill="#aaa")
            self.stats_label.config(text="")
            return
        t0, t1 = points[0][0], points[-1][0]
        if window is not None:
            t0 = max(t0, t1 - window)       # until the data fills the window
        t1 = max(t1, t0 + 1e-6)
        lo, hi = value_range([v for _, v in points], fixed)
        self.last_range = (lo, hi)

        def px(t):
            return L + (R - L) * (t - t0) / (t1 - t0)

        def py(v):
            return B - (B - T) * (v - lo) / (hi - lo)

        font = ("TkDefaultFont", 8)
        for v in nice_ticks(lo, hi):
            if lo <= v <= hi:
                c.create_line(L, py(v), R, py(v), fill="#e6e6e6")
                c.create_text(L - 6, py(v), anchor="e", text="%.6g" % v, font=font, fill="#666")
        for t in nice_ticks(t0 / tfactor, t1 / tfactor, 6):
            x = px(t * tfactor)
            if L <= x <= R:
                c.create_line(x, T, x, B, fill="#efefef")
                c.create_text(x, B + 4, anchor="n", text="%.6g" % t, font=font, fill="#666")
        c.create_rectangle(L, T, R, B, outline="#cfcfcf")
        c.create_text((L + R) // 2, h - 4, anchor="s", font=font, fill="#666",
                      text="time (%s)" % self.tunit_var.get())
        c.create_text(8, T, anchor="nw", font=font, fill="#666", text=unit or "value")

        coords = []
        for t, v in points:
            coords += [px(t), min(max(py(v), T - 2), B + 2)]
        c.create_line(*coords, fill=self.color, width=2)

        if self.hover is not None and L <= self.hover <= R:
            t, v = min(points, key=lambda p: abs(px(p[0]) - self.hover))
            x, y = px(t), py(v)
            c.create_line(x, T, x, B, fill="#999", dash=(2, 2))
            c.create_oval(x - 3, y - 3, x + 3, y + 3, outline=self.color, width=2)
            c.create_text(min(x + 8, R - 4), T + 4, anchor="ne" if x + 8 > R - 120 else "nw",
                          font=font, text="%.6g %s at %.4g %s" % (
                              v, unit, t / tfactor, self.tunit_var.get()))
        s = stats([v for _, v in points])
        self.stats_label.config(text="shown: min %.6g   mean %.6g   max %.6g   last %.6g %s"
                                "   (%d points)%s" % (s["min"], s["mean"], s["max"], s["last"],
                                                     unit, s["n"],
                                                     "   PAUSED" if self.paused else ""))
