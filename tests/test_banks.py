"""Both banks side by side, calculated channels, ready-made sets, alerts,
and logs that survive a dropped connection."""

import csv
import queue
import socket
import threading
import time
import types

import pytest

from n63read import cli, core, playback

from fake_car import FakeCar

DME = core.load_catalog()


def full_catalog():
    cat = core.load_catalog()
    cat.update(core.load_module_catalogs())
    return core.add_derived(core.add_bank2(cat))


# ----------------------------------------------------------------- banks

def test_bank_twins_and_pairs():
    assert core.bank_twin("ILAG2") == "ILAG2@0x13"
    assert core.bank_twin("ILAG2@0x13") is None
    assert core.bank_pairs(["A", "B", "A@0x13", "C@0x13"]) == [("A", "A@0x13")]


# ------------------------------------------------------ calculated channels

def test_calculated_channels_need_all_their_inputs():
    cat = full_catalog()
    assert all(n in cat for n in core.DERIVED)
    assert core.is_derived(cat["CALC_BOOST_MINUS_TARGET"])
    partial = core.add_derived({"IPLAD": {}})
    assert "CALC_BOOST_MINUS_TARGET" not in partial


def test_inputs_come_before_the_calculated_channel_once():
    got = core.with_inputs(["IPLAD", "CALC_BOOST_MINUS_TARGET", "ITOEL"])
    assert got == ["IPLAD", "S_LADEDRUCK", "CALC_BOOST_MINUS_TARGET", "ITOEL"]


def test_calculated_values():
    s = {"IPLAD": (1800.0, "hPa", ""), "S_LADEDRUCK": (1750.0, "hPa", "")}
    assert core.derive("CALC_BOOST_MINUS_TARGET", s) == (50.0, "hPa", "")
    assert core.derive("CALC_BOOST_MINUS_TARGET", {"IPLAD": (1.0, "", "")})[2] == \
        "needs S_LADEDRUCK"
    iluz = {"ILUZ%d" % (i + 1): (float(i), "", "") for i in range(8)}   # mean 3.5
    assert core.derive("CALC_ILUZ8_FROM_MEAN", iluz)[0] == pytest.approx(3.5)
    assert core.derive("CALC_ILUZ1_FROM_MEAN", iluz)[0] == pytest.approx(-3.5)


class Car:
    def __init__(self, answers):
        self.answers, self.asked = answers, []

    def read_did(self, target, did, timeout=None):
        self.asked.append((target, did))
        return self.answers[(target, did)]


def test_the_reader_works_out_calculated_channels_without_asking_the_car():
    cat = full_catalog()
    names = core.with_inputs(["CALC_LAMBDA_BANK1_MINUS_BANK2"])
    chans = [(n, int(cat[n]["ID"], 16), cat[n]) for n in names]
    did = int(cat["LAMBDA_SONDE_IST"]["ID"], 16)
    raw = {0x12: b"\x10\x00", 0x13: b"\x0f\x00"}
    car = Car({(0x12, did): raw[0x12], (0x13, did): raw[0x13]})
    r = core.ChannelReader({}, 0x12)
    got = r.read(car, chans)
    b1, b2 = got["LAMBDA_SONDE_IST"][0], got["LAMBDA_SONDE_IST@0x13"][0]
    assert got["CALC_LAMBDA_BANK1_MINUS_BANK2"][0] == pytest.approx(b1 - b2)
    assert (0x12, 0) not in car.asked and len(car.asked) == 2


def test_the_catalog_check_leaves_calculated_channels_out():
    layouts = core.check_layouts(full_catalog())
    assert (core.DME_MASTER, 0) not in layouts


def test_every_ready_made_set_is_in_the_catalog():
    cat = full_catalog()
    for name, names in core.BUILTIN_TEMPLATES.items():
        assert names and [n for n in names if n not in cat] == [], name


# ------------------------------------------------------------------ alerts

@pytest.mark.parametrize("value, limits, state", [
    (5, (0, 10), None), (-1, (0, 10), "low"), (11, (0, 10), "high"),
    (11, (None, 10), "high"), (-50, (None, 10), None), (None, (0, 10), None),
    ("text", (0, 10), None), (5, None, None),
])
def test_alert_state(value, limits, state):
    assert core.alert_state(value, limits) == state


# -------------------------------------------------------------- log layout

def test_a_log_row_has_time_clock_channels_and_event():
    names = ["A", "B"]
    assert core.log_header(names) == ["Time", "Clock", "A", "B", "Event"]
    now = time.mktime((2026, 10, 17, 14, 3, 22, 0, 0, -1)) + 0.25
    row = core.log_row(1.23456, now, names, {"A": (1.0, "", ""), "B": (None, "", "")},
                       "=bad")
    assert row == [1.235, "2026-10-17 14:03:22.250", 1.0, "", "'=bad"]


def test_replay_skips_the_clock_and_event_columns(tmp_path):
    path = tmp_path / "log.csv"
    path.write_text("Time,Clock,ITOEL,Event\n0.0,2026-10-17 14:00:00.000,80,\n"
                    "0.5,2026-10-17 14:00:00.500,81,ITOEL above 80\n", encoding="utf-8")
    names, rows = playback.read_log(str(path))
    assert names == ["ITOEL"] and rows[1] == (0.5, {"ITOEL": 81.0})


# -------------------------------------------------------------- reconnect

@pytest.fixture
def car(monkeypatch):
    c = FakeCar()
    monkeypatch.setattr(core.Hsfz.__init__, "__defaults__", (c.port, core.TESTER_ADDR, 3.0))
    monkeypatch.setattr(core, "RECONNECT_WAIT", 0.1)
    yield c
    c.close()


def rows_of(path):
    try:
        with open(path, encoding="utf-8") as f:
            return list(csv.reader(f))
    except FileNotFoundError:
        return []


def test_the_cli_log_carries_on_after_a_dropped_connection(tmp_path, car):
    out = tmp_path / "drive.csv"

    def bump():                                         # a cable bump, once logging
        # The car's request count, not the file: the log reaches disk only
        # once a second, and a quick run can finish before that.
        deadline = time.time() + 5
        while len(car.services) < 5 and time.time() < deadline:
            time.sleep(0.005)
        car.drop()
    threading.Thread(target=bump, daemon=True).start()
    cli.main(["log", "--ip", "127.0.0.1", "--timeout", "0.5", "--interval", "0.02",
              "--samples", "40", "--out", str(out), "ILAG2"])
    rows = rows_of(out)
    events = [r[-1] for r in rows[1:] if r[-1]]
    assert any(e.startswith("connection lost") for e in events)
    assert any(e.startswith("reconnected after") for e in events)
    assert rows[-1][2] == "1.0"                            # reading again, same file
    assert car.connections >= 2


def test_the_app_log_carries_on_after_a_dropped_connection(tmp_path, car):
    gui = pytest.importorskip("n63read.gui")
    q = queue.Queue()
    p = gui.Poller("127.0.0.1", 0x12, 0xF4, 1.0, q)
    p.interval = 0.05
    p.set_channels([("ILAG2", int(DME["ILAG2"]["ID"], 16), DME["ILAG2"])])
    path = str(tmp_path / "drive.csv")
    p.start_log(path)
    p.start()
    try:
        deadline = time.time() + 5
        while len(rows_of(path)) < 3 and time.time() < deadline:
            time.sleep(0.05)
        car.drop()
        deadline = time.time() + 10
        while time.time() < deadline and not any(
                r[-1].startswith("reconnected") for r in rows_of(path)[1:]):
            time.sleep(0.05)
    finally:
        p.stop()
        p.join(3)
    events = [r[-1] for r in rows_of(path)[1:] if r[-1]]
    assert any(e.startswith("connection lost") for e in events)
    assert any(e.startswith("reconnected after") for e in events)
    msgs = []
    while not q.empty():
        msgs.append(q.get())
    assert ("status", "reconnecting", p) in msgs
    assert not [m for m in msgs if m[0] == "error"]       # not treated as a failure


def test_without_a_log_a_dropped_connection_is_an_error(car):
    gui = pytest.importorskip("n63read.gui")
    q = queue.Queue()
    p = gui.Poller("127.0.0.1", 0x12, 0xF4, 1.0, q)
    p.interval = 0.05
    p.set_channels([("ILAG2", int(DME["ILAG2"]["ID"], 16), DME["ILAG2"])])
    p.start()
    try:
        deadline = time.time() + 5
        while q.empty() and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(0.2)
        car.drop()
        p.join(5)
    finally:
        p.stop()
    msgs = []
    while not q.empty():
        msgs.append(q.get())
    assert [m for m in msgs if m[0] == "error"]
    assert ("status", "disconnected", p) in msgs


def test_losing_bank2_does_not_take_the_main_connection_down(car):
    with core.Tester("127.0.0.1", timeout=1.0) as t:
        assert t.read_did(core.DME_SLAVE, 0x588A) == b"\x0f\x00"
        car.live[-1].shutdown(socket.SHUT_RDWR)          # bank 2's connection breaks
        with pytest.raises(core.HsfzError) as e:
            t.read_did(core.DME_SLAVE, 0x588A)
        assert not isinstance(e.value, core.ConnectionLost)
        assert t.slave is None
        assert t.read_did(0x12, 0x588A) == b"\x10\x00"   # bank 1 carries on
        assert t.read_did(core.DME_SLAVE, 0x588A) == b"\x0f\x00"   # and bank 2 reopens


# --------------------------------------------------------------------- GUI

def test_comparing_banks_folds_bank2_into_the_bank1_row(app):
    app.compare_var.set(False)
    app._set_watch(["ILAG2", "ITOEL"])
    app.compare_var.set(True)
    app._compare_changed()
    try:
        names = [n for n, _, _ in app.watch]
        assert "ILAG2@0x13" in names and "ITOEL@0x13" in names
        assert app.tree.get_children() == ("ILAG2", "ITOEL")      # twins folded in
        assert "b2" in app.tree.cget("displaycolumns")
        app._show_sample({"ILAG2": (1.02, "-", ""), "ILAG2@0x13": (0.98, "-", "")})
        assert app.tree.set("ILAG2", "b2") == "0.98"
        assert app.tree.set("ILAG2", "d") == "+0.04"
        app.tree.selection_set("ILAG2")
        app._remove()                                             # takes bank 2 too
        assert [n for n, _, _ in app.watch] == ["ITOEL", "ITOEL@0x13"]
    finally:
        app.compare_var.set(False)
        app._compare_changed()
    assert app.tree.get_children() == ("ITOEL", "ITOEL@0x13")     # back as its own row
    assert "b2" not in app.tree.cget("displaycolumns")
    app._clear()
    assert app.tree.get_children() == () and app.watch == []


def test_bank2_is_drawn_over_bank1(app):
    app.compare_var.set(True)
    try:
        app._set_watch(["ITKUA"])
        for i in range(5):
            app._show_sample({"ITKUA": (90.0 + i, "", ""), "ITKUA@0x13": (89.0 + i, "", "")},
                             elapsed=float(i))
        texts = [app.canvas.itemcget(i, "text") for i in app.canvas.find_all()
                 if app.canvas.type(i) == "text"]
        assert any("bank 2 dashed" in t for t in texts)
        dashed = [i for i in app.canvas.find_all()
                  if app.canvas.type(i) == "line" and app.canvas.itemcget(i, "dash")]
        assert dashed
        win = app._pop_out(["ITKUA"])[0]
        assert win.overlay is app.history["ITKUA@0x13"]
        win.close()
    finally:
        app.compare_var.set(False)
        app._compare_changed()


def test_a_calculated_channel_brings_its_inputs(app):
    app._set_watch(["CALC_BOOST_MINUS_TARGET"])
    assert [n for n, _, _ in app.watch] == ["IPLAD", "S_LADEDRUCK", "CALC_BOOST_MINUS_TARGET"]
    assert app._info["CALC_BOOST_MINUS_TARGET"][0] == core.CALC_LABEL


def test_ready_made_sets_load(app):
    app._load_builtin("Misfire hunt")
    names = [n for n, _, _ in app.watch]
    assert "ILUZ8" in names and "CALC_ILUZ8_FROM_MEAN" in names


def test_an_alert_highlights_beeps_and_marks_the_log(app, monkeypatch):
    app._set_watch(["ITOEL"])
    beeps, marks = [], []
    monkeypatch.setattr(app, "bell", lambda: beeps.append(1))
    app.poller = types.SimpleNamespace(mark=marks.append, set_channels=lambda c: None)
    try:
        app._set_alert("ITOEL", (None, 120.0))
        app._show_sample({"ITOEL": (100.0, "", "")})
        assert app.tree.item("ITOEL", "tags") == "" and beeps == []
        app._show_sample({"ITOEL": (125.0, "", "")})
        assert "alert" in app.tree.item("ITOEL", "tags")
        assert beeps == [1] and marks and "ITOEL above 120" in marks[0]
        app._show_sample({"ITOEL": (126.0, "", "")})
        assert beeps == [1]                                     # once per crossing
        app._show_sample({"ITOEL": (110.0, "", "")})
        assert app.tree.item("ITOEL", "tags") == ""
        assert app.settings["alerts"]["ITOEL"] == [None, 120.0]
    finally:
        app.poller = None
        app._set_alert("ITOEL", (None, None))
    assert "ITOEL" not in app.settings["alerts"]


def test_alert_limits_are_typed_in_shown_units(app):
    meta = app.catalog["ITOEL"]
    app.units = "imperial"
    try:
        lo, hi = app._limits_from("", "248", meta)              # 248 °F
        assert lo is None and hi == pytest.approx(120.0)        # stored as 120 °C
        win = app._edit_alert() if app.tree.selection() else None
        if win:
            win.destroy()
    finally:
        app.units = "metric"


def test_the_alert_dialog_saves_limits(app, monkeypatch):
    app._set_watch(["ITOEL"])
    app.tree.selection_set("ITOEL")
    win = app._edit_alert()
    try:
        entries = [w for w in win.winfo_children() if w.winfo_class() == "TEntry"]
        entries[1].delete(0, "end")
        entries[1].insert(0, "130")
        buttons = [b for f in win.winfo_children() if f.winfo_class() == "TFrame"
                   for b in f.winfo_children()]
        next(b for b in buttons if b.cget("text") == "Save").invoke()
        assert app.alerts["ITOEL"] == (None, 130.0)
    finally:
        app._set_alert("ITOEL", (None, None))
        if win.winfo_exists():
            win.destroy()


def test_a_reconnecting_status_keeps_the_connection(app):
    poller = app.poller = object()
    app.logging_to = "C:/logs/run.csv"
    try:
        app.q.put(("status", "reconnecting", poller))
        app._drain()
        assert app.poller is poller and app.logging_to == "C:/logs/run.csv"
        assert "reconnecting" in app.status.cget("text")
    finally:
        app.poller = None
        app.logging_to = None

