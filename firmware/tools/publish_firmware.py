"""Build the logger firmware with a version and publish it for the board to
take over the air, or flash it by USB:

    python firmware/tools/publish_firmware.py                 # build, publish
    python firmware/tools/publish_firmware.py --usb COM4      # build, flash by USB

The version is the UTC time and the commit, e.g. 20261027-0215-706ddb6; a
build from uncommitted changes is refused unless --allow-dirty (it then
ends "-dirty"). The board checks the Worker every 10 minutes, downloads a
version it hasn't got to its card, and installs it at the next power-on (or
after a minute idle). A build flashed from the IDE is "dev" and takes no
updates, so flash once with --usb to put the board on the published track.

Publishing uses an Access service token: CF_ACCESS_CLIENT_ID and
CF_ACCESS_CLIENT_SECRET, else the board's own from logger_secrets.h. Test
builds: --flag OTA_TEST_SILENT (never checks in, so the board must go back
to the build before), --version to pick the name. Standard library only.
"""

import argparse
import datetime
import os
import pathlib
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
PROJECT = ROOT / "firmware" / "logger"
IMAGE = PROJECT / ".pio" / "build" / "waveshare-s3-eth" / "firmware.bin"
SECRETS = PROJECT / "src" / "logger_secrets.h"


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                          check=True).stdout.strip()


def version(allow_dirty):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M")
    v = "%s-%s" % (stamp, git("rev-parse", "--short=7", "HEAD"))
    if git("status", "--porcelain", "--", "firmware"):
        if not allow_dirty:
            sys.exit("firmware/ has uncommitted changes: commit them, or --allow-dirty")
        v += "-dirty"
    return v


def pio():
    found = shutil.which("pio")
    if found:
        return found
    home = pathlib.Path.home() / ".platformio" / "penv"
    for p in (home / "Scripts" / "pio.exe", home / "bin" / "pio"):
        if p.exists():
            return str(p)
    sys.exit("PlatformIO's pio not found")


def build(v, flags, port):
    env = dict(os.environ)
    env["PLATFORMIO_BUILD_FLAGS"] = " ".join(['-DFW_VERSION=\\"%s\\"' % v] +
                                             ["-D" + f for f in flags])
    # PlatformIO can't print esptool's progress bar to a redirected Windows
    # console and the upload then hangs; UTF-8 output avoids it.
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    cmd = [pio(), "run", "-d", str(PROJECT)]
    if port:
        cmd += ["-t", "upload", "--upload-port", port]
    print("building %s%s" % (v, " and flashing " + port if port else ""), flush=True)
    if subprocess.run(cmd, env=env).returncode:
        sys.exit("build failed")


def token():
    cid, secret = os.environ.get("CF_ACCESS_CLIENT_ID"), os.environ.get("CF_ACCESS_CLIENT_SECRET")
    if cid and secret:
        return cid, secret
    text = SECRETS.read_text(encoding="utf-8") if SECRETS.exists() else ""
    found = dict(re.findall(r'^#define (ACCESS_CLIENT_ID|ACCESS_CLIENT_SECRET) "([^"]+)"', text,
                            re.M))
    if len(found) != 2:
        sys.exit("no Access token: set CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET")
    return found["ACCESS_CLIENT_ID"], found["ACCESS_CLIENT_SECRET"]


def worker_host():
    text = SECRETS.read_text(encoding="utf-8") if SECRETS.exists() else ""
    m = re.search(r'^#define WORKER_HOST "([^"]+)"', text, re.M)
    return m.group(1) if m else None


def publish(v, host):
    data = IMAGE.read_bytes()
    cid, secret = token()
    req = urllib.request.Request(
        "https://%s/api/logger/firmware?version=%s" % (host, v), data=data, method="PUT",
        headers={"CF-Access-Client-Id": cid, "CF-Access-Client-Secret": secret,
                 "Content-Type": "application/octet-stream"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            reply = r.read().decode()
    except urllib.error.HTTPError as e:
        sys.exit("publish failed: %d %s" % (e.code, e.read().decode(errors="replace")[:300]))
    print("published %s (%d bytes): %s" % (v, len(data), reply))
    print("the board takes it within 10 minutes on Wi-Fi, installs it at the next power-on")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--usb", metavar="PORT", help="flash by USB instead of publishing")
    ap.add_argument("--version", help="the version's name (default: UTC time and commit)")
    ap.add_argument("--flag", action="append", default=[], metavar="NAME[=VALUE]",
                    help="an extra define for a test build, e.g. OTA_TEST_SILENT")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="build uncommitted firmware changes")
    ap.add_argument("--host", help="the Worker's host (default: WORKER_HOST in logger_secrets.h)")
    a = ap.parse_args()
    v = a.version or version(a.allow_dirty)
    if not re.fullmatch(r"[\w.-]{1,40}", v) or v == "dev":
        sys.exit("version: letters, digits, dot, dash or underscore, not \"dev\"")
    host = a.host or worker_host()
    if not a.usb and not host:
        sys.exit("no Worker host: --host, or WORKER_HOST in logger_secrets.h")
    build(v, a.flag, a.usb)
    if not a.usb:
        publish(v, host)


if __name__ == "__main__":
    main()
