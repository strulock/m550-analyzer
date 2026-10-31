/**
 * The newest stored value of every channel (GET /api/logger/values), for the
 * logger page's channel list, and what the page flags as odd (oddValue: its
 * source is embedded in the page, as estimateRates' is).
 *
 * channel_values keeps a row a channel: when its newest value was recorded,
 * that value, its count, min and max over every stored drive, and whether
 * it only ever goes up (`rises`: an odometer, a counter). A drive's
 * channels are brought up to date when its series are stored; the whole
 * table is built the first time it's asked for and found empty.
 */

export const VALUES_SCHEMA = [
  `CREATE TABLE IF NOT EXISTS channel_values (channel TEXT PRIMARY KEY, t REAL, value REAL,
     n INTEGER, lo REAL, hi REAL, drives INTEGER, rises INTEGER)`,
];

// Which rule the rows were worked out by (logger_settings "values_rule"):
// raised when `refresh` changes, so the table is built again under it.
const RULE = "2";

/** The table from before `rises`, or from an older rule, goes (it's all
 * derived; getValues builds it again). */
export async function migrateValues(env) {
  const cols = await env.DB.prepare("SELECT name FROM pragma_table_info('channel_values')").all();
  const rule = (await env.DB.prepare(
    "SELECT value FROM logger_settings WHERE key = 'values_rule'").all()).results;
  if ((cols.results || []).some((c) => c.name === "rises") && rule && rule.length &&
      rule[0].value === RULE) return;
  await env.DB.batch([env.DB.prepare("DROP TABLE IF EXISTS channel_values"),
    env.DB.prepare(VALUES_SCHEMA[0]),
    env.DB.prepare("INSERT OR REPLACE INTO logger_settings (key, value) VALUES ('values_rule', ?)")
      .bind(RULE)]);
}

// The newest value is the last point of the channel's newest piece. A
// channel rises if it's in three drives or more (two that happen to read
// higher prove nothing), went up, each piece (in time order) starts at its
// lowest, ends at its highest and starts no lower than the one before
// ended, and no point in a piece is below the one before it (1, 5, 2, 5
// starts lowest and ends highest, but isn't a counter). That last walks
// every point, so only for a channel that passed the rest (CASE: SQLite
// doesn't evaluate the branch not taken).
//
// Each piece's first and last value and whether it ever falls are kept with
// it (series v0, v1, falls; pieceStats), so this reads no JSON: it parsed
// every stored piece of each channel on every upload, 110 MB of it by 31
// Oct. A piece stored before those columns (falls NULL until
// backfillPieces fills them) is read from its JSON as before.
const V0 = "COALESCE(s.v0, json_extract(s.data, '$[0][1]'))";
const V1 = (s) => `COALESCE(${s}.v1, json_extract(${s}.data, '$[#-1][1]'))`;
const WALK = (x) => `EXISTS (SELECT 1 FROM (SELECT json_extract(j.value, '$[1]') -
  LAG(json_extract(j.value, '$[1]')) OVER (ORDER BY j.key) AS d FROM json_each(${x}.data) j)
  WHERE d < 0)`;
const refresh = (where) => `WITH p AS (SELECT s.channel, s.drive, s.t1, s.n, s.min, s.max,
    ${V0} AS a, ${V1("s")} AS b,
    LAG(${V1("s")}) OVER (PARTITION BY s.channel ORDER BY s.t0) AS prev
    FROM series s WHERE ${where})
  INSERT OR REPLACE INTO channel_values (channel, t, value, n, lo, hi, drives, rises)
  SELECT p.channel, MAX(p.t1), (SELECT ${V1("x")} FROM series x
    WHERE x.channel = p.channel ORDER BY x.t1 DESC LIMIT 1), SUM(p.n), MIN(p.min), MAX(p.max),
    COUNT(DISTINCT p.drive), CASE WHEN COUNT(DISTINCT p.drive) > 2 AND MAX(p.max) > MIN(p.min)
    AND COALESCE(MIN(p.a <= p.min AND p.b >= p.max AND (p.prev IS NULL OR p.a >= p.prev)), 0)
    THEN NOT EXISTS (SELECT 1 FROM series x WHERE x.channel = p.channel AND
      (x.falls = 1 OR (x.falls IS NULL AND ${WALK("x")})))
    ELSE 0 END
  FROM p GROUP BY p.channel`;

/** A piece's first and last value and whether any value is below the one
 * before it ([t, v] points; a gap, null, is skipped over without comparing
 * across it, as the SQL above does). */
export function pieceStats(data) {
  let v0 = null, v1 = null, falls = false, prev = null;
  for (let i = 0; i < data.length; i++) {
    const v = Array.isArray(data[i]) ? data[i][1] : null;
    if (i === 0) v0 = typeof v === "number" ? v : null;
    if (i === data.length - 1) v1 = typeof v === "number" ? v : null;
    if (typeof v === "number" && typeof prev === "number" && v < prev) falls = true;
    prev = v;
  }
  return { v0, v1, falls };
}

/** Pieces stored before v0, v1 and falls: filled in BACKFILL at a time,
 * oldest rowid first; {done, next} (send ?after=<next> until it's null). */
export const BACKFILL = 4000;
export async function backfillPieces(env, after = 0) {
  const r = (await env.DB.prepare(`SELECT rowid FROM series WHERE falls IS NULL AND rowid > ?
    ORDER BY rowid LIMIT ?`).bind(Number(after) || 0, BACKFILL).all()).results || [];
  if (!r.length) return { done: 0, next: null };
  const lo = r[0].rowid, hi = r[r.length - 1].rowid;
  await env.DB.prepare(`UPDATE series AS s SET v0 = json_extract(s.data, '$[0][1]'),
    v1 = json_extract(s.data, '$[#-1][1]'), falls = ${WALK("s")}
    WHERE s.falls IS NULL AND s.rowid BETWEEN ? AND ?`).bind(lo, hi).run();
  return { done: r.length, next: r.length === BACKFILL ? hi : null };
}

/** Bring these channels' rows up to date (a channel with no series left loses its row). */
export async function refreshValues(env, names) {
  const list = JSON.stringify([...new Set(names)]);
  await env.DB.batch([
    env.DB.prepare("DELETE FROM channel_values WHERE channel IN (SELECT value FROM json_each(?))")
      .bind(list),
    env.DB.prepare(refresh("s.channel IN (SELECT value FROM json_each(?))")).bind(list),
  ]);
}

const COLUMNS = ["t", "value", "n", "lo", "hi", "drives", "rises"];
const short = (v) => (typeof v === "number" && Number.isFinite(v) ? +v.toPrecision(9) : null);

/** {columns, values: {channel: [t (UTC seconds), value, n, lo, hi, drives, rises (0/1)]}}. */
export async function getValues(env) {
  const read = () => env.DB.prepare(
    "SELECT channel, t, value, n, lo, hi, drives, rises FROM channel_values").all();
  let rows = (await read()).results || [];
  if (!rows.length) {
    await env.DB.prepare(refresh("1")).run();
    rows = (await read()).results || [];
  }
  const values = {};
  for (const x of rows) {
    values[x.channel] = [Math.round(x.t), short(x.value), x.n, short(x.lo), short(x.hi), x.drives,
      x.rises ? 1 : 0];
  }
  return { columns: COLUMNS, values };
}

/**
 * Why a channel's value looks wrong, or null: `ch` is its catalog row
 * ({type, mul, div, add, unit}). Two tests: the raw value at its type's
 * limit (0xFF, 0xFFFF, the signed extremes: what modules send for "none"
 * or "error" when the catalog names no "no value"), and a value no car
 * could have for its unit (-273 °C, 400 V).
 *
 * Plain function, no closures over module scope: the page embeds its source.
 */
export function oddValue(v, ch) {
  if (typeof v !== "number" || !isFinite(v)) return null;
  var BITS = { u8: 8, s8: 8, u16: 16, s16: 16, u32: 32, s32: 32 };
  var bits = BITS[ch.type];
  var mul = Number(ch.mul) || 1, div = Number(ch.div) || 1, add = Number(ch.add) || 0;
  if (bits) {
    var exact = (v - add) * div / mul, raw = Math.round(exact);
    if (Math.abs(exact - raw) < 0.01) {
      var full = Math.pow(2, bits);
      if (ch.type.charAt(0) === "u" && raw === full - 1) {
        return "raw 0x" + raw.toString(16).toUpperCase() + ", usually “no value”";
      }
      if (ch.type.charAt(0) === "s" && (raw === full / 2 - 1 || raw === -full / 2)) {
        return "raw " + raw + " (the type's limit), usually “no value”";
      }
    }
  }
  var RANGE = { "°C": [-60, 1100], "V": [0, 60], "mV": [-30000, 30000], "km/h": [0, 350],
    "1/min": [-1000, 9000], "U/min": [-1000, 9000], "%": [-200, 200], "bar": [0, 400],
    "hPa": [0, 30000], "kPa": [0, 3000], "MPa": [0, 40], "km": [0, 2000000],
    "Nm": [-2000, 2000], "kg/h": [0, 2000], "A": [-1000, 1000] };
  var r = RANGE[ch.unit];
  if (r && (v < r[0] || v > r[1])) return "outside " + r[0] + " to " + r[1] + " " + ch.unit;
  return null;
}
