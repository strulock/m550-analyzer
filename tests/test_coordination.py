"""Sessions share the car: Claude, the app and the CLI never knock each other off.

The car takes two diagnostic connections at most (and bank 2 needs one), so
whoever holds it lends it, everyone else borrows, and when the holder stops,
whoever is still reading takes the car over.
"""

import queue
import threading
import time

import pytest

from n63read import cli, core, engine, mcp, share

from fake_car import FakeCar

DME = core.load_catalog()
ILAG2 = [("ILAG2", int(DME["ILAG2"]["ID"], 16), DME["ILAG2"])]


@pytest.fixture
def car(monkeypatch):
    c = FakeCar()
    monkeypatch.setattr(core.Hsfz.__init__, "__defaults__", (c.port, core.TESTER_ADDR, 3.0))
    monkeypatch.setattr(core, "RECONNECT_WAIT", 0.1)
    yield c
    c.close()


def wait_for(cond, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class App:
    """A session as the app runs one, with what it said."""

    def __init__(self):
        self.q = queue.Queue()
        self.said = []
        self.session = engine.Session("127.0.0.1", out=self, interval=0.03)
        self.session.set_channels(ILAG2)

    def put(self, message):
        self.said.append(message)

    def start(self):
        self.session.start()
        assert wait_for(lambda: self.statuses()[-1:] == ["connected"])
        return self

    def statuses(self):
        return [m[1] for m in self.said if m[0] == "status"]

    def errors(self):
        return [m for m in self.said if m[0] == "error"]

    def samples(self):
        return sum(1 for m in self.said if m[0] == "sample")

    def still_reading(self):
        before = self.samples()
        return wait_for(lambda: self.samples() > before + 3)

    def stop(self):
        self.session.stop()
        self.session.join(5)


def call(tool, **args):
    try:
        return mcp.HANDLERS[tool](args), False
    except mcp.ToolError as e:
        return str(e), True


def test_claude_logs_while_the_app_is_connected(car):
    app = App().start()
    try:
        text, error = call("start_log", ip="127.0.0.1", names=["ILAG2"], interval=0.03)
        assert not error and "borrowing the app's connection" in text
        assert app.still_reading()
        text, error = call("stop_log")
        assert not error and "rows in" in text and not text.startswith("Stopped: 0")
        assert app.still_reading()                          # the app never noticed
        assert app.errors() == [] and app.statuses() == ["connected"]
        assert car.connections == 1                         # one connection for both
    finally:
        app.stop()


def test_the_app_connects_while_claude_logs_and_takes_over_after(car):
    text, error = call("start_log", ip="127.0.0.1", names=["ILAG2"], interval=0.03)
    assert not error and "borrowing" not in text            # Claude holds the car
    app = App().start()
    try:
        assert app.session.borrowed and car.connections == 1
        assert app.still_reading()
        call("stop_log")                                    # Claude lets go...
        assert wait_for(lambda: app.statuses()[-1:] == ["connected"]
                        and not app.session.borrowed)       # ...the app holds it now
        assert app.still_reading()
        assert app.errors() == []
        assert car.connections == 2                         # Claude's, then the app's
    finally:
        app.stop()


def test_a_claude_read_while_the_app_logs(car, tmp_path):
    app = App().start()
    app.session.start_log(str(tmp_path / "drive.csv"))
    try:
        text, error = call("read_channels", ip="127.0.0.1", names=["ILAG2"])
        assert not error and "1.0" in text
        text, error = call("sample", ip="127.0.0.1", names=["ILAG2"], seconds=1,
                           interval=0.05)
        assert not error and "borrowing the app's connection" in text
        assert app.still_reading() and app.errors() == []
        assert car.connections == 1
    finally:
        app.stop()


def test_the_app_connects_during_a_quick_claude_read_and_carries_on(car):
    quick = share.open_tester("127.0.0.1", 0xF6, 2.0)       # Claude's read, mid-way
    app = App().start()
    try:
        assert app.session.borrowed
        assert app.still_reading()
        quick.close()                                       # the read is done
        assert wait_for(lambda: not app.session.borrowed)
        assert app.still_reading() and app.errors() == []
    finally:
        app.stop()


def test_claude_carries_on_when_the_app_disconnects(car):
    app = App().start()
    text, error = call("start_log", ip="127.0.0.1", names=["ILAG2"], interval=0.03)
    assert not error and "borrowing" in text
    session = mcp._logger[0]
    try:
        app.stop()                                          # the user presses Disconnect
        assert wait_for(lambda: not session.borrowed and session.is_alive())
        rows = session.rows
        assert wait_for(lambda: session.rows > rows + 3)    # Claude's log carries on
    finally:
        call("stop_log")


def test_the_cli_logs_alongside_the_app(car, tmp_path):
    app = App().start()
    try:
        cli.main(["log", "--ip", "127.0.0.1", "--timeout", "1", "--interval", "0.02",
                  "--samples", "10", "--out", str(tmp_path / "cli.csv"), "ILAG2"])
        assert (tmp_path / "cli.csv").read_text(encoding="utf-8").count("\n") == 11
        assert app.still_reading() and app.errors() == []
        assert car.connections == 1
    finally:
        app.stop()


def test_two_starting_together_make_one_connection(car):
    ready = threading.Barrier(4)
    got = []

    def start():
        ready.wait()
        got.append(share.open_tester("127.0.0.1", 0xF4, 2.0))
    threads = [threading.Thread(target=start) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    try:
        assert len(got) == 4
        assert sum(1 for t in got if not getattr(t, "shared", False)) == 1
        assert car.connections == 1
        assert all(t.read_did(0x12, 0x588A) == b"\x10\x00" for t in got)
    finally:
        for t in sorted(got, key=lambda t: not getattr(t, "shared", False)):
            t.close()


def test_the_pc_stays_awake_while_connected(car, monkeypatch):
    calls = []
    monkeypatch.setattr(engine, "keep_awake", lambda on: calls.append(
        (on, threading.current_thread().name)))
    app = App().start()
    assert [on for on, _ in calls] == [True]
    app.stop()
    assert [on for on, _ in calls] == [True, False]
    assert calls[0][1] == calls[1][1] == app.session.name   # the session's own thread


def test_keep_awake_is_harmless_here():
    assert engine.keep_awake(True) in (True, False)
    assert engine.keep_awake(False) in (True, False)


def test_a_read_cycle_holds_the_connection_until_it_ends(car):
    """Requests lent to another program wait for the end of a cycle instead
    of slotting in between each of its reads."""
    app = App()
    held = []
    real = app.session.reader.read

    def read(h, chans, now=None):
        held.append(h.lock._is_owned())
        return real(h, chans, now)
    app.session.reader.read = read
    app.start()
    try:
        assert wait_for(lambda: len(held) >= 3)
    finally:
        app.stop()
    assert all(held)


def test_borrowed_and_own_requests_take_turns(car):
    """The holder's reads and the requests it lends run on different threads;
    the connection's lock keeps every reply with its request."""
    owner = share.open_tester("127.0.0.1", 0xF4, 2.0)
    borrower = share.open_tester("127.0.0.1", 0xF6, 2.0)
    assert borrower.shared
    bad = []

    def hammer(t, did, want):
        for _ in range(60):
            if t.read_did(0x12, did) != want:
                bad.append(did)
    threads = [threading.Thread(target=hammer, args=(owner, 0x588A, b"\x10\x00")),
               threading.Thread(target=hammer, args=(borrower, 0x5889, b"\x0F\xA0"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    borrower.close()
    owner.close()
    assert bad == []
