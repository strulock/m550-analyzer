/**
 * The logger board's settings from the logger page: its Wi-Fi networks, the
 * setup access point's password, its Access token and its time zone (the
 * board keeps them in its flash: firmware/lib/logger/settings.h).
 *
 * A change waits here until the board's next check-in, whose reply carries
 * it ("settings_version" and a "set_<key>" per field, the value in hex); the
 * board says in its check-ins which version it has (settings_version) or
 * which it refused and why (settings_failed, settings_error). Then the
 * change, passwords and all, is dropped, leaving only what was asked, when,
 * by whom and how it went. Only a person (an Access login, not a service
 * token such as the board's) can ask for one. Nothing here is ever shown
 * back: the page sees which keys a change sets, the board's networks and the
 * start of its token's id.
 */

/** A change refused, with why (a 400). */
export class SettingsError extends Error {}

export const BOARD_SETTINGS_SCHEMA = [
  `CREATE TABLE IF NOT EXISTS board_settings (id INTEGER PRIMARY KEY CHECK (id = 1),
     version INTEGER NOT NULL, pending TEXT, requested TEXT, by TEXT, result TEXT, done TEXT)`,
  // The board's own Access identity (its token's client id): what checks in.
  `CREATE TABLE IF NOT EXISTS board_identity (id INTEGER PRIMARY KEY CHECK (id = 1), who TEXT)`,
];

// The board is whatever service token checks in (only the board does); a
// person's login (an email) never counts. Written when it changes, so not on
// every check-in.
const boardSeen = new WeakMap();             // per D1 binding
export async function noteBoard(env, who) {
  if (!who || who.includes("@") || boardSeen.get(env.DB) === who) return;
  await env.DB.prepare("INSERT OR REPLACE INTO board_identity (id, who) VALUES (1, ?)").bind(who).run();
  boardSeen.set(env.DB, who);
}

/** True for the board's own token: it may check in and upload its drives,
 * never publish firmware (an extracted token mustn't be able to put code on
 * the board). */
export async function isBoard(env, who) {
  if (!who || who.includes("@")) return false;
  const r = (await env.DB.prepare("SELECT who FROM board_identity WHERE id = 1").all()).results;
  return !!(r && r.length && r[0].who === who);
}

// The board's keys (settings.h SETTING_FIELDS), with its rules for a value.
const PASS = (v) => v === "" || (v.length >= 8 && v.length <= 63);
export const FIELDS = {
  wifi_ssid: (v) => v.length >= 1 && v.length <= 32,
  wifi_pass: PASS,
  wifi2_ssid: (v) => v.length <= 32,
  wifi2_pass: PASS,
  ap_pass: PASS,
  access_id: (v) => v.length > 7 && v.length <= 100 && v.endsWith(".access"),
  access_secret: (v) => v.length >= 16 && v.length <= 128,
  tz: (v) => v.length >= 1 && v.length <= 60 && !v.includes('"'),
};
const PRINTABLE = /^[\x20-\x7e]*$/;
const PEOPLE_ONLY = "only a person can change the board's settings";

const hex = (s) => [...new TextEncoder().encode(s)].map((b) => b.toString(16).padStart(2, "0"))
  .join("");

async function row(env) {
  const r = (await env.DB.prepare("SELECT * FROM board_settings WHERE id = 1").all()).results;
  return r && r.length ? r[0] : null;
}

async function lastBeat(env) {
  const r = (await env.DB.prepare(
    "SELECT received, data FROM heartbeats ORDER BY received DESC, rowid DESC LIMIT 1").all())
    .results;
  if (!r || !r.length) return null;
  const b = JSON.parse(r[0].data);
  return { received: r[0].received, fw: b.fw, settings_version: b.settings_version ?? null,
    networks: b.networks ?? null, token_id: b.token_id ?? null,
    settings_failed: b.settings_failed ?? null, settings_error: b.settings_error ?? null };
}

/** What the page shows: the board's side (from its last check-in) and the
 * change waiting or the last one's outcome, never a value. */
export async function getBoardSettings(env) {
  const r = await row(env);
  return {
    board: await lastBeat(env),
    version: r ? r.version : 0,
    pending: r && r.pending ? { keys: Object.keys(JSON.parse(r.pending)), requested: r.requested,
      by: r.by } : null,
    last: r && r.done ? { result: r.result, done: r.done, by: r.by } : null,
  };
}

/** A change from the page: {key: value} for the keys it sets ("" clears a
 * second network or a password). Added to one still waiting. */
export async function putBoardSettings(env, body, who, now = new Date()) {
  if (!who || !who.includes("@")) throw new SettingsError(PEOPLE_ONLY);
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    throw new SettingsError("body: {key: value}");
  }
  const change = {};
  for (const [k, v] of Object.entries(body)) {
    if (!(k in FIELDS)) throw new SettingsError(`unknown setting ${k}`);
    if (typeof v !== "string" || !PRINTABLE.test(v) || !FIELDS[k](v)) {
      throw new SettingsError(`not a usable ${k}`);
    }
    change[k] = v;
  }
  if (!Object.keys(change).length) throw new SettingsError("nothing to change");
  if (("access_id" in change) !== ("access_secret" in change)) {
    throw new SettingsError("a new token needs both access_id and access_secret");
  }
  const r = await row(env);
  const beat = await lastBeat(env);
  const pending = r && r.pending ? { ...JSON.parse(r.pending), ...change } : change;
  // Past the board's, and past one it refused (it ignores that number again
  // until it restarts).
  const version = Math.max(r ? r.version : 0, (beat && beat.settings_version) || 0,
    (beat && beat.settings_failed) || 0) + 1;
  await env.DB.prepare(
    `INSERT OR REPLACE INTO board_settings (id, version, pending, requested, by, result, done)
     VALUES (1, ?, ?, ?, ?, NULL, NULL)`)
    .bind(version, JSON.stringify(pending), now.toISOString(), who).run();
  return getBoardSettings(env);
}

/** Take back a change the board hasn't had yet. */
export async function cancelBoardSettings(env, who, now = new Date()) {
  if (!who || !who.includes("@")) throw new SettingsError(PEOPLE_ONLY);
  const r = await row(env);
  if (r && r.pending) {
    await env.DB.prepare(`UPDATE board_settings SET pending = NULL, result = 'cancelled',
      done = ?, by = ? WHERE id = 1 AND version = ?`).bind(now.toISOString(), who, r.version).run();
  }
  return getBoardSettings(env);
}

/** For a check-in's reply: the change waiting, if the board hasn't it yet;
 * done with it (dropped) once the board says it has it or refused it. */
export async function beatSettings(env, beat, now = new Date()) {
  const r = await row(env);
  if (!r || !r.pending) return {};
  const has = Number(beat.settings_version) || 0;
  const failed = Number(beat.settings_failed) || 0;
  if (has >= r.version || failed >= r.version) {
    const result = has >= r.version ? "taken"
      : `refused: ${String(beat.settings_error || "").slice(0, 200)}`;
    // Only the change this check-in answered: one saved meanwhile stays.
    await env.DB.prepare(`UPDATE board_settings SET pending = NULL, result = ?, done = ?
      WHERE id = 1 AND version = ?`).bind(result, now.toISOString(), r.version).run();
    return {};
  }
  if (beat.settings_version === undefined) return {};   // a firmware from before settings
  const out = { settings_version: r.version };
  for (const [k, v] of Object.entries(JSON.parse(r.pending))) out[`set_${k}`] = hex(v);
  return out;
}
