/**
 * n63read cloud: the car's logs and history, stored and served by a
 * Cloudflare Worker.
 *
 * - The app uploads every drive log and fault list (PUT /api/files/<path>)
 *   into R2, and its drive and fault summaries (PUT /api/history) into D1.
 * - The logger board uploads its drive logs under logger/; the Worker works
 *   out their time series and drive summary itself (drive.js), and the
 *   app's history leaves those drives alone.
 * - Its fault reads (<drive>.faults.csv) go into fault history as source
 *   "logger", with texts from n63read's table the app sends (FAULT_TEXTS);
 *   each failed code's odometer and count, read in P (<stamp>.details.csv),
 *   into fault_details (faultdetails.js). It also checks in (POST
 *   /api/logger/heartbeat) at power-on and every 10 s while it has Wi-Fi.
 * - What the board reads comes from here too (logger.js): the app sends the
 *   channel catalog, the page at /logger picks each channel's tier in
 *   presets, and the board downloads the active one's channels.csv
 *   (GET /api/logger/channels) when it changes.
 * - A full channel check (sweep.js), asked for on the page, goes to the
 *   board in its check-ins' replies; its answers (<drive>.check.csv) go
 *   into the catalog's.
 * - Drive health (health.js): every drive's warm samples summarized per
 *   channel and RPM x load cell, scored against each channel's recent past
 *   whenever a drive's series arrive; the Health tab (/logger/health,
 *   healthpage.js) and the drive_health tool show them.
 * - The Board tab (/logger/board, boardpage.js) shows the board's firmware
 *   and card, and sends it commands (boardcmd.js) in its check-ins'
 *   replies: list, delete or format the card, upload now, update now,
 *   restart; the board_command tool too.
 * - /mcp is a remote MCP server (JSON-RPC over streamable HTTP, JSON
 *   replies) with tools over that copy: drive_history, fault_history,
 *   trend, list_files, read_log, a drive's fault list (drive_faults), the
 *   full channel check's results (channel_check) and drive health
 *   (drive_health), all read-only; the board's commands (board_command,
 *   board_commands); and
 *   the logger's channel presets, which the logger_* tools list and change
 *   (tiers, presets, the one published to the board, versions and
 *   rollback). Nothing here writes to the car.
 *
 * Cloudflare Access sits in front of the whole hostname; this Worker also
 * checks the Access token itself on every request (ACCESS_AUD,
 * ACCESS_TEAM_DOMAIN), so a missing or misconfigured Access application
 * fails closed. The app authenticates with an Access service token; people
 * and MCP portals with their Access login.
 *
 * No dependencies: plain JavaScript, deployed with `wrangler deploy`.
 */

import { beatCommand, BOARD_COMMANDS_SCHEMA, cancelCommand, CommandError, getCommand, KINDS,
  listCommands, MAX_CARD_BODY, putCommand, putListing } from "./boardcmd.js";
import { BOARD_PAGE } from "./boardpage.js";
import { beatSettings, BOARD_SETTINGS_SCHEMA, boardRole, cancelBoardSettings, getBoardSettings,
  putBoardSettings, SettingsError } from "./boardsettings.js";
import { BANK2, buildSeries, clockEpoch, DEFAULT_TIME_ZONE, isDriveLog, loggerExtras, readLog,
  summarizeDrive } from "./drive.js";
import { channelFile, ConfigError, getCatalog, getPresets, listTiers, LOGGER_SCHEMA, presetVersions,
  putCatalog, putPresets, rollbackPreset, setTiers } from "./logger.js";
import { LOGGER_PAGE } from "./loggerpage.js";
import { absentInNotes, beatSweep, cancelSweep, CHECK, checkList, getSweep, identifiers,
  ingestCheck, markAbsent, requestSweep } from "./sweep.js";
import { tanks } from "./tanks.js";
import { backfillPieces, getValues, migrateValues, pieceStats, refreshValues, VALUES_SCHEMA }
  from "./values.js";
import { DETAILS, DETAILS_SCHEMA, ingestDetails, withDetails } from "./faultdetails.js";
import { excludeDrive, HEALTH_SCHEMA, healthDetail, healthOverview, levelOf, rebuildHealth,
  updateHealth } from "./health.js";
import { HEALTH_PAGE } from "./healthpage.js";

export const SERVER = { name: "n63read-cloud", version: "1.0.0" };
export const PROTOCOLS = ["2025-06-18", "2025-03-26", "2024-11-05"];
const MAX_UPLOAD = 50 * 1024 * 1024;
export const LOGGER = "logger/";          // where the logger board's drive logs go
const APP_LOGS = "logs/";                 // appKey
export const SWEEP = ".sweep.csv";        // a logger drive's second file
const MAX_ROWS = 500;
const MAX_TEXT = 60000;

// ------------------------------------------------------------------ Access

let jwksCache = null;
const JWKS_TTL_MS = 60 * 60 * 1000;
// A token signed with a key the cache hasn't (Access rotated its keys): the
// keys are fetched again, at most this often, rather than every request
// refused for up to an hour.
const JWKS_REFRESH_MS = 5 * 60 * 1000;
const EXP_LEEWAY_S = 60;                  // the Worker's clock and Access's may differ

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

let jwksForcedAt = 0;

async function jwks(env, fetcher = fetch, fresh = false) {
  const now = Date.now();
  if (fresh && now - jwksForcedAt < JWKS_REFRESH_MS) fresh = false;
  if (fresh) jwksForcedAt = now;
  if (!fresh && jwksCache && now - jwksCache.at < JWKS_TTL_MS) return jwksCache.keys;
  const res = await fetcher(`${teamDomain(env)}/cdn-cgi/access/certs`);
  if (!res.ok) throw new Error(`JWKS fetch failed: ${res.status}`);
  const body = await res.json();
  jwksCache = { keys: body.keys || [], at: now };
  return jwksCache.keys;
}

export function resetJwksCache() {
  jwksCache = null;
  jwksForcedAt = 0;
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
  const jwk = (await jwks(env, fetcher)).find((k) => k.kid === header.kid) ||
    (await jwks(env, fetcher, true)).find((k) => k.kid === header.kid);
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
  if (payload.exp && (payload.exp + EXP_LEEWAY_S) * 1000 < Date.now()) {
    return { ok: false, reason: "expired" };
  }
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

/** An app's name for a log, as every installation has it: one whose logs
 * sit in a "logs" folder of the logs folder sends "logs/<name>" for what the
 * others send as "<name>", and each drive was stored, listed and counted
 * twice (14-20 Oct). */
export function appKey(key) {
  return key && key.startsWith(APP_LOGS) ? key.slice(APP_LOGS.length) : key;
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
  // seconds, data = [[seconds after t0, value], ...]; v0, v1 its first and
  // last value and falls whether it ever goes down (values.js pieceStats;
  // NULL in pieces from before them until backfillPieces).
  `CREATE TABLE IF NOT EXISTS series (drive TEXT NOT NULL, channel TEXT NOT NULL,
     t0 REAL NOT NULL, t1 REAL NOT NULL, n INTEGER NOT NULL, unit TEXT, min REAL,
     max REAL, mean REAL, approx INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL,
     v0 REAL, v1 REAL, falls INTEGER, PRIMARY KEY (drive, channel, t0))`,
  `CREATE INDEX IF NOT EXISTS series_channel ON series (channel, t0)`,
  // The logger board's check-ins: one at power-on (event "boot"), then one
  // every 10 s while it has Wi-Fi; data is what it sent.
  `CREATE TABLE IF NOT EXISTS heartbeats (received TEXT NOT NULL, boot INTEGER,
     event TEXT, data TEXT NOT NULL)`,
  `CREATE INDEX IF NOT EXISTS heartbeats_received ON heartbeats (received)`,
  // A power-on's check-ins, newest first: its last fault codes and Wi-Fi
  // note on every poll of the logger page (they scanned every check-in).
  `CREATE INDEX IF NOT EXISTS heartbeats_boot ON heartbeats (boot, received)`,
  // The newest logger fault read applied per module (ingestFaults).
  `CREATE TABLE IF NOT EXISTS logger_fault_reads (module TEXT PRIMARY KEY, latest TEXT)`,
  // What the board reads: presets of channel tiers, and which is active.
  ...LOGGER_SCHEMA,
  // Each channel's newest value and range, for the logger page (values.js).
  ...VALUES_SCHEMA,
  ...DETAILS_SCHEMA,
  // A change to the board's settings from the Board tab (boardsettings.js).
  ...BOARD_SETTINGS_SCHEMA,
  // Commands for the board from the Board tab (boardcmd.js).
  ...BOARD_COMMANDS_SCHEMA,
  // Each drive's health summary and scores (health.js).
  ...HEALTH_SCHEMA,
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
  const old = await env.DB.prepare("SELECT DISTINCT channel FROM series WHERE drive = ?")
    .bind(drive).all();
  await env.DB.batch(stmts);
  await refreshValues(env, (old.results || []).map((x) => x.channel)
    .concat(chunks.map((c) => c.name)));
  return { drive, channels: new Set(chunks.map((c) => c.name)).size, chunks: chunks.length,
    health: await healthAfter(env, drive) };
}

/** A drive's health once its series are in; a failure there never fails
 * the upload (the Health tab's rebuild catches it up). */
async function healthAfter(env, drive) {
  try {
    const h = await updateHealth(env, drive);
    return { warm_min: +(h.warm_s / 60).toFixed(1), scored: h.kept };
  } catch (e) {
    console.error(e);
    return { error: String(e.message || e) };
  }
}

/** The statement storing one chunk whose data is already JSON (`json`). */
function insertChunk(env, drive, approx, c) {
  const p = pieceStats(c.data);
  return env.DB.prepare(
    `INSERT OR REPLACE INTO series (drive, channel, t0, t1, n, unit, min, max, mean, approx,
     data, v0, v1, falls) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`).bind(
    drive, c.name, c.t0, c.t1, c.n, String(c.unit || ""), number(c.min),
    number(c.max), number(c.mean), approx ? 1 : 0, c.json, p.v0, p.v1, p.falls ? 1 : 0);
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
  // A series table from before each piece's v0, v1 and falls: added, NULL
  // until POST /api/logger/values/backfill fills them (values.js reads the
  // JSON for those meanwhile).
  const scols = await env.DB.prepare("SELECT name FROM pragma_table_info('series')").all();
  if (!(scols.results || []).some((c) => c.name === "falls")) {
    await env.DB.batch(["v0 REAL", "v1 REAL", "falls INTEGER"].map((c) =>
      env.DB.prepare(`ALTER TABLE series ADD COLUMN ${c}`)));
  }
  await migrateValues(env);
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
    const file = appKey(d.file.replace(/\\/g, "/"));
    stmts.push(env.DB.prepare(
      "INSERT OR REPLACE INTO drives (source, file, start, data) VALUES (?, ?, ?, ?)")
      .bind(source, file, String(d.start || ""), JSON.stringify({ ...d, file })));
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
     data, v0, v1, falls) SELECT ?, value->>'name', value->>'t0', value->>'t1', value->>'n',
     value->>'unit', value->>'min', value->>'max', value->>'mean', ?, value->>'data',
     value->>'v0', value->>'v1', value->>'falls' FROM json_each(?)`)
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
        `"mean":${c.mean},"v0":${c.v0},"v1":${c.v1},"falls":${c.falls ? 1 : 0},` +
        `"data":${c.json}}`;
      if (partBytes + one.length > INSERT_BYTES) await send();
      part.push(one);
      partBytes += one.length;
    }
    channels++;
  }
  await send();
  await refreshValues(env, series.names);
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
  const row = await storeDriveRow(env, drive, (sweep ? [other, log] : [log, other])
    .filter(Boolean), now);
  // Identifiers the board found its modules haven't: off for good.
  const absent = await markAbsent(env, absentInNotes(log.notes));
  const health = await healthAfter(env, drive);
  return { processed: true, drive_file: drive, channels, approx_time: series.approx,
    with_other_file: !!other, absent, health,
    drive: row ? { start: row.start, minutes: row.minutes, km: row.km } : null };
}

/** A logger drive's row in History from its logs (the fast file first), or
 * none when they're too short. `uploaded`: UTC seconds, the end of a log
 * without a clock; its start is in TIME_ZONE (the car's). */
async function storeDriveRow(env, drive, logs, uploaded) {
  const row = summarizeDrive(logs, drive, uploaded, env.TIME_ZONE || DEFAULT_TIME_ZONE);
  if (row) Object.assign(row, loggerExtras(logs));
  await env.DB.prepare(row
    ? "INSERT OR REPLACE INTO drives (source, file, start, data) VALUES ('logger', ?, ?, ?)"
    : "DELETE FROM drives WHERE source = 'logger' AND file = ?")
    .bind(...(row ? [drive, String(row.start), JSON.stringify(row)] : [drive])).run();
  return row;
}

const RESUMMARIZE_BATCH = 5;

/** Work out stored logger drives' rows again from their files (after the
 * summary learns something new), RESUMMARIZE_BATCH a request, by file name
 * after `after`: {done: [{file, start}], next} (next: null when finished).
 * A log without a clock keeps the upload time it was stored with. */
async function resummarize(env, after) {
  await ensureSchema(env);
  const r = await env.DB.prepare(`SELECT file, data FROM drives WHERE source = 'logger'
    AND file > ? ORDER BY file LIMIT ?`).bind(after || "", RESUMMARIZE_BATCH).all();
  const done = [];
  for (const { file, data } of r.results || []) {
    const logs = [];
    let uploaded = null;
    for (const key of [file, file.slice(0, -".csv".length) + SWEEP]) {
      const obj = await env.FILES.get(key);
      if (!obj) continue;
      try {
        logs.push(readLog(new Uint8Array(await obj.arrayBuffer())));
      } catch {
        continue;
      }
      const t = obj.uploaded ? obj.uploaded.getTime() / 1000 : null;
      if (t && (uploaded === null || t > uploaded)) uploaded = t;
    }
    if (!logs.length) {
      done.push({ file, start: JSON.parse(data).start, skipped: "no file" });
      continue;
    }
    const row = await storeDriveRow(env, file, logs, uploaded ?? Date.now() / 1000);
    done.push({ file, start: row ? row.start : null });
  }
  const more = (r.results || []).length === RESUMMARIZE_BATCH;
  return { done, next: more ? done[done.length - 1].file : null };
}

// ------------------------------------------------------------------ logger faults

export const FAULTS = ".faults.csv";           // a logger drive's fault reads
export const FAULT_TEXTS = "ref/BMW_fault_code_texts.csv";   // n63read's, sent by the app

/** "YYYY-MM-DD HH:MM:SS" plus seconds, as local time stays local. */
function addSeconds(text, s) {
  const t = Date.parse(text.slice(0, 19).replace(" ", "T") + "Z");
  return Number.isFinite(t) ? stamp((t + s * 1000) / 1000) : "";
}

function stamp(seconds) {
  return new Date(seconds * 1000).toISOString().slice(0, 19).replace("T", " ");
}

/** One comma-separated line's fields; quotes allowed. */
function csvLine(line) {
  const out = [""];
  let quoted = false;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (quoted) {
      if (c === '"' && line[i + 1] === '"') { out[out.length - 1] += '"'; i++; }
      else if (c === '"') quoted = false;
      else out[out.length - 1] += c;
    } else if (c === '"') quoted = true;
    else if (c === ",") out.push("");
    else if (c !== "\r") out[out.length - 1] += c;
  }
  return out;
}

/** English texts for these codes, from n63read's table in R2 (absent: none).
 * Each code's line is found directly rather than parsing 10,000 rows. */
async function faultTexts(env, codes) {
  const out = {};
  if (!codes.length) return out;
  const obj = await env.FILES.get(FAULT_TEXTS);
  if (!obj) return out;
  const text = (await obj.text()).replace(/^﻿/, "");
  const at = csvLine(text.slice(0, text.indexOf("\n"))).map((h) => h.trim()).indexOf("text_en");
  if (at < 0) return out;
  for (const code of codes) {
    const i = text.indexOf(`\n${code},`);
    if (i < 0) continue;
    const end = text.indexOf("\n", i + 1);
    const row = csvLine(text.slice(i + 1, end < 0 ? undefined : end));
    if (row[at]) out[code] = row[at];
  }
  return out;
}

/** A logger drive's fault reads (n63read's fault CSV, plus time_s) merged
 * into the board's fault history, as history.fault_history does for the
 * app: a fault's first and last read, how many reads listed it and were
 * present, and `now` from its module's newest read (gone once a later read
 * doesn't list it). Each module's reads are applied once, in order, so a
 * re-upload changes nothing. */
async function ingestFaults(env, key, text) {
  const rows = parseCsv(text);
  const head = (rows.shift() || []).map((h) => h.trim());
  const col = (name) => head.indexOf(name);
  if (col("module") < 0 || col("read_at") < 0 || col("state") < 0) {
    return { processed: false, reason: "not a fault list" };
  }
  await ensureSchema(env);
  // Reads made before the board knew the time: the drive's start plus time_s.
  const drive = key.slice(0, -FAULTS.length) + ".csv";
  const d = (await env.DB.prepare(
    "SELECT start FROM drives WHERE source = 'logger' AND file = ?").bind(drive).all()).results;
  const driveStart = d && d.length ? String(d[0].start || "") : "";
  const reads = new Map();                     // "read_at module" -> rows
  for (const r of rows) {
    const get = (name) => (col(name) >= 0 && col(name) < r.length ? r[col(name)].trim() : "");
    const module = get("module");
    if (!module) continue;
    let at = get("read_at");
    if (!at && driveStart && get("time_s") !== "") at = addSeconds(driveStart, Number(get("time_s")));
    if (!at) continue;
    const k = `${at} ${module}`;
    if (!reads.has(k)) reads.set(k, []);
    reads.get(k).push({ module, at, name: get("module_name"), code: get("code").toUpperCase(),
      status: get("status"), state: get("state") });
  }
  const known = new Map(), latest = new Map();
  for (const x of (await env.DB.prepare(
    "SELECT module, code, data FROM faults WHERE source = 'logger'").all()).results || []) {
    known.set(`${x.module} ${x.code}`, JSON.parse(x.data));
  }
  for (const x of (await env.DB.prepare(
    "SELECT module, latest FROM logger_fault_reads").all()).results || []) {
    latest.set(x.module, x.latest);
  }
  const touched = new Set(), changed = new Set();
  let applied = 0;
  for (const k of [...reads.keys()].sort()) {
    const group = reads.get(k), { module, at } = group[0];
    if (group[0].state.startsWith("refused") || group[0].state.startsWith("no reply")) continue;
    if (at <= (latest.get(module) || "")) continue;
    latest.set(module, at);
    touched.add(module);
    applied++;
    for (const r of group) {
      if (!r.code) continue;
      const fk = `${module} ${r.code}`;
      const f = known.get(fk) || { module, module_name: r.name, code: r.code, text_en: "",
        first_seen: at, reads_seen: 0, reads_present: 0 };
      Object.assign(f, { last_seen: at, last_state: r.state, last_status: r.status });
      f.module_name = r.name || f.module_name;
      f.reads_seen += 1;
      if (r.state.startsWith("present")) f.reads_present += 1;
      known.set(fk, f);
      changed.add(fk);
    }
  }
  for (const [fk, f] of known) {
    if (!touched.has(f.module)) continue;
    const now = f.last_seen < latest.get(f.module) ? "gone"
      : String(f.last_state || "").startsWith("present") ? "present" : "stored";
    if (now !== f.now) { f.now = now; changed.add(fk); }
  }
  const texts = await faultTexts(env,
    [...changed].map((fk) => known.get(fk)).filter((f) => !f.text_en).map((f) => f.code));
  const stmts = [];
  for (const fk of changed) {
    const f = known.get(fk);
    if (!f.text_en && texts[f.code]) f.text_en = texts[f.code];
    stmts.push(env.DB.prepare(
      `INSERT OR REPLACE INTO faults (source, module, code, now, last_seen, data)
       VALUES ('logger', ?, ?, ?, ?, ?)`)
      .bind(f.module, f.code, f.now, f.last_seen, JSON.stringify(f)));
  }
  for (const module of touched) {
    stmts.push(env.DB.prepare(
      "INSERT OR REPLACE INTO logger_fault_reads (module, latest) VALUES (?, ?)")
      .bind(module, latest.get(module)));
  }
  if (stmts.length) await env.DB.batch(stmts);
  const mine = [...known.values()].filter((f) => touched.has(f.module));
  return { processed: true, fault_reads: applied, modules: touched.size,
    present: mine.filter((f) => f.now === "present").length,
    stored: mine.filter((f) => f.now === "stored").length };
}

/** The newest `limit` drives, oldest first; a log two installations both
 * have counts once (SQLite takes the other columns from the MAX(start) row). */
async function drives(env, limit) {
  await ensureSchema(env);
  const r = await env.DB.prepare(
    `SELECT data, MAX(start) AS latest FROM drives GROUP BY file
     ORDER BY latest DESC, file LIMIT ?`).bind(limit).all();
  return (r.results || []).map((x) => JSON.parse(x.data)).reverse();
}

/** The tanks of fuel (tanks.js) from the logger's drives. */
async function tankHistory(env) {
  await ensureSchema(env);
  const r = await env.DB.prepare("SELECT data FROM drives WHERE source = 'logger'").all();
  return tanks((r.results || []).map((x) => JSON.parse(x.data)));
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
  const all = await withDetails(env,
    [...by.values()].filter((f) => which === "all" || f.now !== "gone"));
  const k = (f) => `${f.now || ""} ${f.module} ${f.code}`;
  return all.sort((a, b) => (k(a) < k(b) ? -1 : k(a) > k(b) ? 1 : 0));
}

// ------------------------------------------------------------------ heartbeats

const MAX_BEAT = 4096;                      // bytes in one check-in
const KEEP_BEATS_DAYS = 30;
export const ONLINE_S = 30;                 // three missed beats: offline

/** Store one check-in from the board; older than KEEP_BEATS_DAYS go. */
async function putBeat(env, body, now = new Date()) {
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    throw new ToolError("body: a JSON object");
  }
  const boot = Number.isInteger(body.boot) ? body.boot : null;
  const event = body.event === "boot" ? "boot" : "beat";
  const received = now.toISOString();
  const cutoff = new Date(now.getTime() - KEEP_BEATS_DAYS * 86400e3).toISOString();
  await ensureSchema(env);
  await env.DB.batch([
    env.DB.prepare("INSERT INTO heartbeats (received, boot, event, data) VALUES (?, ?, ?, ?)")
      .bind(received, boot, event, JSON.stringify(body)),
    env.DB.prepare("DELETE FROM heartbeats WHERE received < ?").bind(cutoff),
  ]);
  return { received };
}

const MAX_BEATS_BODY = 65536;               // bytes in one batch of kept check-ins
const MAX_BEATS = 200;
const FUTURE_S = 300;                       // a board clock this far ahead is wrong

/** Check-ins the board kept while it had no Wi-Fi ({beats: [...]}), each
 * filed under its own clock: one without a clock, from the future or older
 * than KEEP_BEATS_DAYS is skipped, as is one already stored (a batch sent
 * again after a reboot). */
async function putBeats(env, body, now = new Date()) {
  if (!body || typeof body !== "object" || !Array.isArray(body.beats) ||
      body.beats.length > MAX_BEATS) {
    throw new ToolError(`body: JSON {beats: [...]}, at most ${MAX_BEATS}`);
  }
  const cutoff = now.getTime() / 1000 - KEEP_BEATS_DAYS * 86400;
  const stmts = [];
  let skipped = 0;
  for (const b of body.beats) {
    const at = b && typeof b === "object" && !Array.isArray(b) ? clockEpoch(b.clock) : null;
    if (at === null || at < cutoff || at > now.getTime() / 1000 + FUTURE_S) {
      skipped++;
      continue;
    }
    const received = new Date(Math.round(at * 1000)).toISOString();
    const boot = Number.isInteger(b.boot) ? b.boot : null;
    const event = b.event === "boot" ? "boot" : "beat";
    stmts.push(env.DB.prepare(
      `INSERT INTO heartbeats (received, boot, event, data) SELECT ?, ?, ?, ?
       WHERE NOT EXISTS (SELECT 1 FROM heartbeats WHERE received = ? AND boot IS ?)`)
      .bind(received, boot, event, JSON.stringify(b), received, boot));
  }
  await ensureSchema(env);
  if (stmts.length) await env.DB.batch(stmts);
  return { stored: stmts.length, skipped };
}

/** The newest check-in and how long ago, and each power-on in the last
 * `hours`: when it started and last checked in, why it booted, top speed,
 * hottest coolant and oil, and how many check-ins were kept offline. */
/** A check-in's fault_codes ("0x12:1A400A 0x40:804415") as [{module,
 * module_name, code, text_en}], the texts from n63read's table; null when
 * it has none. */
async function beatFaults(env, text, count) {
  if (typeof text !== "string" || !text.trim()) return null;
  const list = [];
  for (const one of text.trim().split(/\s+/).slice(0, 100)) {
    const m = /^(0x[0-9A-Fa-f]{2}):([0-9A-Fa-f]{6})$/.exec(one);
    if (m) list.push({ module: "0x" + m[1].slice(2).toUpperCase(), code: m[2].toUpperCase() });
  }
  // The board's fault_codes stop at about 400 characters, between modules in
  // address order (main.cpp, MAX_FAULT_CODES_TEXT): a check-in counting more
  // faults than it lists left out every module after its last. Those come
  // from each module's newest uploaded read (fault history), marked so.
  if (list.length && Number.isFinite(count) && count > list.length) {
    const lastModule = list.reduce((a, f) => (f.module > a ? f.module : a), "0x00");
    const listed = new Set(list.map((f) => `${f.module} ${f.code}`));
    const rest = (await env.DB.prepare(
      `SELECT module, code FROM faults WHERE source = 'logger' AND now != 'gone'
         AND upper(module) > upper(?) ORDER BY module, code`).bind(lastModule).all()).results || [];
    for (const r of rest) {
      const module = "0x" + String(r.module).slice(2).toUpperCase();
      if (list.length >= count || listed.has(`${module} ${r.code}`)) continue;
      list.push({ module, code: String(r.code).toUpperCase(), from_history: true });
    }
  }
  const texts = await faultTexts(env, [...new Set(list.map((f) => f.code))]);
  const names = Object.fromEntries((await env.DB.prepare(
    "SELECT module, json_extract(data, '$.module_name') AS name FROM faults WHERE source = 'logger'")
    .all()).results.map((r) => [r.module, r.name]));
  return withDetails(env, list.map((f) => ({ ...f, module_name: names[f.module] || "",
    text_en: texts[f.code] || "" })));
}

async function loggerStatus(env, hours, now = new Date()) {
  await ensureSchema(env);
  const last = (await env.DB.prepare(
    "SELECT received, data FROM heartbeats ORDER BY received DESC, rowid DESC LIMIT 1").all()).results;
  const since = new Date(now.getTime() - hours * 3600e3).toISOString();
  const sessions = (await env.DB.prepare(
    `SELECT boot, MIN(received) AS first, MAX(received) AS last, COUNT(*) AS beats,
       MAX(json_extract(data, '$.reset')) AS reset,
       MAX(json_extract(data, '$.speed_kmh')) AS max_speed_kmh,
       MAX(json_extract(data, '$.coolant_c')) AS max_coolant_c,
       MAX(json_extract(data, '$.oil_c')) AS max_oil_c,
       SUM(COALESCE(json_extract(data, '$.buffered'), 0)) AS buffered,
       (SELECT json_extract(w.data, '$.wifi_note') FROM heartbeats w
         WHERE w.boot = heartbeats.boot AND w.received >= ?
           AND json_extract(w.data, '$.wifi_note') IS NOT NULL
         ORDER BY w.received DESC LIMIT 1) AS wifi_note
     FROM heartbeats WHERE received >= ? GROUP BY boot ORDER BY first, boot`).bind(since, since).all())
    .results || [];
  const firmware = await firmwareManifest(env);
  const sweep = await getSweep(env);
  if (!last || !last.length) return { online: false, last: null, sessions, firmware, sweep };
  const age = Math.round((now.getTime() - Date.parse(last[0].received)) / 1000);
  const beat = { received: last[0].received, ...JSON.parse(last[0].data) };
  // The last drive's rates per tier, for the logger page's estimates.
  const d = (await env.DB.prepare(
    "SELECT file, start, data FROM drives WHERE source = 'logger' ORDER BY start DESC LIMIT 1")
    .all()).results;
  const lastDrive = d && d.length ? (({ rate_fast, rate_medium, slow_pass_s }) => ({ file: d[0].file,
    start: d[0].start, rate_fast, rate_medium, slow_pass_s }))(JSON.parse(d[0].data)) : null;
  // The board sends its fault codes only when they change (and every few
  // minutes): a check-in with faults but no codes has its power-on's newest.
  if (beat.faults > 0 && beat.fault_codes === undefined && beat.boot !== undefined) {
    const prev = (await env.DB.prepare(
      `SELECT json_extract(data, '$.fault_codes') AS codes FROM heartbeats
       WHERE boot = ? AND json_extract(data, '$.fault_codes') IS NOT NULL
       ORDER BY received DESC, rowid DESC LIMIT 1`).bind(beat.boot).all()).results;
    if (prev && prev.length) beat.fault_codes = prev[0].codes;
  }
  const faultList = await beatFaults(env, beat.fault_codes, Number(beat.faults));
  if (faultList) beat.fault_list = faultList;
  return { online: age <= ONLINE_S, seen_s_ago: age, last: beat, sessions, firmware, sweep,
    last_drive: lastDrive };
}

// ------------------------------------------------------------------ firmware
// The logger's firmware for over-the-air updates: one published build at a
// time (firmware/tools/publish_firmware.py sends it), the image in R2 and a
// manifest beside it. The board compares the manifest's version with its
// own, downloads the image to its card and checks the SHA-256 (firmware/
// README.md has the rest).

const FIRMWARE_PREFIX = "firmware/";
const FIRMWARE_BIN = "firmware/logger.bin";
const FIRMWARE_MANIFEST = "firmware/logger.json";
const MAX_FIRMWARE = 6 * 1024 * 1024;        // the board's app partition is 6.25 MB
const VERSION_RE = /^[\w.-]{1,40}$/;

/** {version, sha256, size, uploaded}, or null when nothing is published. */
async function firmwareManifest(env) {
  const obj = await env.FILES.get(FIRMWARE_MANIFEST);
  return obj ? JSON.parse(await obj.text()) : null;
}

/** Whether `bytes` holds `want` anywhere. */
function holds(bytes, want) {
  outer: for (let i = bytes.indexOf(want[0]); i >= 0 && i <= bytes.length - want.length;
    i = bytes.indexOf(want[0], i + 1)) {
    for (let k = 1; k < want.length; k++) if (bytes[i + k] !== want[k]) continue outer;
    return true;
  }
  return false;
}

/** Publish a build: the image (an ESP32 app image starts 0xE9) and its
 * version, which mustn't be "dev" (the board's unversioned builds) and must
 * be the one built into it (FW_VERSION): the board installs whatever version
 * isn't its own, so one built as another would be installed for good. */
async function putFirmware(env, version, data, now = new Date()) {
  if (!VERSION_RE.test(version || "") || version === "dev") {
    throw new ToolError("version: letters, digits, dot, dash or underscore, not \"dev\"");
  }
  const bytes = new Uint8Array(data);
  if (!bytes.length || bytes[0] !== 0xE9) throw new ToolError("not an ESP32 app image");
  if (bytes.length > MAX_FIRMWARE) throw new ToolError("too large for the app partition");
  if (!holds(bytes, new TextEncoder().encode(version))) {
    throw new ToolError(`the image doesn't say it's ${version}: built as another version, the ` +
      "board would take it again and again");
  }
  const manifest = { version, sha256: await sha256Hex(data), size: bytes.length,
    uploaded: now.toISOString() };
  await env.FILES.put(FIRMWARE_BIN, data, {
    httpMetadata: { contentType: "application/octet-stream" },
    customMetadata: { version, sha256: manifest.sha256 } });
  await env.FILES.put(FIRMWARE_MANIFEST, new TextEncoder().encode(JSON.stringify(manifest)),
    { httpMetadata: { contentType: "application/json" } });
  return manifest;
}

// ------------------------------------------------------------------ MCP tools

export const TOOLS = [
  {
    name: "drive_history",
    description: "The car's long-term statistics, one entry per drive (newest last): misfires, " +
      "uneven running and spark burn time per cylinder, knock retard and fuel trims per bank, " +
      "temperatures, battery, tire pressures, distance, fuel used and economy (fuel_l, " +
      "l_per_100km, mpg, tank_used_l). From the logs the BMW Analyzer app " +
      "uploaded (2018 BMW M550i, N63TU2 V8). The logger board's drives add the rate each " +
      "channel tier got (rate_fast, rate_medium: reads a second per identifier; slow_pass_s), " +
      "the tank (whole and each half) and odometer at each end.",
    inputSchema: { type: "object", properties: {
      limit: { type: "integer", description: "Drives, newest; default 30" } } },
  },
  {
    name: "fault_history",
    description: "Every fault read from the car's modules: first and last seen, how many reads " +
      "listed it, whether it is present, stored or gone now, the odometer when it first and " +
      "last set (first_km, last_km) with the dates the drives' odometers put them at " +
      "(first_date, last_date: {after, by}) and how often it occurred. From the app's fault " +
      "scans and the logger board's reads during drives (every module a drive, the engine " +
      "every few minutes; each code's odometer and count while parked). Pass code for the " +
      "full stored details of one fault (the app's scans only).",
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
  {
    name: "logger_status",
    description: "The logger board in the car: whether it is on now (it checks in every 10 s " +
      "while it has Wi-Fi, and once at power-on; with no Wi-Fi it keeps them and sends them " +
      "later), its last check-in (state, the drive being logged, speed, rpm, battery " +
      "voltage, coolant and oil temperature, GPS position, files waiting to upload, Wi-Fi, " +
      "memory, its firmware version and update state) and each power-on in the last " +
      "`hours` (first and last check-in, why it booted, top speed, hottest coolant and " +
      "oil, and wifi_note: its last word on why it couldn't join Wi-Fi), and the firmware " +
      "published for it. wifi_note names each known network as \"not seen\" (not in the " +
      "board's 2.4 GHz scan: out of range, not advertising, or 5 GHz only) or seen with its " +
      "signal and why the join failed (\"wrong password?\" for an authentication failure). " +
      "Times in UTC.",
    inputSchema: { type: "object", properties: {
      hours: { type: "number", description: "Power-ons this far back; default 24" } } },
  },
  {
    name: "tank_history",
    description: "Each tank of fuel, as the trip meter is reset at a fill-up: a fill-up is " +
      "the logger's tank level rising between drives. Per tank: first and last drive's " +
      "start (local time), drives, litres added, the level at its start and now, distance " +
      "(km, miles; from the odometer), fuel used (litres, US gallons; from the engine's fuel " +
      "flow, else the level's fall) and economy (mpg, l/100 km). The last is the tank in " +
      "the car now (open); the first may have started before the logs (partial).",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "drive_health",
    description: "Drive health: each drive's channels (fuel trims and lambda per bank, knock " +
      "retard, spark burn time and misfires per cylinder, coolant, oil temperature and " +
      "pressure, intake air, gearbox oil, after-catalyst temperatures, boost, battery) " +
      "with the engine fully warm (coolant ITKUM >= 95 °C, oil >= 90 °C), compared in " +
      "RPM x load cells against the same channel's last 10 healthy drives: a robust " +
      "z-score per channel (0 normal; |score| >= 2 watch, >= 3 alert), the drive's value " +
      "and the normal band (both sample-weighted over the cells scored). Without drive: " +
      "the drives' scores, newest first: the last `limit` (default 10, up to 50, as many " +
      "as fit), `total` saying how many there are (narrow with from, to, group, " +
      "flagged_only; to reaches older ones). With " +
      "drive and channel: that drive's RPM x load cells (value, baseline median, spread, " +
      "z, samples, and the longest time in each cell, for the raw log).",
    inputSchema: { type: "object", properties: {
      drive: { type: "string", description: "A drive, as the overview lists it" },
      channel: { type: "string", description: "A channel key, as the overview lists it" },
      group: { type: "string", description: "fueling, ignition, temps, boost, misfires or " +
        "electrical" },
      from: { type: "string", description: "ISO date: drives from" },
      to: { type: "string", description: "ISO date: drives up to" },
      flagged_only: { type: "boolean", description: "Only channels at watch or alert in " +
        "some drive shown" },
      limit: { type: "integer", description: "Drives, newest first: default 10, up to 50" } } },
  },
  {
    name: "drive_faults",
    description: "One logger drive's fault reads, from its .faults.csv: each module's read " +
      "(when, how far into the drive, the module, and its state: no faults, refused, no " +
      "reply) with every code it listed (status byte, state, English text). Pass a drive " +
      "file from list_files (its .csv or .faults.csv); without one, the newest drive's.",
    inputSchema: { type: "object", properties: {
      name: { type: "string", description: "logger/<drive>.csv or .faults.csv" } } },
  },
  {
    name: "channel_check",
    description: "The logger's full channel check (the logger page's sweep): the one " +
      "waiting or under way and the last one done (how far, counts, modules that didn't " +
      "answer), and what the car answers per module from every check so far (ok, refused, " +
      "absent: the module hasn't it, size: the wrong length, no answer, unchecked). Pass " +
      "module for its identifiers one by one.",
    inputSchema: { type: "object", properties: {
      module: { type: "string", description: "e.g. 0x12: list its identifiers' answers" },
      status: { type: "string", description: "with module: only these (ok, refused, absent...)" },
      limit: { type: "integer", description: "Identifiers listed; default 300" } } },
  },
  {
    name: "logger_channels",
    description: "The logger board's channel catalog with each channel's tier under a " +
      "preset (the active one by default): fast (every cycle), medium (a few times a second), " +
      "slow (in rotation), drive (once a drive), daily, weekly, monthly, off. Filter by " +
      "module (0x12), an identifier range (did_from, did_to), part of a name (match), exact " +
      "names, tier, or values: \"same\" (never changed, in two stored drives or more) or " +
      "\"rises\" (only ever goes up, in three or more: a counter); values never picks what " +
      "the board, the tanks, fault dates or Health read (odometers, misfire counters, tank " +
      "level, tires). Each: name, module, did, tier, " +
      "whether the preset names it (else its rest applies), what the car answered, unit, " +
      "description; with values, also lo, hi and drives.",
    inputSchema: { type: "object", properties: {
      preset: { type: "string" }, module: { type: "string" }, did_from: { type: "string" },
      did_to: { type: "string" }, match: { type: "string" },
      names: { type: "array", items: { type: "string" } }, tier: { type: "string" },
      values: { type: "string", enum: ["same", "rises"] },
      limit: { type: "integer", description: "Default 200, up to 2000" } } },
  },
  {
    name: "logger_set_tiers",
    description: "Set a tier for channels in a preset (the active one by default), one at a " +
      "time (names) or in bulk (module, did_from/did_to, match, values: \"same\" never " +
      "changed, \"rises\" only goes up, never what the board, tanks or Health read; all that " +
      "are given). tier: " +
      "fast, medium, slow, drive, daily, weekly, monthly, off, or default (back to the " +
      "preset's rest). The preset's previous state is kept as a version (logger_rollback). " +
      "If it's the active preset, the board takes the new channel file within 10 minutes " +
      "(at power-on, then every 10 minutes on Wi-Fi), between drives. dry_run: true says " +
      "what would change.",
    inputSchema: { type: "object", properties: {
      tier: { type: "string" }, preset: { type: "string" },
      names: { type: "array", items: { type: "string" } }, module: { type: "string" },
      did_from: { type: "string" }, did_to: { type: "string" }, match: { type: "string" },
      values: { type: "string", enum: ["same", "rises"] },
      dry_run: { type: "boolean" }, why: { type: "string", description: "Kept with the version" } },
      required: ["tier"] },
  },
  {
    name: "logger_presets",
    description: "The logger's presets (name, rest: the tier of channels it doesn't name, " +
      "how many channels it names per tier, last change), which one is active (published " +
      "to the board), and the channel file the board gets from it (counts per tier, ETag). " +
      "Pass name for that preset's saved versions (id, when, why, counts), newest first.",
    inputSchema: { type: "object", properties: {
      name: { type: "string", description: "List this preset's versions" } } },
  },
  {
    name: "logger_save_preset",
    description: "Save a preset: a copy of another (from, default the active one) under a new " +
      "name, or an existing one's rest changed (slow or off). Doesn't publish it unless " +
      "activate is true.",
    inputSchema: { type: "object", properties: {
      name: { type: "string" }, from: { type: "string" },
      rest: { type: "string", description: "slow or off" }, activate: { type: "boolean" } },
      required: ["name"] },
  },
  {
    name: "logger_publish",
    description: "Make a preset the active one: the board downloads its channel file within " +
      "10 minutes on Wi-Fi (or at its next power-on) and uses it from the next drive.",
    inputSchema: { type: "object", properties: { name: { type: "string" } },
      required: ["name"] },
  },
  {
    name: "logger_rollback",
    description: "Put a preset back as one of its versions was (logger_presets with its name " +
      "lists them); what it was before is kept as a version too.",
    inputSchema: { type: "object", properties: {
      name: { type: "string" }, version: { type: "integer" } }, required: ["name", "version"] },
  },
  {
    name: "board_commands",
    description: "The commands sent to the logger board from the Board tab or board_command, " +
      "newest first: kind, path, state (waiting for its next check-in, sent, done, failed, " +
      "cancelled), the board's result and, for a list, the folder's entries ([name, bytes, " +
      "is folder]) and the card's size and use. Pass id for one.",
    inputSchema: { type: "object", properties: {
      id: { type: "integer" }, limit: { type: "integer", description: "Default 10, up to 50" } } },
  },
  {
    name: "board_command",
    description: "Send the logger board a command; it gets it at its next check-in (every " +
      "10 s on Wi-Fi) and does the card's work between drives. kind: list (a folder on the " +
      "card, path default /), upload (its waiting files now), update (check for firmware " +
      "now and install it once it isn't logging), restart, delete (a file or folder, path), " +
      "format (the whole card; refused while files wait to upload unless discard). delete " +
      "and format erase for good: ask the user to confirm first, and pass confirm: true. " +
      "Read the outcome with board_commands.",
    inputSchema: { type: "object", properties: {
      kind: { type: "string", enum: Object.keys(KINDS) }, path: { type: "string" },
      discard: { type: "boolean" }, confirm: { type: "boolean" } }, required: ["kind"] },
  },
];

class ToolError extends Error {}

async function callTool(env, name, a, who) {
  a = a || {};
  // The tables first: the first request to a fresh isolate can be a tool
  // (seen: the first preset change after a deploy, before the versions table).
  await ensureSchema(env);
  if (name === "board_commands") {
    if (a.id !== undefined) {
      const c = await getCommand(env, Number(a.id));
      if (!c) throw new ToolError("no such command");
      return c;
    }
    return { commands: await listCommands(env, Math.min(50, Number(a.limit) || 10)) };
  }
  if (name === "board_command") {
    if (KINDS[a.kind] && ["delete", "format"].includes(a.kind) && a.confirm !== true) {
      throw new ToolError(`${a.kind} erases for good: confirm with the user, then pass confirm: true`);
    }
    try {
      return await putCommand(env, a, who);
    } catch (e) {
      if (e instanceof CommandError) throw new ToolError(e.message);
      throw e;
    }
  }
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
  if (name === "drive_health") {
    if (a.drive && a.channel) {
      const d = await healthDetail(env, String(a.drive), String(a.channel));
      if (!d) throw new ToolError("no health for that drive and channel");
      return d;
    }
    return healthTool(await healthOverview(env, { from: a.from, to: a.to }), a);
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
    const key = appKey(safeKey(String(a.name || "")));
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
    const key = appKey(safeKey(String(a.name || "")));
    if (!key) throw new ToolError("name: a file from list_files");
    const obj = await env.FILES.get(key);
    if (!obj) throw new ToolError(`no file ${key}; see list_files`);
    return logWindow(await obj.text(), {
      start: number(a.start), end: number(a.end),
      columns: Array.isArray(a.columns) ? a.columns.map(String) : null,
      resample: number(a.resample) || 0 });
  }
  if (name === "logger_status") {
    return loggerStatus(env, Math.max(0.1, Math.min(24 * KEEP_BEATS_DAYS, Number(a.hours) || 24)));
  }
  if (name === "tank_history") return { tanks: await tankHistory(env) };
  if (name === "drive_faults") return driveFaults(env, a.name);
  if (name === "channel_check") return channelCheck(env, a);
  if (name === "logger_channels") return listTiers(env, a);
  if (name === "logger_set_tiers") {
    return setTiers(env, { ...a, tier: String(a.tier || ""), why: a.why ? String(a.why) : null });
  }
  if (name === "logger_presets") {
    const all = await getPresets(env);
    if (a.name) {
      if (!all.presets.some((p) => p.name === a.name)) throw new ToolError(`no preset named ${a.name}`);
      return { name: a.name, versions: await presetVersions(env, String(a.name)) };
    }
    const file = await channelFile(env);
    return { active: all.active, file: file && { preset: file.preset, etag: file.etag,
      counts: file.counts }, presets: all.presets.map((p) => {
      const named = {};
      for (const t of Object.values(p.tiers)) named[t] = (named[t] || 0) + 1;
      return { name: p.name, rest: p.rest, named, updated: p.updated };
    }) };
  }
  if (name === "logger_save_preset") {
    const all = await getPresets(env);
    const target = String(a.name || "").trim();
    const have = all.presets.find((p) => p.name === target);
    const from = have || all.presets.find((p) => p.name === (a.from || all.active));
    if (!from) throw new ToolError(`no preset named ${a.from || all.active}`);
    const rest = a.rest || from.rest;
    const out = await putPresets(env, { changes: [{ name: target, rest, tiers: from.tiers }],
      ...(a.activate ? { active: target } : {}) }, new Date(),
    have ? `rest to ${rest}` : null);
    return { saved: target, from: have ? null : from.name, active: out.active };
  }
  if (name === "logger_publish") {
    const out = await putPresets(env, { active: String(a.name || "") });
    const file = await channelFile(env);
    return { active: out.active, file: file && { etag: file.etag, counts: file.counts } };
  }
  if (name === "logger_rollback") {
    const out = await rollbackPreset(env, String(a.name || ""), Number(a.version));
    return { name: a.name, rolled_back_to: Number(a.version), active: out.active };
  }
  throw new ToolError(`unknown tool ${name}`);
}

const INSTRUCTIONS = "The cloud copy of a 2018 BMW M550i's drive logs and fault reads, " +
  "uploaded by the BMW Analyzer (n63read) app. Start with drive_history or fault_history; " +
  "trend follows one statistic (misfires_5, knock_b1...) over time; list_files and read_log " +
  "look inside a drive, compare_banks and find_events analyse one; list_channels and " +
  "channel_series follow any channel across every " +
  "drive in UTC time; list_templates shows the app's saved channel sets; logger_status " +
  "says whether the logger board in the car is on and what it last reported; " +
  "tank_history gives each tank of fuel's distance and economy; drive_faults lists one " +
  "logger drive's fault reads and channel_check the full channel check's results. " +
  "The logger_* tools read and change what the logger board reads: logger_channels and " +
  "logger_presets look, logger_set_tiers, logger_save_preset, logger_publish and " +
  "logger_rollback change the presets in the cloud (every change kept as a version). " +
  "board_command sends the board a command (list, delete or format its card, upload now, " +
  "update its firmware now, restart) and board_commands reads how they went. " +
  "Everything else is read-only, and nothing writes to the car.";

/** One JSON-RPC message in, its reply out (null for a notification). */
export async function rpc(env, msg, who) {
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
        let text = JSON.stringify(await callTool(env, p.name, p.arguments, who));
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

const HEALTH_DRIVES = 10, HEALTH_DRIVES_MAX = 50;

/** The overview as drive_health gives it: per drive, each channel's score
 * and level with its value and band, narrowed as asked. The newest drives
 * first, `limit` of them (HEALTH_DRIVES unless asked), and never more than
 * the reply holds: a drive is about 3.4 KB, and a reply cut at MAX_TEXT was
 * broken JSON past 17 drives, the newest the ones lost. `total` says how
 * many there are; from and to reach older ones. */
export function healthTool(o, a) {
  const limit = Math.max(1, Math.min(HEALTH_DRIVES_MAX, Math.floor(Number(a.limit)) ||
    HEALTH_DRIVES));
  const idx = o.drives.map((_, i) => i).reverse().slice(0, limit);
  const keep = o.channels.filter((c) => !a.group || c.group === a.group).filter((c) =>
    !a.flagged_only || idx.some((i) => {
      const s = o.scores[c.key][i];
      return s && levelOf(c.group, s[0]) >= 1;
    }));
  const out = {
    settings: o.settings, groups: o.groups,
    channels: keep.map(({ key, label, group, unit }) => ({ key, label, group, unit })),
    total: o.drives.length,
    drives: idx.map((i) => ({ ...o.drives[i], channels: Object.fromEntries(keep
      .filter((c) => o.scores[c.key][i])
      .map((c) => {
        const [score, cells, value, low, high] = o.scores[c.key][i];
        return [c.key, { score, level: levelOf(c.group, score), cells, value, low, high }];
      })) })),
  };
  // The oldest shown go until it fits (with room for what's added below).
  let size = JSON.stringify(out).length + 200;
  while (out.drives.length > 1 && size > MAX_TEXT) {
    size -= JSON.stringify(out.drives.pop()).length + 1;
  }
  out.shown = out.drives.length;
  if (out.shown < out.total) {
    out.older = `${out.total - out.shown} older drives not shown: ask with to (a date) for ` +
      "them, or a group to fit more";
  }
  return out;
}

/** One logger drive's fault reads (its .faults.csv), each module's read
 * with the codes it listed and their texts; the newest drive's without a
 * name. */
async function driveFaults(env, name) {
  let key;
  if (name) {
    key = appKey(safeKey(String(name)));
    if (!key) throw new ToolError("name: a drive file from list_files");
    if (!key.endsWith(FAULTS)) key = key.replace(/(\.sweep|\.check)?\.csv$/, "") + FAULTS;
  } else {
    const r = (await env.DB.prepare(`SELECT key FROM uploads WHERE key LIKE 'logger/%' AND
      key LIKE '%' || ? ORDER BY uploaded DESC LIMIT 1`).bind(FAULTS).all()).results;
    if (!r || !r.length) throw new ToolError("no logger fault reads uploaded yet");
    key = r[0].key;
  }
  const obj = await env.FILES.get(key);
  if (!obj) throw new ToolError(`no file ${key}; see list_files`);
  const rows = parseCsv(await obj.text());
  const head = (rows.shift() || []).map((h) => h.trim());
  const col = (n) => head.indexOf(n);
  if (col("module") < 0 || col("state") < 0) throw new ToolError(`${key} isn't a fault list`);
  const get = (r, n) => (col(n) >= 0 && col(n) < r.length ? r[col(n)].trim() : "");
  const reads = [], at = new Map();
  for (const r of rows) {
    const module = get(r, "module");
    if (!module) continue;
    const k = `${get(r, "read_at")}|${get(r, "time_s")}|${module}`;
    if (!at.has(k)) {
      at.set(k, { read_at: get(r, "read_at") || null, time_s: number(get(r, "time_s")), module,
        module_name: get(r, "module_name"), state: get(r, "code") ? "listed codes" : get(r, "state"),
        codes: [] });
      reads.push(at.get(k));
    }
    if (get(r, "code")) {
      at.get(k).codes.push({ code: get(r, "code").toUpperCase(), status: get(r, "status"),
        state: get(r, "state") });
    }
  }
  const texts = await faultTexts(env, [...new Set(reads.flatMap((x) => x.codes.map((c) => c.code)))]);
  for (const x of reads) for (const c of x.codes) c.text_en = texts[c.code] || "";
  return { file: key, reads, failed: reads.reduce((n, x) => n + x.codes.filter((c) =>
    c.state.startsWith("present")).length, 0) };
}

const CHECK_STATUSES = ["ok", "refused", "absent", "size", "no answer"];

/** The full channel check: the request waiting or under way, the last one,
 * and the catalog's answers per module (or one module's identifiers). */
async function channelCheck(env, a) {
  const [sweep, catalog] = await Promise.all([getSweep(env), getCatalog(env)]);
  if (!catalog) return { sweep, modules: [] };
  const ids = identifiers(catalog);
  if (a.module) {
    const m = parseInt(String(a.module), 16);
    const limit = Math.max(1, Math.min(5000, Number(a.limit) || 300));
    const list = ids.filter((x) => x.module === m).map((x) => ({ did: x.key.split(":")[1],
      size: x.size, answer: catalog.answers[x.key] || "unchecked" }))
      .filter((x) => !a.status || x.answer === a.status);
    return { sweep, module: a.module, name: catalog.modules[`0x${m.toString(16).toUpperCase()
      .padStart(2, "0")}`] || "", total: list.length, identifiers: list.slice(0, limit) };
  }
  const by = new Map();
  for (const x of ids) {
    const m = x.key.split(":")[0];
    if (!by.has(m)) by.set(m, Object.fromEntries([...CHECK_STATUSES, "unchecked"].map((s) => [s, 0])));
    const s = catalog.answers[x.key];
    by.get(m)[CHECK_STATUSES.includes(s) ? s : "unchecked"]++;
  }
  return { sweep, modules: [...by].map(([module, counts]) => ({ module,
    name: catalog.modules[module] || "", ...counts })) };
}

// ------------------------------------------------------------------ logger config

/** The logger's channel catalog, presets and channel file (logger.js);
 * null for a path that isn't one of them. */
async function loggerConfig(request, env, url) {
  const path = url.pathname.slice("/api/logger/".length);
  const method = request.method;
  const body = async () => {
    try {
      return await request.json();
    } catch {
      throw new ConfigError("body: JSON");
    }
  };
  try {
    if (path === "catalog" && method === "PUT") {
      await ensureSchema(env);
      return json({ ok: true, ...(await putCatalog(env, await body())) });
    }
    if (path === "catalog" && method === "GET") {
      const catalog = await getCatalog(env);
      return catalog ? json(catalog) : json({ error: "no catalog yet: the app sends it " +
        "(Answers > Logger channels, or any sync)" }, 404);
    }
    if (path === "presets" && (method === "GET" || method === "PUT")) {
      await ensureSchema(env);
      const presets = method === "PUT" ? await putPresets(env, await body())
        : await getPresets(env);
      const file = await channelFile(env);
      return json({ ...presets, file: file && { preset: file.preset, etag: file.etag,
        counts: file.counts } });
    }
    if (path === "sweep") {
      await ensureSchema(env);
      if (method === "GET") return json(await getSweep(env));
      if (method === "POST") return json({ ok: true, ...(await requestSweep(env)) });
      if (method === "DELETE") return json({ ok: true, ...(await cancelSweep(env)) });
    }
    if (path === "values" && method === "GET") {
      await ensureSchema(env);
      return json(await getValues(env));
    }
    if (path === "check-list" && method === "GET") {
      const catalog = await getCatalog(env);
      if (!catalog) return json({ error: "no catalog yet" }, 404);
      return new Response(checkList(catalog), { headers: { "content-type": "text/csv",
        "cache-control": "no-store" } });
    }
    if (path === "channels" && method === "GET") {
      await ensureSchema(env);
      const file = await channelFile(env);
      if (!file) return json({ error: "no catalog or no active preset yet" }, 404);
      const etag = `"${file.etag}"`;
      const headers = { etag, "x-preset": encodeURIComponent(file.preset),
        "cache-control": "no-store" };
      const have = (request.headers.get("If-None-Match") || "").split(",").map((s) => s.trim());
      if (have.includes(etag) || have.includes(file.etag)) {
        return new Response(null, { status: 304, headers });
      }
      return new Response(file.csv, { headers: { ...headers, "content-type": "text/csv" } });
    }
  } catch (e) {
    if (e instanceof ConfigError) return json({ error: e.message }, 400);
    throw e;
  }
  return null;
}

// ------------------------------------------------------------------ routes

/** /api/health: the overview (GET), one drive and channel (GET /cell),
 * a drive kept out of the baselines (POST /exclude {drive, excluded}) and
 * every summary worked out again (POST /rebuild?after=), the last two for
 * people only, not the board. */
async function healthApi(request, env, url, who) {
  await ensureSchema(env);
  const q = Object.fromEntries(url.searchParams);
  if (url.pathname === "/api/health" && request.method === "GET") {
    return json(await healthOverview(env, q));
  }
  if (url.pathname === "/api/health/cell" && request.method === "GET") {
    const d = await healthDetail(env, String(q.drive || ""), String(q.channel || ""));
    return d ? json(d) : json({ error: "no health for that drive and channel" }, 404);
  }
  if (request.method !== "POST") return null;
  if (!who.includes("@")) return json({ error: "a person's login only" }, 403);
  if (url.pathname === "/api/health/rebuild") return json(await rebuildHealth(env, q.after));
  if (url.pathname === "/api/health/exclude") {
    let body;
    try {
      body = await request.json();
    } catch {
      return json({ error: "body: JSON {drive, excluded}" }, 400);
    }
    if (!body || typeof body.drive !== "string" || !body.drive) {
      return json({ error: "body: JSON {drive, excluded}" }, 400);
    }
    return json(await excludeDrive(env, body.drive, !!body.excluded, who));
  }
  return null;
}

// The board's token may only do the board's work: check in, upload its
// files and its card's listings, and fetch its channels, check list and firmware. So one taken from
// the board can't read the drives, change presets, write the app's files or
// publish code.
const CHECK_INS = ["/api/logger/heartbeat", "/api/logger/heartbeats"];
const BOARD_ROUTES = [
  ["POST", /^\/api\/logger\/heartbeats?$/],
  ["POST", /^\/api\/logger\/card$/],
  ["PUT", /^\/api\/files\/logger\/[^/]+$/],
  ["GET", /^\/api\/logger\/(channels|check-list|firmware|firmware\.bin)$/],
];
const boardRoute = (method, path) => BOARD_ROUTES.some(([m, p]) => m === method && p.test(path));

/** Service tokens that may publish firmware besides a person's login: the
 * app's, which publish_firmware.py uses (comma-separated client ids). */
const publishers = (env) => (env.FIRMWARE_PUBLISHERS || "").split(",").map((s) => s.trim())
  .filter(Boolean);

export async function handle(request, env, fetcher = fetch) {
  const url = new URL(request.url);
  const auth = await verifyAccess(request, env, fetcher);
  if (!auth.ok) {
    // An MCP client signs in again (or uses its refresh token) only on a 401
    // that says where (RFC 9728): a 403 left it stuck once the day's Access
    // token was refused here. Access's own metadata for this path, as Access
    // gives it when it turns a request away itself.
    if (url.pathname === "/mcp") {
      const meta = `${url.origin}/.well-known/cloudflare-access-protected-resource${url.pathname}`;
      return new Response(JSON.stringify({ error: "invalid_token", reason: auth.reason }), {
        status: 401, headers: { "content-type": "application/json",
          "www-authenticate": `Bearer error="invalid_token", resource_metadata="${meta}"` } });
    }
    return json({ error: "forbidden", reason: auth.reason }, 403);
  }

  // The board's token, or the new one it's trying, only on the board's
  // routes; one it had before nowhere. Only the board checks in.
  const checkIn = request.method === "POST" && CHECK_INS.includes(url.pathname);
  let role = null;
  if (!auth.who.includes("@")) {
    await ensureSchema(env);
    role = await boardRole(env, auth.who, checkIn);
  }
  if (role && (role === "old" || !boardRoute(request.method, url.pathname))) {
    return json({ error: "not for the board's token" }, 403);
  }
  if (checkIn && !role) return json({ error: "only the board checks in" }, 403);

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
      const replies = (await Promise.all(body.map((m) => rpc(env, m, auth.who)))).filter(Boolean);
      return replies.length ? json(replies) : new Response(null, { status: 202 });
    }
    const one = await rpc(env, body, auth.who);
    return one ? json(one) : new Response(null, { status: 202 });
  }

  if (url.pathname.startsWith("/api/series/") && request.method === "PUT") {
    const drive = appKey(safeKey(url.pathname.slice("/api/series/".length)));
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

  if (url.pathname === "/api/logger/heartbeat" && request.method === "POST") {
    const text = await request.text();
    if (text.length > MAX_BEAT) return json({ error: "too large" }, 413);
    let body;
    try {
      body = JSON.parse(text);
    } catch {
      return json({ error: "body: JSON {boot, event, ...}" }, 400);
    }
    try {
      const stored = await putBeat(env, body);
      return json({ ok: true, ...stored, ...(await beatSweep(env, body)),
        ...(await beatSettings(env, body, role, auth.who)), ...(await beatCommand(env, body, role)) });
    } catch (e) {
      if (e instanceof ToolError) return json({ error: e.message }, 400);
      throw e;
    }
  }
  if (url.pathname === "/api/logger/settings") {
    await ensureSchema(env);
    try {
      if (request.method === "GET") return json(await getBoardSettings(env));
      if (request.method === "PUT") {
        let body;
        try {
          body = await request.json();
        } catch {
          return json({ error: "body: JSON {key: value}" }, 400);
        }
        return json(await putBoardSettings(env, body, auth.who));
      }
      if (request.method === "DELETE") return json(await cancelBoardSettings(env, auth.who));
    } catch (e) {
      if (e instanceof SettingsError) return json({ error: e.message }, 400);
      throw e;
    }
    return new Response(null, { status: 405 });
  }
  if (url.pathname === "/api/logger/commands") {
    await ensureSchema(env);
    try {
      if (request.method === "GET") {
        return json({ kinds: KINDS, commands: await listCommands(env, url.searchParams.get("limit")) });
      }
      if (request.method === "POST") {
        let body;
        try {
          body = await request.json();
        } catch {
          return json({ error: "body: JSON {kind, path}" }, 400);
        }
        return json(await putCommand(env, body, auth.who));
      }
      if (request.method === "DELETE") {
        return json(await cancelCommand(env, url.searchParams.get("id"), auth.who));
      }
    } catch (e) {
      if (e instanceof CommandError) return json({ error: e.message }, 400);
      throw e;
    }
    return new Response(null, { status: 405 });
  }
  if (url.pathname === "/api/logger/card" && request.method === "POST") {
    if (role !== "board") return json({ error: "only the board sends its card's listing" }, 403);
    const text = await request.text();
    if (text.length > MAX_CARD_BODY) return json({ error: "too large" }, 413);
    await ensureSchema(env);
    try {
      return json({ ok: true, ...(await putListing(env, JSON.parse(text))) });
    } catch (e) {
      if (e instanceof CommandError || e instanceof SyntaxError) {
        return json({ error: e.message }, 400);
      }
      throw e;
    }
  }
  if (url.pathname === "/api/logger/heartbeats" && request.method === "POST") {
    const text = await request.text();
    if (text.length > MAX_BEATS_BODY) return json({ error: "too large" }, 413);
    let body;
    try {
      body = JSON.parse(text);
    } catch {
      return json({ error: "body: JSON {beats: [...]}" }, 400);
    }
    try {
      return json({ ok: true, ...(await putBeats(env, body)) });
    } catch (e) {
      if (e instanceof ToolError) return json({ error: e.message }, 400);
      throw e;
    }
  }
  if (url.pathname === "/api/logger/firmware") {
    if (request.method === "GET") {
      const m = await firmwareManifest(env);
      return m ? json(m) : json({ error: "no firmware published" }, 404);
    }
    if (request.method === "PUT") {
      if (!auth.who.includes("@") && !publishers(env).includes(auth.who)) {
        return json({ error: "firmware is published by a person's login or a token in " +
          "FIRMWARE_PUBLISHERS" }, 403);
      }
      const len = Number(request.headers.get("content-length") || 0);
      if (len > MAX_FIRMWARE) return json({ error: "too large" }, 413);
      try {
        return json({ ok: true, ...(await putFirmware(env, url.searchParams.get("version"),
          await request.arrayBuffer())) });
      } catch (e) {
        if (e instanceof ToolError) return json({ error: e.message }, 400);
        throw e;
      }
    }
    return new Response(null, { status: 405 });
  }
  if (url.pathname === "/api/logger/firmware.bin" && request.method === "GET") {
    const obj = await env.FILES.get(FIRMWARE_BIN);
    if (!obj) return json({ error: "no firmware published" }, 404);
    const data = await obj.arrayBuffer();
    return new Response(data, { headers: { "content-type": "application/octet-stream",
      "content-length": String(data.byteLength), "cache-control": "no-store" } });
  }
  if (url.pathname === "/api/logger/tanks" && request.method === "GET") {
    return json({ tanks: await tankHistory(env) });
  }
  if (url.pathname === "/api/logger/resummarize" && request.method === "POST") {
    return json(await resummarize(env, url.searchParams.get("after")));
  }
  if (url.pathname === "/api/logger/values/backfill" && request.method === "POST") {
    await ensureSchema(env);
    return json(await backfillPieces(env, url.searchParams.get("after")));
  }
  if (url.pathname === "/logger/board" && request.method === "GET") {
    return new Response(BOARD_PAGE, { headers: { "content-type": "text/html; charset=utf-8",
      "cache-control": "no-store" } });
  }
  if (url.pathname === "/logger/health" && request.method === "GET") {
    return new Response(HEALTH_PAGE, { headers: { "content-type": "text/html; charset=utf-8",
      "cache-control": "no-store" } });
  }
  if (url.pathname.startsWith("/api/health")) {
    const reply = await healthApi(request, env, url, auth.who);
    if (reply) return reply;
  }
  if (url.pathname === "/logger" && request.method === "GET") {
    return new Response(LOGGER_PAGE, { headers: { "content-type": "text/html; charset=utf-8",
      "cache-control": "no-store" } });
  }
  if (url.pathname.startsWith("/api/logger/")) {
    const reply = await loggerConfig(request, env, url);
    if (reply) return reply;
  }
  if (url.pathname === "/api/logger" && request.method === "GET") {
    const hours = Math.max(0.1, Math.min(24 * KEEP_BEATS_DAYS,
      Number(url.searchParams.get("hours")) || 24));
    return json(await loggerStatus(env, hours));
  }

  if (url.pathname === "/api/files" && request.method === "GET") {
    await ensureSchema(env);
    const r = await env.DB.prepare("SELECT key, size, sha256, uploaded FROM uploads").all();
    return json({ files: r.results || [] });
  }

  if (url.pathname.startsWith("/api/files/")) {
    const key = appKey(safeKey(url.pathname.slice("/api/files/".length)));
    if (!key) return json({ error: "bad file name" }, 400);
    if (request.method === "PUT") {
      // The firmware goes only through PUT /api/logger/firmware (whose token
      // is checked), never straight into its R2 keys.
      if (key.startsWith(FIRMWARE_PREFIX)) {
        return json({ error: "firmware is published with PUT /api/logger/firmware" }, 403);
      }
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
        if (key.endsWith(FAULTS)) {
          return json({ ...stored, ...(await ingestFaults(env, key,
            new TextDecoder().decode(data))) });
        }
        if (key.endsWith(DETAILS)) {
          return json({ ...stored, ...(await ingestDetails(env,
            parseCsv(new TextDecoder().decode(data)))) });
        }
        if (key.endsWith(CHECK)) {
          return json({ ...stored, ...(await ingestCheck(env, key,
            new TextDecoder().decode(data))) });
        }
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
