"""
n63read.core - read BMW N63TU2 DME measurements over ENET using BMW's HSFZ protocol.

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
  <MODULE>_measurements.csv              other modules' channels: NBTEVO (head
                                         unit, GPS), GSZFB1 (transmission),
                                         ACSM5 (airbag), DSC_G30N, BDC_G11
  <MODULE>_enum_tables.csv               their value -> text enumerations
"""

import csv
import json
import os
import re
import socket
import struct
import sys
import time

# ---------------------------------------------------------------- constants

HSFZ_DIAG_PORT = 6801          # TCP, diagnostic channel
HSFZ_CONTROL_PORT = 6811       # UDP, discovery broadcast
TESTER_ADDR = 0xF4             # standard tester source address
DME_MASTER = 0x12              # master DME, confirmed from CarData fault memory

PROBE_TIMEOUT = 0.5            # seconds to wait at an empty address in a sweep
# Module addresses to sweep. The G30 map has modules up to 0x65 (ELV), so
# this runs past the 0x60 that scan started with.
SWEEP_RANGE = (0x00, 0x7F)

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
ALLOWED_SERVICES = frozenset({0x19, 0x22, 0x3E})

DEFAULT_CSV = "N63TU2_DME_readable_measurements.csv"
FAULT_CSV = "BMW_fault_code_texts.csv"
STATUS_CSV = "BMW_dtc_status_byte.csv"
ENUM_CSV = "BMW_enum_tables.csv"
FREEZE_CSV = "BMW_freeze_frame_fields.csv"
MODULE_MAP_CSV = "G30_module_sgbd_map.csv"
# Per-module catalogs from tools/module_catalog.py: <NAME>_measurements.csv
# and <NAME>_enum_tables.csv for each.
MODULE_CATALOGS = ("NBTEVO", "GSZFB1", "ACSM5", "DSC_G30N", "BDC_G11",
                   "EPS_G11", "VDP_G11", "LMV_G11", "IHKA_G11", "EKP301",
                   "PMA_20")

DATA_FILES = (DEFAULT_CSV, FAULT_CSV, STATUS_CSV, ENUM_CSV, FREEZE_CSV,
              MODULE_MAP_CSV)


def resource_path(name):
    """Locate a bundled data file.

    Search order: PyInstaller's unpack dir, the package's data/ directory,
    the directory holding this module, the executable's own folder, then the
    working directory. The last two let a user drop an updated CSV next to
    the exe without rebuilding.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(getattr(sys, "_MEIPASS", here), name),
        os.path.join(here, "data", name),
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


class HsfzTimeout(HsfzError):
    """Nothing answered in time: an empty address, or a module asleep."""


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
            try:
                chunk = self.sock.recv(n - len(buf))
            except socket.timeout:
                # Surface as HsfzError so callers (scan especially) can treat
                # a silent address as "nothing there" rather than a crash.
                raise HsfzTimeout("timed out waiting for response") from None
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
            # A late reply from an earlier request (e.g. the previous address
            # in a scan) must not be credited to this target.
            if src != target or dst != self.source:
                continue

            if data[0] == 0x7F and len(data) >= 3:
                if data[1] != service:
                    continue
                code = data[2]
                if code == 0x78:          # responsePending - ECU is working
                    continue
                raise NegativeResponse(data[1], code)

            if data[0] == service + 0x40:
                return data
            # Not ours (another module chattering) - keep waiting.

        raise HsfzTimeout("timed out waiting for response")

    def read_did(self, target, did, timeout=None):
        """UDS 0x22. Returns the raw payload bytes after the echoed identifier.

        `timeout` overrides the connection's for this read: an absent or
        sleeping module then costs that long, not the full timeout.
        """
        if timeout is not None:
            saved = self.timeout
            self.timeout = timeout
            self.sock.settimeout(timeout)
            try:
                return self.read_did(target, did)
            finally:
                self.timeout = saved
                self.sock.settimeout(saved)
        resp = self.request(target, bytes([0x22, (did >> 8) & 0xFF, did & 0xFF]))
        if len(resp) < 3:
            raise HsfzError("short response")
        echoed = (resp[1] << 8) | resp[2]
        if echoed != did:
            raise HsfzError("identifier mismatch: asked 0x%04X got 0x%04X" % (did, echoed))
        return resp[3:]

    def tester_present(self, target):
        return self.request(target, bytes([0x3E, 0x00]))

    def probe(self, target, timeout=PROBE_TIMEOUT):
        """True if anything answers TesterPresent at `target`, even negatively.

        Uses a short timeout: a present module answers in milliseconds, and
        a sweep spends most of its time waiting on empty addresses.
        """
        saved = self.timeout
        self.timeout = timeout
        self.sock.settimeout(timeout)
        try:
            self.request(target, bytes([0x3E, 0x00]), retries=0)
            return True
        except NegativeResponse:
            return True
        except HsfzError:
            return False
        finally:
            self.timeout = saved
            self.sock.settimeout(saved)

    def read_dtcs(self, target, status_mask=0xFF):
        """UDS 0x19 sub-function 0x02 (reportDTCByStatusMask).

        Returns [(code, status)] where code is the 6-hex-digit BMW fault code
        (3 bytes, matching BMW_fault_code_texts.csv) and status is the raw
        statusOfDTC byte (decode with dtc_status_flags()).

        A mask of 0xFF asks for every DTC the module holds regardless of
        state. Narrow it if you only want currently-present faults.
        """
        resp = self.request(target, bytes([0x19, 0x02, status_mask & 0xFF]))
        return parse_dtc_records(resp)


def parse_dtc_records(resp):
    """Split a 59 02 response into [(code, status)].

    Layout: 59 02 <availabilityMask> then 4-byte records: DTC[3] + status[1].
    """
    body = resp[3:]
    out = []
    for i in range(0, len(body) - 3, 4):
        code = "%02X%02X%02X" % (body[i], body[i + 1], body[i + 2])
        out.append((code, body[i + 3]))
    return out


# ISO 14229-1 statusOfDTC bits, LSB first. BMW's own texts are combinations of
# these: "currently present" is testFailed, "stored" is pending or confirmed.
DTC_STATUS_BITS = (
    "testFailed",
    "testFailedThisOperationCycle",
    "pendingDTC",
    "confirmedDTC",
    "testNotCompletedSinceLastClear",
    "testFailedSinceLastClear",
    "testNotCompletedThisOperationCycle",
    "warningIndicatorRequested",
)


def dtc_status_flags(status):
    """statusOfDTC byte -> list of the ISO bit names that are set."""
    return [name for bit, name in enumerate(DTC_STATUS_BITS) if status & (1 << bit)]


def dtc_status_summary(status):
    """One-line reading of a statusOfDTC byte: is the fault active, is it stored."""
    present = bool(status & 0x01)
    stored = bool(status & 0x0C)
    if present and stored:
        text = "present, stored"
    elif present:
        text = "present, not yet stored"
    elif stored:
        text = "not present, stored"
    else:
        text = "not present"
    if status & 0x80:
        text += ", warning lamp requested"
    return text


# ------------------------------------------------------------- fault sweep

# Addresses confirmed for this car (PROJECT.md). Others are named from
# G30_module_sgbd_map.csv where it has exactly one module at that address.
KNOWN_MODULES = {
    0x01: "Airbag / crash safety (ACSM)",
    0x10: "Central gateway (ZGW)",
    0x12: "Engine, master DME",
    0x17: "Fuel pump control (EKP)",
    0x18: "Transmission, ZF 8HP (EGS)",
    0x19: "Transfer case, xDrive (LMV)",
    0x2C: "Park assist (PMA / PDC)",
    0x30: "Electric power steering (EPS)",
    0x40: "Body domain controller (BDC)",
    0x60: "Instrument cluster (KOMBI)",
    0x63: "Head unit (NBT Evo)",
    0x76: "Suspension, vertical dynamics (VDP)",
    0x78: "Climate control (IHKA)",
}

FAULT_CSV_HEADER = ("read_at", "module", "module_name", "code", "status",
                    "state", "text_en", "text_de", "catalog_source")


def load_module_names():
    """Module address -> name, from KNOWN_MODULES and the G30 module map."""
    names = {}
    path = resource_path(MODULE_MAP_CSV)
    if os.path.exists(path):
        seen = {}
        with open(path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                try:
                    addr = int((row.get("ecu_addr_hex") or "").strip(), 16)
                except ValueError:
                    continue
                seen.setdefault(addr, []).append((row.get("description") or "").strip())
        # Several rows at one address are variants for other engines; don't guess.
        names = {a: d[0] for a, d in seen.items() if len(d) == 1 and d[0]}
    names.update(KNOWN_MODULES)
    return names


def fault_steps(h, modules=None, texts=None, names=None):
    """Sweep for modules (unless given) and read their faults, one step at a time.

    A generator that does one network round trip per step and yields
    ("finding", addr), ("found", addr) or ("reading", addr) after each, then
    ("done", rows). The GUI advances it between samples so a running log
    keeps recording during the read.
    """
    if modules is None:
        lo, hi = SWEEP_RANGE
        modules = []
        for addr in range(lo, hi + 1):
            present = h.probe(addr)
            if present:
                modules.append(addr)
                yield ("found", addr)
            else:
                yield ("finding", addr)

    texts = load_fault_texts() if texts is None else texts
    names = load_module_names() if names is None else names
    read_at = time.strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for addr in modules:
        rows.extend(_module_fault_rows(h, addr, texts, names, read_at))
        yield ("reading", addr)
    yield ("done", rows)


def sweep_modules(h, lo=None, hi=None, progress=None):
    """Addresses in lo..hi (default SWEEP_RANGE) that answer TesterPresent."""
    lo = SWEEP_RANGE[0] if lo is None else lo
    hi = SWEEP_RANGE[1] if hi is None else hi
    found = []
    for addr in range(lo, hi + 1):
        if progress:
            progress(addr)
        if h.probe(addr):
            found.append(addr)
    return found


def read_all_faults(h, modules, texts=None, names=None, progress=None):
    """Read fault memory (0x19) from each module. Returns rows for a fault CSV.

    Every module gets at least one row, so the file shows what was checked:
    one per fault, or a single row saying "no faults" or why it refused.
    """
    texts = load_fault_texts() if texts is None else texts
    names = load_module_names() if names is None else names
    read_at = time.strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for addr in modules:
        if progress:
            progress(addr)
        rows.extend(_module_fault_rows(h, addr, texts, names, read_at))
    return rows


def _module_fault_rows(h, addr, texts, names, read_at):
    """Fault CSV rows for one module: one per fault, or one saying why none."""
    base = {"read_at": read_at, "module": "0x%02X" % addr,
            "module_name": names.get(addr, ""), "code": "", "status": "",
            "state": "", "text_en": "", "text_de": "", "catalog_source": ""}
    try:
        dtcs = h.read_dtcs(addr)
    except NegativeResponse as e:
        return [dict(base, state="refused (%s)" % NRC.get(e.code, "nrc 0x%02X" % e.code))]
    except HsfzError as e:
        return [dict(base, state="no reply (%s)" % e)]
    if not dtcs:
        return [dict(base, state="no faults")]
    rows = []
    for code, status in sorted(dtcs):
        entry = texts.get(code.upper(), {})
        rows.append(dict(base, code=code, status="0x%02X" % status,
                         state=dtc_status_summary(status),
                         text_en=entry.get("text_en", "") or "(not in catalog)",
                         text_de=entry.get("text_de", ""),
                         catalog_source=entry.get("source_module", "")))
    return rows


def write_fault_csv(path, rows):
    """Write read_all_faults() rows. utf-8-sig so Excel shows umlauts."""
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FAULT_CSV_HEADER)
        w.writeheader()
        w.writerows(rows)


# --------------------------------------------------------- head unit GPS
#
# From the NBT Evo SGBD (nbtevo.prg). Its STATUS_GPS_POSITION job is
# hand-coded rather than table-driven, so the layout below comes from
# disassembling that job: it sends 22 D0 39 and reads, after the echoed DID,
#   [0]      validity, 0x00 = valid, 0x01 = not valid
#   [1..5]   latitude:  0x00 N / 0x01 S, degrees, minutes, seconds, 1/100 s
#   [6..10]  longitude: 0x00 E / 0x01 W, degrees, minutes, seconds, 1/100 s
#   [11..12] height, unsigned big-endian, unit not stated (presumably m)

HEAD_UNIT = 0x63
GPS_POSITION_DID = 0xD039
GPS_DMS = "gps_dms"            # catalog data type for the 5-byte coordinates
GPS_STATUS_DID = 0xD038        # one byte, see GPS_FIX below

GPS_FIX = {
    0x00: "not supported: no GPS", 0x01: "not supported: communication error",
    0x02: "receiver error", 0x03: "no almanac", 0x04: "searching for satellites",
    0x05: "tracking 1 satellite", 0x06: "tracking 2 satellites",
    0x07: "tracking 3 satellites", 0x08: "tracking 4 satellites",
    0x09: "tracking 5 satellites", 0x0A: "tracking 6 satellites",
    0x0B: "2D position", 0x0C: "3D position",
}


def _dms(b):
    """5 bytes (hemisphere, deg, min, sec, 1/100 s) to signed decimal degrees."""
    deg = b[1] + b[2] / 60 + (b[3] + b[4] / 100) / 3600
    return -deg if b[0] == 0x01 else deg


def parse_gps_position(payload):
    """Decode a 0xD039 payload. Returns a dict, or raises ValueError if short."""
    if len(payload) < 13:
        raise ValueError("GPS position needs 13 bytes, got %d" % len(payload))
    return {
        "valid": payload[0] == 0x00,
        "lat": round(_dms(payload[1:6]), 7),
        "lon": round(_dms(payload[6:11]), 7),
        "height": (payload[11] << 8) | payload[12],
    }


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

def load_catalog(path=DEFAULT_CSV):
    """ARG name -> metadata dict, from the SGBD-derived CSV."""
    if not os.path.exists(path):
        path = resource_path(os.path.basename(path))
    if not os.path.exists(path):
        raise FileNotFoundError(
            "measurement CSV not found: %s\n"
            "Pass --csv with the path to %s" % (path, DEFAULT_CSV))
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


def _enum_key(value):
    """Normalise an enum value so '0x01', '0x0001' and '1' all match."""
    try:
        return int(str(value).strip(), 0)
    except ValueError:
        return str(value).strip().lower()


def load_enum_tables():
    """Table name (upper-cased) -> {value: {text_de, text_en}}.

    The SGBD directory stores table names upper-cased while SG_Funktionen
    references them in mixed case, so every lookup here is case-insensitive.
    Values are stored both as decimal and hex in the source, so they are
    keyed by integer.
    """
    tables = {}
    # Module tables are prefixed with the module (NBTEVO.), so never collide.
    for name in (ENUM_CSV,) + tuple(m + "_enum_tables.csv" for m in MODULE_CATALOGS):
        path = resource_path(name)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                table = (row.get("table_name") or "").strip().upper()
                val = (row.get("value") or "").strip()
                if not table or not val:
                    continue
                tables.setdefault(table, {})[_enum_key(val)] = {
                    "text_de": (row.get("text_de") or "").strip(),
                    "text_en": (row.get("text_en") or "").strip(),
                }
    return tables


def enum_text(value, meta, enums, lang="text_en"):
    """Text for a scaled value from the table in the channel's NAME, or None."""
    table_name = (meta.get("NAME") or "").strip().upper()
    if not enums or table_name in ("", "-") or not isinstance(value, (int, float)) \
            or value != int(value):
        return None
    entry = enums.get(table_name, {}).get(int(value))
    if not entry:
        return None
    return entry.get(lang) or entry.get("text_de") or None


_WIDTH = {
    "unsigned char": (1, False), "signed char": (1, True),
    "unsigned int": (2, False), "signed int": (2, True),
    "unsigned long": (4, False), "signed long": (4, True),
    "int": (2, True),               # EDIABAS shorthand for signed 16-bit
}
def _scale(meta, key, default):
    """MUL / DIV / ADD as a number; "-" or blank means the default."""
    try:
        return float((meta.get(key) or "").strip())
    except ValueError:
        return default


# IEEE 754 single precision; "motorola" = most significant byte first.
_FLOAT = {"motorola float": ">f", "intel float": "<f"}


def decode(raw, meta, enums=None, lang="text_en"):
    """Apply data type and MUL/DIV/ADD scaling. Returns (value, unit).

    If `enums` (from load_enum_tables()) is given and the channel names a
    value->text table in its NAME column, the scaled value is looked up and
    the text returned instead; unmatched values fall back to the number.
    """
    dtype = (meta.get("DATENTYP") or "").strip().lower()
    unit = (meta.get("EINHEIT") or "").strip()
    if unit == "-":
        unit = ""

    if dtype == "string" or dtype.startswith("string["):
        return raw.split(b"\x00")[0].decode("latin-1").strip(), unit
    if dtype == GPS_DMS:
        return (round(_dms(raw), 7), unit) if len(raw) >= 5 else (raw.hex(), unit)
    if dtype in _FLOAT:
        if len(raw) < 4:
            return raw.hex(), unit
        value = struct.unpack(_FLOAT[dtype], raw[:4])[0]
        mul, div, add = (_scale(meta, k, d) for k, d in (("MUL", 1.0), ("DIV", 1.0), ("ADD", 0.0)))
        return value * mul / (div or 1.0) + add, unit

    if dtype not in _WIDTH:
        # data[n], strings, bitfields - hand back hex, don't guess.
        return raw.hex(), unit

    width, signed = _WIDTH[dtype]
    if len(raw) < width:
        return raw.hex(), unit

    # 'High' means most significant byte first, which is the common case.
    order = "little" if (meta.get("L/H") or "").strip().lower() == "low" else "big"
    value = int.from_bytes(raw[:width], order, signed=signed)

    mul, div, add = (_scale(meta, "MUL", 1.0), _scale(meta, "DIV", 1.0),
                     _scale(meta, "ADD", 0.0))
    if div == 0:
        div = 1.0
    scaled = value * mul / div + add

    # The enum table for a scalar channel is in NAME. RES_TABELLE names a
    # multi-field result record, which is a different thing entirely.
    text = enum_text(scaled, meta, enums, lang)
    return (text if text is not None else scaled), unit


# ------------------------------------------------------------ module catalogs

# Short names for the picker, by module address.
MODULE_LABELS = {
    0x01: "Airbag", 0x17: "Fuel pump", 0x18: "Transmission", 0x19: "Transfer case", 0x29: "DSC",
    0x2C: "Park assist", 0x30: "Steering", 0x40: "Body", 0x63: "Head unit", 0x76: "Suspension",
    0x78: "Climate",
}

# Channels on modules other than the DME are refreshed on these periods
# rather than every cycle: most of what they report changes slowly, and the
# DME channels must keep their rate. FAST is for what moves while driving.
FAST_PERIOD = 1.0
SLOW_PERIOD = 10.0
FAST_DIDS = frozenset(
    [(0x63, d) for d in (0xD030, 0xD031, 0xD032, 0xD034, 0xD038, 0xD039)]  # GPS, speeds
    + [(0x18, d) for d in (0xDA12, 0xDA1C, 0xDA1F, 0xDA22, 0xDA24,
                           0xDA27, 0xDA2A, 0xDA2C, 0xDA2E, 0xDA34)]        # gear, temps, speeds
    + [(0x29, d) for d in (0xDBE4, 0xDBE5, 0xDBF4, 0xDBF5)]                # wheels, brakes
    + [(0x01, d) for d in (0x4101, 0x4102)]                                # acceleration, yaw
    + [(0x40, d) for d in (0xD601, 0xD611)]                                # ride height, drive mode
    + [(0x30, d) for d in (0xDB57, 0xDB99, 0xDB9A)]                        # steering angle, torque
    + [(0x76, d) for d in (0xD665, 0xDC05, 0xDC0A)]                        # wheel accel, height, dampers
    + [(0x19, d) for d in (0xDB32, 0xDFEA)]                                # xDrive clutch torque
    + [(0x17, 0x4010)]                                                     # fuel pump current
    + [(0x2C, d) for d in (0xD645, 0xD646, 0xD647, 0xD648, 0xD64A, 0xD64B,
                           0xD64C, 0xD64D, 0xD64E, 0xD64F, 0xD65C, 0xD65E)])  # parking distances
SLOW_READS_PER_CYCLE = 6       # module reads allowed per DME cycle
NO_ANSWER_BACKOFF = 30.0       # seconds to leave a silent module alone
MODULE_TIMEOUT = 0.5           # a present module answers in milliseconds; a
                               # missing or sleeping one must not stall the DME

# Values a log should not carry by default: credentials, SIM identity,
# where the car has been, and paired devices. Still in the catalog, so
# they can be added one by one; "add all" skips them.
SENSITIVE = re.compile(
    r"NETWORK_KEY|LABEL_PIN|IMEI|ICC_ID|COOKIES|LAST_DESTINATIONS|"
    r"BT_ADR_DEV|BT_GERAETEADRESSE|DEVICELIST|WLAN_ADDRESS|SERVICE_PARTNER")


def is_sensitive(name):
    return bool(SENSITIVE.search(name))


def load_module_catalogs():
    """ARG -> row for every module catalog present (head unit, transmission...).

    A missing file just leaves that module's channels out.
    """
    cat = {}
    for name in MODULE_CATALOGS:
        path = resource_path(name + "_measurements.csv")
        if os.path.exists(path):
            with open(path, encoding="utf-8-sig") as f:
                cat.update((row["ARG"].strip(), row) for row in csv.DictReader(f))
    return cat


def record_layouts(catalog):
    """(module, DID) -> that identifier's fields in reply order.

    Only channels with a MODULE (the module catalogs) are records; DME
    channels are single values and are left out.
    """
    layouts = {}
    for row in catalog.values():
        if (row.get("MODULE") or "").strip():
            key = (int(row["MODULE"], 16), int(row["ID"], 16))
            layouts.setdefault(key, []).append(row)
    for fields in layouts.values():
        fields.sort(key=lambda r: int(r["FIELD"]))
    return layouts


def field_size(dtype, raw, pos):
    """Bytes a record field takes at `pos`, or None if it cannot be known."""
    if dtype in _WIDTH:
        return _WIDTH[dtype][0]
    m = re.fullmatch(r"(?:data|string)\[(\d+)\]", dtype)
    if m:
        return int(m.group(1))
    if dtype == "string":
        # Unsized: up to and including a NUL, or the rest of the reply.
        end = raw.find(b"\x00", pos)
        return len(raw) - pos if end < 0 else end - pos + 1
    if dtype == GPS_DMS:
        return 5
    if dtype in _FLOAT:
        return 4
    return None


def _field_pos(meta, pos):
    """A field's offset: its POS column if set (fields taken from a hand-coded
    job, which skips bytes), otherwise right after the previous field."""
    given = (meta.get("POS") or "").strip()
    return int(given) if given else pos


def decode_record(raw, fields):
    """Split one identifier's reply into its fields, in order.

    Returns {ARG: (value, unit, note)}. Values are numbers or text, never
    enum text, so they log and plot; enum_text() gives the words. A field
    whose size is unknown (a bitfield) ends what can be decoded.
    """
    out, pos = {}, 0
    for i, meta in enumerate(fields):
        dtype = (meta.get("DATENTYP") or "").strip().lower()
        pos = _field_pos(meta, pos)
        size = field_size(dtype, raw, pos)
        if size is None:
            for rest in fields[i:]:
                out[rest["ARG"]] = (None, "", "layout unknown")
            break
        chunk = raw[pos:pos + size]
        if len(chunk) < size and not dtype.startswith("string"):
            out[meta["ARG"]] = (None, "", "short reply")
        else:
            value, unit = decode(chunk, meta)
            out[meta["ARG"]] = (value, unit, "")
        pos += size
    return out


class ChannelReader:
    """Reads a watch list from the DME and other modules, one request per DID.

    DME channels (no MODULE) are read from `target` every cycle, as before.
    Other modules' channels are records: one read fills every field of that
    identifier. They are refreshed on FAST_PERIOD / SLOW_PERIOD, at most
    SLOW_READS_PER_CYCLE per cycle, stalest first, and the last values are
    reused in between, so every sample and log row is complete.
    """

    def __init__(self, layouts, target):
        self.layouts, self.target = layouts, target
        self.cache = {}         # (module, did) -> {ARG: (value, unit, note)}
        self.last = {}          # (module, did) -> time of last read
        self.refused = {}       # (module, did) -> reason; not asked again
        self.quiet_until = {}   # module -> time; it did not answer

    @staticmethod
    def period(key):
        return FAST_PERIOD if key in FAST_DIDS else SLOW_PERIOD

    def read(self, h, chans, now=None):
        now = time.time() if now is None else now
        sample, records = {}, {}
        for name, did, meta in chans:
            module = (meta.get("MODULE") or "").strip()
            if module:
                records.setdefault((int(module, 16), did), []).append(name)
            else:
                sample[name] = self._read_value(h, did, meta)

        due = [k for k in records
               if k not in self.refused and k in self.layouts
               and now >= self.quiet_until.get(k[0], 0)
               and now - self.last.get(k, -1e9) >= self.period(k)]
        due.sort(key=lambda k: self.last.get(k, -1e9))
        for key in due[:SLOW_READS_PER_CYCLE]:
            self._read_record(h, key, now)

        for key, names in records.items():
            got = self.cache.get(key, {})
            if key not in self.layouts:
                note = "not in catalog"
            elif key in self.refused:
                note = self.refused[key]
            elif now < self.quiet_until.get(key[0], 0):
                note = "module not answering"
            else:
                note = "waiting"
            for name in names:
                sample[name] = got.get(name) or (None, "", note)
        return sample

    def _read_value(self, h, did, meta):
        try:
            value, unit = decode(h.read_did(self.target, did), meta)
            return (value, unit, "")
        except NegativeResponse as e:
            return (None, "", NRC.get(e.code, "0x%02X" % e.code))
        except Exception as e:                                      # noqa: BLE001
            return (None, "", str(e)[:28])

    def _read_record(self, h, key, now):
        module, did = key
        self.last[key] = now
        try:
            raw = h.read_did(module, did, timeout=MODULE_TIMEOUT)
        except NegativeResponse as e:
            reason = NRC.get(e.code, "0x%02X" % e.code)
            if e.code in (0x11, 0x12, 0x31):
                self.refused[key] = reason          # will never work: stop asking
            self.cache[key] = {f["ARG"]: (None, "", reason) for f in self.layouts[key]}
            return
        except HsfzTimeout:
            self.quiet_until[module] = now + NO_ANSWER_BACKOFF
            self.cache.pop(key, None)
            return
        self.cache[key] = decode_record(raw, self.layouts[key])


# -------------------------------------------------------------- catalog check

def expected_size(fields):
    """(bytes, exact) a record's reply should have. `exact` is False when an
    unsized string, a field of unknown size, or fields at given positions
    (the rest of the reply undescribed) make `bytes` only a minimum."""
    pos = end = 0
    exact = True
    for f in fields:
        dtype = (f.get("DATENTYP") or "").strip().lower()
        if (f.get("POS") or "").strip():
            exact = False
        pos = _field_pos(f, pos)
        if dtype == "string":
            return max(end, pos), False
        size = field_size(dtype, b"", 0)
        if size is None:
            return max(end, pos), False
        pos += size
        end = max(end, pos)
    return end, exact


def check_layouts(catalog, target=DME_MASTER):
    """(module, DID) -> fields for every identifier in the catalog.

    DME channels (no MODULE) become one-field records at `target`; several
    DME channels can share an identifier, so the widest one is kept.
    """
    out = record_layouts(catalog)
    for row in catalog.values():
        if not (row.get("MODULE") or "").strip():
            key = (target, int(row["ID"], 16))
            if key not in out or expected_size([row])[0] > expected_size(out[key])[0]:
                out[key] = [row]
    return out


def check_identifier(h, key, fields):
    """Read one identifier and judge whether the catalog's layout fits it.

    Returns (status, detail, raw): status is "ok", "size", "refused" or
    "no answer". Only service 0x22, read-only like everything else here.
    """
    module, did = key
    try:
        raw = h.read_did(module, did, timeout=MODULE_TIMEOUT)
    except NegativeResponse as e:
        return "refused", NRC.get(e.code, "0x%02X" % e.code), b""
    except HsfzTimeout:
        return "no answer", "", b""
    size, exact = expected_size(fields)
    if (exact and len(raw) == size) or (not exact and len(raw) >= size):
        return "ok", "%d bytes" % len(raw), raw
    return "size", "got %d bytes, expected %s%d" % (len(raw), "" if exact else ">=", size), raw


def check_verdict(counts):
    """Summary for one module from {status: n}."""
    answered = counts.get("ok", 0) + counts.get("size", 0)
    if not answered:
        return "no identifiers answered - module absent, asleep, or the wrong file"
    share = counts.get("ok", 0) / answered
    if share >= 0.9:
        return "fits (%d%% of answers are the expected size)" % round(share * 100)
    if share < 0.5:
        return "does NOT fit (only %d%% of answers are the expected size)" % round(share * 100)
    return "unclear (%d%% of answers are the expected size)" % round(share * 100)


# ------------------------------------------------------------ function groups

# Keyword rules for grouping channels by what they measure, across modules.
# First match wins, so specific groups come before broad ones (gearbox oil
# temperature is Gearbox, not Oil or Temperatures). Matched against the code
# name, the German and the English description, lower-cased.
FUNCTION_GROUPS = [
    ("Versions, coding & identification",
     r"svk|sgbm|version|seriennummer|sachnummer|serial|ident|^(hu|egs|acsm|dsc|bdc|eps|vdp"
     r"|lmv|ihka|ekp)_(prog|flash|codier|hw_|sw_)|programming|(?<![a-z])(codier|coding)|logisti"),
    ("GPS & navigation", r"gps|navi|satell|latitude|longitude|breitengrad|laengengrad"),
    ("Gearbox & driveline",
     r"getriebe|gearbox|transmission|istgang|gang|wandler|converter|kupplung|clutch|"
     r"^egs_|^lmv_|waehlhebel|schaltpaddle|kick.?down|fahrstufe|verteiler"),
    ("Brakes & tires",
     r"brems|brake|reifen|tire|tyre|^dsc_rdc|rdc_|epb|abs_|druck_kreis|bremsbelag|vakuum|"
     r"radelektronik|druckcodier"),
    ("Steering & suspension",
     r"lenk|steer|^eps_|hoehenst|ride height|daempf|damper|^vdp_|ars_|ehc|wank|"
     r"radbeschleunigung|stabilis"),
    ("Speeds & wheels", r"geschwindigkeit|speed|raddreh|wheel|rad_v|rad_h"),
    ("Lambda & exhaust",
     r"lambda|lsu|sonde|probe|abgas|exhaust|katalys|^kat|nox|partikel|opf"),
    ("Ignition & misfire",
     r"zuend|ignition|aussetz|misfire|laufunruhe|iluz|ilaz|klopf|knock"),
    ("Fuel", r"kraftstoff|fuel|tank|rail|einspritz|inject|^ekp_"),
    ("Air, boost & throttle",
     r"ladedruck|boost|turbo|lader|drossel|throttle|luftmasse|air mass|ansaug|intake|"
     r"saugrohr|umgebungsdruck|ambient pressure"),
    ("Oil", r"oel|öl|\boil"),
    ("Cooling", r"kuehl|kühl|coolant|wasserpumpe|water pump|luefter|lüfter|\bfan\b"),
    ("Climate", r"klima|^ihka_|kaelte|kälte|kompressor|compressor|gebläse|geblaese|heiz"),
    ("Safety & airbag",
     r"airbag|crash|^acsm_|gurt|belt|imu|beschleunigung|acceleration|sitzbeleg|occupan"),
    ("Body, lights & comfort",
     r"licht|light|lamp|leucht|spiegel|mirror|fenster|^bdc_fh|window|sitz|seat|tuer|tür|"
     r"door|wisch|wiper|wasch|rollo|hupe|horn|schluessel|key|regen|rain"),
    ("Electrical & battery",
     r"spannung|voltage|strom\b|current|batter|klemme|terminal|generator|ibs|\bubat"),
    ("Engine",
     r"drehzahl|\brpm\b|motor|engine|nockenwell|camshaft|vanos|valvetronic|moment|torque|"
     r"zylinder|cylinder"),
    ("Temperatures", r"temp"),
    ("Counters, adaptations & statistics",
     r"zaehler|zähler|counter|statisti|histor|histogram|adapt|lern|learn|kalibr|calib"),
]
OTHER_GROUP = "Other"
_FUNCTION_RES = [(name, re.compile(rx)) for name, rx in FUNCTION_GROUPS]


def function_group(*texts):
    """Function group for a channel, from its name and descriptions."""
    text = " ".join(t or "" for t in texts).lower()
    for name, rx in _FUNCTION_RES:
        if rx.search(text):
            return name
    return OTHER_GROUP


# ---------------------------------------------------- what the car answers

# Which identifiers this car answers, learned from live reads, a check in
# the GUI, or `n63read check --out` imported. Kept between sessions so the
# picker can show only channels that work. "size" means it answers but the
# reply is not the size the catalog expects.
ANSWERS = ("ok", "size")
AVAILABILITY_FILE = "availability.json"


def availability_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "n63read", AVAILABILITY_FILE)


# ------------------------------------------------------------------ settings

SETTINGS_FILE = "settings.json"
LANGUAGES = {"en": "English", "de": "Deutsch"}


def settings_path():
    return os.path.join(os.path.dirname(availability_path()), SETTINGS_FILE)


def load_settings(path=None):
    """GUI preferences ({"language": "en"}...); {} if none saved yet."""
    try:
        with open(path or settings_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(settings, path=None):
    path = path or settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=1)
    os.replace(tmp, path)


def _avail_key(module, did):
    return "0x%02X:0x%04X" % (module, did)


def load_availability(path=None):
    """{(module, DID): status} from disk; {} if there is none yet."""
    path = path or availability_path()
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    out = {}
    for key, status in (raw.get("identifiers") or {}).items():
        try:
            module, did = (int(p, 16) for p in key.split(":"))
        except ValueError:
            continue
        out[(module, did)] = status
    return out


def save_availability(avail, path=None):
    path = path or availability_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {"saved": time.strftime("%Y-%m-%d %H:%M:%S"),
            "identifiers": {_avail_key(m, d): s for (m, d), s in sorted(avail.items())}}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=0)
    os.replace(tmp, path)


def import_check_csv(path):
    """{(module, DID): status} from a `n63read check --out` CSV."""
    out = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                key = (int(row["module"], 16), int(row["did"], 16))
            except (KeyError, ValueError):
                continue
            out[key] = row.get("status") or ""
    if not out:
        raise ValueError("no module/did/status rows - is this a `n63read check` CSV?")
    return out


def check_steps(h, layouts, keys=None):
    """Generator for a check run a step at a time (GUI): yields (key, status).

    Like `n63read check`, but a module that leaves three identifiers in a
    row unanswered is skipped, so an absent module costs about 1.5 s.
    """
    keys = sorted(layouts) if keys is None else keys
    silent, skip = {}, set()
    for key in keys:
        if key[0] in skip:
            continue
        status, _, _ = check_identifier(h, key, layouts[key])
        silent[key[0]] = silent.get(key[0], 0) + 1 if status == "no answer" else 0
        if silent[key[0]] >= 3:
            skip.add(key[0])
        yield key, status
