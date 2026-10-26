"""
n63read.cloud - copy the logs folder to the cloud Worker (cloud/ in the repo).

sync() uploads every drive log and fault list that is new or changed since
the last upload (PUT /api/files/<path>), the channel templates both ways
(/api/templates), the logger board's channel catalog with what the car
answered (PUT /api/logger/catalog, for the logger channel page), each drive
log's values as time series (PUT /api/series/<path>, see series.py), then
the drive and fault summaries from history.scan() (PUT /api/history). What was sent is remembered in
%APPDATA%\\n63read\\cloud_state.json, so a sync sends only what changed and a
file that failed goes again next time. AutoSync runs it in the background:
after a log closes, after a fault list is saved, and every few minutes, so
whatever couldn't go while the PC was offline goes once it's back online.

The Worker sits behind Cloudflare Access; requests carry an Access service
token (CF-Access-Client-Id / CF-Access-Client-Secret). The secret is kept in
the app's settings, encrypted for this Windows user (DPAPI) where possible.
Nothing here is sent anywhere until a Worker URL and token are set.
"""

import base64
import csv
import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from n63read import __version__, core, history, series

STATE_FILE = "cloud_state.json"
TIMEOUT = 60
EVERY = 10 * 60             # s between background syncs
FIRST_DELAY = 20            # s after start before the first
USER_AGENT = "BMW-Analyzer/%s" % __version__
UPLOADED = ("drive", "faults", "check")     # what goes up; other CSVs stay put


class CloudError(Exception):
    pass


# ------------------------------------------------------------ the secret

def _dpapi(data, protect):
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    buf = ctypes.create_string_buffer(data, len(data))
    src, dst = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), Blob()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else \
        ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(dst)):
        raise OSError("DPAPI failed")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


def protect(secret):
    """The secret as kept in settings: encrypted for this Windows user."""
    if not secret:
        return ""
    if sys.platform == "win32":
        try:
            return "dpapi:" + base64.b64encode(_dpapi(secret.encode(), True)).decode()
        except (OSError, AttributeError):
            pass
    return "plain:" + secret


def unprotect(stored):
    if not stored:
        return ""
    if stored.startswith("dpapi:"):
        return _dpapi(base64.b64decode(stored[6:]), False).decode()
    return stored[6:] if stored.startswith("plain:") else stored


# ------------------------------------------------------------ settings

def config(settings):
    """(url, client id, client secret) from the app's settings."""
    return ((settings.get("cloud_url") or "").rstrip("/"),
            settings.get("cloud_client_id") or "",
            unprotect(settings.get("cloud_client_secret") or ""))


def configured(settings):
    return all(config(settings))


def source_name(settings):
    """This installation's name in the cloud: its drives and faults are kept
    under it, so another PC's sync never replaces them. The cloud_source
    setting, else the PC's name."""
    name = (settings.get("cloud_source") or socket.gethostname() or "").strip()
    name = re.sub(r"[^\w .@()+,=-]", "-", name)[:64].strip()
    return name if name and name != "logger" else "pc"


def state_path():
    return os.path.join(os.path.dirname(core.availability_path()), STATE_FILE)


def load_state(path=None):
    try:
        with open(path or state_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state, path=None):
    path = path or state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)


# ------------------------------------------------------------ requests

def request(settings, method, path, data=None, ctype="application/octet-stream"):
    """Call the Worker; its JSON answer."""
    body = _send(settings, method, path, data, ctype)
    try:
        return json.loads(body or b"{}")
    except ValueError:
        # Access answers a login page, not JSON, when the token isn't accepted.
        raise CloudError("not the Worker's answer (a login page?): check the URL and that "
                         "the Access policy allows the service token") from None


def fetch(settings, path):
    """GET a file the Worker stores (path: /api/files/<key>); its bytes."""
    return _send(settings, "GET", path)


def check_files(settings):
    """The check results in the cloud (`n63read check --out` and the app's
    Check car now, both uploaded by sync): [(answers, key)], oldest first."""
    listed = request(settings, "GET", "/api/files").get("files", [])
    out = []
    for f in sorted(listed, key=lambda f: f.get("uploaded") or ""):
        key = f.get("key") or ""
        name = key.rsplit("/", 1)[-1].lower()
        if "check" not in name or not name.endswith(".csv"):
            continue
        text = fetch(settings, "/api/files/" + _key(key)).decode("utf-8-sig", "replace")
        try:
            out.append((core.check_answers(text.splitlines()), key))
        except (ValueError, csv.Error):
            continue                        # named "check" but not a check
    return out


def _send(settings, method, path, data=None, ctype="application/octet-stream"):
    url, cid, secret = config(settings)
    if not (url and cid and secret):
        raise CloudError("set the Worker URL and the Access service token first")
    req = urllib.request.Request(url + path, data=data, method=method, headers={
        "CF-Access-Client-Id": cid, "CF-Access-Client-Secret": secret,
        "User-Agent": USER_AGENT, "Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read(300).decode("utf-8", "replace")
        if e.code in (401, 403):
            raise CloudError("the Worker refused the service token (%d): check the token "
                             "and the Access policy. %s" % (e.code, detail)) from None
        raise CloudError("the Worker answered %d: %s" % (e.code, detail)) from None
    except (urllib.error.URLError, OSError) as e:
        raise CloudError("can't reach %s: %s" % (url, getattr(e, "reason", e))) from None
    return body


# ------------------------------------------------------------ sync

class Result:
    def __init__(self):
        self.uploaded, self.unchanged, self.errors, self.history = [], 0, [], None
        self.templates = (0, 0)                 # (sent, received)
        self.series = 0                         # drive logs whose values went up

    def text(self):
        parts = ["%d file%s uploaded" % (len(self.uploaded), "" if len(self.uploaded) == 1
                                         else "s")]
        if any(self.templates):
            parts.append("templates: %d sent, %d received" % self.templates)
        if self.series:
            parts.append("%d drive%s into the time series" % (self.series, "" if
                                                               self.series == 1 else "s"))
        if self.history:
            parts.append("history: %d drives, %d faults" % self.history)
        if self.errors:
            parts.append("%d failed: %s" % (len(self.errors), self.errors[0]))
        return ", ".join(parts)


def sync_templates(settings, state, path=None):
    """Two-way merge of the saved templates with the cloud's. Each side is
    compared with what both looked like after the last sync (kept in the
    state): a template changed only here is sent, one changed only in the
    cloud is taken, deletions included; changed on both, this PC's wins.
    Returns (sent, received)."""
    path = path or core.templates_path()
    local = core.load_templates(path)
    base = state.get("templates_base") or {}
    entries = {t["name"]: t for t in request(settings, "GET", "/api/templates")
               .get("templates", []) if isinstance(t, dict) and t.get("name")}
    remote = {n: (None if t.get("deleted") else t.get("channels")) for n, t in entries.items()}
    merged, changes, received = dict(local), [], 0
    for name in sorted(set(local) | set(base) | set(remote)):
        mine, theirs, before = local.get(name), remote.get(name), base.get(name)
        if mine != before:
            changes.append({"name": name, "channels": mine})
        elif name in entries and theirs != before:
            received += 1
            if theirs is None:
                merged.pop(name, None)
            else:
                merged[name] = list(theirs)
    if changes:
        request(settings, "PUT", "/api/templates",
                json.dumps({"changes": changes}).encode(), "application/json")
    if merged != local:
        core.save_settings(merged, path)
    state["templates_base"] = merged
    return len(changes), received


def sync_series(settings, state, logs, result):
    """Upload the values of each drive log not yet in the time-series store
    (or changed since). Failures are noted and tried again next time."""
    files, sent = state.get("files", {}), state.setdefault("series", {})
    for rel, entry in sorted(files.items()):
        path = os.path.join(logs, rel)
        if not entry.get("sha256") or sent.get(rel) == entry["sha256"]:
            continue
        if "kind" not in entry:
            entry["kind"] = history.kind(path) if os.path.exists(path) else None
        if entry["kind"] != "drive" or not os.path.exists(path):
            continue
        try:
            built = series.build(path)
            chunks = [c for ch in built["channels"] for c in series.chunks(ch)]
            body = json.dumps({"start": built["start"], "approx": built["approx"],
                               "chunks": chunks}, separators=(",", ":")).encode()
            request(settings, "PUT", "/api/series/" + _key(rel), body, "application/json")
        except (CloudError, OSError, ValueError) as e:
            result.errors.append("%s (time series): %s" % (rel, e))
            continue
        sent[rel] = entry["sha256"]
        result.series += 1


FAULT_TEXTS = "ref/BMW_fault_code_texts.csv"   # where the Worker finds the fault texts


def sync_fault_texts(settings, state):
    """Send n63read's fault texts, for the logger board's fault reads (the
    board sends codes only); again only when this version's table differs.
    True if sent."""
    with open(core.resource_path(core.FAULT_CSV), "rb") as f:
        data = f.read()
    sha = hashlib.sha256(data).hexdigest()
    if state.get("fault_texts_sha256") == sha:
        return False
    request(settings, "PUT", "/api/files/" + FAULT_TEXTS, data, "text/csv")
    state["fault_texts_sha256"] = sha
    return True


def full_catalog():
    """Every channel the app knows: the DME's with bank 2 twins, and the
    other modules' (as cli._catalog)."""
    cat = core.load_catalog(core.DEFAULT_CSV)
    cat.update(core.load_module_catalogs())
    return core.add_derived(core.add_bank2(cat))


def sync_logger_catalog(settings, state, force=False):
    """Send the logger board's channel catalog (loggerchannels.catalog_payload:
    what it can decode, what the car answered here, the built-in tiers) for
    the logger channel page; again only when it changed. True if sent."""
    from n63read import loggerchannels
    # Building it takes seconds: only when the app, the answers or the
    # templates changed since it last went.
    key = [__version__] + [os.path.getmtime(p) if os.path.exists(p) else None
                           for p in (core.availability_path(), core.templates_path())]
    if not force and state.get("logger_catalog_key") == key:
        return False
    fast, medium = loggerchannels.tier_lists(core.load_templates())
    payload = loggerchannels.catalog_payload(full_catalog(), core.load_availability(),
                                             fast, medium)
    data = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    sha = hashlib.sha256(data).hexdigest()
    sent = False
    if force or state.get("logger_catalog_sha256") != sha:
        request(settings, "PUT", "/api/logger/catalog", data, "application/json")
        state["logger_catalog_sha256"] = sha
        sent = True
    state["logger_catalog_key"] = key
    return sent


def logger_page(settings):
    """The logger channel page's address on the Worker."""
    return config(settings)[0] + "/logger"


def _key(rel):
    return urllib.parse.quote(rel.replace(os.sep, "/"), safe="/")


def sync(settings, logs=None, state_file=None, progress=None, templates_file=None):
    """Upload what's new or changed, then the summaries. Raises CloudError
    when the Worker can't be reached at all; a file that fails is listed
    in the result and tried again next time."""
    logs = logs or core.logs_dir()
    state = load_state(state_file)
    files = state.setdefault("files", {})
    result = Result()
    todo = []
    for path in history._files(logs):
        rel = os.path.relpath(path, logs)
        st = os.stat(path)
        stamp = [st.st_size, int(st.st_mtime)]
        if files.get(rel, {}).get("stamp") == stamp:
            result.unchanged += 1
        elif history.kind(path) in UPLOADED:
            todo.append((rel, path, stamp))
    for i, (rel, path, stamp) in enumerate(todo, 1):
        with open(path, "rb") as f:
            data = f.read()
        sha = hashlib.sha256(data).hexdigest()
        if files.get(rel, {}).get("sha256") == sha:
            files[rel]["stamp"] = stamp             # touched, not changed
            result.unchanged += 1
            continue
        try:
            reply = request(settings, "PUT", "/api/files/" + _key(rel), data, "text/csv")
        except CloudError as e:
            if not result.uploaded and "can't reach" in str(e):
                save_state(state, state_file)
                raise                               # offline: try again later
            result.errors.append("%s: %s" % (rel, e))
            continue
        if reply.get("sha256") not in (None, sha):
            result.errors.append("%s: arrived damaged" % rel)
            continue
        files[rel] = {"stamp": stamp, "sha256": sha, "kind": history.kind(path)}
        result.uploaded.append(rel)
        save_state(state, state_file)
        if progress:
            progress(i, len(todo))
    try:
        result.templates = sync_templates(settings, state, templates_file)
    except CloudError as e:
        result.errors.append("templates: %s" % e)
    try:
        sync_fault_texts(settings, state)
    except CloudError as e:
        result.errors.append("fault texts: %s" % e)
    try:
        sync_logger_catalog(settings, state)
    except (CloudError, OSError, ValueError) as e:
        result.errors.append("logger catalog: %s" % e)
    save_state(state, state_file)
    sync_series(settings, state, logs, result)
    save_state(state, state_file)
    summary = history.scan(logs)
    payload = json.dumps({"source": source_name(settings), "drives": summary.drives,
                          "faults": summary.faults}, sort_keys=True).encode()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != state.get("history_sha256"):
        reply = request(settings, "PUT", "/api/history", payload, "application/json")
        state["history_sha256"] = digest
        result.history = (reply.get("drives", 0), reply.get("faults", 0))
    state["last_sync"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state, state_file)
    return result


class AutoSync:
    """sync() in the background: now and then, and soon after trigger().
    `status` says how the last one went, for the app to show."""

    def __init__(self, settings, logs=None, every=EVERY, first=FIRST_DELAY):
        self.settings, self.logs, self.every = settings, logs, every
        self.status = "not set up" if not configured(settings) else "waiting"
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._busy = threading.Lock()
        self._first = first
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def trigger(self):
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def enabled(self):
        return self.settings.get("cloud_auto", True) and configured(self.settings)

    def run_once(self):
        if not configured(self.settings):
            self.status = "not set up"
            return None
        with self._busy:
            self.status = "syncing..."
            try:
                result = sync(self.settings, self.logs)
            except (CloudError, OSError) as e:
                self.status = "not synced: %s" % e
                return None
            self.status = "synced %s: %s" % (time.strftime("%H:%M"), result.text())
            return result

    def send_logger_catalog(self):
        """The logger catalog now, if it changed (before opening the logger
        channel page). Raises CloudError."""
        with self._busy:
            state = load_state()
            sync_logger_catalog(self.settings, state)
            save_state(state)

    def _run(self):
        self._wake.wait(self._first)
        while not self._stop.is_set():
            self._wake.clear()
            if self.enabled():
                self.run_once()
            self._wake.wait(self.every)
