import os

import pytest

from n63read import core

CAT = core.load_catalog()


# Verified cases from PROJECT.md. They need no hardware.
@pytest.mark.parametrize("arg, did, raw, expected", [
    ("ILAG2", 0x588A, "1000", 1.0),
    ("ILAG1", 0x5889, "0FA0", 0.9765625),
    ("ILUZ5", 0x4A34, "FF70", -9.0),
    ("ILUZ5", 0x4A34, "0090", 9.0),
    ("ZAEHLER_AUSSETZERK_ZYL5", 0x4B31, "0002", 2),
])
def test_verified_measurements(arg, did, raw, expected):
    meta = CAT[arg]
    assert int(meta["ID"], 16) == did
    value, _ = core.decode(bytes.fromhex(raw), meta)
    assert value == pytest.approx(expected)


def test_v8_back_half_is_not_contiguous():
    # Cylinders 7-8 live in a separate block; a sequential loop misses them.
    assert int(CAT["ILUZ6"]["ID"], 16) == 0x4A35
    assert int(CAT["ILUZ7"]["ID"], 16) == 0x4A43


def test_non_scalar_returns_hex():
    meta = {"DATENTYP": "data[3]", "EINHEIT": "-"}
    assert core.decode(b"\x01\x02\x03", meta) == ("010203", "")


def test_enum_lookup_via_name_column():
    enums = core.load_enum_tables()
    meta = CAT["INFO_MOTORAUSGEHER"]
    table = enums[meta["NAME"].upper()]
    value, _ = core.decode(b"\x01", meta, enums, "text_de")
    assert value == table[1]["text_de"]
    # Without the tables, the number comes back unchanged (log format).
    assert core.decode(b"\x01", meta)[0] == 1


def test_enum_unmatched_value_falls_back_to_number():
    enums = core.load_enum_tables()
    value, _ = core.decode(b"\xEE", CAT["INFO_MOTORAUSGEHER"], enums)
    assert value == 0xEE


def test_every_data_file_is_locatable():
    for name in core.DATA_FILES:
        assert os.path.exists(core.resource_path(name)), name


@pytest.mark.parametrize("value, cell", [
    (None, ""), (1.5, 1.5), (-3, -3), ("3D position", "3D position"),
    ("=HYPERLINK(\"x\")", "'=HYPERLINK(\"x\")"), ("+1", "'+1"), ("-x", "'-x"),
    ("@SUM(A1)", "'@SUM(A1)"),
])
def test_log_cells_never_run_as_spreadsheet_formulas(value, cell):
    assert core.log_cell(value) == cell
