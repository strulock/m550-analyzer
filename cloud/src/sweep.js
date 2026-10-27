/**
 * The logger's full channel check, "Full channel sweep" on the logger page:
 * every identifier in the catalog asked once by the board, as the app's
 * Answers > Check car now does, whether the car answered it before or not
 * (firmware/lib/logger/carcheck.h has the board's side).
 *
 * - The page asks for one (POST /api/logger/sweep); it waits in
 *   logger_settings ("sweep") with an id until the board has done it, and
 *   each check-in's reply carries {sweep: id} meanwhile. DELETE cancels.
 * - The board fetches the list (GET /api/logger/check-list: each identifier
 *   with the reply size its layout needs) and asks it from a minute into
 *   its next drive, beside the fast reads, carrying on in the next drive if
 *   that one ends first. Its check-ins say how far it got (sweep_next,
 *   sweep_of) and which modules it skipped for not answering
 *   (sweep_silent); sweep_done: id once all are asked.
 * - Its answers come up with the drive as logger/<drive>.check.csv, the
 *   app's check CSV: merged into the catalog's answers here ("ok" stays ok,
 *   as the app's sends do), so the channel file picks up whatever answers
 *   now, and the app reads them too (cloud.check_files).
 * - The finished one moves to "sweep_last" with its counts, for the page.
 */

import { CATALOG_KEY, getCatalog } from "./logger.js";

export const CHECK = ".check.csv";            // a logger drive's check answers
export const STATUSES = ["ok", "size", "refused", "no answer"];
const WIDTH = { u8: 1, s8: 1, u16: 2, s16: 2, u32: 4, s32: 4, f32: 4, gps: 5 };
const JOB_RE = /^logger sweep ([\w.-]{1,40})$/;
const MAX_FILES = 200;                        // check files remembered per request

const hex = (n, w) => "0x" + n.toString(16).toUpperCase().padStart(w, "0");

/** Each catalog identifier once, sorted by module then identifier, as
 * [{module, did, size, exact, key}]: `size` the reply's data its layout
 * needs (exact when the catalog gives the identifier's size, otherwise at
 * least the furthest field's end), `key` the catalog's answers key. */
export function identifiers(catalog) {
  const col = Object.fromEntries(catalog.columns.map((c, i) => [c, i]));
  const by = new Map();
  for (const r of catalog.rows) {
    const module = parseInt(r[col.module], 16), did = parseInt(r[col.did], 16);
    if (!(module >= 0 && module <= 0xFF && did >= 0 && did <= 0xFFFF)) continue;
    const id = module * 0x10000 + did;
    const x = by.get(id) || { module, did, size: 0, exact: false, need: 0,
      key: `${r[col.module]}:${r[col.did]}` };
    const fixed = Number(r[col.did_size]);
    if (Number.isInteger(fixed) && fixed > 0) {
      x.size = fixed;
      x.exact = true;
    }
    const end = Number(r[col.offset]) + (WIDTH[r[col.type]] || 0);
    if (Number.isFinite(end) && end > x.need) x.need = end;
    by.set(id, x);
  }
  return [...by.keys()].sort((a, b) => a - b).map((id) => {
    const x = by.get(id);
    return { module: x.module, did: x.did, size: x.exact ? x.size : x.need, exact: x.exact,
      key: x.key };
  });
}

/** The board's list (carcheck.h parseCheckList): "module,did,size,exact". */
export function checkList(catalog) {
  const lines = identifiers(catalog).map((x) =>
    `${hex(x.module, 2)},${hex(x.did, 4)},${x.size},${x.exact ? 1 : 0}`);
  return ["module,did,size,exact", ...lines].join("\n") + "\n";
}

async function setting(env, key) {
  const r = (await env.DB.prepare("SELECT value FROM logger_settings WHERE key = ?").bind(key)
    .all()).results;
  return r && r.length && r[0].value ? JSON.parse(r[0].value) : null;
}

function put(env, key, value) {
  return value === null
    ? env.DB.prepare("DELETE FROM logger_settings WHERE key = ?").bind(key)
    : env.DB.prepare("INSERT OR REPLACE INTO logger_settings (key, value) VALUES (?, ?)")
      .bind(key, JSON.stringify(value));
}

/** {pending, last}: the request waiting for the board, the last one done. */
export async function getSweep(env) {
  const [pending, last] = await Promise.all([setting(env, "sweep"), setting(env, "sweep_last")]);
  return { pending, last };
}

/** Ask for one on the next drive (the one waiting already, if there is). */
export async function requestSweep(env, now = new Date()) {
  const have = await setting(env, "sweep");
  if (have) return { pending: have, created: false };
  const id = "s" + now.toISOString().slice(0, 19).replace(/[-:]/g, "").replace("T", "-");
  const pending = { id, requested: now.toISOString(), next: 0, of: null, silent: "",
    counts: {}, files: [] };
  await put(env, "sweep", pending).run();
  return { pending, created: true };
}

/** Cancel the waiting one; the board stops at its next check-in. */
export async function cancelSweep(env) {
  const have = await setting(env, "sweep");
  if (have) await put(env, "sweep", null).run();
  return { cancelled: have ? have.id : null };
}

/** A check-in: its progress on the waiting request, or that it finished.
 * {sweep: the id the board should be doing, or null} for the reply. */
export async function beatSweep(env, body, now = new Date()) {
  const pending = await setting(env, "sweep");
  if (!pending) return { sweep: null };
  const silent = typeof body.sweep_silent === "string" ? body.sweep_silent.slice(0, 200) : null;
  if (body.sweep_done === pending.id) {
    const last = { ...pending, done: now.toISOString(), silent: silent ?? pending.silent,
      next: pending.of ?? pending.next };
    await env.DB.batch([put(env, "sweep_last", last), put(env, "sweep", null)]);
    return { sweep: null };
  }
  if (body.sweep === pending.id) {
    const next = Number.isInteger(body.sweep_next) ? body.sweep_next : pending.next;
    const of = Number.isInteger(body.sweep_of) ? body.sweep_of : pending.of;
    const changed = next !== pending.next || of !== pending.of ||
      (silent !== null && silent !== pending.silent) || !pending.seen;
    if (changed) {
      await put(env, "sweep", { ...pending, next, of, silent: silent ?? pending.silent,
        seen: pending.seen || now.toISOString() }).run();
    }
  }
  return { sweep: pending.id };
}

/** Rows of the app's check CSV: [{module, did, job, status}]. Its fields
 * have no commas (the board's details don't). */
export function checkRows(text) {
  const lines = text.split(/\r?\n/).filter((l) => l.trim());
  if (!lines.length) return [];
  const head = lines[0].split(",").map((h) => h.trim());
  const at = Object.fromEntries(head.map((h, i) => [h, i]));
  if (at.module === undefined || at.did === undefined || at.status === undefined) {
    throw new Error("not a check CSV");
  }
  return lines.slice(1).map((l) => l.split(",")).map((c) => ({
    module: parseInt(c[at.module], 16), did: parseInt(c[at.did], 16),
    job: at.job === undefined ? "" : (c[at.job] || "").trim(),
    status: (c[at.status] || "").trim(),
  })).filter((r) => Number.isInteger(r.module) && Number.isInteger(r.did) &&
    STATUSES.includes(r.status));
}

/** A logger drive's check answers just stored: into the catalog's answers,
 * and counted on the request they're for. */
export async function ingestCheck(env, key, text) {
  const rows = checkRows(text);
  const catalog = await getCatalog(env);
  if (!catalog) return { processed: false, reason: "no catalog yet" };
  const keys = new Map(identifiers(catalog).map((x) => [x.module * 0x10000 + x.did, x.key]));
  const answers = { ...catalog.answers };
  const counts = {};
  let changed = 0, newlyOk = 0, id = null;
  for (const r of rows) {
    const k = keys.get(r.module * 0x10000 + r.did);
    if (!k) continue;
    counts[r.status] = (counts[r.status] || 0) + 1;
    const m = JOB_RE.exec(r.job);
    if (m) id = m[1];
    if (answers[k] === "ok" || answers[k] === r.status) continue;
    if (r.status === "ok") newlyOk++;
    answers[k] = r.status;
    changed++;
  }
  if (changed) {
    await env.FILES.put(CATALOG_KEY, new TextEncoder().encode(JSON.stringify({ ...catalog,
      answers })), { httpMetadata: { contentType: "application/json" } });
  }
  // The request's counts, once per file (an upload sent again after a lost
  // reply counts once).
  for (const which of ["sweep", "sweep_last"]) {
    const req = id && await setting(env, which);
    if (!req || req.id !== id) continue;
    const files = req.files || [];
    if (files.includes(key)) break;
    const sum = { ...req.counts };
    for (const [s, n] of Object.entries(counts)) sum[s] = (sum[s] || 0) + n;
    await put(env, which, { ...req, counts: sum, newly_ok: (req.newly_ok || 0) + newlyOk,
      files: [...files, key].slice(-MAX_FILES) }).run();
    break;
  }
  return { processed: true, identifiers: rows.length, changed, newly_ok: newlyOk, sweep: id };
}
