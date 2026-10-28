"""A stand-in for the M550i's ENET gateway on the bench, over a real cable.

Run it on a PC whose Ethernet port is cabled straight to the logger board
(no router: Windows then gives the port a 169.254.x.y address by itself):

    python firmware/tools/bench_car.py

It answers HSFZ discovery on UDP 6811 with a FAKE VIN, and UDS reads on TCP
6801 for the spike's seven channels, from the master DME (0x12) and the slave
(0x13), single or several identifiers to a request. Engine speed swings
slowly so you can see live values change. Every request is echoed as an
acknowledge frame first, as the real gateway does. The fault list (0x19 02)
answers with no faults. Anything but 0x22, 0x3E and 0x19 02 is refused (NRC
0x11) and printed, which on the bench means the firmware sent something it
never should.

For the logger, pass its channel file and every identifier in it answers too,
on any module, with bytes of the right size (a slowly changing pattern, not
real values):

    python firmware/tools/bench_car.py --channels channels.csv

To act like the car did on a drive: --delay 30 (the DMEs' answer time),
--slow 0x63=800 (a module that takes most of a second), --bad 0x19:0x1234
(an identifier that makes the module refuse the whole request with 0x31),
--single 0x18 (a module that refuses requests for several identifiers with
0x13). Each can be given more than once.

To try the drive's events: --park 90 (from 90 s, the engine off and the
selector in P; the transmission, 0x18, answers its gear identifier 0xDA2E
either way), --drive 200 (the engine running and in D again from then),
--close 5 (the first connection closed 5 s after its first request, as the
gateway did at the start of each drive on 27 Oct), --quiet 0x12=30-34 (a
module that doesn't answer from 30 s to 34 s).

Windows asks whether Python may use the network: allow it on Public networks
too, because a direct cable is an "unidentified" (public) network.
Standard library only; Ctrl+C to stop.
"""

import argparse
import csv
import math
import socket
import struct
import threading
import time

CTRL_DIAG, CTRL_ACK, CTRL_IDENT = 0x0001, 0x0002, 0x0011
FAKE_VIN = "WBAFAKEBENCH00001"
IDENT = ("DIAGADR10BMWMAC0012BENCHCARBMWVIN" + FAKE_VIN).encode()
T0 = time.time()
DMES = (0x12, 0x13)
EXTRA = {}                  # (module, identifier) -> reply size, from --channels
EXTRA_MODULES = set()
DELAY = 0.0                 # s before each answer (--delay): the car's DMEs take ~30 ms
SLOW = {}                   # module -> s before its answers (--slow)
BAD = set()                 # (module, identifier) refused with the whole request (--bad)
SINGLE = set()              # modules that take one identifier a request (--single)
PARK_AT = DRIVE_AT = None   # s from the start: the engine off in P, then running in D
CLOSE_AT = None             # s into the first connection: closed once (--close)
QUIET = {}                  # module -> (from, to) s it doesn't answer (--quiet)
GEAR = (0x18, 0xDA2E)       # the transmission's ISTGANG record: gear, then the selector


def parked():
    s = time.time() - T0
    return PARK_AT is not None and s >= PARK_AT and (DRIVE_AT is None or s < DRIVE_AT)


def u16(v):
    return int(round(v)).to_bytes(2, "big", signed=v < 0)


def values(dme):
    """Raw values per identifier, in the catalog's scaling, for 0x12 or 0x13."""
    rpm = 0 if parked() else 720 + 60 * math.sin((time.time() - T0) / 3)   # gentle idle hunt
    bank2 = dme == 0x13
    return {
        0x4807: u16(rpm * 2),                                  # 1/min, /2
        0x4A21: u16(((91.5 if bank2 else 90.0) + 273.14) / 0.1),  # C
        0x460C: u16(12.62 / 0.023481),                         # V
        0x4402: u16((96 + 48) / 0.75),                         # C
        0x4AB1: u16(0),                                        # km/h
        0x4201: u16(1003 * 256 / 10),                          # hPa
        0x5545: u16(0),                                        # %
    }


def frame(control, src, dst, payload):
    return struct.pack(">IH", len(payload) + 2, control) + bytes([src, dst]) + payload


def load_channels(path):
    """Answer every identifier in a logger channel file (n63read logger-channels)."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            key = (int(row["module"], 16), int(row["did"], 16))
            EXTRA[key] = int(row["did_size"]) if row["did_size"] else 8
            EXTRA_MODULES.add(key[0])


def filler(module, did, size):
    """Bytes for an identifier from the channel file: changing a few times a
    second, so a log shows movement."""
    t = int((time.time() - T0) * 4)
    return bytes((did + module * 7 + i * 13 + t) & 0x7F for i in range(size))


def answer(dme, uds):
    if dme not in DMES and dme not in EXTRA_MODULES and dme != GEAR[0]:
        return None                                  # silent, like an empty address
    quiet = QUIET.get(dme)
    if quiet and quiet[0] <= time.time() - T0 < quiet[1]:
        return None
    service = uds[0]
    if service == 0x3E:
        return b"\x7E" + uds[1:2]
    if service == 0x19 and uds[1:2] == b"\x02":
        return b"\x59\x02\xFF"                       # status mask, no faults
    if service != 0x22:
        print("  !! refused service 0x%02X: the firmware should never send this" % service)
        return bytes([0x7F, service, 0x11])
    table = values(dme) if dme in DMES else {}
    dids = [int.from_bytes(uds[i:i + 2], "big") for i in range(1, len(uds) - 1, 2)]
    if dme in SINGLE and len(dids) > 1:
        return bytes([0x7F, 0x22, 0x13])             # incorrectMessageLengthOrInvalidFormat
    if any((dme, did) in BAD for did in dids):
        return bytes([0x7F, 0x22, 0x31])             # one it hasn't spoils the request
    out = b""
    for did in dids:
        if (dme, did) == GEAR:
            # P, or the 5th gear in D
            out += did.to_bytes(2, "big") + (b"\x00\x00" if parked() else b"\x07\x03")
        elif did in table:
            out += did.to_bytes(2, "big") + table[did]
        elif (dme, did) in EXTRA:
            out += did.to_bytes(2, "big") + filler(dme, did, EXTRA[(dme, did)])
    if not out:
        return bytes([0x7F, 0x22, 0x31])             # requestOutOfRange
    return b"\x62" + out


def send_later(conn, lock, data):
    try:
        with lock:
            conn.sendall(data)
    except OSError:
        pass


CLOSED_ONCE = []


def serve(conn, peer):
    print("[tcp] %s connected" % peer[0])
    count = 0
    first_at = None
    buf = b""
    lock = threading.Lock()
    with conn:
        while True:
            try:
                chunk = conn.recv(4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while len(buf) >= 6:
                length, control = struct.unpack(">IH", buf[:6])
                if len(buf) < 6 + length:
                    break
                body, buf = buf[6:6 + length], buf[6 + length:]
                src, dst, uds = body[0], body[1], body[2:]
                if count == 0:
                    print("[tcp] first request: tester 0x%02X -> 0x%02X" % (src, dst))
                    first_at = time.time()
                count += 1
                if CLOSE_AT is not None and not CLOSED_ONCE and time.time() - first_at >= CLOSE_AT:
                    CLOSED_ONCE.append(True)
                    print("[tcp] closing the connection (--close)")
                    conn.close()
                    return
                reply = answer(dst, uds)
                try:
                    with lock:
                        conn.sendall(frame(CTRL_ACK, src, dst, uds))
                except OSError:
                    break
                if reply is None:
                    continue
                wait = SLOW.get(dst, DELAY)
                if wait:
                    # Each module answers after its own delay, as on the car,
                    # where requests to different modules overlap.
                    threading.Timer(wait, send_later, (conn, lock, frame(CTRL_DIAG, dst, src,
                                                                         reply))).start()
                    continue
                try:
                    with lock:
                        conn.sendall(frame(CTRL_DIAG, dst, src, reply))
                except OSError:
                    break
    print("[tcp] %s closed after %d requests" % (peer[0], count))


def tcp_loop():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", 6801))
    srv.listen(4)
    while True:
        conn, peer = srv.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        threading.Thread(target=serve, args=(conn, peer), daemon=True).start()


def udp_loop():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", 6811))
    reply = struct.pack(">IH", len(IDENT), CTRL_IDENT) + IDENT
    while True:
        data, peer = s.recvfrom(1024)
        if len(data) >= 6 and data[4:6] == b"\x00\x11":
            print("[udp] discovery probe from %s, answering" % peer[0])
            s.sendto(reply, peer)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="a stand-in for the car's ENET gateway")
    ap.add_argument("--channels", metavar="CSV",
                    help="also answer every identifier in this logger channel file")
    ap.add_argument("--delay", type=float, default=0.0, metavar="MS",
                    help="wait this long before each answer, as the car does (~30)")
    ap.add_argument("--slow", action="append", default=[], metavar="MODULE=MS",
                    help="this module answers after MS instead (e.g. 0x63=800)")
    ap.add_argument("--bad", action="append", default=[], metavar="MODULE:DID",
                    help="a request naming this identifier is refused with 0x31")
    ap.add_argument("--single", action="append", default=[], metavar="MODULE",
                    help="this module refuses several identifiers a request (0x13)")
    ap.add_argument("--park", type=float, metavar="S",
                    help="from S seconds, the engine off and the selector in P")
    ap.add_argument("--drive", type=float, metavar="S",
                    help="from S seconds, the engine running in D again")
    ap.add_argument("--close", type=float, metavar="S",
                    help="close the first connection S seconds after its first request")
    ap.add_argument("--quiet", action="append", default=[], metavar="MODULE=FROM-TO",
                    help="this module doesn't answer from FROM to TO seconds (e.g. 0x12=30-34)")
    args = ap.parse_args()
    PARK_AT, DRIVE_AT, CLOSE_AT = args.park, args.drive, args.close
    for item in args.quiet:
        module, span = item.split("=")
        a, b = span.split("-")
        QUIET[int(module, 16)] = (float(a), float(b))
    DELAY = args.delay / 1000.0
    for item in args.slow:
        module, ms = item.split("=")
        SLOW[int(module, 16)] = float(ms) / 1000.0
    for item in args.bad:
        module, did = item.split(":")
        BAD.add((int(module, 16), int(did, 16)))
    SINGLE.update(int(m, 16) for m in args.single)
    if args.channels:
        load_channels(args.channels)
        print("answering %d identifiers from %s too" % (len(EXTRA), args.channels))
    threading.Thread(target=udp_loop, daemon=True).start()
    threading.Thread(target=tcp_loop, daemon=True).start()
    print("bench car up: UDP 6811 + TCP 6801, VIN %s. Ctrl+C to stop." % FAKE_VIN)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
