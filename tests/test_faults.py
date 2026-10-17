import csv
import queue
import time

import pytest

from n63read import cli, core

from fake_car import FakeCar


@pytest.fixture
def car():
    c = FakeCar()
    yield c
    c.close()


def connect(car, timeout=0.5):
    return core.Hsfz("127.0.0.1", port=car.port, timeout=timeout)


def test_sweep_finds_only_present_modules(car):
    with connect(car) as h:
        found = core.sweep_modules(h, 0x0E, 0x13)
    assert found == [0x10, 0x12]


def test_probe_restores_the_normal_timeout(car):
    with connect(car, timeout=2.0) as h:
        h.probe(0x55, timeout=0.1)
        assert h.timeout == 2.0 and h.sock.gettimeout() == 2.0


def test_read_all_faults_reports_every_module(car):
    with connect(car) as h:
        rows = core.read_all_faults(h, [0x10, 0x12, 0x40, 0x55],
                                    names={0x12: "Engine, master DME"})
    by_module = {}
    for r in rows:
        by_module.setdefault(r["module"], []).append(r)

    assert by_module["0x10"][0]["state"] == "no faults"
    assert by_module["0x40"][0]["state"].startswith("refused (serviceNotSupported")
    assert by_module["0x55"][0]["state"].startswith("no reply")

    dme = by_module["0x12"]
    assert [r["code"] for r in dme] == ["021200", "ABCDEF"]
    assert dme[0]["module_name"] == "Engine, master DME"
    assert dme[0]["state"] == "present, stored"
    assert dme[0]["text_de"] == "Energiesparmode aktiv"
    assert dme[1]["state"] == "not present, stored"
    assert dme[1]["text_en"] == "(not in catalog)"


def test_fault_sweep_sends_only_read_services(car):
    with connect(car) as h:
        core.read_all_faults(h, core.sweep_modules(h, 0x10, 0x12))
    assert set(car.services) <= {0x19, 0x3E}


def test_write_fault_csv_round_trips(tmp_path, car):
    with connect(car) as h:
        rows = core.read_all_faults(h, [0x12])
    path = tmp_path / "faults.csv"
    core.write_fault_csv(str(path), rows)
    with open(path, encoding="utf-8-sig") as f:
        back = list(csv.DictReader(f))
    assert tuple(back[0]) == core.FAULT_CSV_HEADER
    assert [r["code"] for r in back] == ["021200", "ABCDEF"]


def test_module_names_skip_ambiguous_addresses():
    names = core.load_module_names()
    assert names[0x60] == "Instrument cluster (KOMBI)"   # known, overrides map
    assert names[0x29].startswith("Dynamische")          # one row in the map
    assert names[0x12] == "Engine, master DME"           # map has 3 variants


def test_cli_faults_all_saves_csv(tmp_path, car, monkeypatch, capsys):
    monkeypatch.setattr(core, "HSFZ_DIAG_PORT", car.port)
    monkeypatch.setattr(core, "SWEEP_RANGE", (0x10, 0x12))
    monkeypatch.setattr(core.Hsfz.__init__, "__defaults__",
                        (car.port, core.TESTER_ADDR, 3.0))
    out = tmp_path / "f.csv"
    cli.main(["faults", "--ip", "127.0.0.1", "--timeout", "0.5", "--all", "--out", str(out)])
    text = capsys.readouterr().out
    assert "2 fault(s) across 2 module(s)." in text
    with open(out, encoding="utf-8-sig") as f:
        assert len(list(csv.DictReader(f))) == 3     # gateway row + 2 DME faults


# ------------------------------------------------------------ GUI poller

gui = pytest.importorskip("n63read.gui")


def poller_for(car, q):
    p = gui.Poller("127.0.0.1", 0x12, 0xF4, 1.0, q)
    p.interval = 0.05
    # Point the poller's connection at the fake car.
    core.Hsfz.__init__.__defaults__ = (car.port, core.TESTER_ADDR, 3.0)
    return p


@pytest.fixture
def restore_gui_core():
    saved = core.Hsfz.__init__.__defaults__
    yield
    core.Hsfz.__init__.__defaults__ = saved


def chan(name):
    meta = core.load_catalog()[name]
    return (name, int(meta["ID"], 16), meta)


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def lines(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().splitlines()
    except FileNotFoundError:
        return []


def test_log_reaches_disk_while_still_logging(tmp_path, car, restore_gui_core):
    q = queue.Queue()
    p = poller_for(car, q)
    p.set_channels([chan("ILAG2")])
    path = str(tmp_path / "drive.csv")
    p.start_log(path)
    p.start()
    try:
        # No stop_log(): the data must already be on disk, as after a crash.
        assert wait_for(lambda: len(lines(path)) >= 3, timeout=3.0)
        assert lines(path)[0] == "Time,Clock,ILAG2,Event"
        assert lines(path)[1].endswith(",1.0,")
    finally:
        p.stop()


def test_channel_change_starts_a_new_file(tmp_path, car, restore_gui_core):
    q = queue.Queue()
    p = poller_for(car, q)
    p.set_channels([chan("ILAG2")])
    path = str(tmp_path / "drive.csv")
    p.start_log(path)
    p.start()
    try:
        assert wait_for(lambda: len(lines(path)) >= 2)
        p.set_channels([chan("ILAG1"), chan("ILAG2")])
        second = str(tmp_path / "drive_2.csv")
        assert wait_for(lambda: len(lines(second)) >= 2)
    finally:
        p.stop()
        p.join(3)
    first = lines(path)
    assert first[0] == "Time,Clock,ILAG2,Event"
    assert all(not l.startswith("Time") for l in first[1:])   # one header only
    assert lines(second)[0] == "Time,Clock,ILAG1,ILAG2,Event"
    msgs = []
    while not q.empty():
        msgs.append(q.get())
    assert ("logfile", second) in msgs


def test_fault_job_runs_on_the_live_connection(car, restore_gui_core):
    q = queue.Queue()
    p = poller_for(car, q)
    p.start()
    try:
        done = []
        p.submit(lambda h: done.append(core.read_all_faults(h, [0x12])))
        assert wait_for(lambda: done)
        assert [r["code"] for r in done[0]] == ["021200", "ABCDEF"]
    finally:
        p.stop()


def test_fault_steps_one_round_trip_per_step(car, monkeypatch):
    monkeypatch.setattr(core, "SWEEP_RANGE", (0x10, 0x13))
    with connect(car) as h:
        steps = list(core.fault_steps(h))
    kinds = [k for k, _ in steps]
    assert kinds == ["found", "finding", "found", "finding",   # 0x10..0x13
                     "reading", "reading", "done"]
    assert [r["code"] for r in steps[-1][1] if r["code"]] == ["021200", "ABCDEF"]


def test_logging_continues_during_a_fault_sweep(tmp_path, car, monkeypatch, restore_gui_core):
    monkeypatch.setattr(core, "SWEEP_RANGE", (0x00, 0x13))   # mostly empty: ~8 s
    q = queue.Queue()
    p = poller_for(car, q)
    p.set_channels([chan("ILAG2")])
    path = str(tmp_path / "drive.csv")
    p.start_log(path)
    p.start()
    try:
        assert wait_for(lambda: len(lines(path)) >= 2)
        done = []

        def job(h):
            for kind, value in core.fault_steps(h):
                if kind == "done":
                    done.append(value)
                yield
        p.submit(job)
        before = len(lines(path))
        time.sleep(3)
        assert not done                     # sweep still running...
        assert len(lines(path)) > before    # ...and the log is still growing
        assert wait_for(lambda: done, timeout=60)
    finally:
        p.stop()


# ------------------------------------------------------------ probe wait

def test_probe_waits_only_as_long_as_asked(car):
    with connect(car, timeout=2.0) as h:
        t = time.time()
        assert not h.probe(0x55, timeout=0.2)
        assert time.time() - t < 0.45                 # not the 0.5 s default


def test_probe_default_is_read_when_called(car, monkeypatch):
    # Changing PROBE_TIMEOUT at run time must take effect, not the value at import.
    monkeypatch.setattr(core, "PROBE_TIMEOUT", 0.1)
    with connect(car) as h:
        t = time.time()
        h.probe(0x55)
        assert time.time() - t < 0.4


def test_sweep_and_fault_steps_take_a_probe_timeout(car, monkeypatch):
    monkeypatch.setattr(core, "SWEEP_RANGE", (0x10, 0x16))
    with connect(car) as h:
        t = time.time()
        found = core.sweep_modules(h, probe_timeout=0.1)
        steps = list(core.fault_steps(h, probe_timeout=0.1))
    assert found == [0x10, 0x12]
    assert steps[-1][0] == "done"
    assert time.time() - t < 2.5                      # 10 empty probes at 0.1 s


@pytest.mark.parametrize("text", ["0", "0.01", "11", "abc", ""])
def test_parse_probe_timeout_rejects_bad_values(text):
    with pytest.raises(ValueError):
        core.parse_probe_timeout(text)


def test_parse_probe_timeout_and_sweep_estimate():
    assert core.parse_probe_timeout("1.5") == 1.5
    lo, hi = core.SWEEP_RANGE
    assert core.sweep_seconds(1.0) == hi - lo + 1


@pytest.mark.parametrize("code, module", [("025600", 0x56), ("025D00", 0x5D), ("026100", 0x61)])
def test_roof_camera_and_telematics_faults_have_texts(code, module):
    entry = core.load_fault_texts()[code]
    assert entry["text_en"] == "Energy-saving mode active"
    assert core.load_module_names()[module]


@pytest.mark.parametrize("code, module", [("026D00", 0x6D), ("026E00", 0x6E), ("025E00", 0x5E),
                                          ("022B00", 0x2B), ("020D00", 0x0D)])
def test_seat_and_other_g11_family_faults_have_texts(code, module):
    assert core.load_fault_texts()[code]["text_en"] == "Energy-saving mode active"
    assert core.load_module_names()[module]
