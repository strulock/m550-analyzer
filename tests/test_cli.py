"""CLI commands against the fake car: read, log, gps, and the data selftest."""

import csv
import time
import types

import pytest

from n63read import cli, core

from fake_car import FakeCar, HU_GPS


@pytest.fixture
def car(monkeypatch):
    c = FakeCar()
    # Every Hsfz the CLI opens goes to the fake car.
    monkeypatch.setattr(core.Hsfz.__init__, "__defaults__", (c.port, core.TESTER_ADDR, 3.0))
    yield c
    c.close()


def run(argv):
    cli.main(argv)


def test_read_prints_dme_and_head_unit_values(car, capsys):
    run(["read", "--ip", "127.0.0.1", "--timeout", "0.5",
         "ILAG2", "HU_STATUS_GPS", "HU_STATUS_SPEED.GPS_SPEED", "NOT_A_CHANNEL"])
    out = capsys.readouterr().out.splitlines()
    line = {l.split()[0]: l for l in out}
    assert "1.0" in line["ILAG2"].split() and "raw=1000" in line["ILAG2"]
    assert "3D position" in line["HU_STATUS_GPS"]               # enum text
    assert "98.0" in line["HU_STATUS_SPEED.GPS_SPEED"].split()  # field 5 of the record
    assert "UNKNOWN" in line["NOT_A_CHANNEL"]


def test_read_reports_a_refused_identifier(car, capsys):
    run(["read", "--ip", "127.0.0.1", "--timeout", "0.5", "ITOEL"])
    assert "requestOutOfRange" in capsys.readouterr().out


def test_log_writes_dme_and_head_unit_columns(car, tmp_path, monkeypatch, capsys):
    calls = []

    def sleep(_):
        calls.append(1)
        if len(calls) >= 3:
            raise KeyboardInterrupt
    monkeypatch.setattr(cli, "time", types.SimpleNamespace(time=time.time, sleep=sleep))
    out = tmp_path / "run.csv"
    names = ["ILAG1", "ILAG2", "HU_STATUS_GPS_POSITION.LATITUDE", "HU_STATUS_SPEED.GPS_SPEED"]
    run(["log", "--ip", "127.0.0.1", "--timeout", "0.5", "--interval", "0",
         "--out", str(out)] + names)
    assert "wrote 3 samples" in capsys.readouterr().out
    with open(out, encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["Time"] + names
    assert len(rows) == 4
    for row in rows[1:]:
        values = dict(zip(rows[0], row))
        assert float(values["ILAG2"]) == 1.0
        assert float(values["ILAG1"]) == pytest.approx(0.9765625)
        # Head unit values arrive on the first cycle and fill every row.
        assert float(values["HU_STATUS_GPS_POSITION.LATITUDE"]) == pytest.approx(30.1346, abs=1e-4)
        assert float(values["HU_STATUS_SPEED.GPS_SPEED"]) == 98


def test_log_rejects_an_unknown_channel(car, tmp_path):
    with pytest.raises(SystemExit, match="unknown channel"):
        run(["log", "--ip", "127.0.0.1", "--out", str(tmp_path / "x.csv"), "NOPE"])


def test_gps_prints_fix_position_and_raw_bytes(car, capsys):
    run(["gps", "--ip", "127.0.0.1", "--timeout", "0.5"])
    out = capsys.readouterr().out
    assert out.startswith("3D position")
    assert "valid" in out and "INVALID" not in out
    assert "lat 30.134600" in out and "lon -97.635900" in out and "height 160" in out
    assert "raw=" + HU_GPS.hex() in out


def test_gps_says_when_the_module_refuses(car):
    with pytest.raises(SystemExit, match="refused"):
        run(["gps", "--ip", "127.0.0.1", "--timeout", "0.5", "--target", "0x12"])


def refuse(self):
    # As an absent car does, but at once: a real refusal on Windows takes ~2 s.
    raise ConnectionRefusedError("refused")


def test_no_car_is_a_message_not_a_traceback(monkeypatch):
    monkeypatch.setattr(core.Hsfz, "connect", refuse)
    with pytest.raises(SystemExit, match="cannot talk to 127.0.0.1"):
        run(["read", "--ip", "127.0.0.1", "ILAG2"])


# ------------------------------------------------------------------ selftest

def test_selftest_passes_with_every_file(capsys):
    run(["selftest"])
    out = capsys.readouterr().out
    assert "selftest ok" in out
    for name in core.MODULE_CATALOGS:
        assert name + "_measurements.csv" in out


def test_selftest_fails_when_a_module_catalog_is_missing(monkeypatch, tmp_path, capsys):
    real = core.resource_path

    def resource_path(name):
        return str(tmp_path / "gone.csv") if name == "GSZFB1_measurements.csv" else real(name)
    monkeypatch.setattr(core, "resource_path", resource_path)
    with pytest.raises(SystemExit) as e:
        run(["selftest"])
    assert "GSZFB1_measurements.csv: not found" in str(e.value)
    assert "MISSING" in capsys.readouterr().out


def test_selftest_fails_on_an_empty_catalog(monkeypatch, tmp_path):
    empty = tmp_path / "empty.csv"
    empty.write_text("ARG,ID\n", encoding="utf-8")
    real = core.resource_path
    monkeypatch.setattr(core, "resource_path",
                        lambda n: str(empty) if n == core.DEFAULT_CSV else real(n))
    with pytest.raises(SystemExit) as e:
        run(["selftest"])
    assert core.DEFAULT_CSV + ": no rows" in str(e.value)


def test_scan_uses_the_probe_timeout(car, monkeypatch, capsys):
    t = time.time()
    run(["scan", "--ip", "127.0.0.1", "--range", "0x0E", "0x14", "--probe-timeout", "0.1"])
    out = capsys.readouterr().out
    assert "0x10  responds" in out and "0x12  responds" in out
    assert "waiting 0.1 s per address" in out
    assert time.time() - t < 3                        # 5 empty addresses at 0.1 s


def test_scan_defaults_to_the_full_sweep_range():
    args = cli.build_parser().parse_args(["scan", "--ip", "x"])
    assert tuple(args.range) == core.SWEEP_RANGE
    assert args.probe_timeout == core.PROBE_TIMEOUT


def test_faults_all_takes_a_probe_timeout(car, monkeypatch, capsys):
    monkeypatch.setattr(core, "SWEEP_RANGE", (0x0E, 0x14))
    run(["faults", "--ip", "127.0.0.1", "--all", "--probe-timeout", "0.1"])
    out = capsys.readouterr().out
    assert "waiting 0.1 s per address" in out
    assert "fault(s) across" in out


@pytest.mark.parametrize("value", ["0", "abc", "60"])
def test_probe_timeout_is_validated(value, capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["scan", "--ip", "x", "--probe-timeout", value])
    assert "probe" in capsys.readouterr().err
