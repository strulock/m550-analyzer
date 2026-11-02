/**
 * The logger page's Board tab (GET /logger/board): the board's firmware
 * (running, published, where an update is), its card (free space, errors,
 * files waiting) and the commands sent to it (boardcmd.js): list a folder
 * on the card and delete from it, format it, upload now, update now,
 * restart. A build can be published from here too (PUT /api/logger/firmware,
 * its version read from the image), and its settings changed (its Wi-Fi,
 * Access token, access point and time zone: /api/logger/settings,
 * boardsettings.js; sent to it, never shown back). Talks to /api/logger,
 * /api/logger/commands and /api/logger/settings with the visitor's Access
 * login. No dependencies;
 * the script avoids backticks and template placeholders so it can sit in
 * this template.
 */

export const BOARD_PAGE = String.raw`<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>M550 Logger Board</title>
<link rel="icon" href="data:,">
<style>
:root {
  --bg: #f4f5f7; --panel: #fff; --fg: #1d2126; --muted: #5d6670; --rule: #d9dde2;
  --accent: #1f6feb; --accent-bg: #e8f0fe; --warn: #9a5b00; --warn-bg: #fff4e0;
  --ok: #1a7f37; --bad: #b42318;
  --mono: ui-monospace, "Cascadia Mono", Consolas, monospace;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) { :root {
  --bg: #121417; --panel: #1b1e23; --fg: #e4e7eb; --muted: #9aa3ad; --rule: #2e333a;
  --accent: #6aa5ff; --accent-bg: #1c2a40; --warn: #f0b35a; --warn-bg: #2d2416;
  --ok: #4fc06b; --bad: #ff7b6e;
  color-scheme: dark; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
.wrap { max-width: 1280px; margin: 0 auto; padding: 20px 16px 60px; display: grid; gap: 14px; }
.wrap > * { min-width: 0; }
h1 { font-size: 22px; margin: 0; }
h2 { font-size: 15px; margin: 0 0 8px; }
h3 { font-size: 13.5px; margin: 14px 0 6px; }
.muted { color: var(--muted); }
.card { background: var(--panel); border: 1px solid var(--rule); border-radius: 8px; padding: 12px 14px; }
.row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
button, select, input { font: inherit; color: var(--fg); background: var(--panel);
  border: 1px solid var(--rule); border-radius: 6px; padding: 6px 10px; }
button { cursor: pointer; }
button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
button.danger { border-color: var(--bad); color: var(--bad); }
button.danger.primary { background: var(--bad); color: #fff; }
button:disabled { opacity: 0.5; cursor: default; }
button.link { border: 0; background: none; color: var(--accent); padding: 0; }
label { display: inline-flex; gap: 6px; align-items: center; }
.hide { display: none !important; }
.msg { padding: 8px 12px; border-radius: 6px; background: var(--warn-bg); color: var(--warn); }
.msg.ok { background: var(--accent-bg); color: var(--accent); }
nav.tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--rule); }
nav.tabs a { padding: 8px 14px; color: var(--muted); text-decoration: none; border-bottom: 2px solid transparent; margin-bottom: -1px; }
nav.tabs a[aria-current] { color: var(--fg); border-bottom-color: var(--accent); font-weight: 600; }
details.card > summary { cursor: pointer; list-style: none; display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
details.card > summary::-webkit-details-marker { display: none; }
details.card > summary::before { content: "\25B8"; color: var(--muted); width: 1em; }
details.card[open] > summary::before { content: "\25BE"; }
details.card > summary h2 { display: inline; margin: 0; }
details.card[open] > summary { margin-bottom: 8px; }
.foldstate { font-size: 12.5px; color: var(--muted); }
.foldstate.warn { color: var(--warn); }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-bottom: 10px; }
.stat span { display: block; font-size: 12px; color: var(--muted); }
.stat b { font-size: 16px; font-variant-numeric: tabular-nums; word-break: break-word; }
.stat small { display: block; color: var(--muted); }
.stat.warn b { color: var(--warn); } .stat.bad b { color: var(--bad); } .stat.ok b { color: var(--ok); }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; }
th { text-align: left; font-size: 12px; color: var(--muted); font-weight: 600; padding: 6px 8px;
  border-bottom: 1px solid var(--rule); white-space: nowrap; }
td { padding: 6px 8px; border-bottom: 1px solid var(--rule); vertical-align: top; }
td.n, th.n { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
td.f { font-family: var(--mono); font-size: 12.5px; word-break: break-all; }
td.when { white-space: nowrap; }
.path { font-family: var(--mono); font-size: 13px; }
.path button.link { font-family: var(--mono); }
.st-done { color: var(--ok); } .st-failed { color: var(--bad); } .st-waiting, .st-sent { color: var(--warn); }
.st-cancelled { color: var(--muted); }
.box { border: 1px solid var(--rule); border-radius: 6px; padding: 10px; margin-top: 10px; display: grid; gap: 8px; }
.box.danger { border-color: var(--bad); }
.note { font-size: 12.5px; color: var(--muted); margin: 8px 0 0; }
.setgrid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 8px 14px; margin: 10px 0; }
.setgrid label { display: flex; flex-direction: column; align-items: stretch; gap: 3px; font-size: 13px; }
@media (max-width: 640px) {
  .card { padding: 10px; }
  td, th { padding: 6px 4px; }
  td.by, th.by { display: none; }
}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Board</h1>
    <div class="muted" id="info">Loading…</div>
  </header>
  <nav class="tabs"><a href="/logger">Logger</a><a href="/logger/health">Health</a><a href="/logger/board" aria-current="page">Board</a></nav>

  <div id="msg" class="msg hide" role="alert"></div>

  <details class="card" id="foldFw" open>
    <summary><h2>Firmware</h2><span class="foldstate" id="fwFold"></span></summary>
    <div class="stats" id="fwStats"></div>
    <div class="row">
      <button id="fwUpdate">Update now</button>
      <button id="restart">Restart the board</button>
    </div>
    <p class="note">The board looks for a new build every 10 minutes on Wi-Fi, downloads it to its card
    and installs it at the next power-on, or after a minute idle away from the car. <b>Update now</b> looks
    at once and installs as soon as it isn't logging. A new build is kept only once it checks in; one that
    doesn't goes back to the one before.</p>
    <div class="box">
      <b>Publish a build</b>
      <div class="row">
        <input type="file" id="fwFile" accept=".bin,application/octet-stream">
        <label>Version <input id="fwVersion" placeholder="20261101-0553-31194c6" size="24" autocomplete="off"></label>
        <button id="fwPublish" class="primary" disabled>Publish…</button>
      </div>
      <div class="note" style="margin:0">The image from <span class="path">firmware/logger/.pio/build/waveshare-s3-eth/firmware.bin</span>,
      built with its version (<span class="path">firmware/tools/publish_firmware.py</span> does both). The version
      is read from the image; it must be the one built in, or the board would take it again and again.</div>
    </div>
  </details>

  <details class="card" id="foldCard" open>
    <summary><h2>SD card</h2><span class="foldstate" id="cardFold"></span></summary>
    <div class="stats" id="cardStats"></div>
    <div class="row">
      <button id="upload">Upload now</button>
      <button id="list">List files</button>
      <button id="formatOpen" class="danger">Format the card…</button>
    </div>
    <div id="browser" class="hide">
      <h3>Files <span class="path" id="crumbs"></span></h3>
      <div id="listState" class="muted"></div>
      <div class="scroll"><table id="files"></table></div>
    </div>
    <div class="box danger hide" id="formatBox">
      <b>Format the card</b>
      <div>Erases everything on it for good: the drives not yet uploaded (unless none wait), the channel and
      check lists (downloaded again) and anything else on it. The board does it between drives, then carries on.</div>
      <label><input type="checkbox" id="discard"> Even if files are still waiting to upload (they're lost)</label>
      <div class="row">
        <label>Type FORMAT <input id="formatWord" autocomplete="off" size="10"></label>
        <button id="formatGo" class="danger primary" disabled>Format</button>
        <button id="formatClose">Cancel</button>
      </div>
    </div>
  </details>

  <details class="card" id="foldSet">
    <summary><h2>Settings</h2><span class="foldstate" id="setFold"></span></summary>
    <div id="setState" class="muted">Loading…</div>
    <div class="setgrid">
      <label>Wi-Fi network <input id="set_wifi_ssid" autocomplete="off"></label>
      <label>Its password <input id="set_wifi_pass" type="password" autocomplete="new-password"></label>
      <label>Backup network <input id="set_wifi2_ssid" autocomplete="off"></label>
      <label>Its password <input id="set_wifi2_pass" type="password" autocomplete="new-password"></label>
      <label>Access token's client ID <input id="set_access_id" autocomplete="off" placeholder="….access"></label>
      <label>Its client secret <input id="set_access_secret" type="password" autocomplete="new-password"></label>
      <label>Setup access point's password <input id="set_ap_pass" type="password" autocomplete="new-password"></label>
      <label>Time zone (POSIX) <input id="set_tz" autocomplete="off" placeholder="CST6CDT,M3.2.0,M11.1.0"></label>
    </div>
    <div class="row">
      <button id="setSave">Send to the board…</button>
      <button id="setCancel" class="hide">Take the change back</button>
      <label><input type="checkbox" id="setClear2"> Remove the backup network</label>
    </div>
    <p class="note">Blank fields keep what the board has.
    The board takes a change at its next check-in, and then the passwords are dropped here; none is
    ever shown. A new token is tried before the board keeps it, so make the new one in Zero Trust
    (Access &gt; Service credentials, added to the application's Service Auth policy), wait for
    "taken", and only then revoke the old. A board that can't check in is set up over USB
    (firmware/tools/provision.py) or from its access point's page.</p>
  </details>

  <details class="card" id="foldCmds" open>
    <summary><h2>Commands</h2><span class="foldstate" id="cmdFold"></span></summary>
    <div class="scroll"><table id="cmds"></table></div>
    <p class="note">The board gets a command at its next check-in (every 10 s on Wi-Fi), does it once,
    and does what touches the card (delete, format) or restarts it only between drives.</p>
  </details>
</div>

<script>
(function () {
  var $ = function (id) { return document.getElementById(id); };
  var status = null, cmds = [], path = "/", waitingList = 0;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; });
  }
  function say(text, ok) { var m = $("msg"); m.textContent = text || ""; m.className = "msg" + (ok ? " ok" : "") + (text ? "" : " hide"); }
  function api(method, path, body, raw) {
    return fetch(path, { method: method, credentials: "same-origin",
      headers: raw ? { "content-type": "application/octet-stream" } : body ? { "content-type": "application/json" } : {},
      body: raw || (body ? JSON.stringify(body) : undefined) })
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) throw new Error(j.error || ("HTTP " + r.status)); return j; }); });
  }
  function local(iso) { return iso ? new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "–"; }
  function ago(s) {
    return s < 60 ? Math.round(s) + " s ago" : s < 7200 ? Math.round(s / 60) + " min ago"
      : s < 172800 ? Math.round(s / 3600) + " h ago" : Math.round(s / 86400) + " days ago";
  }
  function bytes(n) {
    if (n == null) return "–";
    return n >= 1e9 ? (n / 1e9).toFixed(1) + " GB" : n >= 1e6 ? (n / 1e6).toFixed(1) + " MB"
      : n >= 1e3 ? (n / 1e3).toFixed(0) + " KB" : n + " B";
  }
  function stat(label, value, sub, cls) {
    return '<div class="stat' + (cls ? " " + cls : "") + '"><span>' + esc(label) + "</span><b>" + esc(value) + "</b>" +
      (sub ? "<small>" + esc(sub) + "</small>" : "") + "</div>";
  }
  // The folds stay as they were left (this browser only).
  ["foldFw", "foldCard", "foldSet", "foldCmds"].forEach(function (id) {
    try { var v = localStorage.getItem("board." + id); if (v) $(id).open = v === "1"; } catch (e) {}
    $(id).addEventListener("toggle", function () {
      try { localStorage.setItem("board." + id, $(id).open ? "1" : "0"); } catch (e) {}
    });
  });

  function send(kind, extra, done) {
    var body = { kind: kind };
    for (var k in extra || {}) body[k] = extra[k];
    return api("POST", "/api/logger/commands", body).then(function (c) {
      say("Sent: " + describe(c) + ". The board gets it at its next check-in.", true);
      if (done) done(c);
      return refresh();
    }).catch(function (e) { say(e.message); });
  }
  function describe(c) {
    if (c.kind === "list") return "list " + c.arg;
    if (c.kind === "delete") return "delete " + c.arg;
    if (c.kind === "format") return "format the card" + (c.arg === "discard" ? ", waiting files and all" : "");
    return { upload: "upload now", update: "update now", restart: "restart" }[c.kind] || c.kind;
  }

  // ---- firmware
  function renderFirmware() {
    var b = (status && status.last) || {}, pub = status && status.firmware;
    var running = b.fw || "–", state, cls = "";
    if (!b.fw) state = "not known yet";
    else if (b.fw === "dev") { state = "a USB build: takes no updates"; cls = "warn"; }
    else if (b.ota) { state = b.ota; cls = /fail|went back/.test(b.ota) ? "bad" : "warn"; }
    else if (!pub) state = "nothing published";
    else if (pub.version === b.fw) { state = "up to date"; cls = "ok"; }
    else { state = "downloads within 10 min on Wi-Fi"; cls = "warn"; }
    $("fwStats").innerHTML =
      stat("Running", running, status && status.last ? "as of " + local(status.last.received) : "") +
      stat("Published", pub ? pub.version : "none", pub ? local(pub.uploaded) + ", " + bytes(pub.size) : "") +
      stat("Update", state, "", cls);
    $("fwFold").textContent = running + (cls === "ok" ? ", up to date" : pub && pub.version !== b.fw ? ", " + pub.version + " published" : "");
    $("fwFold").className = "foldstate" + (cls === "warn" || cls === "bad" ? " warn" : "");
  }
  $("fwUpdate").onclick = function () { send("update"); };
  $("restart").onclick = function () {
    if (confirm("Restart the board? It waits until it isn't logging.")) send("restart");
  };
  var image = null;
  $("fwFile").onchange = function () {
    var f = $("fwFile").files[0];
    image = null;
    $("fwPublish").disabled = true;
    if (!f) return;
    f.arrayBuffer().then(function (buf) {
      var bytesIn = new Uint8Array(buf);
      if (bytesIn[0] !== 0xE9) { say("That isn't an ESP32 app image (firmware.bin)."); return; }
      image = buf;
      var text = new TextDecoder("latin1").decode(bytesIn);
      var m = text.match(/20\d{6}-\d{4}-[0-9a-f]{7}(-dirty)?/);
      $("fwVersion").value = m ? m[0] : "";
      if (!m) say("No version found in the image: type the one it was built with.");
      else say("");
      $("fwPublish").disabled = false;
    });
  };
  $("fwPublish").onclick = function () {
    var v = $("fwVersion").value.trim();
    if (!image || !v) return;
    if (!confirm("Publish " + v + " (" + bytes(image.byteLength) + ")? The board takes it within 10 minutes on Wi-Fi.")) return;
    $("fwPublish").disabled = true;
    api("PUT", "/api/logger/firmware?version=" + encodeURIComponent(v), null, image).then(function (m) {
      say("Published " + m.version + ".", true);
      return refresh();
    }).catch(function (e) { say(e.message); }).then(function () { $("fwPublish").disabled = false; });
  };

  // ---- card
  function renderCard() {
    var b = (status && status.last) || {};
    var free = b.sd_free_mb, errs = b.card_errors, pend = b.pending;
    var listing = latestListing();
    var size = listing && listing.card ? listing.card.size_mb : null;
    $("cardStats").innerHTML =
      stat("Free", free == null ? "no card" : bytes(free * 1e6), size ? "of " + bytes(size * 1e6) + (listing.card.type ? ", " + listing.card.type : "") : "",
        free == null ? "bad" : free < 1000 ? "warn" : "") +
      stat("Card errors", errs == null ? "–" : errs, "this power-on", errs > 0 ? "warn" : "") +
      stat("Waiting to upload", pend == null ? "–" : pend,
        b.oldest_pending_min != null ? "oldest " + Math.round(b.oldest_pending_min) + " min" : "", pend > 0 ? "warn" : "") +
      stat("Uploaded", b.uploaded == null ? "–" : b.uploaded, "this power-on" + (b.up_failed ? ", " + b.up_failed + " failed" : "")) +
      (b.up_error ? stat("Last upload error", b.up_error, Math.round(b.up_error_min || 0) + " min before that check-in", "warn") : "");
    $("cardFold").textContent = free == null ? "" : bytes(free * 1e6) + " free" + (errs ? ", " + errs + " errors" : "") +
      (pend ? ", " + pend + " waiting" : "");
    $("cardFold").className = "foldstate" + (errs || free == null ? " warn" : "");
  }
  function latestListing(forPath) {
    for (var i = 0; i < cmds.length; i++) {
      var c = cmds[i];
      if (c.kind === "list" && c.listing && (!forPath || c.arg === forPath)) return c.listing;
    }
    return null;
  }
  function pendingList(forPath) {
    for (var i = 0; i < cmds.length; i++) {
      var c = cmds[i];
      if (c.kind === "list" && c.arg === forPath && (c.state === "waiting" || c.state === "sent")) return c;
    }
    return null;
  }
  function crumbs() {
    var parts = path.split("/").filter(Boolean), html = '<button class="link" data-p="/">/</button>', at = "";
    parts.forEach(function (p, i) {
      at += "/" + p;
      html += (i ? "/" : "") + '<button class="link" data-p="' + esc(at) + '">' + esc(p) + "</button>";
    });
    $("crumbs").innerHTML = html;
  }
  $("crumbs").onclick = function (e) { var p = e.target.getAttribute("data-p"); if (p) open(p); };
  function open(p) {
    path = p;
    $("browser").classList.remove("hide");
    crumbs();
    renderFiles();
    if (!latestListing(p) && !pendingList(p)) send("list", { path: p });
  }
  function join(name) { return (path === "/" ? "" : path) + "/" + name; }
  function renderFiles() {
    if ($("browser").classList.contains("hide")) return;
    var l = latestListing(path), p = pendingList(path), wait = (status && status.last && status.last.cmd_next_why) || "";
    var stateText = p ? "Asked for " + local(p.requested) + "; " + (p.state === "sent" ? "the board has it" + (wait ? " (" + wait + ")" : "") : "waiting for the board's next check-in") + "…" : "";
    if (!l) {
      $("listState").innerHTML = esc(stateText || "Not listed yet.") + ' <button class="link" id="relist">List it</button>';
      $("files").innerHTML = "";
    } else {
      var listed = null;
      for (var i = 0; i < cmds.length; i++) if (cmds[i].kind === "list" && cmds[i].listing === l) { listed = cmds[i]; break; }
      $("listState").innerHTML = esc((l.count > l.entries.length ? l.entries.length + " of " + l.count + " entries" : l.entries.length + " entries") +
        (listed ? ", as of " + local(listed.done) : "") + (stateText ? ". " + stateText : ". ")) + ' <button class="link" id="relist">Refresh</button>';
      var rows = '<tr><th>Name</th><th class="n">Size</th><th></th></tr>';
      if (path !== "/") rows += '<tr><td class="f"><button class="link" data-open="' + esc(path.replace(/\/[^\/]*$/, "") || "/") + '">..</button></td><td></td><td></td></tr>';
      l.entries.forEach(function (e) {
        var full = join(e[0]);
        rows += "<tr><td class=\"f\">" + (e[2] ? '<button class="link" data-open="' + esc(full) + '">' + esc(e[0]) + "/</button>" : esc(e[0])) +
          '</td><td class="n">' + (e[2] ? "" : bytes(e[1])) + '</td><td class="n"><button class="link" data-del="' + esc(full) + '" data-dir="' + (e[2] ? 1 : 0) + '">Delete</button></td></tr>';
      });
      $("files").innerHTML = rows;
    }
    var r = $("relist");
    if (r) r.onclick = function () { send("list", { path: path }); };
  }
  $("files").onclick = function (e) {
    var o = e.target.getAttribute("data-open"), d = e.target.getAttribute("data-del");
    if (o) open(o);
    if (d && confirm("Delete " + d + (e.target.getAttribute("data-dir") === "1" ? " and everything in it" : "") +
      " from the card? It can't be undone.")) {
      send("delete", { path: d }, function () { send("list", { path: path }); });
    }
  };
  $("list").onclick = function () { open(path); if (latestListing(path)) send("list", { path: path }); };
  $("upload").onclick = function () { send("upload"); };
  $("formatOpen").onclick = function () { $("formatBox").classList.remove("hide"); $("formatWord").focus(); };
  $("formatClose").onclick = function () { $("formatBox").classList.add("hide"); $("formatWord").value = ""; $("formatGo").disabled = true; };
  $("formatWord").oninput = function () { $("formatGo").disabled = $("formatWord").value !== "FORMAT"; };
  $("formatGo").onclick = function () {
    send("format", { discard: $("discard").checked }, function () { $("formatClose").onclick(); });
  };

  // ---- settings: what its last check-in says it has, the change waiting
  // for it, or how the last one went. Never a value.
  var SET_KEYS = ["wifi_ssid", "wifi_pass", "wifi2_ssid", "wifi2_pass", "access_id", "access_secret",
    "ap_pass", "tz"];
  var settings = null;
  function renderSettings(j) {
    settings = j;
    var b = j.board, parts = [];
    if (!b) parts.push("The board hasn't checked in yet.");
    else if (b.settings_version === null) parts.push("The board's firmware (" + esc(b.fw || "?") + ") keeps its settings built in; update it to change them here.");
    else {
      parts.push("The board has networks <b>" + esc(b.networks || "none") + "</b>, token " +
        esc(b.token_id ? b.token_id + "…" : "none") + ", settings change " + esc(String(b.settings_version)) +
        " (checked in " + esc(local(b.received)) + ").");
    }
    if (j.pending) {
      parts.push("Waiting for its next check-in: " + esc(j.pending.keys.join(", ")) + " (asked by " +
        esc(j.pending.by) + ", " + esc(local(j.pending.requested)) + ").");
    } else if (j.last) {
      parts.push("Last change: " + esc(j.last.result) + ", " + esc(local(j.last.done)) + ".");
    }
    $("setState").innerHTML = parts.join(" ");
    var refused = j.last && /^refused/.test(j.last.result);
    $("setFold").textContent = j.pending ? "A change is waiting for the board" :
      refused ? "Last change refused" : b && b.networks ? b.networks.replace(",", ", ") : "";
    $("setFold").className = "foldstate" + (j.pending || refused ? " warn" : "");
    $("setCancel").className = j.pending ? "" : "hide";
  }
  function loadSettings() {
    return api("GET", "/api/logger/settings").then(renderSettings).catch(function (e) {
      $("setState").textContent = "Couldn't load the board's settings: " + e.message; });
  }
  $("setSave").onclick = function () {
    var change = {};
    SET_KEYS.forEach(function (k) { var v = $("set_" + k).value; if (v !== "") change[k] = v; });
    if ($("setClear2").checked) { change.wifi2_ssid = ""; change.wifi2_pass = ""; }
    var keys = Object.keys(change);
    if (!keys.length) { say("Nothing to send: fill in what should change."); return; }
    if (!confirm("Send to the board at its next check-in: " + keys.join(", ") + "?")) return;
    api("PUT", "/api/logger/settings", change).then(function (j) {
      SET_KEYS.forEach(function (k) { $("set_" + k).value = ""; });
      $("setClear2").checked = false;
      renderSettings(j);
      say("Sent: the board takes it at its next check-in (every 10 s while it has Wi-Fi).", true);
    }).catch(function (e) { say(e.message); });
  };
  $("setCancel").onclick = function () {
    if (!confirm("Take back the change the board hasn't had yet?")) return;
    api("DELETE", "/api/logger/settings").then(function (j) { renderSettings(j); say("Taken back.", true); })
      .catch(function (e) { say(e.message); });
  };

  // ---- commands
  function renderCmds() {
    var b = (status && status.last) || {};
    if (!cmds.length) { $("cmds").innerHTML = '<tr><td class="muted">None sent yet.</td></tr>'; $("cmdFold").textContent = ""; return; }
    var rows = '<tr><th>Asked</th><th>Command</th><th>State</th><th class="by">By</th><th></th></tr>', busy = 0;
    cmds.forEach(function (c) {
      var st = c.state, text = st;
      if (st === "waiting") { text = "waiting for its next check-in"; busy++; }
      if (st === "sent") { text = "with the board" + (b.cmd_next === c.id && b.cmd_next_why ? ": " + b.cmd_next_why : ""); busy++; }
      if (st === "done" || st === "failed") text = (st === "done" ? "done" : "failed") + (c.result ? ": " + c.result : "") + " (" + local(c.done) + ")";
      rows += '<tr><td class="when">' + esc(local(c.requested)) + "</td><td>" + esc(describe(c)) +
        '</td><td class="st-' + st + '">' + esc(text) + '</td><td class="by muted">' + esc(c.by || "") + "</td><td>" +
        (st === "waiting" || st === "sent" ? '<button class="link" data-cancel="' + c.id + '">Cancel</button>' : "") + "</td></tr>";
    });
    $("cmds").innerHTML = rows;
    $("cmdFold").textContent = busy ? busy + " not done yet" : "";
  }
  $("cmds").onclick = function (e) {
    var id = e.target.getAttribute("data-cancel");
    if (id) api("DELETE", "/api/logger/commands?id=" + id).then(refresh).catch(function (err) { say(err.message); });
  };

  function refresh() {
    return Promise.all([api("GET", "/api/logger?hours=1"), api("GET", "/api/logger/commands?limit=30")])
      .then(function (r) {
        status = r[0];
        cmds = r[1].commands || [];
        var b = status.last;
        $("info").textContent = !b ? "The board hasn't checked in yet." :
          (status.online ? "Online" : "Offline") + ", last check-in " + ago(status.seen_s_ago) + " (" + local(b.received) + ")" +
          (b.state ? ", " + b.state : "") + (b.wifi ? ", on " + b.wifi : "");
        renderFirmware();
        renderCard();
        renderFiles();
        renderCmds();
      }).catch(function (e) { say(e.message); });
  }
  refresh();
  loadSettings();
  setInterval(function () {
    if (document.hidden) return;
    refresh();
    if (settings && settings.pending) loadSettings();
  }, 5000);
})();
</script>
</body>
</html>
`;
