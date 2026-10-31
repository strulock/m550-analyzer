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

The build never holds logger_secrets.h (LOGGER_NO_SECRETS): the board keeps
its Wi-Fi, Worker and token in its flash, set once with provision.py, so the
image in R2 has no secrets and a PC without the file can build one.

Publishing uses an Access service token: CF_ACCESS_CLIENT_ID and
CF_ACCESS_CLIENT_SECRET, else the n63read app's (Settings > Cloud), never the
board's own: the Worker takes firmware only from a person's login or a token
in its FIRMWARE_PUBLISHERS (the app's), and the board's token only does the
board's work, so one taken from a board can't put code on it. The Worker is --host, else
N63_WORKER_HOST, else the app's, else WORKER_HOST in logger_secrets.h. Test
builds: --flag OTA_TEST_SILENT (never checks in, so the board must go back
to the build before), --version to pick the name. Standard library and the
app's own settings code only.
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
    env["PLATFORMIO_BUILD_FLAGS"] = " ".join(['-DFW_VERSION=\\"%s\\"' % v, "-DLOGGER_NO_SECRETS"] +
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


def app_cloud():
    """(url, client id, client secret) from the n63read app's settings on this
    PC, or empty strings."""
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from n63read import cloud, core
        return cloud.config(core.load_settings())
    except (ImportError, OSError, ValueError):      # no app or no settings here
        return "", "", ""


def token():
    cid, secret = os.environ.get("CF_ACCESS_CLIENT_ID"), os.environ.get("CF_ACCESS_CLIENT_SECRET")
    if cid and secret:
        return cid, secret
    _, cid, secret = app_cloud()
    if cid and secret:
        return cid, secret
    sys.exit("no Access token: the app's (Settings > Cloud), or set CF_ACCESS_CLIENT_ID and "
             "CF_ACCESS_CLIENT_SECRET (not the board's: the Worker refuses it)")


def worker_host():
    if os.environ.get("N63_WORKER_HOST"):
        return os.environ["N63_WORKER_HOST"]
    url = app_cloud()[0]
    if url:
        return re.sub(r"^https?://", "", url).split("/")[0]
    text = SECRETS.read_text(encoding="utf-8") if SECRETS.exists() else ""
    m = re.search(r'^#define WORKER_HOST "([^"]+)"', text, re.M)
    return m.group(1) if m else None


def publish(v, host):
    data = IMAGE.read_bytes()
    cid, secret = token()
    req = urllib.request.Request(
        "https://%s/api/logger/firmware?version=%s" % (host, v), data=data, method="PUT",
        # Cloudflare turns away urllib's own User-Agent (error 1010).
        headers={"CF-Access-Client-Id": cid, "CF-Access-Client-Secret": secret,
                 "Content-Type": "application/octet-stream",
                 "User-Agent": "n63read-publish-firmware/1"})
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
    ap.add_argument("--host", help="the Worker's host (default: N63_WORKER_HOST, else the "
                    "app's, else WORKER_HOST in logger_secrets.h)")
    a = ap.parse_args()
    v = a.version or version(a.allow_dirty)
    if not re.fullmatch(r"[\w.-]{1,40}", v) or v == "dev":
        sys.exit("version: letters, digits, dot, dash or underscore, not \"dev\"")
    host = a.host or worker_host()
    if not a.usb and not host:
        sys.exit("no Worker host: --host, N63_WORKER_HOST, or WORKER_HOST in logger_secrets.h")
    build(v, a.flag, a.usb)
    if not a.usb:
        publish(v, host)


if __name__ == "__main__":
    main()
