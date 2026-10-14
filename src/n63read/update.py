r"""
n63read.update - keep BMW Analyzer up to date from the project's GitHub releases.

The app is a folder (n63read.exe, n63read-cli.exe, _internal\) put there by
the installer, so an update is the next release's installer run silently:
check() asks GitHub for the latest release; download() fetches its
BMW-Analyzer-Setup.exe to %LOCALAPPDATA%\n63read\updates and checks its
SHA-256 against GitHub's published digest (or the release's SHA256SUMS.txt);
install() starts it and the app closes, so the installer can replace the
files. Inno Setup keeps Settings > Apps and the Start menu in step.

Only a strictly newer version, only with a matching digest, and only for an
installed copy: a portable copy (the zip) or one running from source is
told a newer version exists, nothing more.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request

from n63read import __version__

REPO = "strulock/m550-analyzer"
# The installer's AppId (installer/BMW-Analyzer.iss); its per-user uninstall
# entry says where it installed the app.
APP_ID = "{15CE8A75-71CE-4317-BF3E-1730307815CD}"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\%s_is1" % APP_ID
SETUP = "BMW-Analyzer-Setup.exe"
API = "https://api.github.com/repos/%s/releases/latest" % REPO
RELEASES_PAGE = "https://github.com/%s/releases" % REPO
CHECK_EVERY = 6 * 3600          # seconds between checks while open
MAX_SIZE = 200 * 1024 * 1024    # refuse anything absurd
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


def install_location():
    """The folder the installer put the app in, or None."""
    if sys.platform != "win32":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
            return winreg.QueryValueEx(key, "InstallLocation")[0] or None
    except OSError:
        return None


def installed(exe_path=None):
    """True if this exe is the installer's copy, which can update itself."""
    exe_path = exe_path or running_exe()
    where = install_location()
    if not exe_path or not where:
        return False
    norm = lambda p: os.path.normcase(os.path.normpath(p))    # noqa: E731
    return norm(os.path.dirname(exe_path)) == norm(where)


def updates_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "n63read", "updates")


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
    """The asset called `name`, or None."""
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


def check():
    """{"current", "latest", "newer", "asset", "release", "url"}; raises UpdateError."""
    release = latest_release()
    tag = release.get("tag_name") or ""
    return {"current": __version__, "latest": tag.lstrip("v"), "newer": newer(tag),
            "asset": pick_asset(release, SETUP), "release": release,
            "url": release.get("html_url") or RELEASES_PAGE}


def download(info, folder=None, progress=None):
    """Download the release's installer and verify it. Returns its path."""
    asset = info.get("asset")
    if not asset:
        raise UpdateError("release %s has no %s" % (info.get("latest"), SETUP))
    size = int(asset.get("size") or 0)
    if not 0 < size <= MAX_SIZE:
        raise UpdateError("unexpected size %d" % size)
    want = expected_sha256(info["release"], asset)
    folder = folder or updates_dir()
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "BMW-Analyzer-Setup-%s.exe" % info["latest"])
    part = path + ".part"
    h, got = hashlib.sha256(), 0
    try:
        with _get(asset["browser_download_url"], accept="application/octet-stream") as r, \
                open(part, "wb") as f:
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
        _remove(part)
        raise UpdateError("download failed: %s" % e) from None
    if got != size or h.hexdigest() != want:
        _remove(part)
        raise UpdateError("downloaded file failed verification (size %d of %d, sha256 %s, "
                          "expected %s); nothing was changed" % (got, size, h.hexdigest(), want))
    os.replace(part, path)
    return path


def update(exe_path=None, progress=None):
    """Check and, for an installed copy with a newer release, download and
    verify its installer. Returns the info dict with "setup" (the verified
    installer's path, or None); raises UpdateError on any failure."""
    exe_path = exe_path or running_exe()
    if not exe_path:
        raise UpdateError("running from source; nothing to update")
    info = check()
    info["setup"] = None
    if info["newer"] and installed(exe_path):
        info["setup"] = download(info, progress=progress)
    return info


def install(setup, relaunch=False):
    """Run the verified installer silently, detached. The caller then exits
    so its files can be replaced; with relaunch the installer starts the
    app again when it's done."""
    args = [setup, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
            "/CURRENTUSER", "/CLOSEAPPLICATIONS"]
    if relaunch:
        args.append("/RELAUNCH=1")
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(args, close_fds=True, creationflags=flags)
    except OSError as e:
        raise UpdateError("cannot start the installer: %s" % e) from None


def tidy(exe_path=None):
    """At start: drop installers already used (not one still to run), and
    what the one-file builds' self-update left next to the exe. Running
    from source, there's nothing of ours to tidy."""
    exe_path = exe_path or running_exe()
    if not exe_path:
        return
    for suffix in (".new", ".old", ".previous"):
        _remove(exe_path + suffix)
    folder = updates_dir()
    try:
        names = os.listdir(folder)
    except OSError:
        names = []
    for name in names:
        m = re.fullmatch(r"BMW-Analyzer-Setup-(.+)\.exe", name)
        if not (m and newer(m.group(1))):
            _remove(os.path.join(folder, name))


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass
