"""Logging rate, memory and redraw cost: what keeps a long drive fast."""

import pytest

from n63read import core, graph, mcp, playback

from conftest import pump


# ------------------------------------------------------------- DME reads

class Car:
    """read_did() as a car answers it: values, refusals, or silence."""

    def __init__(self, answers):
        self.answers = answers              # (target, did) -> bytes | int (NRC) | None
        self.asked = []

    def read_did(self, target, did, timeout=None):
        self.asked.append((target, did, timeout))
        a = self.answers.get((target, did))
        if a is None:
            raise core.HsfzTimeout("timed out waiting for response")
        if isinstance(a, int):
            raise core.NegativeResponse(0x22, a)
        return a


def chan(name, did, target=None):
    meta = {"ARG": name, "ID": "0x%04X" % did, "DATENTYP": "unsigned char",
            "MUL": "1", "DIV": "1", "ADD": "0", "EINHEIT": ""}
    if target is not None:
        meta["TARGET"] = "0x%02X" % target
    return (name, did, meta)


def test_a_silent_identifier_is_rested_and_the_others_carry_on():
    car = Car({(0x12, 0x0001): b"\x05", (0x12, 0x0003): b"\x07"})
    r = core.ChannelReader({}, 0x12)
    chans = [chan("A", 0x0001), chan("B", 0x0002), chan("C", 0x0003)]
    got = r.read(car, chans, now=100.0)
    assert got["A"][0] == 5 and got["B"] == (None, "", "not answering")
    # C came after the silent B in the same cycle: skipped this once.
    assert got["C"] == (None, "", "not answering")
    car.asked.clear()
    got = r.read(car, chans, now=101.0)
    assert [a[1] for a in car.asked] == [0x0001, 0x0003]  # B isn't asked again yet
    assert got["C"][0] == 7
    assert all(a[2] == core.VALUE_TIMEOUT for a in car.asked)
    car.asked.clear()
    r.read(car, chans, now=100.0 + core.VALUE_KEY_BACKOFF + 1)
    assert (0x12, 0x0002, core.VALUE_TIMEOUT) in car.asked  # tried again later


def test_a_sleeping_bank2_dme_costs_one_timeout_not_one_per_channel():
    car = Car({(0x12, 0x0001): b"\x05"})
    r = core.ChannelReader({}, 0x12)
    chans = [chan("A", 0x0001)] + [chan("B%d" % i, 0x10 + i, core.DME_SLAVE) for i in range(20)]
    r.read(car, chans, now=100.0)
    assert sum(1 for t, _, _ in car.asked if t == core.DME_SLAVE) == 1
    car.asked.clear()
    r.read(car, chans, now=102.0)                        # 0x13 still resting
    assert [t for t, _, _ in car.asked] == [0x12]
    car.asked.clear()
    r.read(car, chans, now=100.0 + core.VALUE_TARGET_BACKOFF + 1)
    # Back to 0x13, past the one identifier that timed out.
    assert (core.DME_SLAVE, 0x11, core.VALUE_TIMEOUT) in car.asked
    assert (core.DME_SLAVE, 0x10, core.VALUE_TIMEOUT) not in car.asked


def test_a_refused_identifier_is_not_asked_again():
    car = Car({(0x12, 0x0001): 0x31, (0x12, 0x0002): 0x22})
    r = core.ChannelReader({}, 0x12)
    chans = [chan("A", 0x0001), chan("B", 0x0002)]
    got = r.read(car, chans, now=1.0)
    assert got["A"][2] == "requestOutOfRange"
    car.asked.clear()
    got = r.read(car, chans, now=2.0)
    assert [a[1] for a in car.asked] == [0x0002]           # conditions not correct: retried
    assert got["A"][2] == "requestOutOfRange"
    assert r.refused[(0x12, 0x0001)] == "requestOutOfRange"


def test_the_interval_counts_the_time_the_reads_took():
    assert core.rest(0.25, started=10.0, now=10.1) == pytest.approx(0.15)
    assert core.rest(0.25, started=10.0, now=10.4) == 0.0   # a slow cycle: straight on


# --------------------------------------------------------------- history

def test_history_keeps_the_last_points_compactly():
    h = graph.History(5)
    for i in range(23):
        h.append((float(i), i * 10.0))
    assert len(h) == 5 and list(h) == [(float(i), i * 10.0) for i in range(18, 23)]
    assert h[0] == (18.0, 180.0) and h[-1] == (22.0, 220.0) and h.last() == (22.0, 220.0)
    assert len(h.t) <= 10                                 # trimmed as it goes
    with pytest.raises(IndexError):
        h[5]
    assert h.window(2) == [(20.0, 200.0), (21.0, 210.0), (22.0, 220.0)]
    assert h.window(None) == list(h)
    h.clear()
    assert not h and h.last() is None and h.window(10) == []


def test_thinning_keeps_every_spike():
    points = [(i / 100, 1.0) for i in range(10000)]
    points[5000] = (50.0, 99.0)
    points[7000] = (70.0, -99.0)
    thin = graph.thin(points, lambda t: t * 4, 400)        # 400 px columns
    assert len(thin) <= 2 * 400 + 2
    assert (50.0, 99.0) in thin and (70.0, -99.0) in thin
    assert thin == sorted(thin)
    assert graph.thin(points[:10], lambda t: t, 400) == points[:10]


def test_nearest_point_by_time():
    points = [(0.0, 1.0), (1.0, 2.0), (2.0, 3.0)]
    assert graph.nearest(points, 1.4) == (1.0, 2.0)
    assert graph.nearest(points, 1.6) == (2.0, 3.0)
    assert graph.nearest(points, -5) == (0.0, 1.0) and graph.nearest(points, 9) == (2.0, 3.0)


# ---------------------------------------------------------------- replay

def test_replay_rows_read_like_dicts(tmp_path):
    path = tmp_path / "log.csv"
    path.write_text("Time,A,B\n0.0,1,x\n0.5,2\n", encoding="utf-8")
    names, rows = playback.read_log(str(path))
    assert rows[0] == (0.0, {"A": 1.0, "B": "x"})
    assert rows[1] == (0.5, {"A": 2.0})                     # a short row lacks B
    assert rows[1][1].get("B") is None and "B" not in rows[1][1]


# ------------------------------------------------------------------- GUI

def test_waiting_samples_draw_the_chart_once(app, monkeypatch):
    app._set_watch(["ITKUA"])
    plots = []
    monkeypatch.setattr(app, "_plot", lambda: plots.append(1))
    for i in range(10):
        app.q.put(("sample", float(i), {"ITKUA": (90.0 + i, "", "")}))
    app._drain()
    assert len(plots) == 1 and app.series["ITKUA"][-1] == 99.0


def test_an_unchanged_value_is_not_set_again(app, monkeypatch):
    app._set_watch(["ITKUA"])
    calls = []
    real = app.tree.set
    monkeypatch.setattr(app.tree, "set", lambda *a: calls.append(a) or real(*a))
    for _ in range(5):
        app._show_sample({"ITKUA": (90.0, "", "")})
    assert len(calls) == 2                                 # value and status, once
    app._show_sample({"ITKUA": (91.0, "", "")})
    assert len(calls) == 3
    app._set_watch(["ITKUA"])                              # a fresh row shows it again
    app._show_sample({"ITKUA": (91.0, "", "")})
    assert len(calls) == 5


def test_availability_keys_follow_the_dme_address(app):
    name = "ILAG2"
    assert app._avail_key(name)[0] == core.DME_MASTER
    app.target_var.set("0x13")
    try:
        assert app._avail_key(name)[0] == 0x13
    finally:
        app.target_var.set("0x12")
    assert app._avail_key(name)[0] == core.DME_MASTER


def test_the_picker_rebuilds_only_when_answers_change(app, monkeypatch):
    app.only_var.set(True)
    try:
        app._refilter()
        rebuilds = []
        monkeypatch.setattr(app, "_refilter", lambda: rebuilds.append(1))
        app._refresh_marks()
        assert rebuilds == []
        app.avail[app._avail_key("ILAG2")] = "ok"
        app._refresh_marks()
        assert rebuilds == [1]
    finally:
        monkeypatch.undo()
        app.only_var.set(False)
        app._refilter()


def test_a_replay_loads_without_blocking_the_window(app, tmp_path, monkeypatch):
    gui = pytest.importorskip("n63read.gui")
    path = tmp_path / "drive.csv"
    # Ten seconds long, so the replay is still running when the test looks.
    path.write_text("Time,ITKUA\n" + "".join("%.1f,%d\n" % (i / 10, 90 + i % 5)
                                             for i in range(100)), encoding="utf-8")
    monkeypatch.setattr(gui.filedialog, "askopenfilename", lambda **k: str(path))
    app._toggle_replay()
    assert app.feed is None or app.feed.kind != "replay"   # still reading
    assert pump(app, lambda: app.feed is not None and app.feed.kind == "replay")
    assert [n for n, _, _ in app.watch] == ["ITKUA"]
    app._stop_feed()


# ------------------------------------------------------------------- MCP

def test_search_loads_the_catalog_once(monkeypatch):
    mcp._search_catalog.cache_clear()
    loads = []
    real = core.load_catalog
    monkeypatch.setattr(core, "load_catalog", lambda *a: loads.append(1) or real(*a))
    for _ in range(3):
        assert "ILAG2" in mcp.tool_search_channels({"query": "ILAG2"})
    assert loads == [1]
    mcp._search_catalog.cache_clear()


def test_read_log_summarises_in_one_pass(tmp_path, monkeypatch):
    path = tmp_path / "drive.csv"
    rows = "".join("%d,%d,%s\n" % (i, i % 7, "on" if i % 2 else "off") for i in range(5000))
    path.write_text("Time,N,STATE\n" + rows, encoding="utf-8")
    monkeypatch.setattr(mcp, "_find", lambda name: str(path))
    text = mcp.tool_read_log({"name": "drive.csv", "head": 2, "tail": 1})
    assert "5000 rows, 3 columns" in text and "time: 0 .. 4999" in text
    assert "min 0 " in text and "max 6 " in text
    assert "2 distinct: off; on" in text
    assert text.endswith("4999,1,on") and "\n0,0,off\n1,1,on\n" in text
