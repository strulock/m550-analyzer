"""history: drive statistics and fault history from the logs folder."""

import csv
import json
import os

import pytest

from n63read import cli, core, history, mcp

LAT0, LON0 = 30.1346, -97.6359          # Circuit of the Americas, not anyone's home
B2 = core.BANK2_SUFFIX


def write_log(path, columns, rows, clock="2026-10-19 07:00:00.000"):
    """A drive log as the app writes it: Time, Clock, channels, Event."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Time", "Clock"] + columns + ["Event"])
        for i, row in enumerate(rows):
            w.writerow([row[0], clock if i == 0 else "x"] + list(row[1:]) + [""])


def drive(path, seconds=60, misfire5_at=(20, 40)):
    """A minute at 72 km/h, cylinder 5 misfiring twice, both banks logged."""
    cols = ["FZG_GESCHWINDIGKEIT_VA_HA_FASCHWERP", "INMOT", "ZAEHLER_AUSSETZERK_ZYL1",
            "ZAEHLER_AUSSETZERK_ZYL5", "ILUZ5", "DIAG_BRENNDAUER_ZUENDFUNKE_CYL1",
            "DIAG_BRENNDAUER_ZUENDFUNKE_CYL1" + B2, "MITTELW_ZYLIND_ZWSPAET_KR_HALBQUANT",
            "IMUL1", "IMUL1" + B2, "IUIBS", "DSC_RDC_MESSDATENBLOCK_2.LETZTER_REIFENDRUCKWERT",
            history.LAT, history.LON]
    rows = []
    for t in range(seconds):
        # The counter is a window: it rises on a misfire and falls back later.
        counter = 1 if any(a <= t < a + 5 for a in misfire5_at) else 0
        rows.append((t, 72.0, 1500 + t, 0, counter, (-1) ** t * 3.0, 1.6, 1.4, -1.5,
                     1.05, 1.02, 14000 - t, 2.4,
                     0 if t < 5 else LAT0 + t * 1e-4, 0 if t < 5 else LON0))
    write_log(path, cols, rows)


def test_a_drive_becomes_one_row_of_statistics(tmp_path):
    path = str(tmp_path / "n63_a.csv")
    drive(path)
    names, rows = history.playback.read_log(path)
    d = history.summarize(names, rows, "n63_a.csv", "2026-10-19 07:00:00")
    assert d["minutes"] == pytest.approx(59 / 60, abs=0.01)
    assert d["km"] == pytest.approx(72 * 59 / 3600, rel=0.01)       # from the speedometer
    assert d["misfires_5"] == 2 and d["misfires_1"] == 0 and d["misfires"] == 2
    assert history.misfire_text(d) == "cyl 5 x2"
    assert d["uneven_5"] == pytest.approx(3.0, rel=0.05)              # spread, not mean
    assert d["spark_1"] == 1.6 and d["spark_5"] == 1.4               # 5 is bank 2's 1
    assert d["knock_b1"] == -1.5 and d["knock_b2"] is None           # bank 2 not logged
    assert d["trim_b1"] == pytest.approx(5.0) and d["trim_b2"] == pytest.approx(2.0)
    assert d["battery_min"] == pytest.approx(13.941)                  # mV to V
    assert d["tire_2"] == 2.4 and d["tire_1"] is None
    assert d["gps_points"] == 55                                      # 0/0 is no fix
    assert d["rpm_max"] == 1559


def test_distance_from_gps_when_no_speed_was_logged(tmp_path):
    path = str(tmp_path / "gps.csv")
    write_log(path, [history.LAT, history.LON],
              [(t, LAT0 + t * 0.001, LON0) for t in range(11)])
    names, rows = history.playback.read_log(path)
    assert history.summarize(names, rows)["km"] == pytest.approx(10 * 0.1112, rel=0.01)


def test_a_counter_that_resets_still_counts_each_rise():
    assert history._aggregate("increase", [(0, 3), (1, 4), (2, 0), (3, 2)]) == 3.0


def fault_csv(path, read_at, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(core.FAULT_CSV_HEADER)
        for module, code, state in rows:
            w.writerow([read_at, module, "Module %s" % module, code, "0x2F" if code else "",
                        state, "Fault %s" % code if code else "", "", ""])


def test_fault_history_follows_each_fault_across_reads(tmp_path):
    a = str(tmp_path / "f1.csv")
    b = str(tmp_path / "f2.csv")
    fault_csv(a, "2026-10-18 18:00:00", [("0x29", "4806DA", "present, stored"),
                                         ("0x40", "804415", "present, stored"),
                                         ("0x12", "", "no faults")])
    fault_csv(b, "2026-10-19 07:00:00", [("0x29", "48071F", "present, stored"),
                                         ("0x40", "804415", "not present, stored"),
                                         ("0x12", "1A400A", "not present")])
    faults = history.fault_history(history.read_faults(a) + history.read_faults(b))
    by = {f["code"]: f for f in faults}
    assert by["4806DA"]["now"] == "gone"                 # not listed in the newer read
    assert by["48071F"]["now"] == "present"
    assert by["804415"]["now"] == "stored" and by["804415"]["reads_seen"] == 2
    assert by["804415"]["first_seen"] == "2026-10-18 18:00:00"
    assert by["804415"]["reads_present"] == 1
    assert by["1A400A"]["now"] == "stored"
    assert faults[0]["now"] == "present"                  # present ones first


def test_codes_that_never_failed_are_not_faults(tmp_path):
    path = str(tmp_path / "every.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(core.FAULT_CSV_HEADER)
        w.writerow(["2026-10-14 19:10:00", "0x12", "DME", "1A400A", "0x20", "not present",
                    "fan", "", ""])
        w.writerow(["2026-10-14 19:10:00", "0x12", "DME", "1A4000", "0x00", "not present",
                    "never", "", ""])
        w.writerow(["2026-10-14 19:10:00", "0x12", "DME", "1A4001", "0x50", "not present",
                    "not tested", "", ""])
    faults = history.fault_history(history.read_faults(path))
    assert [f["code"] for f in faults] == ["1A400A"]
    assert history.failed("") and history.failed("0x08") and not history.failed("0x40")


def test_the_same_fault_list_saved_twice_counts_once(tmp_path):
    rows = [("0x40", "804415", "present, stored")]
    fault_csv(str(tmp_path / "a.csv"), "2026-10-18 18:00:00", rows)
    fault_csv(str(tmp_path / "b.csv"), "2026-10-18 18:00:00", rows)
    faults = history.fault_history(history.read_faults(str(tmp_path / "a.csv"))
                                   + history.read_faults(str(tmp_path / "b.csv")))
    assert faults[0]["reads_seen"] == 1


def test_scan_reads_the_logs_folder_and_writes_the_csvs(tmp_path):
    logs = str(tmp_path / "logs")
    drive(os.path.join(logs, "n63_1.csv"))
    drive(os.path.join(logs, "claude", "n63_2.csv"), misfire5_at=(10,))
    fault_csv(os.path.join(logs, "n63_faults.csv"), "2026-10-19 07:00:00",
              [("0x40", "804415", "present, stored")])
    with open(os.path.join(logs, "check.csv"), "w") as f:      # a catalog check: no drive
        f.write("module,did,job,status,detail,reply_bytes,raw\n0x12,0x4807,X,ok,,2,0000\n")
    s = history.scan(logs)
    assert [d["misfires"] for d in s.drives] == [2, 1]
    assert [d["file"] for d in s.drives] == ["n63_1.csv", "claude/n63_2.csv"]
    assert s.faults[0]["code"] == "804415" and not s.errors
    with open(os.path.join(s.folder, history.DRIVES_CSV), encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2 and rows[0]["misfires_5"] == "2.0"
    with open(os.path.join(s.folder, history.FAULTS_CSV), encoding="utf-8") as f:
        assert next(csv.DictReader(f))["now"] == "present"


def test_a_rescan_only_reads_new_or_changed_files(tmp_path, monkeypatch):
    logs = str(tmp_path / "logs")
    drive(os.path.join(logs, "n63_1.csv"))
    history.scan(logs)
    read = []
    real = history.playback.read_log
    monkeypatch.setattr(history.playback, "read_log", lambda p: read.append(p) or real(p))
    assert len(history.scan(logs).drives) == 1 and read == []
    drive(os.path.join(logs, "n63_2.csv"))
    assert len(history.scan(logs).drives) == 2 and len(read) == 1
    os.remove(os.path.join(logs, "n63_1.csv"))
    assert len(history.scan(logs).drives) == 1           # a deleted log drops out


def test_an_old_index_is_read_again_from_scratch(tmp_path):
    logs = str(tmp_path / "logs")
    drive(os.path.join(logs, "n63_1.csv"))
    s = history.scan(logs)
    with open(os.path.join(s.folder, history.INDEX_JSON), "w") as f:
        json.dump({"version": 0, "files": {}}, f)
    assert len(history.scan(logs).drives) == 1


def test_cli_history_prints_drives_and_faults(tmp_path, capsys):
    logs = core.logs_dir()
    drive(os.path.join(logs, "n63_1.csv"))
    fault_csv(os.path.join(logs, "n63_faults.csv"), "2026-10-19 07:00:00",
              [("0x40", "804415", "present, stored")])
    cli.main(["history"])
    out = capsys.readouterr().out
    assert "cyl 5 x2" in out and "804415" in out and "present" in out
    assert history.DRIVES_CSV in out


def test_claude_can_ask_for_the_history(tmp_path):
    logs = core.logs_dir()
    drive(os.path.join(logs, "n63_1.csv"))
    text = mcp.HANDLERS["drive_history"]({"limit": 5})
    data = json.loads(text)
    assert data["drives"][0]["misfires_5"] == 2
    assert "faults" in data and data["folder"].endswith(history.FOLDER)


# ------------------------------------------------------------ the window

def test_the_history_window_lists_drives_trends_and_faults(app):
    from conftest import pump
    logs = core.logs_dir()
    drive(os.path.join(logs, "n63_1.csv"))
    fault_csv(os.path.join(logs, "n63_faults.csv"), "2026-10-19 07:00:00",
              [("0x40", "804415", "present, stored")])
    app._open_history()
    win = app.history_window
    try:
        assert pump(app, lambda: win.summary is not None)
        rows = win.drives.get_children()
        assert len(rows) == 1 and "cyl 5 x2" in win.drives.item(rows[0], "values")
        assert win.faults.item(win.faults.get_children()[0], "values")[0] == "present"
        win.trend_var.set(history.TRENDS[0][0])       # misfires per cylinder
        win.update()
        win.draw_trend()
        kinds = [win.chart.type(i) for i in win.chart.find_all()]
        assert "oval" in kinds and "text" in kinds
        opened = []
        win.on_open = opened.append
        win.drives.selection_set(rows[0])
        win._open_drive(type("E", (), {"y": win.drives.bbox(rows[0])[1] + 2})())
        assert opened == [os.path.join(logs, "n63_1.csv")]
    finally:
        win.destroy()
        app.history_window = None


def test_a_faults_stored_details_follow_it_into_the_history(tmp_path):
    details = {"snapshots": [
        {"record": 1, "fields": [{"id": 0x1700, "label": "Odometer", "value": 107031,
                                  "unit": "km", "text": "", "name": ""}], "rest": ""},
        {"record": 2, "fields": [{"id": 0x1700, "label": "Odometer", "value": 111321,
                                  "unit": "km", "text": "", "name": ""}], "rest": ""}],
        "extended": [{"record": 2, "name": "HFK", "label": "times it occurred", "value": 3}],
        "errors": []}
    cols = core.detail_columns(details)
    assert (cols["first_km"], cols["last_km"], cols["occurrences"]) == (107031, 111321, 3)
    path = str(tmp_path / "f.csv")
    row = {"read_at": "2026-10-19 20:08:31", "module": "0x29", "module_name": "DSC",
           "code": "4806DA", "status": "0x68", "state": "not present, stored",
           "text_en": "No reception, front right", "text_de": "", "catalog_source": ""}
    core.write_fault_csv(path, [dict(row, **cols)])
    f, = history.fault_history(history.read_faults(path))
    assert f["last_km"] == 111321 and f["occurrences"] == 3
    assert f["details"]["snapshots"][1]["fields"][0]["value"] == 111321


def test_a_log_without_a_clock_starts_its_length_before_the_file_time(tmp_path):
    logs = str(tmp_path / "logs")
    os.makedirs(logs)
    path = os.path.join(logs, "old.csv")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(["Time,INMOT"] + ["%d,800" % t for t in range(0, 600, 60)]) + "\n")
    import time as _t
    end = _t.mktime((2026, 10, 14, 22, 43, 38, 0, 0, -1))
    os.utime(path, (end, end))
    d, = history.scan(logs).drives
    assert d["start"] == "2026-10-14 22:34:38"             # 9 minutes of log before


def test_the_odometer_is_read_once_a_log_starts_and_history_finds_it(tmp_path, monkeypatch):
    class H:
        def __init__(self, answers):
            self.answers, self.asked = answers, []

        def read_did(self, module, did, timeout=None):
            self.asked.append((module, did))
            a = self.answers.get((module, did))
            if isinstance(a, Exception):
                raise a
            if a is None:
                raise core.HsfzTimeout("no answer")
            return a
    monkeypatch.setattr(core, "ODOMETERS", [(0x19, 0xDFE7, "X.MILE_KM"), (0x76, 0x2540, None)])
    layouts = {(0x19, 0xDFE7): [{"ARG": "X.MILE_KM", "DATENTYP": "unsigned long"}]}
    h = H({(0x19, 0xDFE7): (111321).to_bytes(4, "big")})
    assert core.read_odometer(h, layouts) == 111321
    h = H({(0x19, 0xDFE7): core.NegativeResponse(0x22, 0x31),
           (0x76, 0x2540): bytes.fromhex("01b2c8")})
    assert core.read_odometer(h, layouts) == 111304 and h.asked[-1] == (0x76, 0x2540)
    assert core.read_odometer(H({}), layouts) is None

    # A log that noted it in its first row: History takes it as the drive's.
    logs = str(tmp_path / "logs")
    os.makedirs(logs)
    with open(os.path.join(logs, "n63_1.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Time", "Clock", "INMOT", "Event"])
        w.writerow([0.1, "2026-10-21 07:00:00.000-05:00", 800, "odometer 111321 km"])
        for t in range(1, 10):
            w.writerow([t, "x", 800, ""])
    assert history.scan(logs).drives[0]["odometer_km"] == 111321


def test_a_session_notes_the_odometer_in_the_first_row(tmp_path, monkeypatch):
    from n63read import engine
    from fake_car import FakeCar
    car = FakeCar()
    monkeypatch.setattr(core.Hsfz.__init__, "__defaults__", (car.port, core.TESTER_ADDR, 3.0))
    monkeypatch.setattr(core, "read_odometer", lambda h, layouts: 111321)
    path = str(tmp_path / "log.csv")
    s = engine.Session("127.0.0.1", interval=0.02)
    cat = core.load_catalog()
    s.set_channels([("ILAG2", int(cat["ILAG2"]["ID"], 16), cat["ILAG2"])])
    s.stop_after = 3
    s.start_log(path)
    s.start()
    s.join(10)
    car.close()
    with open(path, encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert rows[1][-1] == "odometer 111321 km" and all(r[-1] == "" for r in rows[2:])
