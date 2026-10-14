"""
n63read.update - keep the exe up to date from the project's GitHub releases.

check() asks GitHub for the latest release; a newer one is downloaded next to
the running exe, its SHA-256 checked against GitHub's published digest (or
the release's SHA256SUMS.txt), and swapped in by renaming: Windows lets a
running exe be renamed though not overwritten. The old one keeps running
until it is closed; the next start runs the new version and keeps the one
before as <exe>.previous, for rolling back by hand.

Only the running exe's own asset (n63read.exe or n63read-cli.exe) from this
repository, only a strictly newer version, only with a matching digest.
Running from source (not frozen), nothing is replaced.
"""

import hashlib
import json
import os
import re
import sys
import urllib.request

from n63read import __version__

REPO = "strulock/m550-analyzer"
# The installer's AppId (installer/BMW-Analyzer.iss); its per-user uninstall
# entry is where Settings > Apps reads the version from.
APP_ID = "{15CE8A75-71CE-4317-BF3E-1730307815CD}"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\%s_is1" % APP_ID
API = "https://api.github.com/repos/%s/releases/latest" % REPO
RELEASES_PAGE = "https://github.com/%s/releases" % REPO
CHECK_EVERY = 6 * 3600          # seconds between checks while open
MAX_SIZE = 100 * 1024 * 1024    # refuse anything absurd
TIMEOUT = 20


class UpdateError(Exception):
    pass


def parse_version(text):
    """'v0.3.10' -> (0, 3, 10); None if it isn't a version."""
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", (text or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def newer(latest, current=__version__):
    a, b = parse_version(latest), parse_version(current)
    return bool(a and b and a > b)


def running_exe():
    """Path of the running exe when frozen by PyInstaller, else None."""
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    return None


def _get(url, accept="application/vnd.github+json"):
    req = urllib.request.Request(url, headers={
        "Accept": accept, "User-Agent": "n63read/%s" % __version__})
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def latest_release():
    """The latest release's JSON from the GitHub API."""
    try:
        with _get(API) as r:
            return json.loads(r.read().decode("utf-8"))
    except (OSError, ValueError) as e:
        raise UpdateError("could not reach GitHub: %s" % e) from None


def pick_asset(release, name):
    """The asset called `name` (n63read.exe / n63read-cli.exe), or None."""
    for asset in release.get("assets") or []:
        if asset.get("name") == name:
            return asset
    return None


def expected_sha256(release, asset):
    """The asset's SHA-256: GitHub's digest, else the release's SHA256SUMS.txt."""
    digest = (asset.get("digest") or "").lower()
    if digest.startswith("sha256:") and len(digest) == 71:
        return digest[7:]
    sums = pick_asset(release, "SHA256SUMS.txt")
    if sums:
        try:
            with _get(sums["browser_download_url"], accept="application/octet-stream") as r:
                text = r.read(64 * 1024).decode("utf-8", "replace")
        except OSError as e:
            raise UpdateError("could not fetch SHA256SUMS.txt: %s" % e) from None
        for line in text.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("*") == asset["name"]:
                return parts[0].lower()
    raise UpdateError("the release publishes no checksum for %s" % asset["name"])


def check(exe_name=None):
    """{"current", "latest", "newer", "asset", "release"}; raises UpdateError."""
    release = latest_release()
    tag = release.get("tag_name") or ""
    name = exe_name or os.path.basename(running_exe() or "n63read.exe")
    return {"current": __version__, "latest": tag.lstrip("v"), "newer": newer(tag),
            "asset": pick_asset(release, name), "release": release,
            "url": release.get("html_url") or RELEASES_PAGE}


def download(info, exe_path, progress=None):
    """Download the new exe to <exe>.new and verify it. Returns that path."""
    asset = info.get("asset")
    if not asset:
        raise UpdateError("release %s has no %s" % (info.get("latest"), os.path.basename(exe_path)))
    size = int(asset.get("size") or 0)
    if not 0 < size <= MAX_SIZE:
        raise UpdateError("unexpected size %d" % size)
    want = expected_sha256(info["release"], asset)
    new = exe_path + ".new"
    h, got = hashlib.sha256(), 0
    try:
        with _get(asset["browser_download_url"], accept="application/octet-stream") as r, \
                open(new, "wb") as f:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                got += len(chunk)
                if got > MAX_SIZE:
                    raise UpdateError("download larger than expected")
                h.update(chunk)
                f.write(chunk)
                if progress:
                    progress(got, size)
    except (OSError, UpdateError) as e:
        _remove(new)
        raise UpdateError("download failed: %s" % e) from None
    if got != size or h.hexdigest() != want:
        _remove(new)
        raise UpdateError("downloaded file failed verification (size %d of %d, sha256 %s, "
                          "expected %s); nothing was changed" % (got, size, h.hexdigest(), want))
    return new


def install(new, exe_path):
    """Swap the verified download in: running exe -> .old, .new -> exe."""
    old = exe_path + ".old"
    _remove(old)
    try:
        os.replace(exe_path, old)           # allowed while running
    except OSError as e:
        _remove(new)
        raise UpdateError("cannot replace %s: %s" % (exe_path, e)) from None
    try:
        os.replace(new, exe_path)
    except OSError as e:
        os.replace(old, exe_path)           # put it back
        raise UpdateError("cannot install the update: %s" % e) from None
    return exe_path


def update(exe_path=None, progress=None):
    """Check and, if newer, download, verify and install. Returns the info
    dict with "installed" set; raises UpdateError on any failure."""
    exe_path = exe_path or running_exe()
    if not exe_path:
        raise UpdateError("running from source; nothing to update")
    info = check(os.path.basename(exe_path))
    info["installed"] = False
    if info["newer"]:
        install(download(info, exe_path, progress), exe_path)
        info["installed"] = True
    return info


def tidy(exe_path=None):
    """At start: after an update, keep the version before as .previous and
    drop leftovers. Returns the .previous path if one was made."""
    exe_path = exe_path or running_exe()
    if not exe_path:
        return None
    _remove(exe_path + ".new")
    old = exe_path + ".old"
    if os.path.exists(old):
        previous = exe_path + ".previous"
        try:
            os.replace(old, previous)
            return previous
        except OSError:
            pass                            # still running somewhere; next time
    return None


def set_installed_version(version=__version__):
    """After a self-update, show the new version in Settings > Apps.

    Only touches the installer's own per-user entry, and only if it exists
    (a copy run from Downloads has none). Returns True if it was set.
    """
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY, 0,
                            winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, version)
        return True
    except OSError:
        return False


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass
