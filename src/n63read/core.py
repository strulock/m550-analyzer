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
  BMW_freeze_enum_tables.csv             value tables of the modules' own fields
  G30_module_sgbd_map.csv                module address -> SGBD variant
  <MODULE>_measurements.csv              other modules' channels: NBTEVO (head
                                         unit, GPS), GSZFB1 (transmission),
                                         ACSM5 (airbag), DSC_G30N, BDC_G11
  <MODULE>_enum_tables.csv               their value -> text enumerations
"""

import calendar
import contextlib
import csv
import json
import os
import re
import socket
import struct
import sys
import threading
import time

# ---------------------------------------------------------------- constants

HSFZ_DIAG_PORT = 6801          # TCP, diagnostic channel
HSFZ_CONTROL_PORT = 6811       # UDP, discovery broadcast
TESTER_ADDR = 0xF4             # standard tester source address
DME_MASTER = 0x12              # master DME, confirmed from CarData fault memory

PROBE_TIMEOUT = 0.5            # seconds to wait at an empty address in a sweep
PROBE_TIMEOUT_RANGE = (0.05, 10.0)   # accepted from the GUI and CLI
# Module addresses to sweep. The G30 map has modules up to 0x65 (ELV), so
# this runs past the 0x60 that scan started with.
SWEEP_RANGE = (0x00, 0x7F)

CTRL_DIAG = 0x0001             # diagnostic request / response
CTRL_ACK = 0x0002              # acknowledge transfer (ECU echoes the request)
CTRL_IDENT = 0x0011            # vehicle identification data
# The gateway's refusals: a frame with one of these control words instead of
# an answer. 0x40 came back to every request for minutes after the PC slept
# mid-log (in-car, 2026-10-19): the car still held the old session.
HSFZ_NACK = {
    0x0040: "incorrect tester address (the car may still hold an earlier "
            "connection, e.g. from before the PC slept; it lets go within minutes)",
    0x0041: "incorrect control word",
    0x0042: "incorrect format",
    0x0043: "incorrect destination address",
    0x0044: "message too large",
    0x0045: "diagnostic application not ready",
}

NRC = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat",
    0x14: "responseTooLong",
    0x21: "busyRepeatRequest",          # <- asked again, up to BUSY_RETRIES
    0x22: "conditionsNotCorrect",
    0x24: "requestSequenceError",
    0x31: "requestOutOfRange",          # <- identifier not implemented here
    0x33: "securityAccessDenied",
    0x78: "responsePending",
    0x7E: "subFunctionNotSupportedInActiveSession",
    0x7F: "serviceNotSupportedInActiveSession",
}

# A module that answers busyRepeatRequest (0x21) is asked again, after a
# pause, this many times. The head unit said busy to 37 identifiers in a
# row during a check at the car (2026-10-18).
BUSY_RETRIES = 3
BUSY_WAIT = 0.2

# Read-only services. 0x19 reports fault memory; it cannot clear it (that is
# 0x14) and cannot write or command anything. Do not widen this set - see
# PROJECT.md, "Safety constraints".
ALLOWED_SERVICES = frozenset({0x19, 0x22, 0x3E})

DEFAULT_CSV = "N63TU2_DME_readable_measurements.csv"
FAULT_CSV = "BMW_fault_code_texts.csv"
STATUS_CSV = "BMW_dtc_status_byte.csv"
ENUM_CSV = "BMW_enum_tables.csv"
FREEZE_ENUM_CSV = "BMW_freeze_enum_tables.csv"   # modules' snapshot value tables
FREEZE_CSV = "BMW_freeze_frame_fields.csv"
MODULE_MAP_CSV = "G30_module_sgbd_map.csv"
# Per-module catalogs from tools/module_catalog.py: <NAME>_measurements.csv
# and <NAME>_enum_tables.csv for each.
MODULE_CATALOGS = ("NBTEVO", "GSZFB1", "ACSM5", "DSC_G30N", "BDC_G11",
                   "EPS_G11", "VDP_G11", "LMV_G11", "IHKA_G11", "EKP301",
                   "PMA_20")

DATA_FILES = (DEFAULT_CSV, FAULT_CSV, STATUS_CSV, ENUM_CSV, FREEZE_CSV, FREEZE_ENUM_CSV,
              MODULE_MAP_CSV)


def replace_file(tmp, path, tries=10):
    """os.replace(tmp, path), tried again for a moment while Windows says the
    file is in use (a virus scanner or the search indexer opening it just
    then: "Access is denied", seen in the test suite)."""
    for i in range(tries):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.05)


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


# The logo (tools/make_icon.py): .ico for Windows, .png for Tk elsewhere.
ICON_FILES = ("n63read.ico", "n63read.png")


# ------------------------------------------------------------- HSFZ framing

class HsfzError(Exception):
    pass


class HsfzTimeout(HsfzError):
    """Nothing answered in time: an empty address, or a module asleep."""


class ConnectionLost(HsfzError):
    """The connection itself is gone (closed or reset), not just one read:
    the reader passes this on, so a log can reconnect."""


class NegativeResponse(HsfzError):
    def __init__(self, service, code):
        self.service = service
        self.code = code
        name = NRC.get(code, "unknown")
        super().__init__("negative response to 0x%02X: 0x%02X (%s)" % (service, code, name))


# How many bytes after the service a positive reply echoes from the request:
# the first identifier for 0x22, the sub-function for 0x19 and 0x3E. A reply
# that echoes something else answers an earlier request that timed out.
REPLY_ECHO = {0x22: 2, 0x19: 1, 0x3E: 1}


class Hsfz:
    """
    HSFZ frame: [length:4 BE][control:2 BE][source:1][target:1][uds payload...]

    `length` counts the source and target bytes plus the payload, i.e. two
    more than the UDS payload length. Note this is NOT DoIP - BMW F/G cars
    use HSFZ on port 6801 with single-byte addressing and no routing
    activation step.
    """

    TRACE_LIMIT = 400               # frames kept when tracing
    # Largest frame accepted. Real ones are a few hundred bytes; without a
    # cap a bad length field from whatever answers at the IP has us try to
    # allocate up to 4 GB.
    MAX_FRAME = 0x10000

    def __init__(self, ip, port=HSFZ_DIAG_PORT, source=TESTER_ADDR, timeout=3.0, *,
                 trace=None):
        self.ip = ip
        self.port = port
        self.source = source
        self.timeout = timeout
        self.sock = None
        # A list to record the first TRACE_LIMIT frames into, for diagnostics:
        # (seconds since connect, "tx"/"rx", control, source, target, hex).
        self.trace = trace
        self._t0 = time.time()
        # One request at a time: the holder's own reads and the requests it
        # lends to other processes run on different threads.
        self.lock = threading.RLock()
        self.on_close = []              # called first by close() (stop lending)

    def __enter__(self):
        if self.sock is None:           # share.open_tester() hands it over open
            self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def connect(self):
        self._t0 = time.time()
        self.sock = socket.create_connection((self.ip, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        self._note("connected", None, None, None, b"")

    def _note(self, direction, control, src, dst, data):
        if self.trace is not None and len(self.trace) < self.TRACE_LIMIT:
            self.trace.append((round(time.time() - self._t0, 4), direction,
                               control, src, dst, data.hex()))

    def close(self):
        callbacks, self.on_close = self.on_close, []
        for callback in callbacks:
            callback()
        with self.lock:
            if self.sock:
                try:
                    self.sock.close()
                finally:
                    self.sock = None

    def _set_timeout(self, seconds):
        self.timeout = seconds
        if self.sock:
            self.sock.settimeout(seconds)

    # -- low level ---------------------------------------------------------

    def _send(self, target, payload):
        header = struct.pack(">IH", len(payload) + 2, CTRL_DIAG)
        try:
            self.sock.sendall(header + bytes([self.source, target]) + payload)
        except OSError as e:
            raise ConnectionLost("connection lost: %s" % e) from e
        self._note("tx", CTRL_DIAG, self.source, target, payload)

    def _recv_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = self.sock.recv(n - len(buf))
            except socket.timeout:
                # Surface as HsfzError so callers (scan especially) can treat
                # a silent address as "nothing there" rather than a crash.
                raise HsfzTimeout("timed out waiting for response") from None
            except OSError as e:
                raise ConnectionLost("connection lost: %s" % e) from e
            if not chunk:
                raise ConnectionLost("connection closed by ECU")
            buf += chunk
        return bytes(buf)

    def _recv_frame(self):
        head = self._recv_exact(6)
        length, control = struct.unpack(">IH", head)
        if length > self.MAX_FRAME:
            raise HsfzError("bad frame from %s: length %d (control 0x%04X)"
                            % (self.ip, length, control))
        body = self._recv_exact(length)
        if length >= 2:
            self._note("rx", control, body[0], body[1], body[2:])
            return control, body[0], body[1], body[2:]
        self._note("rx", control, None, None, body)
        return control, None, None, body

    # -- request/response --------------------------------------------------

    def request(self, target, payload, retries=4):
        """Send a UDS payload and return the positive response payload."""
        with self.lock:
            return self._request(target, payload, retries)

    def _request(self, target, payload, retries):
        service = payload[0]
        if service not in ALLOWED_SERVICES:
            raise ValueError(
                "refusing service 0x%02X - this tool only sends 0x19/0x22/0x3E"
                % service
            )

        self._send(target, payload)
        deadline = time.time() + self.timeout * (retries + 1)
        busy = 0

        while time.time() < deadline:
            control, src, dst, data = self._recv_frame()

            # The gateway echoes the request back as an ack; ignore it.
            if control == CTRL_ACK:
                continue
            if control in HSFZ_NACK:
                text = "the car refused the request: %s" % HSFZ_NACK[control]
                if control == 0x0040:     # the whole connection: reconnect
                    raise ConnectionLost(text)
                raise HsfzError(text)
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
                if code == 0x21 and busy < BUSY_RETRIES:   # busy: ask again
                    busy += 1
                    time.sleep(BUSY_WAIT)
                    self._send(target, payload)
                    deadline = time.time() + self.timeout * (retries + 1)
                    continue
                raise NegativeResponse(data[1], code)

            if data[0] == service + 0x40:
                echo = REPLY_ECHO.get(service, 0)
                if data[1:1 + echo] == payload[1:1 + echo]:
                    return data
                continue      # a late answer to an earlier request
            # Not ours (another module chattering) - keep waiting.

        raise HsfzTimeout("timed out waiting for response")

    def read_did(self, target, did, timeout=None):
        """UDS 0x22. Returns the raw payload bytes after the echoed identifier.

        `timeout` overrides the connection's for this read: an absent or
        sleeping module then costs that long, not the full timeout.
        """
        if timeout is not None:
            with self.lock:
                saved = self.timeout
                self._set_timeout(timeout)
                try:
                    return self.read_did(target, did)
                finally:
                    self._set_timeout(saved)
        resp = self.request(target, bytes([0x22, (did >> 8) & 0xFF, did & 0xFF]))
        if len(resp) < 3:
            raise HsfzError("short response")
        echoed = (resp[1] << 8) | resp[2]
        if echoed != did:
            raise HsfzError("identifier mismatch: asked 0x%04X got 0x%04X" % (did, echoed))
        return resp[3:]

    def read_dids(self, target, dids, timeout=None):
        """UDS 0x22 with several identifiers in one request. Returns the reply
        after 0x62: each identifier echoed, then its data (split_reply())."""
        if timeout is not None:
            with self.lock:
                saved = self.timeout
                self._set_timeout(timeout)
                try:
                    return self.read_dids(target, dids)
                finally:
                    self._set_timeout(saved)
        resp = self.request(target, b"\x22" + b"".join(d.to_bytes(2, "big") for d in dids))
        return resp[1:]

    def tester_present(self, target):
        return self.request(target, bytes([0x3E, 0x00]))

    def probe(self, target, timeout=None):
        """True if anything answers TesterPresent at `target`, even negatively.

        Uses a short timeout (default PROBE_TIMEOUT): a present module answers
        in milliseconds, and a sweep spends most of its time waiting on empty
        addresses. Raise it if real modules are being missed.
        """
        timeout = PROBE_TIMEOUT if timeout is None else timeout
        with self.lock:
            saved = self.timeout
            self._set_timeout(timeout)
            try:
                self.request(target, bytes([0x3E, 0x00]), retries=0)
                return True
            except NegativeResponse:
                return True
            except ConnectionLost:
                raise
            except HsfzError:
                return False
            finally:
                self._set_timeout(saved)

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

    def read_snapshots(self, target, code):
        """UDS 0x19 sub-function 0x04: the conditions stored when fault `code`
        (6 hex digits) set - odometer, time and the like. Raw 59 04 reply."""
        return self.request(target, bytes([0x19, 0x04]) + bytes.fromhex(code) + b"\xff")

    def read_extended(self, target, code):
        """UDS 0x19 sub-function 0x06: the fault's counters (how often it
        occurred, healing). Raw 59 06 reply."""
        return self.request(target, bytes([0x19, 0x06]) + bytes.fromhex(code) + b"\xff")


# The N63 has a DME per bank: 0x12 (master) runs bank 1, 0x13 (slave) bank 2,
# and both use the same identifiers. Bank 2's are read from 0x13 under the
# bank-1 names; the catalog's _BANK2 / _2 identifiers are refused on this car.
DME_SLAVE = 0x13
BANK2_SUFFIX = "@0x13"
BANK2_IDLE = 10.0               # s without a bank-2 read before its connection closes
BANK2_RETRY = 30.0              # s before trying again after it was refused


class Tester(Hsfz):
    """The car's diagnostic connections, as one Hsfz.

    The gateway allows two connections, each with its own tester address.
    The first (`source`) carries 0x12 and every other module; the slave DME
    (0x13) gets the second (`source` + 1), opened on its first request and
    closed by tidy() once it has been idle for BANK2_IDLE seconds.
    """

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.slave = None
        self.slave_used = 0.0
        self.slave_refused_until = 0.0

    def request(self, target, payload, retries=4):
        with self.lock:                 # the bank-2 link is part of this one
            return self._route(target, payload, retries)

    def _route(self, target, payload, retries):
        if target != DME_SLAVE:
            return super().request(target, payload, retries)
        slave = self._slave()
        try:
            return slave.request(target, payload, retries)
        except (ConnectionLost, ConnectionError) as e:
            # Only bank 2's connection went: drop it (the next read opens a
            # new one) without taking the main connection, and the log, down.
            self.close_slave()
            raise HsfzError("bank 2 connection lost: %s" % e) from None

    def _slave(self):
        now = time.time()
        if self.slave is None:
            if now < self.slave_refused_until:
                raise HsfzError("bank 2 connection unavailable (the car allows two "
                                "connections; another tool may hold one)")
            slave = Hsfz(self.ip, self.port, source=(self.source + 1) & 0xFF,
                         timeout=self.timeout, trace=self.trace)
            try:
                slave.connect()
            except OSError as e:
                self.slave_refused_until = now + BANK2_RETRY
                raise HsfzError("bank 2 connection failed: %s" % e) from None
            self.slave = slave
        self.slave._set_timeout(self.timeout)
        self.slave_used = now
        return self.slave

    def tidy(self, now=None):
        """Close the bank-2 connection once nothing has used it for a while."""
        now = time.time() if now is None else now
        with self.lock:
            if self.slave and now - self.slave_used > BANK2_IDLE:
                self.close_slave()

    def close_slave(self):
        with self.lock:
            if self.slave:
                self.slave.close()
                self.slave = None

    def close(self):
        callbacks, self.on_close = self.on_close, []
        for callback in callbacks:
            callback()                  # stop lending before the line goes
        self.close_slave()
        super().close()


def connection_problem(e):
    """Plain words for a connection the car dropped, or None."""
    if isinstance(e, ConnectionResetError) or getattr(e, "winerror", None) == 10054:
        return ("The car closed the connection. It allows two diagnostic connections "
                "at a time, so another one is probably open: another n63read, a "
                "Claude session not sharing this one, or another diagnostic tool.")
    return None


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


def dtc_failed(status):
    """True if the code has actually failed: now, this or last cycle, or since
    the memory was cleared. A "confirmed" bit on a code whose test has not
    run since the last clear does not count: some modules (the DSC on this
    car) report their whole list that way, test entries included."""
    if status & 0x25:               # testFailed, pending, testFailedSinceLastClear
        return True
    return bool(status & 0x08) and not status & 0x10


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
    if not dtc_failed(status) and status & 0x08:
        text += " (but not tested since cleared)"
    return text


# ------------------------------------------------------------- fault sweep

def parse_probe_timeout(text):
    """Validate a user-entered probe wait in seconds. Raises ValueError."""
    value = float(text)
    lo, hi = PROBE_TIMEOUT_RANGE
    if not lo <= value <= hi:
        raise ValueError("probe wait must be between %g and %g seconds" % (lo, hi))
    return value


def sweep_seconds(probe_timeout=None):
    """Worst-case time to sweep SWEEP_RANGE if every address is empty."""
    lo, hi = SWEEP_RANGE
    return (hi - lo + 1) * (PROBE_TIMEOUT if probe_timeout is None else probe_timeout)


# Addresses confirmed for this car (docs/modules.md). Others are named from
# G30_module_sgbd_map.csv where it has exactly one module at that address.
KNOWN_MODULES = {
    0x01: "Airbag / crash safety (ACSM)",
    0x08: "Lane-change warning (HC2)",
    0x0D: "Rear function module (HKFM)",
    0x10: "Central gateway (ZGW)",
    0x06: "Surround view / reversing camera (ICAM2)",
    0x12: "Engine, master DME",
    0x13: "Engine, slave DME (bank 2)",
    0x17: "Fuel pump control (EKP)",
    0x18: "Transmission, ZF 8HP (EGS)",
    0x19: "Transfer case, xDrive (LMV)",
    0x21: "Front radar, active cruise (FRR)",
    0x22: "Optional-equipment module (SAS)",
    0x2B: "Rear-axle steering (HSR)",
    0x2C: "Park assist (PMA / PDC)",
    0x29: "Stability control (DSC)",
    0x2E: "Power control unit (PCU)",
    0x30: "Electric power steering (EPS)",
    0x40: "Body domain controller (BDC)",
    0x4F: "Light effect manager (LEM)",
    0x56: "Roof function centre (FZD)",
    0x5D: "Camera driver assistance (KAFAS)",
    0x5E: "Gear selector (GWS)",
    0x60: "Instrument cluster (KOMBI)",
    0x61: "Telematics (ATM)",
    0x63: "Head unit (NBT Evo)",
    0x6D: "Driver's seat module (FAS)",
    0x6E: "Passenger seat module (BFS)",
    0x76: "Suspension, vertical dynamics (VDP)",
    0x78: "Climate control (IHKA)",
}

FAULT_CSV_HEADER = ("read_at", "module", "module_name", "code", "status",
                    "state", "text_en", "text_de", "catalog_source",
                    # What the module stored with the fault (fault_details):
                    "first_km", "last_km", "occurrences", "details")


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


def detail_columns(details):
    """A fault's stored details as fault-CSV columns: the odometer at its
    first and latest snapshot, how often it occurred, and everything as JSON."""
    kms = [f["value"] for rec in details.get("snapshots", []) for f in rec["fields"]
           if f["id"] == 0x1700 and isinstance(f["value"], (int, float))]
    times = next((e["value"] for e in details.get("extended", [])
                  if e.get("name") == "HFK"), "")
    return {"first_km": min(kms) if kms else "", "last_km": max(kms) if kms else "",
            "occurrences": times,
            "details": json.dumps(details, ensure_ascii=False, separators=(",", ":"))}


def _add_details(h, row, fields, lang):
    if row["code"] and dtc_failed(int(row["status"], 16)):
        row.update(detail_columns(fault_details(h, int(row["module"], 16), row["code"],
                                                fields, lang)))


def fault_steps(h, modules=None, texts=None, names=None, probe_timeout=None, details=True,
                lang="text_en"):
    """Sweep for modules (unless given) and read their faults, one step at a time.

    A generator that does one network round trip per step and yields
    ("finding", addr), ("found", addr), ("reading", addr) or ("detail", addr)
    after each, then ("done", rows). With `details`, each fault's snapshots
    and counters are read too (detail_columns). The GUI advances it between
    samples so a running log keeps recording during the read.
    """
    if modules is None:
        lo, hi = SWEEP_RANGE
        modules = []
        for addr in range(lo, hi + 1):
            present = h.probe(addr, probe_timeout)
            if present:
                modules.append(addr)
                yield ("found", addr)
            else:
                yield ("finding", addr)

    texts = load_fault_texts() if texts is None else texts
    names = load_module_names() if names is None else names
    read_at = time.strftime("%Y-%m-%d %H:%M:%S")
    fields = load_freeze_fields() if details else None
    rows = []
    for addr in modules:
        found = _module_fault_rows(h, addr, texts, names, read_at)
        rows.extend(found)
        yield ("reading", addr)
        if details:
            for row in found:
                if row["code"]:
                    _add_details(h, row, fields, lang)
                    yield ("detail", addr)
    yield ("done", rows)


def scan(h, lo=None, hi=None, timeout=None):
    """Who answers TesterPresent (3E 00) at lo..hi (default SWEEP_RANGE).

    Yields (address, note) for each that answers: note "" for a plain
    answer, the NRC's name for a negative one (still a module), or
    "error: ..." when something other than silence went wrong there.
    Silent addresses are skipped; a lost connection ends the scan.
    """
    lo = SWEEP_RANGE[0] if lo is None else lo
    hi = SWEEP_RANGE[1] if hi is None else hi
    timeout = PROBE_TIMEOUT if timeout is None else timeout
    for addr in range(lo, hi + 1):
        with h.lock:
            saved = h.timeout
            h._set_timeout(timeout)
            try:
                h.request(addr, bytes([0x3E, 0x00]), retries=0)
                note = ""
            except NegativeResponse as e:
                note = NRC.get(e.code, "nrc 0x%02X" % e.code)
            except (ConnectionLost, ConnectionError):
                raise
            except HsfzError:
                note = None                     # nothing there
            except Exception as e:                                  # noqa: BLE001
                note = "error: %s: %s" % (type(e).__name__, e)
            finally:
                h._set_timeout(saved)
        if note is not None:
            yield addr, note


def sweep_modules(h, lo=None, hi=None, progress=None, probe_timeout=None):
    """Addresses in lo..hi (default SWEEP_RANGE) that answer TesterPresent."""
    lo = SWEEP_RANGE[0] if lo is None else lo
    hi = SWEEP_RANGE[1] if hi is None else hi
    found = []
    for addr in range(lo, hi + 1):
        if progress:
            progress(addr)
        if h.probe(addr, probe_timeout):
            found.append(addr)
    return found


def read_all_faults(h, modules, texts=None, names=None, progress=None, everything=False,
                    details=False, lang="text_en"):
    """Read fault memory (0x19) from each module. Returns rows for a fault CSV.

    Every module gets at least one row, so the file shows what was checked:
    one per fault, or a single row saying "no faults" or why it refused.
    Modules list every code they can raise, mostly ones that never failed;
    those are left out (see dtc_failed) unless `everything`. With `details`,
    each fault's snapshots and counters are read too (detail_columns).
    """
    texts = load_fault_texts() if texts is None else texts
    names = load_module_names() if names is None else names
    fields = load_freeze_fields() if details else None
    read_at = time.strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for addr in modules:
        if progress:
            progress(addr)
        found = _module_fault_rows(h, addr, texts, names, read_at, everything)
        if details:
            for row in found:
                _add_details(h, row, fields, lang)
        rows.extend(found)
    return rows


def _module_fault_rows(h, addr, texts, names, read_at, everything=False):
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
    hidden = 0
    if not everything:
        kept = [d for d in dtcs if dtc_failed(d[1])]
        hidden, dtcs = len(dtcs) - len(kept), kept
    if not dtcs:
        return [dict(base, state="no faults" + (
            " (%d listed codes never failed)" % hidden if hidden else ""))]
    rows = []
    for code, status in sorted(dtcs):
        entry = texts.get(code.upper(), {})
        rows.append(dict(base, code=code, status="0x%02X" % status,
                         state=dtc_status_summary(status),
                         text_en=entry.get("text_en", "") or "(not in catalog)",
                         text_de=entry.get("text_de", ""),
                         catalog_source=entry.get("source_module", "")))
    return rows


# ------------------------------------------------------------- fault details
#
# What a module stored with a fault. A snapshot (0x19 04) holds identifiers
# with values; BMW's standard ones (0x17xx: odometer, time, SAE code, fault
# class, power state) are the same in every UDS module on this car, and are
# in FREEZE_CSV. A module's own environment values may follow; without their
# sizes they can't be split, so the rest is shown raw. The extended data
# (0x19 06) holds counters, from DTCEXTENDEDDATARECORDNUMBER.

FREEZE_LABELS = {
    0x1700: "Odometer", 0x1701: "Vehicle time", 0x1702: "SAE code",
    0x1731: "Fault class", 0x1750: "Power state (base network)",
    0x1751: "Power state (sub-networks)", 0x1768: "Odometer (extended)",
    0x1769: "Vehicle time (extended)", 0xFFFF: "Unknown identifier",
}
FREEZE_UNITS = {0x1700: "km", 0x1768: "km", 0x1701: "s", 0x1769: "s"}

# BMW's own tables for these, the same in every module file that has them.
FAULT_CLASSES = {
    0x00: ("Keine Fehlerklasse verfuegbar", "no fault class"),
    0x01: ("Ueberpruefung bei naechstem Werkstattbesuch", "check at the next service"),
    0x02: ("Ueberpruefung bei naechstem Halt", "check at the next stop"),
    0x04: ("Ueberpruefung sofort erforderlich !", "check immediately"),
    0xFF: ("unbekannte Fehlerklasse", "unknown fault class"),
}
POWER_STATES = {
    0x00: ("Reserviert", "reserved"), 0x01: ("Parken BN niO", "parked, power not OK"),
    0x02: ("Parken BN iO", "parked, power OK"),
    0x03: ("Standfunktionen Kunde nicht in Fahrzeug",
           "standby functions, driver not in the car"),
    0x05: ("Wohnen", "residing"), 0x07: ("Pruefen Analyse Diagnose", "test, analysis, diagnosis"),
    0x08: ("Fahrbereitschaft herstellen", "getting ready to drive"), 0x0A: ("Fahren", "driving"),
    0x0C: ("Fahrbereitschaft beenden", "ending readiness to drive"),
    0x0E: ("nicht verfügbar", "not available"), 0xFF: ("ungültig", "invalid"),
}
ENUM_FIELDS = {0x1731: FAULT_CLASSES, 0x1750: POWER_STATES}

EXTENDED_RECORDS = {        # record number -> (name, English, bytes)
    0x01: ("CONDITION_BYTE", "condition byte", 1),
    0x02: ("HFK", "times it occurred", 1),
    0x03: ("HLZ", "healing counter", 1),
}


def field_bytes(datatype):
    """Bytes a freeze field takes, from its SGBD type: a mask (0xFFFFFF is 3),
    a C type, or a number (text of that length). None if unknown."""
    t = (datatype or "").strip().lower()
    if re.fullmatch(r"0x(ff)+", t):
        return (len(t) - 2) // 2
    if t.isdigit():
        return int(t)
    return {"u char": 1, "unsigned char": 1, "signed char": 1, "char": 1,
            "unsigned int": 2, "signed int": 2, "int": 2,
            "unsigned long": 4, "signed long": 4, "long": 4, "float": 4}.get(t)


def load_freeze_fields():
    """(module or None, identifier) -> field row, from FREEZE_CSV."""
    fields = {}
    path = resource_path(FREEZE_CSV)
    if not os.path.exists(path):
        return fields
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                uw = int(row["uw_id"], 16)
            except (KeyError, ValueError):
                continue
            # One row can serve several modules ("0x12 0x13": both DMEs).
            modules = (row.get("module") or "").split()
            for module in modules or [None]:
                fields[(int(module, 16) if module else None, uw)] = row
    return fields


def _number(text, default):
    try:
        return float(text)
    except (TypeError, ValueError):
        return default                  # "-": no scaling


def _freeze_value(uw, raw, row, lang, enums=None):
    """(value, text) for one snapshot field."""
    if uw == 0x1702 or (row.get("unit") or "").upper() in ("HEX", "TEXT"):
        return raw.hex().upper(), ""
    dtype = (row.get("datatype") or "").strip().lower()
    order = "little" if (row.get("byte_order") or "").strip().lower() == "low" else "big"
    n = int.from_bytes(raw, order, signed=dtype.startswith("signed"))
    value = n * _number(row.get("mul"), 1.0) / (_number(row.get("div"), 1.0) or 1.0) + \
        _number(row.get("add"), 0.0)
    value = int(value) if value == int(value) else round(value, 6)
    text = ""
    table = ENUM_FIELDS.get(uw)
    if table and n in table:
        text = table[n][0 if lang == "text_de" else 1]
    elif enums and (row.get("name") or "-") != "-":
        entry = enums.get(row["name"].upper(), {}).get(n)
        if entry:
            text = entry.get(lang) or entry.get("text_de") or ""
    return value, text


def _freeze_label(uw, row, lang):
    if uw in FREEZE_LABELS:
        return FREEZE_LABELS[uw]
    return (row.get("name_de") if lang == "text_de" else row.get("name_en")) or \
        row.get("name_de") or ""


def _freeze_unit(uw, row):
    if uw in FREEZE_UNITS:
        return FREEZE_UNITS[uw]
    unit = (row.get("unit") or "").strip()
    return "" if unit in ("", "-", "0-n", "TEXT", "HEX") else unit


def parse_snapshots(resp, module, fields=None, lang="text_en", enums=None):
    """Split a 59 04 reply into records: [{"record", "fields": [{id, name,
    label, value, unit, text}], "rest": hex of what couldn't be split}]."""
    fields = load_freeze_fields() if fields is None else fields
    enums = load_enum_tables() if enums is None else enums
    body, i, records = resp[6:], 0, []
    while i + 2 <= len(body):
        number, count = body[i], body[i + 1]
        first = body[i + 2:i + 4]       # the next record starts the same way
        i += 2
        record = {"record": number, "fields": [], "rest": ""}
        records.append(record)
        for _ in range(count):
            if i + 2 > len(body):
                break
            uw = int.from_bytes(body[i:i + 2], "big")
            row = fields.get((module, uw)) or fields.get((None, uw))
            size = field_bytes(row.get("datatype")) if row else None
            if size is None or i + 2 + size > len(body):
                # A module's own value of unknown size: the rest of this
                # record can't be split. A later record (the latest time the
                # fault set) begins with its number, the same count and the
                # same first identifier; carry on from there if it's found.
                header = bytes([(number + 1) & 0xFF, count]) + first
                nxt = body.find(header, i) if len(first) == 2 else -1
                record["rest"] = body[i:nxt if nxt > i else len(body)].hex()
                if nxt <= i:
                    return records
                i = nxt
                break
            raw = body[i + 2:i + 2 + size]
            value, text = _freeze_value(uw, raw, row, lang, enums)
            record["fields"].append({
                "id": uw, "name": row.get("name_de") or "", "label": _freeze_label(uw, row, lang),
                "value": value, "unit": _freeze_unit(uw, row), "text": text})
            i += 2 + size
    return records


def parse_extended(resp):
    """Split a 59 06 reply into [{"record", "name", "label", "value"}]; a
    record of unknown size ends the list, its bytes given raw."""
    body, i, out = resp[6:], 0, []
    while i < len(body):
        number = body[i]
        name, label, size = EXTENDED_RECORDS.get(number, ("", "", None))
        if size is None or i + 1 + size > len(body):
            out.append({"record": number, "name": "", "label": "",
                        "value": body[i + 1:].hex()})
            break
        out.append({"record": number, "name": name, "label": label,
                    "value": int.from_bytes(body[i + 1:i + 1 + size], "big")})
        i += 1 + size
    return out


# The DME takes more than the usual 2 s to say it keeps no snapshot for a
# fault; with 6 s it answers (in-car, 2026-10-18).
DETAILS_TIMEOUT = 6.0


def fault_details(h, module, code, fields=None, lang="text_en"):
    """{"snapshots", "extended", "errors"} for one fault: each read is tried
    on its own, so a module that keeps no snapshots still gives counters."""
    out = {"snapshots": [], "extended": [], "errors": []}
    saved = getattr(h, "timeout", None)
    longer = saved is not None and saved < DETAILS_TIMEOUT and hasattr(h, "_set_timeout")
    with getattr(h, "lock", None) or contextlib.nullcontext():
        if longer:
            h._set_timeout(DETAILS_TIMEOUT)
        try:
            for key, read, parse in (
                    ("snapshots", h.read_snapshots,
                     lambda r: parse_snapshots(r, module, fields, lang)),
                    ("extended", h.read_extended, parse_extended)):
                try:
                    out[key] = parse(read(module, code))
                except NegativeResponse as e:
                    out["errors"].append("%s: %s" % (key, NRC.get(e.code, "0x%02X" % e.code)))
                except HsfzTimeout as e:
                    out["errors"].append("%s: %s" % (key, e))
        finally:
            if longer:
                h._set_timeout(saved)
    return out


def format_details(details):
    """Fault details as lines of text, for the CLI, the app and Claude."""
    lines = []
    for rec in details["snapshots"]:
        lines.append("snapshot %d:" % rec["record"])
        for f in rec["fields"]:
            text = " (%s)" % f["text"] if f["text"] else ""
            lines.append("  %-28s %s%s%s" % (f["label"] or f["name"] or "0x%04X" % f["id"],
                                              f["value"], " " + f["unit"] if f["unit"] else "",
                                              text))
        if rec["rest"]:
            lines.append("  module's own values (not decoded): %s" % rec["rest"])
    for ext in details["extended"]:
        lines.append("%-30s %s" % (ext["label"] or "record 0x%02X" % ext["record"], ext["value"]))
    lines += ["(%s)" % e for e in details["errors"]]
    return lines or ["(no details stored)"]


def write_fault_csv(path, rows):
    """Write read_all_faults() rows. utf-8-sig so Excel shows umlauts."""
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FAULT_CSV_HEADER, restval="", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


# --------------------------------------------------------- head unit GPS
#
# From the NBT Evo SGBD (nbtevo.prg). Its STATUS_GPS_POSITION job is
# hand-coded rather than table-driven, so the layout below comes from
# disassembling that job: it sends 22 D0 39 and reads, after the echoed DID,
#   [0]      validity, 0x00 = valid, 0x01 = not valid
#   [1..5]   latitude:  sign, degrees, minutes, seconds, 1/100 s
#   [6..10]  longitude: sign, degrees, minutes, seconds, 1/100 s
#            (sign + degrees are a nine-bit two's-complement number: see _dms)
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
    """5 bytes (sign, deg, min, sec, 1/100 s) to signed decimal degrees.

    The first byte is the ninth bit of a two's-complement degree count, not a
    hemisphere flag over a plain one: 100 deg W comes as 01 9C (0x19C = -100
    in nine bits), not 01 64. Seen on the car (October 2026); the minutes and
    seconds are the magnitude's.
    """
    whole = ((b[0] & 0x01) << 8) | b[1]
    if b[0] & 0x01:
        whole -= 0x200
    deg = abs(whole) + b[2] / 60 + (b[3] + b[4] / 100) / 3600
    return -deg if whole < 0 else deg


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


# Vehicle identification requests. BMW's tools (and ediabaslib) send the
# zero-length HSFZ form; the 2-byte form is what this tool sent first and is
# kept in case a gateway wants it. The DoIP one (ISO 13400, UDP 13400) is only
# to tell a DoIP car apart: this tool speaks HSFZ.
HSFZ_IDENT_PROBES = (
    struct.pack(">IH", 0, 0x0011),
    struct.pack(">IH", 2, 0x0011) + b"\x00\x00",
)
DOIP_PORT = 13400
DOIP_IDENT_PROBE = bytes([0x02, 0xFD, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00])


def reply_protocol(payload):
    """"DoIP", "HSFZ" or "unknown", from a discovery reply's first bytes.

    HSFZ needs the vehicle identification control word, so a stray datagram
    on the port can't pass for the car.
    """
    if payload[:2] == b"\x02\xfd":
        return "DoIP"
    if len(payload) >= 6 and payload[4:6] == b"\x00\x11":
        return "HSFZ"
    return "unknown"


def doip_help(ip):
    """What a DoIP-only answer to Discover means, and what to do about it."""
    return ("The car at %s answered Discover with DoIP (UDP %d), not HSFZ. This tool "
            "speaks HSFZ, the protocol the 2018 G30 uses on its OBD Ethernet pins.\n\n"
            "That usually means one of:\n"
            "- a newer car, or a gateway updated to a software level that uses DoIP;\n"
            "- something else on this network answered (check the address is the car's);\n"
            "- the ENET adapter or cable is meant for a different car.\n\n"
            "n63read can't read a DoIP car yet. A diagnostics report shows exactly what "
            "answered; send it on to get DoIP support added." % (ip, DOIP_PORT))


def discovery_outcome(hits, near):
    """What Discover found, as (what, ip): "found" (an HSFZ car; ip is the
    first), "doip" (only a DoIP car answered), "near" (no answer, but a
    device is on the car's link: probably the car) or "none" (ip None).
    Only a well-formed HSFZ identification counts as the car; a stray
    datagram never does."""
    hsfz = [ip for ip, p in hits if reply_protocol(p) == "HSFZ"]
    doip = [ip for ip, p in hits if reply_protocol(p) == "DoIP"]
    if hsfz:
        return "found", hsfz[0]
    if doip:
        return "doip", doip[0]
    if near:
        return "near", near[0]
    return "none", None


def reply_vin(payload):
    """The VIN in an HSFZ identification reply ("...BMWVIN<17 chars>"), or ""."""
    m = re.search(rb"BMWVIN([A-HJ-NPR-Z0-9]{17})", payload)
    return m.group(1).decode("ascii") if m else ""


def link_neighbours(local_ip):
    """Other devices Windows has seen on the link `local_ip` is on (arp -a).

    Before the car answers anything it asks, by ARP, who has this PC's
    address; Windows keeps that even when the answer itself is dropped. So
    with no reply to Discover, this still finds the car's address. Empty
    off Windows or if arp fails.
    """
    import subprocess
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)     # no console flash in the GUI
    try:
        out = subprocess.run(["arp", "-a", "-N", local_ip], capture_output=True,
                             text=True, timeout=5, creationflags=flags).stdout
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    found = []
    for ip, mac in re.findall(r"^\s*(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){5})",
                              out, re.M):
        first = int(ip.split(".")[0])
        if (ip == local_ip or ip.endswith(".255") or first >= 224
                or mac.lower().startswith(("ff-ff-ff", "01-00-5e", "00-00-00"))):
            continue                    # broadcast, multicast, empty
        if ip not in found:
            found.append(ip)
    return found


class AddressNotAvailable(OSError):
    """The local address to send from isn't on this PC (WinError 10049)."""


def link_local(addrs):
    """The ENET side: 169.254.x.x addresses, in order."""
    return [a for a in addrs if a.startswith("169.254.")]


LINK_WAIT = 10.0                # s to wait for the ENET adapter's address

# Where the car keeps its mileage, tried in order: the transfer case's copy
# from the bus (a field of a record), then the suspension module's EEPROM
# copy (3 bytes, km). Both matched the dash at the car (2026-10-18).
ODOMETERS = [(0x19, 0xDFE7, "LMV_DATEN_VERTEILERGETRIEBE.MILE_KM"), (0x76, 0x2540, None)]
ODOMETER_EVENT = "odometer %d km"
ODOMETER_RE = re.compile(r"\bodometer (\d+) km\b")


def read_odometer(h, layouts):
    """The car's mileage in km, or None if no module gives a believable one."""
    for module, did, arg in ODOMETERS:
        try:
            raw = h.read_did(module, did, timeout=MODULE_TIMEOUT)
        except ConnectionLost:
            raise
        except HsfzError:
            continue
        if arg:
            fields = layouts.get((module, did))
            value = decode_record(raw, fields).get(arg, (None,))[0] if fields else None
        else:
            value = int.from_bytes(raw[:3], "big") if len(raw) >= 3 else None
        if isinstance(value, (int, float)) and 0 < value < 2000000:
            return int(value)
    return None


def wait_for_link_local(timeout=None, step=0.5, addrs=None, sleep=time.sleep):
    """This PC's 169.254.x.x addresses, waiting up to `timeout` s for one to
    appear: right after the PC wakes or the app restarts for an update, the
    adapter is still getting its address and Discover would find nothing."""
    addrs = addrs or local_ipv4s
    end = time.time() + (LINK_WAIT if timeout is None else timeout)
    while True:
        found = link_local(addrs())
        if found or time.time() >= end:
            return found
        sleep(step)


def discover(timeout=3.0, bind_ip=None, details=None):
    """
    UDP broadcast on 6811 (HSFZ) and 13400 (DoIP, to recognise such a car).
    Returns [(ip, payload_bytes), ...]; reply_protocol(payload) tells which.
    `details`, if a list, gets every datagram received (duplicates too) with
    its source port and arrival time, for diagnostics.

    bind_ip pins the broadcast to one interface. On a machine with WiFi up,
    an unbound broadcast leaves via the default route and never reaches the
    car - so pass the USB adapter's 169.254.x.x address.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if bind_ip:
        try:
            s.bind((bind_ip, 0))
        except OSError as e:
            s.close()
            # WinError 10049 / EADDRNOTAVAIL: the address isn't on this PC
            # (any more). The adapter is unplugged or not up yet, or it has
            # given itself a new 169.254.x.x after the link reset.
            raise AddressNotAvailable(
                "%s is not an address of this PC right now (%s). Plug in the "
                "ENET adapter, switch the ignition on, wait for Windows to "
                "give it a 169.254.x.x address, then pick that address."
                % (bind_ip, e.strerror or e)) from None
    s.settimeout(timeout)
    probes = [(p, HSFZ_CONTROL_PORT) for p in HSFZ_IDENT_PROBES] + \
        [(DOIP_IDENT_PROBE, DOIP_PORT)]
    for dest in ("255.255.255.255", "169.254.255.255"):
        for probe, port in probes:
            try:
                s.sendto(probe, (dest, port))
            except OSError:
                pass

    found = []
    sent = time.time()
    end = sent + timeout
    while time.time() < end:
        s.settimeout(max(0.05, end - time.time()))
        try:
            data, addr = s.recvfrom(1024)
        except socket.timeout:
            break
        except OSError as e:
            if details is not None:
                details.append({"ms": round((time.time() - sent) * 1000), "error": str(e)})
            continue                    # e.g. WSAECONNRESET from an ICMP reply
        if details is not None:
            details.append({"ms": round((time.time() - sent) * 1000), "from": addr[0],
                            "port": addr[1], "bytes": data.hex()})
        if data in HSFZ_IDENT_PROBES or data == DOIP_IDENT_PROBE:
            continue                    # our own broadcast, heard back
        if (addr[0], data) not in found:
            found.append((addr[0], data))
    s.close()
    # HSFZ replies first: that is the protocol this tool speaks. Then the
    # ENET link's 169.254.x.x, where the car is, ahead of anything on WiFi.
    rank = {"HSFZ": 0, "DoIP": 1, "unknown": 2}
    found.sort(key=lambda h: (rank[reply_protocol(h[1])],
                              not h[0].startswith("169.254.")))
    return found


# Text a spreadsheet would run as a formula when a log is opened in it.
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def log_cell(value):
    """A value as written to a CSV log: blank for no reading, and text that
    a spreadsheet would run as a formula made literal with a leading '."""
    if value is None:
        return ""
    if isinstance(value, str) and value.startswith(FORMULA_START):
        return "'" + value
    return value


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
    for name in (ENUM_CSV, FREEZE_ENUM_CSV) + tuple(m + "_enum_tables.csv"
                                                    for m in MODULE_CATALOGS):
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
    # wheel acceleration, ride height, dampers
    + [(0x76, d) for d in (0xD665, 0xDC05, 0xDC0A)]
    + [(0x19, d) for d in (0xDB32, 0xDFEA)]                                # xDrive clutch torque
    + [(0x17, 0x4010)]                                                     # fuel pump current
    + [(0x2C, d) for d in (0xD645, 0xD646, 0xD647, 0xD648, 0xD64A, 0xD64B,
                           0xD64C, 0xD64D, 0xD64E, 0xD64F, 0xD65C, 0xD65E)])  # parking distances
SLOW_READS_PER_CYCLE = 6       # module reads allowed per DME cycle
NO_ANSWER_BACKOFF = 30.0       # seconds to leave a silent module alone
MODULE_TIMEOUT = 0.5           # a present module answers in milliseconds; a
                               # missing or sleeping one must not stall the DME
CHECK_TIMEOUT = 2.0            # the catalog check waits longer: the BDC takes
                               # over 0.5 s for some records (in-car, 2026-10-18)
# DME channels (0x12, and 0x13 for bank 2) are read every cycle, so one that
# never answers would cost the full timeout every time and drag the whole
# log down. Instead a timeout skips that address for the rest of the cycle,
# rests it briefly, and rests the identifier itself for longer: a sleeping
# slave DME comes back within seconds, while one silent identifier on a
# working DME doesn't hold up the others.
VALUE_TIMEOUT = 1.0
VALUE_TARGET_BACKOFF = 5.0
VALUE_KEY_BACKOFF = 30.0
REFUSED_FOR_GOOD = (0x11, 0x12, 0x31)   # not supported / out of range: stop asking
RECONNECT_WAIT = 2.0           # s between tries after the connection dropped mid-log

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


# Raw values a field sends for "no value", which would otherwise read as a
# number. The head unit's speed difference is 65535 when it has none
# (in-car, 2026-10-19).
NO_VALUE = {"HU_STATUS_SPEED.SPEED_DIFFERENCE_PERCENT": 0xFFFF}


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
        elif int.from_bytes(chunk, "big") == NO_VALUE.get(meta["ARG"], -1):
            out[meta["ARG"]] = (None, "", "no value")
        else:
            value, unit = decode(chunk, meta)
            out[meta["ARG"]] = (value, unit, "")
        pos += size
    return out


# Values each DME keeps for its own four cylinders: the slave's cylinders
# 1-4 are the engine's 5-8 (docs/modules.md, bank 2). Uneven running and the
# misfire counters aren't among them: the master reports all eight.
OWN_CYLINDERS = re.compile(r"^(?:DIAG_BRENNDAUER_ZUENDFUNKE_CYL|IUKZ|SPG_KLOPF_ZYL|"
                           r"AKT_EINSPRITZMODUS_ZYL|FUNKENBRENNDAUER_FEHL_ZYL_)([1-4])$")
_CYLINDER_WORD = re.compile(r"\b(cylinder|Zylinder)( ?)([1-4])\b")


def engine_cylinder(name):
    """The engine's cylinder for a channel the DMEs keep per own cylinder:
    IUKZ1 -> 1, IUKZ1@0x13 -> 5. None for any other channel."""
    bank2 = name.endswith(BANK2_SUFFIX)
    m = OWN_CYLINDERS.match(name[:-len(BANK2_SUFFIX)] if bank2 else name)
    return None if not m else int(m.group(1)) + (4 if bank2 else 0)


def add_bank2(cat):
    """Add each DME channel again as NAME@0x13: the same identifier read from
    the slave DME, i.e. bank 2. Returns the catalog. A value the DMEs keep
    per own cylinder says which of the engine's it is (cylinder 5 for the
    slave's 1)."""
    for name, meta in list(cat.items()):
        if (meta.get("MODULE") or "").strip() or name.endswith(BANK2_SUFFIX):
            continue
        twin = dict(meta, ARG=name + BANK2_SUFFIX, TARGET="0x%02X" % DME_SLAVE)
        cylinder = engine_cylinder(name + BANK2_SUFFIX)
        for key in ("INFO", "INFO_EN"):
            if twin.get(key):
                text = twin[key]
                if cylinder:
                    text, n = _CYLINDER_WORD.subn(
                        lambda m: "%s%s%d" % (m.group(1), m.group(2), cylinder), text)
                    if not n:
                        text += " (cylinder %d)" % cylinder
                twin[key] = "Bank 2: " + text
        cat[name + BANK2_SUFFIX] = twin
    return cat


def bank_twin(name):
    """NAME@0x13 for a bank-1 DME channel name; None for one already bank 2."""
    return None if name.endswith(BANK2_SUFFIX) else name + BANK2_SUFFIX


def bank_pairs(names):
    """[(bank 1, bank 2)] for each channel in `names` whose twin is there too."""
    present = set(names)
    return [(n, n + BANK2_SUFFIX) for n in names
            if not n.endswith(BANK2_SUFFIX) and n + BANK2_SUFFIX in present]


# --------------------------------------------------------- calculated channels

# Values worked out from others each cycle, not read from the car: the
# difference between the banks, how far each cylinder runs from the rest.
# name -> (English, German, unit, inputs, function of the input values).
CALC_LABEL = "Calculated"
CYLINDERS = 8


def _difference(a, b):
    return a - b


def _from_mean(i):
    def f(*values):
        return values[i] - sum(values) / len(values)
    return f


DERIVED = {
    "CALC_LAMBDA_BANK1_MINUS_BANK2": (
        "Lambda, bank 1 minus bank 2", "Lambda, Bank 1 minus Bank 2", "-",
        ("LAMBDA_SONDE_IST", "LAMBDA_SONDE_IST" + BANK2_SUFFIX), _difference),
    "CALC_LAMBDA_TRIM_BANK1_MINUS_BANK2": (
        "Lambda control factor, bank 1 minus bank 2",
        "Lambdaregelfaktor, Bank 1 minus Bank 2", "-",
        ("LAMBD_REGELFAKTOR_MITTEL_SCHNELL", "LAMBD_REGELFAKTOR_MITTEL_SCHNELL" + BANK2_SUFFIX),
        _difference),
    "CALC_BOOST_MINUS_TARGET": (
        "Boost pressure minus its target", "Ladedruck minus Sollwert", "hPa",
        ("IPLAD", "S_LADEDRUCK"), _difference),
    "CALC_GPS_SPEED_MINUS_VEHICLE_SPEED": (
        "GPS speed minus vehicle speed", "GPS-Geschwindigkeit minus Fahrzeuggeschwindigkeit",
        "km/h", ("HU_STATUS_SPEED.GPS_SPEED", "FZG_GESCHWINDIGKEIT_VA_HA_FASCHWERP"),
        _difference),
}
for _i in range(CYLINDERS):
    DERIVED["CALC_ILUZ%d_FROM_MEAN" % (_i + 1)] = (
        "Uneven running cylinder %d minus the mean of all %d" % (_i + 1, CYLINDERS),
        "Laufunruhe Zylinder %d minus Mittelwert aller %d" % (_i + 1, CYLINDERS), "rad/s²",
        tuple("ILUZ%d" % (k + 1) for k in range(CYLINDERS)), _from_mean(_i))


def add_derived(cat):
    """Add the calculated channels whose inputs are all in the catalog."""
    for name, (en, de, unit, inputs, _f) in DERIVED.items():
        if all(n in cat for n in inputs):
            cat[name] = {"ARG": name, "ID": "0x0000", "INFO": de, "INFO_EN": en,
                         "EINHEIT": unit, "DATENTYP": "", "MUL": "1", "DIV": "1",
                         "ADD": "0", "DERIVED": "1"}
    return cat


def is_derived(meta):
    return bool(meta.get("DERIVED"))


def with_inputs(names):
    """`names` with each calculated channel's inputs put before it, once."""
    out, seen = [], set()
    for name in names:
        for n in tuple(DERIVED[name][3] if name in DERIVED else ()) + (name,):
            if n not in seen:
                seen.add(n)
                out.append(n)
    return out


def derive(name, sample):
    """(value, unit, note) of a calculated channel from this cycle's sample."""
    _en, _de, unit, inputs, f = DERIVED[name]
    values = [sample.get(n, (None,))[0] for n in inputs]
    missing = [n for n, v in zip(inputs, values) if not isinstance(v, (int, float))]
    if missing:
        return (None, "", "needs %s" % missing[0])
    return (round(f(*values), 9), unit, "")   # without float noise like ...0000004


# ---------------------------------------------------------- built-in channel sets

# Fuel: each DME's fuel mass flow (kg/h, its own bank's injectors) and the
# tank level (l). History integrates the flows for a drive's fuel economy.
FUEL_FLOWS = ("MASSENSTROM_KRAFTSTOFF", "MASSENSTROM_KRAFTSTOFF" + BANK2_SUFFIX)
FUEL_TANK = "IKTFS"
FUEL_CHANNELS = FUEL_FLOWS + (FUEL_TANK,)

BUILTIN_TEMPLATES = {
    "Misfire hunt": ["INMOT", "ZAEHLER_AUSSETZ_ABGASREL_SUM_ALLEZYL"]
    + ["ILUZ%d" % (i + 1) for i in range(CYLINDERS)]
    + ["ZAEHLER_AUSSETZERK_ZYL%d" % (i + 1) for i in range(CYLINDERS)]
    + ["CALC_ILUZ%d_FROM_MEAN" % (i + 1) for i in range(CYLINDERS)]
    # Spark burn time and knock level, cylinders 1-4 from the master and 5-8
    # from the slave (its own 1-4).
    + ["DIAG_BRENNDAUER_ZUENDFUNKE_CYL%d%s" % (i % 4 + 1, "" if i < 4 else BANK2_SUFFIX)
       for i in range(CYLINDERS)]
    + ["IUKZ%d%s" % (i % 4 + 1, "" if i < 4 else BANK2_SUFFIX) for i in range(CYLINDERS)],
    "Boost and wastegates, both banks": [
        "INMOT", "IPLAD", "S_LADEDRUCK", "CALC_BOOST_MINUS_TARGET"]
    + [n + s for n in ("LADEDRUCK_REGELDIFFERENZ_BANKSPEZ", "I_EWG_POS", "TV_EWG")
       for s in ("", BANK2_SUFFIX)],
    "Lambda and trims, both banks": [
        n + s for n in ("LAMBDA_SONDE_IST", "LAMBDA_SONDE_SOLL",
                        "LAMBD_REGELFAKTOR_MITTEL_SCHNELL") for s in ("", BANK2_SUFFIX)]
    + ["CALC_LAMBDA_BANK1_MINUS_BANK2", "CALC_LAMBDA_TRIM_BANK1_MINUS_BANK2"],
    "Temperatures": ["ITKUM", "ITKUA", "ITOEL", "TEMP_OEL_SUMPF", "TEMP_ANSLUFT1_FILT",
                     "TEMP_KRAFTSTOFF", "TEMP_GETRIEBE", "EGS_GETRIEBEOELTMEPERATUR"],
    "Drive log with GPS": [
        "INMOT", "BUS_FAHRPEDALWERT", "FZG_GESCHWINDIGKEIT_VA_HA_FASCHWERP", "EGS_ISTGANG.ISTGANG",
        "IPLAD", "S_LADEDRUCK", "ITOEL", "ITKUM", "HU_STATUS_GPS",
        "HU_STATUS_GPS_POSITION.LATITUDE", "HU_STATUS_GPS_POSITION.LONGITUDE",
        "HU_STATUS_GPS_POSITION.HEIGHT", "HU_STATUS_SPEED.GPS_SPEED",
        "CALC_GPS_SPEED_MINUS_VEHICLE_SPEED"] + list(FUEL_CHANNELS),
    # History works out each drive's fuel economy from these.
    "Fuel economy": ["INMOT", "FZG_GESCHWINDIGKEIT_VA_HA_FASCHWERP", "BUS_FAHRPEDALWERT"]
    + list(FUEL_CHANNELS),
}


# ------------------------------------------------------------------- alerts

def alert_state(value, limits):
    """"low", "high" or None for a value against (low, high) limits, either
    of which may be None."""
    if not isinstance(value, (int, float)) or not limits:
        return None
    lo, hi = limits
    if lo is not None and value < lo:
        return "low"
    if hi is not None and value > hi:
        return "high"
    return None


# ---------------------------------------------------------------- log layout

# Columns a log has besides the channels: Time first (as BimmerLink has it,
# seconds from the log's start), the wall clock next, and events last.
LOG_TIME, LOG_CLOCK, LOG_EVENT = "Time", "Clock", "Event"
LOG_META = (LOG_TIME, LOG_CLOCK, LOG_EVENT)


def log_header(names):
    return [LOG_TIME, LOG_CLOCK] + list(names) + [LOG_EVENT]


def log_clock(now=None):
    """The PC's time of day with its offset from UTC, to the millisecond:
    2026-10-19 07:42:50.123-05:00. The offset makes logs from different
    time zones, or either side of a daylight-saving change, line up."""
    now = time.time() if now is None else now
    t = time.localtime(now)
    off = (t.tm_gmtoff or 0) // 60
    return "%s.%03d%s%02d:%02d" % (time.strftime("%Y-%m-%d %H:%M:%S", t),
                                   int(now * 1000) % 1000, "-" if off < 0 else "+",
                                   abs(off) // 60, abs(off) % 60)


_CLOCK = re.compile(r"^(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d):(\d\d)(\.\d+)?"
                    r"(?:([+-])(\d\d):?(\d\d)|Z)?$")


def clock_epoch(text):
    """Seconds since 1970 (UTC) of a log's Clock. Without an offset (logs
    before 0.6.2) it is taken as this PC's local time then. None if it
    isn't a clock time."""
    m = _CLOCK.match((text or "").strip())
    if not m:
        return None
    y, mo, d, hh, mm, ss = (int(g) for g in m.groups()[:6])
    frac = float(m.group(7) or 0)
    if m.group(8):
        sign = -1 if m.group(8) == "-" else 1
        off = sign * (int(m.group(9)) * 3600 + int(m.group(10)) * 60)
        return calendar.timegm((y, mo, d, hh, mm, ss, 0, 0, 0)) - off + frac
    if text.strip().endswith("Z"):
        return calendar.timegm((y, mo, d, hh, mm, ss, 0, 0, 0)) + frac
    try:
        return time.mktime((y, mo, d, hh, mm, ss, 0, 0, -1)) + frac
    except (OverflowError, ValueError):
        return None


def log_row(seconds, now, names, sample, event=""):
    return ([round(seconds, 3), log_clock(now)]
            + [log_cell(sample[n][0]) for n in names] + [log_cell(event)])


def channel_target(meta, default):
    """The module a channel is read from."""
    for key in ("MODULE", "TARGET"):
        if (meta.get(key) or "").strip():
            return int(meta[key], 16)
    return default


MULTI_MAX = 8                   # identifiers per request when batching


def split_reply(body, sizes):
    """{identifier: data} from a multi-identifier 0x22 reply, given each
    asked identifier's size. A module leaves out ones it doesn't support;
    anything else that doesn't line up exactly - an echo that wasn't asked
    for, data running short, bytes left over - raises ValueError, so a
    wrong size can never shift one value into the next."""
    out, i = {}, 0
    while i < len(body):
        if i + 2 > len(body):
            raise ValueError("stray byte at the end of a multi-identifier reply")
        did = int.from_bytes(body[i:i + 2], "big")
        if did not in sizes or did in out:
            raise ValueError("reply has 0x%04X where an asked identifier should be" % did)
        end = i + 2 + sizes[did]
        if end > len(body):
            raise ValueError("reply too short for 0x%04X" % did)
        out[did] = body[i + 2:end]
        i = end
    return out


def value_bytes(meta):
    """Bytes a DME value's reply takes, if the catalog says exactly; else None."""
    size, exact = expected_size([meta])
    return size if exact and size else None


def rest(interval, started, now=None):
    """Seconds to wait so cycles start `interval` apart: the time the cycle
    took counts towards it, so the rate is the one asked for."""
    now = time.time() if now is None else now
    return max(0.0, interval - (now - started))


class ChannelReader:
    """Reads a watch list from the DME and other modules, one request per DID.

    DME channels (no MODULE) are read from `target` every cycle, as before.
    Other modules' channels are records: one read fills every field of that
    identifier. They are refreshed on FAST_PERIOD / SLOW_PERIOD, at most
    SLOW_READS_PER_CYCLE per cycle, stalest first, and the last values are
    reused in between, so every sample and log row is complete.
    """

    def __init__(self, layouts, target, batch=False):
        self.layouts, self.target = layouts, target
        # Batching: DME values read up to MULTI_MAX to a request. Off unless
        # asked for; any sign a DME won't do it turns it off for that DME.
        self.batch = batch
        self.batch_off = {}     # DME address -> why batching stopped there
        self.batched = 0        # values that came in batches
        self.cache = {}         # (module, did) -> {ARG: (value, unit, note)}
        self.last = {}          # (module, did) -> time of last read
        self.refused = {}       # (module, did) -> reason; not asked again
        self.quiet_until = {}   # module -> time; it did not answer
        self.heard = set()      # modules that answered at least once
        self.value_quiet = {}   # DME (target, did) or target -> time; no answer

    @staticmethod
    def period(key):
        return FAST_PERIOD if key in FAST_DIDS else SLOW_PERIOD

    def read(self, h, chans, now=None):
        now = time.time() if now is None else now
        sample, records = {}, {}
        answered, silent = set(), set()     # DME addresses, this cycle
        derived = []
        got = self._prefetch(h, chans, now) if self.batch else {}
        for name, did, meta in chans:
            module = (meta.get("MODULE") or "").strip()
            if is_derived(meta):
                derived.append(name)        # worked out below, not read
            elif meta.get("LOGONLY"):
                sample[name] = (None, "", "only in a log")   # another tool's column
            elif module:
                records.setdefault((int(module, 16), did), []).append(name)
            else:
                sample[name] = self._read_value(h, did, meta, now, answered, silent, got)

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
        for name in derived:
            sample[name] = derive(name, sample)
        return sample

    def _prefetch(self, h, chans, now):
        """Read the DME values several to a request. {(target, did): data}
        for those that came; the rest are read one at a time as usual."""
        sizes = {}
        for _name, did, meta in chans:
            if (meta.get("MODULE") or "").strip() or is_derived(meta):
                continue
            target = channel_target(meta, self.target)
            key = (target, did)
            if (target in self.batch_off or key in self.refused
                    or now < self.value_quiet.get(target, 0)
                    or now < self.value_quiet.get(key, 0)):
                continue
            size = value_bytes(meta)
            known = sizes.setdefault(target, {})
            # Two channels on one identifier with different sizes: can't
            # tell where it ends, so that one is read on its own.
            known[did] = size if known.get(did, size) == size else None
        got = {}
        for target, by_did in sizes.items():
            dids = [d for d, s in by_did.items() if s]
            for k in range(0, len(dids), MULTI_MAX):
                chunk = dids[k:k + MULTI_MAX]
                if len(chunk) < 2:
                    continue
                try:
                    parts = split_reply(h.read_dids(target, chunk, timeout=VALUE_TIMEOUT),
                                        {d: by_did[d] for d in chunk})
                except (ConnectionLost, ConnectionError):
                    raise
                except (HsfzError, ValueError) as e:
                    self.batch_off[target] = str(e)[:80]
                    break
                for did, raw in parts.items():
                    got[(target, did)] = raw
                self.batched += len(parts)
        return got

    def _read_value(self, h, did, meta, now, answered, silent, got=None):
        target = channel_target(meta, self.target)
        key = (target, did)
        if key in self.refused:
            return (None, "", self.refused[key])
        if got and key in got:
            answered.add(target)
            try:
                value, unit = decode(got[key], meta)
            except Exception as e:                                  # noqa: BLE001
                return (None, "", str(e)[:28])
            return (value, unit, "")
        if (target in silent or now < self.value_quiet.get(target, 0)
                or now < self.value_quiet.get(key, 0)):
            return (None, "", "not answering")
        try:
            value, unit = decode(h.read_did(target, did, timeout=VALUE_TIMEOUT), meta)
        except NegativeResponse as e:
            answered.add(target)
            reason = NRC.get(e.code, "0x%02X" % e.code)
            if e.code in REFUSED_FOR_GOOD:
                self.refused[key] = reason
            return (None, "", reason)
        except HsfzTimeout:
            silent.add(target)
            self.value_quiet[key] = now + VALUE_KEY_BACKOFF
            if target not in answered:      # nothing from it at all: rest it too
                self.value_quiet[target] = now + VALUE_TARGET_BACKOFF
            return (None, "", "not answering")
        except (ConnectionLost, ConnectionError):
            raise                           # the connection, not this value: reconnect
        except Exception as e:                                      # noqa: BLE001
            return (None, "", str(e)[:28])
        answered.add(target)
        return (value, unit, "")

    def _read_record(self, h, key, now):
        module, did = key
        self.last[key] = now
        try:
            raw = h.read_did(module, did, timeout=MODULE_TIMEOUT)
        except NegativeResponse as e:
            self.heard.add(module)                  # a refusal is an answer
            reason = NRC.get(e.code, "0x%02X" % e.code)
            if e.code in (0x11, 0x12, 0x31):
                self.refused[key] = reason          # will never work: stop asking
            self.cache[key] = {f["ARG"]: (None, "", reason) for f in self.layouts[key]}
            return
        except HsfzTimeout:
            self.quiet_until[module] = now + NO_ANSWER_BACKOFF
            self.cache.pop(key, None)
            return
        self.heard.add(module)
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
        if not (row.get("MODULE") or "").strip() and not is_derived(row):
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
        raw = h.read_did(module, did, timeout=CHECK_TIMEOUT)
    except NegativeResponse as e:
        return "refused", NRC.get(e.code, "0x%02X" % e.code), b""
    except HsfzTimeout:
        return "no answer", "", b""
    except ConnectionLost:
        raise
    except HsfzError as e:            # a garbled reply: note it, carry on
        return "no answer", str(e), b""
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


# ------------------------------------------------------------- logs folder

LOGS_FOLDER = "BMW Analyzer Logs"


def documents_dir():
    """The user's Documents folder, where Windows really keeps it (OneDrive
    can redirect it), else ~/Documents."""
    if sys.platform == "win32":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(1024)
            # CSIDL_PERSONAL (5): Documents, following any redirection.
            if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0 \
                    and buf.value:
                return buf.value
        except Exception as e:                                      # noqa: BLE001
            _note_problem("documents folder", e)
    return os.path.join(os.path.expanduser("~"), "Documents")


def _note_problem(where, error):
    """Record a failure that is worked around, so it can be found later."""
    from n63read import diag                    # diag imports core
    diag.record("problem", where=where, error="%s: %s" % (type(error).__name__, error))


def logs_dir(create=True):
    """Documents\\BMW Analyzer Logs: where drive logs, fault lists and reports
    go by default. Created on first use."""
    path = os.path.join(documents_dir(), LOGS_FOLDER)
    if create:
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as e:
            # Saving there will fail and say so; this says why, beforehand.
            _note_problem("logs folder %s" % path, e)
    return path


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
    replace_file(tmp, path)


# ---------------------------------------------------------------- templates

def templates_path():
    return os.path.join(os.path.dirname(availability_path()), "templates.json")


def load_templates(path=None):
    """Saved channel sets: {name: [channel, ...]}; {} if none."""
    try:
        with open(path or templates_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): [str(c) for c in v] for k, v in data.items() if isinstance(v, list)}


def save_template(name, channels, path=None):
    """Save (or replace) one template. Returns all of them."""
    name = (name or "").strip()
    if not name:
        raise ValueError("a template needs a name")
    templates = load_templates(path)
    templates[name] = list(channels)
    save_settings(templates, path or templates_path())
    return templates


def delete_template(name, path=None):
    templates = load_templates(path)
    if templates.pop(name, None) is not None:
        save_settings(templates, path or templates_path())
    return templates


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
    replace_file(tmp, path)


# A check's results, as `n63read check --out` and the app's Check car now
# write them; history.kind() knows the file by its first three columns, and
# the cloud sync uploads it like a drive log.
CHECK_HEADER = ["module", "did", "job", "status", "detail", "reply_bytes", "raw"]


def write_check_csv(rows, path):
    """Save [(module, DID, job, status)] in the check CSV layout."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CHECK_HEADER)
        for module, did, job, status in rows:
            w.writerow(["0x%02X" % module, "0x%04X" % did, job, status, "", "", ""])


def check_answers(lines):
    """{(module, DID): status} from a check CSV's lines (any iterable of text)."""
    out = {}
    for row in csv.DictReader(lines):
        try:
            key = (int(row["module"], 16), int(row["did"], 16))
        except (KeyError, TypeError, ValueError):
            continue
        out[key] = row.get("status") or ""
    if not out:
        raise ValueError("no module/did/status rows - is this a `n63read check` CSV?")
    return out


def import_check_csv(path):
    """{(module, DID): status} from a `n63read check --out` CSV."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        return check_answers(f)


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
