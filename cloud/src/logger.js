/**
 * What the logger board reads, and how often: the channel catalog the app
 * sends, presets that give channels a tier, and the board's channels.csv
 * made from the active one.
 *
 * - The app sends the catalog (PUT /api/logger/catalog): every channel the
 *   board can decode (n63read loggerchannels.catalog_payload), what the car
 *   answered, and the built-in fast and medium lists. Answers merge with
 *   what was sent before: an identifier that answered "ok" once stays "ok".
 * - A preset is a name, a tier per channel, and `rest`: the tier of every
 *   channel it doesn't name that the car answers ("slow" or "off"; the
 *   tires, tank level and odometer, PER_DRIVE, at least once a drive); a
 *   channel that doesn't answer and isn't named is off. The first catalog
 *   makes "Default" (the built-in lists, everything else slow) and makes it
 *   active.
 * - The board asks for GET /api/logger/channels with the ETag it has; it
 *   downloads only when the active preset or the catalog changed.
 *
 * Tiers: fast (every cycle), medium (a few times a second), slow (in
 * rotation), drive (once a drive), daily, weekly, monthly (once, in the
 * first drive they're due), off.
 */

export const CATALOG_KEY = "config/logger-catalog.json";
export const TIERS = ["fast", "medium", "slow", "drive", "daily", "weekly", "monthly", "off"];
export const RESTS = ["slow", "off"];
export const DEFAULT_PRESET = "Default";
// The board's columns (firmware/lib/logger/channels.h reads them by name).
export const HEADER = ["name", "module", "did", "did_size", "offset", "type", "order", "mul",
  "div", "add", "no_value", "unit", "tier"];
const NEEDED = HEADER.slice(0, -1);
const MAX_ROWS = 50000;
const MAX_NAME = 60;
const MAX_TIERS = 20000;

export const LOGGER_SCHEMA = [
  `CREATE TABLE IF NOT EXISTS logger_presets (name TEXT PRIMARY KEY, rest TEXT NOT NULL,
     tiers TEXT NOT NULL, updated TEXT NOT NULL)`,
  `CREATE TABLE IF NOT EXISTS logger_settings (key TEXT PRIMARY KEY, value TEXT)`,
];

export class ConfigError extends Error {}

const isObject = (v) => v && typeof v === "object" && !Array.isArray(v);

export async function getCatalog(env) {
  const obj = await env.FILES.get(CATALOG_KEY);
  return obj ? JSON.parse(await obj.text()) : null;
}

/** Store the app's catalog, answers merged with the stored ones. */
export async function putCatalog(env, body, now = new Date()) {
  if (!isObject(body) || !Array.isArray(body.columns) || !Array.isArray(body.rows)) {
    throw new ConfigError("body: JSON {columns, rows, answers, modules, defaults}");
  }
  const missing = NEEDED.filter((c) => !body.columns.includes(c));
  if (missing.length) throw new ConfigError(`columns missing: ${missing.join(", ")}`);
  if (body.rows.length > MAX_ROWS) throw new ConfigError("too many rows");
  const width = body.columns.length;
  if (!body.rows.every((r) => Array.isArray(r) && r.length === width && typeof r[0] === "string")) {
    throw new ConfigError(`rows: lists of ${width} values, the name first`);
  }
  const old = await getCatalog(env);
  const answers = { ...(old && old.answers) };
  for (const [k, v] of Object.entries(isObject(body.answers) ? body.answers : {})) {
    if (typeof v === "string" && answers[k] !== "ok") answers[k] = v;
  }
  const defaults = {};
  for (const [k, v] of Object.entries(isObject(body.defaults) ? body.defaults : {})) {
    if (TIERS.includes(v)) defaults[k] = v;
  }
  const catalog = { version: 1, updated: now.toISOString(), columns: body.columns,
    rows: body.rows, answers, modules: isObject(body.modules) ? body.modules : {}, defaults };
  await env.FILES.put(CATALOG_KEY, new TextEncoder().encode(JSON.stringify(catalog)),
    { httpMetadata: { contentType: "application/json" } });
  const have = (await env.DB.prepare("SELECT COUNT(*) AS n FROM logger_presets").all())
    .results[0].n;
  if (!have) {
    await env.DB.batch([
      env.DB.prepare("INSERT INTO logger_presets (name, rest, tiers, updated) VALUES (?, ?, ?, ?)")
        .bind(DEFAULT_PRESET, "slow", JSON.stringify(defaults), now.toISOString()),
      env.DB.prepare("INSERT OR REPLACE INTO logger_settings (key, value) VALUES ('active', ?)")
        .bind(DEFAULT_PRESET),
    ]);
  }
  return { channels: body.rows.length,
    answered: Object.values(answers).filter((v) => v === "ok").length };
}

async function active(env) {
  const r = (await env.DB.prepare("SELECT value FROM logger_settings WHERE key = 'active'").all())
    .results;
  return r.length ? r[0].value : null;
}

/** {active, presets: [{name, rest, tiers, updated}]}. */
export async function getPresets(env) {
  const r = (await env.DB.prepare(
    "SELECT name, rest, tiers, updated FROM logger_presets ORDER BY name").all()).results || [];
  return { active: await active(env),
    presets: r.map((p) => ({ name: p.name, rest: p.rest, tiers: JSON.parse(p.tiers),
      updated: p.updated })) };
}

function checkTiers(name, tiers) {
  if (!isObject(tiers)) throw new ConfigError(`${name}: tiers is {channel: tier}`);
  const entries = Object.entries(tiers);
  if (entries.length > MAX_TIERS) throw new ConfigError(`${name}: too many channels`);
  for (const [k, v] of entries) {
    if (!k || k.length > 200 || !TIERS.includes(v)) {
      throw new ConfigError(`${name}: bad tier for ${k.slice(0, 60)}: ${String(v).slice(0, 20)}`);
    }
  }
}

/** Apply {changes: [{name, rest, tiers} | {name, deleted: true}], active?}. */
export async function putPresets(env, body, now = new Date()) {
  if (!isObject(body)) throw new ConfigError("body: JSON {changes, active}");
  const changes = body.changes === undefined ? [] : body.changes;
  if (!Array.isArray(changes)) throw new ConfigError("changes: a list");
  const current = await getPresets(env);
  const names = new Set(current.presets.map((p) => p.name));
  let want = typeof body.active === "string" ? body.active : current.active;
  const stmts = [];
  for (const c of changes) {
    const name = isObject(c) && typeof c.name === "string" ? c.name.trim() : "";
    if (!name || name.length > MAX_NAME || /[\x00-\x1f]/.test(name)) {
      throw new ConfigError("bad preset name");
    }
    if (c.deleted) {
      names.delete(name);
      stmts.push(env.DB.prepare("DELETE FROM logger_presets WHERE name = ?").bind(name));
      continue;
    }
    if (!RESTS.includes(c.rest)) throw new ConfigError(`${name}: rest is "slow" or "off"`);
    checkTiers(name, c.tiers);
    names.add(name);
    stmts.push(env.DB.prepare(
      "INSERT OR REPLACE INTO logger_presets (name, rest, tiers, updated) VALUES (?, ?, ?, ?)")
      .bind(name, c.rest, JSON.stringify(c.tiers), now.toISOString()));
  }
  if (want && !names.has(want)) {
    throw new ConfigError(body.active === want ? `no preset named ${want}`
      : `${want} is the active preset: make another active first`);
  }
  stmts.push(env.DB.prepare("INSERT OR REPLACE INTO logger_settings (key, value) VALUES ('active', ?)")
    .bind(want || null));
  await env.DB.batch(stmts);
  return getPresets(env);
}

function csvCell(v) {
  const s = v === null || v === undefined ? "" : String(v);
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

// Read in every drive whatever the preset leaves to `rest`: the tire
// pressures, temperatures and wheel positions (History's tire columns), and
// the tank level and odometer that tell a fill-up and the tank's distance
// (tanks.js). A preset can still turn one off by name.
export const PER_DRIVE = ["IKTFS", "FUELLST_TANKH_L", "FUELLST_TANKH_R",
  "BDC_TANK_FUELLSTAND_LINKS", "BDC_TANK_FUELLSTAND_RECHTS", "EGS_KILOMETERSTAND"];
export const PER_DRIVE_RE = /^DSC_RDC_MESSDATENBLOCK_[1-4]\./;

export function perDrive(name) {
  return PER_DRIVE_RE.test(name) || PER_DRIVE.includes(name);
}

/** Each catalog row's tier under a preset: named, else `rest` where the
 * car answers (at least once a drive for the PER_DRIVE channels), else off. */
export function tiersFor(catalog, preset) {
  const col = Object.fromEntries(catalog.columns.map((c, i) => [c, i]));
  return catalog.rows.map((r) => {
    const named = preset.tiers[r[col.name]];
    if (named) return named;
    if (catalog.answers[`${r[col.module]}:${r[col.did]}`] !== "ok") return "off";
    return preset.rest === "off" && perDrive(r[col.name]) ? "drive" : preset.rest;
  });
}

async function sha256Hex(text) {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** The board's channels.csv from the active preset: {csv, etag, preset,
 * counts}, or null when there's no catalog or no active preset. */
export async function channelFile(env) {
  const [catalog, presets] = await Promise.all([getCatalog(env), getPresets(env)]);
  const preset = catalog && presets.presets.find((p) => p.name === presets.active);
  if (!preset) return null;
  const col = Object.fromEntries(catalog.columns.map((c, i) => [c, i]));
  const tiers = tiersFor(catalog, preset);
  const picked = [];
  catalog.rows.forEach((r, i) => { if (tiers[i] !== "off") picked.push([r, tiers[i]]); });
  const num = (s) => parseInt(s, 16);
  picked.sort(([a, ta], [b, tb]) => TIERS.indexOf(ta) - TIERS.indexOf(tb) ||
    num(a[col.module]) - num(b[col.module]) || num(a[col.did]) - num(b[col.did]) ||
    a[col.offset] - b[col.offset] || (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
  const counts = {};
  const lines = [HEADER.join(",")];
  for (const [r, tier] of picked) {
    counts[tier] = (counts[tier] || 0) + 1;
    lines.push([...NEEDED.map((c) => csvCell(r[col[c]])), tier].join(","));
  }
  const csv = lines.join("\n") + "\n";
  return { csv, etag: (await sha256Hex(csv)).slice(0, 16), preset: preset.name, counts };
}
