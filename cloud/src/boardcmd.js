/**
 * Commands for the logger board from the Board tab (and MCP): list a folder
 * on its card, delete a file or folder, format the card, upload its waiting
 * files now, check for (and install) new firmware now, or restart.
 *
 * A command waits here until the board's next check-in. Every reply carries
 * the oldest one it hasn't done ("cmd_id", "cmd" and "cmd_arg", the argument
 * in hex: a card path may hold anything JSON would escape). The board says
 * in its check-ins the last one it finished and how (cmd_done, cmd_ok,
 * cmd_result: firmware/lib/logger/command.h); a folder's listing comes
 * separately (POST /api/logger/card). It keeps the last one done in its
 * flash, so a command never runs twice, and it does the card's work only
 * between drives. Only a person (an Access login, not a service token) can
 * ask for one.
 */

/** A command refused, with why (a 400). */
export class CommandError extends Error {}

export const BOARD_COMMANDS_SCHEMA = [
  `CREATE TABLE IF NOT EXISTS board_commands (id INTEGER PRIMARY KEY AUTOINCREMENT,
     kind TEXT NOT NULL, arg TEXT NOT NULL DEFAULT '', requested TEXT NOT NULL, by TEXT,
     sent TEXT, done TEXT, ok INTEGER, result TEXT, data TEXT)`,
];

/** What each does, for the page and MCP. `card`: waits for the board to be
 * between drives. */
export const KINDS = {
  list: { arg: "path", about: "list a folder on the card" },
  delete: { arg: "path", card: true, about: "delete a file or folder (and all in it) from the card" },
  format: { arg: "discard", card: true,
    about: "format the card (FAT); refused while files wait to upload, unless discard" },
  upload: { about: "upload the card's waiting files now" },
  update: { about: "check for new firmware now and install it as soon as the board isn't logging" },
  restart: { card: true, about: "restart the board (between drives)" },
};

const KEEP = 50;                             // commands kept, newest
const MAX_LISTING = 200;                     // entries the board sends
export const MAX_CARD_BODY = 65536;
const PEOPLE_ONLY = "only a person can send the board a command";

/** A path on the card: absolute, printable, no "..", at most 200 bytes. */
export function cardPath(p) {
  return typeof p === "string" && p.startsWith("/") && p.length <= 200 &&
    /^[\x20-\x7e]*$/.test(p) && !p.split("/").includes("..") && !p.includes("\\") &&
    !p.includes("//") && (p === "/" || !p.endsWith("/"));
}

const hex = (s) => [...new TextEncoder().encode(s)].map((b) => b.toString(16).padStart(2, "0"))
  .join("");

function shape(r) {
  const c = { id: r.id, kind: r.kind, arg: r.arg, requested: r.requested, by: r.by,
    sent: r.sent, done: r.done, ok: r.ok === null ? null : !!r.ok, result: r.result };
  c.state = r.done ? (r.result === "cancelled" ? "cancelled" : r.ok ? "done" : "failed")
    : r.sent ? "sent" : "waiting";
  if (r.data) c.listing = JSON.parse(r.data);
  return c;
}

/** The newest commands first, each with its listing if it has one. */
export async function listCommands(env, limit = 20) {
  const n = Math.max(1, Math.min(KEEP, Number(limit) || 20));
  const r = (await env.DB.prepare("SELECT * FROM board_commands ORDER BY id DESC LIMIT ?")
    .bind(n).all()).results || [];
  return r.map(shape);
}

export async function getCommand(env, id) {
  const r = (await env.DB.prepare("SELECT * FROM board_commands WHERE id = ?").bind(id).all())
    .results;
  return r && r.length ? shape(r[0]) : null;
}

/** A command from the page: {kind, path} or {kind: "format", discard}. */
export async function putCommand(env, body, who, now = new Date()) {
  if (!who || !who.includes("@")) throw new CommandError(PEOPLE_ONLY);
  if (!body || typeof body !== "object" || !(body.kind in KINDS)) {
    throw new CommandError(`kind: one of ${Object.keys(KINDS).join(", ")}`);
  }
  const kind = body.kind;
  let arg = "";
  if (KINDS[kind].arg === "path") {
    arg = body.path === undefined && kind === "list" ? "/" : body.path;
    if (!cardPath(arg)) throw new CommandError("path: a path on the card, like /drives");
    if (kind === "delete" && arg === "/") throw new CommandError("the whole card is format");
  } else if (kind === "format") {
    arg = body.discard ? "discard" : "";
  }
  // The same thing asked again while it waits is the one already waiting.
  const same = (await env.DB.prepare(
    "SELECT id FROM board_commands WHERE done IS NULL AND kind = ? AND arg = ?")
    .bind(kind, arg).all()).results;
  if (same && same.length) return getCommand(env, same[0].id);
  const r = await env.DB.prepare(
    "INSERT INTO board_commands (kind, arg, requested, by) VALUES (?, ?, ?, ?) RETURNING id")
    .bind(kind, arg, now.toISOString(), who).all();
  await env.DB.prepare(`DELETE FROM board_commands WHERE id <=
    (SELECT id FROM board_commands ORDER BY id DESC LIMIT 1 OFFSET ?)`).bind(KEEP).run();
  return getCommand(env, r.results[0].id);
}

/** Take back a command the board hasn't said it's done. The board drops
 * one it holds once a check-in's reply no longer carries it, unless it has
 * started it; then what it did replaces "cancelled" (beatCommand). */
export async function cancelCommand(env, id, who, now = new Date()) {
  if (!who || !who.includes("@")) throw new CommandError(PEOPLE_ONLY);
  const c = await getCommand(env, Number(id));
  if (!c) throw new CommandError("no such command");
  if (c.done) throw new CommandError(`already ${c.state}`);
  await env.DB.prepare(`UPDATE board_commands SET done = ?, ok = 0, result = 'cancelled'
    WHERE id = ? AND done IS NULL`).bind(now.toISOString(), c.id).run();
  return getCommand(env, c.id);
}

/** For a check-in's reply. The board says the last one it finished
 * (cmd_done) and how; that one is done with, any older one it skipped
 * (the board never saw it) too, and the next waiting one goes in the reply.
 * Only the board's token gets one, and only a firmware that knows commands
 * (its check-ins carry cmd_done). */
export async function beatCommand(env, beat, role, now = new Date()) {
  if (role !== "board" || beat.cmd_done === undefined || beat.cmd_done === null) return {};
  const done = Number(beat.cmd_done) || 0;
  const at = now.toISOString();
  if (done) {
    const result = String(beat.cmd_result ?? "").slice(0, 300);
    await env.DB.batch([
      // One cancelled after the board had started it: what the board did.
      env.DB.prepare(`UPDATE board_commands SET done = ?, ok = ?, result = ?
        WHERE id = ? AND (done IS NULL OR result = 'cancelled')`).bind(at, beat.cmd_ok ? 1 : 0,
        result || "done; the board didn't say how", done),
      env.DB.prepare(`UPDATE board_commands SET done = ?, ok = 0,
        result = 'not done: the board went past it' WHERE id < ? AND done IS NULL`).bind(at, done),
    ]);
  }
  const next = (await env.DB.prepare(
    "SELECT id, kind, arg, sent FROM board_commands WHERE done IS NULL AND id > ? ORDER BY id LIMIT 1")
    .bind(done).all()).results;
  if (!next || !next.length) return {};
  const c = next[0];
  if (!c.sent) {
    await env.DB.prepare("UPDATE board_commands SET sent = ? WHERE id = ?").bind(at, c.id).run();
  }
  return { cmd_id: c.id, cmd: c.kind, cmd_arg: hex(c.arg) };
}

/** A folder's listing from the board: {id, path, entries: [[name, size,
 * dir]], count, card: {type, size_mb, used_mb}}. Kept with its command. */
export async function putListing(env, body) {
  if (!body || typeof body !== "object" || !Number.isInteger(body.id) ||
    !Array.isArray(body.entries) || body.entries.length > MAX_LISTING) {
    throw new CommandError(`body: JSON {id, path, entries: [[name, size, dir]]}, ` +
      `at most ${MAX_LISTING}`);
  }
  const entries = [];
  for (const e of body.entries) {
    if (!Array.isArray(e) || typeof e[0] !== "string") throw new CommandError("entries: [name, size, dir]");
    entries.push([e[0].slice(0, 255), Number(e[1]) || 0, !!e[2]]);
  }
  entries.sort((a, b) => (b[2] - a[2]) || a[0].localeCompare(b[0]));
  const card = body.card && typeof body.card === "object" ? {
    type: String(body.card.type ?? "").slice(0, 20),
    size_mb: Number(body.card.size_mb) || null, used_mb: Number(body.card.used_mb) || null,
  } : null;
  const listing = { path: String(body.path ?? "").slice(0, 200), entries,
    count: Number.isInteger(body.count) ? body.count : entries.length, card };
  const c = await getCommand(env, body.id);
  if (!c || c.kind !== "list") throw new CommandError("no such listing asked for");
  await env.DB.prepare("UPDATE board_commands SET data = ? WHERE id = ?")
    .bind(JSON.stringify(listing), body.id).run();
  return { kept: entries.length };
}
