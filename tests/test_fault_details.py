"""What a module stored with a fault, and reading several values per request."""


import pytest

from n63read import cli, core, mcp

from conftest import pump
from fake_car import FakeCar


@pytest.fixture
def car(monkeypatch):
    c = FakeCar()
    monkeypatch.setattr(core.Hsfz.__init__, "__defaults__", (c.port, core.TESTER_ADDR, 3.0))
    yield c
    c.close()


# ------------------------------------------------------------ the catalog

@pytest.mark.parametrize("datatype, size", [
    ("0xFF", 1), ("0xFFFFFF", 3), ("0xFFFFFFFFFFFF", 6), ("u char", 1),
    ("unsigned int", 2), ("signed long", 4), ("6", 6), ("float", 4), ("weird", None), ("", None),
])
def test_field_sizes(datatype, size):
    assert core.field_bytes(datatype) == size


def test_the_freeze_catalog_has_the_odometer_and_the_dsc_exception():
    fields = core.load_freeze_fields()
    assert fields[(None, 0x1700)]["name_de"] == "KM_STAND"
    assert core.field_bytes(fields[(None, 0x1769)]["datatype"]) == 6
    assert core.field_bytes(fields[(0x29, 0x1769)]["datatype"]) == 4
    assert all(k[1] in core.FREEZE_LABELS for k in fields if k[0] is None)
    assert fields[(0x29, 0x1769)]["module"] == "0x29"            # kept beside the DSC's own
    # The DME's own fields, for both DMEs, with real English.
    assert fields[(0x12, 0x4300)] is fields[(0x13, 0x4300)]
    assert fields[(0x12, 0x4300)]["name_en"] == "Engine temperature"


# ------------------------------------------------------------ the parsers

HEAD4 = bytes.fromhex("5904123456" "2f")
HEAD6 = bytes.fromhex("5906123456" "2f")


def test_a_snapshot_is_decoded_field_by_field():
    body = bytes.fromhex("0106" "1700" "0186a0" "1701" "00001234" "1702" "030010"
                         "1731" "04" "1750" "0a" "1751" "000001")
    rec, = core.parse_snapshots(HEAD4 + body, 0x12)
    got = {f["id"]: f for f in rec["fields"]}
    assert got[0x1700]["value"] == 100000 and got[0x1700]["unit"] == "km"
    assert got[0x1701]["value"] == 0x1234 and got[0x1701]["unit"] == "s"
    assert got[0x1702]["value"] == "030010"                      # a code, kept as hex
    assert got[0x1731]["text"] == "check immediately"
    assert got[0x1750]["text"] == "driving"
    assert rec["rest"] == ""
    de, = core.parse_snapshots(HEAD4 + body, 0x12, lang="text_de")
    assert {f["id"]: f for f in de["fields"]}[0x1750]["text"] == "Fahren"


def test_a_modules_own_value_stops_the_split_and_is_kept_raw():
    body = bytes.fromhex("0103" "1700" "000010" "0001" "1234" "1731" "01")
    rec, = core.parse_snapshots(HEAD4 + body, 0x12)
    assert [f["id"] for f in rec["fields"]] == [0x1700]
    assert rec["rest"] == "000112341731" "01"


def FIELDS_WITHOUT_BDC():
    """The snapshot catalog as if the body module's own fields were unknown."""
    return {k: v for k, v in core.load_freeze_fields().items() if k[0] != 0x40}


def test_a_later_snapshot_after_a_modules_own_values_is_still_found():
    # The BDC's light switch fault at the car: its own values (0x4040,
    # 0x4042) sit in each record, and record 2 holds the latest odometer.
    body = bytes.fromhex("0104" "1700" "01ad7b" "1701" "0fb00ff5" "4040" "8b" "4042" "0a"
                         "0204" "1700" "01b2c8" "1701" "0fe22bb4" "4040" "8c" "4042" "0a")
    one, two = core.parse_snapshots(HEAD4 + body, 0x40, fields=FIELDS_WITHOUT_BDC())
    assert [f["value"] for f in one["fields"]] == [109947, 0x0FB00FF5]
    assert one["rest"] == "40408b40420a"
    assert two["record"] == 2 and two["fields"][0]["value"] == 111304
    assert two["rest"] == "40408c40420a"


def test_the_dmes_own_values_are_decoded():
    # Engine temperature: signed, 0.1 degree steps, -273.14 offset; a value
    # table; and German labels when asked for.
    body = bytes.fromhex("0102" "4300" "0e9c" "4ABC" "1234")
    rec, = core.parse_snapshots(HEAD4 + body, 0x13)
    temp, air = rec["fields"]
    assert temp["label"] == "Engine temperature" and temp["unit"] == "°C"
    assert temp["value"] == pytest.approx(0x0E9C * 0.1 - 273.14)
    assert air["value"] == pytest.approx(466) and air["unit"] == "kg/h"
    de, = core.parse_snapshots(HEAD4 + body, 0x12, lang="text_de")
    assert de["fields"][0]["label"] == "Motor-Temperatur"
    neg, = core.parse_snapshots(HEAD4 + bytes.fromhex("0101" "4300" "fffe"), 0x12)
    assert neg["fields"][0]["value"] == pytest.approx(-0.2 - 273.14)   # signed


def test_the_dsc_stores_a_shorter_time():
    body = bytes.fromhex("0101" "1769" "00000064")
    rec, = core.parse_snapshots(HEAD4 + body, 0x29)
    assert rec["fields"][0]["value"] == 100 and rec["rest"] == ""
    rec, = core.parse_snapshots(HEAD4 + body, 0x12)            # elsewhere it's 6 bytes
    assert rec["fields"] == [] and rec["rest"] == "176900000064"


def test_several_snapshots():
    body = bytes.fromhex("0101" "1700" "000001" "0201" "1700" "000002")
    recs = core.parse_snapshots(HEAD4 + body, 0x12)
    assert [(r["record"], r["fields"][0]["value"]) for r in recs] == [(1, 1), (2, 2)]


def test_details_wait_longer_than_a_value_read():
    class H:
        timeout = 2.0
        seen = []

        def _set_timeout(self, s):
            self.timeout = s

        def read_snapshots(self, module, code):
            self.seen.append(self.timeout)
            raise core.HsfzTimeout("timed out")
        read_extended = read_snapshots
    h = H()
    core.fault_details(h, 0x12, "1A400A")
    assert h.seen == [core.DETAILS_TIMEOUT] * 2 and h.timeout == 2.0


def test_the_counters():
    got = core.parse_extended(HEAD6 + bytes.fromhex("0100" "0207" "0329"))
    assert [(e["name"], e["value"]) for e in got] == [
        ("CONDITION_BYTE", 0), ("HFK", 7), ("HLZ", 41)]
    got = core.parse_extended(HEAD6 + bytes.fromhex("0203" "09aabb"))
    assert got[-1] == {"record": 9, "name": "", "label": "", "value": "aabb"}


def test_details_read_off_the_car_with_read_only_requests(car):
    with core.Tester("127.0.0.1", timeout=1.0) as t:
        got = core.fault_details(t, 0x12, "021200")
        none = core.fault_details(t, 0x12, "ABCDEF")
    fields = {f["id"]: f["value"] for f in got["snapshots"][0]["fields"]}
    assert fields[0x1700] == 54321 and fields[0x1750] == 10
    assert got["snapshots"][0]["rest"] == ""            # the DME's own value decoded too
    assert fields[0x4ABC] == pytest.approx(466)
    assert {e["name"]: e["value"] for e in got["extended"]} == {
        "CONDITION_BYTE": 0, "HFK": 3, "HLZ": 40}
    assert none["snapshots"] == [] and len(none["errors"]) == 2
    assert set(car.services) == {0x19}
    text = "\n".join(core.format_details(got))
    assert "Odometer" in text and "54321 km" in text and "times it occurred" in text
    assert core.format_details(none)[-1].startswith("(extended: requestOutOfRange")


def test_cli_faults_with_details(car, capsys):
    cli.main(["faults", "--ip", "127.0.0.1", "--timeout", "1", "--details"])
    out = capsys.readouterr().out
    assert "54321 km" in out and "times it occurred" in out and "check at the next service" in out


def test_cli_faults_all_with_details(car, monkeypatch, capsys):
    monkeypatch.setattr(core, "SWEEP_RANGE", (0x10, 0x13))
    cli.main(["faults", "--ip", "127.0.0.1", "--timeout", "1", "--all",
              "--probe-timeout", "0.1", "--details"])
    assert "54321 km" in capsys.readouterr().out


def test_claude_can_ask_for_details(monkeypatch):
    ran = []
    monkeypatch.setattr(mcp, "run_cli", lambda argv: ran.append(argv) or "")
    mcp.tool_read_faults({"ip": "127.0.0.1"})
    assert "--no-details" not in ran[0]                 # details are read by default
    mcp.tool_read_faults({"ip": "127.0.0.1", "details": False})
    assert "--no-details" in ran[1]


def test_the_fault_window_shows_details(app, car, monkeypatch):
    gui = pytest.importorskip("n63read.gui")
    monkeypatch.setattr(gui.filedialog, "asksaveasfilename", lambda **k: "")
    app.ip_var.set("127.0.0.1")
    app.modules = [0x12]
    before = set(app.winfo_children())
    app._faults()
    win = next(w for w in app.winfo_children() if w not in before)
    try:
        assert pump(app, lambda: win.fault_rows)
        tree = win.parts["tree"]
        first = next(i for i in tree.get_children() if win.fault_rows[int(i)]["code"] == "021200")
        tree.selection_set(first)
        app._fault_details(win)
        detail = win.parts["detail"]
        assert pump(app, lambda: "54321 km" in detail.get("1.0", "end"))
    finally:
        win.destroy()
        app.modules = []


# --------------------------------------------- several values per request

def test_split_reply():
    sizes = {0x1000: 2, 0x2000: 1}
    assert core.split_reply(bytes.fromhex("1000abcd" "200001"), sizes) == {
        0x1000: b"\xab\xcd", 0x2000: b"\x01"}
    assert core.split_reply(bytes.fromhex("200001"), sizes) == {0x2000: b"\x01"}  # one left out
    for bad in ("3000abcd", "1000ab", "1000abcd20", "1000abcd1000abcd"):
        with pytest.raises(ValueError):
            core.split_reply(bytes.fromhex(bad), sizes)


DME = core.load_catalog()


def chans(*names):
    return [(n, int(DME[n]["ID"], 16), DME[n]) for n in names]


def test_batched_reads_match_single_reads(car):
    names = chans("ILAG1", "ILAG2", "ITOEL")            # ITOEL isn't answered by the fake car
    with core.Tester("127.0.0.1", timeout=1.0) as t:
        single = core.ChannelReader({}, 0x12).read(t, names)
        r = core.ChannelReader({}, 0x12, batch=True)
        batched = r.read(t, names)
    assert batched == single and batched["ILAG2"][0] == 1.0
    assert r.batched == 2 and r.batch_off == {}


def test_a_dme_that_wont_batch_is_read_one_at_a_time(car):
    car.multi = False
    names = chans("ILAG1", "ILAG2")
    with core.Tester("127.0.0.1", timeout=1.0) as t:
        r = core.ChannelReader({}, 0x12, batch=True)
        first = r.read(t, names)
        car.sources.clear()
        second = r.read(t, names)
    assert first["ILAG2"][0] == 1.0 == second["ILAG2"][0]
    assert 0x12 in r.batch_off and r.batched == 0
    assert len(car.sources) == 2                         # no batch tried again


def test_cli_speed_compares_both_ways(car, capsys):
    cli.main(["speed", "--ip", "127.0.0.1", "--timeout", "1", "--seconds", "0.3",
              "ILAG1", "ILAG2"])
    out = capsys.readouterr().out
    assert "one per request" in out and "several per request" in out
    assert "n63read log` and the app use" in out


def test_cli_speed_says_when_the_dme_wont(car, capsys):
    car.multi = False
    cli.main(["speed", "--ip", "127.0.0.1", "--timeout", "1", "--seconds", "0.3",
              "ILAG1", "ILAG2"])
    assert "won't take several identifiers per request" in capsys.readouterr().out


def test_cli_log_batches_by_default(car, tmp_path):
    out = tmp_path / "run.csv"
    cli.main(["log", "--ip", "127.0.0.1", "--timeout", "1", "--interval", "0",
              "--samples", "3", "--out", str(out), "ILAG1", "ILAG2"])
    rows = out.read_text(encoding="utf-8").splitlines()
    assert rows[0] == "Time,Clock,ILAG1,ILAG2,Event" and rows[-1].split(",")[3] == "1.0"
    assert set(car.services) == {0x22}                  # still only reads
    assert len(car.services) == len(rows) - 1           # one request a row, not two


def test_cli_log_one_at_a_time(car, tmp_path):
    out = tmp_path / "run.csv"
    cli.main(["log", "--ip", "127.0.0.1", "--timeout", "1", "--interval", "0",
              "--one-at-a-time", "--samples", "3", "--out", str(out), "ILAG1", "ILAG2"])
    rows = out.read_text(encoding="utf-8").splitlines()
    assert len(car.services) == 2 * (len(rows) - 1)     # two requests a row


def test_a_modules_own_snapshot_values_are_decoded():
    # The body module's light switch fault at the car: each record has its
    # own on-board voltage (0x4040) and light switch position (0x4042).
    body = bytes.fromhex("0104" "1700" "01ad7b" "1701" "0fb00ff5" "4040" "8b" "4042" "0a"
                         "0204" "1700" "01b2c8" "1701" "0fe22bb4" "4040" "8c" "4042" "0a")
    one, two = core.parse_snapshots(HEAD4 + body, 0x40)
    got = {f["label"]: f["value"] for f in two["fields"]}
    assert got["Odometer"] == 111304 and got["Voltage electrical system"] == 14
    assert "Light switch position" in got and two["rest"] == "" and one["rest"] == ""
    fields = core.load_freeze_fields()
    assert sum(1 for m, _ in fields if m == 0x18) > 300          # the transmission's
    assert fields[(0x40, 0x4042)]["name"] == "BDC_G11.TAB_UW_LICHTSCHALTER"
    assert "BDC_G11.TAB_UW_LICHTSCHALTER" in core.load_enum_tables()
