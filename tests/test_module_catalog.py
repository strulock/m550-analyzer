"""tools/module_catalog.py on made-up tables: the .prg files can't be committed."""

import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import module_catalog as mc   # noqa: E402
import sgbd_tables            # noqa: E402

FUNC_HEAD = ["ARG", "ID", "RESULTNAME", "INFO", "EINHEIT", "L/H", "DATENTYP", "NAME",
             "MUL", "DIV", "ADD", "SERVICE", "ARG_TABELLE", "RES_TABELLE"]
RES_HEAD = ["RESULTNAME", "EINHEIT", "L/H", "DATENTYP", "MASKE", "NAME", "MUL", "DIV",
            "ADD", "INFO"]


def func(arg, did, service="22", result="-", dtype="-", name="-", res="-", unit="-",
         info="Info"):
    return [arg, did, result, info, unit, "high", dtype, name, "1.0", "10.0", "0.0",
            service, "-", res]


def res(result, dtype, name="-", unit="-", info="Feld"):
    return [result, unit, "high", dtype, "-", name, "1.0", "1.0", "0.0", info]


TABS = {
    "SG_FUNKTIONEN": (FUNC_HEAD, [
        func("STATUS_TEMP", "0xD001", result="STAT_TEMP_WERT", dtype="signed int",
             unit="°C", info="Temperatur"),
        func("STATUS_GANG", "0xD002", result="STAT_GANG", dtype="unsigned char",
             name="TAB_GANG", info="Gang"),
        func("STATUS_REC", "0xD003", res="RES_0xD003_D"),
        func("STEUERN_X", "0xD004", service="2E", result="STAT_X", dtype="unsigned char"),
        func("ROUTINE_Y", "0xD005", service="31", res="RES_0xD005_D"),
        func("STATUS_NIX", "0xD006"),                     # no result at all: skipped
    ]),
    "RES_0XD003_D": (RES_HEAD, [
        res("STAT_A_WERT", "unsigned int", unit="km/h"),
        res("STAT_B_TEXT", "string[4]"),
        res("-", "BITFIELD", name="BF_PORTS"),            # named from its table
    ]),
    "TAB_GANG": (["WERT", "TEXT"], [["0x01", "Gang 1"], ["0x02", "Gang 2"]]),
}


def test_build_expands_records_and_keeps_only_readable_jobs():
    rows, enums = mc.build(TABS, "TEST", "0x42", "T_")
    by = {r["ARG"]: r for r in rows}
    assert list(by) == ["T_STATUS_TEMP", "T_STATUS_GANG", "T_STATUS_REC.A",
                        "T_STATUS_REC.B_TEXT", "T_STATUS_REC.BF_PORTS"]
    assert by["T_STATUS_TEMP"]["DIV"] == "10.0" and by["T_STATUS_TEMP"]["MODULE"] == "0x42"
    assert [by[n]["FIELD"] for n in ("T_STATUS_REC.A", "T_STATUS_REC.B_TEXT",
                                     "T_STATUS_REC.BF_PORTS")] == [0, 1, 2]
    assert {by[n]["ID"] for n in by if n.startswith("T_STATUS_REC")} == {"0xD003"}
    assert by["T_STATUS_GANG"]["NAME"] == "TEST.TAB_GANG"
    assert by["T_STATUS_TEMP"]["INFO_EN"] == "Temperature"           # translated
    assert enums == [["TEST.TAB_GANG", "0x01", "Gang 1", "Gear 1"],
                     ["TEST.TAB_GANG", "0x02", "Gang 2", "Gear 2"]]


def test_build_refuses_duplicate_channel_names():
    tabs = dict(TABS)
    tabs["SG_FUNKTIONEN"] = (FUNC_HEAD, [func("DUP", "0xD001", result="R", dtype="unsigned char"),
                                         func("DUP", "0xD009", result="R", dtype="unsigned char")])
    with pytest.raises(SystemExit, match="duplicate channel names: T_DUP"):
        mc.build(tabs, "TEST", "0x42", "T_")


def test_build_skips_a_missing_result_table(capsys):
    tabs = {"SG_FUNKTIONEN": (FUNC_HEAD, [func("STATUS_REC", "0xD003", res="RES_GONE")])}
    assert mc.build(tabs, "TEST", "0x42", "T_") == ([], [])
    assert "RES_GONE missing" in capsys.readouterr().err


def test_head_unit_gets_the_hand_decoded_gps_rows():
    rows, _ = mc.build({"SG_FUNKTIONEN": (FUNC_HEAD, [])}, "NBTEVO", "0x63", "HU_")
    gps = [r for r in rows if r["ID"] == "0xD039"]
    assert [r["ARG"].split(".")[1] for r in gps] == ["VALIDITY", "LATITUDE", "LONGITUDE",
                                                      "HEIGHT"]
    assert [r["DATENTYP"] for r in gps] == ["unsigned char", "gps_dms", "gps_dms",
                                            "unsigned int"]


def test_fuel_pump_rows_carry_byte_positions_without_sg_funktionen():
    rows, enums = mc.build({}, "EKP301", "0x17", "EKP_")
    assert enums == [] and {r["ID"] for r in rows} == {"0x4010"}
    pos = {r["ARG"].split(".")[1]: r["POS"] for r in rows}
    assert pos["MOTOR_SPANNUNG_A"] == 2 and pos["SPANNUNG_KL30"] == 20 and pos["MOTOR_STROM_C"] == 44
    assert all(r["DATENTYP"] == "unsigned char" for r in rows)


@pytest.mark.parametrize("path, key", [
    ("00691315-nbtevo.prg", "nbtevo"),
    ("C:/x/8f4d427c-dsc_g30n.prg", "dsc_g30n"),
    ("bdc_g11.prg", "bdc_g11"),
    ("847ec575-ekp301.prg", "ekp301"),
])
def test_module_for_recognises_upload_names(path, key):
    assert mc.module_for(path) == key


@pytest.mark.parametrize("path", ["dsc_25.prg", "nbtevo.txt", "xnbtevo.prg"])
def test_module_for_rejects_unknown_files(path):
    with pytest.raises(SystemExit, match="don't know which module"):
        mc.module_for(path)


# ------------------------------------------------------- table directory

def sgbd_bytes(tables):
    """A file laid out like a deobfuscated SGBD: table bodies, then a directory
    of [name, NUL-padded to 64][offset][0][cols][rows] entries, back to back."""
    body, offsets = bytearray(b"\xff" * 16), {}
    for name, (head, rows) in tables.items():
        offsets[name] = len(body)
        for field in head + [f for r in rows for f in r]:
            body += field.encode("latin-1") + b"\x00"
    directory = bytearray()
    for name, (head, rows) in tables.items():
        directory += name.encode().ljust(64, b"\x00")
        directory += struct.pack("<IIII", offsets[name], 0, len(head), len(rows))
    return bytes(body + b"\xff" * 16 + directory)


TWO = {
    "TAB_A": (["WERT", "TEXT"], [["0x01", "eins"]]),
    "TAB_B": (["WERT", "TEXT"], [["0x02", "zwei"], ["0x03", "drei"]]),
}


def test_tables_pairs_each_name_with_its_own_contents():
    got = mc.tables(sgbd_bytes(TWO))
    assert got["TAB_A"] == (["WERT", "TEXT"], [["0x01", "eins"]])
    assert got["TAB_B"] == (["WERT", "TEXT"], [["0x02", "zwei"], ["0x03", "drei"]])


def test_the_old_directory_reader_pairs_names_with_neighbours():
    # Why module_catalog has its own reader: this one reads the 16 bytes
    # before each name, i.e. the previous entry's numbers.
    d = sgbd_bytes(TWO)
    entries = {n: (off, cols, rows) for n, off, cols, rows in sgbd_tables.table_directory(d)}
    assert "TAB_A" not in entries                     # its numbers went to TAB_B...
    off, cols, rows = entries["TAB_B"]
    assert sgbd_tables.read_table(d, off, cols, rows)[1] == [["0x01", "eins"]]   # ...A's rows


def test_deobfuscate_is_xor_f7(tmp_path):
    path = tmp_path / "x.prg"
    path.write_bytes(bytes(b ^ 0xF7 for b in b"SG_FUNKTIONEN"))
    assert mc.deobfuscate(str(path)) == b"SG_FUNKTIONEN"
