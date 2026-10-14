import queue
import time

import pytest

from n63read import core


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
    try:
        a = gui.App()
    except Exception as e:                                  # noqa: BLE001
        core.availability_path = saved
        pytest.skip("no display: %s" % e)
    a._stop_feed()
    a.avail_file = avail
    yield a
    if a.poller:
        a.poller.stop()
    a.destroy()
    core.availability_path = saved


def reset_app(a):
    """Back to a freshly started, not-connected window with nothing watched."""
    if a.poller:
        a.poller.stop()
        a.poller.join(3)
        a.poller = None
    a._stop_check()
    a._checking = None
    a._stop_feed()
    a._clear()
    a.avail.clear()
    a._avail_dirty = False
    a.series.clear()
    a.modules = []
    a.logging_to = None
    a.log_btn.config(text="Start logging")
    a.replay_btn.config(text="Open log...")
    a.connect_btn.config(text="Connect")
    a.status.config(text="● not connected", foreground="#999")
    a.ip_var.set("")
    a.target_var.set("0x12")
    a.interval_var.set("0.25")
    a.filter_var.set("")
    a.group_var.set("Module")
    a.only_var.set(False)
    while True:
        try:
            a.q.get_nowait()
        except queue.Empty:
            break
    a._refilter()


@pytest.fixture
def app(shared_app):
    reset_app(shared_app)
    yield shared_app
    reset_app(shared_app)


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
