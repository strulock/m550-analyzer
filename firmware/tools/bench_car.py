"""A stand-in for the M550i's ENET gateway on the bench, over a real cable.

Run it on a PC whose Ethernet port is cabled straight to the logger board
(no router: Windows then gives the port a 169.254.x.y address by itself):

    python firmware/tools/bench_car.py

It answers HSFZ discovery on UDP 6811 with a FAKE VIN, and UDS reads on TCP
6801 for the spike's seven channels, from the master DME (0x12) and the slave
(0x13), single or several identifiers to a request. Engine speed swings
slowly so you can see live values change. Every request is echoed as an
acknowledge frame first, as the real gateway does. Anything but 0x22 and
0x3E is refused (NRC 0x11) and printed, which on the bench means the
firmware sent something it never should.

Windows asks whether Python may use the network: allow it on Public networks
too, because a direct cable is an "unidentified" (public) network.
Standard library only; Ctrl+C to stop.
"""

import math
import socket
import struct
import threading
import time

CTRL_DIAG, CTRL_ACK, CTRL_IDENT = 0x0001, 0x0002, 0x0011
FAKE_VIN = "WBAFAKEBENCH00001"
IDENT = ("DIAGADR10BMWMAC0012BENCHCARBMWVIN" + FAKE_VIN).encode()
T0 = time.time()


def u16(v):
    return int(round(v)).to_bytes(2, "big", signed=v < 0)


def values(dme):
    """Raw values per identifier, in the catalog's scaling, for 0x12 or 0x13."""
    rpm = 720 + 60 * math.sin((time.time() - T0) / 3)       # gentle idle hunt
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


def answer(dme, uds):
    if dme not in (0x12, 0x13):
        return None                                  # silent, like an empty address
    service = uds[0]
    if service == 0x3E:
        return b"\x7E" + uds[1:2]
    if service != 0x22:
        print("  !! refused service 0x%02X: the firmware should never send this" % service)
        return bytes([0x7F, service, 0x11])
    table = values(dme)
    dids = [int.from_bytes(uds[i:i + 2], "big") for i in range(1, len(uds) - 1, 2)]
    out = b""
    for did in dids:
        if did in table:
            out += did.to_bytes(2, "big") + table[did]
    if not out:
        return bytes([0x7F, 0x22, 0x31])             # requestOutOfRange
    return b"\x62" + out


def serve(conn, peer):
    print("[tcp] %s connected" % peer[0])
    count = 0
    buf = b""
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
                count += 1
                reply = answer(dst, uds)
                try:
                    conn.sendall(frame(CTRL_ACK, src, dst, uds))
                    if reply is not None:
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
    threading.Thread(target=udp_loop, daemon=True).start()
    threading.Thread(target=tcp_loop, daemon=True).start()
    print("bench car up: UDP 6811 + TCP 6801, VIN %s. Ctrl+C to stop." % FAKE_VIN)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
