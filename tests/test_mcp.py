"""The MCP server Claude uses: protocol, tools against the fake car, files,
and registering with Claude Code and Claude Desktop."""

import io
import json
import os
import types

import pytest

from n63read import core, diag, mcp

from fake_car import FakeCar


@pytest.fixture
def car(monkeypatch):
    c = FakeCar()
    monkeypatch.setattr(core.Hsfz.__init__, "__defaults__", (c.port, core.TESTER_ADDR, 3.0))
    yield c
    c.close()


def talk(*messages):
    """Feed messages through serve(); the replies, parsed."""
    stdin = io.BytesIO(b"".join(
        (m if isinstance(m, bytes) else json.dumps(m).encode()) + b"\n" for m in messages))
    stdout = io.BytesIO()
    mcp.serve(stdin, stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines()]


def call(tool, **arguments):
    reply = talk({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": tool, "arguments": arguments}})[0]["result"]
    return reply["content"][0]["text"], reply["isError"]


# --------------------------------------------------------------- protocol

def test_handshake_and_tool_list():
    init, tools = talk(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},     # no reply
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert init["result"]["protocolVersion"] == "2025-03-26"
    assert init["result"]["serverInfo"]["name"] == "n63read"
    names = [t["name"] for t in tools["result"]["tools"]]
    assert {"status", "connection_check", "read_channels", "read_faults",
            "read_log", "diagnostics_journal"} <= set(names)
    assert all(t["annotations"]["readOnlyHint"] for t in tools["result"]["tools"])


def test_an_unknown_protocol_version_gets_ours():
    init, = talk({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "1999-01-01"}})
    assert init["result"]["protocolVersion"] == mcp.PROTOCOLS[0]


def test_errors_are_json_rpc_errors():
    bad, unknown, ping = talk(b"{not json", {"jsonrpc": "2.0", "id": 5, "method": "nope"},
                              {"jsonrpc": "2.0", "id": 6, "method": "ping"})
    assert bad["error"]["code"] == -32700
    assert unknown["id"] == 5 and unknown["error"]["code"] == -32601
    assert ping == {"jsonrpc": "2.0", "id": 6, "result": {}}


def test_a_failing_tool_is_a_tool_error_not_a_crash():
    text, error = call("read_channels", ip="127.0.0.1")
    assert error and "names" in text
    text, error = call("no_such_tool")
    assert error


@pytest.mark.parametrize("args", [
    {"ip": "127.0.0.1", "names": ["--source", "0xF4"]},
    {"ip": "127.0.0.1", "names": ["ILAG2", "--csv"]},
    {"ip": "--timeout", "names": ["ILAG2"]},
    {"ip": "127.0.0.1", "names": ["ILAG2"], "target": "-1"},
])
def test_arguments_cannot_become_cli_options(args, monkeypatch):
    ran = []
    monkeypatch.setattr(mcp, "run_cli", lambda argv: ran.append(argv) or "")
    text, error = call("read_channels", **args)
    assert error and "not a valid value" in text and ran == []


def test_channel_names_follow_a_double_dash(monkeypatch):
    ran = []
    monkeypatch.setattr(mcp, "run_cli", lambda argv: ran.append(argv) or "")
    call("read_channels", ip="127.0.0.1", names=["ILAG2"])
    assert ran[0][-2:] == ["--", "ILAG2"]


# --------------------------------------------------------------- the car

def test_read_channels_from_the_car(car):
    text, error = call("read_channels", ip="127.0.0.1", names=["ILAG2", "HU_STATUS_GPS"])
    assert not error
    assert "ILAG2" in text and "3D position" in text


def test_no_car_is_a_tool_error():
    text, error = call("read_channels", ip="127.0.0.1", names=["ILAG2"])
    assert error and "cannot talk to 127.0.0.1" in text


def test_tool_output_never_reaches_the_protocol_stream(car):
    # Everything the CLI prints goes into the result, not between messages.
    replies = talk({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "read_gps", "arguments": {"ip": "127.0.0.1"}}})
    assert len(replies) == 1 and not replies[0]["result"]["isError"]


def test_search_channels():
    text, error = call("search_channels", query="lambda bank 2")
    assert not error and "ILAG2" in text


# ------------------------------------------------------------------ files

def test_list_and_read_a_log():
    path = os.path.join(core.logs_dir(), "drive.csv")
    with open(path, "w", encoding="utf-8") as f:
        f.write("Time,ILAG2,HU_STATUS_GPS\n10:00:00,1.0,3D position\n"
                "10:00:01,0.98,3D position\n10:00:02,1.02,no fix\n")
    text, error = call("list_files")
    assert not error and "drive.csv" in text
    text, error = call("read_log", name="drive.csv", tail=1)
    assert not error
    assert "3 rows" in text and "10:00:00 .. 10:00:02" in text
    assert "min 0.98" in text and "max 1.02" in text
    assert "2 distinct: 3D position; no fix" in text
    assert text.rstrip().endswith("10:00:02,1.02,no fix")


def test_read_log_only_reads_its_own_folders():
    text, error = call("read_log", name=r"..\..\Windows\win.ini")
    assert error and "list_files" in text


def test_journal_leaves_snapshots_out():
    diag.record("discover", hits=0, snapshot={"windows": "big"})
    diag.record("connect", ok=False)
    text, error = call("diagnostics_journal")
    assert not error
    entries = [json.loads(line) for line in text.splitlines()]
    assert [e["event"] for e in entries] == ["discover", "connect"]
    assert entries[0]["snapshot"].startswith("(left out")
    text, _ = call("diagnostics_journal", event="connect", full=True)
    assert json.loads(text)["event"] == "connect"


# ----------------------------------------------------------- registration

def test_register_with_claude_desktop_keeps_its_other_settings(tmp_path, monkeypatch):
    appdata = tmp_path / "Roaming"
    (appdata / "Claude").mkdir(parents=True)
    config = appdata / "Claude" / "claude_desktop_config.json"
    config.write_text(json.dumps({"theme": "dark", "mcpServers": {"other": {"command": "x"}}}))
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setattr(mcp, "_claude_code", lambda: None)

    done = mcp.register()
    assert done == [("Claude Desktop", True, "added to %s" % config)]
    saved = json.loads(config.read_text())
    assert saved["theme"] == "dark" and "other" in saved["mcpServers"]
    entry = saved["mcpServers"]["n63read"]
    assert [entry["command"]] + entry["args"] == mcp.server_command()
    assert (appdata / "Claude" / "claude_desktop_config.json.bak").exists()

    mcp.register(remove=True)
    assert "n63read" not in json.loads(config.read_text())["mcpServers"]


def test_a_broken_desktop_config_is_left_alone(tmp_path, monkeypatch):
    (tmp_path / "Claude").mkdir()
    config = tmp_path / "Claude" / "claude_desktop_config.json"
    config.write_text("{oops")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setattr(mcp, "_claude_code", lambda: None)
    (where, ok, message), = mcp.register()
    assert not ok and "left alone" in message and config.read_text() == "{oops"


def test_register_with_claude_code(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))                  # no Claude Desktop
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(mcp, "_claude_code", lambda: "claude")
    runs = []

    def run(args, **k):
        runs.append(args)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(mcp.subprocess, "run", run)
    assert mcp.register() == [("Claude Code", True, "added for all your projects")]
    assert runs[0][:4] == ["claude", "mcp", "remove", "-s"]
    assert runs[1][:7] == ["claude", "mcp", "add", "-s", "user", "n63read", "--"]
    assert runs[1][7:] == mcp.server_command()


def test_cli_register_says_when_there_is_no_claude(tmp_path, monkeypatch, capsys):
    from n63read import cli
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(mcp, "_claude_code", lambda: None)
    cli.main(["mcp", "--register"])
    assert "Neither Claude Code nor Claude Desktop" in capsys.readouterr().out


def test_gui_connect_to_claude(app, monkeypatch):
    gui = pytest.importorskip("n63read.gui")
    from conftest import pump
    shown = []
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda t, m, **k: shown.append(m))
    monkeypatch.setattr(mcp, "_claude_code", lambda: None)
    monkeypatch.setattr(mcp, "register", lambda: [("Claude Desktop", True, "added to x")])
    app._connect_claude()
    assert pump(app, lambda: shown)
    assert "Claude Desktop: added to x" in shown[0] and "Restart Claude Desktop" in shown[0]

    monkeypatch.setattr(mcp, "register", lambda: [])
    app._connect_claude()
    assert pump(app, lambda: len(shown) == 2)
    assert "Neither Claude Code nor Claude Desktop" in shown[1]
