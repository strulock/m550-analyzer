/**
 * The logger page's Health tab (GET /logger/health): every drive's health
 * scores (health.js) as a heatmap, channels down the side and drives along
 * the bottom, coloured by how far each is from its own baseline. A cell or
 * row opens the channel: its value across drives with the normal band, the
 * other bank (or matching cylinder) beside it, the drive's RPM x load cells
 * against the baseline, and the drive's raw log at the longest time in a
 * cell. Talks to /api/health with the visitor's Access login. No
 * dependencies; the script avoids backticks and template placeholders so it
 * can sit in this template.
 */

export const HEALTH_PAGE = String.raw`<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>M550 Drive Health</title>
<link rel="icon" href="data:,">
<style>
:root {
  --bg: #f4f5f7; --panel: #fff; --fg: #1d2126; --muted: #5d6670; --rule: #d9dde2;
  --accent: #1f6feb; --accent-bg: #e8f0fe; --warn: #9a5b00; --warn-bg: #fff4e0;
  --ok: #1a7f37; --bad: #b42318;
  --h-none: #eceef1; --h-0: #dcefe1; --h-1: #b9dfc3; --h-watch: #f3c25f; --h-alert: #e0574c;
  --line: #1f6feb; --line2: #c2410c; --band: rgba(26, 127, 55, 0.13);
  --mono: ui-monospace, "Cascadia Mono", Consolas, monospace;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) { :root {
  --bg: #121417; --panel: #1b1e23; --fg: #e4e7eb; --muted: #9aa3ad; --rule: #2e333a;
  --accent: #6aa5ff; --accent-bg: #1c2a40; --warn: #f0b35a; --warn-bg: #2d2416;
  --ok: #4fc06b; --bad: #ff7b6e;
  --h-none: #262a30; --h-0: #1f3a28; --h-1: #2b5537; --h-watch: #9a7224; --h-alert: #a8352d;
  --line: #6aa5ff; --line2: #ff8a4c; --band: rgba(79, 192, 107, 0.16);
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
button:disabled { opacity: 0.5; cursor: default; }
label { display: inline-flex; gap: 6px; align-items: center; }
.hide { display: none !important; }
.msg { padding: 8px 12px; border-radius: 6px; background: var(--warn-bg); color: var(--warn); }
nav.tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--rule); }
nav.tabs a { padding: 8px 14px; color: var(--muted); text-decoration: none; border-bottom: 2px solid transparent; margin-bottom: -1px; }
nav.tabs a[aria-current] { color: var(--fg); border-bottom-color: var(--accent); font-weight: 600; }
.legend { display: flex; flex-wrap: wrap; gap: 10px; font-size: 12.5px; color: var(--muted); }
.legend i { display: inline-block; width: 12px; height: 12px; border-radius: 3px; vertical-align: -2px; margin-right: 4px; }
.scroll { overflow-x: auto; }
table.hm { border-collapse: separate; border-spacing: 2px; font-size: 12px; }
table.hm th { font-weight: 600; color: var(--muted); white-space: nowrap; }
table.hm th.ch { text-align: left; position: sticky; left: 0; background: var(--panel); padding-right: 8px;
  font-weight: 400; color: var(--fg); cursor: pointer; max-width: 230px; overflow: hidden; text-overflow: ellipsis; }
table.hm th.ch:hover { color: var(--accent); }
table.hm tr.grp th { text-align: left; padding-top: 10px; color: var(--muted); font-size: 11.5px;
  text-transform: uppercase; letter-spacing: 0.04em; position: sticky; left: 0; background: var(--panel);
  white-space: normal; vertical-align: bottom; }
table.hm td { width: 20px; min-width: 20px; height: 20px; border-radius: 3px; cursor: pointer; padding: 0; }
table.hm td.x { cursor: default; background: transparent; }
table.hm td.sel, table.hm th.dsel { outline: 2px solid var(--accent); outline-offset: -1px; }
table.hm thead th.d { writing-mode: vertical-rl; transform: rotate(180deg); font-weight: 400;
  padding: 4px 0; height: 92px; text-align: left; cursor: pointer; }
table.hm thead th.d.ex { text-decoration: line-through; }
.c-none { background: var(--h-none); } .c-0 { background: var(--h-0); } .c-1 { background: var(--h-1); }
.c-w { background: var(--h-watch); } .c-a { background: var(--h-alert); }
svg { display: block; width: 100%; height: auto; }
svg text { fill: var(--muted); font-size: 11px; }
table.grid { border-collapse: separate; border-spacing: 2px; font-size: 12px; font-variant-numeric: tabular-nums; }
table.grid th { color: var(--muted); font-weight: 600; padding: 2px 6px; white-space: nowrap; }
table.grid td { padding: 4px 6px; border-radius: 4px; text-align: center; min-width: 70px; cursor: pointer; }
table.grid td.e { background: transparent; cursor: default; }
table.grid td small { display: block; color: inherit; opacity: 0.75; font-size: 11px; }
table.grid td.sel { outline: 2px solid var(--accent); outline-offset: -1px; }
.facts { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; margin: 6px 0; }
.facts div span { display: block; font-size: 12px; color: var(--muted); }
.facts div b { font-variant-numeric: tabular-nums; }
a { color: var(--accent); }
.key { font-family: var(--mono); font-size: 12px; color: var(--muted); word-break: break-all; }
@media (max-width: 640px) {
  .card { padding: 10px; }
  table.hm th.ch { max-width: 130px; }
  table.grid td { min-width: 54px; padding: 3px; }
}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Drive health</h1>
    <div class="muted" id="info">Loading…</div>
  </header>
  <nav class="tabs"><a href="/logger">Logger</a><a href="/logger/health" aria-current="page">Health</a><a href="/logger/board">Board</a></nav>

  <div id="err" class="msg hide" role="alert"></div>

  <section class="card">
    <div class="row" style="justify-content:space-between">
      <div class="row">
        <label>Group <select id="group"><option value="">All</option></select></label>
        <label>From <input type="date" id="from"></label>
        <label>To <input type="date" id="to"></label>
        <label><input type="checkbox" id="flagged"> Flagged channels only</label>
      </div>
      <div class="legend">
        <span><i class="c-0"></i>Normal</span><span><i class="c-1"></i>Within 1–2</span>
        <span><i class="c-w"></i>Watch</span><span><i class="c-a"></i>Alert</span>
        <span><i class="c-none"></i>No baseline yet</span>
      </div>
    </div>
    <div class="scroll" style="margin-top:10px"><table class="hm" id="hm"></table></div>
    <p class="muted" id="note" style="font-size:12.5px;margin:8px 0 0"></p>
  </section>

  <section class="card hide" id="drill">
    <div class="row" style="justify-content:space-between">
      <h2 id="dTitle" style="margin:0"></h2>
      <button id="dClose">Close</button>
    </div>
    <div class="key" id="dKey"></div>
    <h3>Across drives</h3>
    <div class="muted" id="trendNote" style="font-size:12.5px"></div>
    <div id="trend"></div>
    <div id="dDrive">
      <h3 id="gridTitle"></h3>
      <div class="facts" id="dFacts"></div>
      <div class="scroll"><table class="grid" id="grid"></table></div>
      <p class="muted" style="font-size:12.5px;margin:6px 0 0">Each cell: this drive's value
      (median; a counter's rate per 1,000 revolutions), the baseline's median below it, coloured
      by the z-score. Click a cell for the times the drive spent there.</p>
      <h3>Raw log</h3>
      <div id="raw" class="muted">Pick a cell above.</div>
      <div id="rawChart"></div>
      <div class="row" style="margin-top:10px">
        <button id="exclude"></button>
        <span class="muted" id="exNote" style="font-size:12.5px"></span>
      </div>
    </div>
  </section>

  <section class="card">
    <h2>Working it out</h2>
    <p class="muted" style="margin:0 0 8px;font-size:13px" id="how"></p>
    <div class="row"><button id="rebuild">Work out every drive again</button><span class="muted" id="rebuildNote"></span></div>
  </section>
</div>
<script>
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var data = null, view = null, sel = { key: null, drive: null }, detail = null, cellSel = null;
  var LEVEL_CLASS = ["c-0", "c-w", "c-a"];

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fmt(x, unit) {
    if (x == null) return "–";
    var a = Math.abs(x), d = a >= 1000 ? 0 : a >= 100 ? 1 : a >= 10 ? 2 : 3;
    return Number(x).toFixed(d) + (unit ? " " + unit : "");
  }
  function when(iso) {
    return new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit" });
  }
  function clock(iso) {
    return new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }
  function fail(e) { $("err").textContent = String(e && e.message || e); $("err").classList.remove("hide"); }
  function get(url) {
    return fetch(url, { credentials: "same-origin" }).then(function (r) {
      if (!r.ok) return r.json().catch(function () { return {}; }).then(function (j) {
        throw new Error(j.error || ("HTTP " + r.status)); });
      return r.json();
    });
  }
  function post(url, body) {
    return fetch(url, { method: "POST", credentials: "same-origin",
      headers: { "content-type": "application/json" }, body: JSON.stringify(body || {}) })
      .then(function (r) { return r.json().then(function (j) {
        if (!r.ok) throw new Error(j.error || ("HTTP " + r.status)); return j; }); });
  }
  function group(key) {
    for (var i = 0; i < data.groups.length; i++) if (data.groups[i].key === key) return data.groups[i];
    return data.groups[0];
  }
  function chan(key) {
    for (var i = 0; i < data.channels.length; i++) if (data.channels[i].key === key) return data.channels[i];
    return null;
  }
  // The class for a score: no baseline, under 1, 1 to watch, watch, alert.
  function cls(c, s) {
    if (!s) return "x";
    if (s[0] == null) return "c-none";
    var g = group(c.group), a = Math.abs(s[0]);
    return a >= g.alert ? "c-a" : a >= g.watch ? "c-w" : a >= 1 ? "c-1" : "c-0";
  }
  function levelOf(c, score) {
    if (score == null) return null;
    var g = group(c.group), a = Math.abs(score);
    return a >= g.alert ? 2 : a >= g.watch ? 1 : 0;
  }

  // ---------------------------------------------------------------- heatmap

  function filtered() {
    var from = $("from").value, to = $("to").value, idx = [];
    data.drives.forEach(function (d, i) {
      var day = d.start.slice(0, 10);
      if ((!from || day >= from) && (!to || day <= to)) idx.push(i);
    });
    var g = $("group").value, flagged = $("flagged").checked;
    var chans = data.channels.filter(function (c) {
      if (g && c.group !== g) return false;
      if (!flagged) return true;
      return idx.some(function (i) { var s = data.scores[c.key][i]; return s && levelOf(c, s[0]) >= 1; });
    });
    return { idx: idx, chans: chans };
  }

  function render() {
    view = filtered();
    var h = ['<thead><tr><th class="ch"></th>'];
    view.idx.forEach(function (i) {
      var d = data.drives[i];
      h.push('<th class="d' + (d.excluded ? " ex" : "") + (sel.drive === d.drive ? " dsel" : "") +
        '" data-i="' + i + '" title="' + esc(d.drive + " · " + d.warm_min + " min warm" +
        (d.excluded ? " · left out of baselines" : "")) + '">' + esc(when(d.start)) + "</th>");
    });
    h.push("</tr></thead><tbody>");
    h.push('<tr><th class="ch" style="cursor:default;font-weight:600">Whole drive</th>');
    view.idx.forEach(function (i) {
      var d = data.drives[i], c = d.level == null ? "c-none" : LEVEL_CLASS[d.level];
      h.push('<td class="' + c + '" data-i="' + i + '" title="' + esc(when(d.start) +
        ": worst channel " + fmt(d.overall)) + '"></td>');
    });
    h.push("</tr>");
    var last = null;
    view.chans.forEach(function (c) {
      if (c.group !== last) {
        last = c.group;
        h.push('<tr class="grp"><th>' + esc(group(c.group).label) + '</th><td class="x" colspan="' +
          view.idx.length + '"></td></tr>');
      }
      h.push('<tr><th class="ch" data-k="' + esc(c.key) + '" title="' + esc(c.key) + '">' + esc(c.label) + "</th>");
      view.idx.forEach(function (i) {
        var s = data.scores[c.key][i], k = cls(c, s), d = data.drives[i];
        var t = s ? (c.label + ", " + when(d.start) + ": " + (s[0] == null ? "no baseline yet" :
          "score " + fmt(s[0]) + " · value " + fmt(s[2], c.unit) +
          (s[3] != null ? " (normal " + fmt(s[3]) + " to " + fmt(s[4]) + ")" : "")) +
          " · " + s[1] + " cells") : "";
        h.push('<td class="' + k + (sel.key === c.key && sel.drive === d.drive ? " sel" : "") +
          '" data-k="' + esc(c.key) + '" data-i="' + i + '" title="' + esc(t) + '"></td>');
      });
      h.push("</tr>");
    });
    h.push("</tbody>");
    $("hm").innerHTML = view.idx.length ? h.join("") :
      '<tbody><tr><td class="x muted" style="width:auto">No drives in that range.</td></tr></tbody>';
    var sc = $("hm").parentNode;          // the newest drives in view
    sc.scrollLeft = sc.scrollWidth;
  }

  $("hm").addEventListener("click", function (ev) {
    var el = ev.target.closest("[data-k], [data-i]");
    if (!el) return;
    var key = el.getAttribute("data-k"), i = el.getAttribute("data-i");
    if (key) sel.key = key;
    if (i != null) sel.drive = data.drives[+i].drive;
    if (!sel.key) {        // a drive picked before a channel: its worst one
      var d = +i, worst = null;
      data.channels.forEach(function (c) {
        var s = data.scores[c.key][d];
        if (s && s[0] != null && (!worst || Math.abs(s[0]) > Math.abs(data.scores[worst.key][d][0]))) worst = c;
      });
      if (!worst) return;
      sel.key = worst.key;
    }
    if (!sel.drive) {      // a channel picked: its newest scored drive
      for (var j = view.idx.length - 1; j >= 0; j--) {
        if (data.scores[sel.key][view.idx[j]]) { sel.drive = data.drives[view.idx[j]].drive; break; }
      }
    }
    render();
    openDrill();
  });

  // ---------------------------------------------------------------- drill-down

  function openDrill() {
    var c = chan(sel.key);
    if (!c) return;
    $("drill").classList.remove("hide");
    $("dTitle").textContent = c.label;
    $("dKey").textContent = c.key + (c.unit ? " · " + c.unit : "");
    drawTrend(c);
    cellSel = null;
    $("raw").textContent = "Pick a cell above.";
    $("rawChart").innerHTML = "";
    if (!sel.drive) { $("dDrive").classList.add("hide"); return; }
    $("dDrive").classList.remove("hide");
    $("gridTitle").textContent = "RPM × load, " + when(driveOf(sel.drive).start);
    $("grid").innerHTML = '<tr><td class="e muted">Loading…</td></tr>';
    get("/api/health/cell?drive=" + encodeURIComponent(sel.drive) + "&channel=" + encodeURIComponent(sel.key))
      .then(function (d) { detail = d; drawGrid(c, d); }).catch(fail);
    drawExclude();
    $("drill").scrollIntoView({ behavior: "smooth", block: "start" });
  }
  $("dClose").addEventListener("click", function () {
    $("drill").classList.add("hide"); sel = { key: null, drive: null }; render();
  });
  function driveOf(name) {
    for (var i = 0; i < data.drives.length; i++) if (data.drives[i].drive === name) return data.drives[i];
    return null;
  }

  // The channel's value per drive with its normal band, the other bank's
  // dashed beside it; the picked drive ringed.
  function drawTrend(c) {
    var pair = c.pair && data.scores[c.pair] ? chan(c.pair) : null;
    var idx = view.idx.filter(function (i) { return data.scores[c.key][i] || (pair && data.scores[pair.key][i]); });
    if (!idx.length) { $("trend").innerHTML = '<p class="muted">No drives with this channel.</p>'; return; }
    var W = width("trend"), H = 250, L = 72, R = 30, T = 12, B = 50;
    var vals = [];
    idx.forEach(function (i) {
      [data.scores[c.key][i], pair ? data.scores[pair.key][i] : null].forEach(function (s) {
        if (!s) return;
        [s[2], s[3], s[4]].forEach(function (v) { if (v != null) vals.push(v); });
      });
    });
    var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    if (hi === lo) { hi += 1; lo -= 1; }
    var pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
    var x = function (j) { return L + (idx.length === 1 ? (W - L - R) / 2 : j * (W - L - R) / (idx.length - 1)); };
    var y = function (v) { return T + (hi - v) / (hi - lo) * (H - T - B); };
    var o = ['<svg viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="' + esc(c.label) + ' across drives">'];
    o.push(yUnit(c.unit, T, H - B));
    for (var g = 0; g <= 4; g++) {
      var v = lo + (hi - lo) * g / 4;
      o.push('<line x1="' + L + '" x2="' + (W - R) + '" y1="' + y(v) + '" y2="' + y(v) + '" stroke="var(--rule)"/>');
      o.push('<text x="' + (L - 6) + '" y="' + (y(v) + 4) + '" text-anchor="end">' + esc(fmt(v)) + "</text>");
    }
    // The band: one polygon per run of drives that have one.
    var run = [];
    var flush = function () {
      if (run.length) {
        var up = run.map(function (p) { return x(p.j) + "," + y(p.s[4]); });
        var dn = run.slice().reverse().map(function (p) { return x(p.j) + "," + y(p.s[3]); });
        if (run.length === 1) {
          var px = x(run[0].j);
          up = [(px - 4) + "," + y(run[0].s[4]), (px + 4) + "," + y(run[0].s[4])];
          dn = [(px + 4) + "," + y(run[0].s[3]), (px - 4) + "," + y(run[0].s[3])];
        }
        o.push('<polygon fill="var(--band)" points="' + up.concat(dn).join(" ") + '"/>');
      }
      run = [];
    };
    idx.forEach(function (i, j) {
      var s = data.scores[c.key][i];
      if (s && s[3] != null) run.push({ j: j, s: s }); else flush();
    });
    flush();
    var line = function (key, color, dash) {
      var pts = [];
      idx.forEach(function (i, j) { var s = data.scores[key][i]; if (s && s[2] != null) pts.push([x(j), y(s[2]), s, i]); });
      if (pts.length > 1) {
        o.push('<polyline fill="none" stroke="' + color + '" stroke-width="1.6"' + (dash ? ' stroke-dasharray="5 4"' : "") +
          ' points="' + pts.map(function (p) { return p[0] + "," + p[1]; }).join(" ") + '"/>');
      }
      pts.forEach(function (p) {
        var lv = levelOf(chan(key), p[2][0]);
        var fill = lv === 2 ? "var(--bad)" : lv === 1 ? "var(--warn)" : color;
        var picked = data.drives[p[3]].drive === sel.drive;
        o.push('<circle cx="' + p[0] + '" cy="' + p[1] + '" r="' + (picked ? 5.5 : 3) + '" fill="' + fill + '"' +
          (picked ? ' stroke="var(--fg)" stroke-width="1.5"' : "") + '><title>' +
          esc(chan(key).label + ", " + when(data.drives[p[3]].start) + ": " + fmt(p[2][2], chan(key).unit) +
          (p[2][0] != null ? " · score " + fmt(p[2][0]) : "")) + "</title></circle>");
      });
    };
    if (pair) line(pair.key, "var(--line2)", true);
    line(c.key, "var(--line)", false);
    // The drive axis: a minor tick at every drive (its time, where there's
    // room), a major tick and a dashed line where a new day starts (its date).
    var gap = idx.length > 1 ? (W - L - R) / (idx.length - 1) : W, base = H - B;
    var times = gap >= 52, lastDay = null, lastDateX = -Infinity;
    o.push('<line x1="' + L + '" x2="' + (W - R) + '" y1="' + base + '" y2="' + base + '" stroke="var(--muted)"/>');
    idx.forEach(function (i, j) {
      var t = new Date(data.drives[i].start), day = t.toDateString(), px = x(j);
      var major = day !== lastDay;
      lastDay = day;
      if (major && j) {
        o.push('<line x1="' + px + '" x2="' + px + '" y1="' + T + '" y2="' + base +
          '" stroke="var(--muted)" stroke-dasharray="2 4" opacity="0.6"/>');
      }
      o.push('<line x1="' + px + '" x2="' + px + '" y1="' + base + '" y2="' + (base + (major ? 8 : 5)) +
        '" stroke="var(--muted)"' + (major ? ' stroke-width="2"' : "") + "/>");
      if (times) {
        o.push('<text x="' + px + '" y="' + (base + 17) + '" text-anchor="middle">' +
          esc(t.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })) + "</text>");
      }
      if (major && px - lastDateX >= 56) {
        o.push('<text x="' + (px + 4) + '" y="' + (base + 40) + '" style="font-weight:600">' +
          esc(t.toLocaleDateString(undefined, { month: "short", day: "numeric" })) + "</text>");
        lastDateX = px;
      }
    });
    o.push("</svg>");
    $("trend").innerHTML = o.join("");
    $("trendNote").innerHTML = "Each drive's value" + (c.unit ? " (" + esc(c.unit) + ")" : "") +
      " over the cells it shares with the baseline (sample-weighted), and the shaded normal band: " +
      "the baseline ± " + group(c.group).watch + " spreads. Drives are evenly spaced; a tick each, " +
      "and a dashed line where a new day starts." + (pair ? ' <span style="color:var(--line)">━ ' + esc(c.label) +
      '</span> &nbsp; <span style="color:var(--line2)">┅ ' + esc(pair.label) + "</span>" : "");
  }

  // A chart's width in its box's own pixels, so text stays its size on a phone.
  function width(id) { return Math.max(320, Math.round($(id).clientWidth || 900)); }
  var resized = null;
  window.addEventListener("resize", function () {
    clearTimeout(resized);
    resized = setTimeout(function () {
      if (sel.key && !$("drill").classList.contains("hide")) drawTrend(chan(sel.key));
    }, 200);
  });

  // The y-axis's unit, written up its side.
  function yUnit(unit, top, bottom) {
    if (!unit) return "";
    return '<text transform="translate(14 ' + (top + bottom) / 2 + ') rotate(-90)" text-anchor="middle">' +
      esc(unit) + "</text>";
  }

  function drawGrid(c, d) {
    var rpm = d.rpm_edges, load = d.load_edges;
    var h = ['<tr><th>Load · RPM</th>'];
    for (var r = 0; r < rpm.length - 1; r++) h.push("<th>" + rpm[r] + "–" + rpm[r + 1] + "</th>");
    h.push("</tr>");
    for (var l = load.length - 2; l >= 0; l--) {
      h.push("<tr><th>" + load[l] + "–" + load[l + 1] + " %</th>");
      for (var r2 = 0; r2 < rpm.length - 1; r2++) {
        var k = r2 + "." + l, cell = d.cells[k];
        if (!cell || cell.value == null) {
          h.push('<td class="e' + (cell ? " c-none" : "") + '"' + (cell ? ' data-c="' + k + '" title="' +
            esc(Math.round(cell.seconds) + " s here; too few samples") + '"' : "") + "></td>");
          continue;
        }
        var lv = cell.z == null ? null : Math.abs(cell.z), g = group(c.group);
        var k2 = lv == null ? "c-none" : lv >= g.alert ? "c-a" : lv >= g.watch ? "c-w" : lv >= 1 ? "c-1" : "c-0";
        h.push('<td class="' + k2 + (cellSel === k ? " sel" : "") + '" data-c="' + k + '" title="' + esc(
          Math.round(cell.seconds) + " s · " + cell.n + (c.kind === "count" ? " counter reads" : " samples") +
          (cell.z != null ? " · z " + fmt(cell.z) : " · no baseline")) + '">' + esc(fmt(cell.value)) +
          "<small>" + (cell.baseline != null ? esc(fmt(cell.baseline)) : "–") + "</small></td>");
      }
      h.push("</tr>");
    }
    $("grid").innerHTML = h.join("");
    var dr = driveOf(sel.drive), s = data.scores[c.key][data.drives.indexOf(dr)];
    $("dFacts").innerHTML = [
      ["Score", s && s[0] != null ? fmt(s[0]) : "no baseline yet"],
      ["Value", s ? fmt(s[2], c.unit) : "–"],
      ["Normal", s && s[3] != null ? fmt(s[3]) + " to " + fmt(s[4]) : "–"],
      ["Cells scored", s ? s[1] : 0],
      ["Warm", d.warm_min + " min"],
    ].map(function (f) { return "<div><span>" + esc(f[0]) + "</span><b>" + esc(f[1]) + "</b></div>"; }).join("");
  }

  $("grid").addEventListener("click", function (ev) {
    var td = ev.target.closest("td[data-c]");
    if (!td || !detail) return;
    cellSel = td.getAttribute("data-c");
    drawGrid(chan(sel.key), detail);
    showRaw();
  });

  // The cell's longest visits, each a link to the raw values then, and the
  // channel through the drive with them shaded.
  function showRaw() {
    var d = detail, cell = d.cells[cellSel], c = chan(sel.key);
    var rpm = d.rpm_edges, load = d.load_edges, p = cellSel.split(".");
    var where = rpm[+p[0]] + "–" + rpm[+p[0] + 1] + " rpm, " + load[+p[1]] + "–" + load[+p[1] + 1] + " % load";
    var file = "/api/files/" + d.drive.split("/").map(encodeURIComponent).join("/");
    var links = cell.visits.map(function (v) {
      var a = new Date(v[0]).getTime() / 1000 - 30, b = new Date(v[1]).getTime() / 1000 + 30;
      return '<a href="/api/series?channel=' + encodeURIComponent(d.name || c.key) + "&start=" + a + "&end=" + b +
        '" target="_blank" rel="noopener">' + esc(clock(v[0]) + "–" + clock(v[1])) + "</a> (" +
        Math.round((new Date(v[1]) - new Date(v[0])) / 1000) + " s)";
    });
    $("raw").innerHTML = esc(where) + ": " + Math.round(cell.seconds) + " s in this drive" +
      (links.length ? ". Longest visits: " + links.join(", ") : "") + '. <a href="' + file +
      '">Whole log (CSV)</a>';
    if (!d.name) return;
    get("/api/series?channel=" + encodeURIComponent(d.name) + "&start=" + new Date(d.start).getTime() / 1000 +
      "&end=" + new Date(d.end).getTime() / 1000 + "&points=600").then(function (s) { drawRaw(s, cell, c); }).catch(fail);
  }

  function drawRaw(s, cell, c) {
    var pts = (s.points || []).map(function (p) { return [new Date(p[0]).getTime(), p[1]]; });
    if (pts.length < 2) { $("rawChart").innerHTML = ""; return; }
    var W = width("rawChart"), H = 170, L = 72, R = 30, T = 10, B = 26;
    var t0 = pts[0][0], t1 = pts[pts.length - 1][0];
    var vs = pts.map(function (p) { return p[1]; });
    var lo = Math.min.apply(null, vs), hi = Math.max.apply(null, vs);
    if (hi === lo) { hi += 1; lo -= 1; }
    var x = function (t) { return L + (t - t0) / (t1 - t0 || 1) * (W - L - R); };
    var y = function (v) { return T + (hi - v) / (hi - lo) * (H - T - B); };
    var o = ['<svg viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="' + esc(c.label) + ' through the drive">'];
    o.push(yUnit(c.unit, T, H - B));
    cell.visits.forEach(function (v) {
      var a = x(new Date(v[0]).getTime()), b = x(new Date(v[1]).getTime());
      o.push('<rect x="' + a + '" y="' + T + '" width="' + Math.max(2, b - a) + '" height="' + (H - T - B) +
        '" fill="var(--accent-bg)"/>');
    });
    [lo, (lo + hi) / 2, hi].forEach(function (v) {
      o.push('<text x="' + (L - 6) + '" y="' + (y(v) + 4) + '" text-anchor="end">' + esc(fmt(v)) + "</text>");
    });
    o.push('<polyline fill="none" stroke="var(--line)" stroke-width="1.2" points="' +
      pts.map(function (p) { return x(p[0]) + "," + y(p[1]); }).join(" ") + '"/>');
    [t0, (t0 + t1) / 2, t1].forEach(function (t, j) {
      o.push('<text x="' + x(t) + '" y="' + (H - 8) + '" text-anchor="' + ["start", "middle", "end"][j] + '">' +
        esc(clock(new Date(t).toISOString())) + "</text>");
    });
    o.push("</svg>");
    $("rawChart").innerHTML = o.join("") + '<div class="muted" style="font-size:12px">' + esc(c.label) +
      " through the drive (all of it, warm or not); shaded: the longest visits to the cell.</div>";
  }

  function drawExclude() {
    var d = driveOf(sel.drive);
    $("exclude").textContent = d.excluded ? "Use this drive in baselines again" : "Leave this drive out of baselines";
    $("exNote").textContent = d.excluded ? "Left out by hand: it's scored, but no later drive is compared with it." :
      "For a drive you know wasn't normal (a fault, a tow, a test). Every drive is scored again.";
  }
  $("exclude").addEventListener("click", function () {
    var d = driveOf(sel.drive);
    $("exclude").disabled = true;
    post("/api/health/exclude", { drive: d.drive, excluded: !d.excluded })
      .then(load).then(function () { $("exclude").disabled = false; if (sel.key) openDrill(); })
      .catch(function (e) { $("exclude").disabled = false; fail(e); });
  });

  // ---------------------------------------------------------------- rebuild

  $("rebuild").addEventListener("click", function () {
    var n = 0;
    $("rebuild").disabled = true;
    var step = function (after) {
      return post("/api/health/rebuild" + (after ? "?after=" + encodeURIComponent(after) : ""))
        .then(function (r) {
          n += r.done.length;
          $("rebuildNote").textContent = n + " drives so far…";
          return r.next ? step(r.next) : null;
        });
    };
    step(null).then(load).then(function () {
      $("rebuildNote").textContent = n + " drives worked out.";
      $("rebuild").disabled = false;
    }).catch(function (e) { $("rebuild").disabled = false; fail(e); });
  });

  ["group", "from", "to", "flagged"].forEach(function (id) {
    $(id).addEventListener("change", function () { render(); if (sel.key && !$("drill").classList.contains("hide")) drawTrend(chan(sel.key)); });
  });

  function load() {
    return get("/api/health").then(function (d) {
      data = d;
      var g = $("group"), keep = g.value;
      g.innerHTML = '<option value="">All</option>' + d.groups.filter(function (x) {
        return d.channels.some(function (c) { return c.group === x.key; });
      }).map(function (x) { return '<option value="' + esc(x.key) + '">' + esc(x.label) + "</option>"; }).join("");
      g.value = keep;
      var st = d.settings;
      $("info").textContent = d.drives.length + " drives scored, " + d.channels.length + " channels.";
      $("note").textContent = "Each cell: how far the drive's channel is from its own last " + st.baseline_drives +
        " healthy drives, in robust spreads (median and MAD), compared in RPM × load cells and averaged by samples. " +
        "Watch from " + d.groups[0].watch + ", alert from " + d.groups[0].alert +
        ". A cell needs " + st.min_samples + " samples (a counter " + st.min_revs.toLocaleString() +
        " revolutions) and " + st.min_baseline + " baseline drives. Click a cell or a channel to open it.";
      $("how").textContent = "Only samples with the engine fully warm count: coolant (ITKUM) at least " +
        st.warm_coolant_c + " °C and, where it was read, oil at least " + st.warm_oil_c +
        " °C. Load is the calculated load from the throttle air flow and RPM (as ILMAR reports it). " +
        "Drives are worked out as they upload; this button does every stored drive again, " +
        "for after the method changes.";
      render();
    });
  }
  load().catch(fail);
})();
</script>
</body>
</html>`;
