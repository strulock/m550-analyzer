"""The car's two connections (bank 2 on its own), sharing them with other
n63read processes, and which fault codes count."""

import json
import os
import queue
import socket
import threading
import time

import pytest

from n63read import cli, core, share

from fake_car import FakeCar


@pytest.fixture
def car(monkeypatch):
    c = FakeCar()
    monkeypatch.setattr(core.Hsfz.__init__, "__defaults__", (c.port, core.TESTER_ADDR, 3.0))
    yield c
    c.close()


# ----------------------------------------------------------- two connections

def test_bank2_has_its_own_connection_and_tester_address(car):
    with core.Tester("127.0.0.1", timeout=3.0) as h:
        assert h.read_did(0x12, 0x588A) == b"\x10\x00"
        assert car.connections == 1                 # bank 2 not opened until needed
        assert h.read_did(core.DME_SLAVE, 0x588A) == b"\x0F\x00"
        assert car.connections == 2
        h.read_did(0x12, 0x588A)
        h.read_did(core.DME_SLAVE, 0x588A)
        assert car.connections == 2                 # both reused
    assert set(car.sources) == {(0xF4, 0x12), (0xF5, 0x13)}


def test_bank2_connection_closes_when_idle(car):
    with core.Tester("127.0.0.1", timeout=3.0) as h:
        h.read_did(core.DME_SLAVE, 0x588A)
        h.tidy()
        assert h.slave is not None
        h.tidy(now=time.time() + core.BANK2_IDLE + 1)
        assert h.slave is None
        assert h.read_did(core.DME_SLAVE, 0x588A) == b"\x0F\x00"     # reopens


def test_bank2_refused_is_an_error_and_not_retried_at_once(car, monkeypatch):
    real = core.Hsfz.connect

    def connect(self):
        if self.source == 0xF5:
            raise ConnectionResetError(10054, "forcibly closed")
        real(self)
    monkeypatch.setattr(core.Hsfz, "connect", connect)
    with core.Tester("127.0.0.1", timeout=3.0) as h:
        with pytest.raises(core.HsfzError, match="bank 2 connection failed"):
            h.read_did(core.DME_SLAVE, 0x588A)
        with pytest.raises(core.HsfzError, match="another tool may hold one"):
            h.read_did(core.DME_SLAVE, 0x588A)
        assert h.read_did(0x12, 0x588A) == b"\x10\x00"   # bank 1 unaffected


def test_bank2_channels_read_from_the_slave(car):
    cat = core.add_bank2({"ILAG2": {"ARG": "ILAG2", "ID": "0x588A", "INFO_EN": "Lambda",
                                    "DATENTYP": "unsigned int", "MUL": "16.0",
                                    "DIV": "65536.0", "ADD": "0.0", "EINHEIT": "-"}})
    assert cat["ILAG2@0x13"]["INFO_EN"] == "Bank 2: Lambda"
    chans = [(n, 0x588A, cat[n]) for n in ("ILAG2", "ILAG2@0x13")]
    reader = core.ChannelReader({}, core.DME_MASTER)
    with core.Tester("127.0.0.1", timeout=3.0) as h:
        sample = reader.read(h, chans)
    assert sample["ILAG2"][0] == 1.0 and sample["ILAG2@0x13"][0] == 0.9375


def test_cli_reads_bank2(car, capsys):
    cli.main(["read", "--ip", "127.0.0.1", "--timeout", "0.5", "ILAG2", "ILAG2@0x13"])
    lines = {l.split()[0]: l for l in capsys.readouterr().out.splitlines()}
    assert "1.0" in lines["ILAG2"].split() and "0.9375" in lines["ILAG2@0x13"].split()


def test_a_dropped_connection_is_explained():
    assert "two diagnostic connections" in core.connection_problem(
        ConnectionResetError(10054, "forcibly closed"))
    assert core.connection_problem(TimeoutError()) is None


# ------------------------------------------------------------------ sharing

class Owner:
    """Holds the car and lends it, as the app or `n63read log` does."""

    def __init__(self):
        self.h = core.Tester("127.0.0.1", timeout=3.0)
        self.h.connect()
        self.host = share.Host("127.0.0.1").start()
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        while self.running:
            self.host.serve(self.h)
            time.sleep(0.01)

    def close(self):
        self.running = False
        self.thread.join(2)
        self.host.stop()
        self.h.close()


@pytest.fixture
def owner(car):
    o = Owner()
    yield o
    o.close()


def test_another_process_borrows_the_connections(car, owner):
    with share.find("127.0.0.1") as t:
        assert t.read_did(0x12, 0x588A) == b"\x10\x00"
        assert t.read_did(core.DME_SLAVE, 0x588A) == b"\x0F\x00"
        with pytest.raises(core.NegativeResponse):
            t.read_did(0x12, 0x1234)
        with pytest.raises(core.HsfzTimeout):
            t.read_did(0x55, 0x588A, timeout=0.2)    # nothing at 0x55
    assert car.connections == 2                       # only the owner's two
    assert {s for s, _ in car.sources} == {0xF4, 0xF5}


def test_cli_borrows_instead_of_connecting(car, owner, capsys):
    cli.main(["read", "--ip", "127.0.0.1", "--timeout", "0.5", "ILAG2"])
    assert "1.0" in capsys.readouterr().out
    assert car.connections == 1


def test_only_for_the_same_car(owner):
    assert share.find("169.254.1.2") is None


def test_the_read_only_rule_holds_both_ways(owner):
    with share.find() as t:
        with pytest.raises(ValueError, match="refusing service 0x2E"):
            t.request(0x12, bytes([0x2E, 0x58, 0x8A, 0x00]))
        # A forged request straight to the host is refused there too.
        assert t._ask({"target": 0x12, "payload": "2e588a00"})["error"] == "refused"


def test_a_wrong_token_is_dropped(owner):
    with open(share.state_path(), encoding="utf-8") as f:
        port = json.load(f)["port"]
    with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
        s.sendall(b'{"token": "guess", "op": "hello"}\n')
        assert s.recv(100) == b""


def test_stopping_the_owner_withdraws_the_offer(car):
    o = Owner()
    assert os.path.exists(share.state_path())
    o.close()
    assert not os.path.exists(share.state_path())
    assert share.find() is None


def test_the_app_lends_its_connection(car):
    gui = pytest.importorskip("n63read.gui")
    q = queue.Queue()
    p = gui.Poller("127.0.0.1", 0x12, 0xF4, 1.0, q)
    p.interval = 0.05
    p.set_channels([("ILAG2", 0x588A, {"DATENTYP": "unsigned int", "MUL": "16.0",
                                       "DIV": "65536.0", "ADD": "0.0", "EINHEIT": "-"})])
    p.start()
    try:
        end = time.time() + 5
        t = None
        while t is None and time.time() < end:
            t = share.find("127.0.0.1")
            time.sleep(0.05)
        assert t is not None
        with t:
            assert t.read_did(core.DME_SLAVE, 0x588A) == b"\x0F\x00"
        assert car.connections == 2
    finally:
        p.stop()
        p.join(5)
    assert not os.path.exists(share.state_path())


# ------------------------------------------------------------------- faults

@pytest.mark.parametrize("status, failed", [
    (0x01, True), (0x04, True), (0x20, True), (0x2F, True), (0x28, True),
    (0x08, True), (0x48, True),         # confirmed, and tested since the clear
    (0x58, False),                      # "confirmed" but never tested since the clear
    (0x50, False), (0x40, False), (0x10, False), (0x00, False),
])
def test_which_codes_have_failed(status, failed):
    assert core.dtc_failed(status) is failed


def test_codes_that_never_failed_are_hidden_unless_asked(car, monkeypatch):
    import fake_car
    monkeypatch.setitem(fake_car.MODULES, 0x29, [("480780", 0x58), ("48069E", 0x6D),
                                                 ("480000", 0x50)])
    with core.Tester("127.0.0.1", timeout=3.0) as h:
        rows = core.read_all_faults(h, [0x29], texts={}, names={})
        every = core.read_all_faults(h, [0x29], texts={}, names={}, everything=True)
    assert [r["code"] for r in rows] == ["48069E"]
    assert len(every) == 3
    assert "not tested since cleared" in [r for r in every if r["code"] == "480780"][0]["state"]
