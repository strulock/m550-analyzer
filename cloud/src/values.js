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

/** The table from before `rises` goes (it's all derived; getValues builds it again). */
export async function migrateValues(env) {
  const cols = await env.DB.prepare("SELECT name FROM pragma_table_info('channel_values')").all();
  if ((cols.results || []).some((c) => c.name === "rises")) return;
  await env.DB.batch([env.DB.prepare("DROP TABLE IF EXISTS channel_values"),
    env.DB.prepare(VALUES_SCHEMA[0])]);
}

// The newest value is the last point of the channel's newest piece. A
// channel rises if it's in two drives or more, went up, and each piece (in
// time order) starts at its lowest, ends at its highest, and starts no lower
// than the one before ended.
const refresh = (where) => `WITH p AS (SELECT s.channel, s.drive, s.t1, s.n, s.min, s.max,
    json_extract(s.data, '$[0][1]') AS a, json_extract(s.data, '$[#-1][1]') AS b,
    LAG(json_extract(s.data, '$[#-1][1]')) OVER (PARTITION BY s.channel ORDER BY s.t0) AS prev
    FROM series s WHERE ${where})
  INSERT OR REPLACE INTO channel_values (channel, t, value, n, lo, hi, drives, rises)
  SELECT p.channel, MAX(p.t1), (SELECT json_extract(x.data, '$[#-1][1]') FROM series x
    WHERE x.channel = p.channel ORDER BY x.t1 DESC LIMIT 1), SUM(p.n), MIN(p.min), MAX(p.max),
    COUNT(DISTINCT p.drive), COUNT(DISTINCT p.drive) > 1 AND MAX(p.max) > MIN(p.min) AND
    COALESCE(MIN(p.a <= p.min AND p.b >= p.max AND (p.prev IS NULL OR p.a >= p.prev)), 0)
  FROM p GROUP BY p.channel`;

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
