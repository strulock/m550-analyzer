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

import bisect
import math
import tkinter as tk
from array import array
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


class History:
    """A channel's timed points, the last `maxlen` of them.

    Two arrays of doubles: 16 bytes a point, where a deque of (t, v) tuples
    takes about 120, and every watched channel keeps one for a whole drive.
    Iterating gives (t, v) pairs; times only grow (a new session clears it),
    so window() finds the visible part by bisection.
    """

    def __init__(self, maxlen):
        self.maxlen = maxlen
        self.t, self.v = array("d"), array("d")

    def append(self, point):
        self.t.append(point[0])
        self.v.append(point[1])
        if len(self.t) > 2 * self.maxlen:       # trim now and then, not every time
            del self.t[:-self.maxlen]
            del self.v[:-self.maxlen]

    def _start(self):
        return max(0, len(self.t) - self.maxlen)

    def __len__(self):
        return min(len(self.t), self.maxlen)

    def __iter__(self):
        s = self._start()
        return zip(self.t[s:], self.v[s:])

    def __getitem__(self, i):
        n = len(self)
        if i < 0:
            i += n
        if not 0 <= i < n:
            raise IndexError("history index out of range")
        s = self._start() + i
        return self.t[s], self.v[s]

    def clear(self):
        del self.t[:]
        del self.v[:]

    def last(self):
        return self[-1] if len(self) else None

    def window(self, seconds):
        """The (t, v) points inside the last `seconds` (None: all)."""
        s = self._start()
        if seconds is not None and len(self):
            s = max(s, bisect.bisect_left(self.t, self.t[-1] - seconds, s))
        return list(zip(self.t[s:], self.v[s:]))


def thin(points, x_of, columns):
    """At most about two points per pixel column - the lowest and highest,
    in time order - so a long history draws as fast as a short one and
    still shows every spike. `x_of(t)` is the point's x in pixels."""
    if len(points) <= 2 * columns:
        return points
    out, col, lo, hi = [], None, None, None
    for p in points:
        x = int(x_of(p[0]))
        if x != col:
            if col is not None:
                out += [lo, hi] if lo is not hi else [lo]
            col, lo, hi = x, p, p
        else:
            if p[1] < lo[1]:
                lo = p
            if p[1] > hi[1]:
                hi = p
    out += [lo, hi] if lo is not hi else [lo]
    out.sort(key=lambda p: p[0])
    return out


def nearest(points, t):
    """The point whose time is nearest `t`; points are in time order."""
    i = bisect.bisect_left(points, (t, -math.inf))
    near = points[max(0, i - 1):i + 1]
    return min(near, key=lambda p: abs(p[0] - t))


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


# What each unit becomes when the app is set to imperial. Units with no
# everyday imperial form (V, A, %, s...) stay as they are.
IMPERIAL = {"°C": "°F", "hPa": "psi", "mbar": "psi", "bar": "psi", "kPa": "psi",
            "MPa": "psi", "km/h": "mph", "km": "mi", "m": "ft", "cm": "in", "mm": "in",
            "kg/h": "lb/min", "Nm": "lb-ft"}


def display_unit(unit, system="metric"):
    """The unit a value in `unit` is shown in, for "metric" or "imperial"."""
    unit = canonical(unit)
    return IMPERIAL.get(unit, unit) if system == "imperial" else unit


# The window's own words, in the app's two languages.
TEXT = {
    "en": {"time": "Time", "unit": "Unit", "scale": "Scale", "auto": "Auto", "to": "to",
           "pause": "Pause", "resume": "Resume", "all": "All", "waiting": "waiting for data",
           "axis_t": "time (%s)", "axis_v": "value", "at": "%s %s at %s %s",
           "stats": "shown: min %s   mean %s   max %s   last %s %s   (%d points)",
           "paused": "PAUSED"},
    "de": {"time": "Zeit", "unit": "Einheit", "scale": "Skala", "auto": "Auto", "to": "bis",
           "pause": "Anhalten", "resume": "Weiter", "all": "Alle", "waiting": "warte auf Daten",
           "axis_t": "Zeit (%s)", "axis_v": "Wert", "at": "%s %s bei %s %s",
           "stats": "angezeigt: Min %s   Mittel %s   Max %s   zuletzt %s %s   (%d Punkte)",
           "paused": "ANGEHALTEN"},
}


def fmt(value, lang="en"):
    """A number for display: German uses a decimal comma."""
    text = "%.6g" % value
    return text.replace(".", ",") if lang == "de" else text


class GraphWindow(tk.Toplevel):
    """One channel, live, with its own axes."""

    REFRESH_MS = 250
    PAD_L, PAD_R, PAD_T, PAD_B = 64, 16, 12, 34

    def __init__(self, master, name, title, unit, history, color="#2d7dd2", *,
                 lang="en", show_unit=None, colors=None):
        super().__init__(master)
        self.name, self.unit, self.history, self.color = name, canonical(unit), history, color
        self.lang = lang if lang in TEXT else "en"
        self.colors = colors or {}
        self.geometry("820x460")
        self.minsize(520, 320)
        self.paused = None                  # the points frozen by Pause
        self.hover = None
        self._drawn = None                  # what the last draw showed, to skip repeats
        self._hover_job = None
        self.last_range = (0.0, 1.0)

        self.heading = ttk.Label(self, padding=(8, 6, 8, 0), font=("TkDefaultFont", 10, "bold"))
        self.heading.pack(fill="x")
        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(fill="x")
        self.time_label = ttk.Label(bar)
        self.time_label.pack(side="left")
        self.window_var = tk.StringVar()
        self.window_box = ttk.Combobox(bar, textvariable=self.window_var, width=7,
                                       state="readonly")
        self.window_box.pack(side="left", padx=(4, 2))
        self.tunit_var = tk.StringVar(value="s")
        ttk.Combobox(bar, textvariable=self.tunit_var, width=4, state="readonly",
                     values=list(TIME_UNITS)).pack(side="left", padx=(0, 12))

        self.unit_label = ttk.Label(bar)
        self.unit_label.pack(side="left")
        choices = units_for(self.unit)
        self.unit_var = tk.StringVar(value=show_unit if show_unit in choices else self.unit)
        ttk.Combobox(bar, textvariable=self.unit_var, width=7, state="readonly"
                     if len(choices) > 1 else "disabled",
                     values=choices).pack(side="left", padx=(4, 12))

        self.scale_label = ttk.Label(bar)
        self.scale_label.pack(side="left")
        self.auto_var = tk.BooleanVar(value=True)
        self.auto_check = ttk.Checkbutton(bar, variable=self.auto_var,
                                          command=self._auto_changed)
        self.auto_check.pack(side="left", padx=(4, 2))
        self.min_var, self.max_var = tk.StringVar(), tk.StringVar()
        self.min_entry = ttk.Entry(bar, textvariable=self.min_var, width=8, state="disabled")
        self.min_entry.pack(side="left")
        self.to_label = ttk.Label(bar)
        self.to_label.pack(side="left", padx=2)
        self.max_entry = ttk.Entry(bar, textvariable=self.max_var, width=8, state="disabled")
        self.max_entry.pack(side="left", padx=(0, 12))

        self.pause_btn = ttk.Button(bar, command=self._toggle_pause)
        self.pause_btn.pack(side="right")

        self.canvas = tk.Canvas(self, highlightthickness=0,
                                bg=self.colors.get("canvas", "#fbfbfb"))
        self.canvas.pack(fill="both", expand=True, padx=8)
        self.stats_label = ttk.Label(self, padding=(8, 4))
        self.stats_label.pack(fill="x")
        self.canvas.bind("<Motion>", self._motion)
        self.canvas.bind("<Leave>", lambda e: self._set_hover(None))
        self.set_language(self.lang, title)
        self._job = self.after(10, self._tick)
        self.protocol("WM_DELETE_WINDOW", self.close)

    def _t(self, key):
        return TEXT[self.lang][key]

    # -- what the app changes

    def set_language(self, lang, title=None):
        """Relabel everything in `lang`; `title` is the channel's description."""
        self.lang = lang if lang in TEXT else "en"
        if title is not None:
            self.title("%s  -  %s" % (title, self.name) if title and title != self.name
                       else self.name)
            self.heading.config(text=title or self.name)
        index = self.window_box.current() if self.window_var.get() else 2   # 2 min
        labels = [self._t("all") if seconds is None else label
                  for label, seconds in TIME_WINDOWS]
        self.window_box.configure(values=labels)
        self.window_box.current(index if index >= 0 else 2)
        self.time_label.config(text=self._t("time"))
        self.unit_label.config(text=self._t("unit"))
        self.scale_label.config(text=self._t("scale"))
        self.auto_check.config(text=self._t("auto"))
        self.to_label.config(text=self._t("to"))
        self.pause_btn.config(text=self._t("resume" if self.paused is not None else "pause"))
        self.draw()

    def set_colors(self, colors):
        self.colors = colors or {}
        self.canvas.configure(bg=self.colors.get("canvas", "#fbfbfb"))
        self.draw()

    def set_unit(self, unit):
        if unit in units_for(self.unit):
            self.unit_var.set(unit)
            self.draw()

    # -- controls

    def _auto_changed(self):
        state = "disabled" if self.auto_var.get() else "normal"
        if not self.auto_var.get() and not self.min_var.get():
            lo, hi = self.last_range
            self.min_var.set("%.4g" % lo)
            self.max_var.set("%.4g" % hi)
        self.min_entry.config(state=state)
        self.max_entry.config(state=state)

    def _toggle_pause(self):
        if self.paused is None:
            self.paused = list(self.history)
        else:
            self.paused = None
        self.pause_btn.config(text=self._t("resume" if self.paused is not None else "pause"))

    def _set_hover(self, x):
        # Mouse moves come far faster than a redraw: draw once they settle.
        self.hover = x
        if self._hover_job is None:
            self._hover_job = self.after_idle(self._hover_draw)

    def _hover_draw(self):
        self._hover_job = None
        self.draw()

    def _motion(self, event):
        self._set_hover(event.x)

    def close(self):
        for job in (self._job, self._hover_job):
            if job:
                self.after_cancel(job)
        self._job = self._hover_job = None
        self.destroy()

    def _tick(self):
        # Redraw only when something shown would change: a new point, a
        # setting, the window's size or the hover.
        if self._signature() != self._drawn:
            self.draw()
        self._job = self.after(self.REFRESH_MS, self._tick)

    def _signature(self):
        last = self.history.last() if hasattr(self.history, "last") else \
            (self.history[-1] if len(self.history) else None)
        return (last, len(self.history), self.paused is not None, self.settings(),
                self.canvas.winfo_width(), self.canvas.winfo_height(), self.hover)

    def _points(self, window):
        if self.paused is not None:
            return visible(self.paused, window)
        if hasattr(self.history, "window"):
            return self.history.window(window)
        return visible(list(self.history), window)

    # -- drawing

    def settings(self):
        """(window s, time unit factor, value unit, fixed range or None)."""
        index = self.window_box.current()
        window = TIME_WINDOWS[index][1] if index >= 0 else 120
        fixed = None
        if not self.auto_var.get():
            try:
                fixed = (float(self.min_var.get().replace(",", ".")),
                         float(self.max_var.get().replace(",", ".")))
            except ValueError:
                fixed = None
        return window, TIME_UNITS.get(self.tunit_var.get(), 1.0), self.unit_var.get(), fixed

    def draw(self):
        self._drawn = self._signature()
        c = self.canvas
        c.delete("all")
        col = self.colors.get
        w, h = max(c.winfo_width(), 200), max(c.winfo_height(), 120)
        L, R, T, B = self.PAD_L, w - self.PAD_R, self.PAD_T, h - self.PAD_B
        window, tfactor, unit, fixed = self.settings()
        try:
            conv = converter(self.unit, unit)
        except ValueError:
            conv, unit = (lambda v: v), self.unit
        points = [(t, conv(v)) for t, v in self._points(window)]
        self.stats_label.config(foreground=col("muted", "#555"))
        if len(points) < 2:
            c.create_text(w // 2, h // 2, text=self._t("waiting"), fill=col("muted", "#aaa"))
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
        axis = col("axis", "#666")
        for v in nice_ticks(lo, hi):
            if lo <= v <= hi:
                c.create_line(L, py(v), R, py(v), fill=col("grid", "#e6e6e6"))
                c.create_text(L - 6, py(v), anchor="e", text=fmt(v, self.lang), font=font,
                              fill=axis)
        for t in nice_ticks(t0 / tfactor, t1 / tfactor, 6):
            x = px(t * tfactor)
            if L <= x <= R:
                c.create_line(x, T, x, B, fill=col("grid2", "#efefef"))
                c.create_text(x, B + 4, anchor="n", text=fmt(t, self.lang), font=font,
                              fill=axis)
        c.create_rectangle(L, T, R, B, outline=col("frame", "#cfcfcf"))
        c.create_text((L + R) // 2, h - 4, anchor="s", font=font, fill=axis,
                      text=self._t("axis_t") % self.tunit_var.get())
        c.create_text(8, T, anchor="nw", font=font, fill=axis, text=unit or self._t("axis_v"))

        coords = []
        for t, v in thin(points, px, max(1, int(R - L))):
            coords += [px(t), min(max(py(v), T - 2), B + 2)]
        if len(coords) >= 4:
            c.create_line(*coords, fill=self.color, width=2)

        if self.hover is not None and L <= self.hover <= R:
            t, v = nearest(points, t0 + (self.hover - L) / (R - L) * (t1 - t0))
            x, y = px(t), py(v)
            c.create_line(x, T, x, B, fill=col("muted", "#999"), dash=(2, 2))
            c.create_oval(x - 3, y - 3, x + 3, y + 3, outline=self.color, width=2)
            c.create_text(min(x + 8, R - 4), T + 4, anchor="ne" if x + 8 > R - 160 else "nw",
                          font=font, fill=col("fg", "#1e1e1e"),
                          text=self._t("at") % (fmt(v, self.lang), unit,
                                                fmt(t / tfactor, self.lang),
                                                self.tunit_var.get()))
        s = stats([v for _, v in points])
        text = self._t("stats") % (fmt(s["min"], self.lang), fmt(s["mean"], self.lang),
                                   fmt(s["max"], self.lang), fmt(s["last"], self.lang),
                                   unit, s["n"])
        if self.paused is not None:
            text += "   " + self._t("paused")
        self.stats_label.config(text=text)
