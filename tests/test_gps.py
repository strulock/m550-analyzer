import pytest

from n63read import core


def test_parse_gps_position_north_west():
    # 30 deg 08' 04.56" N, 97 deg 38' 09.24" W, 160 m (Circuit of the Americas)
    payload = bytes([0x00, 0x00, 30, 8, 4, 56, 0x01, 97, 38, 9, 24, 0x00, 160])
    p = core.parse_gps_position(payload)
    assert p["valid"] is True
    assert p["lat"] == pytest.approx(30 + 8 / 60 + 4.56 / 3600)
    assert p["lon"] == pytest.approx(-(97 + 38 / 60 + 9.24 / 3600))
    assert p["height"] == 160


def test_parse_gps_position_invalid_and_south_east():
    payload = bytes([0x01, 0x01, 33, 0, 0, 0, 0x00, 151, 0, 0, 0, 0x01, 0x2C])
    p = core.parse_gps_position(payload)
    assert p["valid"] is False
    assert p["lat"] == -33 and p["lon"] == 151 and p["height"] == 300


def test_parse_gps_position_rejects_short_payload():
    with pytest.raises(ValueError):
        core.parse_gps_position(b"\x00\x00\x20")
