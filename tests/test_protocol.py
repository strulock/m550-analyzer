import struct

import pytest

from n63read import core


class FakeSocket:
    """Feeds canned HSFZ frames to Hsfz and records what it sent."""

    def __init__(self, frames):
        self.rx = b"".join(frames)
        self.sent = []

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, n):
        if not self.rx:
            raise core.socket.timeout()
        chunk, self.rx = self.rx[:n], self.rx[n:]
        return chunk

    def close(self):
        pass


def frame(src, dst, payload, control=core.CTRL_DIAG):
    return struct.pack(">IH", len(payload) + 2, control) + bytes([src, dst]) + payload


def hsfz(frames):
    h = core.Hsfz("0.0.0.0", timeout=0.05)
    h.sock = FakeSocket(frames)
    return h


@pytest.mark.parametrize("service", [0x10, 0x14, 0x27, 0x2C, 0x2E, 0x2F, 0x31, 0x34, 0x85])
def test_refuses_anything_but_read_services(service):
    h = hsfz([])
    with pytest.raises(ValueError):
        h.request(0x12, bytes([service, 0x00]))
    assert h.sock.sent == []


def test_allow_list_is_exactly_the_read_services():
    assert core.ALLOWED_SERVICES == {0x19, 0x22, 0x3E}


def test_read_did_skips_ack_and_pending():
    h = hsfz([
        frame(0xF4, 0x12, b"\x22\x58\x8A", control=core.CTRL_ACK),
        frame(0x12, 0xF4, b"\x7F\x22\x78"),
        frame(0x12, 0xF4, b"\x62\x58\x8A\x10\x00"),
    ])
    assert h.read_did(0x12, 0x588A) == b"\x10\x00"
    assert h.sock.sent[0] == frame(0xF4, 0x12, b"\x22\x58\x8A")


def test_late_reply_from_another_module_is_not_credited():
    # A scan's previous target answers late; it must not count for 0x13.
    h = hsfz([frame(0x12, 0xF4, b"\x7E\x00")])
    with pytest.raises(core.HsfzError):
        h.tester_present(0x13)


def test_a_late_reply_to_an_earlier_identifier_is_skipped():
    # 0xDA5F timed out; its answer arrives while 0xDA77 is being asked for.
    h = hsfz([frame(0x18, 0xF4, bytes.fromhex("62DA5F01")),
              frame(0x18, 0xF4, bytes.fromhex("62DA7702"))])
    assert h.read_did(0x18, 0xDA77) == bytes.fromhex("02")


def test_a_late_reply_alone_is_a_timeout_not_a_mismatch():
    h = hsfz([frame(0x18, 0xF4, bytes.fromhex("62DA5F01"))])
    with pytest.raises(core.HsfzTimeout):
        h.read_did(0x18, 0xDA77)


def test_a_late_fault_reply_of_another_kind_is_skipped():
    h = hsfz([frame(0x12, 0xF4, bytes.fromhex("590401")),
              frame(0x12, 0xF4, bytes.fromhex("5902FF"))])
    assert h.request(0x12, bytes.fromhex("1902FF")) == bytes.fromhex("5902FF")


def test_the_check_notes_a_bad_reply_and_carries_on():
    class H:
        def read_did(self, *a, **k):
            raise core.HsfzError("short response")
    status, detail, raw = core.check_identifier(H(), (0x18, 0xDA77), [])
    assert (status, detail, raw) == ("no answer", "short response", b"")


def test_a_busy_module_is_asked_again(monkeypatch):
    monkeypatch.setattr(core, "BUSY_WAIT", 0)
    h = hsfz([frame(0x63, 0xF4, bytes.fromhex("7F2221")),
              frame(0x63, 0xF4, bytes.fromhex("62D03801"))])
    assert h.read_did(0x63, 0xD038) == bytes.fromhex("01")
    assert len(h.sock.sent) == 2                      # the request went twice


def test_a_module_that_stays_busy_is_a_negative_response(monkeypatch):
    monkeypatch.setattr(core, "BUSY_WAIT", 0)
    h = hsfz([frame(0x63, 0xF4, bytes.fromhex("7F2221"))] * (core.BUSY_RETRIES + 1))
    with pytest.raises(core.NegativeResponse) as e:
        h.read_did(0x63, 0xD038)
    assert e.value.code == 0x21 and len(h.sock.sent) == core.BUSY_RETRIES + 1


def test_a_refused_tester_address_ends_the_connection():
    # After the PC slept, the car answered every request with control 0x40.
    h = hsfz([struct.pack(">IH", 0, 0x0040)])
    with pytest.raises(core.ConnectionLost, match="incorrect tester address"):
        h.read_did(0x12, 0x588A)


def test_other_gateway_refusals_are_errors_for_that_request():
    h = hsfz([struct.pack(">IH", 2, 0x0043) + bytes([0x99, 0xF4])])
    with pytest.raises(core.HsfzError, match="destination address") as e:
        h.read_did(0x99, 0x588A)
    assert not isinstance(e.value, core.ConnectionLost)


def test_silent_address_raises_hsfz_error_not_socket_timeout():
    with pytest.raises(core.HsfzError):
        hsfz([]).tester_present(0x55)


def test_an_absurd_frame_length_is_refused_not_allocated():
    bad = struct.pack(">IH", 0xFFFFFFFF, core.CTRL_DIAG) + bytes([0x12, 0xF4])
    with pytest.raises(core.HsfzError, match="bad frame"):
        hsfz([bad]).tester_present(0x12)


def test_request_out_of_range_is_a_negative_response():
    h = hsfz([frame(0x12, 0xF4, b"\x7F\x22\x31")])
    with pytest.raises(core.NegativeResponse) as e:
        h.read_did(0x12, 0x1234)
    assert e.value.code == 0x31


def test_parse_dtc_records():
    resp = bytes.fromhex("5902FF" "02120005" "48A30109")
    assert core.parse_dtc_records(resp) == [("021200", 0x05), ("48A301", 0x09)]


@pytest.mark.parametrize("status, text", [
    (0x05, "present, stored"),
    (0x04, "not present, stored"),
    (0x08, "not present, stored"),
    (0x01, "present, not yet stored"),
    (0x00, "not present"),
    (0x89, "present, stored, warning lamp requested"),
])
def test_dtc_status_summary(status, text):
    assert core.dtc_status_summary(status) == text


def test_dtc_status_flags():
    assert core.dtc_status_flags(0x09) == ["testFailed", "confirmedDTC"]
