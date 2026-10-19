import gc
import queue
import time

import pytest

from n63read import core


@pytest.fixture(autouse=True)
def quick_diagnostics(tmp_path, monkeypatch):
    """Diagnostics write to a temp journal, and the Windows snapshot (a ~10 s
    PowerShell call) is a stub: tests stay fast and never touch %APPDATA%."""
    from n63read import diag
    journal = str(tmp_path / "diagnostics")
    monkeypatch.setattr(diag, "journal_dir", lambda: journal)
    monkeypatch.setattr(diag, "windows_snapshot", lambda timeout=30: {"stub": True})
    monkeypatch.setattr(diag, "enabled", True)
    # Never reach the real GitHub from a test.
    from n63read import update

    def offline():
        raise update.UpdateError("no network in tests")
    monkeypatch.setattr(update, "latest_release", offline)
    updates = str(tmp_path / "updates")
    monkeypatch.setattr(update, "updates_dir", lambda: updates)
    # No other n63read is running from the app's folder, unless a test says so.
    monkeypatch.setattr(update, "others_running", lambda folder=None: [])
    # Each test has its own shared-tester file, so a poller never borrows
    # another test's connection, and nothing lands in the real %APPDATA%.
    from n63read import share
    tester = str(tmp_path / "tester.json")
    monkeypatch.setattr(share, "state_path", lambda: tester)
    templates = str(tmp_path / "templates.json")
    monkeypatch.setattr(core, "templates_path", lambda: templates)
    # Logs go under a temp "Documents", never the real one.
    docs = str(tmp_path / "Documents")
    monkeypatch.setattr(core, "documents_dir", lambda: docs)
    return journal


@pytest.fixture(scope="session")
def shared_app(tmp_path_factory):
    """One GUI window for every GUI test.

    Creating Tk roots back to back is flaky on some Windows Pythons ("Can't
    find a usable init.tcl"), so the whole session shares one. What the car
    answers is saved under a temp dir, never the real %APPDATA%.
    """
    gui = pytest.importorskip("n63read.gui")
    avail = str(tmp_path_factory.mktemp("answers") / "avail.json")
    saved = core.availability_path
    core.availability_path = lambda: avail
    docs = str(tmp_path_factory.mktemp("Documents"))
    saved_docs = core.documents_dir
    core.documents_dir = lambda: docs       # the start-up thread makes the logs folder
    try:
        a = gui.App()
    except Exception as e:                                  # noqa: BLE001
        core.availability_path = saved
        core.documents_dir = saved_docs
        pytest.skip("no display: %s" % e)
    a._stop_feed()
    a.autoupdate_var.set(False)             # no update checks during tests
    a.avail_file = avail
    yield a
    if a.poller:
        a.poller.stop()
    a.destroy()
    core.availability_path = saved
    core.documents_dir = saved_docs


def reset_app(a):
    """Back to a freshly started, not-connected window with nothing watched."""
    if a.poller:
        a.poller.stop()
        a.poller.join(3)
        a.poller = None
    a._stop_check()
    a._checking = None
    a._closing = False               # a test may have run _close()
    a._stop_feed()
    a._clear()
    a.avail.clear()
    a._avail_dirty = False
    a.series.clear()
    a.modules = []
    a.logging_to = None
    a.template_name = ""
    a.log_btn.config(text="Start logging")
    a.replay_btn.config(text="Open log...")
    a.connect_btn.config(text="Connect")
    a.status.config(text="● not connected", foreground="#999")
    a.ip_var.set("")
    a.target_var.set("0x12")
    a.interval_var.set("0.25")
    a.probe_var.set("%g" % core.PROBE_TIMEOUT)
    a.filter_var.set("")
    a.group_var.set("Module")
    a.only_var.set(False)
    if a.lang != "en":
        a.lang_var.set(core.LANGUAGES["en"])
        a._set_language()
    a._set_units("metric")
    if a.theme_name != "light":
        a._set_theme("light")
    if getattr(a, "settings_window", None) and a.settings_window.winfo_exists():
        a.settings_window.destroy()
    a.chart_hover = None
    while True:
        try:
            a.q.get_nowait()
        except queue.Empty:
            break
    a._refilter()


@pytest.fixture
def app(shared_app, monkeypatch):
    # A real dialog blocks forever on a headless CI runner. Make any dialog
    # a test didn't stub fail at once instead; tests that expect one stub it
    # themselves (their monkeypatch runs after this one and wins).
    gui = pytest.importorskip("n63read.gui")

    def unexpected(name):
        def fail(*a, **k):
            raise AssertionError("unstubbed dialog %s(%r) would hang CI" % (name, a or k))
        return fail
    for name in ("asksaveasfilename", "askopenfilename", "askopenfilenames",
                 "askdirectory"):
        monkeypatch.setattr(gui.filedialog, name, unexpected(name))
    for name in ("showinfo", "showerror", "showwarning", "askyesno",
                 "askokcancel", "askquestion", "askretrycancel", "askyesnocancel"):
        monkeypatch.setattr(gui.messagebox, name, unexpected(name))
    for name in ("askstring", "askinteger", "askfloat"):
        monkeypatch.setattr(gui.simpledialog, name, unexpected(name))

    reset_app(shared_app)
    yield shared_app
    reset_app(shared_app)
    # Free this test's Tk variables now, on the main thread. Collected later
    # on another thread (the fake car's), Tk stalls that thread for a
    # second waiting for a main loop, and its replies come too late.
    gc.collect()


def pump(app, cond, timeout=5.0):
    """Run the GUI's event loop until `cond()` holds; False on timeout."""
    end = time.time() + timeout
    while time.time() < end:
        app.update()
        app._drain()
        if cond():
            return True
        time.sleep(0.02)
    return False
