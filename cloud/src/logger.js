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
 * - Every change to a preset (the page's or the MCP tools') keeps what it
 *   replaced as a version (logger_preset_versions, the last MAX_VERSIONS a
 *   preset), so any can be rolled back to.
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
  `CREATE TABLE IF NOT EXISTS logger_preset_versions (id INTEGER PRIMARY KEY AUTOINCREMENT,
     name TEXT NOT NULL, rest TEXT NOT NULL, tiers TEXT NOT NULL, saved TEXT NOT NULL,
     why TEXT)`,
];
const MAX_VERSIONS = 50;                // kept a preset

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

/** Apply {changes: [{name, rest, tiers} | {name, deleted: true}], active?}.
 * Each preset changed or deleted keeps what it was as a version first
 * (`why`, optional, says what replaced it). */
export async function putPresets(env, body, now = new Date(), why = null) {
  if (!isObject(body)) throw new ConfigError("body: JSON {changes, active}");
  const changes = body.changes === undefined ? [] : body.changes;
  if (!Array.isArray(changes)) throw new ConfigError("changes: a list");
  const current = await getPresets(env);
  const names = new Set(current.presets.map((p) => p.name));
  const before = new Map(current.presets.map((p) => [p.name, p]));
  let want = typeof body.active === "string" ? body.active.trim() : current.active;
  if (typeof body.active === "string" && !want) {
    throw new ConfigError("active: a preset's name");   // "" would leave none active
  }
  const stmts = [];
  const keep = (name) => {              // the preset as it was, as a version
    const old = before.get(name);
    if (!old) return;
    before.delete(name);                // once, however many changes name it
    stmts.push(env.DB.prepare(`INSERT INTO logger_preset_versions (name, rest, tiers, saved, why)
      VALUES (?, ?, ?, ?, ?)`).bind(name, old.rest, JSON.stringify(old.tiers), now.toISOString(),
      why ? String(why).slice(0, 200) : null));
    stmts.push(env.DB.prepare(`DELETE FROM logger_preset_versions WHERE name = ? AND id NOT IN
      (SELECT id FROM logger_preset_versions WHERE name = ? ORDER BY id DESC LIMIT ?)`)
      .bind(name, name, MAX_VERSIONS));
  };
  for (const c of changes) {
    const name = isObject(c) && typeof c.name === "string" ? c.name.trim() : "";
    if (!name || name.length > MAX_NAME || /[\x00-\x1f]/.test(name)) {
      throw new ConfigError("bad preset name");
    }
    if (c.deleted) {
      keep(name);
      names.delete(name);
      stmts.push(env.DB.prepare("DELETE FROM logger_presets WHERE name = ?").bind(name));
      continue;
    }
    if (!RESTS.includes(c.rest)) throw new ConfigError(`${name}: rest is "slow" or "off"`);
    checkTiers(name, c.tiers);
    const old = before.get(name);
    if (old && old.rest === c.rest && JSON.stringify(old.tiers) === JSON.stringify(c.tiers)) {
      continue;                         // unchanged: no version, no write
    }
    keep(name);
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

/** A preset's versions, newest first: [{id, saved, why, rest, counts}]. */
export async function presetVersions(env, name) {
  const r = (await env.DB.prepare(`SELECT id, rest, tiers, saved, why FROM logger_preset_versions
    WHERE name = ? ORDER BY id DESC`).bind(name).all()).results || [];
  return r.map((v) => {
    const counts = {};
    for (const t of Object.values(JSON.parse(v.tiers))) counts[t] = (counts[t] || 0) + 1;
    return { id: v.id, saved: v.saved, why: v.why, rest: v.rest, counts };
  });
}

/** A preset back as one of its versions was (itself kept as a version). */
export async function rollbackPreset(env, name, id, now = new Date()) {
  const r = (await env.DB.prepare(`SELECT rest, tiers FROM logger_preset_versions
    WHERE name = ? AND id = ?`).bind(name, id).all()).results;
  if (!r || !r.length) throw new ConfigError(`${name} has no version ${id}`);
  return putPresets(env, { changes: [{ name, rest: r[0].rest, tiers: JSON.parse(r[0].tiers) }] },
    now, `rolled back to version ${id}`);
}

/** Which catalog rows a selection names: `names` (exact), `module`
 * ("0x12"), `did_from`/`did_to` (an identifier range, "0x4000"-"0x40FF"),
 * `match` (part of a name, any case), all that are given. */
export function selectRows(catalog, sel) {
  const col = Object.fromEntries(catalog.columns.map((c, i) => [c, i]));
  const num = (s, what) => {          // hex, "0x" or not; anything else is refused, not ignored
    if (s === undefined || s === null || s === "") return null;
    const v = String(s).trim();
    if (!/^(0x)?[0-9a-f]+$/i.test(v)) throw new ConfigError(`${what}: hex, like 0x4000`);
    return parseInt(v, 16);
  };
  const module = num(sel.module, "module"), from = num(sel.did_from, "did_from"),
    to = num(sel.did_to, "did_to");
  const names = Array.isArray(sel.names) ? new Set(sel.names.map(String)) : null;
  const match = sel.match ? String(sel.match).toLowerCase() : null;
  if (!names && module === null && from === null && to === null && !match) {
    throw new ConfigError("select channels: names, module, did_from/did_to or match");
  }
  const out = [];
  catalog.rows.forEach((r, i) => {
    const did = parseInt(r[col.did], 16);
    if (names && !names.has(r[col.name])) return;
    if (module !== null && parseInt(r[col.module], 16) !== module) return;
    if (from !== null && did < from) return;
    if (to !== null && did > to) return;
    if (match && !String(r[col.name]).toLowerCase().includes(match)) return;
    out.push(i);
  });
  return out;
}

async function presetNamed(env, name) {
  const all = await getPresets(env);
  const want = name || all.active;
  const p = all.presets.find((x) => x.name === want);
  if (!p) throw new ConfigError(want ? `no preset named ${want}` : "no active preset");
  return { preset: p, active: all.active };
}

/** Channels and their tier under a preset (the active one by default):
 * {preset, total, channels: [{name, module, did, tier, named, answer, unit,
 * info}]}, filtered as selectRows (all, without a selection) and by `tier`,
 * at most `limit` (default 200, up to 2000). */
export async function listTiers(env, a = {}) {
  const catalog = await getCatalog(env);
  if (!catalog) throw new ConfigError("no catalog yet: the app sends it");
  const { preset } = await presetNamed(env, a.preset);
  const col = Object.fromEntries(catalog.columns.map((c, i) => [c, i]));
  const tiers = tiersFor(catalog, preset);
  let idx = a.names || a.module || a.did_from || a.did_to || a.match
    ? selectRows(catalog, a) : catalog.rows.map((_, i) => i);
  if (a.tier) idx = idx.filter((i) => tiers[i] === a.tier);
  const limit = Math.max(1, Math.min(2000, Number(a.limit) || 200));
  return { preset: preset.name, total: idx.length, channels: idx.slice(0, limit).map((i) => {
    const r = catalog.rows[i];
    return { name: r[col.name], module: r[col.module], did: r[col.did], tier: tiers[i],
      named: !!preset.tiers[r[col.name]],
      answer: catalog.answers[`${r[col.module]}:${r[col.did]}`] || null,
      unit: r[col.unit] || "", info: col.info === undefined ? "" : r[col.info] || "" };
  }) };
}

/** Set the tier of the selected channels in a preset (the active one by
 * default); "default" un-names them (back to the preset's rest). `dry_run`
 * says what would change. {preset, tier, changed, channels (the first 50)}. */
export async function setTiers(env, a, now = new Date()) {
  const catalog = await getCatalog(env);
  if (!catalog) throw new ConfigError("no catalog yet: the app sends it");
  if (a.tier !== "default" && !TIERS.includes(a.tier)) {
    throw new ConfigError(`tier: one of ${TIERS.join(", ")}, or default`);
  }
  const { preset } = await presetNamed(env, a.preset);
  const col = Object.fromEntries(catalog.columns.map((c, i) => [c, i]));
  const tiers = { ...preset.tiers };
  const changed = [];
  for (const i of selectRows(catalog, a)) {
    const name = catalog.rows[i][col.name];
    if (a.tier === "default" ? !(name in tiers) : tiers[name] === a.tier) continue;
    if (a.tier === "default") delete tiers[name];
    else tiers[name] = a.tier;
    changed.push(name);
  }
  if (changed.length && !a.dry_run) {
    await putPresets(env, { changes: [{ name: preset.name, rest: preset.rest, tiers }] }, now,
      a.why || `${changed.length} channels to ${a.tier}`);
  }
  return { preset: preset.name, tier: a.tier, changed: changed.length, dry_run: !!a.dry_run,
    channels: changed.slice(0, 50) };
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

// Identifiers whose answer on this car doesn't fit the catalog's layout, off
// whatever a preset says (as n63read's loggerchannels.UNDECODABLE): the
// airbag module's _IMU_WERTE holds an event record here, not accelerations,
// which read in the millions.
export const UNDECODABLE = new Set(["0x01:0x4101"]);

// What the board's own logic reads, at least this often whatever a preset
// says (a faster tier stays): the selector and the engine speed for Park,
// engine-off and the end of a drive, the speed for the full channel check's
// standing still, and the check-ins' coolant and battery.
export const BOARD_NEEDS = { INMOT: "medium", FZG_GESCHWINDIGKEIT_VA_HA_FASCHWERP: "medium",
  "EGS_ISTGANG.FAHRSTUFE": "medium", ITKUM: "medium", IUIBS: "medium" };

const faster = (a, b) => (TIERS.indexOf(a) <= TIERS.indexOf(b) ? a : b);

export function perDrive(name) {
  return PER_DRIVE_RE.test(name) || PER_DRIVE.includes(name);
}

/** Each catalog row's tier under a preset: named, else `rest` where the
 * car answers (at least once a drive for the PER_DRIVE channels), else off;
 * an UNDECODABLE identifier, or one its module says it hasn't ("absent",
 * sweep.js), always off; one the board's logic reads (BOARD_NEEDS) at least
 * its tier there. */
export function tiersFor(catalog, preset) {
  const col = Object.fromEntries(catalog.columns.map((c, i) => [c, i]));
  return catalog.rows.map((r) => {
    const key = `${r[col.module]}:${r[col.did]}`;
    if (UNDECODABLE.has(key) || catalog.answers[key] === "absent") return "off";
    const need = BOARD_NEEDS[r[col.name]];
    const named = preset.tiers[r[col.name]];
    let tier;
    if (named) tier = named;
    else if (catalog.answers[key] !== "ok") tier = "off";
    else tier = preset.rest === "off" && perDrive(r[col.name]) ? "drive" : preset.rest;
    return need ? faster(tier, need) : tier;
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
