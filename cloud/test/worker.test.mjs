// Tests for the cloud Worker, on Node's own test runner and SQLite (Node 24):
//   node --test "cloud/test/*.test.mjs"
// D1 and R2 are small stand-ins; Access tokens are signed here with a key
// whose public half the fake team domain serves.

import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";

import worker, { handle, logWindow, parseCsv, resetJwksCache, rpc, safeKey, TOOLS }
  from "../src/worker.js";

const TEAM = "https://example-team.cloudflareaccess.com";
const AUD = "test-audience";

// --- stand-ins for D1 and R2

class D1 {
  constructor() { this.db = new DatabaseSync(":memory:"); }
  prepare(sql) {
    const db = this.db;
    const stmt = { args: [],
      bind(...a) { this.args = a; return this; },
      async all() { return { results: db.prepare(sql).all(...this.args) }; },
      async run() { db.prepare(sql).run(...this.args); return { success: true }; } };
    return stmt;
  }
  async batch(stmts) { for (const s of stmts) await s.run(); return []; }
}

class R2 {
  constructor() { this.objects = new Map(); }
  async put(key, data, opts) {
    this.objects.set(key, { key, data: new Uint8Array(data), size: data.byteLength,
      uploaded: new Date(Date.now() + this.objects.size).toISOString(), opts });
  }
  async get(key) {
    const o = this.objects.get(key);
    if (!o) return null;
    const text = new TextDecoder().decode(o.data);
    return { text: async () => text, body: text };
  }
  async list({ prefix } = {}) {
    return { objects: [...this.objects.values()].filter((o) => !prefix || o.key.startsWith(prefix)) };
  }
}

// --- Access tokens

let keys, jwk;

async function newKey() {
  keys = await crypto.subtle.generateKey({ name: "RSASSA-PKCS1-v1_5", modulusLength: 2048,
    publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" }, true, ["sign", "verify"]);
  jwk = { ...(await crypto.subtle.exportKey("jwk", keys.publicKey)), kid: "k1" };
}

const b64url = (bytes) => Buffer.from(bytes).toString("base64url");

async function token(claims = {}, header = {}) {
  const h = b64url(JSON.stringify({ alg: "RS256", kid: "k1", ...header }));
  const p = b64url(JSON.stringify({ aud: [AUD], iss: TEAM, email: "owner@example.com",
    exp: Math.floor(Date.now() / 1000) + 600, ...claims }));
  const sig = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", keys.privateKey,
    new TextEncoder().encode(`${h}.${p}`));
  return `${h}.${p}.${b64url(new Uint8Array(sig))}`;
}

const fetcher = async (url) => {
  assert.equal(url, `${TEAM}/cdn-cgi/access/certs`);
  return new Response(JSON.stringify({ keys: [jwk] }));
};

let env;

beforeEach(async () => {
  resetJwksCache();
  if (!keys) await newKey();
  env = { DB: new D1(), FILES: new R2(), ACCESS_AUD: AUD, ACCESS_TEAM_DOMAIN: TEAM };
});

async function call(method, path, { body, auth = true, headers = {} } = {}) {
  const h = new Headers(headers);
  if (auth) h.set("Cf-Access-Jwt-Assertion", typeof auth === "string" ? auth : await token());
  return handle(new Request(`https://car.example.com${path}`, { method, body, headers: h }),
    env, fetcher);
}

// --- Access

test("no token, no entry", async () => {
  const r = await call("GET", "/api/status", { auth: false });
  assert.equal(r.status, 403);
});

test("a valid token gets in and says who", async () => {
  const r = await call("GET", "/api/status");
  assert.equal(r.status, 200);
  assert.equal((await r.json()).who, "owner@example.com");
});

test("wrong audience, expired, forged or unconfigured: refused", async () => {
  for (const bad of [await token({ aud: ["other"] }),
    await token({ exp: 1 }), await token({ iss: "https://evil.example.com" }),
    (await token()).slice(0, -4) + "AAAA"]) {
    assert.equal((await call("GET", "/api/status", { auth: bad })).status, 403);
  }
  env.ACCESS_AUD = "";
  assert.equal((await call("GET", "/api/status")).status, 403);       // fails closed
});

test("the Access cookie works too", async () => {
  const r = await call("GET", "/api/status",
    { auth: false, headers: { Cookie: `x=1; CF_Authorization=${await token()}` } });
  assert.equal(r.status, 200);
});

// --- uploads and history

test("a log uploads, lists and comes back", async () => {
  const csv = "Time,Clock,INMOT,Event\n0.1,2026-10-19 07:00:00,800,\n0.3,x,810,\n";
  const r = await call("PUT", "/api/files/claude/n63_1.csv", { body: csv });
  const put = await r.json();
  assert.equal(put.size, csv.length);
  assert.match(put.sha256, /^[0-9a-f]{64}$/);
  const listed = await (await call("GET", "/api/files")).json();
  assert.deepEqual(listed.files.map((f) => f.key), ["claude/n63_1.csv"]);
  assert.equal(await (await call("GET", "/api/files/claude/n63_1.csv")).text(), csv);
});

test("unsafe file names are refused", async () => {
  for (const name of ["../x.csv", "a/../b.csv", "a//b.csv", "%2e%2e/x.csv", "x%00.csv"]) {
    assert.equal(safeKey(name), null, name);
  }
  assert.equal(safeKey("sub\\n63 (2).csv"), "sub/n63 (2).csv");
  assert.equal((await call("PUT", "/api/files/..%2Fx.csv", { body: "x" })).status, 400);
});

const DRIVES = [
  { file: "a.csv", start: "2026-10-18 18:43:57", misfires_5: 5, knock_b1: -0.9, km: 3.7 },
  { file: "b.csv", start: "2026-10-19 07:42:50", misfires_5: 2, knock_b1: -0.3, km: 5.3 },
];
const FAULTS = [
  { module: "0x40", code: "804415", now: "present", last_seen: "2026-10-19", text_en: "fob",
    last_km: 111321, occurrences: 1,
    details: { snapshots: [{ record: 1, fields: [{ id: 5888, value: 107031 }] }] } },
  { module: "0x29", code: "4806DA", now: "gone", last_seen: "2026-10-18", text_en: "tire" },
];

test("history replaces the summaries", async () => {
  await call("PUT", "/api/history", { body: JSON.stringify({ drives: DRIVES, faults: FAULTS }) });
  const r = await call("PUT", "/api/history",
    { body: JSON.stringify({ drives: DRIVES, faults: FAULTS }) });
  assert.deepEqual(await r.json(), { ok: true, drives: 2, faults: 2 });
  assert.equal((await call("PUT", "/api/history", { body: "not json" })).status, 400);
});

// --- templates

test("templates sync both ways, deletions included", async () => {
  const put = (changes) => call("PUT", "/api/templates", { body: JSON.stringify({ changes }) });
  let r = await (await put([{ name: "Misfire hunt", channels: ["INMOT", "ILUZ5"] },
    { name: "Old", channels: ["ITOEL"] }])).json();
  assert.deepEqual(r.templates.map((t) => [t.name, t.deleted]),
    [["Misfire hunt", false], ["Old", false]]);
  r = await (await put([{ name: "Old", channels: null }])).json();
  const old = r.templates.find((t) => t.name === "Old");
  assert.equal(old.deleted, true);
  assert.equal(old.channels, null);
  const got = await (await call("GET", "/api/templates")).json();
  assert.deepEqual(got.templates.find((t) => t.name === "Misfire hunt").channels,
    ["INMOT", "ILUZ5"]);
  assert.equal((await put([{ name: "", channels: [] }])).status, 400);
  assert.equal((await put([{ name: "x", channels: [1, 2] }])).status, 400);
  assert.equal((await call("PUT", "/api/templates", { body: "nope" })).status, 400);
});

// --- time series

function chunk(name, t0, values, unit = "") {
  const data = values.map((v, i) => [i, v]);
  return { name, unit, t0, t1: t0 + values.length - 1, n: values.length,
    min: Math.min(...values), max: Math.max(...values),
    mean: values.reduce((s, v) => s + v, 0) / values.length, data };
}

test("series: stored per drive, one channel across drives, resampled", async () => {
  const day1 = Date.parse("2026-10-18T23:43:57Z") / 1000;
  const day2 = Date.parse("2026-10-19T12:35:34Z") / 1000;
  const put = (drive, chunks) => call("PUT", `/api/series/${drive}`,
    { body: JSON.stringify({ start: chunks[0].t0, approx: false, chunks }) });
  let r = await put("logs/a.csv", [chunk("ITKUA", day1, [80, 90, 100], "°C"),
    chunk("ILUZ5", day1, [1, -2, 3])]);
  assert.deepEqual(await r.json(), { ok: true, drive: "logs/a.csv", channels: 2, chunks: 2 });
  await put("logs/b.csv", [chunk("ITKUA", day2, [85, 95], "°C")]);
  await put("logs/a.csv", [chunk("ITKUA", day1, [81, 91, 101], "°C"),    // replaced, not added
    chunk("ILUZ5", day1, [1, -2, 3])]);

  const ch = (await tool("list_channels", {})).data.channels;
  assert.deepEqual(ch.map((c) => [c.channel, c.drives, c.values]),
    [["ILUZ5", 1, 3], ["ITKUA", 2, 5]]);
  assert.equal(ch[1].first, "2026-10-18T23:43:57.000Z");

  const s = (await tool("channel_series", { channel: "ITKUA" })).data;
  assert.equal(s.count, 5);
  assert.deepEqual(s.points.map((p) => p[1]), [81, 91, 101, 85, 95]);
  assert.equal(s.points[3][0], "2026-10-19T12:35:34.000Z");
  assert.deepEqual(s.drives, ["logs/a.csv", "logs/b.csv"]);

  const win = (await tool("channel_series",
    { channel: "ITKUA", start: "2026-10-19T00:00:00Z" })).data;
  assert.deepEqual(win.points.map((p) => p[1]), [85, 95]);

  const by = (await tool("channel_series", { channel: "ITKUA", by_drive: true })).data.drives;
  assert.deepEqual(by.map((d) => [d.drive, d.min, d.max]),
    [["logs/a.csv", 81, 101], ["logs/b.csv", 85, 95]]);

  await put("logs/c.csv", [chunk("INMOT", day2, Array.from({ length: 300 }, (_, i) => i))]);
  const many = (await tool("channel_series", { channel: "INMOT", points: 10 })).data;
  assert.equal(many.resampled, true);
  assert.equal(many.points.length, 10);
  assert.deepEqual(many.columns, ["time", "mean", "min", "max", "n"]);
  assert.equal(many.points[0][2], 0);
  assert.equal(many.points[9][3], 299);

  const http = await (await call("GET", "/api/series?channel=ITKUA&by_drive=1")).json();
  assert.equal(http.drives.length, 2);
  assert.equal((await call("PUT", "/api/series/x.csv", { body: "{}" })).status, 400);
});

// --- MCP

async function mcp(method, params, id = 1) {
  const r = await call("POST", "/mcp",
    { body: JSON.stringify({ jsonrpc: "2.0", id, method, params }) });
  return r.status === 202 ? null : r.json();
}

async function tool(name, args) {
  const r = await mcp("tools/call", { name, arguments: args });
  return { error: r.result.isError, data: r.result.isError ? r.result.content[0].text
    : JSON.parse(r.result.content[0].text) };
}

test("MCP handshake and tool list", async () => {
  const init = await mcp("initialize", { protocolVersion: "2025-06-18" });
  assert.equal(init.result.protocolVersion, "2025-06-18");
  assert.ok(init.result.capabilities.tools);
  const r = await call("POST", "/mcp",
    { body: JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }) });
  assert.equal(r.status, 202);
  const list = await mcp("tools/list");
  assert.deepEqual(list.result.tools.map((t) => t.name), TOOLS.map((t) => t.name));
  assert.equal((await mcp("nope")).error.code, -32601);
});

test("MCP tools read the history and the logs", async () => {
  await call("PUT", "/api/history", { body: JSON.stringify({ drives: DRIVES, faults: FAULTS }) });
  const h = await tool("drive_history", { limit: 10 });
  assert.deepEqual(h.data.drives.map((d) => d.file), ["a.csv", "b.csv"]);   // newest last
  const brief = (await tool("fault_history", {})).data.faults;
  assert.deepEqual(brief.map((f) => f.code), ["804415"]);
  assert.equal(brief[0].has_details, true);
  assert.equal(brief[0].details, undefined);                 // long: only on request
  assert.equal(brief[0].last_km, 111321);
  const one = (await tool("fault_history", { code: "804415" })).data.faults;
  assert.equal(one[0].details.snapshots[0].fields[0].value, 107031);
  assert.equal((await tool("fault_history", { which: "all" })).data.faults.length, 2);
  assert.deepEqual((await tool("trend", { key: "misfires_5" })).data.points,
    [["2026-10-18 18:43:57", 5], ["2026-10-19 07:42:50", 2]]);
  assert.equal((await tool("trend", { key: "x; DROP TABLE" })).error, true);

  const lines = ["Time,Clock,INMOT,ILAG1,Event"];
  for (let t = 0; t < 20; t++) lines.push(`${t},x,${800 + t},1.0,`);
  await call("PUT", "/api/files/n63_2.csv", { body: lines.join("\n") });
  assert.deepEqual((await tool("list_files", {})).data.files.map((f) => f.name), ["n63_2.csv"]);
  const w = await tool("read_log", { name: "n63_2.csv", start: 5, end: 12, columns: ["INMOT"],
    resample: 2 });
  assert.deepEqual(w.data.columns, ["Time", "INMOT"]);
  assert.deepEqual(w.data.rows.map((r) => r[0]), ["5", "7", "9", "11"]);
  assert.equal((await tool("read_log", { name: "missing.csv" })).error, true);

  await call("PUT", "/api/templates", { body: JSON.stringify({ changes: [
    { name: "Boost", channels: ["IPLAD"] }, { name: "Gone", channels: null }] }) });
  assert.deepEqual((await tool("list_templates", {})).data.templates.map((t) => t.name),
    ["Boost"]);
});

test("MCP batches and parse errors", async () => {
  const r = await call("POST", "/mcp", { body: JSON.stringify([
    { jsonrpc: "2.0", id: 1, method: "ping" },
    { jsonrpc: "2.0", method: "notifications/initialized" }]) });
  assert.deepEqual((await r.json()).map((x) => x.id), [1]);
  assert.equal((await call("POST", "/mcp", { body: "{" })).status, 400);
  assert.equal((await rpc(env, { jsonrpc: "1.0" })).error.code, -32600);
});

// --- CSV

test("CSV: quotes, semicolons, a BOM, CRLF", () => {
  assert.deepEqual(parseCsv('﻿a,b\r\n1,"x,""y"""\r\n'), [["a", "b"], ["1", 'x,"y"']]);
  assert.deepEqual(parseCsv("a;b\n1,5;2\n"), [["a", "b"], ["1,5", "2"]]);
  const w = logWindow("Time;Wert\n0,5;1\n1,5;2\n");
  assert.deepEqual(w.rows, [["0,5", "1"], ["1,5", "2"]]);
});

test("the default export answers, and hides internal errors", async () => {
  const r = await worker.fetch(new Request("https://car.example.com/api/status"),
    { ACCESS_AUD: "", ACCESS_TEAM_DOMAIN: "" });
  assert.equal(r.status, 403);
});
