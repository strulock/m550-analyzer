/**
 * n63read cloud: the car's logs and history, stored and served by a
 * Cloudflare Worker.
 *
 * - The app uploads every drive log and fault list (PUT /api/files/<path>)
 *   into R2, and its drive and fault summaries (PUT /api/history) into D1.
 * - The logger board uploads its drive logs under logger/; the Worker works
 *   out their time series and drive summary itself (drive.js), and the
 *   app's history leaves those drives alone.
 * - /mcp is a remote MCP server (JSON-RPC over streamable HTTP, JSON
 *   replies) with read-only tools over that copy: drive_history,
 *   fault_history, trend, list_files, read_log.
 *
 * Cloudflare Access sits in front of the whole hostname; this Worker also
 * checks the Access token itself on every request (ACCESS_AUD,
 * ACCESS_TEAM_DOMAIN), so a missing or misconfigured Access application
 * fails closed. The app authenticates with an Access service token; people
 * and MCP portals with their Access login.
 *
 * No dependencies: plain JavaScript, deployed with `wrangler deploy`.
 */

import { BANK2, buildSeries, isDriveLog, readLog, summarizeDrive } from "./drive.js";

export const SERVER = { name: "n63read-cloud", version: "1.0.0" };
export const PROTOCOLS = ["2025-06-18", "2025-03-26", "2024-11-05"];
const MAX_UPLOAD = 50 * 1024 * 1024;
export const LOGGER = "logger/";          // where the logger board's drive logs go
export const SWEEP = ".sweep.csv";        // a logger drive's second file
const MAX_ROWS = 500;
const MAX_TEXT = 60000;

// ------------------------------------------------------------------ Access

let jwksCache = null;
const JWKS_TTL_MS = 60 * 60 * 1000;

function b64urlBytes(s) {
  s = s.replace(/-/g, "+").replace(/_/g, "/");
  if (s.length % 4) s += "=".repeat(4 - (s.length % 4));
  const bin = atob(s);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function b64urlJson(s) {
  return JSON.parse(new TextDecoder().decode(b64urlBytes(s)));
}

function teamDomain(env) {
  return (env.ACCESS_TEAM_DOMAIN || "").replace(/\/$/, "");
}

function audiences(env) {
  return (env.ACCESS_AUD || "").split(",").map((s) => s.trim()).filter(Boolean);
}

async function jwks(env, fetcher = fetch) {
  const now = Date.now();
  if (jwksCache && now - jwksCache.at < JWKS_TTL_MS) return jwksCache.keys;
  const res = await fetcher(`${teamDomain(env)}/cdn-cgi/access/certs`);
  if (!res.ok) throw new Error(`JWKS fetch failed: ${res.status}`);
  const body = await res.json();
  jwksCache = { keys: body.keys || [], at: now };
  return jwksCache.keys;
}

export function resetJwksCache() {
  jwksCache = null;
}

function cookie(header, name) {
  for (const part of (header || "").split(";")) {
    const [k, ...v] = part.trim().split("=");
    if (k === name) return v.join("=");
  }
  return null;
}

/** {ok, who} for a request carrying a valid Access token for this app. */
export async function verifyAccess(request, env, fetcher = fetch) {
  if (!audiences(env).length || !teamDomain(env)) {
    return { ok: false, reason: "Access is not configured (ACCESS_AUD, ACCESS_TEAM_DOMAIN)" };
  }
  const token = request.headers.get("Cf-Access-Jwt-Assertion") ||
    cookie(request.headers.get("Cookie"), "CF_Authorization");
  if (!token) return { ok: false, reason: "missing Access token" };
  const parts = token.split(".");
  if (parts.length !== 3) return { ok: false, reason: "malformed token" };
  let header, payload;
  try {
    header = b64urlJson(parts[0]);
    payload = b64urlJson(parts[1]);
  } catch {
    return { ok: false, reason: "undecodable token" };
  }
  if (header.alg !== "RS256" || !header.kid) return { ok: false, reason: "unexpected alg" };
  const jwk = (await jwks(env, fetcher)).find((k) => k.kid === header.kid);
  if (!jwk) return { ok: false, reason: "unknown key id" };
  const key = await crypto.subtle.importKey(
    "jwk", { kty: jwk.kty, n: jwk.n, e: jwk.e, alg: "RS256", ext: true },
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
  const valid = await crypto.subtle.verify(
    "RSASSA-PKCS1-v1_5", key, b64urlBytes(parts[2]),
    new TextEncoder().encode(`${parts[0]}.${parts[1]}`));
  if (!valid) return { ok: false, reason: "bad signature" };
  const auds = Array.isArray(payload.aud) ? payload.aud : payload.aud ? [payload.aud] : [];
  if (!auds.some((a) => audiences(env).includes(a))) return { ok: false, reason: "aud mismatch" };
  if (payload.iss && payload.iss.replace(/\/$/, "") !== teamDomain(env)) {
    return { ok: false, reason: "iss mismatch" };
  }
  if (payload.exp && payload.exp * 1000 < Date.now()) return { ok: false, reason: "expired" };
  return { ok: true, who: payload.email || payload.common_name || "service" };
}

// ------------------------------------------------------------------ helpers

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status, headers: { "content-type": "application/json" },
  });
}

/** A safe R2 key from a path in the logs folder: forward slashes, no dot
 * segments, ordinary characters only. Null if it isn't one. */
export function safeKey(path) {
  let p;
  try {
    p = decodeURIComponent(path);
  } catch {
    return null;
  }
  p = p.replace(/\\/g, "/").replace(/^\/+/, "");
  if (!p || p.length > 512) return null;
  const parts = p.split("/");
  if (parts.some((s) => !s || s === "." || s === ".." || !/^[\w .@()+,=-]+$/.test(s))) {
    return null;
  }
  return parts.join("/");
}

async function sha256Hex(buffer) {
  const d = await crypto.subtle.digest("SHA-256", buffer);
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// ------------------------------------------------------------------ CSV

/** Rows of a CSV (quoted fields allowed), delimiter guessed from the header. */
export function parseCsv(text) {
  text = text.replace(/^﻿/, "");
  const nl = text.indexOf("\n");
  const first = nl < 0 ? text : text.slice(0, nl);
  const delim = [",", ";", "\t"].reduce(
    (best, d) => (first.split(d).length > first.split(best).length ? d : best), ",");
  const rows = [];
  let row = [], field = "", quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') { field += '"'; i++; }
      else if (c === '"') quoted = false;
      else field += c;
    } else if (c === '"') quoted = true;
    else if (c === delim) { row.push(field); field = ""; }
    else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++;
      row.push(field); field = "";
      if (row.length > 1 || row[0] !== "") rows.push(row);
      row = [];
    } else field += c;
  }
  if (field !== "" || row.length) { row.push(field); rows.push(row); }
  return rows;
}

function number(v) {
  if (v === undefined || v === "") return null;
  const n = Number(String(v).replace(",", "."));
  return Number.isFinite(n) ? n : null;
}

/** A window of a drive log: rows between start and end seconds, chosen
 * columns, every `resample` seconds at most; at most MAX_ROWS rows. */
export function logWindow(text, { start = null, end = null, columns = null, resample = 0 } = {}) {
  const rows = parseCsv(text);
  if (!rows.length) return { columns: [], rows: [], total: 0 };
  const header = rows[0].map((h) => h.trim());
  const want = columns && columns.length
    ? header.map((h, i) => (i === 0 || columns.includes(h) ? i : -1)).filter((i) => i >= 0)
    : header.map((_, i) => i);
  const out = [];
  let last = null, total = 0;
  for (const r of rows.slice(1)) {
    const t = number(r[0]);
    if (t === null) continue;
    total++;
    if (start !== null && t < start) continue;
    if (end !== null && t > end) break;
    if (resample && last !== null && t - last < resample) continue;
    last = t;
    out.push(want.map((i) => r[i] ?? ""));
    if (out.length >= MAX_ROWS) break;
  }
  return { columns: want.map((i) => header[i]), rows: out, total };
}

const EVENTS_MAX = 200;

/** A drive log's rows: {header, rows: [[t, {name: cell}]]} for numeric t. */
export function logRows(text) {
  const rows = parseCsv(text);
  if (!rows.length) return { header: [], rows: [] };
  const header = rows[0].map((h) => h.trim());
  const out = [];
  for (const r of rows.slice(1)) {
    const t = number(r[0]);
    if (t === null) continue;
    const cells = {};
    header.forEach((h, i) => { if (i) cells[h] = r[i] ?? ""; });
    out.push([t, cells]);
  }
  return { header, rows: out };
}

/** Bank 1 against bank 2 for every NAME / NAME@0x13 pair in a log. */
export function compareBanks(text) {
  const { header, rows } = logRows(text);
  const pairs = header.filter((n) => n && !n.endsWith(BANK2) && header.includes(n + BANK2));
  const mean = (xs) => xs.reduce((s, x) => s + x, 0) / xs.length;
  return pairs.map((b1) => {
    const both = rows.map(([t, c]) => [t, number(c[b1]), number(c[b1 + BANK2])])
      .filter(([, x, y]) => x !== null && y !== null);
    if (!both.length) return { channel: b1, rows: 0 };
    const diffs = both.map(([t, x, y]) => [t, x - y]);
    const [tMax, dMax] = diffs.reduce((m, d) => (Math.abs(d[1]) > Math.abs(m[1]) ? d : m));
    return { channel: b1, rows: both.length, bank1_mean: mean(both.map((b) => b[1])),
      bank2_mean: mean(both.map((b) => b[2])), difference_mean: mean(diffs.map((d) => d[1])),
      largest_difference: dMax, largest_at_s: tMax };
  });
}

/** Alerts and reconnects the log recorded, crossings of a threshold, and
 * counters going up (the misfire counters by default). */
export function findEvents(text, { column = null, above = null, below = null, counters = null } = {}) {
  const { header, rows } = logRows(text);
  const events = [];
  if (header.includes("Event")) {
    for (const [t, c] of rows) if (c.Event) events.push([t, c.Event]);
  }
  if (column) {
    if (!header.includes(column)) throw new ToolError(`no column ${column} in this log`);
    if (above === null && below === null) throw new ToolError("give above or below with column");
    let outside = false, since = null;
    for (const [t, c] of rows) {
      const x = number(c[column]);
      if (x === null) continue;
      const high = above !== null && x > above, low = below !== null && x < below;
      const now = high || low;
      if (now && !outside) {
        since = t;
        events.push([t, `${column} ${high ? `above ${above}` : `below ${below}`} (${x})`]);
      } else if (outside && !now) {
        events.push([t, `${column} back in range after ${(t - since).toFixed(1)} s`]);
      }
      outside = now;
    }
  }
  const watch = counters || header.filter((n) => n.startsWith("ZAEHLER_AUSSETZ"));
  for (const name of watch) {
    if (!header.includes(name)) continue;
    let prev = null;
    for (const [t, c] of rows) {
      const x = number(c[name]);
      if (x === null) continue;
      if (prev !== null && x > prev) events.push([t, `${name} went up ${prev} -> ${x}`]);
      prev = x;
    }
  }
  events.sort((a, b) => a[0] - b[0]);
  return { count: events.length, events: events.slice(0, EVENTS_MAX)
    .map(([t, what]) => ({ at_s: t, what })) };
}

// ------------------------------------------------------------------ D1

export const SCHEMA = [
  // Drives and faults belong to whoever sent them (`source`): each app
  // installation by its name, older apps as "legacy", the board as "logger".
  `CREATE TABLE IF NOT EXISTS drives (source TEXT NOT NULL, file TEXT NOT NULL, start TEXT,
     data TEXT NOT NULL, PRIMARY KEY (source, file))`,
  `CREATE INDEX IF NOT EXISTS drives_start ON drives (start)`,
  `CREATE TABLE IF NOT EXISTS faults (source TEXT NOT NULL, module TEXT NOT NULL,
     code TEXT NOT NULL, now TEXT, last_seen TEXT, data TEXT NOT NULL,
     PRIMARY KEY (source, module, code))`,
  `CREATE TABLE IF NOT EXISTS uploads (key TEXT PRIMARY KEY, size INTEGER, sha256 TEXT,
     uploaded TEXT)`,
  `CREATE TABLE IF NOT EXISTS templates (name TEXT PRIMARY KEY, channels TEXT,
     updated TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0)`,
  // One channel's values from one drive (in pieces when long): t0/t1 in UTC
  // seconds, data = [[seconds after t0, value], ...].
  `CREATE TABLE IF NOT EXISTS series (drive TEXT NOT NULL, channel TEXT NOT NULL,
     t0 REAL NOT NULL, t1 REAL NOT NULL, n INTEGER NOT NULL, unit TEXT, min REAL,
     max REAL, mean REAL, approx INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL,
     PRIMARY KEY (drive, channel, t0))`,
  `CREATE INDEX IF NOT EXISTS series_channel ON series (channel, t0)`,
];

const MAX_POINTS = 2000;

/** Replace one drive's series: {start, approx, chunks: [{name, unit, t0, t1, n, min,
 * max, mean, data}]}. */
async function putSeries(env, drive, body) {
  const chunks = Array.isArray(body.chunks) ? body.chunks : null;
  if (!chunks) throw new ToolError("body: {start, approx, chunks: [...]}");
  for (const c of chunks) {
    if (!c || typeof c.name !== "string" || !Array.isArray(c.data) ||
        !Number.isFinite(c.t0) || !Number.isFinite(c.t1)) {
      throw new ToolError("bad chunk");
    }
  }
  const stmts = [env.DB.prepare("DELETE FROM series WHERE drive = ?").bind(drive)];
  for (const c of chunks) {
    stmts.push(insertChunk(env, drive, body.approx,
      { ...c, n: c.data.length, json: JSON.stringify(c.data) }));
  }
  await ensureSchema(env);
  await env.DB.batch(stmts);
  return { drive, channels: new Set(chunks.map((c) => c.name)).size, chunks: chunks.length };
}

/** The statement storing one chunk whose data is already JSON (`json`). */
function insertChunk(env, drive, approx, c) {
  return env.DB.prepare(
    `INSERT OR REPLACE INTO series (drive, channel, t0, t1, n, unit, min, max, mean, approx,
     data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`).bind(
    drive, c.name, c.t0, c.t1, c.n, String(c.unit || ""), number(c.min),
    number(c.max), number(c.mean), approx ? 1 : 0, c.json);
}

async function listChannels(env, match) {
  await ensureSchema(env);
  const r = await env.DB.prepare(
    `SELECT channel, MAX(unit) AS unit, COUNT(DISTINCT drive) AS drives, SUM(n) AS values_,
     MIN(t0) AS first, MAX(t1) AS last FROM series
     WHERE (? IS NULL OR channel LIKE ?) GROUP BY channel ORDER BY channel`)
    .bind(match ? 1 : null, match ? `%${match}%` : null).all();
  return (r.results || []).map((x) => ({ channel: x.channel, unit: x.unit, drives: x.drives,
    values: x.values_, first: iso(x.first), last: iso(x.last) }));
}

function iso(seconds) {
  return seconds == null ? null : new Date(seconds * 1000).toISOString();
}

function when(v) {
  if (v === undefined || v === null || v === "") return null;
  const n = Number(v);
  if (Number.isFinite(n)) return n;
  const t = Date.parse(String(v));
  return Number.isFinite(t) ? t / 1000 : null;
}

/** One channel across drives, between start and end (UTC; ISO or seconds),
 * averaged into at most `points` buckets with min and max when longer. */
export async function channelSeries(env, a) {
  const channel = String(a.channel || "");
  if (!channel) throw new ToolError("channel: a name from list_channels");
  const start = when(a.start), end = when(a.end);
  const max = Math.max(10, Math.min(MAX_POINTS, Number(a.points) || 500));
  await ensureSchema(env);
  const r = await env.DB.prepare(
    `SELECT drive, t0, unit, approx, data FROM series WHERE channel = ?
     AND (? IS NULL OR t1 >= ?) AND (? IS NULL OR t0 <= ?) ORDER BY t0`)
    .bind(channel, start, start, end, end).all();
  const rows = r.results || [];
  const pts = [];
  const drives = new Set();
  let unit = "", approx = false;
  for (const row of rows) {
    unit = row.unit || unit;
    approx = approx || !!row.approx;
    for (const [dt, v] of JSON.parse(row.data)) {
      const t = row.t0 + dt;
      if ((start === null || t >= start) && (end === null || t <= end)) {
        pts.push([t, v]);
        drives.add(row.drive);
      }
    }
  }
  pts.sort((x, y) => x[0] - y[0]);
  const out = { channel, unit, drives: [...drives], count: pts.length, approx_times: approx };
  if (pts.length <= max) {
    out.points = pts.map(([t, v]) => [iso(t), v]);
    return out;
  }
  // Buckets of equal time; gaps between drives stay gaps.
  const t0 = pts[0][0], span = pts[pts.length - 1][0] - t0 || 1;
  const buckets = new Map();
  for (const [t, v] of pts) {
    const k = Math.min(max - 1, Math.floor(((t - t0) / span) * max));
    const b = buckets.get(k) || { t: 0, sum: 0, n: 0, min: Infinity, max: -Infinity };
    b.t += t; b.sum += v; b.n++; b.min = Math.min(b.min, v); b.max = Math.max(b.max, v);
    buckets.set(k, b);
  }
  out.resampled = true;
  out.columns = ["time", "mean", "min", "max", "n"];
  out.points = [...buckets.keys()].sort((x, y) => x - y).map((k) => {
    const b = buckets.get(k);
    return [iso(b.t / b.n), +(b.sum / b.n).toFixed(6), b.min, b.max, b.n];
  });
  return out;
}

/** Per-drive min, mean and max of one channel, straight from the stored pieces. */
async function channelByDrive(env, channel) {
  await ensureSchema(env);
  const r = await env.DB.prepare(
    `SELECT drive, MIN(t0) AS t0, SUM(n) AS n, MIN(min) AS lo, MAX(max) AS hi,
     SUM(mean * n) / SUM(n) AS mean, MAX(unit) AS unit FROM series WHERE channel = ?
     GROUP BY drive ORDER BY t0`).bind(channel).all();
  return (r.results || []).map((x) => ({ drive: x.drive, start: iso(x.t0), n: x.n,
    min: x.lo, mean: x.mean === null ? null : +x.mean.toFixed(6), max: x.hi, unit: x.unit }));
}

const MAX_TEMPLATE_NAME = 100;
const MAX_TEMPLATE_CHANNELS = 2000;

async function templates(env) {
  await ensureSchema(env);
  const r = await env.DB.prepare(
    "SELECT name, channels, updated, deleted FROM templates ORDER BY name").all();
  return (r.results || []).map((t) => ({ name: t.name, updated: t.updated,
    deleted: !!t.deleted, channels: t.deleted ? null : JSON.parse(t.channels) }));
}

/** Apply [{name, channels}] (channels null: deleted) and return them all.
 * A deletion is kept as a marker, so other PCs learn of it. */
async function putTemplates(env, changes) {
  if (!Array.isArray(changes)) throw new ToolError("changes: a list");
  const now = new Date().toISOString();
  const stmts = [];
  for (const c of changes) {
    const name = c && typeof c.name === "string" ? c.name.trim() : "";
    if (!name || name.length > MAX_TEMPLATE_NAME) throw new ToolError("bad template name");
    const ch = c.channels;
    if (ch !== null && !(Array.isArray(ch) && ch.length <= MAX_TEMPLATE_CHANNELS &&
        ch.every((x) => typeof x === "string" && x.length <= 200))) {
      throw new ToolError(`bad channels for ${name}`);
    }
    stmts.push(env.DB.prepare(
      "INSERT OR REPLACE INTO templates (name, channels, updated, deleted) VALUES (?, ?, ?, ?)")
      .bind(name, ch === null ? null : JSON.stringify(ch), now, ch === null ? 1 : 0));
  }
  await ensureSchema(env);
  if (stmts.length) await env.DB.batch(stmts);
  return templates(env);
}

const schemaReady = new WeakSet();       // per D1 binding, once per isolate

// Tables from before sources: the rows they hold become "legacy" (the board's
// drives "logger").
const MIGRATE = [
  `CREATE TABLE drives_new (source TEXT NOT NULL, file TEXT NOT NULL, start TEXT,
     data TEXT NOT NULL, PRIMARY KEY (source, file))`,
  `INSERT INTO drives_new (source, file, start, data) SELECT CASE WHEN substr(file, 1, 7) =
     'logger/' THEN 'logger' ELSE 'legacy' END, file, start, data FROM drives`,
  `DROP TABLE drives`,
  `ALTER TABLE drives_new RENAME TO drives`,
  `CREATE INDEX IF NOT EXISTS drives_start ON drives (start)`,
  `CREATE TABLE faults_new (source TEXT NOT NULL, module TEXT NOT NULL, code TEXT NOT NULL,
     now TEXT, last_seen TEXT, data TEXT NOT NULL, PRIMARY KEY (source, module, code))`,
  `INSERT INTO faults_new (source, module, code, now, last_seen, data) SELECT 'legacy',
     module, code, now, last_seen, data FROM faults`,
  `DROP TABLE faults`,
  `ALTER TABLE faults_new RENAME TO faults`,
];

async function ensureSchema(env) {
  if (schemaReady.has(env.DB)) return;
  await env.DB.batch(SCHEMA.map((s) => env.DB.prepare(s)));
  const cols = await env.DB.prepare("SELECT name FROM pragma_table_info('drives')").all();
  if (!(cols.results || []).some((c) => c.name === "source")) {
    await env.DB.batch(MIGRATE.map((s) => env.DB.prepare(s)));
  }
  schemaReady.add(env.DB);
}

export const LEGACY = "legacy";             // apps from before sources
const MAX_HISTORY = 20000;                  // drives or faults in one upload
const SOURCE_RE = /^[\w .@()+,=-]{1,64}$/;

/** Who a history upload is from: the app's installation name, or "legacy". */
function sourceOf(body) {
  const s = typeof body.source === "string" ? body.source.trim() : "";
  if (!s) return LEGACY;
  if (!SOURCE_RE.test(s) || s === "logger") throw new ToolError("bad source");
  return s;
}

/** One installation's summaries replace that installation's alone: another
 * PC's drives and faults, older apps' ("legacy") and the board's stay. */
async function putHistory(env, body) {
  const drives = Array.isArray(body.drives) ? body.drives : [];
  const faults = Array.isArray(body.faults) ? body.faults : [];
  if (drives.length > MAX_HISTORY || faults.length > MAX_HISTORY) {
    throw new ToolError(`at most ${MAX_HISTORY} drives and ${MAX_HISTORY} faults`);
  }
  const source = sourceOf(body);
  await ensureSchema(env);
  const stmts = [
    env.DB.prepare("DELETE FROM drives WHERE source = ?").bind(source),
    env.DB.prepare("DELETE FROM faults WHERE source = ?").bind(source)];
  for (const d of drives) {
    if (!d || typeof d.file !== "string" || d.file.startsWith(LOGGER)) continue;
    stmts.push(env.DB.prepare(
      "INSERT OR REPLACE INTO drives (source, file, start, data) VALUES (?, ?, ?, ?)")
      .bind(source, d.file, String(d.start || ""), JSON.stringify(d)));
  }
  for (const f of faults) {
    if (!f || typeof f.code !== "string" || typeof f.module !== "string") continue;
    stmts.push(env.DB.prepare(
      `INSERT OR REPLACE INTO faults (source, module, code, now, last_seen, data)
       VALUES (?, ?, ?, ?, ?, ?)`)
      .bind(source, f.module, f.code, String(f.now || ""), String(f.last_seen || ""),
        JSON.stringify(f)));
  }
  await env.DB.batch(stmts);
  return { source, drives: drives.length, faults: faults.length };
}

/** Units the app stored for these channels (a bank 2 twin takes bank 1's). */
async function knownUnits(env, names) {
  const base = (n) => (n.endsWith(BANK2) ? n.slice(0, -BANK2.length) : n);
  const r = await env.DB.prepare(
    `SELECT channel, MAX(unit) AS unit FROM series WHERE unit != '' AND channel IN
     (SELECT value FROM json_each(?)) GROUP BY channel`)
    .bind(JSON.stringify([...new Set(names.map(base))])).all();
  const units = Object.fromEntries((r.results || []).map((x) => [x.channel, x.unit]));
  return Object.fromEntries(names.map((n) => [n, units[base(n)] || ""]));
}

// D1 allows 1,000 queries a request, and a sweep file has thousands of
// channels: chunks go in many at a time, as one JSON parameter unpacked by
// json_each, up to this many bytes a statement.
const INSERT_BYTES = 400000;

function insertMany(env, drive, approx, json) {
  return env.DB.prepare(
    `INSERT OR REPLACE INTO series (drive, channel, t0, t1, n, unit, min, max, mean, approx,
     data) SELECT ?, value->>'name', value->>'t0', value->>'t1', value->>'n', value->>'unit',
     value->>'min', value->>'max', value->>'mean', ?, value->>'data' FROM json_each(?)`)
    .bind(drive, approx ? 1 : 0, json);
}

/** The drive a logger file belongs to: the logger writes each drive as a
 * fast file (logger/<start>.csv, every cycle) and a sweep file
 * (logger/<start>.sweep.csv, everything else in rotation); both are stored
 * under the fast file's name. */
export function driveKey(key) {
  return key.endsWith(SWEEP) ? key.slice(0, -SWEEP.length) + ".csv" : key;
}

/** A logger drive log just stored (its bytes): its series and its drive row. */
async function ingestLog(env, key, bytes) {
  if (!isDriveLog(bytes)) return { processed: false, reason: "not a drive log" };
  const drive = driveKey(key), sweep = drive !== key;
  const now = Date.now() / 1000;
  const log = readLog(bytes);
  const series = buildSeries(log, now);
  await ensureSchema(env);
  const units = await knownUnits(env, series.names);
  // This file's channels only (the drive's other file keeps its own). A
  // re-upload replaces whatever an interrupted one left.
  await env.DB.prepare(`DELETE FROM series WHERE drive = ? AND channel IN
    (SELECT value FROM json_each(?))`).bind(drive, JSON.stringify(series.names)).run();
  // A channel's JSON is made as it's needed and sent a few hundred KB at a
  // time, to keep within the Worker's memory and D1's query count.
  let channels = 0, part = [], partBytes = 0;
  const send = async () => {
    if (part.length) await insertMany(env, drive, series.approx, `[${part.join(",")}]`).run();
    part = [];
    partBytes = 0;
  };
  for (const chunks of series.channels()) {
    for (const c of chunks) {
      const one = `{"name":${JSON.stringify(c.name)},"t0":${c.t0},"t1":${c.t1},"n":${c.n},` +
        `"unit":${JSON.stringify(units[c.name] || "")},"min":${c.min},"max":${c.max},` +
        `"mean":${c.mean},"data":${c.json}}`;
      if (partBytes + one.length > INSERT_BYTES) await send();
      part.push(one);
      partBytes += one.length;
    }
    channels++;
  }
  await send();
  // The summary from both files once both are here.
  const obj = await env.FILES.get(sweep ? drive : drive.slice(0, -".csv".length) + SWEEP);
  let other = null;
  if (obj) {
    try {
      other = readLog(new Uint8Array(await obj.arrayBuffer()));
    } catch {
      other = null;                     // that file says why when it's uploaded
    }
  }
  const row = summarizeDrive((sweep ? [other, log] : [log, other]).filter(Boolean), drive, now);
  await env.DB.prepare(row
    ? "INSERT OR REPLACE INTO drives (source, file, start, data) VALUES ('logger', ?, ?, ?)"
    : "DELETE FROM drives WHERE source = 'logger' AND file = ?")
    .bind(...(row ? [drive, String(row.start), JSON.stringify(row)] : [drive])).run();
  return { processed: true, drive_file: drive, channels, approx_time: series.approx,
    with_other_file: !!other,
    drive: row ? { start: row.start, minutes: row.minutes, km: row.km } : null };
}

/** The newest `limit` drives, oldest first; a log two installations both
 * have counts once. */
async function drives(env, limit) {
  await ensureSchema(env);
  // A log is kept once per installation that has it: a few times `limit` rows cover it.
  const r = await env.DB.prepare(
    "SELECT file, data FROM drives ORDER BY start DESC, source LIMIT ?").bind(limit * 4).all();
  const seen = new Set(), out = [];
  for (const x of r.results || []) {
    if (seen.has(x.file)) continue;
    seen.add(x.file);
    out.push(JSON.parse(x.data));
    if (out.length >= limit) break;
  }
  return out.reverse();
}

/** Each fault once: as the installation that saw it most recently has it,
 * with the earliest first sighting any of them has. "current": not gone. */
async function faults(env, which) {
  await ensureSchema(env);
  const r = await env.DB.prepare("SELECT module, code, data FROM faults").all();
  const by = new Map();
  for (const x of r.results || []) {
    const f = JSON.parse(x.data), key = `${x.module} ${x.code}`;
    const have = by.get(key);
    if (!have) { by.set(key, f); continue; }
    const [newer, older] = String(f.last_seen || "") > String(have.last_seen || "")
      ? [f, have] : [have, f];
    const merged = { ...newer };
    if (older.first_seen && (!newer.first_seen || older.first_seen < newer.first_seen)) {
      merged.first_seen = older.first_seen;
      if (older.first_km !== undefined) merged.first_km = older.first_km;
    }
    by.set(key, merged);
  }
  const all = [...by.values()].filter((f) => which === "all" || f.now !== "gone");
  const k = (f) => `${f.now || ""} ${f.module} ${f.code}`;
  return all.sort((a, b) => (k(a) < k(b) ? -1 : k(a) > k(b) ? 1 : 0));
}

// ------------------------------------------------------------------ MCP tools

export const TOOLS = [
  {
    name: "drive_history",
    description: "The car's long-term statistics, one entry per drive (newest last): misfires, " +
      "uneven running and spark burn time per cylinder, knock retard and fuel trims per bank, " +
      "temperatures, battery, tire pressures, distance, fuel used and economy (fuel_l, " +
      "l_per_100km, mpg, tank_used_l). From the logs the BMW Analyzer app " +
      "uploaded (2018 BMW M550i, N63TU2 V8).",
    inputSchema: { type: "object", properties: {
      limit: { type: "integer", description: "Drives, newest; default 30" } } },
  },
  {
    name: "fault_history",
    description: "Every fault read from the car's modules: first and last seen, how many reads " +
      "listed it, whether it is present, stored or gone now, the odometer when it set and how " +
      "often it occurred. Pass code for the full stored details of one fault.",
    inputSchema: { type: "object", properties: {
      which: { type: "string", description: "current (default) or all" },
      code: { type: "string", description: "One fault code, e.g. 4806DA: returns it with " +
        "everything the module stored (snapshots: odometer, time, conditions; counters)" } } },
  },
  {
    name: "trend",
    description: "One statistic across drives, e.g. misfires_5, uneven_5, spark_5, knock_b1, " +
      "trim_b2, tire_2, battery_min, coolant_max, km, mpg, l_per_100km, fuel_l. " +
      "Returns [start, value] pairs.",
    inputSchema: { type: "object", properties: {
      key: { type: "string" }, limit: { type: "integer", description: "Drives; default 100" } },
      required: ["key"] },
  },
  {
    name: "list_files",
    description: "The uploaded drive logs and fault lists, newest first.",
    inputSchema: { type: "object", properties: {
      prefix: { type: "string" }, limit: { type: "integer", description: "Default 50" } } },
  },
  {
    name: "list_channels",
    description: "Channels in the time-series store (every value of every uploaded drive log, " +
      "with its UTC time): unit, how many drives logged it, how many values, first and last.",
    inputSchema: { type: "object", properties: {
      match: { type: "string", description: "Part of a channel name, e.g. ILUZ or RDC" } } },
  },
  {
    name: "channel_series",
    description: "One channel's values across all drives as a time series, in UTC. Narrow " +
      "with start and end (ISO times or seconds since 1970). Long results are averaged into " +
      "`points` time buckets (default 500) with min and max. by_drive: true gives per-drive " +
      "min, mean and max instead.",
    inputSchema: { type: "object", properties: {
      channel: { type: "string" }, start: { type: "string" }, end: { type: "string" },
      points: { type: "integer" }, by_drive: { type: "boolean" } }, required: ["channel"] },
  },
  {
    name: "compare_banks",
    description: "Bank 1 against bank 2 in one uploaded drive log: for every channel logged " +
      "on both DMEs (NAME and NAME@0x13), each bank's mean, the mean difference, and the " +
      "largest difference and when.",
    inputSchema: { type: "object", properties: {
      name: { type: "string", description: "A file from list_files" } }, required: ["name"] },
  },
  {
    name: "find_events",
    description: "Events in one uploaded drive log: what the log recorded (alerts, " +
      "reconnects, the odometer), a column crossing above or below a value, and counters " +
      "going up (the misfire counters by default).",
    inputSchema: { type: "object", properties: {
      name: { type: "string", description: "A file from list_files" },
      column: { type: "string" }, above: { type: "number" }, below: { type: "number" },
      counters: { type: "array", items: { type: "string" } } }, required: ["name"] },
  },
  {
    name: "list_templates",
    description: "The channel templates saved in the BMW Analyzer app (watch lists: a name and " +
      "its channel names), as synced to the cloud.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "read_log",
    description: "Part of an uploaded drive log: rows between start and end seconds, chosen " +
      "columns, resampled to one row every `resample` seconds; at most 500 rows.",
    inputSchema: { type: "object", properties: {
      name: { type: "string", description: "A file from list_files" },
      start: { type: "number" }, end: { type: "number" },
      columns: { type: "array", items: { type: "string" } },
      resample: { type: "number" } }, required: ["name"] },
  },
];

class ToolError extends Error {}

async function callTool(env, name, a) {
  a = a || {};
  if (name === "drive_history") {
    const limit = Math.max(1, Math.min(500, Number(a.limit) || 30));
    return { drives: await drives(env, limit) };
  }
  if (name === "fault_history") {
    const all = await faults(env, a.code ? "all" : a.which);
    if (a.code) {
      const code = String(a.code).toUpperCase();
      return { faults: all.filter((f) => f.code.toUpperCase() === code) };
    }
    // The stored details are long; ask for one code to see them.
    return { faults: all.map(({ details, ...f }) => ({ ...f, has_details: !!details })) };
  }
  if (name === "trend") {
    if (!/^[a-z0-9_]+$/.test(String(a.key || ""))) throw new ToolError("key: a statistic name");
    const limit = Math.max(1, Math.min(1000, Number(a.limit) || 100));
    return { key: a.key, points: (await drives(env, limit))
      .filter((d) => typeof d[a.key] === "number").map((d) => [d.start, d[a.key]]) };
  }
  if (name === "list_files") {
    const listed = await env.FILES.list({ prefix: a.prefix ? String(a.prefix) : undefined,
      limit: 1000 });
    const files = listed.objects.sort((x, y) => (y.uploaded > x.uploaded ? 1 : -1))
      .slice(0, Math.max(1, Math.min(1000, Number(a.limit) || 50)))
      .map((o) => ({ name: o.key, size: o.size, uploaded: o.uploaded }));
    return { files };
  }
  if (name === "compare_banks" || name === "find_events") {
    const key = safeKey(String(a.name || ""));
    if (!key) throw new ToolError("name: a file from list_files");
    const obj = await env.FILES.get(key);
    if (!obj) throw new ToolError(`no file ${key}; see list_files`);
    const text = await obj.text();
    if (name === "compare_banks") {
      const pairs = compareBanks(text);
      if (!pairs.length) throw new ToolError(`${key} has no bank pairs (NAME and NAME@0x13)`);
      return { file: key, pairs };
    }
    return { file: key, ...findEvents(text, { column: a.column ? String(a.column) : null,
      above: number(a.above), below: number(a.below),
      counters: Array.isArray(a.counters) ? a.counters.map(String) : null }) };
  }
  if (name === "list_channels") return { channels: await listChannels(env, a.match) };
  if (name === "channel_series") {
    if (a.by_drive) return { channel: a.channel, drives: await channelByDrive(env, a.channel) };
    return channelSeries(env, a);
  }
  if (name === "list_templates") {
    return { templates: (await templates(env)).filter((t) => !t.deleted)
      .map((t) => ({ name: t.name, channels: t.channels, updated: t.updated })) };
  }
  if (name === "read_log") {
    const key = safeKey(String(a.name || ""));
    if (!key) throw new ToolError("name: a file from list_files");
    const obj = await env.FILES.get(key);
    if (!obj) throw new ToolError(`no file ${key}; see list_files`);
    return logWindow(await obj.text(), {
      start: number(a.start), end: number(a.end),
      columns: Array.isArray(a.columns) ? a.columns.map(String) : null,
      resample: number(a.resample) || 0 });
  }
  throw new ToolError(`unknown tool ${name}`);
}

const INSTRUCTIONS = "The cloud copy of a 2018 BMW M550i's drive logs and fault reads, " +
  "uploaded by the BMW Analyzer (n63read) app. Start with drive_history or fault_history; " +
  "trend follows one statistic (misfires_5, knock_b1...) over time; list_files and read_log " +
  "look inside a drive, compare_banks and find_events analyse one; list_channels and " +
  "channel_series follow any channel across every " +
  "drive in UTC time; list_templates shows the app's saved channel sets. Read-only.";

/** One JSON-RPC message in, its reply out (null for a notification). */
export async function rpc(env, msg) {
  const reply = (result) => ({ jsonrpc: "2.0", id: msg.id, result });
  const error = (code, message) => ({ jsonrpc: "2.0", id: msg.id ?? null,
    error: { code, message } });
  if (!msg || msg.jsonrpc !== "2.0" || typeof msg.method !== "string") {
    return error(-32600, "invalid request");
  }
  if (msg.id === undefined) return null;                  // a notification
  switch (msg.method) {
    case "initialize": {
      const asked = msg.params && msg.params.protocolVersion;
      return reply({ protocolVersion: PROTOCOLS.includes(asked) ? asked : PROTOCOLS[0],
        capabilities: { tools: {} }, serverInfo: SERVER, instructions: INSTRUCTIONS });
    }
    case "ping":
      return reply({});
    case "tools/list":
      return reply({ tools: TOOLS });
    case "tools/call": {
      const p = msg.params || {};
      try {
        let text = JSON.stringify(await callTool(env, p.name, p.arguments));
        if (text.length > MAX_TEXT) text = text.slice(0, MAX_TEXT) + " ...(cut)";
        return reply({ content: [{ type: "text", text }], isError: false });
      } catch (e) {
        if (!(e instanceof ToolError)) console.error(e);
        return reply({ content: [{ type: "text", text: String(e.message || e) }], isError: true });
      }
    }
    default:
      return error(-32601, `method not found: ${msg.method}`);
  }
}

// ------------------------------------------------------------------ routes

export async function handle(request, env, fetcher = fetch) {
  const url = new URL(request.url);
  const auth = await verifyAccess(request, env, fetcher);
  if (!auth.ok) return json({ error: "forbidden", reason: auth.reason }, 403);

  if (url.pathname === "/mcp") {
    if (request.method === "GET") return new Response(null, { status: 405 });  // no SSE stream
    if (request.method !== "POST") return new Response(null, { status: 405 });
    let body;
    try {
      body = await request.json();
    } catch {
      return json({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "parse error" } },
        400);
    }
    if (Array.isArray(body)) {
      const replies = (await Promise.all(body.map((m) => rpc(env, m)))).filter(Boolean);
      return replies.length ? json(replies) : new Response(null, { status: 202 });
    }
    const one = await rpc(env, body);
    return one ? json(one) : new Response(null, { status: 202 });
  }

  if (url.pathname.startsWith("/api/series/") && request.method === "PUT") {
    const drive = safeKey(url.pathname.slice("/api/series/".length));
    if (!drive) return json({ error: "bad drive name" }, 400);
    let body;
    try {
      body = await request.json();
    } catch {
      return json({ error: "body: JSON {start, approx, chunks}" }, 400);
    }
    try {
      return json({ ok: true, ...(await putSeries(env, drive, body)) });
    } catch (e) {
      if (e instanceof ToolError) return json({ error: e.message }, 400);
      throw e;
    }
  }
  if (url.pathname === "/api/series" && request.method === "GET") {
    try {
      const q = Object.fromEntries(url.searchParams);
      if (!q.channel) return json({ channels: await listChannels(env, q.match) });
      return json(q.by_drive ? { drives: await channelByDrive(env, q.channel) }
        : await channelSeries(env, q));
    } catch (e) {
      if (e instanceof ToolError) return json({ error: e.message }, 400);
      throw e;
    }
  }

  if (url.pathname === "/api/templates") {
    if (request.method === "GET") return json({ templates: await templates(env) });
    if (request.method === "PUT") {
      let body;
      try {
        body = await request.json();
      } catch {
        return json({ error: "body: JSON {changes: [{name, channels}]}" }, 400);
      }
      try {
        return json({ templates: await putTemplates(env, body.changes) });
      } catch (e) {
        if (e instanceof ToolError) return json({ error: e.message }, 400);
        throw e;
      }
    }
    return new Response(null, { status: 405 });
  }

  if (url.pathname === "/api/history" && request.method === "PUT") {
    let body;
    try {
      body = await request.json();
    } catch {
      return json({ error: "body: JSON {drives, faults}" }, 400);
    }
    try {
      return json({ ok: true, ...(await putHistory(env, body)) });
    } catch (e) {
      if (e instanceof ToolError) return json({ error: e.message }, 400);
      throw e;
    }
  }

  if (url.pathname === "/api/files" && request.method === "GET") {
    await ensureSchema(env);
    const r = await env.DB.prepare("SELECT key, size, sha256, uploaded FROM uploads").all();
    return json({ files: r.results || [] });
  }

  if (url.pathname.startsWith("/api/files/")) {
    const key = safeKey(url.pathname.slice("/api/files/".length));
    if (!key) return json({ error: "bad file name" }, 400);
    if (request.method === "PUT") {
      const len = Number(request.headers.get("content-length") || 0);
      if (len > MAX_UPLOAD) return json({ error: "too large" }, 413);
      const data = await request.arrayBuffer();
      if (data.byteLength > MAX_UPLOAD) return json({ error: "too large" }, 413);
      const sha = await sha256Hex(data);
      await env.FILES.put(key, data, { httpMetadata: { contentType: "text/csv" },
        customMetadata: { sha256: sha } });
      await ensureSchema(env);
      await env.DB.prepare(
        "INSERT OR REPLACE INTO uploads (key, size, sha256, uploaded) VALUES (?, ?, ?, ?)")
        .bind(key, data.byteLength, sha, new Date().toISOString()).run();
      const stored = { ok: true, key, size: data.byteLength, sha256: sha };
      if (!key.startsWith(LOGGER)) return json(stored);
      // The file is safe in R2 either way; a log that can't be read only
      // misses its series and summary, and says why.
      try {
        return json({ ...stored, ...(await ingestLog(env, key, new Uint8Array(data))) });
      } catch (e) {
        console.error(e);
        return json({ ...stored, processed: false, reason: String(e.message || e) });
      }
    }
    if (request.method === "GET") {
      const obj = await env.FILES.get(key);
      if (!obj) return json({ error: "not found" }, 404);
      return new Response(obj.body, { headers: { "content-type": "text/csv" } });
    }
    return new Response(null, { status: 405 });
  }

  if (url.pathname === "/" || url.pathname === "/api/status") {
    return json({ ok: true, server: SERVER, who: auth.who });
  }
  return json({ error: "not found" }, 404);
}

export default {
  async fetch(request, env) {
    try {
      return await handle(request, env);
    } catch (e) {
      console.error(e);
      return json({ error: "internal error" }, 500);
    }
  },
};
