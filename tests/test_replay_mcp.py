"""Replay at any speed, other tools' CSVs, and Claude's log and sampling tools."""

import os
import queue
import time

import pytest

from n63read import core, mcp, playback

from conftest import pump
from fake_car import FakeCar


# ------------------------------------------------------- other tools' logs

def write(tmp_path, text, name="log.csv"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_a_semicolon_export_with_decimal_commas_and_clock_times(tmp_path):
    path = write(tmp_path, "Time;Engine speed (rpm);Boost [psi];Gear\n"
                           "23:59:59,500;812,5;-1,2;P\n00:00:00,000;815;-1,1;P\n")
    names, rows = playback.read_log(path)
    assert names == ["Engine speed (rpm)", "Boost [psi]", "Gear"]
    assert rows[0] == (0.0, {"Engine speed (rpm)": 812.5, "Boost [psi]": -1.2, "Gear": "P"})
    assert rows[1][0] == pytest.approx(0.5)                     # across midnight


def test_seconds_with_a_named_time_column(tmp_path):
    names, rows = playback.read_log(write(tmp_path, "Time (s),RPM\n0.0,800\n0.25,810\n"))
    assert names == ["RPM"] and [t for t, _ in rows] == [0.0, 0.25]


def test_the_first_column_must_be_the_time(tmp_path):
    with pytest.raises(ValueError, match="first column must be the time"):
        playback.read_log(write(tmp_path, "RPM,Boost\n800,1\n"))


@pytest.mark.parametrize("name, label, unit", [
    ("Engine speed (rpm)", "Engine speed", "rpm"), ("Boost [psi]", "Boost", "psi"),
    ("ILAG2", "ILAG2", ""), ("Oil temp (°C)", "Oil temp", "°C"),
])
def test_label_and_unit(name, label, unit):
    assert playback.label_and_unit(name) == (label, unit)


def test_the_reader_leaves_log_only_columns_alone():
    meta = {"ARG": "RPM", "ID": "0x0000", "LOGONLY": "1"}
    got = core.ChannelReader({}, 0x12).read(None, [("RPM", 0, meta)])
    assert got["RPM"] == (None, "", "only in a log")


# ------------------------------------------------------------- the feed

ROWS = [(float(i), {"A": float(i)}) for i in range(40)]


def samples(q):
    out = []
    while not q.empty():
        m = q.get()
        if m[0] == "feedsample":
            out.append(m[2])
    return out


def test_the_feed_speeds_up_seeks_and_pauses():
    q = queue.Queue()
    feed = playback.ReplayFeed(q, ROWS, speed=16)
    feed.set_channels([("A", 0, {})])
    feed.start()
    time.sleep(0.3)
    feed.seek(30)
    deadline = time.time() + 3
    while time.time() < deadline and not any(t >= 30 for t in [m for m in samples(q)]):
        time.sleep(0.05)
    feed.pause(True)
    time.sleep(0.2)
    while not q.empty():
        q.get()
    time.sleep(0.4)
    assert samples(q) == []                                  # paused: nothing comes
    feed.pause(False)
    feed.join(5)
    assert not feed.is_alive()


def test_a_slow_feed_reacts_to_a_speed_change_at_once():
    q = queue.Queue()
    rows = [(0.0, {"A": 1.0}), (2.0, {"A": 2.0})]         # a 2 s gap at 1x
    feed = playback.ReplayFeed(q, rows)
    feed.set_channels([("A", 0, {})])
    started = time.time()
    feed.start()
    time.sleep(0.2)
    feed.set_speed(16)
    feed.join(3)
    assert time.time() - started < 1.5


# --------------------------------------------------------------- the app

def test_replaying_a_foreign_log_with_the_controls(app, tmp_path, monkeypatch):
    gui = pytest.importorskip("n63read.gui")
    lines = "".join("%d,%d,%d\n" % (i, 800 + i, i % 3) for i in range(200))
    path = write(tmp_path, "Time,Engine speed (rpm),ILAG2\n" + lines)
    monkeypatch.setattr(gui.filedialog, "askopenfilename", lambda **k: path)
    app._toggle_replay()
    assert pump(app, lambda: app.feed is not None and app.feed.kind == "replay")
    try:
        assert [n for n, _, _ in app.watch] == ["Engine speed (rpm)", "ILAG2"]
        meta = app.catalog["Engine speed (rpm)"]
        assert meta["LOGONLY"] and meta["EINHEIT"] == "rpm"
        assert app.replay_bar.winfo_manager() == "pack"
        app.replay_speed_var.set("16x")
        app._replay_speed()
        assert app.feed.speed == 16
        app._scrub(150)
        assert pump(app, lambda: app.scrub_var.get() >= 150)
        app._replay_pause()
        assert app.feed.paused and app.replay_pause_btn.cget("text") == "Resume"
        feed = app.feed
        app._whole_log()
        assert feed.whole and app.feed is feed
        assert len(app.history["Engine speed (rpm)"]) == 200
        assert app.series["ILAG2"][-1] == 199 % 3
        assert app.tree.set("Engine speed (rpm)", "v") == "999"
        assert app.replay_bar.winfo_manager() == ""
    finally:
        app._stop_feed()
    assert app.replay_bar.winfo_manager() == ""


# ------------------------------------------------------------ the car tools

@pytest.fixture
def car(monkeypatch):
    c = FakeCar()
    monkeypatch.setattr(core.Hsfz.__init__, "__defaults__", (c.port, core.TESTER_ADDR, 3.0))
    yield c
    c.close()


def call(tool, **args):
    try:
        return mcp.HANDLERS[tool](args), False
    except mcp.ToolError as e:
        return str(e), True


def test_read_channels_reads_through_core(car):
    text, error = call("read_channels", ip="127.0.0.1", names=["ILAG2", "HU_STATUS_GPS"])
    assert not error
    assert "ILAG2" in text and "1.0" in text and "3D position" in text
    assert set(car.services) <= {0x22}


def test_sample_summarises_each_channel(car):
    text, error = call("sample", ip="127.0.0.1", names=["ILAG2", "ITOEL"], seconds=1,
                       interval=0.05)
    assert not error and "cycles in 1 s" in text
    assert "ILAG2" in text and "mean 1" in text
    assert "ITOEL" in text and "requestOutOfRange" in text


def test_start_and_stop_a_log(car):
    text, error = call("start_log", ip="127.0.0.1", names=["ILAG2"], interval=0.05)
    assert not error, text
    try:
        text2, error2 = call("start_log", ip="127.0.0.1", names=["ILAG2"])
        assert error2 and "already logging" in text2
        time.sleep(0.6)
    finally:
        text, error = call("stop_log")
    assert not error and "rows in n63_" in text
    name = text.split(" in ")[-1].strip()
    with open(os.path.join(core.logs_dir(), name), encoding="utf-8") as f:
        rows = f.read().splitlines()
    assert rows[0] == "Time,Clock,ILAG2,Event" and len(rows) > 3
    assert call("stop_log")[1]                                 # nothing to stop now


def test_a_template_log_and_a_bad_one(car):
    text, error = call("start_log", ip="127.0.0.1", template="Nope")
    assert error and "no template" in text


# ------------------------------------------------------- looking into logs

LOG = ("Time,Clock,ILAG2,ILAG2@0x13,ZAEHLER_AUSSETZERK_ZYL5,ITOEL,Event\n"
       + "".join("%d,c,%.2f,%.2f,%d,%d,%s\n" % (
           i, 1.0 + i / 100, 0.98, 0 if i < 5 else 2, 100 + i * 4,
           "ITOEL above 130 (132)" if i == 8 else "") for i in range(10)))


@pytest.fixture
def log():
    path = os.path.join(core.logs_dir(), "banks.csv")
    with open(path, "w", encoding="utf-8") as f:
        f.write(LOG)
    yield "banks.csv"
    os.remove(path)


def test_read_log_window_columns_and_resample(log):
    text, error = call("read_log", name=log, start=2, end=7, columns=["ITOEL"])
    assert not error
    assert "6 of 10 rows from 2 to 7 s" in text
    assert text.splitlines()[1] == "Time,ITOEL" and text.splitlines()[2] == "2.0,108.0"
    text, error = call("read_log", name=log, resample=5, columns=["ITOEL"])
    assert "averaged over 5 s" in text and text.splitlines()[-1] == "5.0,128.0"
    text, error = call("read_log", name=log, columns=["NOPE"])
    assert error and "no column(s) NOPE" in text


def test_compare_banks(log):
    text, error = call("compare_banks", name=log)
    assert not error and "1 pair(s)" in text
    assert "ILAG2" in text and "largest +0.11 at 9 s" in text


def test_find_events(log):
    text, error = call("find_events", name=log, column="ITOEL", above=125)
    assert not error
    assert "ITOEL above 130 (132)" in text                    # the log's own Event
    assert "ITOEL above 125 (128)" in text                    # the crossing asked for
    assert "ZAEHLER_AUSSETZERK_ZYL5 went up 0 -> 2" in text   # misfire counter rose
    text, error = call("find_events", name=log, column="ITOEL")
    assert error and "above or below" in text
