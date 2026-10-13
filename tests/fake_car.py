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
}
# (address, DID) -> raw value
DIDS = {(0x12, 0x588A): b"\x10\x00", (0x12, 0x5889): b"\x0F\xA0"}


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
        self._stop = False
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self._stop = True
        self.srv.close()

    def _accept(self):
        while not self._stop:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
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
                    conn.sendall(_frame(CTRL_ACK, src, dst, uds))
                    reply = self._answer(dst, uds)
                    if reply is not None:
                        conn.sendall(_frame(CTRL_DIAG, dst, src, reply))

    def _answer(self, addr, uds):
        if addr not in MODULES:
            return None
        service = uds[0]
        if service == 0x3E:
            return b"\x7E\x00"
        if service == 0x19:
            faults = MODULES[addr]
            if isinstance(faults, int):
                return bytes([0x7F, 0x19, faults])
            body = b"".join(bytes.fromhex(c) + bytes([s]) for c, s in faults)
            return b"\x59\x02\xFF" + body
        if service == 0x22:
            did = (uds[1] << 8) | uds[2]
            raw = DIDS.get((addr, did))
            if raw is None:
                return bytes([0x7F, 0x22, 0x31])
            return b"\x62" + uds[1:3] + raw
        return bytes([0x7F, service, 0x11])
