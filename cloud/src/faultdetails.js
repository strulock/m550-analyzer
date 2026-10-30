/**
 * When each fault set: the odometer its module stored with it, and roughly
 * the date, from the drives.
 *
 * The board reads each failed code's snapshots (UDS 0x19 04) and counters
 * (0x19 06) while the car is in P, and uploads them as
 * logger/<stamp>.details.csv: read_at, module, module_name, code, first_km,
 * last_km, occurrences, and both replies in hex (snapshots, extended).
 * fault_details keeps a row a code: the lowest first_km and highest last_km
 * any read gave, the newest count and replies. The app's own fault reads
 * carry first_km and last_km too (n63read's detail_columns); either will do.
 *
 * A module's odometer has no date, so one comes from the drives: the drive in
 * which the car's odometer passed that reading, or the two drives either
 * side of it (kmDates).
 */

export const DETAILS = ".details.csv";       // a logger power-on's fault details

export const DETAILS_SCHEMA = [
  `CREATE TABLE IF NOT EXISTS fault_details (module TEXT NOT NULL, code TEXT NOT NULL,
     read_at TEXT, data TEXT NOT NULL, PRIMARY KEY (module, code))`,
];

const km = (v) => {
  const n = v === undefined || v === null || v === "" ? NaN : Number(v);
  return Number.isFinite(n) && n >= 0 ? n : null;
};

/** The bytes of a snapshot identifier the walk can step over (the firmware's
 * standardFieldBytes): BMW's standard ones, and the gateway's and head unit's
 * control-unit address ahead of their odometer. 0: unknown. */
function fieldBytes(module, id) {
  switch (id) {
    case 0x1700: case 0x1702: case 0x1751: case 0xFFFF: return 3;
    case 0x1701: case 0x1768: return 4;
    case 0x1731: case 0x1750: return 1;
    case 0x1707: return module === 0x10 || module === 0x63 ? 1 : 0;
    case 0x1769: return module === 0x29 ? 4 : 6;
    default: return 0;
  }
}

/** The lowest and highest odometer (0x1700) in a 59 04 reply, as hex, or null.
 * A record that runs into a module's own value the walk can't size carries on
 * at the next record (its number, the same count and first identifier), as
 * the board does; so an older board's details, which missed some, still get
 * their odometer here. */
export function snapshotKm(hex, module) {
  if (typeof hex !== "string" || !/^([0-9A-Fa-f]{2})+$/.test(hex)) return null;
  const p = hex.match(/../g).map((b) => parseInt(b, 16));
  const m = typeof module === "string" ? parseInt(module, 16) : module;
  if (p.length < 6 || p[0] !== 0x59 || p[1] !== 0x04) return null;
  let lo = null, hi = null;
  let i = 6;
  walk: while (i + 2 <= p.length) {
    const number = p[i], count = p[i + 1], start = i + 2;
    i = start;
    for (let k = 0; k < count; k++) {
      if (i + 2 > p.length) break walk;
      const id = (p[i] << 8) | p[i + 1];
      const size = fieldBytes(m, id);
      if (!size || i + 2 + size > p.length) {
        let next = 0;
        for (let j = i; j + 4 <= p.length; j++) {
          if (p[j] === ((number + 1) & 0xFF) && p[j + 1] === count && p[j + 2] === p[start] &&
              p[j + 3] === p[start + 1]) { next = j; break; }
        }
        if (!next) break walk;
        i = next;
        continue walk;
      }
      if (id === 0x1700) {
        const v = (p[i + 2] << 16) | (p[i + 3] << 8) | p[i + 4];
        if (v !== 0xFFFFFF) {
          lo = lo === null ? v : Math.min(lo, v);
          hi = hi === null ? v : Math.max(hi, v);
        }
      }
      i += 2 + size;
    }
  }
  return lo === null ? null : { first_km: lo, last_km: hi };
}

/** A stored details row with its odometer from its own snapshots where the
 * board's read gave none. */
function withSnapshotKm(d) {
  if (!d || (d.first_km !== null && d.first_km !== undefined)) return d;
  const got = snapshotKm(d.snapshots, d.module);
  return got ? { ...d, ...got } : d;
}

/** One details file's rows (parsed CSV, header first) into fault_details. */
export async function ingestDetails(env, rows) {
  const head = (rows.shift() || []).map((h) => h.trim());
  const col = (name) => head.indexOf(name);
  if (col("module") < 0 || col("code") < 0 || col("first_km") < 0) {
    return { processed: false, reason: "not a fault details file" };
  }
  const get = (r, name) => (col(name) >= 0 && col(name) < r.length ? r[col(name)].trim() : "");
  const found = new Map();
  for (const r of rows) {
    const module = get(r, "module").toUpperCase().replace(/^0X/, "0x"), code = get(r, "code").toUpperCase();
    if (!/^0x[0-9A-F]{2}$/.test(module) || !/^[0-9A-F]{6}$/.test(code)) continue;
    found.set(`${module} ${code}`, withSnapshotKm({ module, code, read_at: get(r, "read_at") || null,
      first_km: km(get(r, "first_km")), last_km: km(get(r, "last_km")),
      occurrences: km(get(r, "occurrences")), snapshots: get(r, "snapshots"),
      extended: get(r, "extended") }));
  }
  if (!found.size) return { processed: true, details: 0 };
  const have = new Map((await env.DB.prepare(
    `SELECT module, code, data FROM fault_details WHERE module || ' ' || code IN
       (SELECT value FROM json_each(?))`).bind(JSON.stringify([...found.keys()])).all())
    .results.map((x) => [`${x.module} ${x.code}`, JSON.parse(x.data)]));
  const stmts = [];
  for (const [k, d] of found) {
    const old = have.get(k) || {};
    const lowest = [old.first_km, d.first_km].filter((v) => v !== null && v !== undefined);
    const highest = [old.last_km, d.last_km].filter((v) => v !== null && v !== undefined);
    // A read that got nothing (refused, no reply) keeps what an earlier one got.
    const newer = d.first_km !== null || d.occurrences !== null || !old.read_at;
    const merged = { ...(newer ? d : old),
      first_km: lowest.length ? Math.min(...lowest) : null,
      last_km: highest.length ? Math.max(...highest) : null };
    stmts.push(env.DB.prepare(
      "INSERT OR REPLACE INTO fault_details (module, code, read_at, data) VALUES (?, ?, ?, ?)")
      .bind(d.module, d.code, merged.read_at, JSON.stringify(merged)));
  }
  await env.DB.batch(stmts);
  return { processed: true, details: found.size,
    with_km: [...found.values()].filter((d) => d.first_km !== null).length };
}

// A drive's odometer that can't be right next to the drive after it: higher
// than it (the odometer never goes back; 2 km of slack for readings taken a
// moment apart), or further behind it than the car
// could go in the time between them, at this speed plus this slack.
const MAX_KMH = 250, SLACK_KM = 50;

/** Whether reading `b` can follow reading `a` (both {start, lo, hi}). */
function canFollow(a, b) {
  const hours = (Date.parse(b.start.replace(" ", "T")) - Date.parse(a.start.replace(" ", "T"))) / 3600000;
  if (a.hi > b.lo + 2) return false;
  return !Number.isFinite(hours) || b.lo - a.hi <= MAX_KMH * Math.max(0, hours) + SLACK_KM;
}

/** Readings ({start, lo, hi}, oldest first) without the ones that don't fit:
 * the longest run in which each can follow the one before, so a bad reading
 * (seen: an app log's 48,554 km six minutes before 111,610) is dropped
 * wherever it falls, first, last or between, and no single reading is taken
 * on trust. On a tie, the run ending latest. */
export function plausibleOdometers(readings) {
  const n = readings.length;
  const len = new Array(n).fill(1), prev = new Array(n).fill(-1);
  let end = -1;
  for (let i = 0; i < n; i++) {
    for (let j = 0; j < i; j++) {
      if (len[j] + 1 > len[i] && canFollow(readings[j], readings[i])) {
        len[i] = len[j] + 1;
        prev[i] = j;
      }
    }
    if (end < 0 || len[i] >= len[end]) end = i;
  }
  const kept = [];
  for (let i = end; i >= 0; i = prev[i]) kept.push(readings[i]);
  return kept.reverse();
}

/** The drives' odometer readings, oldest first: [{date, lo, hi}]. */
async function odometers(env) {
  const r = await env.DB.prepare(
    "SELECT start, data FROM drives WHERE start IS NOT NULL ORDER BY start").all();
  const out = [];
  for (const x of r.results || []) {
    const d = JSON.parse(x.data);
    const lo = km(d.odometer_start_km) ?? km(d.odometer_km) ?? km(d.odometer_end_km);
    const hi = km(d.odometer_end_km) ?? km(d.odometer_km) ?? lo;
    if (lo !== null) out.push({ start: String(x.start), lo, hi: Math.max(lo, hi) });
  }
  return plausibleOdometers(out).map((o) => ({ date: o.start.slice(0, 10), lo: o.lo, hi: o.hi }));
}

/** When the odometer read `at` km: {after, by}, dates (YYYY-MM-DD) or null.
 * The same date: during that day's drive. after null: before the drives
 * begin; by null: since the last one. */
export function kmDate(drives, at) {
  if (at === null || at === undefined || !drives.length) return null;
  const i = drives.findIndex((d) => d.hi >= at);
  if (i < 0) return { after: drives[drives.length - 1].date, by: null };
  if (drives[i].lo <= at) return { after: drives[i].date, by: drives[i].date };
  return { after: i > 0 ? drives[i - 1].date : null, by: drives[i].date };
}

/** These faults ([{module, code, ...}]) with their details where known:
 * first_km, last_km, occurrences, first_date and last_date ({after, by}). */
export async function withDetails(env, list) {
  if (!list.length) return list;
  const keys = [...new Set(list.map((f) => `${f.module} ${f.code}`))];
  const mine = new Map((await env.DB.prepare(
    `SELECT module, code, data FROM fault_details WHERE module || ' ' || code IN
       (SELECT value FROM json_each(?))`).bind(JSON.stringify(keys)).all())
    .results.map((x) => [`${x.module} ${x.code}`, withSnapshotKm(JSON.parse(x.data))]));
  // The app's reads of the same codes, where the board hasn't any.
  const theirs = new Map();
  const rows = (await env.DB.prepare(
    `SELECT module, code, data FROM faults WHERE module || ' ' || code IN
       (SELECT value FROM json_each(?))`).bind(JSON.stringify(keys)).all()).results;
  for (const x of rows || []) {
    const f = JSON.parse(x.data);
    const k = `${x.module} ${x.code}`;
    const a = km(f.first_km), b = km(f.last_km);
    if (a === null && b === null) continue;
    const t = theirs.get(k) || { first_km: null, last_km: null, occurrences: null };
    if (a !== null && (t.first_km === null || a < t.first_km)) t.first_km = a;
    if (b !== null && (t.last_km === null || b > t.last_km)) t.last_km = b;
    if (km(f.occurrences) !== null) t.occurrences = km(f.occurrences);
    theirs.set(k, t);
  }
  const drives = mine.size || theirs.size ? await odometers(env) : [];
  return list.map((f) => {
    const k = `${f.module} ${f.code}`, d = mine.get(k), t = theirs.get(k);
    if (!d && !t) return f;
    const pick = (name) => (d && d[name] !== null && d[name] !== undefined ? d[name]
      : t ? t[name] : null);
    const lows = [d && d.first_km, t && t.first_km].filter((v) => v !== null && v !== undefined);
    const highs = [d && d.last_km, t && t.last_km].filter((v) => v !== null && v !== undefined);
    const first = lows.length ? Math.min(...lows) : null;
    const last = highs.length ? Math.max(...highs) : null;
    return { ...f, first_km: first, last_km: last, occurrences: pick("occurrences"),
      first_date: kmDate(drives, first), last_date: kmDate(drives, last),
      details_read: d ? d.read_at : null };
  });
}
