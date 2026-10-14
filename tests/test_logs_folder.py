"""Documents\\BMW Analyzer Logs, and the installer's Settings > Apps entry."""

import os
import sys
import time
import types

import pytest

from n63read import cli, core, update

from fake_car import FakeCar

REAL_DOCUMENTS_DIR = core.documents_dir     # before the autouse fixture swaps it


def test_documents_dir_is_a_real_folder():
    path = REAL_DOCUMENTS_DIR()
    assert path and os.path.isabs(path)


def test_logs_dir_is_made_under_documents(tmp_path):
    path = core.logs_dir()
    assert path == os.path.join(str(tmp_path / "Documents"), "BMW Analyzer Logs")
    assert os.path.isdir(path)


def test_logs_dir_can_just_name_it(tmp_path):
    path = core.logs_dir(create=False)
    assert path.endswith(core.LOGS_FOLDER) and not os.path.exists(path)


def test_cli_log_defaults_to_the_logs_folder(monkeypatch, capsys):
    c = FakeCar()
    monkeypatch.setattr(core.Hsfz.__init__, "__defaults__", (c.port, core.TESTER_ADDR, 3.0))

    def sleep(_):
        raise KeyboardInterrupt
    monkeypatch.setattr(cli, "time", types.SimpleNamespace(
        time=time.time, sleep=sleep, strftime=time.strftime))
    try:
        cli.main(["log", "--ip", "127.0.0.1", "--timeout", "0.5", "--interval", "0", "ILAG2"])
    finally:
        c.close()
    files = os.listdir(core.logs_dir())
    assert len(files) == 1 and files[0].startswith("n63_") and files[0].endswith(".csv")
    assert "wrote 1 samples" in capsys.readouterr().out


def test_gui_dialogs_open_in_the_logs_folder(app, monkeypatch):
    gui = pytest.importorskip("n63read.gui")
    asked = []
    monkeypatch.setattr(gui.filedialog, "askopenfilename", lambda **k: asked.append(k) or "")
    app._toggle_replay()
    assert asked and asked[0]["initialdir"] == core.logs_dir()


class FakeWinreg:
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ, KEY_SET_VALUE, REG_SZ = 1, 2, 1

    def __init__(self, keys):
        self.keys = keys

    def OpenKey(self, root, sub, reserved, access):
        if (root, sub) not in self.keys:
            raise FileNotFoundError(sub)
        key = self.keys[(root, sub)]

        class Handle:
            def __enter__(s):
                return key

            def __exit__(s, *a):
                return False
        return Handle()

    def SetValueEx(self, key, name, reserved, kind, value):
        key[name] = value


def test_installed_version_follows_a_self_update(monkeypatch):
    entry = {"DisplayVersion": "0.3.2"}
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg",
                        FakeWinreg({("HKCU", update.UNINSTALL_KEY): entry}))
    assert update.set_installed_version("0.3.3") is True
    assert entry["DisplayVersion"] == "0.3.3"


def test_no_installer_entry_means_nothing_to_set(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", FakeWinreg({}))
    assert update.set_installed_version("0.3.3") is False


def test_uninstall_key_matches_the_installer_script():
    iss = os.path.join(os.path.dirname(__file__), os.pardir, "installer", "BMW-Analyzer.iss")
    with open(iss, encoding="utf-8") as f:
        text = f.read()
    assert "AppId={%s" % update.APP_ID in text
