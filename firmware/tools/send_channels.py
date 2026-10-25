"""Load a channel file into the logger over its Wi-Fi terminal, no SD card
shuffling:

    n63read logger-channels --out channels.csv
    python firmware/tools/send_channels.py 192.168.20.86 channels.csv

The board saves it as channels.csv on its card and uses it from the next
drive (a drive in progress ends there). Standard library only.
"""

import socket
import sys
import time


def drain(sock, seconds):
    end, out = time.time() + seconds, b""
    while time.time() < end:
        try:
            data = sock.recv(65536)
        except socket.timeout:
            continue
        if not data:
            break
        out += data
    return out.decode(errors="replace")


def main():
    if len(sys.argv) != 3:
        sys.exit("usage: send_channels.py <logger address> <channels.csv>")
    host, path = sys.argv[1], sys.argv[2]
    with open(path, "rb") as f:
        data = f.read().replace(b"\r\n", b"\n")
    if not data.endswith(b"\n"):
        data += b"\n"
    sock = socket.create_connection((host, 23), timeout=10)
    sock.settimeout(0.2)
    drain(sock, 1.5)                         # the replay and status on connecting
    sock.sendall(b"C")
    time.sleep(0.5)
    sock.settimeout(30)
    sock.sendall(data + b".\n")              # the lines, then one with only a dot
    sock.settimeout(0.2)
    got, end = "", time.time() + 60
    while time.time() < end and "lines saved" not in got and "card unchanged" not in got:
        got += drain(sock, 1.0)
    sock.close()
    answer = [line for line in got.splitlines() if line.startswith("[chan]")]
    print("\n".join(answer) or "no answer from the logger")
    sys.exit(0 if "lines saved" in got else 1)


if __name__ == "__main__":
    main()
