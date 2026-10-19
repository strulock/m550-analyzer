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

import collections
import contextlib
import csv
import datetime
import functools
import glob
import io
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time

from n63read import __version__, core, diag, engine, share, update

SERVER_NAME = "n63read"
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")    # newest first
MAX_TEXT = 60000                # characters per tool result
SAMPLE_MAX = 60.0               # seconds the sample tool reads, at most
CONNECT_WAIT = 20.0             # s for a session to connect
WINDOW_ROWS = 500               # rows read_log shows from a window
EVENTS_MAX = 200                # events find_events lists
# The car tools borrow the app's (or a running log's) connection when one is
# open. Otherwise they connect themselves, as tester 0xF6 (bank 2: 0xF7), so
# the app connecting meanwhile as 0xF4 cannot knock either off.
SOURCE = "0xF6"

INSTRUCTIONS = """\
n63read reads a 2018 BMW M550i (G30, N63TU2 V8) over the OBD Ethernet (ENET)
port: live values, fault memory and GPS, read-only. It also keeps drive logs
(CSV, in Documents\\BMW Analyzer Logs) and a diagnostics journal of every
connection attempt.

Start with `status`. For a car that won't connect: `connection_check` (no IP
needed), then the journal (`diagnostics_journal`). Once the car is found
(`discover`), pass its IP to `scan_modules`, `read_channels`, `read_faults`,
`read_gps`; `sample` watches channels for a few seconds, and `start_log` /
`stop_log` keep a drive log. Find channel names with `search_channels`.
Review logs with `list_files` and `read_log`, `compare_banks` (bank 1 vs
bank 2) and `find_events`. Scans and checks of every module take a minute
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
     "description": "Read channels once and decode them, e.g. ILAG1, ILAG2 (DME, bank 1) "
                    "or HU_STATUS_GPS (head unit). Other modules' channels go to their own "
                    "module whatever target says. Bank 2 (cylinders 5-8) is the second "
                    "DME, 0x13: add @0x13 to a DME channel, e.g. ILAG1@0x13.",
     "inputSchema": _ip({"names": {"type": "array", "items": {"type": "string"},
                                   "description": "Channel names (see search_channels)"},
                         "target": _target()}, required=("ip", "names"))},
    {"name": "read_faults", "car": True,
     "description": "Read and decode fault memory: the DME's, another module's, or with "
                    "all_modules every module that answers (a minute or more). Lists "
                    "codes that have failed; every_code adds the many that never did. "
                    "details adds what was stored with each fault: odometer, vehicle "
                    "time, fault class, power state, and how often it occurred.",
     "inputSchema": _ip({"target": _target(),
                         "all_modules": {"type": "boolean"},
                         "every_code": {"type": "boolean"},
                         "details": {"type": "boolean"},
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
    {"name": "sample", "car": True,
     "description": "Read channels for a few seconds (up to 60) and give each one's min, "
                    "mean, max and last value: a quick look at how something behaves, "
                    "e.g. lambda at idle on both banks (ILAG1, ILAG1@0x13).",
     "inputSchema": _ip({"names": {"type": "array", "items": {"type": "string"}},
                         "seconds": {"type": "number", "description": "Default 10"},
                         "interval": {"type": "number", "description":
                                      "Seconds between readings (default 0.25)"}},
                        required=("ip", "names"))},
    {"name": "start_log", "car": True,
     "description": "Start a drive log (CSV in the logs folder) of the named channels or a "
                    "template, e.g. 'Misfire hunt', 'Lambda and trims, both banks'. It runs "
                    "until stop_log; read it with read_log, compare_banks, find_events.",
     "inputSchema": _ip({"names": {"type": "array", "items": {"type": "string"}},
                         "template": {"type": "string"},
                         "interval": {"type": "number", "description": "Default 0.25 s"}})},
    {"name": "stop_log", "car": False,
     "description": "Stop the log start_log began; says how many rows and the file name.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "read_log", "car": False,
     "description": "Summarise a CSV from list_files: columns, rows, time span, and "
                    "min/mean/max of each numeric column; optionally the first or last rows. "
                    "With start/end (seconds), columns or resample (seconds per averaged "
                    "row), give those rows instead, as CSV.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "File name from list_files"},
         "head": {"type": "integer", "description": "Also show the first N rows (max 200)"},
         "tail": {"type": "integer", "description": "Also show the last N rows (max 200)"},
         "start": {"type": "number"}, "end": {"type": "number"},
         "columns": {"type": "array", "items": {"type": "string"}},
         "resample": {"type": "number"}},
         "required": ["name"]}},
    {"name": "compare_banks", "car": False,
     "description": "In a log, bank 1 against bank 2 for every channel logged both ways "
                    "(NAME and NAME@0x13): each bank's mean and the difference's mean and "
                    "largest value, with when it happened.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}},
                     "required": ["name"]}},
    {"name": "find_events", "car": False,
     "description": "What happened in a log, in time order: the alerts and reconnects it "
                    "recorded, when a column went above/below a limit and for how long, "
                    "and every rise of a counter (the misfire counters by default).",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"},
         "column": {"type": "string"}, "above": {"type": "number"},
         "below": {"type": "number"},
         "counters": {"type": "array", "items": {"type": "string"}}},
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


def _value(value):
    """A tool argument as one CLI argument. One starting with "-" would be
    read as an option (say --source, overriding the tester address)."""
    value = str(value)
    if value.startswith("-"):
        raise ToolError("not a valid value: %r" % value)
    return value


def _opt(argv, flag, value):
    if value not in (None, ""):
        argv += [flag, _value(value)]
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
    return run_cli(_opt(["scan", "--ip", _value(a["ip"]), "--source", SOURCE],
                        "--probe-timeout", a.get("probe_timeout")))


def tool_read_faults(a):
    argv = _opt(["faults", "--ip", _value(a["ip"]), "--source", SOURCE, "-v"],
                "--target", a.get("target"))
    if a.get("all_modules"):
        argv.append("--all")
    if a.get("every_code"):
        argv.append("--every-code")
    if a.get("details"):
        argv.append("--details")
    if a.get("save"):
        argv += ["--out", os.path.join(core.logs_dir(),
                                       time.strftime("faults_%Y-%m-%d_%H-%M-%S.csv"))]
    return run_cli(argv)


def tool_read_gps(a):
    return run_cli(["gps", "--ip", _value(a["ip"]), "--source", SOURCE])


def tool_check_modules(a):
    return run_cli(_opt(["check", "--ip", _value(a["ip"]), "--source", SOURCE],
                        "--module", a.get("module")))


@functools.lru_cache(maxsize=1)
def _search_catalog():
    """Every channel, loaded once for the life of the server (read only)."""
    cat = core.load_catalog()
    cat.update(core.load_module_catalogs())
    return core.add_derived(core.add_bank2(cat))


def tool_search_channels(a):
    words = str(a.get("query") or "").lower().split()
    if not words:
        raise ToolError("query: say what to look for")
    limit = int(a.get("limit") or 40)
    hits = []
    for name, meta in _search_catalog().items():
        text = " ".join([name, meta.get("INFO_EN") or "", meta.get("INFO") or "",
                         meta.get("JOB_INFO_EN") or "", meta.get("JOB_INFO") or ""]).lower()
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


DISTINCT_MAX = 1000         # distinct texts remembered per column


class _Column:
    """Running summary of one log column, so a log is read in one pass
    without holding it in memory."""

    def __init__(self):
        self.filled = self.n = 0
        self.lo, self.hi, self.total = math.inf, -math.inf, 0.0
        self.distinct = set()

    def add(self, cell):
        if cell == "":
            return
        self.filled += 1
        if len(self.distinct) < DISTINCT_MAX:
            self.distinct.add(cell)
        v = _number(cell)
        if v is not None:
            self.n += 1
            self.lo, self.hi, self.total = min(self.lo, v), max(self.hi, v), self.total + v


def tool_read_log(a):
    if any(a.get(k) not in (None, "", []) for k in ("start", "end", "columns", "resample")):
        return _log_window(a)
    path = _find(a["name"])
    if not path.lower().endswith(".csv"):
        raise ToolError("read_log reads CSVs; for the journal use diagnostics_journal")
    head_n = min(int(a.get("head") or 0), 200)
    tail_n = min(int(a.get("tail") or 0), 200) if a.get("tail") else 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is None:
            return "empty file"
        cols = [_Column() for _ in header]
        head, tail = [], collections.deque(maxlen=tail_n or None)
        count, first, last = 0, None, None
        for r in reader:
            count += 1
            if first is None:
                first = r
            last = r
            if len(head) < head_n:
                head.append(r)
            if tail_n:
                tail.append(r)
            for col, cell in zip(cols, r):
                col.add(cell)
    lines = ["%s: %d rows, %d columns" % (os.path.basename(path), count, len(header))]
    if count and header and header[0].lower() == "time":
        lines.append("time: %s .. %s" % (first[0] if first else "", last[0] if last else ""))
    for name, col in zip(header, cols):
        if col.n and col.n == col.filled and name.lower() != "time":
            lines.append("  %-40s min %-12g mean %-12g max %-12g (%d values)" % (
                name, col.lo, col.total / col.n, col.hi, col.n))
        else:
            sample = sorted(col.distinct)
            many = "+" if len(col.distinct) >= DISTINCT_MAX else ""
            lines.append("  %-40s %d%s distinct: %s" % (
                name, len(sample), many, "; ".join(sample[:6])
                + (" ..." if len(sample) > 6 else "")))
    for key, part in (("head", head), ("tail", list(tail) if tail_n else [])):
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


# ------------------------------------------------- reading the car directly
#
# These call core rather than the CLI, so they return what they read, not a
# report printed for a person: changing the CLI's wording can't break them.

def _hex(value, default):
    if value in (None, ""):
        return default
    try:
        address = int(str(value), 0)
    except ValueError:
        address = -1
    if not 0 <= address <= 0xFF:
        raise ToolError("not an address: %r (e.g. 0x12)" % value)
    return address


def _chans(names):
    cat = _search_catalog()
    names = [str(n) for n in names or []]
    if not names:
        raise ToolError("names: give at least one channel")
    unknown = [n for n in names if n not in cat]
    if unknown:
        raise ToolError("unknown channel(s): %s; see search_channels" % ", ".join(unknown))
    return [(n, int(cat[n]["ID"], 16), cat[n]) for n in core.with_inputs(names)]


def _tester(ip):
    try:
        return share.open_tester(_value(ip), int(SOURCE, 16), 2.0)
    except (OSError, core.HsfzError) as e:
        raise ToolError("cannot talk to %s: %s" % (ip, e)) from None


def _read_all(h, reader, chans, cycles=12):
    """One reading of every channel: module records come a few per cycle,
    so read until none is still waiting."""
    sample = {}
    for _ in range(cycles):
        sample = reader.read(h, chans)
        if not any(v[0] is None and v[2] == "waiting" for v in sample.values()):
            break
    return sample


def _shown(name, entry, enums):
    value, unit, note = entry
    meta = _search_catalog()[name]
    if value is None:
        return "%-44s -          (%s)" % (name, note or "no value")
    words = core.enum_text(value, meta, enums, "text_en") or ""
    unit = unit if unit not in ("", "-") else (meta.get("EINHEIT") or "")
    return "%-44s %-10s %s%s" % (name, value, "" if unit in ("", "-") else unit,
                                 "  (%s)" % words if words else "")


def tool_read_channels(a):
    chans = _chans(a.get("names"))
    reader = core.ChannelReader(core.record_layouts(_search_catalog()),
                                _hex(a.get("target"), core.DME_MASTER), batch=True)
    try:
        with _tester(a["ip"]) as h:
            sample = _read_all(h, reader, chans)
    except core.HsfzError as e:
        raise ToolError("cannot talk to %s: %s" % (a["ip"], e)) from None
    enums = core.load_enum_tables()
    return "\n".join(_shown(n, sample[n], enums) for n, _, _ in chans)


class _Collector:
    """What a Claude session has to say (engine.Session's messages): how it
    connected, any error, and the samples if wanted."""

    def __init__(self, keep_samples=False):
        self.keep = keep_samples
        self.samples = []
        self.status = self.error = None
        self.changed = threading.Event()

    def put(self, message):
        kind = message[0]
        if kind == "sample" and self.keep:
            self.samples.append(message[2])
        elif kind == "status":
            self.status = message[1]
            self.changed.set()
        elif kind == "error":
            self.error = message[1].split("\n")[0]
            self.changed.set()


def _start_session(ip, chans, interval, keep_samples=False):
    """A Session like the app's: it borrows the car if the app (or a log)
    holds it and lends it if Claude holds it, so neither knocks the other
    off, and whichever carries on takes the car over when the other stops."""
    out = _Collector(keep_samples)
    session = engine.Session(_value(ip), core.DME_MASTER, int(SOURCE, 16), 2.0, out=out,
                             layouts=core.record_layouts(_search_catalog()),
                             interval=max(float(interval or 0.25), 0.05))
    session.set_channels(chans)
    session.start()
    deadline = time.time() + CONNECT_WAIT
    while time.time() < deadline and out.status != "connected" and out.error is None:
        out.changed.wait(0.1)
        out.changed.clear()
    if out.status != "connected":
        session.stop()
        raise ToolError("cannot talk to %s: %s" % (ip, out.error or "no answer"))
    return session, out


def tool_sample(a):
    """Read channels for a few seconds and summarise each."""
    chans = _chans(a.get("names"))
    seconds = min(max(float(a.get("seconds") or 10), 1.0), SAMPLE_MAX)
    session, out = _start_session(a["ip"], chans, a.get("interval"), keep_samples=True)
    time.sleep(seconds)
    session.stop()
    session.join(10)
    values = {n: [] for n, _, _ in chans}
    texts = {}
    for sample in out.samples:
        for name, (value, _unit, note) in sample.items():
            if isinstance(value, (int, float)):
                values[name].append(float(value))
            elif value is not None:
                texts[name] = value
            elif note and not values[name]:
                texts.setdefault(name, "(%s)" % note)
    lines = ["%d cycles in %g s%s:" % (len(out.samples), seconds,
                                       " (borrowing the app's connection)"
                                       if session.borrowed else "")]
    for name, _, meta in chans:
        unit = meta.get("EINHEIT") or ""
        unit = "" if unit in ("-", "") else " " + unit
        v = values[name]
        if v:
            lines.append("  %-44s min %-10g mean %-10g max %-10g last %g%s  (%d)" % (
                name, min(v), sum(v) / len(v), max(v), v[-1], unit, len(v)))
        else:
            lines.append("  %-44s %s" % (name, texts.get(name, "no value")))
    return "\n".join(lines)


_logger = None                  # (session, path) of the log start_log began


def tool_start_log(a):
    global _logger
    if _logger is not None and _logger[0].is_alive():
        raise ToolError("already logging to %s; stop_log first" % _logger[1])
    names = list(a.get("names") or [])
    template = a.get("template")
    if template:
        saved = dict(core.BUILTIN_TEMPLATES, **core.load_templates())
        if template not in saved:
            raise ToolError("no template %r; available: %s"
                            % (template, ", ".join(sorted(saved, key=str.lower))))
        names = saved[template] + [n for n in names if n not in saved[template]]
    chans = _chans(names)
    path = os.path.join(core.logs_dir(), time.strftime("n63_%Y-%m-%d_%H-%M-%S_claude.csv"))
    session, _out = _start_session(a["ip"], chans, a.get("interval"))
    session.start_log(path)
    _logger = (session, path)
    return "Logging %d channel(s) to %s%s. stop_log ends it." % (
        len(chans), path, ", borrowing the app's connection" if session.borrowed else "")


def tool_stop_log(a):
    global _logger
    if _logger is None:
        raise ToolError("not logging")
    (session, path), _logger = _logger, None
    session.stop()
    session.join(10)
    return "Stopped: %d rows in %s" % (session.rows, os.path.basename(path))


# ----------------------------------------------------- looking into logs

def _log_rows(name):
    from n63read import playback
    path = _find(name)
    if not path.lower().endswith(".csv"):
        raise ToolError("read_log reads CSVs; for the journal use diagnostics_journal")
    try:
        return path, playback.read_log(path)
    except (OSError, ValueError, csv.Error) as e:
        raise ToolError("cannot read %s: %s" % (os.path.basename(path), e)) from None


def _number_of(v):
    return float(v) if isinstance(v, (int, float)) else None


def _log_window(a):
    """read_log with start/end/columns/resample: the rows asked for, as CSV."""
    path, (names, rows) = _log_rows(a["name"])
    cols = [str(c) for c in a.get("columns") or names]
    missing = [c for c in cols if c not in names]
    if missing:
        raise ToolError("no column(s) %s; the log has: %s" % (", ".join(missing),
                                                               ", ".join(names)))
    start = float(a["start"]) if a.get("start") not in (None, "") else rows[0][0]
    end = float(a["end"]) if a.get("end") not in (None, "") else rows[-1][0]
    picked = [(t, v) for t, v in rows if start <= t <= end]
    step = float(a.get("resample") or 0)
    if step > 0:
        buckets = {}
        for t, v in picked:
            buckets.setdefault(int((t - start) // step), []).append(v)
        out = []
        for k in sorted(buckets):
            row = [round(start + k * step, 3)]
            for c in cols:
                nums = [x for x in (_number_of(v.get(c)) for v in buckets[k]) if x is not None]
                texts = [v.get(c) for v in buckets[k] if v.get(c) is not None]
                row.append(round(sum(nums) / len(nums), 6) if nums else
                           (texts[-1] if texts else ""))
            out.append(row)
    else:
        out = [[t] + ["" if v.get(c) is None else v.get(c) for c in cols] for t, v in picked]
    lines = ["%s: %d of %d rows from %g to %g s%s" % (
        os.path.basename(path), len(out), len(rows), start, end,
        ", averaged over %g s" % step if step > 0 else "")]
    if len(out) > WINDOW_ROWS:
        lines.append("(showing the first %d; narrow start/end or resample more)" % WINDOW_ROWS)
    lines.append(",".join(["Time"] + cols))
    lines += [",".join(str(x) for x in row) for row in out[:WINDOW_ROWS]]
    return "\n".join(lines)


def tool_compare_banks(a):
    """Bank 1 against bank 2 for every NAME / NAME@0x13 pair in a log."""
    path, (names, rows) = _log_rows(a["name"])
    pairs = core.bank_pairs(names)
    if not pairs:
        return ("%s has no bank pairs (NAME and NAME@0x13). Log with Compare banks on, "
                "or a ready-made 'both banks' set." % os.path.basename(path))
    lines = ["%s: %d pair(s), bank 1 minus bank 2:" % (os.path.basename(path), len(pairs))]
    for b1, b2 in pairs:
        both = [(t, _number_of(v.get(b1)), _number_of(v.get(b2))) for t, v in rows]
        both = [(t, x, y) for t, x, y in both if x is not None and y is not None]
        if not both:
            lines.append("  %-40s no rows with both banks" % b1)
            continue
        diffs = [(t, x - y) for t, x, y in both]
        t_max, d_max = max(diffs, key=lambda p: abs(p[1]))
        mean = lambda xs: sum(xs) / len(xs)                       # noqa: E731
        lines.append("  %-40s bank 1 mean %-10g bank 2 mean %-10g difference mean %+g, "
                     "largest %+g at %g s (%d rows)" % (
                         b1, mean([x for _, x, _ in both]), mean([y for _, _, y in both]),
                         mean([d for _, d in diffs]), d_max, t_max, len(both)))
    return "\n".join(lines)


def _log_events(path):
    """(time, text) from a log's Event column, if it has one."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader, [])
        if core.LOG_EVENT not in header:
            return []
        i = header.index(core.LOG_EVENT)
        return [(r[0], r[i]) for r in reader if len(r) > i and r[i]]


def tool_find_events(a):
    """Alerts and reconnects the log recorded, threshold crossings, and
    counters going up (misfire counters by default)."""
    path, (names, rows) = _log_rows(a["name"])
    events = [(float(t) if _is_float(t) else t, text) for t, text in _log_events(path)]
    column = a.get("column")
    above, below = a.get("above"), a.get("below")
    if column:
        if column not in names:
            raise ToolError("no column %s; the log has: %s" % (column, ", ".join(names)))
        if above is None and below is None:
            raise ToolError("give above or below with column")
        outside, since = False, None
        for t, v in rows:
            x = _number_of(v.get(column))
            if x is None:
                continue
            now = (above is not None and x > float(above)) or \
                (below is not None and x < float(below))
            if now and not outside:
                since = t
                events.append((t, "%s %s (%g)" % (column, "above %g" % float(above)
                                                  if above is not None and x > float(above)
                                                  else "below %g" % float(below), x)))
            elif outside and not now:
                events.append((t, "%s back in range after %.1f s" % (column, t - since)))
            outside = now
    counters = a.get("counters") or [n for n in names if n.startswith("ZAEHLER_AUSSETZ")]
    for name in counters:
        if name not in names:
            continue
        prev = None
        for t, v in rows:
            x = _number_of(v.get(name))
            if x is None:
                continue
            if prev is not None and x > prev:
                events.append((t, "%s went up %g -> %g" % (name, prev, x)))
            prev = x
    events.sort(key=lambda e: e[0] if isinstance(e[0], float) else 0.0)
    if not events:
        return "%s: no events found." % os.path.basename(path)
    lines = ["%s: %d event(s)%s" % (os.path.basename(path), len(events),
                                    ", the first %d:" % EVENTS_MAX
                                    if len(events) > EVENTS_MAX else ":")]
    lines += ["  %10s s  %s" % (t if not isinstance(t, float) else "%.2f" % t, text)
              for t, text in events[:EVENTS_MAX]]
    return "\n".join(lines)


def _is_float(text):
    try:
        float(text)
        return True
    except (TypeError, ValueError):
        return False


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
    try:
        serve()
    except KeyboardInterrupt:
        pass                            # closed (an update, Ctrl+C): just stop
