import hashlib
import http.server
import json
import os
import threading

import pytest

from n63read import update

SETUP = b"MZ installer " * 1000


class FakeGitHub:
    """A local HTTP server standing in for api.github.com and the downloads."""

    def __init__(self, tag="v9.9.9", payload=SETUP, digest=True, sums=False, corrupt=False):
        sha = hashlib.sha256(payload).hexdigest()
        files = {"/dl/" + update.SETUP: payload if not corrupt else payload[:-1] + b"X"}
        if sums:
            files["/dl/SHA256SUMS.txt"] = ("%s  %s\n" % (sha, update.SETUP)).encode()
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
        for name in (update.SETUP, "SHA256SUMS.txt"):
            path = "/dl/" + name
            if path in files:
                asset = {"name": name, "size": len(payload) if name == update.SETUP
                         else len(files[path]), "browser_download_url": base + path}
                if digest and name == update.SETUP:
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
def exe(tmp_path, monkeypatch):
    """An installed copy: the uninstall entry points at its folder."""
    folder = tmp_path / "BMW Analyzer"
    folder.mkdir()
    path = folder / "n63read.exe"
    path.write_bytes(b"MZ old version")
    monkeypatch.setattr(update, "install_location", lambda: str(folder) + "\\")
    return str(path)


@pytest.fixture
def started(monkeypatch):
    """What install() would have run, instead of running it."""
    runs = []
    monkeypatch.setattr(update.subprocess, "Popen", lambda args, **k: runs.append(args))
    return runs


# ------------------------------------------------------------------ versions

@pytest.mark.parametrize("latest, current, expect", [
    ("v0.3.3", "0.3.2", True), ("0.4.0", "0.3.9", True), ("v0.3.10", "0.3.9", True),
    ("v0.3.2", "0.3.2", False), ("v0.3.1", "0.3.2", False), ("nightly", "0.3.2", False),
])
def test_newer(latest, current, expect):
    assert update.newer(latest, current) is expect


# ----------------------------------------------------------- the whole flow

def test_update_downloads_and_verifies_the_installer(github, exe):
    github()
    info = update.update(exe)
    assert info["latest"] == "9.9.9"
    assert info["setup"] == os.path.join(update.updates_dir(), "BMW-Analyzer-Setup-9.9.9.exe")
    assert open(info["setup"], "rb").read() == SETUP
    assert open(exe, "rb").read() == b"MZ old version"     # the installer replaces it
    assert not os.path.exists(info["setup"] + ".part")


def test_install_runs_the_installer_silently(started, tmp_path):
    update.install("setup.exe", relaunch=True)
    args = started[-1]
    assert args[0] == "setup.exe" and "/VERYSILENT" in args and "/CURRENTUSER" in args
    assert "/RELAUNCH=1" in args
    update.install("setup.exe")
    assert "/RELAUNCH=1" not in started[-1]


def test_a_corrupt_download_is_discarded(github, exe):
    github(corrupt=True)
    with pytest.raises(update.UpdateError, match="verification"):
        update.update(exe)
    assert os.listdir(update.updates_dir()) == []


def test_checksum_from_sha256sums_when_no_digest(github, exe):
    github(digest=False, sums=True)
    assert update.update(exe)["setup"]


def test_no_checksum_no_update(github, exe):
    github(digest=False, sums=False)
    with pytest.raises(update.UpdateError, match="no checksum"):
        update.update(exe)


def test_not_newer_does_nothing(github, exe):
    github(tag="v" + update.__version__)
    assert update.update(exe)["setup"] is None


def test_a_portable_copy_is_only_told(github, exe, monkeypatch):
    github()
    monkeypatch.setattr(update, "install_location", lambda: r"C:\elsewhere")
    info = update.update(exe)
    assert info["newer"] and info["setup"] is None


def test_installed_compares_folders(monkeypatch):
    monkeypatch.setattr(update, "install_location", lambda: r"C:\Apps\BMW Analyzer\\")
    assert update.installed(r"C:\apps\bmw analyzer\n63read.exe") == (os.name == "nt")
    assert not update.installed(r"C:\Downloads\n63read.exe")
    monkeypatch.setattr(update, "install_location", lambda: None)
    assert not update.installed(r"C:\Apps\BMW Analyzer\n63read.exe")


def test_tidy_keeps_only_an_installer_still_to_run(exe):
    folder = update.updates_dir()
    os.makedirs(folder)
    for name in ("BMW-Analyzer-Setup-9.9.9.exe", "BMW-Analyzer-Setup-%s.exe" % update.__version__,
                 "BMW-Analyzer-Setup-0.0.1.exe.part"):
        open(os.path.join(folder, name), "wb").close()
    for suffix in (".old", ".previous", ".new"):          # left by one-file builds
        open(exe + suffix, "wb").close()
    update.tidy(exe)
    assert os.listdir(folder) == ["BMW-Analyzer-Setup-9.9.9.exe"]
    assert os.listdir(os.path.dirname(exe)) == ["n63read.exe"]


def test_offline_is_an_update_error(monkeypatch):
    monkeypatch.setattr(update, "API", "http://127.0.0.1:9/nothing")
    monkeypatch.setattr(update, "latest_release", _real_latest_release)
    with pytest.raises(update.UpdateError, match="could not reach GitHub"):
        update.check()


def test_from_source_nothing_is_replaced():
    assert update.running_exe() is None and not update.installed()
    with pytest.raises(update.UpdateError, match="source"):
        update.update()


# --------------------------------------------------------------------- GUI

gui = pytest.importorskip("n63read.gui")


def test_footer_shows_the_version(app):
    assert app.version_btn.cget("text").startswith("n63read %s" % update.__version__)


def test_footer_shows_a_ready_update(app, monkeypatch, started):
    app.q.put(("update", "ready", "9.9.9", "https://example/rel", False, "setup.exe"))
    app._drain()
    assert "9.9.9 ready" in app.version_btn.cget("text")
    assert app.version_menu.entrycget("Restart to update", "state") == "normal"
    closed = []
    monkeypatch.setattr(app, "_close", lambda: closed.append(1))
    app._restart_to_update()
    assert started[-1][0] == "setup.exe" and "/RELAUNCH=1" in started[-1] and closed
    assert app._update_setup is None                # not run again on the way out
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


# ------------------------------------------- other n63read processes running

REAL_OTHERS_RUNNING = update.others_running     # before conftest stubs it per test


def test_other_processes_in_the_app_folder_are_found(monkeypatch, tmp_path):
    real = REAL_OTHERS_RUNNING
    app_dir = str(tmp_path / "BMW Analyzer")
    procs = {101: os.path.join(app_dir, "n63read-cli.exe"),
             102: os.path.join(app_dir, "n63read.exe"),
             103: os.path.join(str(tmp_path), "elsewhere", "n63read.exe"),
             os.getpid(): os.path.join(app_dir, "n63read.exe")}       # ourselves
    monkeypatch.setattr(update, "_process_paths", lambda: procs)
    assert real(app_dir) == [(101, "n63read-cli.exe"), (102, "n63read.exe")]
    assert real(str(tmp_path / "empty")) == []


def test_restart_to_update_closes_the_others_first(app, monkeypatch, started):
    app._update_setup, app._update_ready = "setup.exe", "9.9.9"
    monkeypatch.setattr(update, "others_running", lambda folder=None: [(4242, "n63read-cli.exe")])
    stopped, closed = [], []
    monkeypatch.setattr(update, "stop", lambda pids: stopped.extend(pids))
    monkeypatch.setattr(app, "_close", lambda: closed.append(1))
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: False)
    app._restart_to_update()
    assert not started and not stopped and not closed      # declined: nothing happens
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: True)
    app._restart_to_update()
    assert stopped == [4242] and started[-1][0] == "setup.exe" and closed
    app._update_ready = None


def test_closing_waits_while_others_run(app, monkeypatch, started):
    app._update_setup = "setup.exe"
    monkeypatch.setattr(update, "others_running", lambda folder=None: [(7, "n63read-cli.exe")])
    assert app._install_on_exit() is False and not started
    monkeypatch.setattr(update, "others_running", lambda folder=None: [])
    assert app._install_on_exit() is True and started[-1][0] == "setup.exe"
    app._update_setup = None


def test_cli_update_waits_while_others_run(monkeypatch, capsys):
    from n63read import cli
    monkeypatch.setattr(update, "installed", lambda exe_path=None: True)
    monkeypatch.setattr(update, "update", lambda progress=None: {
        "setup": "setup.exe", "current": "0.3.7", "latest": "9.9.9", "newer": True})
    monkeypatch.setattr(update, "others_running", lambda folder=None: [(9, "n63read-cli.exe")])
    ran = []
    monkeypatch.setattr(update, "install", lambda setup, relaunch=False: ran.append(setup))
    with pytest.raises(SystemExit, match="close these first"):
        cli.main(["update"])
    assert not ran
