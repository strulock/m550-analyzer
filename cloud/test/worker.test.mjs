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
    return { text: async () => text, body: text, arrayBuffer: async () => o.data.slice().buffer };
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

test("Access rotated its keys: fetched again, not refused for an hour", async () => {
  assert.equal((await call("GET", "/api/status")).status, 200);       // k1 cached
  const old = jwk;
  const was = keys;
  try {
    await newKey();
    jwk = { ...jwk, kid: "k2" };
    const fresh = await token({}, { kid: "k2" });
    assert.equal((await call("GET", "/api/status", { auth: fresh })).status, 200);
    // A made-up key id doesn't fetch the keys every request.
    const forged = await token({}, { kid: "k9" });
    assert.equal((await call("GET", "/api/status", { auth: forged })).status, 403);
  } finally {
    keys = was;
    jwk = old;
  }
});

test("a token a few seconds past its expiry still gets in; minutes past, not", async () => {
  const now = Math.floor(Date.now() / 1000);
  assert.equal((await call("GET", "/api/status", { auth: await token({ exp: now - 20 }) })).status, 200);
  assert.equal((await call("GET", "/api/status", { auth: await token({ exp: now - 300 }) })).status, 403);
});

test("MCP: a refused token is a 401 that says where to sign in again", async () => {
  const r = await call("POST", "/mcp", { auth: await token({ exp: 1 }),
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list" }) });
  assert.equal(r.status, 401);
  assert.equal(r.headers.get("www-authenticate"), 'Bearer error="invalid_token", ' +
    'resource_metadata="https://car.example.com/.well-known/cloudflare-access-protected-resource/mcp"');
  assert.equal((await call("POST", "/mcp", { auth: false, body: "{}" })).status, 401);
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
  assert.deepEqual(await r.json(), { ok: true, source: "legacy", drives: 2, faults: 2 });
  assert.equal((await call("PUT", "/api/history", { body: "not json" })).status, 400);
  const huge = Array.from({ length: 20001 }, (_, i) => ({ file: `x${i}.csv`, start: "" }));
  assert.equal((await call("PUT", "/api/history",
    { body: JSON.stringify({ drives: huge, faults: [] }) })).status, 400);
  for (const bad of ["../x", "logger", "a".repeat(65)]) {
    assert.equal((await call("PUT", "/api/history",
      { body: JSON.stringify({ source: bad, drives: [], faults: [] }) })).status, 400, bad);
  }
});

// --- several app installations

async function history(source, drives, faults) {
  const body = { drives, faults };
  if (source) body.source = source;
  return (await call("PUT", "/api/history", { body: JSON.stringify(body) })).json();
}

test("one installation's history leaves another's alone", async () => {
  await history("desk-pc", DRIVES, FAULTS);
  // The laptop has only its own drive, and an empty fault history.
  await history("laptop", [{ file: "c.csv", start: "2026-10-20 08:00:00", km: 1.5 }], []);
  const files = async () => (await toolData("drive_history")).drives.map((d) => d.file);
  assert.deepEqual(await files(), ["a.csv", "b.csv", "c.csv"]);
  assert.equal((await toolData("fault_history", { which: "all" })).faults.length, 2);
  // An app from before sources ("legacy") with nothing: only legacy rows go.
  await history(null, [], []);
  assert.deepEqual(await files(), ["a.csv", "b.csv", "c.csv"]);
  // The desk PC deleting a log takes its drive away, not the laptop's.
  await history("desk-pc", DRIVES.slice(1), FAULTS);
  assert.deepEqual(await files(), ["b.csv", "c.csv"]);
});

test("a log two installations have counts once; faults merge", async () => {
  await history("desk-pc", DRIVES, [
    { module: "0x40", code: "804415", now: "gone", first_seen: "2026-10-01", first_km: 110000,
      last_seen: "2026-10-10", text_en: "fob" }]);
  await history("laptop", [DRIVES[1]], [
    { module: "0x40", code: "804415", now: "present", first_seen: "2026-10-05", first_km: 110500,
      last_seen: "2026-10-19", text_en: "fob" }]);
  assert.deepEqual((await toolData("drive_history")).drives.map((d) => d.file), ["a.csv", "b.csv"]);
  const [f] = (await toolData("fault_history", { which: "all" })).faults;
  assert.equal(f.now, "present");                       // the laptop saw it last
  assert.equal(f.last_seen, "2026-10-19");
  assert.equal(f.first_seen, "2026-10-01");             // the desk PC saw it first
  assert.equal(f.first_km, 110000);
  assert.equal((await toolData("fault_history")).faults.length, 1);   // current: present
});

test("an installation whose logs sit in a logs folder names them as the others do", async () => {
  await history("desk-pc", DRIVES, []);
  await history("laptop", DRIVES.map((d) => ({ ...d, file: "logs/" + d.file })), []);
  assert.deepEqual((await toolData("drive_history")).drives.map((d) => d.file), ["a.csv", "b.csv"]);
  const put = await (await call("PUT", "/api/files/logs/a.csv", { body: "Time,INMOT\n0,700\n" })).json();
  assert.equal(put.key, "a.csv");
  assert.equal(await (await call("GET", "/api/files/a.csv")).text(), "Time,INMOT\n0,700\n");
  const series = { start: 0, approx: false,
    chunks: [{ name: "INMOT", t0: 0, t1: 1, data: [[0, 700], [1, 710]] }] };
  await call("PUT", "/api/series/a.csv", { body: JSON.stringify(series) });
  await call("PUT", "/api/series/logs/a.csv", { body: JSON.stringify(series) });
  assert.deepEqual(env.DB.db.prepare("SELECT DISTINCT drive FROM series").all().map((x) => x.drive),
    ["a.csv"]);
});

test("tables from before sources are converted, rows kept", async () => {
  const db = env.DB.db;
  db.exec(`CREATE TABLE drives (file TEXT PRIMARY KEY, start TEXT, data TEXT NOT NULL);
    CREATE TABLE faults (module TEXT NOT NULL, code TEXT NOT NULL, now TEXT, last_seen TEXT,
      data TEXT NOT NULL, PRIMARY KEY (module, code));`);
  db.prepare("INSERT INTO drives VALUES (?, ?, ?)").run("a.csv", "2026-10-18", JSON.stringify(DRIVES[0]));
  db.prepare("INSERT INTO drives VALUES (?, ?, ?)").run("logger/x.csv", "2026-10-25",
    JSON.stringify({ file: "logger/x.csv", start: "2026-10-25" }));
  db.prepare("INSERT INTO faults VALUES (?, ?, ?, ?, ?)").run("0x40", "804415", "present",
    "2026-10-19", JSON.stringify(FAULTS[0]));
  assert.deepEqual((await toolData("drive_history")).drives.map((d) => d.file),
    ["a.csv", "logger/x.csv"]);
  const sources = db.prepare("SELECT file, source FROM drives ORDER BY file").all()
    .map((x) => [x.file, x.source]);
  assert.deepEqual(sources, [["a.csv", "legacy"], ["logger/x.csv", "logger"]]);
  assert.equal(db.prepare("SELECT source FROM faults").get().source, "legacy");
  // A named installation's sync now leaves those rows alone.
  await history("desk-pc", [DRIVES[1]], []);
  assert.deepEqual((await toolData("drive_history")).drives.map((d) => d.file),
    ["a.csv", "b.csv", "logger/x.csv"]);
});

// --- the logger board's logs

const LOGGER_CSV = "Time,Clock,INMOT,INMOT@0x13,IUIBS,Event\n" +
  [0, 1, 2, 3, 4, 5].map((t) =>
    `${t},2026-10-25 07:00:0${t}.000-05:00,${700 + t},${705 + t},${14100 - t},`).join("\n") + "\n";

async function toolData(name, args = {}) {
  const r = await rpc(env, { jsonrpc: "2.0", id: 1, method: "tools/call",
    params: { name, arguments: args } });
  return JSON.parse(r.result.content[0].text);
}

test("a logger log gets its series and drive row on upload", async () => {
  // Units come from what the app stored for the same channels.
  await call("PUT", "/api/series/app.csv", { body: JSON.stringify({ start: 0, approx: false,
    chunks: [{ name: "INMOT", unit: "rpm", t0: 0, t1: 1, n: 1, min: 1, max: 1, mean: 1,
      data: [[0, 1]] }] }) });
  const put = await (await call("PUT", "/api/files/logger/2026-10-25_07-00-00.csv",
    { body: LOGGER_CSV })).json();
  assert.equal(put.processed, true);
  assert.equal(put.channels, 3);
  assert.equal(put.approx_time, false);
  assert.deepEqual(put.drive, { start: "2026-10-25 07:00:00", minutes: 0.08, km: 0 });
  assert.match(put.sha256, /^[0-9a-f]{64}$/);

  const [d] = (await toolData("drive_history")).drives;
  assert.equal(d.file, "logger/2026-10-25_07-00-00.csv");
  assert.equal(d.rpm_max, 705);
  assert.equal(d.battery_min, 14.095);                  // mV to V, as the app does

  const channels = Object.fromEntries((await toolData("list_channels")).channels
    .map((c) => [c.channel, c.unit]));
  assert.deepEqual(channels, { INMOT: "rpm", "INMOT@0x13": "rpm", IUIBS: "" });
  const s = await toolData("channel_series", { channel: "INMOT@0x13" });
  assert.deepEqual(s.points[0], ["2026-10-25T12:00:00.000Z", 705]);
});

test("channels that only go up across drives, and the table from before that", async () => {
  // A channel_values without rises (from before it): dropped and built again.
  env.DB.db.exec(`CREATE TABLE channel_values (channel TEXT PRIMARY KEY, t REAL, value REAL,
     n INTEGER, lo REAL, hi REAL, drives INTEGER)`);
  env.DB.db.prepare("INSERT INTO channel_values VALUES ('OLD', 0, 1, 1, 1, 1, 1)").run();
  const drive = (name, t0, chunks) => call("PUT", `/api/series/${name}`, { body: JSON.stringify({
    start: 0, approx: false, chunks: chunks.map(([ch, data]) => ({ name: ch, t0, t1: t0 + 1,
      min: Math.min(...data.map((d) => d[1])), max: Math.max(...data.map((d) => d[1])),
      mean: 0, data })) }) });
  await drive("a.csv", 1000, [["KM", [[0, 100], [1, 101]]], ["DROPS", [[0, 5], [1, 6]]],
    ["SAME", [[0, 7], [1, 7]]], ["WOBBLE", [[0, 1], [1, 3]]], ["ONCE", [[0, 1], [1, 2]]]]);
  await drive("b.csv", 2000, [["KM", [[0, 101], [1, 101], [2, 105]]], ["DROPS", [[0, 4]]],
    ["SAME", [[0, 7]]], ["WOBBLE", [[0, 3], [1, 2], [2, 4]]]]);
  await drive("c.csv", 3000, [["KM", [[0, 105]]]]);
  const v = (await (await call("GET", "/api/logger/values")).json()).values;
  const rises = Object.fromEntries(Object.entries(v).map(([k, x]) => [k, x[6]]));
  // ONCE: one drive only; WOBBLE: down within a drive; SAME: never changed.
  assert.deepEqual(rises, { KM: 1, DROPS: 0, SAME: 0, WOBBLE: 0, ONCE: 0 });
  assert.deepEqual(v.SAME.slice(2, 5), [3, 7, 7]);
});

test("each channel's newest value and range, kept up to date as drives come in", async () => {
  const values = async () => (await (await call("GET", "/api/logger/values")).json()).values;
  // Series stored before the table existed: built whole on the first ask.
  env.DB.db.exec(`CREATE TABLE series (drive TEXT NOT NULL, channel TEXT NOT NULL, t0 REAL NOT NULL,
    t1 REAL NOT NULL, n INTEGER NOT NULL, unit TEXT, min REAL, max REAL, mean REAL,
    approx INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL, PRIMARY KEY (drive, channel, t0))`);
  env.DB.db.prepare("INSERT INTO series VALUES ('old.csv', 'OELTEMP', 100, 110, 2, '', 80, 95, 87.5, 0, ?)")
    .run("[[0,80],[10,95]]");
  assert.deepEqual((await values()).OELTEMP, [110, 95, 2, 80, 95, 1, 0]);

  await call("PUT", "/api/files/logger/2026-10-25_07-00-00.csv", { body: LOGGER_CSV });
  let v = await values();
  assert.deepEqual(v.INMOT.slice(1), [705, 6, 700, 705, 1, 0]);                    // the last row's
  assert.deepEqual(v.IUIBS.slice(1), [14095, 6, 14095, 14100, 1, 0]);
  assert.equal(v.INMOT[0], Date.parse("2026-10-25T12:00:05Z") / 1000);
  assert.ok(v.OELTEMP);                                                          // untouched

  // The app's series for a drive replace what it sent before, and the newest wins.
  const app = (chunks) => call("PUT", "/api/series/app.csv",
    { body: JSON.stringify({ start: 0, approx: false, chunks }) });
  await app([{ name: "INMOT", unit: "rpm", t0: 2e9, t1: 2e9 + 1, min: 650, max: 660,
    mean: 655, data: [[0, 660], [1, 650]] }, { name: "GONE", t0: 0, t1: 1, data: [[0, 1]] }]);
  v = await values();
  assert.deepEqual(v.INMOT.slice(0, 2), [2e9 + 1, 650]);
  assert.deepEqual(v.INMOT.slice(2), [8, 650, 705, 2, 0]);
  assert.ok(v.GONE);
  await app([{ name: "INMOT", unit: "rpm", t0: 2e9, t1: 2e9 + 1, data: [[0, 640]] }]);
  v = await values();
  assert.equal(v.GONE, undefined);                                               // no series left
  assert.equal(v.INMOT[1], 640);
});

test("the app's history sync leaves logger drives in place", async () => {
  await call("PUT", "/api/files/logger/a.csv", { body: LOGGER_CSV });
  await call("PUT", "/api/history", { body: JSON.stringify({
    drives: [...DRIVES, { file: "logger/a.csv", start: "x", km: 999 }], faults: [] }) });
  const files = (await toolData("drive_history")).drives.map((d) => [d.file, d.km]);
  assert.deepEqual(files, [["a.csv", 3.7], ["b.csv", 5.3], ["logger/a.csv", 0]]);
});

test("a drive's fast and sweep files make one drive, in either order", async () => {
  const tire = "DSC_RDC_MESSDATENBLOCK_1.LETZTER_REIFENDRUCKWERT";
  const odo = "BDC_ZUSATZINFO_LICHT.KM_STAND_1";
  const sweepCsv = `Time,Clock,${tire},${odo}\n` + [0, 2, 4, 6, 8].map((t) =>
    `${t},2026-10-25 07:00:0${t}.000-05:00,2.4,41234`).join("\n") + "\n";
  const s = await (await call("PUT", "/api/files/logger/d.sweep.csv", { body: sweepCsv })).json();
  assert.equal(s.drive_file, "logger/d.csv");
  assert.equal(s.with_other_file, false);
  const f = await (await call("PUT", "/api/files/logger/d.csv", { body: LOGGER_CSV })).json();
  assert.equal(f.with_other_file, true);

  const [d] = (await toolData("drive_history")).drives;
  assert.equal(d.file, "logger/d.csv");
  assert.equal(d.rows, 6);                              // the fast file's
  assert.equal(d.rpm_max, 705);                         // fast
  assert.equal(d.tire_1, 2.4);                          // sweep
  assert.equal(d.odometer_km, 41234);                   // sweep

  const channels = async () => (await toolData("list_channels")).channels
    .map((c) => [c.channel, c.drives]).sort();
  const both = [["BDC_ZUSATZINFO_LICHT.KM_STAND_1", 1], ["DSC_RDC_MESSDATENBLOCK_1." +
    "LETZTER_REIFENDRUCKWERT", 1], ["INMOT", 1], ["INMOT@0x13", 1], ["IUIBS", 1]];
  assert.deepEqual(await channels(), both);
  // Sending one file again leaves the other's channels alone.
  await call("PUT", "/api/files/logger/d.csv", { body: LOGGER_CSV });
  assert.deepEqual(await channels(), both);
});

test("a sweep file with thousands of channels stays within D1's query count", async () => {
  const names = Array.from({ length: 3000 }, (_, i) => `BDC_CH_${i}`);
  const csvText = `Time,Clock,${names.join(",")}\n` + [0, 60, 120, 180, 240].map((t) =>
    `${t},2026-10-25 07:00:00.000-05:00,${names.map((_, k) => (k + t) % 97).join(",")}`)
    .join("\n") + "\n";
  let statements = 0;
  const prepare = env.DB.prepare.bind(env.DB);
  env.DB.prepare = (sql) => { statements++; return prepare(sql); };
  const put = await (await call("PUT", "/api/files/logger/w.sweep.csv", { body: csvText })).json();
  assert.equal(put.processed, true);
  assert.equal(put.channels, 3000);
  assert.ok(statements < 40, `${statements} statements`);         // D1 allows 1,000
  env.DB.prepare = prepare;
  const listed = (await (await call("GET", "/api/series?match=BDC_CH_")).json()).channels;
  assert.equal(listed.length, 3000);
  const one = await toolData("channel_series", { channel: "BDC_CH_7" });
  assert.deepEqual(one.points.map((p) => p[1]), [7, 67, 30, 90, 53]);
});

test("logger uploads that aren't drive logs are stored and say why", async () => {
  const faults = await (await call("PUT", "/api/files/logger/faults.csv",
    { body: "read_at,module,code\n2026-10-25,0x12,1A400A\n" })).json();
  assert.equal(faults.processed, false);
  assert.equal(faults.reason, "not a drive log");
  const empty = await (await call("PUT", "/api/files/logger/empty.csv",
    { body: "Time,INMOT\n" })).json();
  assert.equal(empty.ok, true);
  assert.equal(empty.processed, false);
  assert.match(empty.reason, /no samples/);
  // Elsewhere nothing changes: the app's own uploads aren't processed.
  const app = await (await call("PUT", "/api/files/n63.csv", { body: LOGGER_CSV })).json();
  assert.equal(app.processed, undefined);
  assert.deepEqual((await toolData("drive_history")).drives, []);
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
  assert.deepEqual(await r.json(), { ok: true, drive: "a.csv", channels: 2, chunks: 2 });
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
  assert.deepEqual(s.drives, ["a.csv", "b.csv"]);

  const win = (await tool("channel_series",
    { channel: "ITKUA", start: "2026-10-19T00:00:00Z" })).data;
  assert.deepEqual(win.points.map((p) => p[1]), [85, 95]);

  const by = (await tool("channel_series", { channel: "ITKUA", by_drive: true })).data.drives;
  assert.deepEqual(by.map((d) => [d.drive, d.min, d.max]),
    [["a.csv", 81, 101], ["b.csv", 85, 95]]);

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

// --- analysing one stored log

test("compare_banks and find_events over a stored log", async () => {
  const lines = ["Time,Clock,ILAG1,ILAG1@0x13,ITOEL,ZAEHLER_AUSSETZERK_ZYL5,Event"];
  for (let t = 0; t < 10; t++) {
    lines.push([t, "x", 1.0, t === 6 ? 0.9 : 1.02, 90 + t * 3, t >= 4 ? 1 : 0,
      t === 0 ? "odometer 111321 km" : ""].join(","));
  }
  await call("PUT", "/api/files/logs/drive.csv", { body: lines.join("\n") });
  const banks = (await tool("compare_banks", { name: "logs/drive.csv" })).data;
  assert.equal(banks.pairs.length, 1);
  assert.equal(banks.pairs[0].channel, "ILAG1");
  assert.equal(banks.pairs[0].largest_at_s, 6);
  assert.ok(Math.abs(banks.pairs[0].largest_difference - 0.1) < 1e-9);

  const ev = (await tool("find_events", { name: "logs/drive.csv", column: "ITOEL",
    above: 105 })).data;
  assert.deepEqual(ev.events.map((e) => [e.at_s, e.what]), [
    [0, "odometer 111321 km"],
    [4, "ZAEHLER_AUSSETZERK_ZYL5 went up 0 -> 1"],
    [6, "ITOEL above 105 (108)"]]);
  assert.equal((await tool("compare_banks", { name: "nope.csv" })).error, true);
  assert.equal((await tool("find_events", { name: "logs/drive.csv", column: "X",
    above: 1 })).error, true);
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

// --- logger fault reads

const FAULT_HEAD = "read_at,module,module_name,code,status,state,text_en,text_de," +
  "catalog_source,first_km,last_km,occurrences,details,time_s\n";
const faultRow = (at, module, code, status, state, t = 0) =>
  `${at},${module},"Engine, master DME",${code},${status},"${state}",,,,,,,,${t}\n`;

test("logger fault reads go into fault history, gone once a later read drops them", async () => {
  await env.FILES.put("ref/BMW_fault_code_texts.csv", new TextEncoder().encode(
    "﻿code,text_de,text_en,source_module\n480A14,Klopf,\"Knock, cylinder 1\",DME\n"));
  const first = FAULT_HEAD +
    faultRow("2026-10-25 12:43:10", "0x12", "480A14", "0x2F", "present, stored") +
    faultRow("2026-10-25 12:43:12", "0x29", "", "", "no faults") +
    faultRow("2026-10-25 12:46:10", "0x12", "480A14", "0x2C", "not present, stored");
  let r = await (await call("PUT", "/api/files/logger/2026-10-25_12-42-05.faults.csv",
    { body: first })).json();
  assert.equal(r.processed, true);
  assert.equal(r.fault_reads, 3);
  let f = (await tool("fault_history", {})).data.faults;
  assert.equal(f.length, 1);
  assert.deepEqual([f[0].module, f[0].code, f[0].now, f[0].text_en, f[0].reads_seen,
    f[0].reads_present, f[0].first_seen, f[0].last_seen],
  ["0x12", "480A14", "stored", "Knock, cylinder 1", 2, 1, "2026-10-25 12:43:10",
    "2026-10-25 12:46:10"]);
  // The same file again changes nothing.
  r = await (await call("PUT", "/api/files/logger/2026-10-25_12-42-05.faults.csv",
    { body: first })).json();
  assert.equal(r.fault_reads, 0);
  assert.equal((await tool("fault_history", {})).data.faults[0].reads_seen, 2);
  // The next drive's read doesn't list it: gone (kept under which: all).
  await call("PUT", "/api/files/logger/2026-10-26_08-00-00.faults.csv", { body: FAULT_HEAD +
    faultRow("2026-10-26 08:01:00", "0x12", "", "", "no faults (40 listed codes never failed)") });
  assert.equal((await tool("fault_history", {})).data.faults.length, 0);
  f = (await tool("fault_history", { which: "all" })).data.faults;
  assert.equal(f[0].now, "gone");
});

test("logger fault reads: a refused or silent module isn't a read; no clock uses the drive", async () => {
  await call("PUT", "/api/files/logger/x.faults.csv", { body: FAULT_HEAD +
    faultRow("2026-10-25 12:43:10", "0x12", "480A14", "0x2F", "present, stored") });
  await call("PUT", "/api/files/logger/y.faults.csv", { body: FAULT_HEAD +
    faultRow("2026-10-25 13:00:00", "0x12", "", "", "no reply") +
    faultRow("2026-10-25 13:00:02", "0x12", "", "", "refused (nrc 0x31)") });
  assert.equal((await tool("fault_history", {})).data.faults[0].now, "present");
  // A drive without a clock: its fault reads are dated from the drive's start.
  env.DB.db.prepare("INSERT INTO drives (source, file, start, data) VALUES (?, ?, ?, ?)")
    .run("logger", "logger/nt-00035-01.csv", "2026-10-25 14:00:00", "{}");
  const r = await (await call("PUT", "/api/files/logger/nt-00035-01.faults.csv", { body: FAULT_HEAD +
    faultRow("", "0x12", "", "", "no faults", 75.5) })).json();
  assert.equal(r.fault_reads, 1);
  const f = (await tool("fault_history", { which: "all" })).data.faults[0];
  assert.equal(f.now, "gone");
  const reads = env.DB.db.prepare("SELECT latest FROM logger_fault_reads WHERE module = '0x12'")
    .get();
  assert.equal(reads.latest, "2026-10-25 14:01:15");
});

test("logger faults and the app's: each fault once, the newest sighting wins", async () => {
  await call("PUT", "/api/history", { body: JSON.stringify({ source: "pc", drives: [],
    faults: [{ module: "0x12", code: "480A14", now: "present", first_seen: "2026-10-01 09:00:00",
      last_seen: "2026-10-20 09:00:00", text_en: "Knock" }] }) });
  await call("PUT", "/api/files/logger/z.faults.csv", { body: FAULT_HEAD +
    faultRow("2026-10-25 12:43:10", "0x12", "", "", "no faults") });
  // The logger never listed it, so it has no row of its own: the app's stands.
  assert.equal((await tool("fault_history", {})).data.faults[0].now, "present");
  await call("PUT", "/api/files/logger/w.faults.csv", { body: FAULT_HEAD +
    faultRow("2026-10-25 12:50:00", "0x12", "480A14", "0x2C", "not present, stored") });
  const f = (await tool("fault_history", {})).data.faults[0];
  assert.equal(f.now, "stored");
  assert.equal(f.first_seen, "2026-10-01 09:00:00");      // the earliest sighting anywhere
});

// --- logger check-ins

test("logger check-ins: stored, the newest says online, power-ons grouped", async () => {
  const off = await (await call("GET", "/api/logger")).json();
  assert.equal(off.online, false);
  assert.equal(off.last, null);
  const beat = (b) => call("POST", "/api/logger/heartbeat", { body: JSON.stringify(b) });
  assert.equal((await beat({ boot: 7, event: "boot", reset: "power on", state: "searching" }))
    .status, 200);
  await beat({ boot: 7, event: "beat", reset: "power on", state: "logging", speed_kmh: 42.5 });
  await beat({ boot: 8, event: "boot", reset: "brownout", speed_kmh: null });
  const s = await (await call("GET", "/api/logger")).json();
  assert.equal(s.online, true);
  assert.equal(s.last.boot, 8);
  assert.equal(s.last.reset, "brownout");
  assert.deepEqual(s.sessions.map((x) => [x.boot, x.beats, x.reset, x.max_speed_kmh]),
    [[7, 2, "power on", 42.5], [8, 1, "brownout", null]]);
  const t = await tool("logger_status", { hours: 1 });
  assert.equal(t.data.last.boot, 8);
  assert.equal(t.data.sessions.length, 2);
});

test("logger check-ins: the failed codes come back with their texts", async () => {
  await env.FILES.put("ref/BMW_fault_code_texts.csv", new TextEncoder().encode(
    "﻿code,text_de,text_en,source_module\n1A400A,Lüfter,\"Electric fan, self-diagnosis\",DME\n"));
  await call("POST", "/api/logger/heartbeat", { body: JSON.stringify({ boot: 9, event: "beat",
    faults: 2, fault_codes: "0x12:1A400A 0x40:804415 junk" }) });
  const s = await (await call("GET", "/api/logger")).json();
  assert.deepEqual(s.last.fault_list.map((f) => [f.module, f.code, f.text_en]),
    [["0x12", "1A400A", "Electric fan, self-diagnosis"], ["0x40", "804415", ""]]);
  // The board sends the codes only when they change: the power-on's newest.
  await new Promise((r) => setTimeout(r, 5));
  await call("POST", "/api/logger/heartbeat", { body: JSON.stringify({ boot: 9, faults: 2 }) });
  const k = (await (await call("GET", "/api/logger")).json()).last;
  assert.equal(k.fault_codes, "0x12:1A400A 0x40:804415 junk");
  assert.equal(k.fault_list.length, 2);
  await new Promise((r) => setTimeout(r, 5));
  await call("POST", "/api/logger/heartbeat", { body: JSON.stringify({ boot: 10, faults: 1 }) });
  assert.equal((await (await call("GET", "/api/logger")).json()).last.fault_list, undefined);
  await new Promise((r) => setTimeout(r, 5));
  await call("POST", "/api/logger/heartbeat", { body: JSON.stringify({ boot: 9, faults: 0 }) });
  assert.equal((await (await call("GET", "/api/logger")).json()).last.fault_list, undefined);
});

test("board settings: a person's change goes to the board in a check-in's reply, then is dropped",
  async () => {
    const beat = async (extra) => (await call("POST", "/api/logger/heartbeat",
      { body: JSON.stringify({ boot: 20, event: "beat", ...extra }) })).json();
    await beat({ settings_version: 2, networks: "Home", token_id: "abcd1234" });
    // The board's own token (no email) can't ask for one.
    const board = await token({ email: undefined, common_name: "abcd1234.access" });
    let r = await call("PUT", "/api/logger/settings",
      { body: JSON.stringify({ wifi2_ssid: "Phone" }), auth: board });
    assert.equal(r.status, 400);
    assert.match((await r.json()).error, /only a person/);
    assert.equal((await call("PUT", "/api/logger/settings",
      { body: JSON.stringify({ wifi_pass: "short" }) })).status, 400);
    assert.equal((await call("PUT", "/api/logger/settings",
      { body: JSON.stringify({ access_id: "x.access" }) })).status, 400);   // and its secret
    assert.equal((await call("PUT", "/api/logger/settings",
      { body: JSON.stringify({ host: "elsewhere.example.com" }) })).status, 400);
    r = await call("PUT", "/api/logger/settings",
      { body: JSON.stringify({ wifi2_ssid: "Phone", wifi2_pass: "p\"ss word" }) });
    let s = await r.json();
    assert.equal(s.version, 3);                              // after the board's 2
    assert.deepEqual(s.pending.keys, ["wifi2_ssid", "wifi2_pass"]);
    assert.equal(s.pending.by, "owner@example.com");
    assert.equal(JSON.stringify(s).includes("p\"ss"), false);  // never shown back
    assert.equal(s.board.networks, "Home");
    // The next check-in's reply carries it, in hex.
    const reply = await beat({ settings_version: 2 });
    assert.equal(reply.settings_version, 3);
    assert.equal(Buffer.from(reply.set_wifi2_ssid, "hex").toString(), "Phone");
    assert.equal(Buffer.from(reply.set_wifi2_pass, "hex").toString(), "p\"ss word");
    // The board has it: dropped, with how it went.
    assert.equal((await beat({ settings_version: 3 })).settings_version, undefined);
    s = await (await call("GET", "/api/logger/settings")).json();
    assert.equal(s.pending, null);
    assert.equal(s.last.result, "taken");
    assert.equal(env.DB.db.prepare("SELECT pending FROM board_settings").get().pending, null);
    // A token the Worker refuses on the board's try: refused, with why.
    await call("PUT", "/api/logger/settings", { body: JSON.stringify({
      access_id: "fedcba9876543210.access", access_secret: "0123456789abcdef0123" }) });
    assert.equal((await beat({ settings_version: 3 })).settings_version, 4);
    await beat({ settings_version: 3, settings_failed: 4, settings_error: "refused (403)" });
    s = await (await call("GET", "/api/logger/settings")).json();
    assert.equal(s.last.result, "refused: refused (403)");
    // The next is numbered past the refused one, even with the table emptied.
    env.DB.db.exec("DELETE FROM board_settings");
    s = await (await call("PUT", "/api/logger/settings", { body: JSON.stringify({ tz: "UTC0" }) }))
      .json();
    assert.equal(s.version, 5);
    // One taken back before the board had it.
    await call("PUT", "/api/logger/settings", { body: JSON.stringify({ tz: "UTC0" }) });
    s = await (await call("DELETE", "/api/logger/settings")).json();
    assert.equal(s.pending, null);
    assert.equal(s.last.result, "cancelled");
    // A board from before settings gets nothing.
    await call("PUT", "/api/logger/settings", { body: JSON.stringify({ tz: "UTC0" }) });
    assert.equal((await beat({})).settings_version, undefined);
  });

test("firmware: the board's own token can't publish it, nor can anyone write its keys", async () => {
  const board = await token({ email: undefined, common_name: "board0000000000.access" });
  const app = await token({ email: undefined, common_name: "app00000000000000.access" });
  const image = new Uint8Array(4096).fill(7);
  image[0] = 0xE9;                                      // an ESP32 app image
  // Before the board has checked in, it isn't known: any token publishes.
  assert.equal((await call("PUT", "/api/logger/firmware?version=v1", { body: image, auth: board })).status,
    200);
  await call("POST", "/api/logger/heartbeat", { body: JSON.stringify({ boot: 1, event: "beat" }),
    auth: board });
  let r = await call("PUT", "/api/logger/firmware?version=v2", { body: image, auth: board });
  assert.equal(r.status, 403);
  assert.match((await r.json()).error, /board's own token/);
  assert.equal((await (await call("GET", "/api/logger/firmware")).json()).version, "v1");
  // The app's service token and a person still publish.
  assert.equal((await call("PUT", "/api/logger/firmware?version=v3", { body: image, auth: app })).status,
    200);
  assert.equal((await call("PUT", "/api/logger/firmware?version=v4", { body: image })).status, 200);
  // Straight into the firmware's R2 keys: refused for everyone.
  for (const key of ["firmware/logger.bin", "firmware/logger.json"]) {
    assert.equal((await call("PUT", "/api/files/" + key, { body: "x", auth: app })).status, 403);
  }
  assert.equal((await (await call("GET", "/api/logger/firmware")).json()).version, "v4");
});

test("board settings: a check-in's answer doesn't drop a change saved after it", async () => {
  const beat = async (extra) => (await call("POST", "/api/logger/heartbeat",
    { body: JSON.stringify({ boot: 30, event: "beat", ...extra }) })).json();
  await beat({ settings_version: 0 });
  assert.equal((await call("PUT", "/api/logger/settings",
    { body: JSON.stringify({ wifi_ssid: "" }) })).status, 400);       // the first network can't go
  await call("PUT", "/api/logger/settings", { body: JSON.stringify({ tz: "UTC0" }) });
  assert.equal((await beat({ settings_version: 0 })).settings_version, 1);
  // A newer change arrives before the board's answer to the first.
  await call("PUT", "/api/logger/settings", { body: JSON.stringify({ wifi2_ssid: "Phone" }) });
  await beat({ settings_version: 1 });
  const s = await (await call("GET", "/api/logger/settings")).json();
  assert.equal(s.version, 2);
  assert.deepEqual(s.pending.keys, ["tz", "wifi2_ssid"]);
  assert.equal((await beat({ settings_version: 1 })).settings_version, 2);
});

const DETAIL_HEAD = "read_at,module,module_name,code,first_km,last_km,occurrences,snapshots," +
  "extended,time_s\n";

test("fault details: each code's odometer, dated from the drives, on the page and in MCP", async () => {
  await history("pc", [
    { file: "a.csv", start: "2026-10-01 08:00:00", odometer_km: 12000 },
    { file: "b.csv", start: "2026-10-10 08:00:00", odometer_km: 12400 },
    { file: "c.csv", start: "2026-10-20 08:00:00", odometer_start_km: 12500, odometer_end_km: 12560 },
  ], [{ module: "0x40", code: "804415", now: "present", last_seen: "2026-10-19", first_km: 11800 }]);
  let r = await (await call("PUT", "/api/files/logger/2026-10-29_08-15-00.details.csv", {
    body: DETAIL_HEAD +
      '2026-10-29 08:15:01,0x29,"Stability control (DSC)",480A14,12345,12546,7,"5904",' +
      '"5906480A142F0207",0.0\n' +
      '2026-10-29 08:15:02,0x12,"Engine, master DME",1A400A,,,,"refused (nrc 0x31)","no reply",0.0\n' +
      "junk,row\n" })).json();
  assert.equal(r.processed, true);
  assert.equal(r.details, 2);
  assert.equal(r.with_km, 1);
  // A later read that got nothing keeps the odometer; a lower first one wins.
  await call("PUT", "/api/files/logger/2026-10-30_08-00-00.details.csv", { body: DETAIL_HEAD +
    '2026-10-30 08:00:01,0x29,"Stability control (DSC)",480A14,,,,"no reply","no reply",0.0\n' });
  await call("POST", "/api/logger/heartbeat", { body: JSON.stringify({ boot: 3, faults: 3,
    fault_codes: "0x29:480A14 0x12:1A400A 0x40:804415" }) });
  const list = (await (await call("GET", "/api/logger")).json()).last.fault_list;
  const dsc = list.find((f) => f.code === "480A14");
  assert.equal(dsc.first_km, 12345);
  assert.equal(dsc.last_km, 12546);
  assert.equal(dsc.occurrences, 7);
  // 12,345 km: after the 1 Oct drive (12,000), by the 10 Oct one (12,400)
  assert.deepEqual(dsc.first_date, { after: "2026-10-01", by: "2026-10-10" });
  assert.deepEqual(dsc.last_date, { after: "2026-10-20", by: "2026-10-20" });   // during it
  const dme = list.find((f) => f.code === "1A400A");
  assert.equal(dme.first_km, null);                          // refused: nothing known
  assert.equal(dme.first_date, null);
  const fob = list.find((f) => f.code === "804415");         // the app's own read
  assert.equal(fob.first_km, 11800);
  assert.deepEqual(fob.first_date, { after: null, by: "2026-10-01" });   // before the drives
  const h = await toolData("fault_history", { which: "all" });
  assert.equal(h.faults.find((f) => f.code === "804415").first_km, 11800);
  // Not a drive log: no drive row from it.
  assert.equal((await toolData("drive_history")).drives.length, 3);
});

test("fault details: the odometer from the snapshots the board couldn't walk, bad drive odometers left out", async () => {
  await history("pc", [
    { file: "bad.csv", start: "2026-10-25 12:18:29", odometer_km: 48554 },     // misread
    { file: "d.csv", start: "2026-10-25 12:24:40", odometer_km: 111610 },
    { file: "e.csv", start: "2026-10-27 08:06:39", odometer_start_km: 111680, odometer_end_km: 111685 },
  ], []);
  // The head unit's E1C447 as an older board saved it: no odometer of its own.
  const nbt = "5904E1C447680006170737170001A2E717010F1EA5A34252291750AA17510112020106170737170001A2E8" +
    "17010F1EA83A4252341750AA17510112030206170737170001A2E917010F1EA85A4252351750AA1751011203";
  await call("PUT", "/api/files/logger/2026-10-30_07-14-00.details.csv", { body: DETAIL_HEAD +
    `2026-10-30 07:14:05,0x63,"Head unit (NBT Evo)",E1C447,,,133,"${nbt}","5906E1C4476801000285",0.0\n` +
    '2026-10-30 07:14:02,0x40,"Body domain controller (BDC)",804415,107031,107031,1,' +
    '"59048044152F0102170001A21717010F128F72","",0.0\n' });
  await call("POST", "/api/logger/heartbeat", { body: JSON.stringify({ boot: 4, faults: 2,
    fault_codes: "0x63:E1C447 0x40:804415" }) });
  const list = (await (await call("GET", "/api/logger")).json()).last.fault_list;
  const head = list.find((f) => f.code === "E1C447");
  assert.equal(head.first_km, 107239);
  assert.equal(head.last_km, 107241);
  assert.equal(head.occurrences, 133);
  // Before the first believable drive: not "on 25 Oct".
  assert.deepEqual(head.first_date, { after: null, by: "2026-10-25" });
  assert.deepEqual(list.find((f) => f.code === "804415").first_date, { after: null, by: "2026-10-25" });
});

test("snapshot odometer walk and odometer plausibility", async () => {
  const { snapshotKm, plausibleOdometers } = await import("../src/faultdetails.js");
  assert.equal(snapshotKm("59044806DA40", "0x29"), null);                       // nothing stored
  assert.equal(snapshotKm("590402192448010417505517510102004015334016AC", "0x19"), null);  // no odometer
  assert.deepEqual(snapshotKm("5904D42C6C280102170001B48417010FEF30D3", "0x2C"),
    { first_km: 111748, last_km: 111748 });
  assert.equal(snapshotKm("5904E1C447680006170737170001A2E7", "0x12"), null);  // 0x1707 isn't the DME's
  assert.equal(snapshotKm("no reply", "0x12"), null);
  const r = (start, lo, hi = lo) => ({ start, lo, hi });
  assert.deepEqual(plausibleOdometers([r("2026-10-25 12:18:29", 48554), r("2026-10-25 12:24:40", 111610),
    r("2026-10-26 08:00:00", 111656, 111660)]).map((x) => x.lo), [111610, 111656]);
  // Backwards: the higher, earlier one goes.
  assert.deepEqual(plausibleOdometers([r("2026-10-01 08:00:00", 999999), r("2026-10-02 08:00:00", 1000),
    r("2026-10-03 08:00:00", 1010)]).map((x) => x.lo), [1000, 1010]);
  // A long gap over days is fine.
  assert.equal(plausibleOdometers([r("2026-09-01 08:00:00", 100000), r("2026-10-01 08:00:00", 104000)]).length, 2);
});

test("logger check-ins: bad bodies refused, old ones dropped", async () => {
  const post = (body) => call("POST", "/api/logger/heartbeat", { body });
  assert.equal((await post("not json")).status, 400);
  assert.equal((await post("[1]")).status, 400);
  assert.equal((await post(JSON.stringify({ pad: "x".repeat(5000) }))).status, 413);
  env.DB.db.exec("CREATE TABLE IF NOT EXISTS heartbeats (received TEXT NOT NULL, boot INTEGER, " +
    "event TEXT, data TEXT NOT NULL)");
  env.DB.db.prepare("INSERT INTO heartbeats VALUES (?, 1, 'beat', '{}')")
    .run("2020-01-01T00:00:00.000Z");
  await post(JSON.stringify({ boot: 2 }));
  const n = env.DB.db.prepare("SELECT COUNT(*) AS n FROM heartbeats").get().n;
  assert.equal(n, 1);
});

test("check-ins kept offline: filed under their own clock, once each", async () => {
  const at = (minAgo) => {
    const d = new Date(Date.now() - minAgo * 60e3 - 5 * 3600e3);   // as the board's -05:00 clock
    return d.toISOString().slice(0, 23).replace("T", " ") + "-05:00";
  };
  const kept = [
    { boot: 9, event: "beat", buffered: true, clock: at(30), state: "logging", coolant_c: 96.5,
      oil_c: 101, lat: 29.76, lon: -95.36 },
    { boot: 9, event: "beat", buffered: true, clock: at(29), state: "logging", coolant_c: 104,
      wifi_note: "Phone: not seen (6 networks in range)" },
    { boot: 9, event: "beat", buffered: true },                        // no clock: skipped
    { boot: 9, event: "beat", buffered: true, clock: "2020-01-01 00:00:00.000-05:00" },  // too old
  ];
  const send = (beats) => call("POST", "/api/logger/heartbeats", { body: JSON.stringify({ beats }) });
  assert.deepEqual(await (await send(kept)).json(), { ok: true, stored: 2, skipped: 2 });
  assert.deepEqual(await (await send(kept.slice(0, 2))).json(), { ok: true, stored: 2, skipped: 0 });
  // The live one that follows is the newest; the kept ones count in its power-on.
  await call("POST", "/api/logger/heartbeat", { body: JSON.stringify({ boot: 9, event: "beat",
    state: "searching", coolant_c: 90 }) });
  const s = await (await call("GET", "/api/logger")).json();
  assert.equal(s.online, true);
  assert.equal(s.last.state, "searching");
  assert.deepEqual(s.sessions.map((x) => [x.boot, x.beats, x.buffered, x.max_coolant_c, x.max_oil_c]),
    [[9, 3, 2, 104, 101]]);
  assert.equal(s.sessions[0].wifi_note, "Phone: not seen (6 networks in range)");
  const first = env.DB.db.prepare("SELECT received FROM heartbeats ORDER BY received LIMIT 1").get();
  assert.ok(Math.abs(Date.parse(first.received) - (Date.now() - 30 * 60e3)) < 2000);
  assert.equal((await call("POST", "/api/logger/heartbeats", { body: "{}" })).status, 400);
  assert.equal((await call("POST", "/api/logger/heartbeats",
    { body: JSON.stringify({ beats: [], pad: "x".repeat(70000) }) })).status, 413);
});

test("logger drive rows carry the tank and odometer; tanks over HTTP and MCP", async () => {
  const log = (start, level0, level1, odo) => "Time,Clock,IKTFS,EGS_KILOMETERSTAND,MASSENSTROM_KRAFTSTOFF\n" +
    [0, 1, 2, 3, 4, 5].map((t) => `${t * 60},${start}:0${t}.000-05:00,${t < 3 ? level0 : level1},` +
      `${odo + t},20`).join("\n") + "\n";
  await call("PUT", "/api/files/logger/a.csv", { body: log("2026-10-25 07:00", 20, 15, 1000) });
  await call("PUT", "/api/files/logger/b.csv", { body: log("2026-10-26 07:00", 65, 60, 1010) });
  const [a, b] = (await toolData("drive_history")).drives;
  assert.deepEqual([a.tank_start_l, a.tank_end_l, a.odometer_start_km, a.odometer_end_km,
    a.start_approx], [20, 15, 1000, 1005, false]);
  assert.equal(b.tank_start_l, 65);
  const http = await (await call("GET", "/api/logger/tanks")).json();
  assert.deepEqual(http.tanks.map((t) => [t.start, t.drives, t.filled_l, t.open]),
    [["2026-10-25 07:00:00", 1, null, false], ["2026-10-26 07:00:00", 1, 50, true]]);
  assert.deepEqual((await toolData("tank_history")).tanks, http.tanks);
});

test("stored logger drives can be worked out again from their files", async () => {
  // A row from before the tank fields, and a log without a clock.
  await call("PUT", "/api/files/logger/a.csv", { body: LOGGER_CSV });
  await call("PUT", "/api/files/logger/nt-00001-01.csv",
    { body: "Time,IKTFS\n0,40\n60,40\n120,40\n180,39\n240,39\n300,39\n" });
  env.DB.db.prepare("UPDATE drives SET data = json_remove(data, '$.tank_start_l')").run();
  const r = await (await call("POST", "/api/logger/resummarize")).json();
  assert.deepEqual(r.done.map((d) => d.file), ["logger/a.csv", "logger/nt-00001-01.csv"]);
  assert.equal(r.next, null);
  const rows = (await toolData("drive_history")).drives;
  const nt = rows.find((d) => d.file === "logger/nt-00001-01.csv");
  assert.deepEqual([nt.tank_start_l, nt.start_approx], [40, true]);
  assert.match(nt.start, /^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$/);
});

test("firmware: publish a build, the board reads the manifest and the image", async () => {
  assert.equal((await call("GET", "/api/logger/firmware")).status, 404);
  const image = new Uint8Array([0xE9, 1, 2, 3, 4, 5]);
  const put = (version, body = image) => call("PUT",
    `/api/logger/firmware${version === undefined ? "" : "?version=" + version}`, { body });
  assert.equal((await put(undefined)).status, 400);                  // no version
  assert.equal((await put("dev")).status, 400);                      // the unversioned builds' name
  assert.equal((await put("a b")).status, 400);
  assert.equal((await put("v1", new Uint8Array([0x00, 1]))).status, 400);   // not an app image
  const r = await (await put("20261027-0200-abc1234")).json();
  assert.equal(r.version, "20261027-0200-abc1234");
  assert.equal(r.size, 6);
  assert.match(r.sha256, /^[0-9a-f]{64}$/);
  const m = await (await call("GET", "/api/logger/firmware")).json();
  assert.deepEqual([m.version, m.sha256, m.size], [r.version, r.sha256, 6]);
  const bin = await call("GET", "/api/logger/firmware.bin");
  assert.equal(bin.headers.get("content-length"), "6");
  assert.deepEqual(new Uint8Array(await bin.arrayBuffer()), image);
  // The status the page and logger_status read says what's published.
  assert.equal((await (await call("GET", "/api/logger")).json()).firmware.version, r.version);
  assert.equal((await call("GET", "/api/logger/firmware.bin", { auth: false })).status, 403);
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

// --- logger channels: catalog, presets, the board's channel file

const COLS = ["name", "module", "did", "did_size", "offset", "type", "order", "mul", "div", "add",
  "no_value", "unit", "info", "group"];
const CATALOG = {
  version: 1, columns: COLS,
  rows: [
    ["INMOT", "0x12", "0x4807", 2, 0, "s16", "big", "1", "4", "0", "", "1/min", "Engine speed", ""],
    ["ITOEL", "0x12", "0x4402", 2, 0, "u16", "big", "0.75", "1", "-48", "", "°C", "Oil temperature", ""],
    ["DSC_RDC_MESSDATENBLOCK_1.LETZTER_REIFENDRUCKWERT", "0x29", "0xDC98", 24, 5, "u16", "big", "1",
      "1000", "0", "", "bar", "Last valid tire pressure", "RDC_MESSDATENBLOCK_1"],
    ["DSC_RDC_MESSDATENBLOCK_1.LETZTER_REIFENTEMPERATURWERT", "0x29", "0xDC98", 24, 7, "u8", "big",
      "1", "1", "-50", "", "°C", "Last valid tire temperature", "RDC_MESSDATENBLOCK_1"],
    ["HU_ODD, \"quoted\"", "0x63", "0xD030", "", 0, "u8", "big", "1", "1", "0", "", "", "", ""],
  ],
  answers: { "0x12:0x4807": "ok", "0x12:0x4402": "ok", "0x29:0xDC98": "ok", "0x63:0xD030": "size" },
  modules: { "0x29": "DSC", "0x12": "DME (bank 1)" },
  defaults: { INMOT: "fast" },
};
const putCatalog = (c = CATALOG) => call("PUT", "/api/logger/catalog", { body: JSON.stringify(c) });
const presets = async (body) => (await call(body ? "PUT" : "GET", "/api/logger/presets",
  body ? { body: JSON.stringify(body) } : {})).json();
const channelRows = async (headers = {}) => {
  const r = await call("GET", "/api/logger/channels", { headers });
  if (r.status !== 200) return { r, rows: null };
  const [head, ...body] = parseCsv(await r.text());
  return { r, rows: body.map((x) => Object.fromEntries(head.map((h, i) => [h, x[i]]))) };
};

test("the first catalog makes the Default preset from the built-in tiers, active", async () => {
  assert.equal((await call("GET", "/api/logger/channels")).status, 404);   // nothing yet
  const put = await (await putCatalog()).json();
  assert.deepEqual([put.channels, put.answered], [5, 3]);
  const p = await presets();
  assert.equal(p.active, "Default");
  assert.deepEqual(p.presets.map((x) => [x.name, x.rest, x.tiers]), [["Default", "slow", { INMOT: "fast" }]]);
  assert.deepEqual(p.file.counts, { fast: 1, slow: 3 });
  const { r, rows } = await channelRows();
  assert.equal(r.headers.get("content-type"), "text/csv");
  // Fast first; the identifier that answered the wrong size is left out.
  assert.deepEqual(rows.map((x) => [x.name, x.tier]), [["INMOT", "fast"], ["ITOEL", "slow"],
    ["DSC_RDC_MESSDATENBLOCK_1.LETZTER_REIFENDRUCKWERT", "slow"],
    ["DSC_RDC_MESSDATENBLOCK_1.LETZTER_REIFENTEMPERATURWERT", "slow"]]);
  assert.deepEqual(Object.keys(rows[0]).slice(0, 13), ["name", "module", "did", "did_size", "offset",
    "type", "order", "mul", "div", "add", "no_value", "unit", "tier"]);
  assert.equal(rows[2].div, "1000");
});

test("answers merge: one that answered ok stays ok", async () => {
  await putCatalog();
  await putCatalog({ ...CATALOG, answers: { "0x29:0xDC98": "no answer", "0x63:0xD030": "ok" } });
  const c = await (await call("GET", "/api/logger/catalog")).json();
  assert.equal(c.answers["0x29:0xDC98"], "ok");
  assert.equal(c.answers["0x63:0xD030"], "ok");
  assert.equal((await presets()).presets.length, 1);                // Default made once only
});

test("presets: save, activate, and the board downloads only when it changed", async () => {
  await putCatalog();
  const first = await channelRows();
  const etag = first.r.headers.get("etag");
  assert.match(etag, /^"[0-9a-f]{16}"$/);
  assert.equal((await channelRows({ "If-None-Match": etag })).r.status, 304);
  const tires = { "DSC_RDC_MESSDATENBLOCK_1.LETZTER_REIFENDRUCKWERT": "daily",
    INMOT: "fast", ITOEL: "off", "HU_ODD, \"quoted\"": "weekly" };
  let p = await presets({ changes: [{ name: "Tires", rest: "off", tiers: tires }] });
  assert.equal(p.active, "Default");                                 // saving doesn't activate
  assert.equal((await channelRows({ "If-None-Match": etag })).r.status, 304);
  p = await presets({ active: "Tires" });
  assert.equal(p.active, "Tires");
  const { r, rows } = await channelRows({ "If-None-Match": etag });
  assert.equal(r.status, 200);
  assert.equal(decodeURIComponent(r.headers.get("x-preset")), "Tires");
  // The tire temperature isn't named and rest is off: still once a drive
  // (logger.js PER_DRIVE); ITOEL, named off, stays off.
  assert.deepEqual(rows.map((x) => [x.name, x.tier]), [["INMOT", "fast"],
    ["DSC_RDC_MESSDATENBLOCK_1.LETZTER_REIFENTEMPERATURWERT", "drive"],
    ["DSC_RDC_MESSDATENBLOCK_1.LETZTER_REIFENDRUCKWERT", "daily"], ["HU_ODD, \"quoted\"", "weekly"]]);
  assert.equal(rows[3].did_size, "");                                // a name with a comma survives
});

test("every preset reads the tires, tank level and odometer once a drive, unless named off", async () => {
  await putCatalog();
  await presets({ changes: [{ name: "Lean", rest: "off", tiers: { INMOT: "fast" } }], active: "Lean" });
  let { rows } = await channelRows();
  assert.deepEqual(rows.map((x) => [x.name, x.tier]), [["INMOT", "fast"],
    ["DSC_RDC_MESSDATENBLOCK_1.LETZTER_REIFENDRUCKWERT", "drive"],
    ["DSC_RDC_MESSDATENBLOCK_1.LETZTER_REIFENTEMPERATURWERT", "drive"]]);
  await presets({ changes: [{ name: "Lean", rest: "off", tiers: { INMOT: "fast",
    "DSC_RDC_MESSDATENBLOCK_1.LETZTER_REIFENDRUCKWERT": "off",
    "DSC_RDC_MESSDATENBLOCK_1.LETZTER_REIFENTEMPERATURWERT": "off" } }] });
  ({ rows } = await channelRows());
  assert.deepEqual(rows.map((x) => x.name), ["INMOT"]);
});

test("an identifier that misdecodes on this car is off, whatever the preset says", async () => {
  const imu = ["ACSM__IMU_WERTE.X_ACC_SCAL", "0x01", "0x4101", 30, 0, "u32", "big", "0.02", "1",
    "-41", "", "m/s²", "", "_IMU_WERTE"];
  await putCatalog({ ...CATALOG, rows: [...CATALOG.rows, imu],
    answers: { ...CATALOG.answers, "0x01:0x4101": "ok" } });
  await presets({ changes: [{ name: "Default", rest: "slow",
    tiers: { INMOT: "fast", "ACSM__IMU_WERTE.X_ACC_SCAL": "fast" } }] });
  const { rows } = await channelRows();
  assert.ok(!rows.some((x) => x.did === "0x4101"));
  assert.ok(rows.some((x) => x.name === "INMOT"));
});

test("what the board's own logic reads is at least medium, whatever the preset says", async () => {
  const gear = ["EGS_ISTGANG.FAHRSTUFE", "0x18", "0xDA2E", 2, 1, "u8", "big", "1", "1", "0", "",
    "0-n", "", "ISTGANG"];
  await putCatalog({ ...CATALOG, rows: [...CATALOG.rows, gear],
    answers: { ...CATALOG.answers, "0x18:0xDA2E": "ok" } });
  await presets({ changes: [{ name: "Default", rest: "off",
    tiers: { INMOT: "fast", "EGS_ISTGANG.FAHRSTUFE": "off" } }] });
  const tiers = Object.fromEntries((await channelRows()).rows.map((x) => [x.name, x.tier]));
  assert.equal(tiers["EGS_ISTGANG.FAHRSTUFE"], "medium");   // named off: the board needs it
  assert.equal(tiers.INMOT, "fast");                         // faster already: stays
});

test("presets refuse bad tiers, bad names and deleting the active one", async () => {
  await putCatalog();
  const put = (body) => call("PUT", "/api/logger/presets", { body: JSON.stringify(body) });
  assert.equal((await put({ changes: [{ name: "X", rest: "slow", tiers: { INMOT: "hourly" } }] })).status, 400);
  assert.equal((await put({ changes: [{ name: "X", rest: "fast", tiers: {} }] })).status, 400);
  assert.equal((await put({ changes: [{ name: " ", rest: "slow", tiers: {} }] })).status, 400);
  assert.equal((await put({ active: "Nope" })).status, 400);
  const r = await put({ changes: [{ name: "Default", deleted: true }] });
  assert.equal(r.status, 400);
  assert.match((await r.json()).error, /active/);
  // Renamed in one go: the new one saved and made active, the old one gone.
  const p = await presets({ active: "Mine", changes: [{ name: "Mine", rest: "slow", tiers: {} },
    { name: "Default", deleted: true }] });
  assert.deepEqual([p.active, p.presets.map((x) => x.name)], ["Mine", ["Mine"]]);
});

test("MCP logger tools: list tiers, set them one or in bulk, versions, rollback, publish", async () => {
  await putCatalog();
  let c = await tool("logger_channels", { module: "0x29" });
  assert.equal(c.data.preset, "Default");
  assert.deepEqual(c.data.channels.map((x) => [x.name, x.tier, x.named, x.answer]), [
    ["DSC_RDC_MESSDATENBLOCK_1.LETZTER_REIFENDRUCKWERT", "slow", false, "ok"],
    ["DSC_RDC_MESSDATENBLOCK_1.LETZTER_REIFENTEMPERATURWERT", "slow", false, "ok"]]);
  assert.equal((await tool("logger_channels", { tier: "fast" })).data.total, 1);
  // A dry run changes nothing; then one by name, then a whole module.
  let s = await tool("logger_set_tiers", { tier: "medium", module: "0x29", dry_run: true });
  assert.deepEqual([s.data.changed, s.data.dry_run], [2, true]);
  assert.equal((await tool("logger_channels", { tier: "medium" })).data.total, 0);
  s = await tool("logger_set_tiers", { tier: "medium", names: ["ITOEL"], why: "oil" });
  assert.deepEqual([s.data.changed, s.data.channels], [1, ["ITOEL"]]);
  s = await tool("logger_set_tiers", { tier: "daily", module: "0x29", did_from: "0xDC00",
    did_to: "0xDCFF" });
  assert.equal(s.data.changed, 2);
  assert.equal((await tool("logger_set_tiers", { tier: "daily", module: "0x29" })).data.changed, 0);
  assert.match((await tool("logger_set_tiers", { tier: "hourly", module: "0x29" })).data, /tier/);
  assert.match((await tool("logger_set_tiers", { tier: "fast" })).data, /select/);
  // A range that isn't hex is refused, not read as no bound (every channel).
  for (const bad of [{ did_from: "0xZZ" }, { did_to: "end" }, { module: "DME" }]) {
    const r = await tool("logger_set_tiers", { tier: "off", dry_run: true, ...bad });
    assert.match(String(r.data), /hex/, JSON.stringify(bad));
  }
  const rows = Object.fromEntries((await channelRows()).rows.map((x) => [x.name, x.tier]));
  assert.deepEqual([rows.ITOEL, rows["DSC_RDC_MESSDATENBLOCK_1.LETZTER_REIFENDRUCKWERT"]],
    ["medium", "daily"]);
  // Each change kept what it replaced.
  let v = (await tool("logger_presets", { name: "Default" })).data.versions;
  assert.deepEqual(v.map((x) => [x.why, x.counts]), [["2 channels to daily", { fast: 1, medium: 1 }],
    ["oil", { fast: 1 }]]);
  await tool("logger_rollback", { name: "Default", version: v[1].id });   // before the oil
  assert.equal((await tool("logger_channels", { names: ["ITOEL"] })).data.channels[0].tier, "slow");
  v = (await tool("logger_presets", { name: "Default" })).data.versions;
  assert.equal(v[0].why, `rolled back to version ${v[2].id}`);
  assert.match((await tool("logger_rollback", { name: "Default", version: 999 })).data, /no version/);
  // A copy, then published: the board's file comes from it.
  const saved = await tool("logger_save_preset", { name: "Lean", from: "Default", rest: "off" });
  assert.deepEqual([saved.data.saved, saved.data.active], ["Lean", "Default"]);
  const pub = await tool("logger_publish", { name: "Lean" });
  assert.equal(pub.data.active, "Lean");
  const list = (await tool("logger_presets", {})).data;
  assert.deepEqual([list.active, list.file.preset, list.presets.map((x) => x.name)],
    ["Lean", "Lean", ["Default", "Lean"]]);
  assert.match((await tool("logger_publish", { name: "Nope" })).data, /no preset/);
  // No name leaves the active one as it was, not none.
  for (const name of [undefined, "", "  "]) {
    assert.match(String((await tool("logger_publish", { name })).data), /active/);
  }
  assert.equal((await tool("logger_presets", {})).data.active, "Lean");
});

test("MCP drive_faults: one drive's reads with their codes and texts", async () => {
  await env.FILES.put("ref/BMW_fault_code_texts.csv", new TextEncoder().encode(
    "\ufeffcode,text_de,text_en,source_module\n480A14,Klopf,\"Knock, cylinder 1\",DME\n"));
  // An older drive named before the clock was known ("nt-" sorts after a
  // date): the newest is the last uploaded, not the last by name.
  await call("PUT", "/api/files/logger/nt-00067-02.faults.csv", { body: FAULT_HEAD +
    faultRow("", "0x29", "", "", "no faults", 60) });
  await new Promise((r) => setTimeout(r, 5));
  await call("PUT", "/api/files/logger/2026-10-27_20-03-50.faults.csv", { body: FAULT_HEAD +
    faultRow("2026-10-27 20:05:08", "0x12", "480A14", "0x2F", "present, stored", 71.6) +
    faultRow("2026-10-27 20:05:08", "0x12", "1A400A", "0x20", "not present", 71.6) +
    faultRow("2026-10-27 20:05:17", "0x17", "", "", "no reply", 80.2) });
  for (const name of [undefined, "logger/2026-10-27_20-03-50.csv"]) {
    const f = await tool("drive_faults", name ? { name } : {});
    assert.equal(f.data.file, "logger/2026-10-27_20-03-50.faults.csv");
    assert.deepEqual(f.data.reads.map((x) => [x.module, x.state, x.codes.length]),
      [["0x12", "listed codes", 2], ["0x17", "no reply", 0]]);
    assert.equal(f.data.reads[0].codes[0].text_en, "Knock, cylinder 1");
    assert.equal(f.data.failed, 1);
  }
  assert.match((await tool("drive_faults", { name: "logger/nope.csv" })).data, /no file/);
});

test("MCP channel_check: the sweep and what each module answers", async () => {
  await putCatalog();
  await call("PUT", "/api/files/logger/nt-00091-01.check.csv", { body:
    "module,did,job,status,detail,reply_bytes,raw\n0x63,0xD030,logger sweep s1,refused,0x31,,\n" });
  const all = (await tool("channel_check", {})).data;
  assert.deepEqual(all.sweep, { pending: null, last: null });
  const dme = all.modules.find((m) => m.module === "0x12");
  assert.deepEqual([dme.ok, dme.unchecked], [2, 0]);
  assert.equal(all.modules.find((m) => m.module === "0x63").absent, 1);
  const one = (await tool("channel_check", { module: "0x12", status: "ok" })).data;
  assert.deepEqual(one.identifiers.map((x) => x.did), ["0x4402", "0x4807"]);
  assert.equal(one.name, "DME (bank 1)");
});

test("the catalog must have the board's columns", async () => {
  const bad = { ...CATALOG, columns: COLS.filter((c) => c !== "offset") };
  assert.equal((await putCatalog(bad)).status, 400);
  assert.equal((await putCatalog({ ...CATALOG, rows: [["x"]] })).status, 400);
});

test("the logger page is served, behind Access like the rest", async () => {
  const r = await call("GET", "/logger");
  assert.equal(r.status, 200);
  assert.match(r.headers.get("content-type"), /text\/html/);
  const html = await r.text();
  assert.match(html, /<title>M550 Logger<\/title>/);
  assert.match(html, /\/api\/logger\/presets/);
  assert.match(html, /\/api\/logger\/tanks/);
  // The page's script parses (the embedded helpers included).
  for (const [, js] of html.matchAll(/<script>([\s\S]*?)<\/script>/g)) new Function(js);
  assert.equal((await call("GET", "/logger", { auth: false })).status, 403);
});

// --- the full channel sweep (sweep.js)

const beatReply = async (b) => (await call("POST", "/api/logger/heartbeat",
  { body: JSON.stringify({ boot: 7, event: "beat", ...b }) })).json();
const sweepState = async () => (await call("GET", "/api/logger/sweep")).json();
const putCheck = async (name, rows, job) => (await call("PUT", `/api/files/logger/${name}`, {
  body: "module,did,job,status,detail,reply_bytes,raw\n" +
    rows.map(([m, d, s]) => `${m},${d},${job},${s},,,`).join("\n") + "\n" })).json();

test("sweep: the board's list has each identifier once, with the size its layout needs", async () => {
  assert.equal((await call("GET", "/api/logger/check-list")).status, 404);   // no catalog yet
  await putCatalog();
  const r = await call("GET", "/api/logger/check-list");
  assert.equal(r.headers.get("content-type"), "text/csv");
  assert.equal(await r.text(), "module,did,size,exact\n" +
    "0x12,0x4402,2,1\n0x12,0x4807,2,1\n0x29,0xDC98,24,1\n" +
    "0x63,0xD030,1,0\n");            // no fixed size: at least the furthest field's end
});

test("sweep: asked on the page, carried in check-in replies, answers merged, done", async () => {
  await putCatalog();
  assert.deepEqual(await sweepState(), { pending: null, last: null });
  assert.equal((await beatReply({})).sweep, null);
  const a = await (await call("POST", "/api/logger/sweep")).json();
  assert.equal(a.created, true);
  const id = a.pending.id;
  assert.match(id, /^s\d{8}-\d{6}$/);
  assert.equal((await (await call("POST", "/api/logger/sweep")).json()).pending.id, id);  // once
  assert.equal((await beatReply({})).sweep, id);
  assert.equal((await sweepState()).pending.seen, undefined);
  // The board has it and is under way.
  assert.equal((await beatReply({ sweep: id, sweep_next: 2, sweep_of: 4, sweep_silent: "0x40" })).sweep, id);
  let p = (await sweepState()).pending;
  assert.deepEqual([p.next, p.of, p.silent, typeof p.seen], [2, 4, "0x40", "string"]);
  // Its answers come up with the drive: the wrong-size one answers now, a
  // refusal doesn't undo an ok, one not in the catalog is left alone.
  const job = `logger sweep ${id}`;
  const up = await putCheck("nt-00080-01.check.csv",
    [["0x63", "0xD030", "ok"], ["0x29", "0xDC98", "refused"], ["0x12", "0x4807", "ok"],
      ["0x77", "0x0001", "ok"]], job);
  assert.deepEqual([up.processed, up.identifiers, up.changed, up.newly_ok, up.sweep],
    [true, 4, 1, 1, id]);
  const c = await (await call("GET", "/api/logger/catalog")).json();
  assert.equal(c.answers["0x63:0xD030"], "ok");
  assert.equal(c.answers["0x29:0xDC98"], "ok");
  const { rows } = await channelRows();
  assert.ok(rows.some((x) => x.name === "HU_ODD, \"quoted\""));      // logged from now on
  await putCheck("nt-00080-01.check.csv", [["0x63", "0xD030", "ok"]], job);   // sent again
  p = (await sweepState()).pending;
  assert.deepEqual([p.counts, p.newly_ok], [{ ok: 2, refused: 1 }, 1]);
  // Done: the flag clears and the page shows when.
  assert.equal((await beatReply({ sweep_done: id, sweep_silent: "0x40" })).sweep, null);
  const s = await sweepState();
  assert.equal(s.pending, null);
  assert.deepEqual([s.last.id, s.last.silent, s.last.next, s.last.newly_ok], [id, "0x40", 4, 1]);
  assert.ok(s.last.done >= s.last.requested);
  assert.equal((await (await call("GET", "/api/logger")).json()).sweep.last.id, id);
  // A drive's file that comes up after the board said done still counts.
  await putCheck("nt-00081-01.check.csv", [["0x12", "0x4402", "no answer"]], job);
  assert.deepEqual((await sweepState()).last.counts, { ok: 2, refused: 1, "no answer": 1 });
  assert.equal((await beatReply({ sweep_done: id })).sweep, null);   // said again: no change
});

test("sweep: cancelled from the page, the next check-in says so", async () => {
  await putCatalog();
  const id = (await (await call("POST", "/api/logger/sweep")).json()).pending.id;
  assert.equal((await beatReply({})).sweep, id);
  assert.deepEqual(await (await call("DELETE", "/api/logger/sweep")).json(),
    { ok: true, cancelled: id });
  assert.equal((await beatReply({ sweep: id, sweep_next: 1 })).sweep, null);
  assert.deepEqual(await sweepState(), { pending: null, last: null });
  assert.equal((await (await call("DELETE", "/api/logger/sweep")).json()).cancelled, null);
});

test("an identifier a module hasn't (0x31) is off for good, named or not; ok stays ok", async () => {
  await putCatalog({ ...CATALOG, answers: { "0x12:0x4807": "ok", "0x29:0xDC98": "ok" } });
  await presets({ changes: [{ name: "Default", rest: "slow",
    tiers: { INMOT: "fast", ITOEL: "medium", "HU_ODD, \"quoted\"": "medium" } }] });
  // From a check: refused with 0x31 is absent, unless it answered ok before.
  await call("PUT", "/api/files/logger/nt-00090-01.check.csv", { body:
    "module,did,job,status,detail,reply_bytes,raw\n" +
    "0x29,0xDC98,logger sweep s1,refused,0x31,,\n0x63,0xD030,logger sweep s1,refused,0x31,,\n" });
  let c = await (await call("GET", "/api/logger/catalog")).json();
  assert.deepEqual([c.answers["0x29:0xDC98"], c.answers["0x63:0xD030"]], ["ok", "absent"]);
  // From a drive's notes: the board rests it for the drive and says so.
  const log = "Time,Clock,INMOT,Event\n0,2026-10-25 07:00:00.000-05:00,700,start\n" +
    "1,2026-10-25 07:00:01.000-05:00,700,module 0x12 hasn't 0x4402 (0x31); " +
    "module 0x12 hasn't 0x4807 (0x13)\n" +
    [2, 3, 4, 5].map((t) => `${t},2026-10-25 07:00:0${t}.000-05:00,700,`).join("\n") + "\n";
  const up = await (await call("PUT", "/api/files/logger/2026-10-25_07-00-00.csv", { body: log })).json();
  assert.equal(up.absent, 1);                        // 0x13 is "too long", not absent
  c = await (await call("GET", "/api/logger/catalog")).json();
  assert.equal(c.answers["0x12:0x4402"], "absent");
  const { rows } = await channelRows();              // named medium, but they aren't there
  assert.deepEqual(rows.map((x) => x.name).filter((n) => n !== "DSC_RDC_MESSDATENBLOCK_1." +
    "LETZTER_REIFENDRUCKWERT" && n !== "DSC_RDC_MESSDATENBLOCK_1.LETZTER_REIFENTEMPERATURWERT"),
  ["INMOT"]);
});

test("sweep: a check file that isn't one is stored, not processed", async () => {
  await putCatalog();
  const r = await (await call("PUT", "/api/files/logger/x.check.csv", { body: "a,b\n1,2\n" })).json();
  assert.deepEqual([r.ok, r.processed], [true, false]);
  assert.match(r.reason, /not a check CSV/);
});

test("MCP logger tools by values: never changed, only goes up", async () => {
  await putCatalog();
  const drive = (name, t0, chunks) => call("PUT", `/api/series/${name}`, { body: JSON.stringify({
    start: 0, approx: false, chunks: chunks.map(([ch, data]) => ({ name: ch, t0, t1: t0 + 1,
      min: Math.min(...data.map((d) => d[1])), max: Math.max(...data.map((d) => d[1])),
      mean: 0, data })) }) });
  await drive("a.csv", 1000, [["ITOEL", [[0, 90], [1, 90]]], ["INMOT", [[0, 700], [1, 800]]]]);
  await drive("b.csv", 2000, [["ITOEL", [[0, 90]]], ["INMOT", [[0, 800], [1, 900]]]]);
  let c = await tool("logger_channels", { values: "same" });
  assert.deepEqual(c.data.channels.map((x) => [x.name, x.lo, x.hi, x.drives]), [["ITOEL", 90, 90, 2]]);
  c = await tool("logger_channels", { values: "rises", module: "0x12" });
  assert.deepEqual(c.data.channels.map((x) => x.name), ["INMOT"]);
  assert.equal((await tool("logger_channels", { values: "rises", module: "0x29" })).data.total, 0);
  // values alone selects for a bulk change.
  const s = await tool("logger_set_tiers", { tier: "monthly", values: "same", dry_run: true });
  assert.deepEqual([s.data.changed, s.data.channels], [1, ["ITOEL"]]);
  assert.match(String((await tool("logger_channels", { values: "odd" })).data), /values/);
});
