"""series: a drive log as one UTC time series per channel."""

import os
import time

import pytest

from n63read import core, series


def write(path, text):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return path


def test_a_log_with_the_utc_offset(tmp_path):
    p = write(str(tmp_path / "a.csv"),
              "Time,Clock,INMOT,ILUZ5@0x13,HU_STATUS_GPS,Event\n"
              "0.5,2026-10-19 07:42:50.500-05:00,800,1.5,12,\n"
              "1.0,2026-10-19 07:42:51.000-05:00,820,,12,alert\n"
              "1.5,x,840,-2.0,12,\n")
    s = series.build(p)
    assert s["approx"] is False
    assert s["start"] == pytest.approx(core.clock_epoch("2026-10-19 12:42:50Z"), abs=0.001)
    by = {c["name"]: c for c in s["channels"]}
    assert by["INMOT"]["points"][0][0] == pytest.approx(1792413770.5, abs=0.001)
    assert [v for _, v in by["INMOT"]["points"]] == [800, 820, 840]
    assert [v for _, v in by["ILUZ5@0x13"]["points"]] == [1.5, -2.0]      # blanks skipped
    assert by["INMOT"]["unit"] == "1/min" and by["ILUZ5@0x13"]["unit"] == "rad/s²"
    assert by["HU_STATUS_GPS"]["unit"] == ""              # a value table, not a unit
    assert "Event" not in by and "Clock" not in by


def test_an_old_log_is_this_pcs_local_time(tmp_path):
    p = write(str(tmp_path / "old.csv"), "Time,Clock,ITOEL,Event\n"
                                         "0.0,2026-10-14 19:23:46.000,80,\n1.0,x,81,\n")
    s = series.build(p)
    assert s["start"] == pytest.approx(time.mktime((2026, 10, 14, 19, 23, 46, 0, 0, -1)),
                                       abs=0.001)


def test_no_clock_takes_the_files_time_and_says_so(tmp_path):
    p = write(str(tmp_path / "bimmerlink.csv"), "Time;Engine speed (rpm)\n0;800\n10;900\n")
    os.utime(p, (1792413770, 1792413770))
    s = series.build(p)
    assert s["approx"] is True and s["start"] == pytest.approx(1792413760)
    assert s["channels"][0]["name"] == "Engine speed (rpm)"


def test_long_channels_are_stored_in_pieces(monkeypatch):
    monkeypatch.setattr(series, "CHUNK", 3)
    pts = [(100.0 + i, float(i)) for i in range(7)]
    parts = list(series.chunks({"name": "X", "unit": "V", "points": pts}))
    assert [p["n"] for p in parts] == [3, 3, 1]
    assert parts[1]["t0"] == 103.0 and parts[1]["data"][0] == [0.0, 3.0]
    assert (parts[1]["min"], parts[1]["max"], parts[1]["mean"]) == (3.0, 5.0, 4.0)
