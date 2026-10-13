#!/usr/bin/env python3
"""
n63read.py - read BMW N63TU2 DME measurements over ENET using BMW's HSFZ protocol.

Pure standard library. No dependencies. Read-only: this tool will only ever
send UDS services 0x22 (ReadDataByIdentifier), 0x19 (ReadDTCInformation) and
0x3E (TesterPresent). Services 0x2F (InputOutputControl), 0x31
(RoutineControl), 0x2E (Write) and 0x14 (ClearDiagnosticInformation) are
explicitly refused - those can command actuators or destroy evidence.

Reference data is loaded from CSVs extracted from BMW's SGBD (.prg) files:

  N63TU2_DME_readable_measurements.csv   live channels, scaling, data types
  BMW_fault_code_texts.csv               fault code -> text (DE and EN)
  BMW_dtc_status_byte.csv                statusOfDTC byte -> text
  BMW_enum_tables.csv                    value -> text enumerations
  BMW_freeze_frame_fields.csv            FS_LESEN_DETAIL field catalog
  G30_module_sgbd_map.csv                module address -> SGBD variant

Usage
-----
  # 1. Find the car on the network
  python3 n63read.py discover

  # 2. Find which modules answer (this is how you locate the slave DME)
  python3 n63read.py scan --ip 169.254.x.x

  # 3. Read one or more channels once
  python3 n63read.py read --ip 169.254.x.x ILAG1 ILAG2

  # 4. Log channels to CSV until Ctrl-C
  python3 n63read.py log --ip 169.254.x.x --interval 0.2 \
      --out bank2_test.csv ILAG1 ILAG2 ILUZ5 ILUZ7

  # 5. Read fault memory, decoded against the SGBD text catalog
  python3 n63read.py faults --ip 169.254.x.x --target 0x12

  # Target a module other than the master DME (0x12)
  python3 n63read.py read --ip 169.254.x.x --target 0x13 ILAG2
"""

import argparse
import csv
import os
import socket
import struct
import sys
import time

# ---------------------------------------------------------------- constants

HSFZ_DIAG_PORT = 6801          # TCP, diagnostic channel
HSFZ_CONTROL_PORT = 6811       # UDP, discovery broadcast
TESTER_ADDR = 0xF4             # standard tester source address
DME_MASTER = 0x12              # master DME, confirmed from CarData fault memory

CTRL_DIAG = 0x0001             # diagnostic request / response
CTRL_ACK = 0x0002              # acknowledge transfer (ECU echoes the request)
CTRL_IDENT = 0x0011            # vehicle identification data

NRC = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat",
    0x22: "conditionsNotCorrect",
    0x31: "requestOutOfRange",          # <- identifier not implemented here
    0x33: "securityAccessDenied",
    0x78: "responsePending",
}

# Read-only services. 0x19 reports fault memory; it cannot clear it (that is
# 0x14) and cannot write or command anything. Do not widen this set - see
# PROJECT.md, "Safety constraints".
ALLOWED_SERVICES = {0x19, 0x22, 0x3E}

DEFAULT_CSV = "N63TU2_DME_readable_measurements.csv"
FAULT_CSV = "BMW_fault_code_texts.csv"
STATUS_CSV = "BMW_dtc_status_byte.csv"
ENUM_CSV = "BMW_enum_tables.csv"
FREEZE_CSV = "BMW_freeze_frame_fields.csv"
MODULE_MAP_CSV = "G30_module_sgbd_map.csv"

DATA_FILES = (DEFAULT_CSV, FAULT_CSV, STATUS_CSV, ENUM_CSV, FREEZE_CSV,
              MODULE_MAP_CSV)


def resource_path(name):
    """Locate a bundled data file.

    Search order: PyInstaller's unpack dir, the directory holding this module,
    the executable's own folder, then the working directory. The last two let
    a user drop an updated CSV next to the exe without rebuilding.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(getattr(sys, "_MEIPASS", here), name),
        os.path.join(here, name),
        os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), name),
        os.path.join(os.getcwd(), name),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return candidates[0]


# ------------------------------------------------------------- HSFZ framing

class HsfzError(Exception):
    pass


class NegativeResponse(HsfzError):
    def __init__(self, service, code):
        self.service = service
        self.code = code
        name = NRC.get(code, "unknown")
        super().__init__("negative response to 0x%02X: 0x%02X (%s)" % (service, code, name))


class Hsfz:
    """
    HSFZ frame: [length:4 BE][control:2 BE][source:1][target:1][uds payload...]

    `length` counts the source and target bytes plus the payload, i.e. two
    more than the UDS payload length. Note this is NOT DoIP - BMW F/G cars
    use HSFZ on port 6801 with single-byte addressing and no routing
    activation step.
    """

    def __init__(self, ip, port=HSFZ_DIAG_PORT, source=TESTER_ADDR, timeout=3.0):
        self.ip = ip
        self.port = port
        self.source = source
        self.timeout = timeout
        self.sock = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def connect(self):
        self.sock = socket.create_connection((self.ip, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    # -- low level ---------------------------------------------------------

    def _send(self, target, payload):
        header = struct.pack(">IH", len(payload) + 2, CTRL_DIAG)
        self.sock.sendall(header + bytes([self.source, target]) + payload)

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise HsfzError("connection closed by ECU")
            buf += chunk
        return buf

    def _recv_frame(self):
        head = self._recv_exact(6)
        length, control = struct.unpack(">IH", head)
        body = self._recv_exact(length)
        if length >= 2:
            return control, body[0], body[1], body[2:]
        return control, None, None, body

    # -- request/response --------------------------------------------------

    def request(self, target, payload, retries=4):
        """Send a UDS payload and return the positive response payload."""
        service = payload[0]
        if service not in ALLOWED_SERVICES:
            raise ValueError(
                "refusing service 0x%02X - this tool only sends 0x19/0x22/0x3E"
                % service
            )

        self._send(target, payload)
        deadline = time.time() + self.timeout * (retries + 1)

        while time.time() < deadline:
            control, src, dst, data = self._recv_frame()

            # The gateway echoes the request back as an ack; ignore it.
            if control == CTRL_ACK:
                continue
            if control != CTRL_DIAG or not data:
                continue

            if data[0] == 0x7F and len(data) >= 3:
                code = data[2]
                if code == 0x78:          # responsePending - ECU is working
                    continue
                raise NegativeResponse(data[1], code)

            if data[0] == service + 0x40:
                return data
            # Not ours (another module chattering) - keep waiting.

        raise HsfzError("timed out waiting for response")

    def read_did(self, target, did):
        """UDS 0x22. Returns the raw payload bytes after the echoed identifier."""
        resp = self.request(target, bytes([0x22, (did >> 8) & 0xFF, did & 0xFF]))
        if len(resp) < 3:
            raise HsfzError("short response")
        echoed = (resp[1] << 8) | resp[2]
        if echoed != did:
            raise HsfzError("identifier mismatch: asked 0x%04X got 0x%04X" % (did, echoed))
        return resp[3:]

    def tester_present(self, target):
        return self.request(target, bytes([0x3E, 0x00]))

    def read_dtcs(self, target, status_mask=0xFF):
        """UDS 0x19 sub-function 0x02 (reportDTCByStatusMask).

        Returns [(code, status)] where code is the 6-hex-digit BMW fault code
        (3 bytes, matching BMW_fault_code_texts.csv) and status is the raw
        statusOfDTC byte (see BMW_dtc_status_byte.csv).

        A mask of 0xFF asks for every DTC the module holds regardless of
        state. Narrow it if you only want currently-present faults.
        """
        resp = self.request(target, bytes([0x19, 0x02, status_mask & 0xFF]))
        # 59 02 <availabilityMask> then 4-byte records: DTC[3] + status[1]
        if len(resp) < 3:
            return []
        body = resp[3:]
        out = []
        for i in range(0, len(body) - 3, 4):
            code = "%02X%02X%02X" % (body[i], body[i + 1], body[i + 2])
            out.append((code, body[i + 3]))
        return out


# --------------------------------------------------------------- discovery

def local_ipv4s():
    """Local IPv4 addresses, link-local (169.254.x.x) first - that's the ENET side."""
    addrs = set()
    try:
        addrs.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    for fam, _, _, _, sa in socket.getaddrinfo(socket.gethostname(), None,
                                               socket.AF_INET):
        addrs.add(sa[0])
    addrs.discard("127.0.0.1")
    return sorted(addrs, key=lambda a: (not a.startswith("169.254."), a))


def discover(timeout=3.0, bind_ip=None):
    """
    UDP broadcast on 6811. Returns [(ip, payload_bytes), ...].

    bind_ip pins the broadcast to one interface. On a machine with WiFi up,
    an unbound broadcast leaves via the default route and never reaches the
    car - so pass the USB adapter's 169.254.x.x address.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if bind_ip:
        s.bind((bind_ip, 0))
    s.settimeout(timeout)
    # Vehicle identification request: length 2, control 0x0011, no payload.
    probe = struct.pack(">IH", 2, CTRL_IDENT) + b"\x00\x00"
    for dest in ("255.255.255.255", "169.254.255.255"):
        try:
            s.sendto(probe, (dest, HSFZ_CONTROL_PORT))
        except OSError:
            pass

    found = []
    end = time.time() + timeout
    while time.time() < end:
        try:
            data, addr = s.recvfrom(1024)
        except socket.timeout:
            break
        found.append((addr[0], data))
    s.close()
    return found


# ------------------------------------------------------------ measurements

def load_catalog(path):
    """ARG name -> metadata dict, from the SGBD-derived CSV."""
    if not os.path.exists(path):
        path = resource_path(os.path.basename(path))
    if not os.path.exists(path):
        sys.exit("measurement CSV not found: %s\n"
                 "Pass --csv with the path to "
                 "N63TU2_DME_readable_measurements.csv" % path)
    cat = {}
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            cat[row["ARG"].strip()] = row
    return cat


def _load_lookup(name, key, value_cols):
    """Generic CSV -> {key: {col: val}} loader. Missing file is not fatal."""
    path = resource_path(name)
    if not os.path.exists(path):
        return {}
    table = {}
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            k = (row.get(key) or "").strip()
            if k:
                table[k.upper()] = {c: (row.get(c) or "").strip()
                                    for c in value_cols}
    return table


def load_fault_texts():
    """Fault code -> {text_de, text_en, source_module}."""
    return _load_lookup(FAULT_CSV, "code",
                        ("text_de", "text_en", "source_module"))


def load_status_texts():
    """statusOfDTC byte (as '0xNN') -> {text_de, text_en}."""
    return _load_lookup(STATUS_CSV, "status_byte", ("text_de", "text_en"))


def load_enum_tables():
    """Table name (upper-cased) -> {value: {text_de, text_en}}.

    The SGBD directory stores table names upper-cased while SG_Funktionen
    references them in mixed case, so every lookup here is case-insensitive.
    """
    path = resource_path(ENUM_CSV)
    if not os.path.exists(path):
        return {}
    tables = {}
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            name = (row.get("table_name") or "").strip().upper()
            val = (row.get("value") or "").strip()
            if not name or not val:
                continue
            tables.setdefault(name, {})[val.lower()] = {
                "text_de": (row.get("text_de") or "").strip(),
                "text_en": (row.get("text_en") or "").strip(),
            }
    return tables


_WIDTH = {
    "unsigned char": (1, False), "signed char": (1, True),
    "unsigned int": (2, False), "signed int": (2, True),
    "unsigned long": (4, False), "signed long": (4, True),
}


def decode(raw, meta):
    """Apply data type and MUL/DIV/ADD scaling. Returns (value, unit)."""
    dtype = (meta.get("DATENTYP") or "").strip().lower()
    unit = (meta.get("EINHEIT") or "").strip()
    if unit == "-":
        unit = ""

    if dtype not in _WIDTH:
        # data[n], strings, bitfields - hand back hex, don't guess.
        return raw.hex(), unit

    width, signed = _WIDTH[dtype]
    if len(raw) < width:
        return raw.hex(), unit

    # 'High' means most significant byte first, which is the common case.
    order = "little" if (meta.get("L/H") or "").strip().lower() == "low" else "big"
    value = int.from_bytes(raw[:width], order, signed=signed)

    def num(key, default):
        v = (meta.get(key) or "").strip()
        try:
            return float(v)
        except ValueError:
            return default

    mul, div, add = num("MUL", 1.0), num("DIV", 1.0), num("ADD", 0.0)
    if div == 0:
        div = 1.0
    return value * mul / div + add, unit


# -------------------------------------------------------------- operations

def op_discover(args):
    if args.list_interfaces:
        print("Local IPv4 addresses (link-local first):")
        for a in local_ipv4s():
            print("   ", a)
        return
    hits = discover(args.timeout, args.bind)
    if not hits:
        print("No response. Check you're on the adapter's network and the "
              "ignition is on (position 2).")
        return
    for ip, payload in hits:
        print("%-16s %s" % (ip, payload.hex()))
        tail = payload[6:]
        printable = bytes(c if 32 <= c < 127 else 0x2E for c in tail)
        print(" " * 17 + printable.decode("ascii", "replace"))


def op_faults(args):
    """Read fault memory via 0x19 and decode it against the SGBD catalogs."""
    texts = load_fault_texts()
    statuses = load_status_texts()
    lang = "text_de" if args.german else "text_en"

    with Hsfz(args.ip, source=args.source, timeout=args.timeout) as h:
        try:
            dtcs = h.read_dtcs(args.target, args.mask)
        except NegativeResponse as e:
            sys.exit("module 0x%02X refused 0x19: %s"
                     % (args.target, NRC.get(e.code, "nrc 0x%02X" % e.code)))

    if not dtcs:
        print("No fault codes reported by module 0x%02X." % args.target)
        return

    if not texts:
        print("(%s not found - showing raw codes)\n" % FAULT_CSV)

    print("%d fault code(s) from module 0x%02X:\n" % (len(dtcs), args.target))
    unknown = 0
    for code, status in sorted(dtcs):
        entry = texts.get(code.upper())
        if entry:
            desc = entry.get(lang) or entry.get("text_de") or ""
        else:
            desc = "(not in catalog)"
            unknown += 1
        skey = "0x%02X" % status
        sentry = statuses.get(skey.upper())
        stext = (sentry.get(lang) or sentry.get("text_de")) if sentry else ""
        print("  %-8s %s" % (code, desc))
        print("  %-8s status %s%s" % ("", skey, " - " + stext if stext else ""))
    if unknown:
        print("\n%d code(s) had no catalog entry. The SGBD for this module "
              "may not be among those extracted - see PROJECT.md." % unknown)


def op_scan(args):
    """
    Sweep target addresses with TesterPresent and report which answer.

    This is how you find the slave DME. Known: 0x12 master DME, 0x40 BDC,
    0x10 gateway. An unexpected responder near 0x12 is the candidate.
    """
    lo, hi = args.range
    print("Scanning targets 0x%02X-0x%02X (ignition on, engine off)...\n" % (lo, hi))
    alive = []
    with Hsfz(args.ip, source=args.source, timeout=args.timeout) as h:
        for target in range(lo, hi + 1):
            try:
                h.tester_present(target)
            except NegativeResponse as e:
                # A negative response still proves something is listening.
                alive.append((target, "responds (%s)" % NRC.get(e.code, "nrc 0x%02X" % e.code)))
                print("  0x%02X  responds (negative: %s)" % (target, NRC.get(e.code, "0x%02X" % e.code)))
            except HsfzError:
                pass
            except Exception as e:                      # noqa: BLE001
                print("  0x%02X  error: %s" % (target, e), file=sys.stderr)
            else:
                alive.append((target, "responds"))
                print("  0x%02X  responds" % target)
    print("\n%d module(s) answered." % len(alive))


def op_read(args):
    cat = load_catalog(args.csv)
    with Hsfz(args.ip, source=args.source, timeout=args.timeout) as h:
        for name in args.names:
            meta = cat.get(name)
            if not meta:
                print("%-34s UNKNOWN (not in catalog)" % name)
                continue
            did = int(meta["ID"], 16)
            try:
                raw = h.read_did(args.target, did)
            except NegativeResponse as e:
                print("%-34s %s  <- %s" % (name, meta["ID"], e))
                continue
            except HsfzError as e:
                print("%-34s %s  <- %s" % (name, meta["ID"], e))
                continue
            value, unit = decode(raw, meta)
            print("%-34s %s  %-14s %-8s raw=%s   %s"
                  % (name, meta["ID"], value, unit, raw.hex(), meta["INFO"][:40]))


def op_log(args):
    cat = load_catalog(args.csv)
    chans = []
    for name in args.names:
        meta = cat.get(name)
        if not meta:
            sys.exit("unknown channel: %s" % name)
        chans.append((name, int(meta["ID"], 16), meta))

    out = open(args.out, "w", newline="", encoding="utf-8")
    w = csv.writer(out)
    w.writerow(["Time"] + [n for n, _, _ in chans])
    print("Logging %d channel(s) to %s - Ctrl-C to stop" % (len(chans), args.out))

    t0 = time.time()
    n = 0
    try:
        with Hsfz(args.ip, source=args.source, timeout=args.timeout) as h:
            while True:
                row = [round(time.time() - t0, 3)]
                for _, did, meta in chans:
                    try:
                        value, _ = decode(h.read_did(args.target, did), meta)
                    except Exception:                   # noqa: BLE001
                        value = ""
                    row.append(value)
                w.writerow(row)
                n += 1
                if n % 25 == 0:
                    out.flush()
                    print("\r%d samples, %.1f s" % (n, time.time() - t0), end="")
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        out.close()
        print("wrote %d samples to %s" % (n, args.out))


# --------------------------------------------------------------------- CLI

def hexint(s):
    return int(s, 0)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, need_ip=True):
        if need_ip:
            sp.add_argument("--ip", required=True, help="ECU/gateway IP (see `discover`)")
        sp.add_argument("--source", type=hexint, default=TESTER_ADDR,
                        help="tester address (default 0xF4)")
        sp.add_argument("--timeout", type=float, default=2.0)
        sp.add_argument("--csv", default=DEFAULT_CSV,
                        help="measurement catalog CSV")

    sp = sub.add_parser("discover", help="find the car via UDP broadcast")
    sp.add_argument("--timeout", type=float, default=3.0)
    sp.add_argument("--bind", help="local IP of the ENET adapter (169.254.x.x)")
    sp.add_argument("--list-interfaces", action="store_true",
                    help="show local IPv4 addresses and exit")
    sp.set_defaults(func=op_discover)

    sp = sub.add_parser("scan", help="sweep target addresses to find modules")
    common(sp)
    sp.add_argument("--range", type=hexint, nargs=2, default=[0x00, 0x60],
                    metavar=("LO", "HI"))
    sp.set_defaults(func=op_scan)

    sp = sub.add_parser("read", help="read channels once")
    common(sp)
    sp.add_argument("--target", type=hexint, default=DME_MASTER)
    sp.add_argument("names", nargs="+", help="ARG names, e.g. ILAG1 ILAG2")
    sp.set_defaults(func=op_read)

    sp = sub.add_parser("faults", help="read and decode fault memory (0x19)")
    common(sp)
    sp.add_argument("--target", type=hexint, default=DME_MASTER)
    sp.add_argument("--mask", type=hexint, default=0xFF,
                    help="statusOfDTC mask (default 0xFF = all stored DTCs)")
    sp.add_argument("--german", action="store_true",
                    help="show BMW's German text instead of the translation")
    sp.set_defaults(func=op_faults)

    sp = sub.add_parser("log", help="poll channels into a CSV")
    common(sp)
    sp.add_argument("--target", type=hexint, default=DME_MASTER)
    sp.add_argument("--interval", type=float, default=0.2)
    sp.add_argument("--out", default="n63log.csv")
    sp.add_argument("names", nargs="+")
    sp.set_defaults(func=op_log)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
