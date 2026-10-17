"""
n63read.diag - connection diagnostics, recorded for later analysis.

Every Discover, Connect, session end and car check appends one JSON line to
a daily journal in %APPDATA%\\n63read\\diagnostics\\ (kept 30 days):

- the PC's network snapshot: adapters and drivers, interfaces, network
  profiles, firewall profiles and n63read's firewall rules, plus the text of
  `ipconfig /all`, `arp -a` and `route print -4`;
- what was sent and every datagram that came back, with timings;
- the devices seen on the car's link (ARP);
- for a failed connect, which diagnostic ports answer (6801 HSFZ, 13400 DoIP);
- for a session, the first frames on the wire (raw bytes), how long it ran,
  what was read and why it ended;
- a plain-language verdict.

With administrator rights, `Capture` also records the packets themselves
(pktmon) to a .pcapng beside the journal.

`report()` gathers recent entries into one file to send to someone, and
`n63read diag` runs the whole check from the command line.

Nothing here talks to a module beyond the discovery broadcast and opening a
TCP connection: read-only like the rest. The journal holds addresses, MACs
and the car's identification reply (which includes the VIN); it stays on
this PC unless a report is sent.
"""

import datetime
import glob
import json
import os
import socket
import subprocess
import sys
import threading
import time

from n63read import __version__, core

KEEP_DAYS = 30
enabled = True                  # the GUI's Diagnostics > Record switch
_lock = threading.Lock()
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# One PowerShell call for the structured Windows side of the snapshot. Enum
# values become text so the JSON reads "Private", not 1.
_PS_SNAPSHOT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$rules = @(Get-NetFirewallRule | Where-Object { $_.DisplayName -match 'n63|python' })
@{
  adapters  = @(Get-NetAdapter -IncludeHidden | ForEach-Object { @{ name = $_.Name;
                description = $_.InterfaceDescription; status = "$($_.Status)"; link = "$($_.LinkSpeed)";
                mac = $_.MacAddress; ifindex = $_.ifIndex; driver = $_.DriverVersion;
                driver_provider = $_.DriverProvider; driver_date = "$($_.DriverDate)";
                media = "$($_.MediaConnectionState)"; duplex = "$($_.FullDuplex)" } });
  interfaces = @(Get-NetIPInterface -AddressFamily IPv4 | ForEach-Object { @{ alias = $_.InterfaceAlias;
                metric = $_.InterfaceMetric; dhcp = "$($_.Dhcp)"; state = "$($_.ConnectionState)";
                mtu = $_.NlMtu } });
  addresses = @(Get-NetIPAddress -AddressFamily IPv4 | ForEach-Object { @{ alias = $_.InterfaceAlias;
                ip = $_.IPAddress; prefix = $_.PrefixLength; origin = "$($_.PrefixOrigin)";
                state = "$($_.AddressState)" } });
  profiles  = @(Get-NetConnectionProfile | ForEach-Object { @{ alias = $_.InterfaceAlias; name = $_.Name;
                category = "$($_.NetworkCategory)"; ipv4 = "$($_.IPv4Connectivity)" } });
  firewall  = @(Get-NetFirewallProfile | ForEach-Object { @{ name = $_.Name; enabled = "$($_.Enabled)";
                inbound = "$($_.DefaultInboundAction)"; outbound = "$($_.DefaultOutboundAction)";
                log_blocked = "$($_.LogBlocked)"; log_file = $_.LogFileName } });
  rules     = @($rules | ForEach-Object { $app = $_ | Get-NetFirewallApplicationFilter;
                $port = $_ | Get-NetFirewallPortFilter;
                @{ name = $_.DisplayName; enabled = "$($_.Enabled)"; direction = "$($_.Direction)";
                   action = "$($_.Action)"; profile = "$($_.Profile)"; program = $app.Program;
                   protocol = "$($port.Protocol)"; local_port = "$($port.LocalPort)" } });
  neighbours = @(Get-NetNeighbor -AddressFamily IPv4 | Where-Object { $_.IPAddress -like '169.254.*' } |
                ForEach-Object { @{ alias = $_.InterfaceAlias; ip = $_.IPAddress;
                mac = $_.LinkLayerAddress; state = "$($_.State)" } })
} | ConvertTo-Json -Depth 5 -Compress
"""


# ------------------------------------------------------------------ journal

def journal_dir():
    """Next to the answers store, so tests' temp redirect covers it too."""
    return os.path.join(os.path.dirname(core.availability_path()), "diagnostics")


def record(event, **fields):
    """Append one event to today's journal. Never raises: diagnostics must
    not be the thing that breaks a session."""
    if not enabled:
        return None
    entry = {"time": datetime.datetime.now().isoformat(timespec="milliseconds"),
             "event": event, "version": __version__}
    entry.update(fields)
    path = os.path.join(journal_dir(), datetime.date.today().isoformat() + ".jsonl")
    try:
        line = json.dumps(entry, default=str)
        with _lock:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except (OSError, TypeError, ValueError):
        return None
    return path


def record_later(event, **fields):
    """record() with a fresh snapshot, in the background (it takes ~10 s)."""
    if not enabled:
        return None
    bind = fields.pop("bind", None)

    def work():
        record(event, snapshot=snapshot(bind), **fields)
    t = threading.Thread(target=work, daemon=True)
    t.start()
    return t


def prune(keep_days=KEEP_DAYS):
    """Delete journal files (and captures) older than keep_days."""
    cutoff = datetime.date.today() - datetime.timedelta(days=keep_days)
    for path in glob.glob(os.path.join(journal_dir(), "*")):
        try:
            if datetime.date.fromisoformat(os.path.basename(path)[:10]) < cutoff:
                os.remove(path)
        except (ValueError, OSError):
            pass


def report(path, days=7):
    """Write the last `days` of journal entries to `path`. Returns the count.

    Packet captures are separate files beside the journal; the report names
    them so they can be sent along.
    """
    cutoff = datetime.date.today() - datetime.timedelta(days=days)
    lines, captures = [], []
    for f in sorted(glob.glob(os.path.join(journal_dir(), "*"))):
        try:
            if datetime.date.fromisoformat(os.path.basename(f)[:10]) < cutoff:
                continue
        except ValueError:
            continue
        if f.endswith(".pcapng"):
            captures.append(os.path.basename(f))
        elif f.endswith(".jsonl"):
            with open(f, encoding="utf-8") as fh:
                lines.extend(line for line in fh if line.strip())
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"report": "n63read connection diagnostics", "version": __version__,
                            "created": datetime.datetime.now().isoformat(timespec="seconds"),
                            "entries": len(lines), "captures": captures,
                            "journal": journal_dir()}) + "\n")
        f.writelines(lines)
    return len(lines)


# ----------------------------------------------------------------- snapshot

def _run(cmd, timeout=15):
    """Output of a command, or a short error; never raises."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                             errors="replace", creationflags=_NO_WINDOW)
        return out.stdout if out.stdout.strip() else (out.stderr or "").strip()
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return "error: %s" % e


def windows_snapshot(timeout=30):
    """Structured adapter/interface/profile/firewall data plus the text of
    ipconfig /all, arp -a and route print; {"error": ...} off Windows."""
    if sys.platform != "win32":
        return {"error": "not Windows"}
    raw = _run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-Command", _PS_SNAPSHOT], timeout)
    try:
        snap = json.loads(raw)
    except ValueError:
        snap = {"error": raw.strip()[:500]}
    snap["ipconfig"] = _run(["ipconfig", "/all"])
    snap["arp"] = _run(["arp", "-a"])
    snap["route"] = _run(["route", "print", "-4"])
    return snap


def snapshot(bind_ip=None):
    """The PC's side of the connection, as far as it can be seen."""
    try:
        addrs = core.local_ipv4s()
    except Exception as e:                                          # noqa: BLE001
        addrs = ["error: %s" % e]
    snap = {"local_ipv4": addrs, "enet": core.link_local(addrs), "bind": bind_ip,
            "admin": is_admin(), "windows": windows_snapshot()}
    snap["enet_interface"] = interface_of(snap, bind_ip)
    return snap


def interface_of(snap, ip):
    """{alias, category, adapter, ...} for the interface that has `ip`."""
    win = snap.get("windows") or {}
    alias = next((a.get("alias") for a in _list(win.get("addresses")) if a.get("ip") == ip), None)
    if not alias:
        return None
    out = {"alias": alias}
    for p in _list(win.get("profiles")):
        if p.get("alias") == alias:
            out.update(category=p.get("category"), network=p.get("name"))
    for a in _list(win.get("adapters")):
        if a.get("name") == alias:
            out.update(adapter=a.get("description"), status=a.get("status"),
                       link=a.get("link"), driver=a.get("driver"))
    return out


def _list(v):
    """ConvertTo-Json writes a one-element array as an object."""
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def is_admin():
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:                                               # noqa: BLE001
        return False


# ---------------------------------------------------------------- the steps

def _probes():
    return ([(p, core.HSFZ_CONTROL_PORT) for p in core.HSFZ_IDENT_PROBES]
            + [(core.DOIP_IDENT_PROBE, core.DOIP_PORT)])


def discover(timeout=3.0, bind_ip=None, on_result=None):
    """core.discover plus a journal entry. Returns (hits, neighbours).

    `on_result(hits, neighbours)` is called as soon as discovery is done, so
    a GUI can show the answer while the slow snapshot is taken for the
    journal.
    """
    t0 = time.time()
    error, hits, received = None, [], []
    try:
        hits = core.discover(timeout, bind_ip, details=received)
    except TypeError:                                               # a stand-in without details
        hits = core.discover(timeout, bind_ip)
    except Exception as e:                                          # noqa: BLE001
        error = e
    near = [] if hits or not bind_ip or error else core.link_neighbours(bind_ip)
    seconds = round(time.time() - t0, 3)
    if on_result is not None and error is None:
        on_result(hits, near)
    if enabled:
        snap = snapshot(bind_ip)
        record("discover", seconds=seconds, timeout=timeout, snapshot=snap,
               sent=[{"to": d, "port": port, "bytes": probe.hex()}
                     for d in ("255.255.255.255", "169.254.255.255") for probe, port in _probes()],
               received=received,
               replies=[{"from": ip, "protocol": core.reply_protocol(p), "bytes": p.hex()}
                        for ip, p in hits],
               neighbours=near,
               error=None if error is None else "%s: %s" % (type(error).__name__, error),
               verdict=verdict(snap, hits, near, error))
    if error is not None:
        raise error
    return hits, near


def port_check(ip, port, timeout=2.0):
    """'open', 'refused', 'timeout' or the error: does anything listen there?"""
    t0 = time.time()
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            result = "open"
    except socket.timeout:
        result = "timeout"
    except ConnectionRefusedError:
        result = "refused"
    except OSError as e:
        result = "error: %s" % (e.strerror or e)
    return {"ip": ip, "port": port, "result": result, "seconds": round(time.time() - t0, 3)}


def connect_failed(ip, error, bind_ip=None):
    """After a failed connect: what answers at that address, who's on the link."""
    checks = [port_check(ip, core.HSFZ_DIAG_PORT), port_check(ip, core.DOIP_PORT)]
    near = core.link_neighbours(bind_ip) if bind_ip else []
    return record_later("connect", ip=ip, result="failed",
                        error="%s: %s" % (type(error).__name__, error),
                        ports=checks, neighbours=near, bind=bind_ip)


def verdict(snap, hits, near, error=None):
    """What most likely went wrong, in a sentence or two."""
    enet = snap.get("enet") or []
    iface = snap.get("enet_interface") or {}
    if error is not None:
        return "Discover failed: %s" % error
    if any(core.reply_protocol(p) == "HSFZ" for _, p in hits):
        return "The car answered (HSFZ) at %s." % hits[0][0]
    doip = [ip for ip, p in hits if core.reply_protocol(p) == "DoIP"]
    if doip:
        return core.doip_help(doip[0]).split("\n\n")[0]
    if hits:
        return ("Something at %s answered on the discovery port, but not as a BMW "
                "gateway: check that address is the car's." % hits[0][0])
    if not enet:
        return ("No 169.254.x.x address on this PC: the ENET adapter is not up "
                "(unplugged, no driver, or Windows still configuring it).")
    if near:
        return ("No reply to Discover, but %s is on the car's link: probably the car. "
                "Its reply may have been blocked (firewall) or malformed; try "
                "connecting to it." % near[0])
    if iface.get("category") == "Public":
        return ("No reply, and %s is a Public network: Windows Firewall drops the "
                "car's reply. Set it to Private." % iface.get("alias"))
    return ("No reply and no other device seen on the car's link: the car's "
            "Ethernet may be off (ignition, or the cable's activation line, "
            "OBD pin 8) or the adapter isn't connected to the car.")


# ------------------------------------------------------- packet capture

class Capture:
    """pktmon around a Discover/Connect, saved as .pcapng (administrator)."""

    def __init__(self, seconds=60):
        self.seconds = seconds
        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.base = os.path.join(journal_dir(), "%s_capture" % stamp)
        self.etl, self.pcap = self.base + ".etl", self.base + ".pcapng"
        self._timer = None
        self.done = threading.Event()
        self.result = None

    def start(self):
        if not is_admin():
            raise PermissionError("packet capture needs n63read run as administrator")
        os.makedirs(journal_dir(), exist_ok=True)
        _run(["pktmon", "stop"])                     # a capture left running
        out = _run(["pktmon", "start", "--capture", "--pkt-size", "0", "-s", "200",
                    "-f", self.etl])
        if "error" in out.lower() and "logger" not in out.lower():
            raise RuntimeError("pktmon: %s" % out.strip()[:300])
        record("capture", state="started", seconds=self.seconds, file=self.pcap)
        self._timer = threading.Timer(self.seconds, self.stop)
        self._timer.daemon = True
        self._timer.start()

    def stop(self):
        if self.done.is_set():
            return self.result
        if self._timer:
            self._timer.cancel()
        stop = _run(["pktmon", "stop"], timeout=60)
        conv = _run(["pktmon", "etl2pcap", self.etl, "-o", self.pcap], timeout=120)
        ok = os.path.exists(self.pcap)
        if ok:
            try:
                os.remove(self.etl)
            except OSError:
                pass
        self.result = self.pcap if ok else None
        record("capture", state="saved" if ok else "failed", file=self.result,
               output=(stop + "\n" + conv).strip()[-800:])
        self.done.set()
        return self.result


# --------------------------------------------------- the whole check (CLI)

def full_check(bind_ip=None, ip=None, timeout=3.0, say=print):
    """Snapshot, Discover, port checks and a read-only HSFZ exchange, saying
    each step as it goes. Returns the recorded result; result["ok"] is True
    when the car answered over HSFZ."""
    result = {"bind": bind_ip, "ip_given": ip}
    addrs = core.local_ipv4s()
    enet = core.link_local(addrs)
    say("This PC's IPv4 addresses: %s" % (", ".join(addrs) or "none"))
    if not bind_ip:
        bind_ip = enet[0] if enet else None
        result["bind"] = bind_ip
    say("ENET (169.254.x.x) address: %s" % (bind_ip or "NONE - the adapter is not up"))

    say("Collecting Windows network details (about 10 s)...")
    snap = snapshot(bind_ip)
    result["snapshot"] = snap
    iface = snap.get("enet_interface") or {}
    if iface:
        say("  %s: %s, %s, network profile %s, driver %s" % (
            iface.get("alias"), iface.get("adapter"), iface.get("link"),
            iface.get("category") or "?", iface.get("driver") or "?"))
    win = snap.get("windows") or {}
    for fw in _list(win.get("firewall")):
        say("  firewall %-8s enabled=%s inbound=%s" % (fw.get("name"), fw.get("enabled"),
                                                       fw.get("inbound")))
    for rule in _list(win.get("rules")):
        say("  rule %r %s %s %s profile=%s" % (rule.get("name"), rule.get("direction"),
                                               rule.get("action"), rule.get("enabled"),
                                               rule.get("profile")))

    say("Discover (HSFZ on UDP %d, DoIP on UDP %d)..." % (core.HSFZ_CONTROL_PORT, core.DOIP_PORT))
    received, hits, near = [], [], []
    try:
        hits = core.discover(timeout, bind_ip, details=received)
    except Exception as e:                                          # noqa: BLE001
        result["discover_error"] = "%s: %s" % (type(e).__name__, e)
        say("  failed: %s" % e)
    for r in received:
        say("  %5d ms  %s" % (r["ms"], r.get("error") or "%s:%s  %s" % (
            r["from"], r["port"], r["bytes"][:64])))
    if not received:
        say("  no reply")
    if bind_ip:
        near = core.link_neighbours(bind_ip)
        say("Devices seen on the car's link: %s" % (", ".join(near) or "none"))
    result.update(received=received, neighbours=near,
                  replies=[{"from": h, "protocol": core.reply_protocol(p), "bytes": p.hex()}
                           for h, p in hits])

    candidates = []
    for c in [ip] + [h for h, _ in hits] + near:
        if c and c not in candidates and c not in addrs:
            candidates.append(c)
    result["ports"] = []
    for c in candidates:
        for port in (core.HSFZ_DIAG_PORT, core.DOIP_PORT):
            check = port_check(c, port)
            result["ports"].append(check)
            say("Port %s:%d -> %s (%.2f s)" % (c, port, check["result"], check["seconds"]))

    result["ok"] = False
    for c in candidates:
        if not any(p["ip"] == c and p["port"] == core.HSFZ_DIAG_PORT and p["result"] == "open"
                   for p in result["ports"]):
            continue
        trace, answers = [], {}
        try:
            with core.Hsfz(c, timeout=2.0, trace=trace) as h:
                for target in (0x10, core.DME_MASTER):
                    try:
                        h.tester_present(target)
                        answers["0x%02X" % target] = "answers"
                    except core.NegativeResponse as e:
                        answers["0x%02X" % target] = "answers (negative 0x%02X)" % e.code
                    except core.HsfzError as e:
                        answers["0x%02X" % target] = "no answer (%s)" % e
        except Exception as e:                                      # noqa: BLE001
            answers["connect"] = "%s: %s" % (type(e).__name__, e)
        result.update(hsfz_ip=c, hsfz_answers=answers, trace=trace)
        result["ok"] = any(v.startswith("answers") for v in answers.values())
        for k, v in answers.items():
            say("HSFZ %s module %s: %s" % (c, k, v))
        break

    if result["ok"]:
        result["verdict"] = ("The car answers over HSFZ at %s. In n63read put %s in "
                             "the IP box and press Connect." % ((result["hsfz_ip"],) * 2))
    else:
        result["verdict"] = verdict(snap, hits, near, None)
    say("")
    say("Verdict: " + result["verdict"])
    path = record("diag", **result)
    say("Recorded in %s" % (path or "(diagnostics recording is off)"))
    return result
