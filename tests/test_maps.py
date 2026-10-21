"""maps: projection, colours, the tile cache and the map window."""

import os
import time

import pytest

from n63read import graph, maps, playback

LAT0, LON0 = 30.1346, -97.6359          # Circuit of the Americas


def test_projection_round_trips():
    for zoom in (3, 12, 17):
        x, y = maps.world_px(LAT0, LON0, zoom)
        lat, lon = maps.lat_lon(x, y, zoom)
        assert lat == pytest.approx(LAT0, abs=1e-9) and lon == pytest.approx(LON0, abs=1e-9)


def test_the_world_is_256_pixels_at_zoom_0():
    assert maps.world_px(0, 0, 0) == pytest.approx((128, 128))
    assert maps.world_px(0, 180, 0)[0] == pytest.approx(256)


def test_fit_zoom_shows_the_whole_route():
    route = [(LAT0, LON0), (LAT0 + 0.02, LON0 + 0.03)]
    zoom = maps.fit_zoom(route, 800, 600)
    (x0, y0), (x1, y1) = (maps.world_px(LAT0 + 0.02, LON0, zoom),
                          maps.world_px(LAT0, LON0 + 0.03, zoom))
    assert x1 - x0 <= 720 and y1 - y0 <= 520
    bigger = maps.world_px(LAT0, LON0 + 0.03, zoom + 1)[0] - maps.world_px(LAT0, LON0,
                                                                            zoom + 1)[0]
    assert bigger > 720 or maps.world_px(LAT0, LON0, zoom + 1)[1] - \
        maps.world_px(LAT0 + 0.02, LON0, zoom + 1)[1] > 520     # one closer wouldn't fit
    assert maps.fit_zoom([(LAT0, LON0)], 800, 600) == maps.MAX_ZOOM - 2   # one point


def test_tiles_cover_the_view():
    cx, cy = maps.world_px(LAT0, LON0, 15)
    tiles = maps.tiles_for(cx, cy, 800, 600, 15)
    xs, ys = {x for x, _ in tiles}, {y for _, y in tiles}
    assert len(xs) in (4, 5) and len(ys) in (3, 4)
    assert (int(cx // 256), int(cy // 256)) in tiles


def test_ramp_runs_blue_to_red():
    assert maps.ramp(0) == "#2c7bb6" and maps.ramp(1) == "#d7191c"
    assert maps.ramp(-5) == maps.ramp(0) and maps.ramp(None) == maps.ramp(0)
    assert maps.ramp(0.5) == "#78c679"


def test_value_at_takes_the_latest_reading():
    points = [(0, 1.0), (2, 2.0), (4, 3.0)]
    assert maps.value_at(points, 3) == 2.0 and maps.value_at(points, -1) == 1.0
    assert maps.value_at(points, 9) == 3.0 and maps.value_at([], 1) is None


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_the_cache_downloads_once_and_keeps_the_tile(tmp_path):
    calls = []
    cache = maps.TileCache(str(tmp_path), opener=lambda z, x, y: calls.append((z, x, y))
                           or b"\x89PNG fake")
    assert cache.get(15, 1, 2) is None                  # not there yet: fetched
    assert wait_for(lambda: cache.ready() == [(15, 1, 2)] or os.path.exists(
        cache.path(15, 1, 2)))
    assert cache.get(15, 1, 2) == cache.path(15, 1, 2)
    assert calls == [(15, 1, 2)]


def test_no_downloads_when_switched_off(tmp_path):
    cache = maps.TileCache(str(tmp_path), fetch=False, opener=lambda *a: pytest.fail("fetched"))
    assert cache.get(15, 1, 2) is None
    time.sleep(0.05)


def test_offline_it_waits_before_trying_again(tmp_path):
    calls = []

    def fail(*a):
        calls.append(a)
        raise OSError("no internet")
    cache = maps.TileCache(str(tmp_path), opener=fail)
    cache.get(15, 1, 2)
    assert wait_for(lambda: calls and cache.failed_at)
    cache.get(15, 1, 3)
    time.sleep(0.1)
    assert len(calls) == 1


def test_the_real_download_identifies_itself(monkeypatch):
    seen = {}

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return b"\x89PNG..."

    def urlopen(req, timeout):
        seen["ua"], seen["url"] = req.get_header("User-agent"), req.full_url
        return Reply()
    monkeypatch.setattr(maps.urllib.request, "urlopen", urlopen)
    assert maps.download(15, 7, 9).startswith(b"\x89PNG")
    assert seen["url"] == "https://tile.openstreetmap.org/15/7/9.png"
    assert seen["ua"].startswith("BMW-Analyzer/")


# ------------------------------------------------------------ the window

class Source:
    def __init__(self, track, values, now=None, seekable=False, limits=None):
        self._track, self._values, self._now = track, values, now
        self._limits = limits
        self.seeks = []
        self.seek = self.seeks.append if seekable else None

    def track(self):
        return self._track

    def channels(self):
        return [("SPEED", "Speed")]

    def values(self, name):
        return self._values

    def shown(self, name, v):
        return "%.4g" % v, "km/h"

    def now(self):
        return self._now

    def default(self):
        return "SPEED"

    def limits(self, name):
        return self._limits


def route(n=40):
    return [(float(t), LAT0 + t * 1e-4, LON0 + t * 1e-4) for t in range(n)]


def test_the_window_draws_the_route_coloured_and_the_marker(app, tmp_path):
    tiles = maps.TileCache(str(tmp_path), fetch=False)
    win = maps.MapWindow(app, Source(route(), [(t, t * 2.0) for t in range(40)], now=20.0),
                         tiles=tiles)
    try:
        win.update()
        win.draw()
        lines = [i for i in win.canvas.find_all() if win.canvas.type(i) == "line"]
        colours = {win.canvas.itemcget(i, "fill") for i in lines}
        assert len(lines) > 10 and len(colours) > 3      # coloured along the way
        assert any(win.canvas.type(i) == "oval" for i in win.canvas.find_all())
        assert "km" in win.info.cget("text") and "km/h" in win.info.cget("text")
        assert maps.ATTRIBUTION in [win.canvas.itemcget(i, "text")
                                    for i in win.canvas.find_all()
                                    if win.canvas.type(i) == "text"]
    finally:
        win.close()


def test_a_click_on_the_route_jumps_the_replay(app, tmp_path):
    src = Source(route(), [], seekable=True)
    win = maps.MapWindow(app, src, tiles=maps.TileCache(str(tmp_path), fetch=False))
    try:
        win.update()
        win.draw()
        x, y, t = win._screen[len(win._screen) // 2]
        win.click(x + 3, y)
        assert src.seeks == [t]
        win.click(5, 5)                                  # nowhere near: nothing
        assert len(src.seeks) == 1
    finally:
        win.close()


def test_no_position_yet_says_what_to_watch(app, tmp_path):
    win = maps.MapWindow(app, Source([], []), tiles=maps.TileCache(str(tmp_path), fetch=False))
    try:
        win.update()
        win.draw()
        texts = [win.canvas.itemcget(i, "text") for i in win.canvas.find_all()]
        assert any("HU_STATUS_GPS_POSITION" in t for t in texts)
    finally:
        win.close()


def test_a_cached_tile_is_drawn(app, tmp_path):
    import tkinter as tk
    cache = maps.TileCache(str(tmp_path), fetch=False)
    cx, cy = maps.world_px(LAT0, LON0, 15)
    x, y = int(cx // 256), int(cy // 256)
    os.makedirs(os.path.dirname(cache.path(15, x, y)))
    img = tk.PhotoImage(width=256, height=256)
    img.put("#88cc88", to=(0, 0, 256, 256))
    img.write(cache.path(15, x, y), format="png")
    win = maps.MapWindow(app, Source([(0.0, LAT0, LON0)], []), tiles=cache)
    try:
        win.update()
        win.zoom, win.center = 15, (cx, cy)
        win.draw()
        assert any(win.canvas.type(i) == "image" for i in win.canvas.find_all())
    finally:
        win.close()


def test_zoom_keeps_the_point_under_the_cursor(app, tmp_path):
    win = maps.MapWindow(app, Source(route(), []), tiles=maps.TileCache(str(tmp_path),
                                                                        fetch=False))
    try:
        win.update()
        win.zoom, win.center = 14, maps.world_px(LAT0, LON0, 14)
        w, h = win.size()
        before = maps.lat_lon(win.center[0] + 100 - w / 2, win.center[1] + 50 - h / 2, 14)
        win.zoom_by(1, 100, 50)
        after = maps.lat_lon(win.center[0] + 100 - w / 2, win.center[1] + 50 - h / 2, 15)
        assert after == pytest.approx(before, abs=1e-9)
    finally:
        win.close()


# ------------------------------------------------------------ in the app

def test_the_app_maps_the_demo_drive(app):
    from conftest import pump
    app._start_demo()
    names = [n for n, _, _ in app.watch]
    assert "HU_STATUS_GPS_POSITION.LATITUDE" in names           # the demo has GPS
    assert pump(app, lambda: len(app.history["HU_STATUS_GPS_POSITION.LATITUDE"]) > 3)
    app._open_map()
    try:
        win = app.map_window
        assert win.source.track() and win.channel() == "FZG_GESCHWINDIGKEIT_VA_HA_FASCHWERP"
        assert win.source.now() is not None and win.source.seek is None   # live: no seek
        shown, unit = win.source.shown("ITOEL", 100.0)
        assert unit in ("°C", "°F")
    finally:
        app.map_window.close()


def test_the_app_maps_a_replayed_log_whole(app, tmp_path):
    from conftest import pump
    path = str(tmp_path / "drive.csv")
    with open(path, "w", encoding="utf-8") as f:
        f.write("Time,FZG_GESCHWINDIGKEIT_VA_HA_FASCHWERP,HU_STATUS_GPS_POSITION.LATITUDE,"
                "HU_STATUS_GPS_POSITION.LONGITUDE\n")
        for t in range(30):
            f.write("%d,%d,%.6f,%.6f\n" % (t, 50 + t, LAT0 + t * 1e-4, LON0))
    names, rows = playback.read_log(path)
    app._replay_loaded(path, (names, rows))
    try:
        assert pump(app, lambda: len(app.history[names[0]]) >= 1)
        app._open_map()
        src = app.map_window.source
        assert len(src.track()) == 30                    # the whole route, not so far
        assert src.seek is not None
        src.seek(12.0)
        assert app.feed.position() == pytest.approx(12.0, abs=1.0) \
            if hasattr(app.feed, "position") else True
    finally:
        app.map_window.close()
        app._stop_feed()


def test_converting_for_the_legend_uses_the_apps_units(app):
    src = __import__("n63read.gui", fromlist=["_MapSource"])._MapSource(app)
    app.units = "imperial"
    try:
        shown, unit = src.shown("FZG_GESCHWINDIGKEIT_VA_HA_FASCHWERP", 100.0)
        assert unit == "mph" and float(shown) == pytest.approx(
            graph.converter("km/h", "mph")(100.0), rel=1e-3)
    finally:
        app.units = "metric"


def test_limit_colours():
    assert maps.limit_colour(5, (10, 20)) == maps.LIMIT_COLOURS["low"]
    assert maps.limit_colour(15, (10, 20)) == maps.LIMIT_COLOURS["ok"]
    assert maps.limit_colour(25, (None, 20)) == maps.LIMIT_COLOURS["high"]
    assert maps.limit_colour(-5, (None, 20)) == maps.LIMIT_COLOURS["ok"]


def test_the_route_can_show_where_a_value_left_its_alert_limits(app, tmp_path):
    values = [(t, float(t)) for t in range(40)]           # 0 ... 39
    src = Source(route(), values, limits=(10.0, 30.0))
    win = maps.MapWindow(app, src, tiles=maps.TileCache(str(tmp_path), fetch=False))
    try:
        win.update()
        assert str(win.limits_check.cget("state")) == "normal"
        win.limits_var.set(True)
        win.draw()
        colours = {win.canvas.itemcget(i, "fill") for i in win.canvas.find_all()
                   if win.canvas.type(i) == "line"}
        assert set(maps.LIMIT_COLOURS.values()) <= colours       # below, within, above
        texts = [win.canvas.itemcget(i, "text") for i in win.canvas.find_all()
                 if win.canvas.type(i) == "text"]
        assert any(t.startswith("below 10") for t in texts)
    finally:
        win.close()
    plain = maps.MapWindow(app, Source(route(), values),
                           tiles=maps.TileCache(str(tmp_path), fetch=False))
    try:
        plain.update()
        assert str(plain.limits_check.cget("state")) == "disabled"   # no limits set
    finally:
        plain.close()
