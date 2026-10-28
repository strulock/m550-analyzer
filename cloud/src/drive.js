/**
 * A drive log's time series and summary, worked out in the Worker for logs
 * the logger board uploads (the PC app sends its own, from Python).
 *
 * A port of n63read's series.build / series.chunks and history.summarize
 * (with playback.read_log, history._first_clock and _event_odometer). Keep
 * the two in step: cloud/test/parity holds logs and what Python makes of
 * them, and both test suites check against it.
 *
 * Written for a Worker's 128 MB: one pass over the uploaded bytes into a
 * typed array per column, and each stored piece serialized straight to JSON.
 * A row-per-object parse of an hour's log needs several times that.
 */

export const LOG_TIME = "Time", LOG_CLOCK = "Clock", LOG_EVENT = "Event";
const MAX_NOTES = 10000;                      // a log's notes kept (readColumns)
const LOG_META = [LOG_TIME, LOG_CLOCK, LOG_EVENT];
export const BANK2 = "@0x13";
export const CHUNK = 5000;
export const MIN_ROWS = 5;
const MAX_STEP = 5.0;
const CYLINDERS = [1, 2, 3, 4, 5, 6, 7, 8];

const LAT = "HU_STATUS_GPS_POSITION.LATITUDE";
const LON = "HU_STATUS_GPS_POSITION.LONGITUDE";
const SPEEDS = ["FZG_GESCHWINDIGKEIT_VA_HA_FASCHWERP", "IVKMH", "HU_STATUS_SPEED.GPS_SPEED"];
const ODOMETERS = ["LMV_DATEN_VERTEILERGETRIEBE.MILE_KM", "BDC_ZUSATZINFO_LICHT.KM_STAND_1"];
const FUEL_FLOWS = ["MASSENSTROM_KRAFTSTOFF", "MASSENSTROM_KRAFTSTOFF" + BANK2];
const FUEL_TANK = "IKTFS";
const FUEL_DENSITY = 0.745;
const MIN_ECONOMY_KM = 1.0;
const MPG_US = 235.215;
const TANK_WINDOW = 60.0;
const ODOMETER_RE = /\bodometer (\d+) km\b/;

function perCylinder(bank1, bank2) {
  const out = {};
  for (const n of CYLINDERS) {
    out[n] = n <= 4 ? [bank1.replace("%d", n)] : [bank2.replace("%d", n - 4) + BANK2];
  }
  return out;
}

// [key, how, channels to try in order], as history.METRICS.
export const METRICS = [
  ["rpm_max", "max", ["INMOT", "MITTL_MOTORDREHZAHL_PRO_ZYLSEGM"]],
  ["speed_max", "max", SPEEDS],
  ["boost_max", "max", ["IPLAD", "S_LADEDRUCK"]],
  ["coolant_max", "max", ["ITKUM", "ITKUA", "S_TEMP_KUEHLMITTEL"]],
  ["oil_temp_max", "max", ["ITOEL"]],
  ["oil_pressure_min", "min", ["OELDRUCK"]],
  ["gearbox_oil_max", "max", ["EGS_GETRIEBEOELTMEPERATUR"]],
  ["intake_max", "max", ["RTANS"]],
  ["battery_min", "min", ["IUIBS"]],
  ["battery_max", "max", ["IUIBS"]],
  ["knock_b1", "mean", ["MITTELW_ZYLIND_ZWSPAET_KR_HALBQUANT"]],
  ["knock_b2", "mean", ["MITTELW_ZYLIND_ZWSPAET_KR_HALBQUANT" + BANK2]],
  ["knock_b1_worst", "min", ["MITTELW_ZYLIND_ZWSPAET_KR_HALBQUANT"]],
  ["knock_b2_worst", "min", ["MITTELW_ZYLIND_ZWSPAET_KR_HALBQUANT" + BANK2]],
  ["trim_b1", "pct", ["IMUL1"]],
  ["trim_b2", "pct", ["IMUL1" + BANK2]],
  ["lambda_b1", "mean", ["ILAG1"]],
  ["lambda_b2", "mean", ["ILAG1" + BANK2, "ILAG2"]],
];
for (const n of CYLINDERS) {
  METRICS.push([`misfires_${n}`, "increase",
    [`ZAEHLER_AUSSETZERK_ZYL${n}`].concat(n > 4 ? [`ZAEHLER_AUSSETZERK_ZYL${n}${BANK2}`] : [])]);
}
for (const n of CYLINDERS) {
  METRICS.push([`uneven_${n}`, "sd", [`ILUZ${n}`].concat(n > 4 ? [`ILUZ${n}${BANK2}`] : [])]);
}
const SPARK = perCylinder("DIAG_BRENNDAUER_ZUENDFUNKE_CYL%d", "DIAG_BRENNDAUER_ZUENDFUNKE_CYL%d");
for (const n of CYLINDERS) METRICS.push([`spark_${n}`, "mean", SPARK[n]]);
const KNOCK = perCylinder("IUKZ%d", "IUKZ%d");
for (const n of CYLINDERS) METRICS.push([`knock_level_${n}`, "mean", KNOCK[n]]);
for (const n of [1, 2, 3, 4]) {
  const block = `DSC_RDC_MESSDATENBLOCK_${n}.`;
  METRICS.push([`tire_${n}`, "mean", [block + "LETZTER_REIFENDRUCKWERT"]]);
  METRICS.push([`tire_temp_${n}`, "max", [block + "LETZTER_REIFENTEMPERATURWERT"]]);
}
const SCALE = { battery_min: 0.001, battery_max: 0.001 };

export const DRIVE_FIELDS = ["file", "start", "minutes", "rows", "km", "fuel_l", "l_per_100km",
  "mpg", "tank_used_l", "odometer_km", "gps_points", "misfires"].concat(METRICS.map((m) => m[0]));

/** Python's round(x, n): ties to even on the double's exact value. */
export function pyRound(x, n = 0) {
  if (!Number.isFinite(x)) return x;
  const scaled = Math.abs(x) * 10 ** n;
  // Only a value within a few ulps of a half can be a tie; the rest round
  // as toFixed does.
  if (Math.abs(scaled - Math.floor(scaled) - 0.5) > 16 * Number.EPSILON * Math.max(1, scaled)) {
    return Number(x.toFixed(n));
  }
  const exact = Math.abs(x).toFixed(100);
  const [whole, frac] = exact.split(".");
  if (frac[n] === "5" && /^0*$/.test(frac.slice(n + 1))) {
    const last = n ? frac[n - 1] : whole[whole.length - 1];
    const down = Number(`${whole}.${frac.slice(0, n)}`);
    const r = Number(last) % 2 ? down + 10 ** -n : down;
    return x < 0 ? -Number(r.toFixed(n)) : Number(r.toFixed(n));
  }
  return Number(x.toFixed(n));
}

const FLOAT = /^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/;
const SPECIAL = /^[+-]?(inf|infinity|nan)$/i;

/** Python's float(text) (text already trimmed), or null if it would raise. */
function pyFloat(t) {
  if (FLOAT.test(t)) return Number(t);
  if (SPECIAL.test(t)) {
    if (/nan/i.test(t)) return NaN;
    return t.startsWith("-") ? -Infinity : Infinity;
  }
  return null;
}

/** series._number (text already trimmed): a value, or null when blank, not a
 * number, NaN or infinite. */
function seriesNumber(t) {
  if (!t) return null;
  const comma = t.indexOf(",");
  if (comma >= 0 && t.indexOf(",", comma + 1) < 0 && !t.includes(".")) t = t.replace(",", ".");
  const v = pyFloat(t);
  return v === null || !Number.isFinite(v) ? null : v;
}

const CLOCK_RE = /^(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d):(\d\d)(\.\d+)?(?:([+-])(\d\d):?(\d\d)|Z)?$/;

/** core.clock_epoch, for clocks with an offset (or Z): UTC seconds, else null.
 * A clock without one is the logging PC's local time, which isn't known here. */
export function clockEpoch(text) {
  const t = (text || "").trim();
  const m = CLOCK_RE.exec(t);
  if (!m) return null;
  const base = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]) / 1000;
  const frac = m[7] ? Number(m[7]) : 0;
  if (m[8]) {
    const off = (m[8] === "-" ? -1 : 1) * (+m[9] * 3600 + +m[10] * 60);
    return base - off + frac;
  }
  return t.endsWith("Z") ? base + frac : null;
}

/** A log's local clock text moved back `seconds`: "YYYY-MM-DD HH:MM:SS". */
function shiftClock(text, seconds) {
  const m = CLOCK_RE.exec(text.trim());
  if (!m) return text.slice(0, 19);
  const t = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]) +
    Math.round(((m[7] ? Number(m[7]) : 0) - seconds) * 1000);
  return new Date(t).toISOString().slice(0, 19).replace("T", " ");
}

function isTime(name) {
  const n = name.trim().toLowerCase();
  return /^(time|zeit|timestamp)/.test(n) || ["t", "s", "sec", "seconds"].includes(n);
}

const HMS = /^(\d{1,2}):(\d{2}):(\d{2}(?:[.,]\d+)?)$/;

// ------------------------------------------------------------ one pass

/** The lines of a log, from a string or the uploaded bytes, without making
 * one string of the whole file. Yields each line without its newline. */
function* lines(input) {
  if (typeof input === "string") {
    let i = 0;
    while (i <= input.length) {
      let j = input.indexOf("\n", i);
      if (j < 0) j = input.length;
      yield input.slice(i, j > i && input[j - 1] === "\r" ? j - 1 : j);
      i = j + 1;
    }
    return;
  }
  const bytes = input instanceof Uint8Array ? input : new Uint8Array(input);
  const dec = new TextDecoder();
  let i = 0;
  while (i <= bytes.length) {
    let j = bytes.indexOf(10, i);
    if (j < 0) j = bytes.length;
    const end = j > i && bytes[j - 1] === 13 ? j - 1 : j;
    yield dec.decode(bytes.subarray(i, end));
    i = j + 1;
  }
}

/** One line's fields: quotes allowed within the line (not across lines). */
function fields(line, delim) {
  if (!line.includes('"')) return line.split(delim);
  const out = [];
  let field = "", quoted = false;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (quoted) {
      if (c === '"' && line[i + 1] === '"') { field += '"'; i++; }
      else if (c === '"') quoted = false;
      else field += c;
    } else if (c === '"') quoted = true;
    else if (c === delim) { out.push(field); field = ""; }
    else field += c;
  }
  out.push(field);
  return out;
}

class Growable {
  constructor() { this.a = new Float64Array(16); this.n = 0; }   // a sweep file has thousands of columns
  push(v) {
    if (this.n === this.a.length) {
      const b = new Float64Array(this.a.length * 2);
      b.set(this.a);
      this.a = b;
    }
    this.a[this.n++] = v;
  }
  get values() { return this.a.subarray(0, this.n); }
}

/**
 * A log read once into columns. For every data line kept by either reader:
 * logTime (playback.read_log's time, NaN if it skips the line), seriesTime
 * (series.build's, NaN likewise) and, per column, the read_log value (NaN
 * for blank or text). series.build's value differs from it only for a cell
 * with a decimal comma; those few are kept aside in `seriesOnly`.
 */
export function readColumns(input) {
  const it = lines(input);
  let first = it.next();
  if (first.done) throw new Error("empty log");
  const raw = first.value.replace(/^﻿/, "");
  const delim = [",", ";", "\t"].reduce(
    (best, d) => (raw.split(d).length > raw.split(best).length ? d : best), ",");
  const header = fields(raw, delim);
  const trimmed = header.map((h) => h.trim());
  const lower = trimmed.map((h) => h.toLowerCase());
  const skip = ["time", "clock", "event"];
  const logCols = header.map((h, i) => i).filter((i) => i && !LOG_META.includes(header[i]) &&
    trimmed[i]);
  const seriesCols = trimmed.map((h, i) => i).filter((i) => i && trimmed[i] && !skip.includes(lower[i]));
  const cols = [...new Set([...logCols, ...seriesCols])].sort((a, b) => a - b);
  const clockCol = lower.indexOf("clock") >= 0 ? lower.indexOf("clock") : null;
  const eventCol = trimmed.indexOf(LOG_EVENT);
  const firstClockCol = header.length > 1 && trimmed[1] === LOG_CLOCK ? 1 : null;

  const logTime = new Growable(), seriesTime = new Growable();
  const values = new Map(cols.map((i) => [i, new Growable()]));
  const seriesOnly = new Map();             // "col:row" -> value
  let hms = null, firstLog = null, seriesStart = null, firstSeries = null;
  let firstClock = null, odometer = null, lineNo = 0;
  const notes = [];                         // the Event column's non-empty cells: {t, text}
  for (const line of it) {
    lineNo++;
    if (!line.length) continue;             // a blank line ends nothing, like csv.reader's []
    const cells = fields(line, delim);
    // history._first_clock and _event_odometer read every line, junk included.
    const clockText = firstClock === null && firstClockCol !== null && cells.length > 1
      ? cells[1].trim() : "";
    if (odometer === null && eventCol >= 0 && lineNo <= 51) {
      const m = ODOMETER_RE.exec(eventCol < cells.length ? cells[eventCol] : "");
      if (m) odometer = parseInt(m[1], 10);
    }
    const c0 = cells[0].trim();
    const m = HMS.exec(c0);
    let tl = m ? +m[1] * 3600 + +m[2] * 60 + Number(m[3].replace(",", ".")) : pyFloat(c0);
    if (tl !== null) {
      if (hms === null) { hms = !!m; firstLog = tl; }
      if (hms) {
        tl -= firstLog;
        if (tl < 0) tl += 24 * 3600;
      }
    }
    const ts = seriesNumber(c0);
    if (clockText) {
      // The logger starts before it knows the time when it boots away from
      // Wi-Fi; its Clock column fills in later. Then the drive started that
      // row's clock less the time since the first row. (The app's logs have a
      // clock from the first row, as history._first_clock expects.)
      firstClock = tl !== null && logTime.n > 0
        ? shiftClock(clockText, tl - logTime.a[0]) : clockText.slice(0, 19);
    }
    if (tl === null && ts === null) continue;
    if (eventCol >= 0 && eventCol < cells.length && cells[eventCol].trim() && notes.length < MAX_NOTES) {
      notes.push({ t: tl, text: cells[eventCol].trim() });
    }
    if (ts !== null) {
      if (firstSeries === null) firstSeries = ts;
      // series.build takes the first row's clock; a later one does as well
      // (above), where the first row has none.
      if (seriesStart === null && clockCol !== null && clockCol < cells.length) {
        const utc = clockEpoch(cells[clockCol]);
        if (utc !== null) seriesStart = utc - ts;
      }
    }
    const row = logTime.n;
    logTime.push(tl === null ? NaN : tl);
    seriesTime.push(ts === null ? NaN : ts);
    for (const i of cols) {
      const text = i < cells.length ? cells[i].trim() : "";
      const v = text ? pyFloat(text) : null;
      values.get(i).push(v === null ? NaN : v);
      if (v === null && text.includes(",")) {
        const s = seriesNumber(text);
        if (s !== null) seriesOnly.set(`${i}:${row}`, s);
      }
    }
  }
  return { header: trimmed, logCols, seriesCols, rows: logTime.n, logTime: logTime.values,
    seriesTime: seriesTime.values, values: new Map([...values].map(([i, g]) => [i, g.values])),
    seriesOnly, firstSeries, seriesStart, firstClock, odometer, notes };
}

/** True when a log's header says it's a drive log (history.kind == "drive"). */
export function isDriveLog(input) {
  const it = lines(input).next();
  if (it.done) return false;
  const first = (it.value.replace(/^﻿/, "").split(/[,;\t]/)[0] || "").trim();
  return !!first && isTime(first);
}

// ------------------------------------------------------------ series

/** A series value at a row (series.build's reading of the cell), or null. */
function seriesValue(log, col, row) {
  const v = log.values.get(col)[row];
  if (Number.isFinite(v)) return v;
  return log.seriesOnly.size ? log.seriesOnly.get(`${col}:${row}`) ?? null : null;
}

/** series.build + series.chunks: {start, approx, names, channels()}, where
 * channels() yields each channel's chunks in turn, so only one channel's
 * JSON exists at a time. A chunk is {name, unit, t0, t1, n, min, max, mean,
 * json}, json being the stored data, [[seconds after t0, value], ...].
 * `fallbackEnd`: UTC seconds the log ended, for a log without a Clock
 * (marked approx). Units are left blank. */
export function buildSeries(log, fallbackEnd) {
  let start = log.seriesStart;
  const approx = start === null;
  if (approx) {
    let last = -Infinity;
    for (const i of log.seriesCols) {
      for (let r = log.rows - 1; r >= 0; r--) {
        if (Number.isFinite(log.seriesTime[r]) && seriesValue(log, i, r) !== null) {
          last = Math.max(last, log.seriesTime[r]);
          break;
        }
      }
    }
    const firstT = log.firstSeries || 0;
    if (last === -Infinity) last = firstT;
    start = fallbackEnd - (last - firstT);
  }
  const utc = new Float64Array(log.rows);       // each row's time, rounded once
  for (let r = 0; r < log.rows; r++) {
    utc[r] = Number.isFinite(log.seriesTime[r]) ? pyRound(start + log.seriesTime[r], 3) : NaN;
  }
  function* channels() {
    for (const i of log.seriesCols) {
      const name = log.header[i], chunks = [];
      let part = [], t0 = 0, t1 = 0, min = Infinity, max = -Infinity, sum = 0, n = 0;
      const flush = () => {
        chunks.push({ name, unit: "", t0, t1, n, min, max, mean: pyRound(sum / n, 6),
          json: `[${part.join(",")}]` });
        part = []; min = Infinity; max = -Infinity; sum = 0; n = 0;
      };
      for (let r = 0; r < log.rows; r++) {
        if (!Number.isFinite(utc[r])) continue;
        const v = seriesValue(log, i, r);
        if (v === null) continue;
        if (n === 0) t0 = utc[r];
        t1 = utc[r];
        part.push(`[${pyRound(utc[r] - t0, 3)},${v}]`);
        if (v < min) min = v;
        if (v > max) max = v;
        sum += v;
        if (++n === CHUNK) flush();
      }
      if (n) flush();
      if (chunks.length) yield chunks;
    }
  }
  return { start: pyRound(start, 3), approx, names: log.seriesCols.map((i) => log.header[i]),
    channels };
}

// ------------------------------------------------------------ summary

/** The first of a drive's logs (the fast file, then the sweep) with a
 * column named `name`, and that column; [null, undefined] if none has it. */
function columnOf(logs, name) {
  for (const log of logs) {
    const col = log.logCols.find((i) => log.header[i] === name);
    if (col !== undefined) return [log, col];
  }
  return [null, undefined];
}

/** history._series over read_log's rows: [times, values] of the numbers. */
function seriesOf(logs, name) {
  const t = [], v = [];
  const [log, col] = columnOf(logs, name);
  if (!log) return [t, v];
  const vals = log.values.get(col);
  for (let r = 0; r < log.rows; r++) {
    const x = vals[r];
    if (Number.isFinite(log.logTime[r]) && !Number.isNaN(x)) { t.push(log.logTime[r]); v.push(x); }
  }
  return [t, v];
}

function sum(xs) {
  let s = 0;
  for (const x of xs) s += x;
  return s;
}

function minOf(xs) {
  let m = Infinity;
  for (const x of xs) if (x < m) m = x;
  return m;
}

function maxOf(xs) {
  let m = -Infinity;
  for (const x of xs) if (x > m) m = x;
  return m;
}

function aggregate(how, values) {
  if (!values.length) return null;
  if (how === "mean") return sum(values) / values.length;
  if (how === "min") return minOf(values);
  if (how === "max") return maxOf(values);
  if (how === "pct") return (sum(values) / values.length - 1.0) * 100.0;
  if (how === "sd") {
    if (values.length < 2) return null;
    const mean = sum(values) / values.length;
    let s = 0;
    for (const v of values) s += (v - mean) ** 2;
    return Math.sqrt(s / (values.length - 1));
  }
  if (how === "increase") {
    let s = 0;
    for (let i = 1; i < values.length; i++) s += Math.max(0.0, values[i] - values[i - 1]);
    return s;
  }
  throw new Error(how);
}

function haversineKm(lat1, lon1, lat2, lon2) {
  const r = 6371.0088, rad = Math.PI / 180;
  const p1 = lat1 * rad, p2 = lat2 * rad, dp = p2 - p1, dl = (lon2 - lon1) * rad;
  const a = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * r * Math.asin(Math.min(1.0, Math.sqrt(a)));
}

function validFix(lat, lon) {
  return lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180 &&
    (Math.abs(lat) > 1e-4 || Math.abs(lon) > 1e-4);
}

/** history.gps_track: [[lat, lon]] of the rows with a real position. */
function gpsTrack(logs) {
  const out = [];
  const [log, a] = columnOf(logs, LAT);
  const b = log ? log.logCols.find((i) => log.header[i] === LON) : undefined;
  if (b === undefined) return out;
  const lats = log.values.get(a), lons = log.values.get(b);
  for (let r = 0; r < log.rows; r++) {
    if (!Number.isFinite(log.logTime[r])) continue;
    const x = lats[r], y = lons[r];
    if (!Number.isNaN(x) && !Number.isNaN(y) && validFix(x, y)) out.push([x, y]);
  }
  return out;
}

function integrate([t, v]) {
  let s = 0;
  for (let i = 1; i < t.length; i++) {
    if (t[i] > t[i - 1]) s += v[i - 1] * Math.min(t[i] - t[i - 1], MAX_STEP) / 3600.0;
  }
  return s;
}

function median(xs) {
  const s = [...xs].sort((a, b) => a - b);
  const m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

/** history.summarize over a drive's logs: one drive's statistics, null where
 * not logged. `logs`: one log, or the logger's fast file and its sweep file;
 * each statistic comes from the first that has its channel, the row count
 * and length from the first. */
export function summarize(logs, file = "", start = "") {
  logs = [].concat(logs);
  const log = logs[0];
  const names = new Set(logs.flatMap((l) => l.logCols.map((i) => l.header[i])));
  const out = Object.fromEntries(DRIVE_FIELDS.map((k) => [k, null]));
  let firstT = null, lastT = null;
  for (let r = 0; r < log.rows; r++) {
    if (!Number.isFinite(log.logTime[r])) continue;
    if (firstT === null) firstT = log.logTime[r];
    lastT = log.logTime[r];
  }
  const rows = log.logTime.reduce((n, t) => n + (Number.isFinite(t) ? 1 : 0), 0);
  Object.assign(out, { file, start, rows,
    minutes: rows ? pyRound((lastT - firstT) / 60.0, 2) : 0.0 });
  for (const [key, how, channels] of METRICS) {
    const name = channels.find((c) => names.has(c));
    if (name === undefined) continue;
    const value = aggregate(how, seriesOf(logs, name)[1]);
    if (value !== null) out[key] = pyRound(value * (SCALE[key] ?? 1.0), 4);
  }
  let km = null;
  for (const name of SPEEDS) {
    if (names.has(name)) {
      const s = seriesOf(logs, name);
      if (s[0].length >= 2) { km = integrate(s); break; }
    }
  }
  if (km === null) {
    const track = gpsTrack(logs);
    km = 0;
    for (let i = 1; i < track.length; i++) {
      km += haversineKm(track[i - 1][0], track[i - 1][1], track[i][0], track[i][1]);
    }
  }
  out.km = pyRound(km, 2);
  const flows = FUEL_FLOWS.filter((n) => names.has(n));
  if (flows.length) {
    let kg = 0.0;
    for (const name of flows) kg += integrate(seriesOf(logs, name));
    const fuel = kg / FUEL_DENSITY;
    out.fuel_l = pyRound(fuel, 3);
    if (out.km >= MIN_ECONOMY_KM && fuel > 0) {
      const per100 = fuel / out.km * 100.0;
      out.l_per_100km = pyRound(per100, 2);
      out.mpg = pyRound(MPG_US / per100, 2);
    }
  }
  if (names.has(FUEL_TANK)) {
    const [t, v] = seriesOf(logs, FUEL_TANK);
    if (t.length >= 2) {
      const t0 = t[0], t1 = t[t.length - 1];
      out.tank_used_l = pyRound(median(v.filter((_, k) => t[k] <= t0 + TANK_WINDOW)) -
        median(v.filter((_, k) => t[k] >= t1 - TANK_WINDOW)), 1);
    }
  }
  out.gps_points = names.has(LAT) && names.has(LON) ? gpsTrack(logs).length : 0;
  for (const name of ODOMETERS) {
    if (names.has(name)) {
      const [, v] = seriesOf(logs, name);
      if (v.length) {
        out.odometer_km = maxOf(v);
        break;
      }
    }
  }
  const counts = CYLINDERS.map((n) => out[`misfires_${n}`]).filter((c) => c !== null);
  out.misfires = counts.length ? sum(counts) : null;
  return out;
}

export const DEFAULT_TIME_ZONE = "America/Chicago";

/** "YYYY-MM-DD HH:MM:SS" of UTC seconds in `timeZone` (an IANA name). */
export function localStamp(seconds, timeZone = DEFAULT_TIME_ZONE) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", { timeZone,
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    second: "2-digit", hourCycle: "h23" }).formatToParts(new Date(seconds * 1000))
    .map((p) => [p.type, p.value]));
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
}

// The odometers the board reads, any one will do (loggerExtras).
const ODOMETERS_ANY = ODOMETERS.concat(["EGS_KILOMETERSTAND", "PMA_BUS_IN_KILOMETERSTAND_WERT"]);

/** The first channel of `names` the logs have: the median of its values in
 * the first and in the last `window` seconds ([null, null] if none). */
function ends(logs, names, window) {
  for (const name of names) {
    const [t, v] = seriesOf(logs, name);
    if (!t.length) continue;
    const t0 = t[0], t1 = t[t.length - 1];
    return [median(v.filter((_, k) => t[k] <= t0 + window)),
      median(v.filter((_, k) => t[k] >= t1 - window))];
  }
  return [null, null];
}

/** A drive log (a string or the uploaded bytes) read into columns; throws
 * if it isn't one with samples. */
export function readLog(input) {
  const log = readColumns(input);
  if (log.logCols.length === 0 && log.seriesCols.length === 0) throw new Error("no channels");
  if (!isTime(log.header[0] || "") || !log.logTime.some(Number.isFinite)) {
    throw new Error("the log has a header but no samples");
  }
  return log;
}

/** A drive's row for History from its logs (one, or the fast file and its
 * sweep), as history.scan makes it: null when the first has too few rows
 * (MIN_ROWS). `uploaded`: UTC seconds, the stand-in for the end of a log
 * without a Clock; its start is then given in `timeZone`, the local time the
 * logs' own clocks keep. */
export function summarizeDrive(logs, file, uploaded, timeZone = DEFAULT_TIME_ZONE) {
  logs = [].concat(logs);
  const times = logs[0].logTime.filter(Number.isFinite);
  if (times.length < MIN_ROWS) return null;
  const start = logs[0].firstClock ||
    localStamp(uploaded - (times[times.length - 1] - times[0]), timeZone);
  const drive = summarize(logs, file, start);
  if (drive.odometer_km === null) {
    drive.odometer_km = logs.map((l) => l.odometer).find((o) => o !== null) ?? null;
  }
  return drive;
}

// The board's "tier rates" note, once a minute: fast and medium reads a
// second per identifier, the slow pass's length.
const TIER_RATES_RE = /tier rates:(?: fast ([\d.]+)\/s)?(?: medium ([\d.]+)\/s)?(?: slow pass (\d+) s)?/;

/** The rates each tier got over a drive, from the board's notes: the means
 * of fast and medium, the median slow pass (null without notes). */
function tierRates(logs) {
  const fast = [], medium = [], slow = [];
  for (const log of logs) {
    for (const n of log.notes || []) {
      const m = TIER_RATES_RE.exec(n.text);
      if (!m) continue;
      if (m[1]) fast.push(Number(m[1]));
      if (m[2]) medium.push(Number(m[2]));
      if (m[3]) slow.push(Number(m[3]));
    }
  }
  const mean = (v) => (v.length ? pyRound(v.reduce((a, b) => a + b, 0) / v.length, 2) : null);
  return { rate_fast: mean(fast), rate_medium: mean(medium),
    slow_pass_s: slow.length ? median(slow) : null };
}

// Each half of the saddle tank, the DME's and the body module's, any one.
const TANK_LEFT = ["FUELLST_TANKH_L", "BDC_TANK_FUELLSTAND_LINKS"];
const TANK_RIGHT = ["FUELLST_TANKH_R", "BDC_TANK_FUELLSTAND_RECHTS"];

/** What the logger's drive rows carry beyond the app's (not in history.py):
 * whether the start is only the upload time less the drive's length (no
 * Clock), and for fill-ups (tanks.js) the tank level and the odometer as
 * the drive started and ended, with each half of the tank (in its
 * channel's unit). The board reads them together at the start, at Park and
 * at engine-off (its snapshots). And the rates each tier got (tierRates). */
export function loggerExtras(logs) {
  logs = [].concat(logs);
  const [tankStart, tankEnd] = ends(logs, [FUEL_TANK], TANK_WINDOW);
  const [leftStart, leftEnd] = ends(logs, TANK_LEFT, TANK_WINDOW);
  const [rightStart, rightEnd] = ends(logs, TANK_RIGHT, TANK_WINDOW);
  const [odoStart, odoEnd] = ends(logs, ODOMETERS_ANY, 0);
  return { start_approx: !logs[0].firstClock, tank_start_l: tankStart, tank_end_l: tankEnd,
    tank_left_start: leftStart, tank_left_end: leftEnd, tank_right_start: rightStart,
    tank_right_end: rightEnd, odometer_start_km: odoStart, odometer_end_km: odoEnd,
    ...tierRates(logs) };
}

/** One drive log on its own: {series, drive}. */
export function processLog(input, file, uploaded, timeZone = DEFAULT_TIME_ZONE) {
  const log = readLog(input);
  return { series: buildSeries(log, uploaded),
    drive: summarizeDrive(log, file, uploaded, timeZone) };
}
