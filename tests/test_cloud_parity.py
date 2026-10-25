"""
The Worker's port of series.build and history.summarize (cloud/src/drive.js)
against the Python originals: cloud/test/parity holds two drive logs and
what Python makes of them, and the Node tests check drive.js against the
same file. When a summary or the series change on purpose, regenerate:

    python tests/test_cloud_parity.py
"""

import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from n63read import history, playback, series  # noqa: E402

PARITY = os.path.join(HERE, "..", "cloud", "test", "parity")
EXPECTED = os.path.join(PARITY, "expected.json")


def _fmt(v):
    return "" if v is None else ("%.4f" % v).rstrip("0").rstrip(".")


def _drive_log():
    """A two-bank drive with speed, fuel, tank, GPS, misfires and the rest."""
    names = ["INMOT", "INMOT@0x13", "FZG_GESCHWINDIGKEIT_VA_HA_FASCHWERP",
             "MASSENSTROM_KRAFTSTOFF", "MASSENSTROM_KRAFTSTOFF@0x13", "IKTFS",
             "HU_STATUS_GPS_POSITION.LATITUDE", "HU_STATUS_GPS_POSITION.LONGITUDE",
             "ZAEHLER_AUSSETZERK_ZYL1", "ZAEHLER_AUSSETZERK_ZYL5@0x13", "ILUZ3", "IUIBS",
             "ITOEL", "IMUL1", "IMUL1@0x13", "MITTELW_ZYLIND_ZWSPAET_KR_HALBQUANT",
             "DSC_RDC_MESSDATENBLOCK_1.LETZTER_REIFENDRUCKWERT", "GANG"]
    lines = [",".join(["Time", "Clock"] + names + ["Event"])]
    t = 0.0
    for i in range(240):
        t += 0.25 if i != 150 else 9.0                     # one gap longer than MAX_STEP
        sec = 50 + t
        clock = "2026-10-19 07:42:%06.3f-05:00" % sec if sec < 60 else \
            "2026-10-19 07:%02d:%06.3f-05:00" % (42 + int(sec // 60), sec % 60)
        speed = 0.0 if i < 20 else 75 + 25 * math.sin(i / 40)
        row = [700 + 900 * abs(math.sin(i / 30)), 705 + 890 * abs(math.sin(i / 30)), speed,
               1.1 + speed / 40, 1.05 + speed / 41, 52.0 - i * 0.002 + 0.3 * math.sin(i),
               0.0 if i < 10 else 32.7767 + i * 1e-5, 0.0 if i < 10 else -96.797 + i * 1.3e-5,
               (i // 60) if i < 200 else 0, i // 50, 3 * math.sin(i / 7),
               14100 + 50 * math.cos(i / 9), 60 + i * 0.1, 1.02 + 0.01 * math.sin(i / 11),
               0.98, -0.75 * abs(math.sin(i / 13)), 2.4 + 0.001 * i, None]
        cells = [_fmt(v) for v in row]
        cells[-1] = "D" if speed else "P"
        if i == 37:
            cells[3] = ""                                  # a blank cell
        event = "connected; odometer 41234 km" if i == 0 else ""
        line = ["%.3f" % t, clock] + cells + [event]
        if i == 120:
            line = line[:8]                                # a short row
        lines.append(",".join(line))
        if i == 60:
            lines.append("junk,row,,")                     # not a time: skipped
    return "\n".join(lines) + "\n"


def _gps_log():
    """No speedometer: distance from GPS; odometer from the Event column;
    bank 2 lambda from ILAG2."""
    names = ["INMOT", "HU_STATUS_GPS_POSITION.LATITUDE", "HU_STATUS_GPS_POSITION.LONGITUDE",
             "ILAG1", "ILAG2"]
    lines = [",".join(["Time", "Clock"] + names + ["Event"])]
    for i in range(30):
        t = i * 1.0
        clock = "2026-10-20T18:05:%02d.000+01:00" % i
        row = [800 + i, 51.5 + i * 2e-4, -0.12 + i * 3e-4, 0.99 + i * 0.001, 1.01]
        event = "odometer 41300 km" if i == 2 else ""
        lines.append(",".join(["%.1f" % t, clock] + [_fmt(v) for v in row] + [event]))
    return "\n".join(lines) + "\n"


LOGS = {"drive.csv": _drive_log, "gps.csv": _gps_log}


def _python_view(path, name):
    names, rows = playback.read_log(path)
    length = rows[-1][0] - rows[0][0]
    drive = history.summarize(names, rows, "logger/" + name, history._first_clock(path, length))
    if drive["odometer_km"] is None:
        drive["odometer_km"] = history._event_odometer(path)
    built = series.build(path)
    chunks = [{k: v for k, v in c.items() if k != "unit"}
              for ch in built["channels"] for c in series.chunks(ch)]
    return {"drive": drive, "series": {"start": built["start"], "approx": built["approx"],
                                       "chunks": chunks}}


def expected():
    return {name: _python_view(os.path.join(PARITY, name), name) for name in LOGS}


def write():
    os.makedirs(PARITY, exist_ok=True)
    for name, make in LOGS.items():
        with open(os.path.join(PARITY, name), "w", newline="", encoding="utf-8") as f:
            f.write(make())
    with open(EXPECTED, "w", encoding="utf-8") as f:
        json.dump(expected(), f, sort_keys=True)
        f.write("\n")


def test_logs_are_current():
    for name, make in LOGS.items():
        with open(os.path.join(PARITY, name), newline="", encoding="utf-8") as f:
            assert f.read() == make(), "%s is stale: python tests/test_cloud_parity.py" % name


def test_expected_matches_python():
    with open(EXPECTED, encoding="utf-8") as f:
        stored = json.load(f)
    assert stored == json.loads(json.dumps(expected())), \
        "expected.json is stale: python tests/test_cloud_parity.py"


if __name__ == "__main__":
    write()
    print("wrote", PARITY)
