"""firmware/tools/publish_firmware.py: the version it names a build, the
build's flags, the Access token, and the request that publishes it. No
PlatformIO, git or network: those are stubbed."""

import io
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "firmware", "tools"))

import publish_firmware as pf  # noqa: E402

SECRETS = '''#define WORKER_HOST "m550.example.com"
#define ACCESS_CLIENT_ID "board-id.access"
#define ACCESS_CLIENT_SECRET "board-secret"
'''


@pytest.fixture
def secrets(tmp_path, monkeypatch):
    path = tmp_path / "logger_secrets.h"
    monkeypatch.setattr(pf, "SECRETS", path)
    monkeypatch.delenv("CF_ACCESS_CLIENT_ID", raising=False)
    monkeypatch.delenv("CF_ACCESS_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("N63_WORKER_HOST", raising=False)
    return path


def fake_git(monkeypatch, dirty=""):
    def git(*args):
        if args[0] == "rev-parse":
            return "abc1234"
        if args[0] == "status":
            return dirty
        raise AssertionError(args)
    monkeypatch.setattr(pf, "git", git)


def test_the_version_is_the_time_and_the_commit(monkeypatch):
    fake_git(monkeypatch)
    assert re.fullmatch(r"\d{8}-\d{4}-abc1234", pf.version(False))


def test_uncommitted_firmware_is_refused_unless_allowed(monkeypatch):
    fake_git(monkeypatch, dirty=" M firmware/logger/src/main.cpp")
    with pytest.raises(SystemExit, match="uncommitted"):
        pf.version(False)
    assert pf.version(True).endswith("-abc1234-dirty")


def test_the_build_gets_the_version_and_test_flags(monkeypatch):
    runs = []

    def run(cmd, env):
        runs.append((cmd, env))
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(pf, "pio", lambda: "pio")
    monkeypatch.setattr(pf.subprocess, "run", run)
    pf.build("20261027-0250-66c0b5f", ["OTA_TEST_SILENT"], None)
    pf.build("v2", [], "COM4")
    (cmd, env), (usb, _) = runs
    assert cmd == ["pio", "run", "-d", str(pf.PROJECT)]
    # Never the secrets: the board keeps its own (provision.py).
    assert env["PLATFORMIO_BUILD_FLAGS"] == ('-DFW_VERSION=\\"20261027-0250-66c0b5f\\" '
                                             '-DLOGGER_NO_SECRETS -DOTA_TEST_SILENT')
    # Without these an upload hangs when pio's output is redirected.
    assert env["PYTHONIOENCODING"] == "utf-8" and env["PYTHONUTF8"] == "1"
    assert usb[-4:] == ["-t", "upload", "--upload-port", "COM4"]


def test_a_failed_build_stops(monkeypatch):
    monkeypatch.setattr(pf, "pio", lambda: "pio")
    monkeypatch.setattr(pf.subprocess, "run",
                        lambda cmd, env: subprocess.CompletedProcess(cmd, 1))
    with pytest.raises(SystemExit, match="build failed"):
        pf.build("v2", [], None)


def test_the_token_comes_from_the_environment_first(secrets, monkeypatch):
    secrets.write_text(SECRETS)
    assert pf.token() == ("board-id.access", "board-secret")
    assert pf.worker_host() == "m550.example.com"
    monkeypatch.setenv("N63_WORKER_HOST", "ci.example.com")
    assert pf.worker_host() == "ci.example.com"
    monkeypatch.setenv("CF_ACCESS_CLIENT_ID", "ci-id")
    monkeypatch.setenv("CF_ACCESS_CLIENT_SECRET", "ci-secret")
    assert pf.token() == ("ci-id", "ci-secret")


def test_no_token_and_no_host(secrets):
    with pytest.raises(SystemExit, match="no Access token"):
        pf.token()
    assert pf.worker_host() is None
    secrets.write_text('#define ACCESS_CLIENT_ID "only-the-id"\n')
    with pytest.raises(SystemExit, match="no Access token"):
        pf.token()


class Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_publish_puts_the_image_with_the_token(secrets, tmp_path, monkeypatch):
    secrets.write_text(SECRETS)
    image = tmp_path / "firmware.bin"
    image.write_bytes(b"\xe9 an app image")
    monkeypatch.setattr(pf, "IMAGE", image)
    sent = []

    def urlopen(req, timeout):
        sent.append(req)
        return Reply(b'{"version":"v2","size":15}')
    monkeypatch.setattr(pf.urllib.request, "urlopen", urlopen)
    pf.publish("v2", "m550.example.com")
    (req,) = sent
    assert req.get_method() == "PUT"
    assert req.full_url == "https://m550.example.com/api/logger/firmware?version=v2"
    assert req.data == b"\xe9 an app image"
    assert req.get_header("Cf-access-client-id") == "board-id.access"
    assert req.get_header("Cf-access-client-secret") == "board-secret"
    # Cloudflare turns away urllib's own User-Agent (error 1010).
    assert not req.get_header("User-agent").startswith("Python-urllib")


def test_a_refused_publish_says_why(secrets, tmp_path, monkeypatch):
    secrets.write_text(SECRETS)
    image = tmp_path / "firmware.bin"
    image.write_bytes(b"\x00 not an image")
    monkeypatch.setattr(pf, "IMAGE", image)

    def urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {},
                                     io.BytesIO(b'{"error":"not an ESP32 app image"}'))
    monkeypatch.setattr(pf.urllib.request, "urlopen", urlopen)
    with pytest.raises(SystemExit, match="publish failed: 400 .*not an ESP32 app image"):
        pf.publish("v2", "m550.example.com")


@pytest.fixture
def steps(secrets, monkeypatch):
    """main() with the build and publish recorded, not run."""
    secrets.write_text(SECRETS)
    fake_git(monkeypatch)
    done = []
    monkeypatch.setattr(pf, "build", lambda v, flags, port: done.append(("build", v, flags, port)))
    monkeypatch.setattr(pf, "publish", lambda v, host: done.append(("publish", v, host)))
    return done


def run_main(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["publish_firmware.py", *args])
    pf.main()


def test_main_builds_then_publishes(steps, monkeypatch):
    run_main(monkeypatch, "--version", "v2", "--flag", "OTA_TEST_SILENT")
    assert steps == [("build", "v2", ["OTA_TEST_SILENT"], None),
                     ("publish", "v2", "m550.example.com")]


def test_main_by_usb_doesnt_publish(steps, monkeypatch):
    run_main(monkeypatch, "--usb", "COM4")
    ((step, v, _, port),) = steps
    assert (step, port) == ("build", "COM4") and v.endswith("-abc1234")


@pytest.mark.parametrize("bad", ["dev", "a b", "x" * 41, "v/1"])
def test_main_refuses_bad_versions(steps, monkeypatch, bad):
    with pytest.raises(SystemExit, match="version"):
        run_main(monkeypatch, "--version", bad)
    assert steps == []


def test_main_needs_a_host_to_publish(steps, secrets, monkeypatch):
    secrets.write_text("")
    with pytest.raises(SystemExit, match="no Worker host"):
        run_main(monkeypatch, "--version", "v2")
    run_main(monkeypatch, "--version", "v2", "--host", "other.example.com")
    assert steps[-1] == ("publish", "v2", "other.example.com")


def test_provision_takes_the_settings_from_a_secrets_file(tmp_path):
    import provision
    path = tmp_path / "logger_secrets.h"
    path.write_text('// a comment\n#define WIFI_SSID "Home Net"\n#define WIFI_PASS "pa=ss word"\n'
                    '// #define WIFI_SSID2 "off"\n#define WORKER_HOST "m550.example.com"\n'
                    '#define ACCESS_CLIENT_ID "id.access"\n#define ACCESS_CLIENT_SECRET "s3cret"\n'
                    '#define TZ_INFO "CST6CDT,M3.2.0,M11.1.0"\n#define OTHER "x"\n')
    assert provision.from_secrets(path) == {
        "wifi_ssid": "Home Net", "wifi_pass": "pa=ss word", "host": "m550.example.com",
        "access_id": "id.access", "access_secret": "s3cret", "tz": "CST6CDT,M3.2.0,M11.1.0"}
