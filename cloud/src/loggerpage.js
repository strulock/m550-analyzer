/**
 * The logger page (GET /logger; its Health tab is healthpage.js): the
 * board's last check-in (state, speed, coolant and oil temperature with an
 * overheating warning, the car on a map), each tank of fuel since its
 * fill-up (/api/logger/tanks), the full channel sweep (/api/logger/sweep,
 * sweep.js), the board's settings (its
 * Wi-Fi, Access token, access point and time zone: /api/logger/settings,
 * boardsettings.js; sent to it, never shown back), and every
 * channel the board can read, filtered to what the car answers, a tier for
 * each, saved as presets, with each channel's newest stored value and a flag
 * when it looks wrong (/api/logger/values; values.js, oddValue's source
 * embedded). It talks to /api/logger, /api/logger/catalog and
 * /api/logger/presets (logger.js) with the visitor's Access login, and
 * estimates the rates the selection gets (estimate.js, its source
 * embedded). No dependencies; the map is OpenStreetMap's embed. The script
 * avoids backticks and template placeholders so it can sit in this template.
 */

import { estimateRates } from "./estimate.js";
import { BOARD_NEEDS, neverChanged, PER_DRIVE, PER_DRIVE_RE, perDrive, UNDECODABLE, VALUE_KEEP,
  valueKept } from "./logger.js";
import { FILL_MIN_L } from "./tanks.js";
import { oddValue } from "./values.js";

const FILL_NOTE = `${FILL_MIN_L} litres (${(FILL_MIN_L / 3.785411784).toFixed(1)} gallons)`;

export const LOGGER_PAGE = String.raw`<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>M550 Logger</title>
<link rel="icon" href="data:,">
<style>
:root {
  --bg: #f4f5f7; --panel: #fff; --fg: #1d2126; --muted: #5d6670; --rule: #d9dde2;
  --accent: #1f6feb; --accent-bg: #e8f0fe; --warn: #9a5b00; --warn-bg: #fff4e0;
  --ok: #1a7f37; --bad: #b42318;
  --t-fast: #c2410c; --t-medium: #b7791f; --t-slow: #2f6f4f; --t-drive: #6d4fc2;
  --t-daily: #2563a8; --t-weekly: #0f766e; --t-monthly: #7c5a3a; --t-off: #8a939c;
  --mono: ui-monospace, "Cascadia Mono", Consolas, monospace;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) { :root {
  --bg: #121417; --panel: #1b1e23; --fg: #e4e7eb; --muted: #9aa3ad; --rule: #2e333a;
  --accent: #6aa5ff; --accent-bg: #1c2a40; --warn: #f0b35a; --warn-bg: #2d2416;
  --ok: #4fc06b; --bad: #ff7b6e;
  --t-fast: #ff8a4c; --t-medium: #e8b44d; --t-slow: #5fbf8e; --t-drive: #a58bff;
  --t-daily: #6aa5ff; --t-weekly: #3ec7b8; --t-monthly: #c9a27c; --t-off: #6c757e;
  color-scheme: dark; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
.wrap { max-width: 1280px; margin: 0 auto; padding: 20px 16px 60px; display: grid; gap: 14px; }
.wrap > * { min-width: 0; }
h1 { font-size: 22px; margin: 0; }
.muted { color: var(--muted); }
.card { background: var(--panel); border: 1px solid var(--rule); border-radius: 8px; padding: 12px 14px; }
.row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
button, select, input[type=search] { font: inherit; color: var(--fg); background: var(--panel);
  border: 1px solid var(--rule); border-radius: 6px; padding: 6px 10px; }
button { cursor: pointer; }
button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
button:disabled { opacity: 0.5; cursor: default; }
input[type=search] { min-width: 240px; flex: 1; }
.badge { font-size: 12px; padding: 2px 8px; border-radius: 99px; background: var(--accent-bg); color: var(--accent); }
.badge.warn { background: var(--warn-bg); color: var(--warn); }
.tiers { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 8px; }
.tier { border-left: 4px solid var(--c); padding: 4px 8px; }
.tier b { display: block; font-size: 18px; font-variant-numeric: tabular-nums; }
.tier span { font-size: 12px; color: var(--muted); }
.tier span.est { display: block; margin-top: 2px; color: var(--fg); font-weight: 600; }
.tier span.est.warn { color: var(--warn); }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; }
th { text-align: left; font-size: 12px; color: var(--muted); font-weight: 600; padding: 6px 8px;
  border-bottom: 1px solid var(--rule); white-space: nowrap; position: sticky; top: 0; background: var(--panel); }
td { padding: 6px 8px; border-bottom: 1px solid var(--rule); vertical-align: top; }
td.name { font-family: var(--mono); font-size: 12.5px; word-break: break-all; min-width: 220px; }
td.name .info { font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; font-size: 12.5px;
  color: var(--muted); word-break: normal; }
td.id { font-family: var(--mono); font-size: 12px; white-space: nowrap; }
td.id small { display: block; color: var(--muted); font-size: 11px; }
.dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; }
.dot.ok { background: var(--ok); } .dot.no { background: transparent; border: 1.5px solid var(--muted); }
select.t { min-width: 140px; border-left: 4px solid var(--c); }
select.t.inherited { color: var(--muted); }
.setgrid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 8px 14px; margin: 10px 0; }
.setgrid label { display: flex; flex-direction: column; align-items: stretch; gap: 3px; font-size: 13px; }
tr.sel td { background: var(--accent-bg); }
.more { justify-self: center; }
.msg { padding: 8px 12px; border-radius: 6px; background: var(--warn-bg); color: var(--warn); }
.msg.ok { background: var(--accent-bg); color: var(--accent); }
.hide { display: none !important; }
nav.tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--rule); }
nav.tabs a { padding: 8px 14px; color: var(--muted); text-decoration: none; border-bottom: 2px solid transparent; margin-bottom: -1px; }
nav.tabs a[aria-current] { color: var(--fg); border-bottom-color: var(--accent); font-weight: 600; }
label { display: inline-flex; gap: 6px; align-items: center; }
h2 { font-size: 15px; margin: 0 0 8px; }
details.card > summary { cursor: pointer; list-style: none; display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
details.card > summary::-webkit-details-marker { display: none; }
details.card > summary::before { content: "\25B8"; color: var(--muted); width: 1em; }
details.card[open] > summary::before { content: "\25BE"; }
details.card > summary h2 { display: inline; margin: 0; }
details.card[open] > summary { margin-bottom: 8px; }
.foldstate { font-size: 12.5px; color: var(--muted); }
.foldstate.warn { color: var(--warn); }
.alarm { padding: 10px 14px; border-radius: 8px; background: var(--bad); color: #fff; font-weight: 600; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px; }
.stat span { display: block; font-size: 12px; color: var(--muted); }
.stat b { font-size: 18px; font-variant-numeric: tabular-nums; }
.stat small { color: var(--muted); }
.stat.warn b { color: var(--warn); } .stat.bad b { color: var(--bad); }
.map { width: 100%; height: 280px; border: 1px solid var(--rule); border-radius: 6px; margin-top: 10px; }
td.n, th.n { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
td.val { font-variant-numeric: tabular-nums; white-space: nowrap; }
td.val small { display: block; color: var(--muted); font-size: 11px; white-space: normal; max-width: 220px; }
td.val small.odd { color: var(--warn); font-weight: 600; }
td.val.odd b { color: var(--warn); }
td.code { font-family: var(--mono); font-size: 12px; white-space: nowrap; }
td.when { white-space: nowrap; }
td.when small { display: block; color: var(--muted); font-size: 11px; }
@media (max-width: 640px) {
  td.mod, th.mod, td.id, th.id, td.ans, th.ans { display: none; }
  td.val { white-space: normal; } td.val small { max-width: 110px; }
  td.name .info { overflow-wrap: anywhere; }
  input[type=search] { min-width: 0; flex-basis: 100%; }
  .card { padding: 10px; }
  td, th { padding: 6px 4px; }
  td.name { min-width: 0; }
  select.t { min-width: 0; max-width: 112px; padding: 6px 4px; }
}
</style>
</head>
<body>
<div class="wrap">
  <header class="row" style="justify-content:space-between">
    <div>
      <h1>Logger</h1>
      <div class="muted" id="catinfo">Loading the catalog…</div>
      <div class="muted" id="valinfo"></div>
    </div>
    <div class="muted" id="logger"></div>
  </header>
  <nav class="tabs"><a href="/logger" aria-current="page">Logger</a><a href="/logger/health">Health</a></nav>

  <div id="alarm" class="alarm hide" role="alert"></div>

  <section class="card" id="live">
    <h2 id="liveTitle">The car</h2>
    <div class="stats" id="liveStats"><span class="muted">Waiting for the logger's check-in…</span></div>
    <iframe id="map" class="map hide" title="The car's position" loading="lazy"></iframe>
    <div class="muted" id="mapNote" style="font-size:12px;margin-top:4px"></div>
  </section>

  <details class="card fold" id="foldFaults">
    <summary><h2>Faults</h2><span class="foldstate" id="faultsState"></span></summary>
    <div class="scroll"><table>
      <thead><tr><th>Code</th><th class="mod">Module</th><th>Text</th><th>First set</th>
        <th>Last set</th><th class="n">Times</th></tr></thead>
      <tbody id="faults"><tr><td colspan="6" class="muted">Waiting for the logger's check-in…</td></tr></tbody>
    </table></div>
    <p class="muted" style="font-size:12.5px;margin:8px 0 0">First and last set: the odometer the
    module stored with the fault (read while the car is in P), and the date the drives' odometer
    readings put it at. A blank: not read yet, or the module keeps no snapshot.</p>
  </details>

  <details class="card fold" id="foldTanks">
    <summary><h2>Tanks</h2><span class="foldstate" id="tanksState"></span></summary>
    <div class="scroll"><table>
      <thead><tr><th>Filled</th><th class="n">Drives</th><th class="n">Added</th><th class="n">Miles</th>
        <th class="n">Gallons</th><th class="n">MPG</th><th class="n">Level</th></tr></thead>
      <tbody id="tanks"><tr><td colspan="7" class="muted">Loading…</td></tr></tbody>
    </table></div>
    <p class="muted" style="font-size:12.5px;margin:8px 0 0">A tank starts when the level is up
    ${FILL_NOTE} between drives, as the trip meter is reset at a fill-up. Miles from the odometer;
    gallons from the engine's fuel flow (else the level's fall).</p>
  </details>

  <details class="card fold" id="foldSweep">
    <summary><h2>Full channel sweep</h2><span class="foldstate" id="sweepFold"></span></summary>
    <div class="row">
      <button id="sweepAsk">Sweep on the next drive…</button>
      <button id="sweepCancel" class="hide">Cancel the sweep</button>
      <span id="sweepState" class="muted">Loading…</span>
    </div>
    <div id="sweepWarn" class="msg hide" style="margin-top:8px"></div>
    <p class="muted" style="font-size:12.5px;margin:8px 0 0">The logger asks the car for every
    identifier in the catalog, whether it answered before or not, as the app's Answers &gt; Check car
    now does (read-only). It starts a minute into the next drive, alongside the fast channels, and
    a drive that ends first leaves the rest to the next. Whatever answers is logged from then on.</p>
  </details>


  <div id="msg" class="msg hide"></div>

  <section class="card row">
    <label>Preset <select id="preset"></select></label>
    <span id="activeBadge" class="badge hide">Active</span>
    <span id="dirtyBadge" class="badge warn hide">Unsaved changes</span>
    <button id="save" class="primary">Save</button>
    <button id="saveAs">Save as…</button>
    <button id="rename">Rename…</button>
    <button id="del">Delete</button>
    <button id="activate">Make active</button>
    <button id="revert">Revert</button>
    <label style="margin-left:auto">Channels not set, that the car answers:
      <select id="rest"><option value="slow">slow</option><option value="off">off</option></select></label>
  </section>

  <section class="card">
    <div class="tiers" id="summary"></div>
    <p class="muted" id="estNote" style="font-size:12.5px;margin:10px 0 0"></p>
  </section>

  <section class="card row">
    <input type="search" id="q" placeholder="Search names, descriptions, groups">
    <select id="mod"><option value="">All modules</option></select>
    <select id="ans">
      <option value="yes">The car answers</option>
      <option value="all">All channels</option>
      <option value="no">Doesn't answer / unknown</option>
    </select>
    <select id="tierf"><option value="">Any tier</option><option value="set">Set in this preset</option></select>
    <select id="valf" title="Never changed and Only goes up leave out what the board, the tanks, fault dates and Health read (the odometers, misfire counters, tank level, tires), so a slower tier for what they show costs nothing">
      <option value="">Any value</option>
      <option value="odd">Looks wrong</option>
      <option value="has">Has a stored value</option>
      <option value="none">No stored value</option>
      <option value="same">Never changed (2+ stored drives)</option>
      <option value="rises">Only goes up (counters; 3+ drives)</option>
    </select>
  </section>

  <section class="card row" id="bulk">
    <label><input type="checkbox" id="all"> <span id="allText">Select all matching</span></label>
    <span class="muted" id="selText"></span>
    <select id="bulkTier"></select>
    <button id="apply" disabled>Set tier</button>
    <button id="clearSel" disabled>Clear selection</button>
  </section>

  <section class="card">
    <div class="muted" id="count" style="margin-bottom:6px"></div>
    <div class="scroll"><table>
      <thead><tr><th></th><th>Channel</th><th class="mod">Module</th><th class="id">Identifier</th>
        <th title="The newest value stored from any drive">Last value</th>
        <th class="ans" title="The car answered this identifier">Ans.</th><th>Tier</th></tr></thead>
      <tbody id="rows"></tbody>
    </table></div>
    <div class="row" style="justify-content:center;margin-top:10px"><button id="more" class="more hide">Show more</button></div>
  </section>

  <p class="muted">Fast: every cycle. Medium: a few times a second. Slow: in rotation, a row of the sweep file
  each pass. Once a drive, daily, weekly, monthly: read once, in the first drive they're due, into that
  drive's sweep file. Channels of one identifier are read together, at the fastest tier any of them has.
  The logger downloads the active preset on its next Wi-Fi check and uses it from its next drive.</p>
  <details class="card fold" id="foldSettings">
    <summary><h2>Board settings</h2><span class="foldstate" id="setFold"></span></summary>
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
    <p class="muted" style="font-size:12.5px;margin:8px 0 0">Blank fields keep what the board has.
    The board takes a change at its next check-in, and then the passwords are dropped here; none is
    ever shown. A new token is tried before the board keeps it, so make the new one in Zero Trust
    (Access &gt; Service credentials, added to the application's Service Auth policy), wait for
    "taken", and only then revoke the old. A board that can't check in is set up over USB
    (firmware/tools/provision.py) or from its access point's page.</p>
  </details>
</div>

<script>
// The functions below are the Worker's own, as text. Wrangler's bundler
// (esbuild, keeping names) wraps the functions inside them in __name(fn,
// "name"), which a browser hasn't got: without this the page stops with
// "__name is not defined" and loads nothing.
var __name = function (fn) { return fn; };
${estimateRates.toString()}
var PER_DRIVE = ${JSON.stringify(PER_DRIVE)}, PER_DRIVE_RE = ${PER_DRIVE_RE.toString()};
var UNDECODABLE = ${JSON.stringify([...UNDECODABLE])}, BOARD_NEEDS = ${JSON.stringify(BOARD_NEEDS)};
${perDrive.toString()}
var VALUE_KEEP = ${JSON.stringify(VALUE_KEEP)};
${valueKept.toString()}
${neverChanged.toString()}
${oddValue.toString()}
</script>
<script>
(function () {
  "use strict";
  var TIERS = ["fast", "medium", "slow", "drive", "daily", "weekly", "monthly", "off"];
  var LABEL = { fast: "Fast", medium: "Medium", slow: "Slow", drive: "Once a drive", daily: "Daily",
    weekly: "Weekly", monthly: "Monthly", off: "Off" };
  var HINT = { fast: "every cycle", medium: "a few a second", slow: "in rotation", drive: "each drive",
    daily: "first drive a day", weekly: "every 7 days", monthly: "every 30 days", off: "not read" };
  var PAGE = 200;
  var $ = function (id) { return document.getElementById(id); };
  var cat = null, items = [], presets = [], activeName = null, file = null;
  var edit = null, savedJson = "", sel = new Set(), shown = PAGE, matches = [];
  var live = {};                 // from the logger's last check-in: {batches, answers, from}
  var achieved = null;           // the last drive's rates per tier: {rate_fast, rate_medium, slow_pass_s}
  var values = null;             // channel: [t, value, n, lo, hi, drives], once loaded

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; });
  }
  function color(t) { return "var(--t-" + t + ")"; }
  function say(text, ok) { var m = $("msg"); m.textContent = text; m.className = "msg" + (ok ? " ok" : "") + (text ? "" : " hide"); }
  function api(method, path, body) {
    return fetch(path, { method: method, credentials: "same-origin",
      headers: body ? { "content-type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined })
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) throw new Error(j.error || ("HTTP " + r.status)); return j; }); });
  }

  // A channel's tier when the preset doesn't name it (logger.js tiersFor).
  function inherited(it) {
    if (!it.ok) return "off";
    return edit.rest === "off" && perDrive(it.name) ? "drive" : edit.rest;
  }
  // Off whatever the preset says: one that misdecodes on this car, or one its
  // module says it hasn't ("absent"); one the board's own logic reads at
  // least its tier there (logger.js tiersFor).
  function effective(it) {
    if (it.status === "absent" || UNDECODABLE.indexOf(it.key) >= 0) return "off";
    var tier = edit.tiers[it.name] || inherited(it), need = BOARD_NEEDS[it.name];
    return need && TIERS.indexOf(need) < TIERS.indexOf(tier) ? need : tier;
  }
  function isDirty() { return edit && JSON.stringify({ r: edit.rest, t: edit.tiers }) !== savedJson; }

  function loadPreset(name) {
    var p = presets.find(function (x) { return x.name === name; }) || presets[0];
    if (!p) { edit = { name: null, rest: "slow", tiers: {} }; savedJson = ""; return; }
    edit = { name: p.name, rest: p.rest, tiers: Object.assign({}, p.tiers) };
    savedJson = JSON.stringify({ r: p.rest, t: p.tiers });
  }

  function readTiers() {
    // Each identifier's read tier: the fastest of its channels'.
    var by = {};
    items.forEach(function (it) {
      var t = effective(it);
      if (t === "off") return;
      var cur = by[it.key];
      if (cur === undefined || TIERS.indexOf(t) < TIERS.indexOf(cur)) by[it.key] = t;
    });
    return by;
  }

  // Estimated rates (estimateRates, above): fast rows a second, each medium
  // identifier's reads a second (the slowest module's), a slow pass.
  function estimates(byId) {
    var mods = {};
    Object.keys(byId).forEach(function (k) {
      var t = byId[k], m = k.split(":")[0];
      if (t !== "fast" && t !== "medium" && t !== "slow") return;
      var n = mods[m] || (mods[m] = { fast: 0, medium: 0, slow: 0 });
      n[t]++;
    });
    return estimateRates(mods, live);
  }

  function rate(r) { return r >= 10 ? Math.round(r) : r >= 1 ? r.toFixed(1) : r.toFixed(2); }
  function duration(s) {
    return !isFinite(s) ? "never (no room)" : s < 90 ? Math.round(s) + " s" : s < 5400 ?
      Math.round(s / 60) + " min" : (s / 3600).toFixed(1) + " h";
  }

  // What the last drive got, after the estimate: the check against reality.
  function achievedText(t) {
    var a = achieved;
    if (!a) return "";
    if (t === "fast" && a.rate_fast != null) return "; last drive " + rate(a.rate_fast) + "/s";
    if (t === "medium" && a.rate_medium != null) return "; last drive " + rate(a.rate_medium) + "/s";
    if (t === "slow" && a.slow_pass_s != null) return "; last drive " + duration(a.slow_pass_s);
    return "";
  }

  function estimateLine(t, e) {
    var line = estimateFor(t, e);
    if (line) line.text += achievedText(t);
    return line;
  }

  function estimateFor(t, e) {
    var mod = function (m) { return esc(cat.modules[m] || m); };
    if (t === "fast") {
      return e.rowsPerS ? { text: "≈ " + rate(e.rowsPerS) + " rows/s", warn: e.rowsPerS < 8 } : null;
    }
    if (t === "medium" && e.mediumMin !== null) {
      return { text: "≈ " + rate(e.mediumMin) + "/s each, slowest " + mod(e.mediumSlowest), warn: e.mediumMin < 0.5 };
    }
    if (t === "slow" && e.slowest) {
      return { text: "≈ " + duration(e.slowPassS) + " a pass, slowest " + mod(e.slowest), warn: e.slowPassS > 300 };
    }
    if (t === "drive" || t === "daily" || t === "weekly" || t === "monthly") return { text: "once, from a minute in" };
    return null;
  }

  function renderSummary(byId) {
    var ch = {}, ids = {}, e = estimates(byId);
    TIERS.forEach(function (t) { ch[t] = 0; ids[t] = 0; });
    items.forEach(function (it) { ch[effective(it)]++; });
    Object.keys(byId).forEach(function (k) { ids[byId[k]]++; });
    $("summary").innerHTML = TIERS.map(function (t) {
      var est = ids[t] || t === "fast" ? estimateLine(t, e) : null;
      return '<div class="tier" style="--c:' + color(t) + '"><span>' + LABEL[t] + "</span><b>" +
        ch[t].toLocaleString() + "</b><span>" + (t === "off" ? HINT[t] : ids[t].toLocaleString() +
        " identifier" + (ids[t] === 1 ? "" : "s") + " · " + HINT[t]) + "</span>" +
        (est ? '<span class="est' + (est.warn ? " warn" : "") + '">' + est.text + "</span>" : "") + "</div>";
    }).join("");
    $("estNote").textContent = "Rates are estimates from a model of the logger's scheduler, fitted to a real drive" +
      (live.from ? ", with the request sizes and answer times from its check-in of " + live.from : "") +
      ". Every fast identifier slows every row; medium and slow share what's left of each request.";
  }

  function renderPresetBar() {
    $("preset").innerHTML = presets.map(function (p) {
      return '<option value="' + esc(p.name) + '"' + (edit && p.name === edit.name ? " selected" : "") + ">" +
        esc(p.name) + (p.name === activeName ? " (active)" : "") + "</option>";
    }).join("") || "<option>(none yet)</option>";
    $("rest").value = edit.rest;
    var isActive = edit.name && edit.name === activeName, dirty = isDirty();
    $("activeBadge").classList.toggle("hide", !isActive);
    $("dirtyBadge").classList.toggle("hide", !dirty);
    $("save").disabled = !dirty || !edit.name;
    $("revert").disabled = !dirty;
    $("activate").disabled = !edit.name || isActive;
    $("del").disabled = !edit.name || isActive;
    $("rename").disabled = !edit.name;
  }

  function filter() {
    var q = $("q").value.trim().toLowerCase(), mod = $("mod").value, ans = $("ans").value,
      tf = $("tierf").value, vf = $("valf").value;
    matches = items.filter(function (it) {
      if (mod && it.module !== mod) return false;
      if (vf === "odd" && !it.odd) return false;
      if (vf === "has" && !(values && values[it.name])) return false;
      if (vf === "none" && values && values[it.name]) return false;
      var sv = values && values[it.name];
      if ((vf === "same" || vf === "rises") && (!sv || valueKept(it.name))) return false;
      if (vf === "same" && !neverChanged(sv)) return false;
      if (vf === "rises" && !sv[6]) return false;
      if (ans === "yes" && !it.ok && !edit.tiers[it.name]) return false;
      if (ans === "no" && it.ok) return false;
      if (tf === "set" && !edit.tiers[it.name]) return false;
      if (tf && tf !== "set" && effective(it) !== tf) return false;
      if (q && it.hay.indexOf(q) < 0) return false;
      return true;
    });
  }

  function options(current, inherited) {
    var out = '<option value="">Default (' + LABEL[inherited] + ")</option>";
    TIERS.forEach(function (t) {
      out += '<option value="' + t + '"' + (current === t ? " selected" : "") + ">" + LABEL[t] + "</option>";
    });
    return out;
  }

  function fmt(v) {
    if (v == null) return "–";
    return Math.abs(v) >= 1000 ? Math.round(v).toLocaleString() : String(+v.toPrecision(5));
  }
  function ago(t) {
    var s = Date.now() / 1000 - t;
    return s < 120 ? "just now" : s < 7200 ? Math.round(s / 60) + " min ago"
      : s < 172800 ? Math.round(s / 3600) + " h ago" : Math.round(s / 86400) + " days ago";
  }
  // Why the newest value looks wrong, else whether the drives' range ever did.
  function markOdd() {
    items.forEach(function (it) {
      var s = values[it.name];
      if (!s) { it.odd = null; return; }
      var past = oddValue(s[3], it) || oddValue(s[4], it);
      it.odd = oddValue(s[1], it) || (past ? "at times: " + past : null);
    });
  }
  function valueCell(it) {
    var unit = esc(it.unit);
    var s = values && values[it.name];
    if (!s) return '<td class="val"><span class="muted">' + (values ? "– " : "") + unit + "</span></td>";
    var tip = "Recorded " + new Date(s[0] * 1000).toLocaleString() + ". " + s[2].toLocaleString() +
      " values in " + s[5] + " drive" + (s[5] === 1 ? "" : "s") + ", " + fmt(s[3]) + " to " + fmt(s[4]) +
      (it.unit ? " " + it.unit : "") + ".";
    var note = it.odd ? '<small class="odd">' + esc(it.odd) + "</small>"
      : s[2] >= 20 && s[5] >= 2 && s[3] === s[4] ? "<small>the same in all " + s[5] + " drives</small>"
      : "<small>" + ago(s[0]) + (s[3] !== s[4] ? "; " + fmt(s[3]) + " to " + fmt(s[4]) : "") + "</small>";
    return '<td class="val' + (it.odd ? " odd" : "") + '" title="' + esc(tip) + '"><b>' + fmt(s[1]) +
      "</b> " + unit + note + "</td>";
  }

  function renderRows(byId) {
    var html = [];
    matches.slice(0, shown).forEach(function (it) {
      var set = edit.tiers[it.name] || "", eff = effective(it), inh = inherited(it);
      var read = byId[it.key], readNote = read && read !== eff ? "<small>read " + LABEL[read].toLowerCase() + "</small>" : "";
      html.push('<tr data-i="' + it.i + '"' + (sel.has(it.i) ? ' class="sel"' : "") + ">" +
        '<td><input type="checkbox" class="pick"' + (sel.has(it.i) ? " checked" : "") + ' aria-label="Select"></td>' +
        '<td class="name"><div>' + esc(it.name) + '</div><div class="info">' + esc(it.info) + "</div></td>" +
        '<td class="mod">' + esc(it.modName) + "</td>" +
        '<td class="id">' + esc(it.did) + "<small>" + esc(it.group) + "</small>" + readNote + "</td>" +
        valueCell(it) +
        '<td class="ans"><span class="dot ' + (it.ok ? "ok" : "no") + '" title="' + esc(it.status || "not checked") + '"></span></td>' +
        '<td><select class="t' + (set ? "" : " inherited") + '" style="--c:' + color(eff) + '">' + options(set, inh) + "</select></td></tr>");
    });
    $("rows").innerHTML = html.join("");
    $("count").textContent = matches.length.toLocaleString() + " channel" + (matches.length === 1 ? "" : "s") +
      " match" + (matches.length > shown ? "; showing " + shown.toLocaleString() : "");
    $("more").classList.toggle("hide", matches.length <= shown);
    $("allText").textContent = "Select all " + matches.length.toLocaleString() + " matching";
    $("all").checked = matches.length > 0 && matches.every(function (it) { return sel.has(it.i); });
    $("selText").textContent = sel.size ? sel.size.toLocaleString() + " selected" : "";
    $("apply").disabled = !sel.size;
    $("clearSel").disabled = !sel.size;
  }

  function render(refilter) {
    if (!cat || !edit) return;
    if (refilter) { filter(); }
    var byId = readTiers();
    renderSummary(byId);
    renderPresetBar();
    renderRows(byId);
  }

  function setTier(names, tier) {
    names.forEach(function (n) { if (tier) edit.tiers[n] = tier; else delete edit.tiers[n]; });
  }

  function renderLogger() {
    api("GET", "/api/logger?hours=1").then(function (s) {
      var last = s.last;
      if (!last) { $("logger").textContent = "The logger hasn't checked in yet."; return; }
      var ago = s.seen_s_ago, when = ago < 90 ? ago + " s" : ago < 5400 ? Math.round(ago / 60) + " min" :
        ago < 172800 ? Math.round(ago / 3600) + " h" : Math.round(ago / 86400) + " days";
      // What the board measured: request sizes it's down to, answer times.
      var pairs = function (text) {
        var out = {};
        String(text || "").split(" ").forEach(function (p) {
          var kv = p.split(":"), v = Number(kv[1]);
          if (kv.length === 2 && v > 0) out[kv[0]] = v;
        });
        return out;
      };
      if (s.last_drive && (s.last_drive.rate_fast != null || s.last_drive.slow_pass_s != null)) {
        achieved = s.last_drive;
        render(false);
      }
      if (last.batches || last.answer_ms) {
        live = { batches: pairs(last.batches), answers: pairs(last.answer_ms),
          from: new Date(last.received).toLocaleString() };
        render(false);
      }
      var has = last.channels || "", text = "Logger " + (s.online ? "online" : "last seen " + when + " ago");
      if (!has) text += "; running a channel file from before downloads (needs the new firmware)";
      else if (file && has === file.etag) text += "; running the active preset ✓";
      else text += "; running " + has.slice(0, 8) + ", gets the active preset on its next Wi-Fi check";
      // Firmware: its own, what's published (over-the-air updates), what it's doing about it.
      if (last.fw) {
        text += "; firmware " + last.fw;
        var pub = s.firmware && s.firmware.version;
        if (last.ota) text += " (" + last.ota + ")";
        else if (pub && pub !== last.fw && last.fw !== "dev") text += ", " + pub + " published";
      }
      // Why it last couldn't join Wi-Fi (sent while off it and 10 minutes after).
      if (last.wifi_note) {
        var mins = Math.round(last.wifi_note_min || 0);
        text += "; Wi-Fi wouldn't join " + (mins < 1 ? "just before this check-in" :
          mins + " min before this check-in") + ": " + last.wifi_note;
      }
      $("logger").textContent = text;
      renderLive(s, when);
      renderSweep(s.sweep);
    }).catch(function () { $("logger").textContent = ""; });
  }

  // The full channel sweep (sweep.js): the one waiting or under way, the last one done.
  var sweep = null;
  function moduleNames(list) {
    return String(list || "").split(" ").filter(Boolean).map(function (m) {
      var name = cat && cat.modules[m];
      return name ? name + " (" + m + ")" : m;
    }).join(", ");
  }
  function counted(c) {
    c = c || {};
    var parts = [];
    if (c.ok) parts.push(c.ok.toLocaleString() + " answered");
    if (c.size) parts.push(c.size.toLocaleString() + " the wrong size");
    if (c.refused) parts.push(c.refused.toLocaleString() + " refused");
    if (c["no answer"]) parts.push(c["no answer"].toLocaleString() + " no answer");
    return parts.join(", ");
  }
  // The board's settings: what its last check-in says it has, the change
  // waiting for it, or how the last one went. Never a value.
  var SET_KEYS = ["wifi_ssid", "wifi_pass", "wifi2_ssid", "wifi2_pass", "access_id", "access_secret",
    "ap_pass", "tz"];
  var settingsState = null;
  function renderSettings(j) {
    settingsState = j;
    var b = j.board, parts = [];
    if (!b) parts.push("The board hasn't checked in yet.");
    else if (b.settings_version === null) parts.push("The board's firmware (" + esc(b.fw || "?") + ") keeps its settings built in; update it to change them here.");
    else {
      parts.push("The board has networks <b>" + esc(b.networks || "none") + "</b>, token " +
        esc(b.token_id ? b.token_id + "…" : "none") + ", settings change " + esc(String(b.settings_version)) +
        " (checked in " + esc(new Date(b.received).toLocaleString()) + ").");
    }
    if (j.pending) {
      parts.push("Waiting for its next check-in: " + esc(j.pending.keys.join(", ")) + " (asked by " +
        esc(j.pending.by) + ", " + esc(new Date(j.pending.requested).toLocaleString()) + ").");
    } else if (j.last) {
      parts.push("Last change: " + esc(j.last.result) + ", " + esc(new Date(j.last.done).toLocaleString()) + ".");
    }
    $("setState").innerHTML = parts.join(" ");
    var refused = j.last && /^refused/.test(j.last.result);
    foldState("setFold", j.pending ? "A change is waiting for the board" :
      refused ? "Last change refused" : b && b.networks ? b.networks.replace(",", ", ") : "", !!(j.pending || refused));
    $("setCancel").className = j.pending ? "" : "hide";
  }
  function loadSettings() {
    api("GET", "/api/logger/settings").then(renderSettings).catch(function (e) {
      $("setState").textContent = "Couldn't load the board's settings: " + e.message; });
  }

  function renderSweep(sw) {
    if (!sw) return;
    sweep = sw;
    var p = sw.pending, l = sw.last, text = "", warn = "";
    if (p) {
      var of = p.of || 0, next = p.next || 0;
      if (next && of) {
        text = "Under way: " + next.toLocaleString() + " of " + of.toLocaleString() + " identifiers (" +
          Math.floor(next * 100 / of) + "%); it carries on next drive if this one ends first.";
      } else if (p.seen) {
        text = "The logger has it: it starts a minute into the next drive.";
      } else {
        text = "Asked for " + new Date(p.requested).toLocaleString() + "; the logger picks it up at its next check-in.";
      }
      if (counted(p.counts)) text += " So far: " + counted(p.counts) + ".";
      if (p.silent) warn = "No answer during the sweep from " + moduleNames(p.silent) + ": skipped after 3 identifiers in a row.";
    } else if (l) {
      text = "Last full sweep finished " + new Date(l.done).toLocaleString() + (counted(l.counts) ? ": " + counted(l.counts) : "") +
        (l.newly_ok ? "; " + l.newly_ok.toLocaleString() + " answer now that didn't before" : "") + ".";
      if (l.silent) warn = "No answer during the last sweep from " + moduleNames(l.silent) + ": skipped after 3 identifiers in a row.";
    } else {
      text = "No full sweep yet.";
    }
    $("sweepState").textContent = text;
    foldState("sweepFold", p ? (p.next && p.of ? "Under way, " + Math.floor(p.next * 100 / p.of) + "%" :
      "Waiting for the next drive") : l ? "Last finished " + new Date(l.done).toLocaleDateString() : "None yet", !!warn);
    $("sweepWarn").textContent = warn;
    $("sweepWarn").classList.toggle("hide", !warn);
    $("sweepAsk").classList.toggle("hide", !!p);
    $("sweepCancel").classList.toggle("hide", !p);
  }
  function identifierCount() {
    if (!items.length) return null;
    var seen = {};
    items.forEach(function (it) { seen[it.key] = true; });
    return Object.keys(seen).length;
  }

  // The car from the last check-in. Coolant and oil limits: the N63's
  // thermostat runs the coolant up to about 105 °C under load.
  var LIMITS = { coolant_c: [110, 115], oil_c: [130, 140] };
  var mapAt = null;
  function level(key, v) {
    var l = LIMITS[key];
    return v == null || !l ? "" : v >= l[1] ? "bad" : v >= l[0] ? "warn" : "";
  }
  function stat(label, value, unit, cls, small) {
    return '<div class="stat ' + (cls || "") + '"><span>' + label + "</span><b>" +
      (value == null ? "–" : esc(value)) + "</b>" + (value == null || !unit ? "" : " " + unit) +
      (small ? " <small>" + esc(small) + "</small>" : "") + "</div>";
  }
  function num(v) { return typeof v === "number" && isFinite(v) ? v : null; }
  function whole(v) { return v == null ? null : Math.round(v); }
  function fahrenheit(v) { return v == null ? "" : Math.round(v * 9 / 5 + 32) + " °F"; }
  function renderLive(s, when) {
    var b = s.last, cool = num(b.coolant_c), oil = num(b.oil_c), kmh = num(b.speed_kmh),
      volts = num(b.battery_v), rate = num(b.rows_per_s);
    $("liveTitle").textContent = s.online ? "The car now" : "The car, last seen " + when + " ago";
    $("liveStats").innerHTML = [
      stat("State", b.state || "–"),
      b.state === "logging" ? stat("Drive", num(b.drive_min), "min", "", rate == null ? "" : rate + " rows/s") : "",
      stat("Speed", kmh == null ? null : Math.round(kmh / 1.609344), "mph"),
      stat("RPM", whole(num(b.rpm))),
      stat("Coolant", whole(cool), "°C", level("coolant_c", cool), fahrenheit(cool)),
      stat("Oil", whole(oil), "°C", level("oil_c", oil), fahrenheit(oil)),
      stat("Battery", volts == null ? null : volts.toFixed(1), "V"),
      stat("Faults", num(b.faults)),
    ].join("");
    renderFaults(b);
    var hot = s.online && (level("coolant_c", cool) === "bad" || level("oil_c", oil) === "bad");
    $("alarm").textContent = hot ? "Running hot: coolant " + whole(cool) + " °C, oil " + whole(oil) +
      " °C (limits " + LIMITS.coolant_c[1] + " and " + LIMITS.oil_c[1] + " °C)." : "";
    $("alarm").classList.toggle("hide", !hot);
    document.title = (hot ? "⚠ " : "") + "M550 Logger";
    var has = num(b.lat) !== null && num(b.lon) !== null;
    $("map").classList.toggle("hide", !has);
    $("mapNote").textContent = has ? (s.online ? "Position from the head unit's GPS, " : "Last known position, ") +
      new Date(b.received).toLocaleString() + "." : "No position in the last check-in.";
    // Moved more than about 20 m: the map again (a reload each check-in would flicker).
    if (has && (!mapAt || Math.abs(mapAt[0] - b.lat) + Math.abs(mapAt[1] - b.lon) > 0.0002)) {
      mapAt = [b.lat, b.lon];
      var d = 0.006;
      $("map").src = "https://www.openstreetmap.org/export/embed.html?layer=mapnik&bbox=" +
        [b.lon - d * 1.6, b.lat - d, b.lon + d * 1.6, b.lat + d].map(function (x) { return x.toFixed(6); }).join(",") +
        "&marker=" + b.lat.toFixed(6) + "," + b.lon.toFixed(6);
    }
  }

  // "2026-10-20" as "Oct 20" (local, so it isn't a day out).
  function day(iso) {
    var p = String(iso).split("-");
    return new Date(+p[0], +p[1] - 1, +p[2]).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }
  // When the odometer said so, from the drives: {after, by}.
  function when(d) {
    if (!d) return "";
    if (d.after && d.by) return d.after === d.by ? day(d.by) : day(d.after) + " – " + day(d.by);
    return d.by ? "before " + day(d.by) : "after " + day(d.after);
  }
  function setAt(km, d) {
    if (km == null) return '<td class="when muted">–</td>';
    return '<td class="when">' + Math.round(km / 1.609344).toLocaleString() + " mi" +
      (d ? "<small>" + esc(when(d)) + "</small>" : "") + "</td>";
  }
  function renderFaults(b) {
    var list = b.fault_list || [], n = num(b.faults), count = n == null ? list.length : n;
    $("faults").innerHTML = list.length ? list.map(function (f) {
      return '<tr><td class="code">' + esc(f.code) + '</td><td class="mod">' + esc(f.module_name || f.module) +
        "</td><td>" + esc(f.text_en || "") + "</td>" + setAt(f.first_km, f.first_date) +
        setAt(f.last_km, f.last_date) +
        '<td class="n">' + (f.occurrences == null ? "–" : esc(f.occurrences)) + "</td></tr>";
    }).join("") : '<tr><td colspan="6" class="muted">' +
      (count ? "The check-in has the count, not the list." : "None in the last check-in.") + "</td></tr>";
    foldState("faultsState", count ? count + (count === 1 ? " fault" : " faults") : "None", count > 0);
  }

  // The folding cards: collapsed unless opened here before (this browser
  // remembers each), the state beside each heading meanwhile.
  function folds() {
    ["foldFaults", "foldTanks", "foldSweep", "foldSettings"].forEach(function (id) {
      var d = $(id), key = "logger.fold." + id;
      try { d.open = window.localStorage.getItem(key) === "open"; } catch (e) { d.open = false; }
      d.addEventListener("toggle", function () {
        try { window.localStorage.setItem(key, d.open ? "open" : "closed"); } catch (e) { /* not kept */ }
      });
    });
  }
  function foldState(id, text, warn) {
    $(id).textContent = text || "";
    $(id).className = "foldstate" + (warn ? " warn" : "");
  }

  function renderTanks() {
    api("GET", "/api/logger/tanks").then(function (j) {
      var t = (j.tanks || []).slice().reverse();
      var gal = function (l) { return l == null ? "–" : (l / 3.785411784).toFixed(1); };
      $("tanks").innerHTML = t.length ? t.map(function (k) {
        return "<tr><td>" + esc(k.start.slice(0, 16)) + (k.open ? ' <span class="badge">now</span>' : "") +
          (k.partial ? ' <span class="badge warn" title="Filled before the logs began">partial</span>' : "") + "</td>" +
          '<td class="n">' + k.drives + '</td><td class="n">' + (k.filled_l == null ? "–" : gal(k.filled_l) + " gal") + "</td>" +
          '<td class="n">' + (k.miles == null ? "–" : Math.round(k.miles)) + "</td>" +
          '<td class="n">' + (k.gallons == null ? "–" : k.gallons.toFixed(1)) + (k.fuel_from === "level" ? " <small>level</small>" : "") + "</td>" +
          '<td class="n"><b>' + (k.mpg == null ? "–" : k.mpg.toFixed(1)) + "</b></td>" +
          '<td class="n">' + gal(k.level_end_l) + " gal</td></tr>";
      }).join("") : '<tr><td colspan="7" class="muted">No logger drives with the tank level yet.</td></tr>';
      var now = t[0];
      foldState("tanksState", !now ? "None yet" : (now.open ? "This tank" : "Last tank") + ": " +
        (now.mpg == null ? "–" : now.mpg.toFixed(1)) + " MPG over " +
        (now.miles == null ? "–" : Math.round(now.miles)) + " miles, " + gal(now.level_end_l) + " gal left");
    }).catch(function (e) { $("tanks").innerHTML = '<tr><td colspan="7" class="muted">' + esc(e.message) + "</td></tr>"; });
  }

  function afterPresets(j, keep) {
    presets = j.presets || []; activeName = j.active; file = j.file;
    loadPreset(keep || activeName || (presets[0] && presets[0].name));
    render(true);
    renderLogger();
  }

  function savePreset(name, extra) {
    var change = { name: name, rest: edit.rest, tiers: edit.tiers };
    var body = Object.assign({ changes: [change] }, extra || {});
    return api("PUT", "/api/logger/presets", body).then(function (j) {
      afterPresets(j, name);
      say("Saved " + name + (name === activeName ? ": the logger gets it on its next Wi-Fi check." : "."), true);
    });
  }

  function askName(title, start) {
    var n = window.prompt(title, start || "");
    if (n === null) return null;
    n = n.trim();
    if (!n) return null;
    if (presets.some(function (p) { return p.name === n; }) && !window.confirm(n + " exists. Replace it?")) return null;
    return n;
  }

  function wire() {
    $("q").addEventListener("input", function () { shown = PAGE; render(true); });
    ["mod", "ans", "tierf", "valf"].forEach(function (id) {
      $(id).addEventListener("change", function () { shown = PAGE; render(true); }); });
    $("more").addEventListener("click", function () { shown += PAGE; render(false); });
    $("rest").addEventListener("change", function () { edit.rest = this.value; render(true); });
    $("rows").addEventListener("change", function (e) {
      var tr = e.target.closest("tr"); if (!tr) return;
      var it = items[+tr.dataset.i];
      if (e.target.classList.contains("pick")) {
        if (e.target.checked) sel.add(it.i); else sel.delete(it.i);
        render(false); return;
      }
      if (e.target.tagName === "SELECT") {
        var names = sel.has(it.i) && sel.size > 1 ? [] : [it.name];
        if (names.length === 0) sel.forEach(function (i) { names.push(items[i].name); });
        setTier(names, e.target.value);
        render(false);
      }
    });
    $("all").addEventListener("change", function () {
      var on = this.checked;
      matches.forEach(function (it) { if (on) sel.add(it.i); else sel.delete(it.i); });
      render(false);
    });
    $("bulkTier").innerHTML = '<option value="">Default</option>' + TIERS.map(function (t) {
      return '<option value="' + t + '">' + LABEL[t] + "</option>"; }).join("");
    $("apply").addEventListener("click", function () {
      var names = []; sel.forEach(function (i) { names.push(items[i].name); });
      setTier(names, $("bulkTier").value);
      say("Set " + names.length.toLocaleString() + " channel" + (names.length === 1 ? "" : "s") + " to " +
        ($("bulkTier").value ? LABEL[$("bulkTier").value] : "the default") + ". Save to keep it.", true);
      sel.clear();                         // so a later search's selection starts empty
      render(true);
    });
    $("clearSel").addEventListener("click", function () { sel.clear(); render(false); });
    $("preset").addEventListener("change", function () {
      if (isDirty() && !window.confirm("Drop the unsaved changes to " + edit.name + "?")) { renderPresetBar(); return; }
      loadPreset(this.value); sel.clear(); say(""); render(true);
    });
    $("save").addEventListener("click", function () { savePreset(edit.name).catch(function (e) { say(e.message); }); });
    $("saveAs").addEventListener("click", function () {
      var n = askName("Save this preset as", edit.name ? edit.name + " copy" : "My preset");
      if (n) savePreset(n).catch(function (e) { say(e.message); });
    });
    $("rename").addEventListener("click", function () {
      var old = edit.name, n = askName("Rename " + old + " to", old);
      if (!n || n === old) return;
      var body = { changes: [{ name: n, rest: edit.rest, tiers: edit.tiers }, { name: old, deleted: true }] };
      if (old === activeName) body.active = n;
      api("PUT", "/api/logger/presets", body).then(function (j) { afterPresets(j, n); say("Renamed to " + n + ".", true); })
        .catch(function (e) { say(e.message); });
    });
    $("del").addEventListener("click", function () {
      if (!window.confirm("Delete the preset " + edit.name + "?")) return;
      api("PUT", "/api/logger/presets", { changes: [{ name: edit.name, deleted: true }] })
        .then(function (j) { afterPresets(j); say("Deleted.", true); }).catch(function (e) { say(e.message); });
    });
    $("activate").addEventListener("click", function () {
      var go = isDirty() ? savePreset(edit.name, { active: edit.name })
        : api("PUT", "/api/logger/presets", { active: edit.name }).then(function (j) { afterPresets(j, edit.name); });
      go.then(function () { say(edit.name + " is active: the logger downloads it on its next Wi-Fi check and uses it from its next drive.", true); })
        .catch(function (e) { say(e.message); });
    });
    $("revert").addEventListener("click", function () { loadPreset(edit.name); render(true); });
    $("sweepAsk").addEventListener("click", function () {
      var n = identifierCount();
      if (!window.confirm("Run a full channel sweep on the next drive?\n\n" +
          "The logger asks the car for " + (n ? "all " + n.toLocaleString() : "every") +
          " identifier" + (n === 1 ? "" : "s") + " in the catalog, starting a minute into the drive.\n\n" +
          "Warning: logging is delayed while the sweep runs. The fast channels keep going at a lower " +
          "rate, and the other channels wait their turn, for several minutes. A drive that ends " +
          "first leaves the rest to the next drive.")) return;
      api("POST", "/api/logger/sweep").then(function (j) {
        renderSweep({ pending: j.pending, last: sweep && sweep.last });
        say(j.created ? "Full channel sweep asked for: the logger picks it up at its next check-in and runs it on the next drive."
          : "A full channel sweep is already waiting for the next drive.", true);
      }).catch(function (e) { say(e.message); });
    });
    $("sweepCancel").addEventListener("click", function () {
      if (!window.confirm("Cancel the full channel sweep? What it has asked so far stays.")) return;
      api("DELETE", "/api/logger/sweep").then(function () {
        renderSweep({ pending: null, last: sweep && sweep.last });
        say("Full channel sweep cancelled: the logger stops at its next check-in.", true);
      }).catch(function (e) { say(e.message); });
    });
    $("setSave").addEventListener("click", function () {
      var change = {};
      SET_KEYS.forEach(function (k) { var v = $("set_" + k).value; if (v !== "") change[k] = v; });
      if ($("setClear2").checked) { change.wifi2_ssid = ""; change.wifi2_pass = ""; }
      var keys = Object.keys(change);
      if (!keys.length) { say("Nothing to send: fill in what should change."); return; }
      if (!window.confirm("Send to the board at its next check-in: " + keys.join(", ") + "?")) return;
      api("PUT", "/api/logger/settings", change).then(function (j) {
        SET_KEYS.forEach(function (k) { $("set_" + k).value = ""; });
        $("setClear2").checked = false;
        renderSettings(j);
        say("Sent: the board takes it at its next check-in (every 10 s while it has Wi-Fi).", true);
      }).catch(function (e) { say(e.message); });
    });
    $("setCancel").addEventListener("click", function () {
      if (!window.confirm("Take back the change the board hasn't had yet?")) return;
      api("DELETE", "/api/logger/settings").then(function (j) { renderSettings(j); say("Taken back.", true); })
        .catch(function (e) { say(e.message); });
    });
    window.addEventListener("beforeunload", function (e) { if (isDirty()) { e.preventDefault(); e.returnValue = ""; } });
  }

  function start() {
    folds();
    wire();
    renderTanks();
    renderLogger();
    loadSettings();
    // The car while the page is open: the board checks in every 10 s.
    setInterval(function () {
      if (document.hidden) return;
      renderLogger();
      if (settingsState && settingsState.pending) loadSettings();
    }, 10000);
    Promise.all([api("GET", "/api/logger/catalog"), api("GET", "/api/logger/presets")]).then(function (r) {
      cat = r[0];
      var c = {};
      cat.columns.forEach(function (n, i) { c[n] = i; });
      var mods = {};
      items = cat.rows.map(function (row, i) {
        var key = row[c.module] + ":" + row[c.did], status = cat.answers[key];
        mods[row[c.module]] = true;
        var it = { i: i, name: row[c.name], module: row[c.module], did: row[c.did], key: key,
          unit: row[c.unit] || "", info: row[c.info] || "", group: row[c.group] || "",
          type: row[c.type], mul: row[c.mul], div: row[c.div], add: row[c.add],
          status: status, ok: status === "ok", modName: cat.modules[row[c.module]] || row[c.module] };
        it.hay = (it.name + " " + it.info + " " + it.group + " " + it.did).toLowerCase();
        return it;
      });
      $("mod").innerHTML += Object.keys(mods).sort().map(function (m) {
        return '<option value="' + esc(m) + '">' + esc((cat.modules[m] || "Module") + " (" + m + ")") + "</option>"; }).join("");
      $("tierf").innerHTML += TIERS.map(function (t) { return '<option value="' + t + '">' + LABEL[t] + "</option>"; }).join("");
      var answering = items.filter(function (it) { return it.ok; }).length;
      $("catinfo").textContent = items.length.toLocaleString() + " channels the logger can decode; the car answers " +
        answering.toLocaleString() + ". Catalog from " + new Date(cat.updated).toLocaleString() + ".";
      afterPresets(r[1]);
      $("valinfo").textContent = "Loading the stored values…";
      api("GET", "/api/logger/values").then(function (j) {
        values = j.values || {};
        markOdd();
        var stored = items.filter(function (it) { return values[it.name]; }).length;
        var odd = items.filter(function (it) { return it.odd; }).length;
        $("valinfo").textContent = "Stored values for " + stored.toLocaleString() + " of them; " +
          odd.toLocaleString() + " look wrong (the filter Looks wrong lists them).";
        render(true);
      }).catch(function (e) { $("valinfo").textContent = "Couldn't load the stored values: " + e.message; });
    }).catch(function (e) {
      $("catinfo").textContent = "";
      say("Couldn't load: " + e.message + ". The app sends the catalog: Answers > Logger channels.");
    });
  }
  start();
})();
</script>
</body>
</html>
`;
