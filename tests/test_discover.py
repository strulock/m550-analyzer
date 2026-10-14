"""Discover when the chosen 'via' address has gone (WinError 10049)."""

import pytest

from n63read import core

from conftest import pump


# An address that is never on a PC: TEST-NET-3 (RFC 5737).
GONE = "203.0.113.77"


def test_link_local():
    assert core.link_local(["10.0.0.5", "169.254.10.2", "169.254.1.1"]) == \
        ["169.254.10.2", "169.254.1.1"]


def test_discover_from_an_address_not_on_this_pc_says_so():
    with pytest.raises(core.AddressNotAvailable) as e:
        core.discover(0.2, GONE)
    assert GONE in str(e.value) and "169.254" in str(e.value)
    assert isinstance(e.value, OSError)            # callers catching OSError still do


# --------------------------------------------------------------------- GUI

gui = pytest.importorskip("n63read.gui")


class Dialogs:
    def __init__(self):
        self.info, self.errors = [], []


@pytest.fixture
def dialogs(monkeypatch):
    d = Dialogs()
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda t, m, **k: d.info.append(m))
    monkeypatch.setattr(gui.messagebox, "showerror", lambda t, m, **k: d.errors.append(m))
    return d


@pytest.fixture
def addrs(app, monkeypatch):
    """Control what the PC's addresses are."""
    box = {"now": ["192.168.1.20", "169.254.33.7"]}
    monkeypatch.setattr(core, "local_ipv4s", lambda: list(box["now"]))
    yield box
    monkeypatch.undo()
    app._refresh_interfaces()


def test_refresh_keeps_a_live_choice(app, addrs):
    app.bind_var.set("192.168.1.20")
    app._refresh_interfaces()
    assert app.bind_var.get() == "192.168.1.20"


def test_refresh_replaces_a_vanished_address_with_the_adapters_new_one(app, addrs):
    app.bind_var.set("169.254.1.1")                 # the adapter's old address
    app._refresh_interfaces()
    assert app.bind_var.get() == "169.254.33.7"     # not "auto" (that leaves via Wi-Fi)


def test_refresh_falls_back_to_auto_without_an_adapter(app, addrs):
    addrs["now"] = ["192.168.1.20"]
    app.bind_var.set("169.254.1.1")
    app._refresh_interfaces()
    assert app.bind_var.get() == "auto"


def test_discover_uses_the_current_address_not_the_stale_one(app, addrs, monkeypatch):
    used = []
    monkeypatch.setattr(core, "discover", lambda t, bind: used.append(bind) or
                        [("169.254.10.10", b"")])
    app.bind_var.set("169.254.1.1")                 # remembered from before a link reset
    app._discover()
    assert "169.254.1.1 is gone" in app.status.cget("text")
    assert pump(app, lambda: used and app.ip_var.get() == "169.254.10.10")
    assert used == ["169.254.33.7"]


def test_no_reply_without_an_enet_address_explains_why(app, addrs, monkeypatch, dialogs):
    addrs["now"] = ["192.168.1.20"]
    monkeypatch.setattr(core, "discover", lambda t, bind: [])
    app._discover()
    assert pump(app, lambda: dialogs.info)
    assert "no 169.254.x.x (ENET) address" in dialogs.info[-1]


def test_a_vanished_address_is_reported_clearly(app, addrs, monkeypatch, dialogs):
    def gone(t, bind):
        raise core.AddressNotAvailable("%s is not an address of this PC right now" % bind)
    monkeypatch.setattr(core, "discover", gone)
    app._discover()
    assert pump(app, lambda: dialogs.errors)
    assert "is not an address of this PC" in dialogs.errors[-1]
    assert app.poller is None


def test_cli_discover_from_a_vanished_address_is_a_message_not_a_traceback(capsys):
    from n63read import cli
    with pytest.raises(SystemExit) as e:
        cli.main(["discover", "--bind", GONE, "--timeout", "0.2"])
    assert GONE in str(e.value.code) and "169.254" in str(e.value.code)


# ------------------------------------------------- this PC's own address

def test_connect_to_this_pcs_own_address_is_caught(app, addrs, dialogs):
    app.ip_var.set("169.254.33.7")                  # what ipconfig shows: the PC's
    app._toggle()
    assert app.poller is None
    assert "this PC's own address" in dialogs.info[-1]


@pytest.mark.parametrize("action", ["_faults", "_check_car"])
def test_faults_and_check_catch_it_too(app, addrs, dialogs, action):
    app.ip_var.set("169.254.33.7")
    getattr(app, action)()
    assert "this PC's own address" in dialogs.info[-1]


def test_no_answer_explains_itself(app, dialogs, monkeypatch):
    # Nothing listens on the diagnostic port here, as at a wrong address.
    monkeypatch.setattr(core, "local_ipv4s", lambda: ["192.168.1.20"])
    app.ip_var.set("127.0.0.1")
    app._toggle()
    assert pump(app, lambda: dialogs.errors, timeout=10)
    text = dialogs.errors[-1]
    assert "Nothing answered at 127.0.0.1" in text and "Discover" in text
    assert app.poller is None
