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
  NBTEVO_head_unit_measurements.csv      head unit (0x63) channels, GPS etc.
  NBTEVO_enum_tables.csv                 head unit value -> text enumerations
"""

import csv
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
HU_CSV = "NBTEVO_head_unit_measurements.csv"
HU_ENUM_CSV = "NBTEVO_enum_tables.csv"

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
    0x10: "Central gateway (ZGW)",
    0x12: "Engine, master DME",
    0x40: "Body domain controller (BDC)",
    0x60: "Instrument cluster (KOMBI)",
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
    # The head unit's tables are prefixed NBTEVO., so they never collide.
    for name in (ENUM_CSV, HU_ENUM_CSV):
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
}


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
    scaled = value * mul / div + add

    # The enum table for a scalar channel is in NAME. RES_TABELLE names a
    # multi-field result record, which is a different thing entirely.
    text = enum_text(scaled, meta, enums, lang)
    return (text if text is not None else scaled), unit


# ----------------------------------------------------------- head unit catalog

# Channels on another module (the head unit) are refreshed on these periods
# rather than every cycle: it answers slowly, most of what it reports
# changes slowly, and the DME channels must keep their rate.
FAST_PERIOD = 1.0              # GPS, speeds, direction
SLOW_PERIOD = 10.0             # everything else
FAST_DIDS = frozenset({0xD030, 0xD031, 0xD032, 0xD034, 0xD038, 0xD039})
SLOW_READS_PER_CYCLE = 4       # head unit reads allowed per DME cycle
NO_ANSWER_BACKOFF = 30.0       # seconds to leave a silent module alone

# Values a log should not carry by default: credentials, SIM identity,
# where the car has been, and paired devices. Still in the catalog, so
# they can be added one by one; "add all" skips them.
SENSITIVE = re.compile(
    r"NETWORK_KEY|LABEL_PIN|IMEI|ICC_ID|COOKIES|LAST_DESTINATIONS|"
    r"BT_ADR_DEV|BT_GERAETEADRESSE|DEVICELIST|WLAN_ADDRESS|SERVICE_PARTNER")


def is_sensitive(name):
    return bool(SENSITIVE.search(name))


def load_head_unit_catalog():
    """ARG -> row for the head unit channels, or {} if the CSV is missing."""
    path = resource_path(HU_CSV)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8-sig") as f:
        return {row["ARG"].strip(): row for row in csv.DictReader(f)}


def record_layouts(catalog):
    """(module, DID) -> that identifier's fields in reply order.

    Only channels with a MODULE (the head unit) are records; DME channels
    are single values and are left out.
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
    return None


def decode_record(raw, fields):
    """Split one identifier's reply into its fields, in order.

    Returns {ARG: (value, unit, note)}. Values are numbers or text, never
    enum text, so they log and plot; enum_text() gives the words. A field
    whose size is unknown (a bitfield) ends what can be decoded.
    """
    out, pos = {}, 0
    for i, meta in enumerate(fields):
        dtype = (meta.get("DATENTYP") or "").strip().lower()
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
    """Reads a watch list from the DME and the head unit, one request per DID.

    DME channels (no MODULE) are read from `target` every cycle, as before.
    Head unit channels are records: one read fills every field of that
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
        return FAST_PERIOD if key[1] in FAST_DIDS else SLOW_PERIOD

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
            raw = h.read_did(module, did)
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
