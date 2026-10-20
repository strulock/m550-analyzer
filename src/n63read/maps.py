"""
n63read.maps - the route on a street map, coloured by a channel.

MapWindow draws OpenStreetMap tiles, the GPS track coloured by any watched
channel (speed, boost, uneven running...), and a marker that follows live
logging and replay; clicking the route during a replay jumps there.

Tiles are downloaded once and kept in %APPDATA%\\n63read\\tiles, so a route
driven before shows offline later; without them the route draws on a plain
background. Downloading can be switched off in Settings; a tile request
tells the tile server roughly where the map is. The projection, the
colouring and the cache have no Tk in them and are tested on their own.

OpenStreetMap's tile policy asks for an identifying User-Agent, caching,
modest use and the attribution shown in the corner; all done here.
"""

import math
import os
import queue
import threading
import time
import tkinter as tk
import urllib.request
from tkinter import ttk

from n63read import __version__, core

TILE = 256
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
ATTRIBUTION = "© OpenStreetMap contributors"
USER_AGENT = "BMW-Analyzer/%s (+https://github.com/strulock/m550-analyzer)" % __version__
MIN_ZOOM, MAX_ZOOM = 3, 19
MAX_AGE = 30 * 24 * 3600        # s before a cached tile is fetched again
RETRY_AFTER = 60.0              # s to leave tiles alone after a failed download
WORKERS = 2                     # downloads at once


def cache_dir():
    return os.path.join(os.path.dirname(core.availability_path()), "tiles")


# ------------------------------------------------------------ projection

def world_px(lat, lon, zoom):
    """Web Mercator: (x, y) in pixels of the whole world at `zoom`."""
    size = TILE * 2 ** zoom
    lat = max(-85.05112878, min(85.05112878, lat))
    s = math.sin(math.radians(lat))
    return ((lon + 180.0) / 360.0 * size,
            (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * size)


def lat_lon(x, y, zoom):
    size = TILE * 2 ** zoom
    lon = x / size * 360.0 - 180.0
    n = math.pi - 2 * math.pi * y / size
    return math.degrees(math.atan(math.sinh(n))), lon


def fit_zoom(points, width, height, pad=40):
    """The closest zoom showing every (lat, lon) in a width x height view."""
    if not points:
        return 15
    lats, lons = [p[0] for p in points], [p[1] for p in points]
    for zoom in range(MAX_ZOOM - 2, MIN_ZOOM - 1, -1):     # street level at most
        x0, y0 = world_px(max(lats), min(lons), zoom)
        x1, y1 = world_px(min(lats), max(lons), zoom)
        if x1 - x0 <= width - 2 * pad and y1 - y0 <= height - 2 * pad:
            return zoom
    return MIN_ZOOM


def tiles_for(cx, cy, width, height, zoom):
    """(x, y) of the tiles covering a view centred on world pixel (cx, cy)."""
    n = 2 ** zoom
    x0, x1 = int((cx - width / 2) // TILE), int((cx + width / 2) // TILE)
    y0, y1 = int((cy - height / 2) // TILE), int((cy + height / 2) // TILE)
    return [(x % n, y) for y in range(max(0, y0), min(n - 1, y1) + 1)
            for x in range(x0, x1 + 1)]


# ------------------------------------------------------------ colours

RAMP = [(0.0, (44, 123, 182)), (0.25, (0, 166, 202)), (0.5, (120, 198, 121)),
        (0.75, (253, 174, 97)), (1.0, (215, 25, 28))]    # blue - green - red


def ramp(frac):
    """A colour for 0..1: blue for low, red for high."""
    frac = 0.0 if frac is None or frac != frac else max(0.0, min(1.0, frac))
    for (f0, c0), (f1, c1) in zip(RAMP, RAMP[1:]):
        if frac <= f1:
            k = (frac - f0) / (f1 - f0) if f1 > f0 else 0.0
            return "#%02x%02x%02x" % tuple(round(a + (b - a) * k) for a, b in zip(c0, c1))
    return "#%02x%02x%02x" % RAMP[-1][1]


def value_at(points, t):
    """The value of a channel's (t, v) points at time t: the latest at or
    before it, else the first."""
    if not points:
        return None
    lo, hi = 0, len(points)
    while lo < hi:
        mid = (lo + hi) // 2
        if points[mid][0] <= t:
            lo = mid + 1
        else:
            hi = mid
    return points[max(0, lo - 1)][1]


# ------------------------------------------------------------ tile cache

def download(z, x, y):
    """One tile from the tile server, identifying the app as its policy asks."""
    req = urllib.request.Request(TILE_URL.format(z=z, x=x, y=y),
                                 headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = r.read(1 << 20)
    if not data.startswith(b"\x89PNG"):
        raise ValueError("not a PNG tile")
    return data


class TileCache:
    """Tiles on disk, downloaded on demand in the background.

    get() returns the path of a cached tile (or None) and queues a download
    when it's missing or old. ready() gives the tiles that arrived since
    the last call, so the window redraws. Nothing is fetched if `fetch` is
    False, and after a failure nothing for a minute (no internet in the car).
    """

    def __init__(self, folder=None, fetch=True, opener=None):
        self.folder = folder or cache_dir()
        self.fetch = fetch
        self.opener = opener            # None: download from the tile server
        self.wanted = queue.Queue()
        self.arrived = queue.Queue()
        self.pending = set()
        self.lock = threading.Lock()
        self.failed_at = 0.0
        self.workers = []

    def path(self, z, x, y):
        return os.path.join(self.folder, str(z), str(x), "%d.png" % y)

    def get(self, z, x, y):
        path = self.path(z, x, y)
        try:
            age = time.time() - os.path.getmtime(path)
        except OSError:
            age = None
        if (age is None or age > MAX_AGE) and self.fetch:
            self._want((z, x, y))
        return path if age is not None else None

    def _want(self, key):
        if time.time() - self.failed_at < RETRY_AFTER:
            return
        with self.lock:
            if key in self.pending:
                return
            self.pending.add(key)
        self.wanted.put(key)
        if len(self.workers) < WORKERS:
            t = threading.Thread(target=self._work, daemon=True)
            self.workers.append(t)
            t.start()

    def _work(self):
        while True:
            key = self.wanted.get()
            try:
                data = (self.opener or self._download)(*key)
                path = self.path(*key)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = path + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, path)
                self.arrived.put(key)
            except Exception:                                       # noqa: BLE001
                self.failed_at = time.time()        # offline: rest a while
            finally:
                with self.lock:
                    self.pending.discard(key)

    @staticmethod
    def _download(z, x, y):
        return download(z, x, y)

    def ready(self):
        out = []
        while True:
            try:
                out.append(self.arrived.get_nowait())
            except queue.Empty:
                return out


# ------------------------------------------------------------ the window

TEXT = {
    "en": {"title": "Map", "colour": "Colour by", "follow": "Follow", "fit": "Fit route",
           "waiting": "Waiting for a GPS position.\n\nWatch HU_STATUS_GPS_POSITION.LATITUDE "
                      "and .LONGITUDE (the Map button offers to add them),\nor open a log "
                      "that has them.",
           "nofix": "no GPS fix in this data yet", "click": "click the route to jump there",
           "km": "%.1f km"},
    "de": {"title": "Karte", "colour": "Farbe nach", "follow": "Folgen", "fit": "Ganze Route",
           "waiting": "Warte auf eine GPS-Position.\n\nHU_STATUS_GPS_POSITION.LATITUDE und "
                      ".LONGITUDE beobachten (die Karte bietet an, sie hinzuzufügen),\noder ein "
                      "Log mit Position öffnen.",
           "nofix": "noch keine GPS-Position in diesen Daten",
           "click": "Route anklicken, um dorthin zu springen", "km": "%.1f km"},
}


class MapWindow(tk.Toplevel):
    """The route on a map. `source` supplies the data (see gui._MapSource):

    - track(): [(t, lat, lon)] with real positions, in time order
    - channels(): [(name, label)] that can colour the route
    - values(name): [(t, v)] of a channel, in time order
    - shown(name, v): (text, unit) of a value as the app shows it
    - now(): time of the marker, or None
    - seek(t) or None: jump a replay to time t
    - default(): the channel to colour by at first
    """

    REFRESH_MS = 400

    def __init__(self, master, source, *, tiles=None, lang="en", colors=None):
        super().__init__(master)
        self.source, self.lang = source, lang if lang in TEXT else "en"
        self.tiles = tiles if tiles is not None else TileCache()
        self.colors = colors or {}
        self.title(self._t("title"))
        self.geometry("900x640")
        self.minsize(480, 360)
        self.zoom, self.center = 15, None           # center: world pixel at self.zoom
        self.images = {}                            # (z, x, y) -> PhotoImage
        self._drawn, self._job, self._drag = None, None, None
        self._screen = []                           # [(x, y, t)] route points drawn
        self.hover = None

        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(fill="x")
        ttk.Label(bar, text=self._t("colour")).pack(side="left")
        self.channel_var = tk.StringVar()
        self.channel_box = ttk.Combobox(bar, textvariable=self.channel_var, width=44,
                                        state="readonly")
        self.channel_box.pack(side="left", padx=(4, 12))
        self.channel_box.bind("<<ComboboxSelected>>", lambda e: self.draw())
        self.follow_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text=self._t("follow"), variable=self.follow_var,
                        command=self.draw).pack(side="left")
        ttk.Button(bar, text=self._t("fit"), command=self.fit).pack(side="left", padx=8)
        ttk.Button(bar, text="−", width=3, command=lambda: self.zoom_by(-1)).pack(
            side="left")
        ttk.Button(bar, text="+", width=3, command=lambda: self.zoom_by(1)).pack(side="left")

        self.canvas = tk.Canvas(self, highlightthickness=0,
                                bg=self.colors.get("canvas", "#eef0f2"))
        self.canvas.pack(fill="both", expand=True)
        self.info = ttk.Label(self, padding=(8, 4))
        self.info.pack(fill="x")
        c = self.canvas
        c.bind("<ButtonPress-1>", self._press)
        c.bind("<B1-Motion>", self._move)
        c.bind("<ButtonRelease-1>", self._release)
        c.bind("<MouseWheel>", lambda e: self.zoom_by(1 if e.delta > 0 else -1, e.x, e.y))
        c.bind("<Button-4>", lambda e: self.zoom_by(1, e.x, e.y))
        c.bind("<Button-5>", lambda e: self.zoom_by(-1, e.x, e.y))
        c.bind("<Motion>", lambda e: self._set_hover((e.x, e.y)))
        c.bind("<Leave>", lambda e: self._set_hover(None))
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh_channels()
        self._job = self.after(50, self._tick)

    def _t(self, key):
        return TEXT[self.lang][key]

    # -- what the app changes

    def refresh_channels(self):
        chans = self.source.channels()
        self._names = [n for n, _ in chans]
        self.channel_box.configure(values=["%s  (%s)" % (label, n) if label != n else n
                                           for n, label in chans])
        current = self.channel()
        want = current if current in self._names else self.source.default()
        if want in self._names:
            self.channel_box.current(self._names.index(want))
        elif self._names:
            self.channel_box.current(0)

    def channel(self):
        i = self.channel_box.current()
        return self._names[i] if 0 <= i < len(getattr(self, "_names", [])) else None

    def set_colors(self, colors):
        self.colors = colors or {}
        self.canvas.configure(bg=self.colors.get("canvas", "#eef0f2"))
        self.draw()

    def close(self):
        if self._job:
            self.after_cancel(self._job)
            self._job = None
        self.destroy()

    # -- view

    def size(self):
        return max(self.canvas.winfo_width(), 200), max(self.canvas.winfo_height(), 150)

    def fit(self):
        track = self.source.track()
        if not track:
            return
        w, h = self.size()
        self.zoom = fit_zoom([(a, o) for _, a, o in track], w, h)
        xs, ys = zip(*(world_px(a, o, self.zoom) for _, a, o in track))
        self.center = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
        self.draw()

    def zoom_by(self, step, x=None, y=None):
        new = max(MIN_ZOOM, min(MAX_ZOOM, self.zoom + step))
        if new == self.zoom or self.center is None:
            return
        w, h = self.size()
        x = w / 2 if x is None else x
        y = h / 2 if y is None else y
        # Keep the point under the cursor where it is.
        wx, wy = self.center[0] + x - w / 2, self.center[1] + y - h / 2
        k = 2 ** (new - self.zoom)
        self.center = (wx * k - (x - w / 2), wy * k - (y - h / 2))
        self.zoom = new
        self.draw()

    def _press(self, e):
        self._drag = (e.x, e.y, e.x, e.y)

    def _move(self, e):
        if self._drag is None or self.center is None:
            return
        x0, y0, lx, ly = self._drag
        self.center = (self.center[0] - (e.x - lx), self.center[1] - (e.y - ly))
        self._drag = (x0, y0, e.x, e.y)
        if abs(e.x - x0) + abs(e.y - y0) > 4:
            self.follow_var.set(False)          # looking around: stop following
        self.draw()

    def _release(self, e):
        if self._drag is None:
            return
        x0, y0, _, _ = self._drag
        self._drag = None
        if abs(e.x - x0) + abs(e.y - y0) <= 4:
            self.click(e.x, e.y)

    def click(self, x, y):
        """A click on the route jumps the replay to that moment."""
        hit = self.near(x, y)
        if hit is not None and self.source.seek is not None:
            self.source.seek(hit)

    def near(self, x, y, radius=12):
        best, best_d = None, radius * radius
        for px, py, t in self._screen:
            d = (px - x) ** 2 + (py - y) ** 2
            if d <= best_d:
                best, best_d = t, d
        return best

    def _set_hover(self, xy):
        self.hover = xy

    # -- drawing

    def _tick(self):
        arrived = self.tiles.ready()
        if arrived:
            for key in arrived:
                self.images.pop(key, None)      # a fresher copy came
        if arrived or self._signature() != self._drawn:
            self.draw()
        self._job = self.after(self.REFRESH_MS, self._tick)

    def _signature(self):
        track = self.source.track()
        return (len(track), track[-1] if track else None, self.source.now(), self.channel(),
                self.zoom, self.center, self.size(), self.follow_var.get(), self.hover)

    def _image(self, z, x, y):
        key = (z, x, y)
        if key not in self.images:
            path = self.tiles.get(z, x, y)
            if path is None:
                return None
            try:
                self.images[key] = tk.PhotoImage(file=path)
            except tk.TclError:
                return None                         # a broken file: fetched again later
            if len(self.images) > 300:              # the oldest go; they're on disk
                for k in list(self.images)[:100]:
                    del self.images[k]
        return self.images[key]

    def draw(self):
        self._drawn = self._signature()
        c = self.canvas
        c.delete("all")
        col = self.colors.get
        w, h = self.size()
        track = self.source.track()
        if not track:
            c.create_text(w // 2, h // 2, text=self._t("waiting"), justify="center",
                          fill=col("muted", "#777"))
            self.info.config(text="")
            self._screen = []
            return
        if self.center is None:
            self.fit()
            return
        now = self.source.now()
        marker = None
        if now is not None:
            marker = min(track, key=lambda p: abs(p[0] - now))
        if self.follow_var.get() and marker is not None:
            mx, my = world_px(marker[1], marker[2], self.zoom)
            # Recentre when the marker gets near the edge.
            if abs(mx - self.center[0]) > w * 0.35 or abs(my - self.center[1]) > h * 0.35:
                self.center = (mx, my)
                self._drawn = self._signature()
        cx, cy = self.center
        ox, oy = w / 2 - cx, h / 2 - cy

        for x, y in tiles_for(cx, cy, w, h, self.zoom):
            img = self._image(self.zoom, x, y)
            if img is not None:
                n = 2 ** self.zoom
                # The tile may wrap round the date line; draw it where it is.
                tx = x * TILE
                while tx + ox < -TILE:
                    tx += n * TILE
                c.create_image(tx + ox, y * TILE + oy, image=img, anchor="nw")

        name = self.channel()
        points = self.source.values(name) if name else []
        values = [v for _, v in points]
        lo, hi = (min(values), max(values)) if values else (0.0, 1.0)
        span = hi - lo if hi > lo else 1.0
        screen, last = [], None
        for t, a, o in track:
            x, y = world_px(a, o, self.zoom)
            x, y = x + ox, y + oy
            if last is not None and (x - last[0]) ** 2 + (y - last[1]) ** 2 < 4:
                continue                    # under 2 px from the last: skip
            screen.append((x, y, t))
            last = (x, y)
        self._screen = screen
        if len(screen) >= 2:
            # A dark edge under the colours, so the route stands out from roads
            # drawn in the same colours.
            c.create_line(*[v for x, y, _ in screen for v in (x, y)], fill="#1e1e1e",
                          width=7, capstyle="round", joinstyle="round")
        for (x0, y0, t), (x, y, _) in zip(screen, screen[1:]):
            v = value_at(points, t) if points else None
            colour = ramp((v - lo) / span) if v is not None else col("accent", "#2d7dd2")
            c.create_line(x0, y0, x, y, fill=colour, width=4, capstyle="round")
        if marker is not None:
            x, y = world_px(marker[1], marker[2], self.zoom)
            x, y = x + ox, y + oy
            c.create_oval(x - 7, y - 7, x + 7, y + 7, fill="#ffffff", outline="#1e1e1e",
                          width=2)

        # Legend, attribution, readout.
        font = ("TkDefaultFont", 8)
        if values:
            lx, ly = 12, h - 34
            for i in range(120):
                c.create_line(lx + i, ly, lx + i, ly + 10, fill=ramp(i / 119))
            c.create_rectangle(lx - 1, ly - 1, lx + 120, ly + 11, outline="#555")
            low, unit = self.source.shown(name, lo)
            high, _ = self.source.shown(name, hi)
            c.create_text(lx, ly + 13, anchor="nw", text=low, font=font, fill="#1e1e1e")
            c.create_text(lx + 120, ly + 13, anchor="ne", text="%s %s" % (high, unit),
                          font=font, fill="#1e1e1e")
        c.create_text(w - 4, h - 2, anchor="se", text=ATTRIBUTION, font=font, fill="#333")

        text = self._t("km") % _length(track)
        at = None
        if self.hover is not None:
            at = self.near(*self.hover)
        if at is None and marker is not None:
            at = marker[0]
        if at is not None and name:
            v = value_at(points, at)
            if v is not None:
                shown, unit = self.source.shown(name, v)
                text += "   %s %s at %s" % (shown, unit, _clock(at))
        if self.source.seek is not None:
            text += "   (%s)" % self._t("click")
        self.info.config(text=text)


def _length(track):
    from n63read.history import haversine_km
    return sum(haversine_km(a1, o1, a2, o2)
               for (_, a1, o1), (_, a2, o2) in zip(track, track[1:]))


def _clock(seconds):
    seconds = int(seconds)
    return "%d:%02d:%02d" % (seconds // 3600, seconds // 60 % 60, seconds % 60) \
        if seconds >= 3600 else "%d:%02d" % (seconds // 60, seconds % 60)
