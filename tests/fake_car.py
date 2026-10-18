"""A stand-in for the car's ENET gateway, for tests. Speaks just enough HSFZ.

Every request is echoed back as an acknowledge frame, as the real gateway
does. Modules that exist answer; empty addresses stay silent, so the client
times out exactly as it would on the car.
"""

import socket
import struct
import threading

CTRL_DIAG, CTRL_ACK = 0x0001, 0x0002

# address -> fault list [(code, status)], or an int NRC to refuse 0x19 with
MODULES = {
    0x10: [],                                   # gateway: no faults
    0x12: [("021200", 0x05), ("ABCDEF", 0x08)], # DME: one known code, one not
    0x40: 0x11,                                 # BDC: refuses 0x19
    0x63: [],                                   # head unit (NBT Evo)
}
# What the DME stored with its first fault: one snapshot (odometer 54,321 km,
# vehicle time, fault class 1, power state "driving", then one of its own:
# filtered air mass, 466 kg/h) and the counters (occurred 3 times, healing 40).
SNAPSHOT = bytes.fromhex("0105" "1700" "00d431" "1701" "0001e240" "1731" "01"
                         "1750" "0a" "4abc" "1234")
EXTENDED = bytes.fromhex("0100" "0203" "0328")
# Head unit GPS position (0xD039): valid, 30 08' 04.56" N, 97 38' 09.24" W, 160 m.
# Sign and degrees are nine-bit two's complement, as the car sends them:
# 97 W is 0x19F (-97), i.e. 01 9F.
HU_GPS = bytes([0x00, 0x00, 30, 8, 4, 56, 0x01, 0x9F, 38, 9, 24, 0x00, 160])
# Head unit speeds (0xD030): wheels 1-4, combined, GPS (signed int), diff % (unsigned int)
HU_SPEED = b"".join(v.to_bytes(2, "big", signed=True) for v in (101, 102, 100, 101, 101, 98)) \
    + (3).to_bytes(2, "big")
# (address, DID) -> raw value
# 0x13 is the slave DME (bank 2): same identifiers, its own values. It only
# answers reads here, so sweeps over the test range still find 0x10 and 0x12.
DIDS = {(0x12, 0x588A): b"\x10\x00", (0x12, 0x5889): b"\x0F\xA0",
        (0x13, 0x588A): b"\x0F\x00",
        (0x63, 0xD039): HU_GPS, (0x63, 0xD030): HU_SPEED, (0x63, 0xD038): b"\x0C"}


def _frame(control, src, dst, payload):
    return struct.pack(">IH", len(payload) + 2, control) + bytes([src, dst]) + payload


class FakeCar:
    def __init__(self):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        self.services = []          # every UDS service byte received
        self.connections = 0        # connections accepted
        self.sources = []           # (tester address, target) of each request
        self._stop = False
        self.live = []              # open connections, for drop()
        self.multi = True           # answers several identifiers in one 0x22
        threading.Thread(target=self._accept, daemon=True).start()

    def drop(self):
        """Cut every open connection, as a cable bump does; new ones still work."""
        for conn in list(self.live):
            try:
                conn.shutdown(socket.SHUT_RDWR)
                conn.close()
            except OSError:
                pass

    def close(self):
        self._stop = True
        try:
            # Wakes the accept() thread; on Linux close() alone leaves the
            # port listening until it returns.
            self.srv.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.srv.close()

    def _accept(self):
        while not self._stop:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            self.connections += 1
            self.live.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        with conn:
            buf = b""
            while True:
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    return
                if not chunk:
                    return
                buf += chunk
                while len(buf) >= 6:
                    length, control = struct.unpack(">IH", buf[:6])
                    if len(buf) < 6 + length:
                        break
                    body, buf = buf[6:6 + length], buf[6 + length:]
                    src, dst, uds = body[0], body[1], body[2:]
                    self.services.append(uds[0])
                    self.sources.append((src, dst))
                    reply = self._answer(dst, uds)
                    try:
                        conn.sendall(_frame(CTRL_ACK, src, dst, uds))
                        if reply is not None:
                            conn.sendall(_frame(CTRL_DIAG, dst, src, reply))
                    except OSError:
                        return              # cut by drop() or the test


    def _answer(self, addr, uds):
        if addr not in MODULES:
            if uds[0] == 0x22 and any(a == addr for a, _ in DIDS):
                return self._read(addr, uds)
            return None
        service = uds[0]
        if service == 0x3E:
            return b"\x7E\x00"
        if service == 0x19:
            faults = MODULES[addr]
            if isinstance(faults, int):
                return bytes([0x7F, 0x19, faults])
            if uds[1] in (0x04, 0x06):
                # One fault's snapshot or counters: the DME's first fault has
                # both, the others none (requestOutOfRange, as a module says).
                code = uds[2:5].hex().upper()
                status = dict(faults).get(code)
                if addr != 0x12 or status is None or code != faults[0][0]:
                    return b"\x7F\x19\x31"
                head = bytes([0x59, uds[1]]) + uds[2:5] + bytes([status])
                return head + (SNAPSHOT if uds[1] == 0x04 else EXTENDED)
            body = b"".join(bytes.fromhex(c) + bytes([s]) for c, s in faults)
            return b"\x59\x02\xFF" + body
        if service == 0x22:
            return self._read(addr, uds)
        return bytes([0x7F, service, 0x11])

    def _read(self, addr, uds):
        dids = [(uds[i] << 8) | uds[i + 1] for i in range(1, len(uds) - 1, 2)]
        if len(dids) > 1 and not self.multi:
            return bytes([0x7F, 0x22, 0x13])        # one identifier per request only
        out = b""
        for did in dids:
            raw = DIDS.get((addr, did))
            if raw is not None:                     # unsupported ones are left out
                out += did.to_bytes(2, "big") + raw
        return b"\x62" + out if out else bytes([0x7F, 0x22, 0x31])
