import datetime
import json
import os
import time

import pytest

from n63read import core, diag

from fake_car import FakeCar

HSFZ_REPLY = bytes.fromhex("000000320011") + b"DIAGADR10BMWMAC001A37AABBCCBMWVINWBA00000000000000"


def entries(journal):
    out = []
    for f in sorted(os.listdir(journal)) if os.path.isdir(journal) else []:
        if f.endswith(".jsonl"):
            with open(os.path.join(journal, f), encoding="utf-8") as fh:
                out += [json.loads(line) for line in fh if line.strip()]
    return out


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


# ------------------------------------------------------------------ journal

def test_record_appends_json_lines(quick_diagnostics):
    diag.record("discover", seconds=1.5, bytes=b"\x01".hex())
    diag.record("connect", result="ok")
    got = entries(quick_diagnostics)
    assert [e["event"] for e in got] == ["discover", "connect"]
    assert got[0]["version"] and got[0]["time"] and got[0]["seconds"] == 1.5


def test_recording_can_be_switched_off(quick_diagnostics, monkeypatch):
    monkeypatch.setattr(diag, "enabled", False)
    assert diag.record("discover") is None
    assert entries(quick_diagnostics) == []


def test_prune_keeps_recent_days(quick_diagnostics):
    os.makedirs(quick_diagnostics)
    old = (datetime.date.today() - datetime.timedelta(days=40)).isoformat()
    for name in (old + ".jsonl", old + "_10-00-00_capture.pcapng"):
        open(os.path.join(quick_diagnostics, name), "w").close()
    diag.record("discover")
    diag.prune()
    assert [f[:10] for f in os.listdir(quick_diagnostics)] == [datetime.date.today().isoformat()]


def test_report_gathers_entries_and_names_captures(quick_diagnostics, tmp_path):
    diag.record("discover")
    diag.record("connect")
    cap = os.path.join(quick_diagnostics, datetime.date.today().isoformat() + "_x_capture.pcapng")
    open(cap, "wb").close()
    out = tmp_path / "report.jsonl"
    assert diag.report(str(out)) == 2
    lines = out.read_text(encoding="utf-8").splitlines()
    head = json.loads(lines[0])
    assert head["entries"] == 2 and head["captures"] == [os.path.basename(cap)]


# ----------------------------------------------------------------- snapshot

SNAP = {"enet": ["169.254.35.7"], "windows": {
    "addresses": {"alias": "Ethernet 4", "ip": "169.254.35.7"},       # one item: no list
    "profiles": [{"alias": "Ethernet 4", "category": "Public", "name": "Unidentified network"}],
    "adapters": [{"name": "Ethernet 4", "description": "NX USB2.0 Fast Ethernet Adapter",
                  "status": "Up", "link": "100 Mbps", "driver": "1.0.0.9"}]}}


def test_interface_of_finds_the_enet_adapter():
    iface = diag.interface_of(SNAP, "169.254.35.7")
    assert iface["alias"] == "Ethernet 4" and iface["category"] == "Public"
    assert iface["adapter"].startswith("NX USB2.0") and iface["driver"] == "1.0.0.9"
    assert diag.interface_of(SNAP, "10.0.0.1") is None


@pytest.mark.parametrize("snap, hits, near, expect", [
    ({"enet": []}, [], [], "adapter is not up"),
    ({"enet": ["169.254.35.7"]}, [("169.254.1.2", HSFZ_REPLY)], [], "answered (HSFZ)"),
    ({"enet": ["169.254.35.7"]}, [("169.254.1.2", bytes.fromhex("02fd0004"))], [], "DoIP only"),
    ({"enet": ["169.254.35.7"]}, [], ["169.254.1.2"], "is on the car's link"),
    (dict(SNAP, enet_interface={"alias": "Ethernet 4", "category": "Public"}), [], [],
     "Public network"),
    ({"enet": ["169.254.35.7"], "enet_interface": {"category": "Private"}}, [], [],
     "activation line"),
])
def test_verdict(snap, hits, near, expect):
    assert expect in diag.verdict(snap, hits, near)


# --------------------------------------------------------------- the steps

def test_discover_reports_first_then_records_everything(quick_diagnostics, monkeypatch):
    order = []

    def fake_discover(timeout, bind, details=None):
        details.append({"ms": 12, "from": "169.254.1.2", "port": 6811, "bytes": HSFZ_REPLY.hex()})
        return [("169.254.1.2", HSFZ_REPLY)]
    monkeypatch.setattr(core, "discover", fake_discover)
    monkeypatch.setattr(diag, "snapshot", lambda bind: order.append("snapshot") or {"enet": [bind]})
    hits, near = diag.discover(1.0, "169.254.35.7",
                               on_result=lambda h, n: order.append("result"))
    assert order == ["result", "snapshot"]
    (e,) = entries(quick_diagnostics)
    assert e["event"] == "discover" and e["replies"][0]["protocol"] == "HSFZ"
    assert e["received"][0]["port"] == 6811 and "answered (HSFZ)" in e["verdict"]
    assert {s["bytes"] for s in e["sent"]} == {p.hex() for p in core.HSFZ_IDENT_PROBES} | \
        {core.DOIP_IDENT_PROBE.hex()}


def test_port_check():
    car = FakeCar()
    try:
        assert diag.port_check("127.0.0.1", car.port)["result"] == "open"
    finally:
        car.close()
    assert diag.port_check("127.0.0.1", car.port, timeout=1)["result"] in ("refused", "timeout")


def test_wire_trace_records_frames():
    car = FakeCar()
    trace = []
    try:
        with core.Hsfz("127.0.0.1", port=car.port, timeout=2.0, trace=trace) as h:
            h.read_did(0x12, 0x588A)
    finally:
        car.close()
    kinds = [t[1] for t in trace]
    assert kinds[0] == "connected" and "tx" in kinds and kinds.count("rx") >= 2   # ack + reply
    tx = next(t for t in trace if t[1] == "tx")
    assert tx[3:] == (0xF4, 0x12, "22588a")


def test_full_check_against_the_fake_car(quick_diagnostics, monkeypatch):
    car = FakeCar()
    monkeypatch.setattr(core, "local_ipv4s", lambda: ["169.254.35.7"])
    monkeypatch.setattr(core, "discover",
                        lambda t, bind, details=None: [("127.0.0.1", HSFZ_REPLY)])
    monkeypatch.setattr(core, "link_neighbours", lambda ip: [])
    monkeypatch.setattr(core, "HSFZ_DIAG_PORT", car.port)
    monkeypatch.setattr(core.Hsfz.__init__, "__defaults__", (car.port, core.TESTER_ADDR, 3.0))
    said = []
    try:
        result = diag.full_check(timeout=0.5, say=said.append)
    finally:
        car.close()
    assert result["ok"] and result["bind"] == "169.254.35.7"
    assert result["hsfz_answers"] == {"0x10": "answers", "0x12": "answers"}
    assert any("Verdict: The car answers over HSFZ at 127.0.0.1" in s for s in said)
    (e,) = entries(quick_diagnostics)
    assert e["event"] == "diag" and e["trace"]


def test_cli_diag_report(quick_diagnostics, tmp_path, capsys):
    from n63read import cli
    diag.record("discover")
    out = tmp_path / "r.jsonl"
    cli.main(["diag", "--report", str(out)])
    assert "Saved 1 journal entries" in capsys.readouterr().out and out.exists()


def test_capture_needs_admin(monkeypatch):
    monkeypatch.setattr(diag, "is_admin", lambda: False)
    with pytest.raises(PermissionError):
        diag.Capture(5).start()


# --------------------------------------------------------------------- GUI

gui = pytest.importorskip("n63read.gui")


def test_failed_connect_is_recorded_with_port_checks(app, quick_diagnostics, monkeypatch):
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *a, **k: None)
    monkeypatch.setattr(core, "local_ipv4s", lambda: ["192.168.1.20"])
    app.ip_var.set("127.0.0.1")                     # nothing listens on 6801 here
    app._toggle()
    assert wait_for(lambda: any(e["event"] == "connect" for e in entries(quick_diagnostics)),
                    timeout=15)
    e = next(e for e in entries(quick_diagnostics) if e["event"] == "connect")
    assert e["result"] == "failed" and {p["port"] for p in e["ports"]} == {6801, 13400}
    assert "snapshot" in e


def test_session_is_recorded_with_its_wire_trace(quick_diagnostics):
    import queue
    car = FakeCar()
    real = core.Hsfz
    try:
        q = queue.Queue()
        p = gui.Poller("127.0.0.1", 0x12, 0xF4, 1.0, q)
        p.interval = 0.05
        gui.core = type("C", (), dict(vars(core), Hsfz=staticmethod(
            lambda ip, source, timeout, **kw: real(ip, port=car.port, source=source,
                                                   timeout=timeout, **kw))))
        meta = {"DATENTYP": "unsigned int", "EINHEIT": "-"}
        p.set_channels([("ILAG2", 0x588A, meta)])
        p.start()
        assert wait_for(lambda: p.trace and any(t[1] == "rx" for t in p.trace))
        time.sleep(0.2)
        p.stop()
        p.join(5)
    finally:
        gui.core = core
        car.close()
    assert wait_for(lambda: any(e["event"] == "session" for e in entries(quick_diagnostics)))
    s = next(e for e in entries(quick_diagnostics) if e["event"] == "session")
    assert s["samples"] >= 1 and s["reason"] == "stopped" and s["trace"]


def test_report_from_the_menu(app, quick_diagnostics, tmp_path, monkeypatch):
    out = tmp_path / "menu_report.jsonl"
    monkeypatch.setattr(gui.filedialog, "asksaveasfilename", lambda **k: str(out))
    shown = []
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda t, m, **k: shown.append(m))
    diag.record("discover")
    app._save_diag_report()
    assert out.exists() and "Saved 1 entries" in shown[-1]
