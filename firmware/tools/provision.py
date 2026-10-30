"""Set up the logger board over USB: its Wi-Fi networks, the setup access
point's password, the Worker and the board's Access token, and the time
zone, which it keeps in its flash (firmware/lib/logger/settings.h):

    python firmware/tools/provision.py --usb COM4             # from logger_secrets.h
    python firmware/tools/provision.py --usb COM4 --secrets other.h
    python firmware/tools/provision.py --usb COM4 --set wifi2_ssid=Phone --set wifi2_pass=...

Once a board, or after its flash is erased; firmware updates keep them.
The board saves them and restarts. Values are sent only over USB and never
printed here. Needs pyserial (PlatformIO's Python has it).
"""

import argparse
import pathlib
import re
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
SECRETS = ROOT / "firmware" / "logger" / "src" / "logger_secrets.h"

# logger_secrets.h's names for the board's settings keys.
DEFINES = {"WIFI_SSID": "wifi_ssid", "WIFI_PASS": "wifi_pass", "WIFI_SSID2": "wifi2_ssid",
           "WIFI_PASS2": "wifi2_pass", "AP_PASS": "ap_pass", "WORKER_HOST": "host",
           "ACCESS_CLIENT_ID": "access_id", "ACCESS_CLIENT_SECRET": "access_secret",
           "TZ_INFO": "tz"}


def from_secrets(path):
    text = pathlib.Path(path).read_text(encoding="utf-8")
    found = dict(re.findall(r'^#define (\w+) "([^"\n]*)"', text, re.M))
    return {DEFINES[k]: v for k, v in found.items() if k in DEFINES}


def send(port, settings):
    import serial                                   # PlatformIO's Python has pyserial

    s = serial.Serial(port, 115200, timeout=0.2)
    time.sleep(4)                                   # opening the port can restart the board
    s.read(1 << 16)
    s.write(b"S")
    out = b""
    t0 = time.time()
    while b"send key=value" not in out:
        out += s.read(4096)
        if time.time() - t0 > 10:
            sys.exit("the board didn't take S: %s" % out.decode(errors="replace")[-300:])
    # A line at a time: all at once overran the board's USB input and it
    # never saw the dot.
    for k, v in settings.items():
        s.write(("%s=%s\n" % (k, v)).encode())
        s.flush()
        time.sleep(0.1)
    s.write(b".\n")
    out = b""
    t0 = time.time()
    while time.time() - t0 < 10:
        out += s.read(4096)
        m = re.search(rb"\[set\] [^\r\n]*", out)
        said = m.group(0) if m else b""
        if any(w in said for w in (b"saved", b"nothing", b"not usable", b"only over")):
            print(said.decode(errors="replace"))
            return b"saved" in said
    sys.exit("no answer from the board")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--usb", metavar="PORT", required=True, help="the board's serial port")
    ap.add_argument("--secrets", default=str(SECRETS),
                    help="a logger_secrets.h to take them from (default: the project's)")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="a setting by its key (wifi_ssid, wifi_pass, wifi2_ssid, wifi2_pass, "
                         "ap_pass, host, access_id, access_secret, tz); only these if given")
    a = ap.parse_args()
    if a.set:
        settings = dict(x.split("=", 1) for x in a.set)
    elif pathlib.Path(a.secrets).exists():
        settings = from_secrets(a.secrets)
    else:
        sys.exit("no %s: --secrets, or --set KEY=VALUE" % a.secrets)
    unknown = set(settings) - set(DEFINES.values())
    if unknown:
        sys.exit("unknown keys: %s" % ", ".join(sorted(unknown)))
    print("sending %s" % ", ".join(settings))
    sys.exit(0 if send(a.usb, settings) else 1)


if __name__ == "__main__":
    main()
