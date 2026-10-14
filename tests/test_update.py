import hashlib
import http.server
import json
import os
import threading

import pytest

from n63read import update

NEW_EXE = b"MZ new version " * 1000


class FakeGitHub:
    """A local HTTP server standing in for api.github.com and the downloads."""

    def __init__(self, tag="v9.9.9", payload=NEW_EXE, digest=True, sums=False, corrupt=False):
        files = {"/dl/n63read.exe": payload if not corrupt else payload[:-1] + b"X",
                 "/dl/n63read-cli.exe": payload}
        sha = hashlib.sha256(payload).hexdigest()
        if sums:
            files["/dl/SHA256SUMS.txt"] = ("%s  n63read.exe\n%s  n63read-cli.exe\n"
                                           % (sha, sha)).encode()
        self.files = files
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/latest":
                    body = json.dumps(server.release).encode()
                elif self.path in server.files:
                    body = server.files[self.path]
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        assets = []
        for name in ("n63read.exe", "n63read-cli.exe", "SHA256SUMS.txt"):
            path = "/dl/" + name
            if path in files:
                asset = {"name": name, "size": len(payload) if name.endswith(".exe")
                         else len(files[path]), "browser_download_url": base + path}
                if digest and name.endswith(".exe"):
                    asset["digest"] = "sha256:" + sha
                assets.append(asset)
        self.release = {"tag_name": tag, "html_url": base + "/release", "assets": assets}
        self.api = base + "/latest"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def github(monkeypatch):
    servers = []

    def make(**kw):
        s = FakeGitHub(**kw)
        servers.append(s)
        monkeypatch.setattr(update, "API", s.api)
        # conftest blocks the network; let this module talk to the fake.
        monkeypatch.setattr(update, "latest_release", _real_latest_release)
        return s
    yield make
    for s in servers:
        s.close()


_real_latest_release = update.latest_release.__wrapped__ \
    if hasattr(update.latest_release, "__wrapped__") else update.latest_release


@pytest.fixture
def exe(tmp_path):
    path = tmp_path / "n63read.exe"
    path.write_bytes(b"MZ old version")
    return str(path)


# ------------------------------------------------------------------ versions

@pytest.mark.parametrize("latest, current, expect", [
    ("v0.3.3", "0.3.2", True), ("0.4.0", "0.3.9", True), ("v0.3.10", "0.3.9", True),
    ("v0.3.2", "0.3.2", False), ("v0.3.1", "0.3.2", False), ("nightly", "0.3.2", False),
])
def test_newer(latest, current, expect):
    assert update.newer(latest, current) is expect


# ----------------------------------------------------------- the whole flow

def test_update_downloads_verifies_and_swaps(github, exe):
    github()
    info = update.update(exe)
    assert info["installed"] and info["latest"] == "9.9.9"
    assert open(exe, "rb").read() == NEW_EXE
    assert open(exe + ".old", "rb").read() == b"MZ old version"     # the running one
    assert not os.path.exists(exe + ".new")
    assert update.tidy(exe) == exe + ".previous"                     # next start
    assert not os.path.exists(exe + ".old")
    assert open(exe + ".previous", "rb").read() == b"MZ old version"


def test_a_corrupt_download_changes_nothing(github, exe):
    github(corrupt=True)
    with pytest.raises(update.UpdateError, match="verification"):
        update.update(exe)
    assert open(exe, "rb").read() == b"MZ old version"
    assert not os.path.exists(exe + ".new") and not os.path.exists(exe + ".old")


def test_checksum_from_sha256sums_when_no_digest(github, exe):
    github(digest=False, sums=True)
    assert update.update(exe)["installed"]


def test_no_checksum_no_update(github, exe):
    github(digest=False, sums=False)
    with pytest.raises(update.UpdateError, match="no checksum"):
        update.update(exe)
    assert open(exe, "rb").read() == b"MZ old version"


def test_not_newer_does_nothing(github, exe):
    github(tag="v" + update.__version__)
    info = update.update(exe)
    assert not info["installed"] and open(exe, "rb").read() == b"MZ old version"


def test_cli_exe_gets_the_cli_asset(github, tmp_path):
    github()
    cli = tmp_path / "n63read-cli.exe"
    cli.write_bytes(b"old")
    assert update.check("n63read-cli.exe")["asset"]["name"] == "n63read-cli.exe"


def test_offline_is_an_update_error(monkeypatch):
    monkeypatch.setattr(update, "API", "http://127.0.0.1:9/nothing")
    monkeypatch.setattr(update, "latest_release", _real_latest_release)
    with pytest.raises(update.UpdateError, match="could not reach GitHub"):
        update.check()


def test_from_source_nothing_is_replaced():
    assert update.running_exe() is None
    assert update.tidy() is None
    with pytest.raises(update.UpdateError, match="source"):
        update.update()


# --------------------------------------------------------------------- GUI

gui = pytest.importorskip("n63read.gui")


def test_footer_shows_the_version(app):
    assert app.version_btn.cget("text").startswith("n63read %s" % update.__version__)


def test_footer_shows_a_ready_update(app, monkeypatch):
    app.q.put(("update", "installed", "9.9.9", "https://example/rel", False))
    app._drain()
    assert "9.9.9 ready" in app.version_btn.cget("text")
    assert app.version_menu.entrycget("Restart to update", "state") == "normal"
    app._update_ready = None
    app.version_menu.entryconfig("Restart to update", state="disabled")


def test_manual_check_says_up_to_date(app, monkeypatch):
    shown = []
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda t, m, **k: shown.append(m))
    app.q.put(("update", "current", update.__version__, None, True))
    app._drain()
    assert "latest version" in shown[-1]


def test_no_update_check_while_connected(app, monkeypatch):
    started = []
    monkeypatch.setattr(app, "_start_update", lambda manual: started.append(manual))
    app.autoupdate_var.set(True)
    app.poller = object()                           # "connected"
    try:
        app._update_tick()
    finally:
        app.poller = None
        app.autoupdate_var.set(False)
    assert started == []
