r"""
n63read.mcp - let Claude use n63read: an MCP server on stdin/stdout.

`n63read-cli.exe mcp` speaks the Model Context Protocol (JSON-RPC 2.0, one
message per line) so Claude Code or Claude Desktop can check the connection
to the car, read channels and fault memory, and review the drive logs and
the diagnostics journal on this PC. register() adds it to both;
the app's Diagnostics > Connect to Claude does that with one click.

Every tool is read-only, like the rest of n63read: the car tools run the
same commands as the CLI, through the same allow-list of read services, and
the file tools only read the logs folder and the diagnostics journal.
"""

import contextlib
import csv
import datetime
import glob
import io
import json
import os
import shutil
import subprocess
import sys
import time

from n63read import __version__, core, diag, update

SERVER_NAME = "n63read"
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")    # newest first
MAX_TEXT = 60000                # characters per tool result

INSTRUCTIONS = """\
n63read reads a 2018 BMW M550i (G30, N63TU2 V8) over the OBD Ethernet (ENET)
port: live values, fault memory and GPS, read-only. It also keeps drive logs
(CSV, in Documents\\BMW Analyzer Logs) and a diagnostics journal of every
connection attempt.

Start with `status`. For a car that won't connect: `connection_check` (no IP
needed), then the journal (`diagnostics_journal`). Once the car is found
(`discover`), pass its IP to `scan_modules`, `read_channels`, `read_faults`,
`read_gps`. Find channel names with `search_channels`. Review logs with
`list_files` and `read_log`. Scans and checks of every module take a minute
or more."""


# ------------------------------------------------------------------ tools

def _ip(schema_props, required=("ip",)):
    props = {"ip": {"type": "string", "description": "The car's IP, from discover "
                                                     "(usually 169.254.x.x)"}}
    props.update(schema_props)
    return {"type": "object", "properties": props, "required": list(required)}


def _target():
    return {"type": "string", "description": "Module address in hex, e.g. 0x12 (DME, the "
                                             "default), 0x18 transmission, 0x63 head unit"}


TOOLS = [
    {"name": "status", "car": False,
     "description": "n63read's version, whether it's installed, this PC's IPv4 addresses "
                    "(the ENET one is 169.254.x.x) and where logs and the journal are.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "connection_check", "car": True,
     "description": "Check the connection to the car step by step, as `n63read diag` "
                    "does: network adapter and profile, firewall, Discover, ports, an "
                    "HSFZ exchange, then a verdict. About 20 seconds. Recorded in the journal.",
     "inputSchema": {"type": "object", "properties": {
         "bind": {"type": "string", "description": "This PC's ENET address (default: "
                                                   "its 169.254.x.x)"},
         "ip": {"type": "string", "description": "Also try this address for the car"}}}},
    {"name": "discover", "car": True,
     "description": "Find the car on the ENET link by broadcast; prints its IP and VIN.",
     "inputSchema": {"type": "object", "properties": {
         "bind": {"type": "string", "description": "This PC's ENET address (169.254.x.x)"},
         "timeout": {"type": "number", "description": "Seconds to wait (default 3)"}}}},
    {"name": "scan_modules", "car": True,
     "description": "Find which modules answer (addresses 0x00-0x7F). About probe_timeout "
                    "x 128 seconds.",
     "inputSchema": _ip({"probe_timeout": {"type": "number", "description":
                                           "Seconds to wait at each address (default 0.5)"}})},
    {"name": "read_channels", "car": True,
     "description": "Read channels once and decode them, e.g. ILAG1, ILAG2 (DME) or "
                    "HU_STATUS_GPS (head unit). Other modules' channels go to their own "
                    "module whatever target says.",
     "inputSchema": _ip({"names": {"type": "array", "items": {"type": "string"},
                                   "description": "Channel names (see search_channels)"},
                         "target": _target()}, required=("ip", "names"))},
    {"name": "read_faults", "car": True,
     "description": "Read and decode fault memory: the DME's, another module's, or with "
                    "all_modules every module that answers (a minute or more).",
     "inputSchema": _ip({"target": _target(),
                         "all_modules": {"type": "boolean"},
                         "save": {"type": "boolean", "description":
                                  "Also save the faults to a CSV in the logs folder"}})},
    {"name": "read_gps", "car": True,
     "description": "The head unit's GPS fix, position, speed and heading.",
     "inputSchema": _ip({})},
    {"name": "check_modules", "car": True,
     "description": "Check each module's catalog against the car: does every identifier "
                    "answer with the size the catalog expects? A few minutes.",
     "inputSchema": _ip({"module": _target()})},
    {"name": "search_channels", "car": False,
     "description": "Find channel names by name or description (English or German), "
                    "e.g. 'lambda', 'oil temp', 'GPS'.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"},
         "limit": {"type": "integer", "description": "Default 40"}}, "required": ["query"]}},
    {"name": "list_files", "car": False,
     "description": "Drive logs, fault lists and reports in the logs folder, and the "
                    "diagnostics journal and packet captures; newest first.",
     "inputSchema": {"type": "object", "properties": {
         "limit": {"type": "integer", "description": "Default 50"}}}},
    {"name": "read_log", "car": False,
     "description": "Summarise a CSV from list_files: columns, rows, time span, and "
                    "min/mean/max of each numeric column; optionally the first or last rows.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "File name from list_files"},
         "head": {"type": "integer", "description": "Also show the first N rows (max 200)"},
         "tail": {"type": "integer", "description": "Also show the last N rows (max 200)"}},
         "required": ["name"]}},
    {"name": "diagnostics_journal", "car": False,
     "description": "Recent entries of the diagnostics journal: every Discover, Connect, "
                    "session and check, with what was sent and received and a verdict. "
                    "Network snapshots are left out unless full is set.",
     "inputSchema": {"type": "object", "properties": {
         "days": {"type": "integer", "description": "Default 2"},
         "limit": {"type": "integer", "description": "Newest N entries (default 30)"},
         "event": {"type": "string", "description": "Only this event, e.g. discover, "
                                                    "connect, session, check"},
         "full": {"type": "boolean"}}}},
]


class ToolError(Exception):
    pass


def run_cli(argv):
    """Run one CLI command, capturing what it prints. Raises ToolError with
    the output if it fails."""
    from n63read import cli
    out = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            args = cli.build_parser().parse_args(argv)
            try:
                args.func(args)
            except (ConnectionError, TimeoutError, core.HsfzError) as e:
                sys.exit("cannot talk to %s: %s" % (getattr(args, "ip", "the car"), e))
        except SystemExit as e:
            code = e.code
            if code not in (None, 0) and not isinstance(code, int):
                print(code)
        except Exception as e:                                       # noqa: BLE001
            print("%s: %s" % (type(e).__name__, e))
            code = 1
    text = out.getvalue().strip()
    if code not in (None, 0):
        raise ToolError(text or "failed")
    return text or "(no output)"


def _opt(argv, flag, value):
    if value not in (None, ""):
        argv += [flag, str(value)]
    return argv


def tool_status(a):
    addrs = core.local_ipv4s()
    lines = ["n63read %s" % __version__,
             "installed: %s" % (update.install_location() or "no (portable or source)"),
             "this PC's IPv4: %s" % (", ".join(addrs) or "none"),
             "ENET (169.254.x.x): %s" % (", ".join(core.link_local(addrs))
                                         or "none - the ENET adapter is not up"),
             "logs folder: %s" % core.logs_dir(create=False),
             "diagnostics journal: %s" % diag.journal_dir()]
    return "\n".join(lines)


def tool_connection_check(a):
    return run_cli(_opt(_opt(["diag"], "--bind", a.get("bind")), "--ip", a.get("ip")))


def tool_discover(a):
    return run_cli(_opt(_opt(["discover"], "--bind", a.get("bind")),
                        "--timeout", a.get("timeout")))


def tool_scan_modules(a):
    return run_cli(_opt(["scan", "--ip", a["ip"]], "--probe-timeout", a.get("probe_timeout")))


def tool_read_channels(a):
    names = [str(n) for n in a.get("names") or []]
    if not names:
        raise ToolError("names: give at least one channel")
    return run_cli(_opt(["read", "--ip", a["ip"]], "--target", a.get("target")) + names)


def tool_read_faults(a):
    argv = _opt(["faults", "--ip", a["ip"], "-v"], "--target", a.get("target"))
    if a.get("all_modules"):
        argv.append("--all")
    if a.get("save"):
        argv += ["--out", os.path.join(core.logs_dir(),
                                       time.strftime("faults_%Y-%m-%d_%H-%M-%S.csv"))]
    return run_cli(argv)


def tool_read_gps(a):
    return run_cli(["gps", "--ip", a["ip"]])


def tool_check_modules(a):
    return run_cli(_opt(["check", "--ip", a["ip"]], "--module", a.get("module")))


def tool_search_channels(a):
    words = str(a.get("query") or "").lower().split()
    if not words:
        raise ToolError("query: say what to look for")
    limit = int(a.get("limit") or 40)
    cat = core.load_catalog()
    cat.update(core.load_module_catalogs())
    hits = []
    for name, meta in cat.items():
        text = " ".join([name, meta.get("INFO_EN") or "", meta.get("INFO") or ""]).lower()
        if all(w in text for w in words):
            hits.append((name, meta))
    lines = ["%d channel(s) match; module, unit, description:" % len(hits)]
    for name, meta in sorted(hits)[:limit]:
        unit = meta.get("EINHEIT") or ""
        lines.append("%s  [%s%s]  %s" % (
            name, meta.get("MODULE") or "0x12",
            "" if unit in ("", "-") else ", " + unit,
            meta.get("INFO_EN") or meta.get("INFO") or ""))
    if len(hits) > limit:
        lines.append("... %d more; narrow the query" % (len(hits) - limit))
    return "\n".join(lines)


def _files():
    """(name, path) of every file the file tools may read, newest first."""
    found = []
    for folder in (core.logs_dir(create=False), diag.journal_dir()):
        for path in glob.glob(os.path.join(folder, "*")):
            if os.path.isfile(path):
                found.append(path)
    found.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return found


def tool_list_files(a):
    files = _files()[:int(a.get("limit") or 50)]
    if not files:
        return "No logs or journal yet. Logs: %s" % core.logs_dir(create=False)
    lines = []
    for path in files:
        st = os.stat(path)
        lines.append("%s  %8.1f KB  %s  (%s)" % (
            datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            st.st_size / 1024, os.path.basename(path),
            "journal" if os.path.dirname(path) == diag.journal_dir() else "logs"))
    return "\n".join(lines)


def _find(name):
    for path in _files():
        if os.path.basename(path).lower() == os.path.basename(str(name)).lower():
            return path
    raise ToolError("no file %r in the logs folder or the journal; see list_files" % name)


def _number(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def tool_read_log(a):
    path = _find(a["name"])
    if not path.lower().endswith(".csv"):
        raise ToolError("read_log reads CSVs; for the journal use diagnostics_journal")
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    if not rows:
        return "empty file"
    header, body = rows[0], rows[1:]
    lines = ["%s: %d rows, %d columns" % (os.path.basename(path), len(body), len(header))]
    if body and header and header[0].lower() == "time":
        lines.append("time: %s .. %s" % (body[0][0], body[-1][0]))
    for i, col in enumerate(header):
        values = [_number(r[i]) for r in body if i < len(r) and r[i] != ""]
        nums = [v for v in values if v is not None]
        if nums and len(nums) == len(values) and col.lower() != "time":
            lines.append("  %-40s min %-12g mean %-12g max %-12g (%d values)" % (
                col, min(nums), sum(nums) / len(nums), max(nums), len(nums)))
        else:
            sample = sorted({r[i] for r in body if i < len(r) and r[i] != ""})
            lines.append("  %-40s %d distinct: %s" % (col, len(sample), "; ".join(sample[:6])
                                                       + (" ..." if len(sample) > 6 else "")))
    for key, part in (("head", body[:min(int(a.get("head") or 0), 200)]),
                      ("tail", body[-min(int(a.get("tail") or 0), 200):]
                       if a.get("tail") else [])):
        if part:
            lines.append("\n%s rows:" % key)
            lines.append(",".join(header))
            lines += [",".join(r) for r in part]
    return "\n".join(lines)


def tool_diagnostics_journal(a):
    days = int(a.get("days") or 2)
    cutoff = (datetime.date.today() - datetime.timedelta(days=days - 1)).isoformat()
    entries = []
    for path in sorted(glob.glob(os.path.join(diag.journal_dir(), "*.jsonl"))):
        if os.path.basename(path)[:10] < cutoff:
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if a.get("event") and entry.get("event") != a["event"]:
                    continue
                if not a.get("full") and "snapshot" in entry:
                    entry["snapshot"] = "(left out; full=true to include)"
                entries.append(entry)
    entries = entries[-int(a.get("limit") or 30):]
    if not entries:
        return "No journal entries in the last %d day(s)." % days
    return "\n".join(json.dumps(e, ensure_ascii=False) for e in entries)


HANDLERS = {t["name"]: globals()["tool_" + t["name"]] for t in TOOLS}


def call_tool(name, arguments):
    """(text, is_error) for one tool call."""
    if name not in HANDLERS:
        return "unknown tool %r" % name, True
    try:
        text, error = HANDLERS[name](arguments or {}), False
    except ToolError as e:
        text, error = str(e), True
    except KeyError as e:
        text, error = "missing argument %s" % e, True
    except Exception as e:                                           # noqa: BLE001
        text, error = "%s: %s" % (type(e).__name__, e), True
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT] + "\n... (cut at %d characters)" % MAX_TEXT
    diag.record("claude", tool=name, error=error)
    return text, error


# --------------------------------------------------------------- protocol

def _tool_list():
    out = []
    for t in TOOLS:
        out.append({"name": t["name"], "description": t["description"],
                    "inputSchema": t["inputSchema"],
                    "annotations": {"readOnlyHint": True, "openWorldHint": t["car"]}})
    return out


def handle(msg):
    """The response to one JSON-RPC message, or None for a notification."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _error(None, -32600, "invalid request")
    mid, method = msg.get("id"), msg.get("method")
    if "id" not in msg:
        return None                                  # notification: no reply
    params = msg.get("params") or {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        return _result(mid, {
            "protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
            "instructions": INSTRUCTIONS})
    if method == "ping":
        return _result(mid, {})
    if method == "tools/list":
        return _result(mid, {"tools": _tool_list()})
    if method == "tools/call":
        text, error = call_tool(params.get("name"), params.get("arguments"))
        return _result(mid, {"content": [{"type": "text", "text": text}], "isError": error})
    return _error(mid, -32601, "method not found: %s" % method)


def _result(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def serve(stdin=None, stdout=None):
    """Answer messages until stdin closes. stdout carries only protocol."""
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    for raw in stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw.decode("utf-8"))
        except ValueError:
            reply = _error(None, -32700, "parse error")
        else:
            reply = ([r for r in (handle(m) for m in msg) if r] or None
                     if isinstance(msg, list) else handle(msg))
        if reply:
            stdout.write(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")
            stdout.flush()


# ----------------------------------------------------------- registration

def server_command():
    """How Claude starts this server: the installed CLI exe, or Python."""
    exe = update.running_exe()
    if exe:
        return [os.path.join(os.path.dirname(exe), "n63read-cli.exe"), "mcp"]
    return [sys.executable, "-m", "n63read", "mcp"]


def desktop_configs():
    """Claude Desktop's config files on this PC (it needn't exist yet)."""
    found = []
    appdata = os.environ.get("APPDATA")
    if appdata and os.path.isdir(os.path.join(appdata, "Claude")):
        found.append(os.path.join(appdata, "Claude", "claude_desktop_config.json"))
    local = os.environ.get("LOCALAPPDATA")
    if local:                                   # the Microsoft Store install
        for folder in glob.glob(os.path.join(local, "Packages", "Claude_*", "LocalCache",
                                             "Roaming", "Claude")):
            found.append(os.path.join(folder, "claude_desktop_config.json"))
    return found


def _register_desktop(path, command, remove=False):
    config = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            text = f.read()
        try:
            config = json.loads(text) if text.strip() else {}
        except ValueError:
            raise ToolError("%s isn't valid JSON; left alone" % path) from None
        if not isinstance(config, dict):
            raise ToolError("%s isn't a JSON object; left alone" % path)
        with open(path + ".bak", "w", encoding="utf-8") as f:
            f.write(text)
    servers = config.setdefault("mcpServers", {})
    if remove:
        servers.pop(SERVER_NAME, None)
    else:
        servers[SERVER_NAME] = {"command": command[0], "args": command[1:]}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def _claude_code():
    return shutil.which("claude")


def register(remove=False):
    """Add (or remove) the server for Claude Code (user scope) and Claude
    Desktop, whichever are here. Returns [(where, ok, message)]."""
    command = server_command()
    done = []
    claude = _claude_code()
    if claude:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        def run(args):
            return subprocess.run([claude, "mcp"] + args, capture_output=True, text=True,
                                  timeout=60, creationflags=flags)
        try:
            run(["remove", "-s", "user", SERVER_NAME])          # absent is fine
            if remove:
                done.append(("Claude Code", True, "removed"))
            else:
                r = run(["add", "-s", "user", SERVER_NAME, "--"] + command)
                done.append(("Claude Code", r.returncode == 0,
                             "added for all your projects" if r.returncode == 0
                             else (r.stderr or r.stdout).strip()))
        except (OSError, subprocess.SubprocessError) as e:
            done.append(("Claude Code", False, str(e)))
    for path in desktop_configs():
        try:
            _register_desktop(path, command, remove)
            done.append(("Claude Desktop", True, ("removed from " if remove else
                                                  "added to ") + path))
        except (OSError, ToolError) as e:
            done.append(("Claude Desktop", False, str(e)))
    return done


def main(args):
    """`n63read mcp`: serve; with --register / --unregister, set Claude up."""
    if args.register or args.unregister:
        done = register(remove=args.unregister)
        if not done and args.unregister:
            print("Neither Claude Code nor Claude Desktop is on this PC; nothing to remove.")
            return
        if not done:
            print("Neither Claude Code nor Claude Desktop was found on this PC.\n"
                  "Install one, then run this again. To add it by hand, the command is:\n  %s"
                  % subprocess.list2cmdline(server_command()))
            return
        for where, ok, message in done:
            print("%-15s %s  %s" % (where, "ok    " if ok else "FAILED", message))
        if not args.unregister:
            print("Restart Claude Desktop to pick it up. In Claude Code, run `claude` and "
                  "ask, e.g., \"use n63read to check the connection to my car\".")
        return
    serve()
