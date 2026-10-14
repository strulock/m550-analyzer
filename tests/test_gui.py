"""The GUI's event handler, and its flows against the fake car."""

import csv
import tkinter as tk

import pytest

from n63read import core, playback

from conftest import pump
from fake_car import FakeCar

gui = pytest.importorskip("n63read.gui")

LAT = "HU_STATUS_GPS_POSITION.LATITUDE"
FIX = "HU_STATUS_GPS"


@pytest.fixture
def dialogs(monkeypatch):
    """Answer every dialog; record what was shown."""
    seen = []
    for name in ("showinfo", "showerror", "showwarning"):
        monkeypatch.setattr(gui.messagebox, name,
                            lambda *a, _n=name, **k: seen.append((_n, a[0], a[1])))
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: True)
    return seen


def drain(app, *msgs):
    for m in msgs:
        app.q.put(m)
    app._drain()


def cell(app, name, col):
    return app.tree.set(name, col)


# ------------------------------------------------------------ event handler

def test_sample_fills_table_with_words_and_coordinates(app):
    app._add_names([LAT, FIX, "ILAG2"])
    drain(app, ("sample", 1.0, {LAT: (30.1346, "deg", ""), FIX: (12.0, "", ""),
                                "ILAG2": (1.0, "", "")}))
    assert cell(app, LAT, "v") == "30.134600"        # %.4g would say 30.13
    assert cell(app, FIX, "v") == "12" and cell(app, FIX, "note") == "3D position"
    assert cell(app, "ILAG2", "v") == "1"
    assert app.series["ILAG2"] == [1.0]


def test_sample_teaches_what_the_car_answers(app):
    app._add_names([LAT, FIX])
    drain(app, ("sample", 0.0, {LAT: (30.1, "deg", ""), FIX: (None, "", "requestOutOfRange")}))
    assert app.avail[(0x63, 0xD039)] == "ok"
    assert app.avail[(0x63, 0xD038)] == "refused"
    assert app._avail_dirty


def test_replay_messages(app):
    rows = [(10.0, {"ILAG2": 1.0}), (75.0, {"ILAG2": 0.9})]
    feed = playback.ReplayFeed(app.q, rows)                   # not started: fed by hand
    feed.name_shown = "drive.csv"
    app._add_names(["ILAG2"])
    app.feed = feed
    app.replay_btn.config(text="Stop replay")
    stale = playback.ReplayFeed(app.q, rows)
    drain(app, ("feedsample", stale, 10.0, {"ILAG2": (5.0, "", "")}),   # ignored
          ("feedsample", feed, 70.0, {"ILAG2": (0.9, "", "")}))
    assert cell(app, "ILAG2", "v") == "0.9"
    assert "replaying drive.csv  1:00 / 1:05" in app.status.cget("text")
    drain(app, ("feeddone", feed))
    assert app.feed is None and app.replay_btn.cget("text") == "Open log..."
    assert "replay finished" in app.status.cget("text")


def test_check_messages(app):
    app._checking = {"stop": False}
    app.avail[(0x63, 0xD039)] = "ok"
    drain(app, ("checkres", (0x63, 0xD039), "no answer", 1, 50),  # keeps what was known
          ("checkres", (0x63, 0xD038), "refused", 2, 50),
          ("checkres", (0x18, 0xDA2E), "no answer", 3, 50),
          ("checkres", (0x12, 0x588A), "size", 50, 50))
    assert app.avail == {(0x63, 0xD039): "ok", (0x63, 0xD038): "refused",
                         (0x18, 0xDA2E): "no answer", (0x12, 0x588A): "size"}
    assert "checking 50 / 50" in app.status.cget("text")
    drain(app, ("checkdone", False))
    assert app._checking is None
    assert "check done: 2 identifiers answer" in app.status.cget("text")
    assert core.load_availability(app.avail_file) == app.avail


def test_status_messages(app):
    app.poller = object()                                     # only its presence matters
    drain(app, ("status", "connected"))
    assert "connected" in app.status.cget("text") and app.poller is not None
    app.connect_btn.config(text="Disconnect")
    drain(app, ("status", "disconnected"))
    assert app.poller is None and app.connect_btn.cget("text") == "Connect"


def test_connection_error_drops_the_poller(app, dialogs):
    app.poller = object()
    drain(app, ("error", "connection reset"))
    assert app.poller is None and "error" in app.status.cget("text")
    assert dialogs == [("showerror", "Connection", "connection reset")]


def test_a_failed_background_job_keeps_the_connection(app, dialogs):
    poller = app.poller = object()
    app.status.config(text="\u25cf connected")
    drain(app, ("joberror", "background read failed: boom"))
    assert app.poller is poller                               # still connected
    assert "connected" in app.status.cget("text")
    assert dialogs == [("showerror", "Error", "background read failed: boom")]
    app.poller = None


def test_logfile_module_and_scanline_messages(app):
    drain(app, ("logfile", "C:/logs/run_2.csv"), ("module", 0x18), ("module", 0x18))
    assert app.logging_to == "C:/logs/run_2.csv"
    assert "logging to run_2.csv" in app.status.cget("text")
    assert app.modules == [0x18]
    box = tk.Text(app)
    drain(app, ("scanline", box, "0x18  responds\n"))
    assert box.get("1.0", "end").strip() == "0x18  responds"
    box.destroy()
    drain(app, ("scanline", box, "late\n"))                   # window closed: ignored


def test_discovered_messages(app, dialogs):
    drain(app, ("discovered", []))
    assert dialogs and dialogs[0][1] == "Discover"
    drain(app, ("discovered", [("169.254.10.20", "WBA...")]))
    assert app.ip_var.get() == "169.254.10.20" and "found 1" in app.status.cget("text")


# ------------------------------------------------------- against the fake car

@pytest.fixture
def car(monkeypatch):
    c = FakeCar()
    monkeypatch.setattr(core.Hsfz.__init__, "__defaults__", (c.port, core.TESTER_ADDR, 3.0))
    yield c
    c.close()


def connect(app):
    app.ip_var.set("127.0.0.1")
    app.interval_var.set("0.05")
    app._toggle()
    assert pump(app, lambda: "connected" in app.status.cget("text"))


def test_connect_reads_dme_and_head_unit_then_disconnects(app, car):
    app._add_names(["ILAG2", LAT, FIX])
    connect(app)
    assert pump(app, lambda: cell(app, "ILAG2", "v") == "1" and cell(app, LAT, "v"))
    assert cell(app, LAT, "v") == "30.134600"
    assert cell(app, FIX, "note") == "3D position"
    assert app.avail[(0x63, 0xD039)] == "ok"
    poller = app.poller
    app._toggle()
    assert app.poller is None and app.connect_btn.cget("text") == "Connect"
    poller.join(3)
    assert not poller.is_alive()


def test_logging_from_the_gui(app, car, tmp_path, monkeypatch, dialogs):
    path = str(tmp_path / "drive.csv")
    monkeypatch.setattr(gui.filedialog, "asksaveasfilename", lambda **k: path)
    app._toggle_log()
    assert dialogs[-1][2] == "Connect first."
    app._add_names(["ILAG2", LAT])
    connect(app)
    app._toggle_log()
    assert app.logging_to == path and app.log_btn.cget("text") == "Stop logging"

    def rows():
        try:
            with open(path, encoding="utf-8") as f:
                return list(csv.reader(f))
        except FileNotFoundError:
            return []
    assert pump(app, lambda: len(rows()) >= 4)
    app._toggle_log()
    assert app.logging_to is None and app.log_btn.cget("text") == "Start logging"
    got = rows()
    assert got[0] == ["Time", "ILAG2", LAT]
    assert float(got[-1][1]) == 1.0 and float(got[-1][2]) == pytest.approx(30.1346, abs=1e-4)


@pytest.fixture
def small_check(app, monkeypatch):
    """Check only a handful of identifiers: absent modules would cost seconds."""
    full = core.check_layouts(app.catalog, 0x12)
    keys = [(0x63, 0xD039), (0x63, 0xD030), (0x63, 0xD038), (0x63, 0xD00D),
            (0x12, 0x588A), (0x12, 0x4402)]
    monkeypatch.setattr(core, "check_layouts", lambda cat, target: {k: full[k] for k in keys})
    return {(0x63, 0xD039): "ok", (0x63, 0xD030): "ok", (0x63, 0xD038): "ok",
            (0x63, 0xD00D): "refused", (0x12, 0x588A): "ok", (0x12, 0x4402): "refused"}


def test_check_car_not_connected(app, car, small_check, dialogs):
    app._check_car()
    assert dialogs[-1][2] == "Enter or discover an IP first."
    app.ip_var.set("127.0.0.1")
    app._check_car()
    assert pump(app, lambda: app._checking is None)
    assert app.avail == small_check
    assert core.load_availability(app.avail_file) == small_check
    assert "check done: 4 identifiers answer" in app.status.cget("text")


def test_check_car_on_the_live_connection(app, car, small_check, dialogs):
    app._add_names(["ILAG2"])
    connect(app)
    app._check_car()
    app._check_car()
    assert dialogs[-1][2] == "A check is already running."
    assert pump(app, lambda: app._checking is None)
    assert app.avail == small_check
    assert app.poller is not None                              # still logging


def test_a_failing_check_finishes_and_keeps_the_connection(app, car, small_check,
                                                           monkeypatch, dialogs):
    def broken(h, layouts, keys=None):
        yield (0x63, 0xD039), "ok"
        raise OSError("socket went away")
    monkeypatch.setattr(core, "check_steps", broken)
    connect(app)
    app._check_car()
    assert pump(app, lambda: app._checking is None)
    assert app.poller is not None
    assert ("showerror", "Error", "background read failed: socket went away") in dialogs
    app._check_car()                                           # can run again
    assert app._checking is not None


def test_check_car_without_a_car(app, small_check, monkeypatch, dialogs):
    def refuse(self):
        raise ConnectionRefusedError("refused")       # at once, not after ~2 s
    monkeypatch.setattr(core.Hsfz, "connect", refuse)
    app.ip_var.set("127.0.0.1")
    app._check_car()
    assert pump(app, lambda: app._checking is None)
    assert any(d[0] == "showerror" and d[2].startswith("check failed") for d in dialogs)


def test_import_and_forget_answers(app, tmp_path, monkeypatch, dialogs):
    path = tmp_path / "check.csv"
    path.write_text("module,did,job,status,detail,reply_bytes,raw\n"
                    "0x63,0xD039,STATUS_GPS_POSITION,ok,13 bytes,13,00\n"
                    "0x18,0xDA2E,ISTGANG,refused,requestOutOfRange,0,\n", encoding="utf-8")
    monkeypatch.setattr(gui.filedialog, "askopenfilename", lambda **k: str(path))
    app._import_check()
    assert app.avail == {(0x63, 0xD039): "ok", (0x18, 0xDA2E): "refused"}
    assert "imported 2 answers (1 answered)" in app.status.cget("text")
    assert core.load_availability(app.avail_file) == app.avail

    bad = tmp_path / "bad.csv"
    bad.write_text("x,y\n1,2\n", encoding="utf-8")
    monkeypatch.setattr(gui.filedialog, "askopenfilename", lambda **k: str(bad))
    app._import_check()
    assert dialogs[-1][0] == "showerror"

    app._forget_answers()
    assert app.avail == {} and core.load_availability(app.avail_file) == {}
