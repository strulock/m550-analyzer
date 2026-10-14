import csv

import pytest

from n63read import cli, core

from fake_car import FakeCar
from test_head_unit import FakeH

CAT = core.load_module_catalogs()
LAYOUTS = core.record_layouts(CAT)
SPEED = (0x63, 0xD030)


def test_every_module_catalog_loaded():
    modules = {int(r["MODULE"], 16) for r in CAT.values()}
    assert modules == set(core.MODULE_LABELS)


def test_new_module_channels_exist():
    for name in ("EGS_ISTGANG.ISTGANG", "EGS_GETRIEBEOELTMEPERATUR",
                 "EGS_EINAUSGANG_DREHZAHL.EINGANGSDREHZAHL",
                 "ACSM__IMU_WERTE.X_ACC_SCAL",
                 "DSC_RDC_MESSDATENBLOCK_1.LETZTER_REIFENDRUCKWERT",
                 "DSC_GESCHWINDIGKEIT_RAD.GESCHWINDIGKEIT_RAD_VL",
                 "BDC_TANK_FUELLSTAND_LINKS", "BDC_STATUS_FES_MODUS"):
        assert name in CAT, name


def test_fault_texts_cover_the_new_modules():
    texts = core.load_fault_texts()
    tags = {t for e in texts.values() for t in e["source_module"].split("; ")}
    for tag in ("NBTEVO", "GSZFB1", "ACSM5", "DSC-G30N", "BDC-G11"):
        assert tag in tags
    assert all(e["text_en"] for e in texts.values() if e["text_de"])


def test_expected_size():
    assert core.expected_size(LAYOUTS[SPEED]) == (14, True)
    assert core.expected_size([{"DATENTYP": "unsigned char"}, {"DATENTYP": "string"},
                               {"DATENTYP": "unsigned int"}]) == (1, False)
    assert core.expected_size([{"DATENTYP": "data[5]"}, {"DATENTYP": "BITFIELD"}]) == (5, False)


def test_check_layouts_adds_the_dme_at_the_target():
    dme = {"X": {"ARG": "X", "ID": "0x588A", "DATENTYP": "unsigned int"}}
    layouts = core.check_layouts(dict(dme, **CAT), 0x13)
    assert layouts[(0x13, 0x588A)] == [dme["X"]]
    assert layouts[SPEED] == LAYOUTS[SPEED]


def test_check_identifier_outcomes():
    fields = LAYOUTS[SPEED]
    cases = {b"\x00" * 14: "ok", b"\x00" * 12: "size",
             core.NegativeResponse(0x22, 0x31): "refused",
             core.HsfzTimeout("timed out"): "no answer"}
    for answer, want in cases.items():
        status, _, _ = core.check_identifier(FakeH({SPEED: answer}), SPEED, fields)
        assert status == want


def test_check_verdict():
    assert core.check_verdict({"ok": 95, "size": 5, "refused": 40}).startswith("fits")
    assert core.check_verdict({"ok": 3, "size": 20}).startswith("does NOT fit")
    assert core.check_verdict({"ok": 6, "size": 4}).startswith("unclear")
    assert core.check_verdict({"refused": 9}).startswith("no identifiers answered")


def test_cli_check_against_the_fake_car(tmp_path, monkeypatch, capsys):
    car = FakeCar()
    try:
        monkeypatch.setattr(core.Hsfz.__init__, "__defaults__",
                            (car.port, core.TESTER_ADDR, 3.0))
        out = tmp_path / "check.csv"
        cli.main(["check", "--ip", "127.0.0.1", "--timeout", "0.5",
                  "--module", "0x63", "--out", str(out)])
    finally:
        car.close()
    text = capsys.readouterr().out
    assert "Head unit (0x63)" in text and "ok 3" in text
    with open(out, encoding="utf-8-sig") as f:
        rows = {r["did"]: r for r in csv.DictReader(f)}
    assert rows["0xD039"]["status"] == "ok" and rows["0xD039"]["reply_bytes"] == "13"
    assert rows["0xD030"]["status"] == "ok"
    assert rows["0xD20E"]["raw"] == "(private)"          # Wi-Fi password record
    assert rows["0xD00D"]["status"] == "refused"


def test_a_missing_module_costs_the_short_timeout_not_the_full_one():
    import time
    car = FakeCar()                                 # has no transmission (0x18)
    try:
        with core.Hsfz("127.0.0.1", port=car.port, timeout=2.0) as h:
            gear = CAT["EGS_ISTGANG.ISTGANG"]
            r = core.ChannelReader(LAYOUTS, 0x12)
            t0 = time.time()
            s = r.read(h, [("EGS_ISTGANG.ISTGANG", 0xDA2E, gear)])
            took = time.time() - t0
    finally:
        car.close()
    assert took < core.MODULE_TIMEOUT + 0.5
    assert s["EGS_ISTGANG.ISTGANG"][2] == "module not answering"


def test_every_fast_identifier_is_in_a_catalog():
    assert core.FAST_DIDS <= set(LAYOUTS)


def test_steering_suspension_transfer_case_and_climate_channels():
    for name in ("EPS_EPS_MOMENTENSENSOR", "VDP_STATUS_HOEHENSTAENDE_LESEN",
                 "LMV_LMV_MOMENT", "IHKA_TEMPERATURSENSOREN_KLIMA"):
        assert any(n == name or n.startswith(name + ".") for n in CAT), name


def test_fault_merge_rewords_only_within_a_module_family(tmp_path, monkeypatch):
    import os
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
    import module_catalog as mc
    path = tmp_path / mc.FAULT_CSV
    path.write_text("code,text_de,text_en,source_module\n"
                    "401111,alt,old,BDC-G05\n"
                    "801230,DME Text,DME text,DME; DME-slave\n", encoding="utf-8-sig")
    monkeypatch.setattr(mc, "DATA", str(tmp_path))
    ft = (["ORT", "ORTTEXT"], [["0x401111", "neu"], ["0x801230", "IHKA Text"],
                               ["0x781234", "Neuer Fehler"]])
    assert mc.merge_faults({"FORTTEXTE": ft}, "BDC-G11") == (1, 0, 1, 1)
    with open(path, encoding="utf-8-sig") as f:
        rows = {r["code"]: r for r in csv.DictReader(f)}
    assert rows["401111"]["text_de"] == "neu" and rows["401111"]["source_module"] == "BDC-G11"
    assert rows["801230"]["text_de"] == "DME Text"      # another module's code: untouched
    assert rows["781234"]["source_module"] == "BDC-G11"
