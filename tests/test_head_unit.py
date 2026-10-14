import pytest

from n63read import core
from fake_car import FakeCar, HU_GPS

CAT = core.load_head_unit_catalog()
LAYOUTS = core.record_layouts(CAT)
ENUMS = core.load_enum_tables()

GPS = (0x63, 0xD039)
SPEED = (0x63, 0xD030)


def test_catalog_has_the_gps_channels():
    for name in ("HU_STATUS_GPS", "HU_STATUS_SPEED.GPS_SPEED",
                 "HU_STATUS_GPS_POSITION.LATITUDE", "HU_STATUS_GPS_POSITION.LONGITUDE",
                 "HU_STATUS_GPS_POSITION.HEIGHT",
                 "HU_STATUS_GPS_SATINFO.NUMBER_TRACKED_SATS"):
        assert CAT[name]["MODULE"] == "0x63"


def test_every_record_has_contiguous_fields():
    for fields in LAYOUTS.values():
        assert [int(f["FIELD"]) for f in fields] == list(range(len(fields)))


def test_named_enum_tables_exist():
    for row in CAT.values():
        if row["NAME"] not in ("", "-") and row["DATENTYP"] != "BITFIELD":
            assert row["NAME"].upper() in ENUMS, row["ARG"]


def test_gps_fix_status_has_words():
    assert core.enum_text(12, CAT["HU_STATUS_GPS"], ENUMS) == "3D position"


def test_private_channels_are_flagged():
    assert core.is_sensitive("HU_WLAN_AP_NETWORK.WLAN_AP_NETWORK_KEY_TEXT")
    assert core.is_sensitive("HU_PROVISIONING_PARAMETER.IMEI_TEXT")
    assert core.is_sensitive("HU_STATUS_LAST_DESTINATIONS_FIRST_DATA_SET.GPS_POSITION_LATITUDE_1_DATA")
    assert not core.is_sensitive("HU_STATUS_GPS_POSITION.LATITUDE")
    for name in CAT:
        if "NETWORK_KEY" in name or "IMEI" in name:
            assert core.is_sensitive(name)


def test_decode_gps_record_matches_the_job_layout():
    rec = core.decode_record(HU_GPS, LAYOUTS[GPS])
    assert rec["HU_STATUS_GPS_POSITION.VALIDITY"][0] == 0
    assert rec["HU_STATUS_GPS_POSITION.LATITUDE"][0] == pytest.approx(30 + 8 / 60 + 4.56 / 3600)
    assert rec["HU_STATUS_GPS_POSITION.LONGITUDE"][0] == pytest.approx(-(97 + 38 / 60 + 9.24 / 3600))
    assert rec["HU_STATUS_GPS_POSITION.HEIGHT"][0] == 160
    p = core.parse_gps_position(HU_GPS)
    assert rec["HU_STATUS_GPS_POSITION.LATITUDE"][0] == p["lat"]


def test_decode_record_walks_fields_in_order():
    raw = b"".join(v.to_bytes(2, "big", signed=True) for v in (10, 20, 30, 40, 25, -1)) + b"\x00\x05"
    rec = core.decode_record(raw, LAYOUTS[SPEED])
    assert rec["HU_STATUS_SPEED.WHEEL3_SENSOR_SPEED"][0] == 30
    assert rec["HU_STATUS_SPEED.GPS_SPEED"][0] == -1
    assert rec["HU_STATUS_SPEED.SPEED_DIFFERENCE_PERCENT"][0] == 5


def test_decode_record_unsized_strings_and_unknown_types():
    fields = [{"ARG": "a", "DATENTYP": "unsigned char"},
              {"ARG": "b", "DATENTYP": "string"},
              {"ARG": "c", "DATENTYP": "unsigned int"},
              {"ARG": "d", "DATENTYP": "BITFIELD"},
              {"ARG": "e", "DATENTYP": "unsigned char"}]
    rec = core.decode_record(b"\x07N 12.5\x00\x01\x02", fields)
    assert rec["a"][0] == 7 and rec["b"][0] == "N 12.5" and rec["c"][0] == 0x0102
    assert rec["d"] == (None, "", "layout unknown") == rec["e"]


def test_decode_record_short_reply():
    rec = core.decode_record(b"\x00\x01", LAYOUTS[SPEED])
    assert rec["HU_STATUS_SPEED.WHEEL1_SENSOR_SPEED"][0] == 1
    assert rec["HU_STATUS_SPEED.GPS_SPEED"] == (None, "", "short reply")


# ------------------------------------------------------------- ChannelReader

class FakeH:
    """Answers read_did from a dict; values may be exceptions to raise."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def read_did(self, target, did):
        self.calls.append((target, did))
        a = self.answers[(target, did)]
        if isinstance(a, Exception):
            raise a
        return a


def chan(name):
    meta = CAT[name]
    return (name, int(meta["ID"], 16), meta)


DME = ("DME_X", 0x588A, {"DATENTYP": "unsigned int", "EINHEIT": "rpm"})
LAT = chan("HU_STATUS_GPS_POSITION.LATITUDE")
GPS_SPEED = chan("HU_STATUS_SPEED.GPS_SPEED")


def test_reader_reads_dme_every_cycle_and_head_unit_on_its_period():
    h = FakeH({(0x12, 0x588A): b"\x10\x00", GPS: HU_GPS})
    r = core.ChannelReader(LAYOUTS, 0x12)
    s1 = r.read(h, [DME, LAT], now=100.0)
    s2 = r.read(h, [DME, LAT], now=100.3)
    assert h.calls.count((0x12, 0x588A)) == 2
    assert h.calls.count(GPS) == 1                      # cached for the second cycle
    assert s1["HU_STATUS_GPS_POSITION.LATITUDE"] == s2["HU_STATUS_GPS_POSITION.LATITUDE"]
    r.read(h, [DME, LAT], now=101.1)
    assert h.calls.count(GPS) == 2


def test_reader_caps_head_unit_reads_per_cycle():
    keys = list(LAYOUTS)[:core.SLOW_READS_PER_CYCLE + 3]
    chans = [(LAYOUTS[k][0]["ARG"], k[1], LAYOUTS[k][0]) for k in keys]
    h = FakeH({k: b"\x00" * 64 for k in keys})
    r = core.ChannelReader(LAYOUTS, 0x12)
    s = r.read(h, chans, now=100.0)
    assert len(h.calls) == core.SLOW_READS_PER_CYCLE
    assert sum(1 for v in s.values() if v[2] == "waiting") == 3
    r.read(h, chans, now=100.1)
    assert len(set(h.calls)) == len(keys)               # the rest come next cycle


def test_reader_stops_asking_for_unsupported_identifiers():
    h = FakeH({GPS: core.NegativeResponse(0x22, 0x31)})
    r = core.ChannelReader(LAYOUTS, 0x12)
    s = r.read(h, [LAT], now=100.0)
    r.read(h, [LAT], now=200.0)
    assert h.calls == [GPS]
    assert s["HU_STATUS_GPS_POSITION.LATITUDE"] == (None, "", "requestOutOfRange")


def test_reader_backs_off_a_silent_head_unit_but_keeps_reading_the_dme():
    h = FakeH({(0x12, 0x588A): b"\x10\x00", GPS: core.HsfzTimeout("timed out")})
    r = core.ChannelReader(LAYOUTS, 0x12)
    r.read(h, [DME, LAT], now=100.0)
    s = r.read(h, [DME, LAT], now=105.0)
    assert h.calls.count(GPS) == 1
    assert h.calls.count((0x12, 0x588A)) == 2
    assert s["HU_STATUS_GPS_POSITION.LATITUDE"][2] == "module not answering"
    r.read(h, [DME, LAT], now=100.0 + core.NO_ANSWER_BACKOFF + 1)
    assert h.calls.count(GPS) == 2


def test_reader_over_hsfz_with_the_fake_car():
    car = FakeCar()
    try:
        with core.Hsfz("127.0.0.1", port=car.port, timeout=2.0) as h:
            r = core.ChannelReader(LAYOUTS, 0x12)
            s = r.read(h, [DME, LAT, GPS_SPEED, chan("HU_STATUS_GPS")])
    finally:
        car.close()
    assert s["DME_X"][0] == 0x1000
    assert s["HU_STATUS_GPS_POSITION.LATITUDE"][0] == pytest.approx(30.13460, abs=1e-5)
    assert s["HU_STATUS_SPEED.GPS_SPEED"][0] == 98
    assert s["HU_STATUS_GPS"][0] == 12
