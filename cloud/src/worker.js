/**
 * n63read cloud: the car's logs and history, stored and served by a
 * Cloudflare Worker.
 *
 * - The app uploads every drive log and fault list (PUT /api/files/<path>)
 *   into R2, and its drive and fault summaries (PUT /api/history) into D1.
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

export const SERVER = { name: "n63read-cloud", version: "1.0.0" };
export const PROTOCOLS = ["2025-06-18", "2025-03-26", "2024-11-05"];
const MAX_UPLOAD = 50 * 1024 * 1024;
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

// ------------------------------------------------------------------ D1

export const SCHEMA = [
  `CREATE TABLE IF NOT EXISTS drives (file TEXT PRIMARY KEY, start TEXT, data TEXT NOT NULL)`,
  `CREATE INDEX IF NOT EXISTS drives_start ON drives (start)`,
  `CREATE TABLE IF NOT EXISTS faults (module TEXT NOT NULL, code TEXT NOT NULL,
     now TEXT, last_seen TEXT, data TEXT NOT NULL, PRIMARY KEY (module, code))`,
  `CREATE TABLE IF NOT EXISTS uploads (key TEXT PRIMARY KEY, size INTEGER, sha256 TEXT,
     uploaded TEXT)`,
  `CREATE TABLE IF NOT EXISTS templates (name TEXT PRIMARY KEY, channels TEXT,
     updated TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0)`,
];

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

async function ensureSchema(env) {
  if (schemaReady.has(env.DB)) return;
  await env.DB.batch(SCHEMA.map((s) => env.DB.prepare(s)));
  schemaReady.add(env.DB);
}

async function putHistory(env, body) {
  const drives = Array.isArray(body.drives) ? body.drives : [];
  const faults = Array.isArray(body.faults) ? body.faults : [];
  await ensureSchema(env);
  const stmts = [env.DB.prepare("DELETE FROM drives"), env.DB.prepare("DELETE FROM faults")];
  for (const d of drives) {
    if (!d || typeof d.file !== "string") continue;
    stmts.push(env.DB.prepare("INSERT OR REPLACE INTO drives (file, start, data) VALUES (?, ?, ?)")
      .bind(d.file, String(d.start || ""), JSON.stringify(d)));
  }
  for (const f of faults) {
    if (!f || typeof f.code !== "string" || typeof f.module !== "string") continue;
    stmts.push(env.DB.prepare(
      "INSERT OR REPLACE INTO faults (module, code, now, last_seen, data) VALUES (?, ?, ?, ?, ?)")
      .bind(f.module, f.code, String(f.now || ""), String(f.last_seen || ""), JSON.stringify(f)));
  }
  await env.DB.batch(stmts);
  return { drives: drives.length, faults: faults.length };
}

async function drives(env, limit) {
  await ensureSchema(env);
  const r = await env.DB.prepare("SELECT data FROM drives ORDER BY start DESC LIMIT ?")
    .bind(limit).all();
  return (r.results || []).map((x) => JSON.parse(x.data)).reverse();
}

async function faults(env, which) {
  await ensureSchema(env);
  const sql = which === "all"
    ? "SELECT data FROM faults ORDER BY now, module, code"
    : "SELECT data FROM faults WHERE now != 'gone' ORDER BY now, module, code";
  const r = await env.DB.prepare(sql).all();
  return (r.results || []).map((x) => JSON.parse(x.data));
}

// ------------------------------------------------------------------ MCP tools

export const TOOLS = [
  {
    name: "drive_history",
    description: "The car's long-term statistics, one entry per drive (newest last): misfires, " +
      "uneven running and spark burn time per cylinder, knock retard and fuel trims per bank, " +
      "temperatures, battery, tire pressures, distance. From the logs the BMW Analyzer app " +
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
      "trim_b2, tire_2, battery_min, coolant_max, km. Returns [start, value] pairs.",
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
  "look inside a drive; list_templates shows the app's saved channel sets. Read-only.";

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
    return json({ ok: true, ...(await putHistory(env, body)) });
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
      return json({ ok: true, key, size: data.byteLength, sha256: sha });
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
