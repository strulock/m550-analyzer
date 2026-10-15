"""
n63read.share - one tester for every n63read process on this PC.

The car allows two diagnostic connections, and n63read's Tester uses both
(0x12 and every other module on one, bank 2's DME on the other). So whoever
holds them - the app while connected, or a running `n63read log` - is the
tester for everyone else: a Host takes requests from other n63read processes
(the CLI, Claude's tools) on a private localhost port and sends them to the
car between its own reads. Those processes find it with find() and get a
SharedTester, which behaves like a connected Tester.

The port and a random token are in a file only this user can read; a request
without the token is dropped. Requests go through the Tester's own request(),
so the read-only allow-list applies to them as to everything else.
"""

import binascii
import json
import os
import queue
import secrets
import socket
import threading
import time

from n63read import core

WAIT = 120.0                    # s a client waits for its answer
BUDGET = 2.0                    # s of shared requests per owner cycle, at most
LINGER = 0.05                   # s to wait for a client's next request


def state_path():
    return os.path.join(os.path.dirname(core.availability_path()), "tester.json")


class Host:
    """Serves the owner's Tester to other processes. Start it once connected,
    call serve(h) between reads, stop() when the connection ends."""

    def __init__(self, ip):
        self.ip = ip
        self.token = secrets.token_hex(16)
        self.requests = queue.Queue()
        self.sock = None
        self.served = 0

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        port = self.sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()
        path = state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"port": port, "token": self.token, "ip": self.ip,
                       "pid": os.getpid()}, f)
        os.replace(tmp, path)
        return self

    def stop(self):
        try:
            with open(state_path(), encoding="utf-8") as f:
                mine = json.load(f).get("token") == self.token
        except (OSError, ValueError):
            mine = False
        if mine:
            try:
                os.remove(state_path())
            except OSError:
                pass
        if self.sock:
            self.sock.close()
            self.sock = None
        while True:                                 # nobody waits for nothing
            try:
                _, reply = self.requests.get_nowait()
            except queue.Empty:
                break
            reply({"error": "closed", "message": "the tester disconnected"})

    def _accept(self):
        while self.sock:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn):
        with conn:
            f = conn.makefile("rwb")
            for line in f:
                try:
                    msg = json.loads(line)
                except ValueError:
                    return
                if not isinstance(msg, dict) or msg.get("token") != self.token:
                    return
                done = threading.Event()
                box = {}

                def reply(answer, box=box, done=done):
                    box["answer"] = answer
                    done.set()
                if msg.get("op") == "hello":
                    reply({"ok": "", "ip": self.ip})
                else:
                    self.requests.put((msg, reply))
                if not done.wait(WAIT):
                    return
                try:
                    f.write(json.dumps(box["answer"]).encode() + b"\n")
                    f.flush()
                except OSError:
                    return

    def serve(self, h, budget=BUDGET):
        """Answer waiting requests on the owner's thread, for up to `budget` s."""
        end = time.time() + budget
        served = False
        while time.time() < end:
            try:
                msg, reply = self.requests.get(timeout=LINGER if served else 0)
            except queue.Empty:
                return
            reply(self._answer(h, msg))
            served = True
            self.served += 1

    @staticmethod
    def _answer(h, msg):
        try:
            payload = binascii.unhexlify(msg["payload"])
            saved = h.timeout
            h._set_timeout(float(msg.get("timeout") or saved))
            try:
                resp = h.request(int(msg["target"]), payload, int(msg.get("retries", 4)))
            finally:
                h._set_timeout(saved)
            return {"ok": resp.hex()}
        except core.NegativeResponse as e:
            return {"error": "negative", "service": e.service, "code": e.code}
        except core.HsfzTimeout as e:
            return {"error": "timeout", "message": str(e)}
        except core.HsfzError as e:
            return {"error": "hsfz", "message": str(e)}
        except (ValueError, KeyError, TypeError) as e:
            return {"error": "refused", "message": str(e)}
        except OSError as e:
            return {"error": "closed", "message": str(e)}


class SharedTester(core.Hsfz):
    """A Tester borrowed from the process that holds the car's connections."""

    def __init__(self, port, token, ip, timeout=3.0):
        super().__init__(ip, timeout=timeout)
        self.host_port, self.token = port, token
        self.trace = None
        self.shared = True
        self._f = None

    def connect(self):
        self.sock = socket.create_connection(("127.0.0.1", self.host_port), timeout=5.0)
        self.sock.settimeout(WAIT + 10)
        self._f = self.sock.makefile("rwb")
        if self._ask({"op": "hello"}).get("ok") is None:
            raise ConnectionError("the shared tester did not answer")

    def close(self):
        if self._f:
            try:
                self._f.close()
            except OSError:
                pass
            self._f = None
        super().close()

    def _set_timeout(self, seconds):
        self.timeout = seconds                      # the car's wait, sent along

    def _ask(self, msg):
        msg = dict(msg, token=self.token)
        self._f.write(json.dumps(msg).encode() + b"\n")
        self._f.flush()
        line = self._f.readline()
        if not line:
            raise ConnectionError("the shared tester went away")
        return json.loads(line)

    def request(self, target, payload, retries=4):
        if payload[0] not in core.ALLOWED_SERVICES:
            raise ValueError("refusing service 0x%02X - this tool only sends 0x19/0x22/0x3E"
                             % payload[0])
        answer = self._ask({"target": target, "payload": payload.hex(),
                            "timeout": self.timeout, "retries": retries})
        if "ok" in answer:
            return binascii.unhexlify(answer["ok"])
        kind = answer.get("error")
        if kind == "negative":
            raise core.NegativeResponse(answer["service"], answer["code"])
        if kind == "timeout":
            raise core.HsfzTimeout(answer.get("message") or "timed out")
        if kind == "refused":
            raise ValueError(answer.get("message"))
        if kind == "closed":
            raise ConnectionError(answer.get("message") or "the tester disconnected")
        raise core.HsfzError(answer.get("message") or "shared tester error")


def find(ip=None):
    """A connected SharedTester if another process holds the car (for `ip`,
    if given), else None."""
    try:
        with open(state_path(), encoding="utf-8") as f:
            state = json.load(f)
        port, token, owner_ip = int(state["port"]), state["token"], state["ip"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if ip and ip != owner_ip:
        return None
    t = SharedTester(port, token, owner_ip)
    try:
        t.connect()
    except (OSError, ValueError):
        t.close()
        return None
    return t


def open_tester(ip, source=core.TESTER_ADDR, timeout=3.0, share=True, trace=None):
    """The car: borrowed from whoever holds it, else a new Tester of our own."""
    if share:
        t = find(ip)
        if t is not None:
            t.timeout = timeout
            return t
    t = core.Tester(ip, source=source, timeout=timeout, trace=trace)
    t.shared = False
    t.connect()
    return t
