"""cloud: uploading the logs folder to the Worker, against a stand-in Worker."""

import hashlib
import http.server
import json
import os
import sys
import threading
import urllib.parse

import pytest

from n63read import cli, cloud, core

from test_history import drive, fault_csv


class FakeWorker:
    """Answers like cloud/src/worker.js behind Access with one service token."""

    def __init__(self, client_id="id.access", secret="s3cret"):
        self.files, self.history, self.requests = {}, None, []
        self.templates = {}                      # name -> {channels, deleted}
        self.series = {}                         # drive -> body
        self.catalog = None                      # the logger catalog, as sent
        self.client_id, self.secret = client_id, secret
        self.fail = None                         # a status to answer instead, once set
        worker = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authorised(self):
                return (self.headers.get("CF-Access-Client-Id") == worker.client_id
                        and self.headers.get("CF-Access-Client-Secret") == worker.secret)

            def _all_templates(self):
                return {"templates": [dict(name=n, **t) for n, t in
                                      sorted(worker.templates.items())]}

            def do_GET(self):
                worker.requests.append((self.command, self.path, {}))
                if not self._authorised():
                    return self._reply(403, {"error": "forbidden"})
                if self.path == "/api/templates":
                    return self._reply(200, self._all_templates())
                return self._reply(404, {"error": "not found"})

            def do_PUT(self):
                worker.requests.append((self.command, self.path,
                                        {k.lower(): v for k, v in self.headers.items()}))
                # The body first, whatever the answer: replying to a request
                # whose body is still unread makes Windows reset the
                # connection (WinError 10053 in the client), a flaky failure.
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if (self.headers.get("CF-Access-Client-Id") != worker.client_id
                        or self.headers.get("CF-Access-Client-Secret") != worker.secret):
                    return self._reply(403, {"error": "forbidden"})
                if worker.fail:
                    return self._reply(worker.fail, {"error": "boom"})
                if self.path.startswith("/api/files/"):
                    key = urllib.parse.unquote(self.path[len("/api/files/"):])
                    worker.files[key] = body
                    return self._reply(200, {"ok": True, "key": key, "size": len(body),
                                             "sha256": hashlib.sha256(body).hexdigest()})
                if self.path.startswith("/api/series/"):
                    drive = urllib.parse.unquote(self.path[len("/api/series/"):])
                    worker.series[drive] = json.loads(body)
                    return self._reply(200, {"ok": True, "drive": drive})
                if self.path == "/api/templates":
                    for c in json.loads(body)["changes"]:
                        worker.templates[c["name"]] = {"channels": c["channels"],
                                                       "deleted": c["channels"] is None}
                    return self._reply(200, self._all_templates())
                if self.path == "/api/logger/catalog":
                    worker.catalog = json.loads(body)
                    return self._reply(200, {"ok": True, "channels": len(worker.catalog["rows"])})
                if self.path == "/api/history":
                    worker.history = json.loads(body)
                    return self._reply(200, {"ok": True,
                                             "drives": len(worker.history["drives"]),
                                             "faults": len(worker.history["faults"])})
                return self._reply(404, {"error": "not found"})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def worker():
    w = FakeWorker()
    yield w
    w.close()


def puts(w):
    """Uploads made: every sync also asks for the templates (a GET)."""
    return sum(1 for r in w.requests if r[0] == "PUT")


def settings_for(w, secret="s3cret"):
    return {"cloud_url": w.url + "/", "cloud_client_id": "id.access",
            "cloud_client_secret": cloud.protect(secret)}


def test_the_secret_is_kept_encrypted():
    stored = cloud.protect("cfast_abc")
    assert "cfast_abc" not in stored or sys.platform != "win32"
    assert cloud.unprotect(stored) == "cfast_abc"
    assert cloud.unprotect("plain:x") == "x" and cloud.protect("") == ""


def test_sync_uploads_logs_and_history_then_only_what_changed(worker, tmp_path):
    logs = str(tmp_path / "logs")
    drive(os.path.join(logs, "n63_1.csv"))
    drive(os.path.join(logs, "claude", "n63_2.csv"))
    fault_csv(os.path.join(logs, "n63_faults.csv"), "2026-10-19 07:00:00",
              [("0x40", "804415", "present, stored")])
    s = settings_for(worker)
    r = cloud.sync(s, logs)
    assert sorted(r.uploaded) == sorted(["n63_1.csv", os.path.join("claude", "n63_2.csv"),
                                         "n63_faults.csv"])
    assert set(worker.files) == {"n63_1.csv", "claude/n63_2.csv", "n63_faults.csv",
                                 cloud.FAULT_TEXTS}                # once, for the logger's faults
    assert r.history == (2, 1) and worker.history["drives"][0]["misfires_5"] == 2
    method, path, headers = worker.requests[0]
    assert headers["cf-access-client-secret"] == "s3cret"
    assert headers["user-agent"].startswith("BMW-Analyzer/")
    assert not any("History" in k for k in worker.files)        # the summaries go as JSON

    sent = puts(worker)
    r = cloud.sync(s, logs)                                      # nothing new
    assert r.uploaded == [] and r.history is None and puts(worker) == sent

    drive(os.path.join(logs, "n63_3.csv"), misfire5_at=(10,))
    r = cloud.sync(s, logs)
    assert r.uploaded == ["n63_3.csv"] and r.history == (3, 1)
    assert "1 file uploaded" in r.text() and "3 drives" in r.text()


def test_the_fault_texts_go_up_once_and_again_when_they_change(worker, tmp_path, monkeypatch):
    logs = str(tmp_path / "logs")
    drive(os.path.join(logs, "n63_1.csv"))
    s = settings_for(worker)
    cloud.sync(s, logs)
    with open(core.resource_path(core.FAULT_CSV), "rb") as f:
        assert worker.files[cloud.FAULT_TEXTS] == f.read()
    worker.files.clear()
    cloud.sync(s, logs)
    assert cloud.FAULT_TEXTS not in worker.files
    texts = tmp_path / "texts.csv"
    texts.write_text("code,text_de,text_en,source_module\n480A14,x,y,DME\n", encoding="utf-8")
    monkeypatch.setattr(core, "resource_path",
                        lambda name: str(texts) if name == core.FAULT_CSV else name)
    cloud.sync(s, logs)
    assert worker.files[cloud.FAULT_TEXTS] == texts.read_bytes()


def test_only_logs_fault_lists_and_checks_go_up(worker, tmp_path):
    logs = str(tmp_path / "logs")
    drive(os.path.join(logs, "n63_1.csv"))
    os.makedirs(os.path.join(logs, "extra"))
    with open(os.path.join(logs, "extra", "catalog.csv"), "w") as f:
        f.write("ARG,ID,INFO\nINMOT,0x4807,Engine speed\n")
    with open(os.path.join(logs, "check.csv"), "w") as f:
        f.write("module,did,job,status,detail,reply_bytes,raw\n0x12,0x4807,X,ok,,2,0000\n")
    assert sorted(cloud.sync(settings_for(worker), logs).uploaded) == ["check.csv", "n63_1.csv"]


def test_each_drive_logs_values_go_into_the_time_series_once(worker, tmp_path):
    logs = str(tmp_path / "logs")
    drive(os.path.join(logs, "n63_1.csv"))
    fault_csv(os.path.join(logs, "n63_faults.csv"), "2026-10-19 07:00:00",
              [("0x40", "804415", "present, stored")])
    s = settings_for(worker)
    r = cloud.sync(s, logs)
    assert r.series == 1 and list(worker.series) == ["n63_1.csv"]     # not the fault list
    body = worker.series["n63_1.csv"]
    by = {c["name"]: c for c in body["chunks"]}
    assert by["IMUL1"]["n"] == 60 and by["IMUL1"]["data"][0] == [0.0, 1.05]
    assert body["approx"] is False and by["INMOT"]["t0"] == pytest.approx(
        core.clock_epoch("2026-10-19 07:00:00.000"), abs=0.01)
    assert by["IUIBS"]["unit"] == "mV" and by["INMOT"]["max"] == 1559
    assert "1 drive into the time series" in r.text()
    assert cloud.sync(s, logs).series == 0                           # once
    drive(os.path.join(logs, "n63_1.csv"), seconds=70)                 # changed: again
    assert cloud.sync(s, logs).series == 1


def test_a_drive_log_from_before_the_time_series_goes_up_on_the_next_sync(worker, tmp_path):
    logs = str(tmp_path / "logs")
    drive(os.path.join(logs, "n63_1.csv"))
    s = settings_for(worker)
    cloud.sync(s, logs)
    state = cloud.load_state()
    state.pop("series")                          # as a 0.6.2 sync left it
    for e in state["files"].values():
        e.pop("kind", None)
    cloud.save_state(state)
    worker.series.clear()
    assert cloud.sync(s, logs).series == 1 and "n63_1.csv" in worker.series


def test_templates_travel_between_pcs(worker, tmp_path):
    logs = str(tmp_path / "logs")
    os.makedirs(logs)
    a = (str(tmp_path / "a_templates.json"), str(tmp_path / "a_state.json"))
    b = (str(tmp_path / "b_templates.json"), str(tmp_path / "b_state.json"))
    s = settings_for(worker)

    def on(pc):
        return cloud.sync(s, logs, state_file=pc[1], templates_file=pc[0])

    core.save_template("Misfire hunt", ["INMOT", "ILUZ5"], a[0])
    core.save_template("Tires", ["DSC_RDC_MESSDATENBLOCK_1.LETZTER_REIFENDRUCKWERT"], a[0])
    assert on(a).templates == (2, 0)
    r = on(b)                                    # the other PC pulls them down
    assert r.templates == (0, 2) and "2 received" in r.text()
    assert core.load_templates(b[0]) == core.load_templates(a[0])

    core.delete_template("Tires", b[0])           # deleted there...
    core.save_template("Boost", ["IPLAD"], b[0])
    assert on(b).templates == (2, 0)
    assert on(a).templates == (0, 2)              # ...and gone here, Boost arrived
    assert sorted(core.load_templates(a[0])) == ["Boost", "Misfire hunt"]
    assert on(a).templates == (0, 0) and on(b).templates == (0, 0)   # settled


def test_both_pcs_changed_a_template_this_pc_wins(worker, tmp_path):
    logs = str(tmp_path / "logs")
    os.makedirs(logs)
    a = (str(tmp_path / "a.json"), str(tmp_path / "as.json"))
    b = (str(tmp_path / "b.json"), str(tmp_path / "bs.json"))
    s = settings_for(worker)
    core.save_template("Set", ["INMOT"], a[0])
    cloud.sync(s, logs, state_file=a[1], templates_file=a[0])
    cloud.sync(s, logs, state_file=b[1], templates_file=b[0])
    core.save_template("Set", ["INMOT", "ITOEL"], a[0])
    core.save_template("Set", ["INMOT", "IPLAD"], b[0])
    cloud.sync(s, logs, state_file=b[1], templates_file=b[0])
    cloud.sync(s, logs, state_file=a[1], templates_file=a[0])   # a's edit wins on a...
    assert core.load_templates(a[0])["Set"] == ["INMOT", "ITOEL"]
    cloud.sync(s, logs, state_file=b[1], templates_file=b[0])   # ...and reaches b
    assert core.load_templates(b[0])["Set"] == ["INMOT", "ITOEL"]


def test_a_touched_but_unchanged_file_isnt_sent_again(worker, tmp_path):
    logs = str(tmp_path / "logs")
    path = os.path.join(logs, "n63_1.csv")
    drive(path)
    s = settings_for(worker)
    cloud.sync(s, logs)
    st = os.stat(path)
    os.utime(path, (st.st_atime, st.st_mtime + 100))
    sent = puts(worker)
    assert cloud.sync(s, logs).uploaded == [] and puts(worker) == sent


def test_a_refused_token_says_so_and_nothing_is_marked_sent(worker, tmp_path):
    logs = str(tmp_path / "logs")
    drive(os.path.join(logs, "n63_1.csv"))
    with pytest.raises(cloud.CloudError, match="refused the service token"):
        cloud.sync(settings_for(worker, secret="wrong"), logs)
    assert cloud.load_state().get("files", {}).get("n63_1.csv") is None


def test_offline_raises_and_tries_everything_next_time(tmp_path):
    logs = str(tmp_path / "logs")
    drive(os.path.join(logs, "n63_1.csv"))
    s = {"cloud_url": "http://127.0.0.1:9", "cloud_client_id": "x",
         "cloud_client_secret": cloud.protect("y")}
    with pytest.raises(cloud.CloudError, match="can't reach"):
        cloud.sync(s, logs)
    assert not cloud.load_state().get("files")


def test_a_failing_file_is_listed_and_retried(worker, tmp_path):
    logs = str(tmp_path / "logs")
    drive(os.path.join(logs, "n63_1.csv"))
    worker.fail = 500
    with pytest.raises(cloud.CloudError):           # the history PUT fails too
        cloud.sync(settings_for(worker), logs)
    worker.fail = None
    assert cloud.sync(settings_for(worker), logs).uploaded == ["n63_1.csv"]


def test_not_set_up_sends_nothing(tmp_path):
    with pytest.raises(cloud.CloudError, match="set the Worker URL"):
        cloud.request({}, "GET", "/api/status")
    a = cloud.AutoSync({}, logs=str(tmp_path), first=0)
    assert a.status == "not set up" and a.run_once() is None and not a.enabled()


def test_autosync_runs_when_triggered(worker, tmp_path):
    logs = str(tmp_path / "logs")
    drive(os.path.join(logs, "n63_1.csv"))
    a = cloud.AutoSync(settings_for(worker), logs=logs, every=3600, first=3600).start()
    try:
        a.trigger()
        end = __import__("time").time() + 10
        while "synced" not in a.status and __import__("time").time() < end:
            __import__("time").sleep(0.05)
        assert "synced" in a.status and "1 file uploaded" in a.status
    finally:
        a.stop()


def _wait_for(check, seconds=10):
    end = __import__("time").time() + seconds
    while not check() and __import__("time").time() < end:
        __import__("time").sleep(0.05)
    return check()


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI")
def test_a_secret_saved_on_another_pc_says_so_instead_of_breaking(worker, tmp_path):
    # A settings.json copied from another PC or Windows user: DPAPI can't
    # read its secret. It raised, so Settings didn't open and sync stopped.
    s = dict(settings_for(worker), cloud_client_secret="dpapi:" + "QUJD" * 8)
    assert cloud.unprotect(s["cloud_client_secret"]) == ""
    assert cloud.config(s)[2] == "" and not cloud.configured(s) and cloud.secret_lost(s)
    a = cloud.AutoSync(s, logs=str(tmp_path), first=0)
    assert "can't be read on this PC" in a.status
    assert a.run_once() is None and "Settings > Cloud" in a.status
    assert cloud.unprotect("dpapi:not base64!") == ""
    assert not cloud.secret_lost({}) and not cloud.secret_lost(settings_for(worker))


def test_an_unexpected_error_is_said_and_the_sync_thread_goes_on(worker, tmp_path, monkeypatch):
    # Anything but CloudError or OSError ended the thread, the status left
    # at "syncing..." and no sync again until the app restarted.
    logs = str(tmp_path / "logs")
    drive(os.path.join(logs, "n63_1.csv"))
    real, calls = cloud.sync, []

    def flaky(settings, logs_dir):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("something unexpected")
        return real(settings, logs_dir)
    monkeypatch.setattr(cloud, "sync", flaky)
    a = cloud.AutoSync(settings_for(worker), logs=logs, every=3600, first=3600).start()
    try:
        a.trigger()
        assert _wait_for(lambda: "RuntimeError" in a.status), a.status
        assert "not synced: RuntimeError: something unexpected" == a.status
        a.trigger()                                  # the thread is still there
        assert _wait_for(lambda: "synced" in a.status and "not" not in a.status), a.status
    finally:
        a.stop()


def test_a_log_csv_cant_read_is_noted_and_the_rest_still_go_up(worker, tmp_path):
    # csv.Error (here a field past csv's limit) from the time series ended the sync.
    logs = str(tmp_path / "logs")
    drive(os.path.join(logs, "n63_1.csv"))
    drive(os.path.join(logs, "n63_2.csv"))
    with open(os.path.join(logs, "n63_2.csv"), "a", encoding="utf-8") as f:
        f.write('99,"%s"\n' % ("x" * 200000))
    r = cloud.sync(settings_for(worker), logs)
    assert list(worker.series) == ["n63_1.csv"]
    assert any("n63_2.csv (time series)" in e for e in r.errors)


def test_cli_sync_saves_settings_and_uploads(worker, capsys):
    drive(os.path.join(core.logs_dir(), "n63_1.csv"))
    saved = {}
    import n63read.core as c
    real_load, real_save = c.load_settings, c.save_settings
    c.load_settings = lambda path=None: dict(saved)
    c.save_settings = lambda s, path=None: saved.update(s)
    try:
        cli.main(["sync", "--url", worker.url, "--client-id", "id.access",
                  "--client-secret", "s3cret"])
        out = capsys.readouterr().out
        assert "1 file uploaded" in out and "n63_1.csv" in worker.files
        assert saved["cloud_url"] == worker.url
        assert cloud.unprotect(saved["cloud_client_secret"]) == "s3cret"
        cli.main(["sync", "--status"])
        assert "1 files sent" in capsys.readouterr().out
    finally:
        c.load_settings, c.save_settings = real_load, real_save


def test_settings_window_saves_the_cloud_token(app):
    app._open_settings()
    try:
        app.cloud_url_var.set("https://car.example.com")
        app.cloud_id_var.set("id.access")
        app.cloud_secret_var.set("s3cret")
        app._save_cloud()
        assert cloud.configured(app.settings)
        assert cloud.unprotect(app.settings["cloud_client_secret"]) == "s3cret"
    finally:
        app.settings_window.destroy()
        for k in ("cloud_url", "cloud_client_id", "cloud_client_secret"):
            app.settings.pop(k, None)


def test_history_says_which_installation_sent_it(worker, tmp_path, monkeypatch):
    logs = str(tmp_path / "logs")
    drive(os.path.join(logs, "n63_1.csv"))
    monkeypatch.setattr(cloud.socket, "gethostname", lambda: "DESK-PC")
    cloud.sync(settings_for(worker), logs)
    assert worker.history["source"] == "DESK-PC"
    assert cloud.source_name({"cloud_source": "laptop / garage"}) == "laptop - garage"
    assert cloud.source_name({"cloud_source": "logger"}) == "pc"      # the board's


def test_the_logger_catalog_goes_up_once_and_again_when_the_answers_change(worker, tmp_path,
                                                                          monkeypatch):
    monkeypatch.setattr(core, "availability_path",
                        lambda: str(tmp_path / "n63read" / "availability.json"))
    logs = str(tmp_path / "logs")
    drive(os.path.join(logs, "n63_1.csv"))
    s = settings_for(worker)
    cloud.sync(s, logs)
    sent = worker.catalog
    assert len(sent["rows"]) > 10000 and sent["answers"] == {}
    assert sent["defaults"]["INMOT"] == "fast"
    worker.catalog = None
    cloud.sync(s, logs)
    assert worker.catalog is None                               # unchanged: not sent again
    core.save_availability({(0x29, 0xDC98): "ok"})
    cloud.sync(s, logs)
    assert worker.catalog["answers"] == {"0x29:0xDC98": "ok"}


def test_the_logger_page_sends_the_catalog_first(worker):
    s = settings_for(worker)
    sync = cloud.AutoSync(s)
    sync.send_logger_catalog()
    assert worker.catalog is not None
    assert cloud.logger_page(s) == worker.url + "/logger"
