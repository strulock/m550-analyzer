/**
 * Drive health (the logger page's Health tab, /api/health, the MCP's
 * drive_health): each drive boiled down to a few numbers per channel in the
 * same operating conditions, and compared with that channel's own recent
 * past, so drift stands out without opening the time series.
 *
 * Only samples with the engine fully warm count: coolant (ITKUM) at least
 * WARM_COOLANT_C and, wherever the oil temperature was read, oil (TEMP_OEL,
 * else ITOEL) at least WARM_OIL_C. S_TEMP_KUEHLMITTEL is the DME's coolant
 * setpoint, not a measurement, and is never used. Each warm sample goes into
 * an RPM x load cell; load is the SAE J1979 calculated load (what ILMAR
 * reports) worked out from the throttle air flow and RPM, because ILMAR
 * itself is read only every half minute or so (ILMAR where there's no air
 * flow). Overrun fuel cut is left out as well (either bank's lambda at
 * FUEL_CUT_LAMBDA or more: the sensors read air, and nothing else in the
 * cell is normal running either). Per channel and cell: count, mean,
 * median, p95, min and max; counters (misfires) as a rate per 1,000
 * revolutions.
 *
 * Scores: per channel and cell, the median and MAD of that cell's value
 * (its median; a counter's rate) over the last BASELINE_DRIVES drives before
 * this one that count as healthy: not left out by hand, and that channel
 * not alerting in them. The drive's robust z-score per cell, and the
 * channel's score the sample-weighted mean of its cells'. A cell needs
 * MIN_SAMPLES samples (a counter MIN_REVS revolutions) and MIN_BASELINE
 * baseline drives; otherwise it has no score. Summaries are kept per drive
 * (health_summaries) and the scores worked out again, oldest drive first,
 * whenever one changes (health_scores).
 */

export const WARM_COOLANT_C = 95;
export const WARM_OIL_C = 90;
export const WARM_MIN_S = 60;              // a drive needs this much warm time to count
export const BASELINE_DRIVES = 10;
export const MIN_BASELINE = 3;
export const MIN_SAMPLES = 10;
export const MIN_REVS = 1000;
export const RPM_EDGES = [400, 900, 1500, 2000, 2500, 3000, 4000, 8000];
export const LOAD_EDGES = [0, 20, 35, 50, 70, 100, 400];
export const SUMMARY_VERSION = 1;

const RPM = "INMOT";
const COOLANT = "ITKUM";
const OILS = ["TEMP_OEL", "ITOEL"];
const FLOW_B1 = "MASSENSTROM_DK_BANK1", FLOW_B2 = "MASSENSTROM_DK_BANK1@0x13";
const ILMAR = "ILMAR";
const LAMBDAS = ["ILAG1", "ILAG1@0x13"];
const FUEL_CUT_LAMBDA = 1.5;
const TEMP_FRESH_S = 60;                   // a temperature this old still stands
const FAST_GAP_S = 1.5;                    // RPM and load: the nearest within this
const RUN_GAP_S = 3;                       // a cell's visit ends after a gap this long
const RUNS_KEPT = 3;                       // each cell's longest visits, for the raw log
const STEP_MAX_S = 2;                      // an RPM sample stands for at most this long
const WEIGHT_MAX = 300;                    // one cell's weight in a channel's score
// SAE J1979 calculated load: the air drawn over what the cylinders hold at
// 25 °C and sea level. N63TU: 4,395 cc, eight cylinders, two intakes a turn.
const DISPLACEMENT_L = 4.395, AIR_G_PER_L = 1.184;

export const GROUPS = [
  { key: "fueling", label: "Fueling", watch: 2, alert: 3 },
  { key: "ignition", label: "Ignition and knock", watch: 2, alert: 3 },
  { key: "temps", label: "Temperatures and oil", watch: 2, alert: 3 },
  { key: "boost", label: "Boost", watch: 2, alert: 3 },
  { key: "misfires", label: "Misfires", watch: 2, alert: 3 },
  { key: "electrical", label: "Electrical", watch: 2, alert: 3 },
];

const B2 = "@0x13";
const ch = (key, group, label, names, floor, extra = {}) =>
  ({ key, group, label, names, floor, kind: "level", rel: 0, ...extra });

/** The channels scored: key (the first name), its group and label, the
 * channels to read it from in order, and `floor`, the least spread a
 * baseline is given (so a channel that never moves doesn't alert on a
 * hair's change; `rel` adds a share of the baseline's median). `pair`: the
 * other bank's (or matching cylinder's) key. */
export const CHANNELS = [
  ch("IMUL1", "fueling", "Fuel trim, bank 1", ["IMUL1"], 0.01, { pair: "IMUL1" + B2 }),
  ch("IMUL1" + B2, "fueling", "Fuel trim, bank 2", ["IMUL1" + B2], 0.01, { pair: "IMUL1" }),
  ch("ILAG1", "fueling", "Lambda, bank 1", ["ILAG1"], 0.01, { pair: "ILAG1" + B2 }),
  ch("ILAG1" + B2, "fueling", "Lambda, bank 2", ["ILAG1" + B2, "ILAG2"], 0.01,
    { pair: "ILAG1" }),
  ch("MITTELW_ZYLIND_ZWSPAET_KR_HALBQUANT", "ignition", "Knock retard, bank 1",
    ["MITTELW_ZYLIND_ZWSPAET_KR_HALBQUANT"], 0.3,
    { pair: "MITTELW_ZYLIND_ZWSPAET_KR_HALBQUANT" + B2 }),
  ch("MITTELW_ZYLIND_ZWSPAET_KR_HALBQUANT" + B2, "ignition", "Knock retard, bank 2",
    ["MITTELW_ZYLIND_ZWSPAET_KR_HALBQUANT" + B2], 0.3,
    { pair: "MITTELW_ZYLIND_ZWSPAET_KR_HALBQUANT" }),
];
const spark = (n) => `DIAG_BRENNDAUER_ZUENDFUNKE_CYL${n <= 4 ? n : n - 4}${n <= 4 ? "" : B2}`;
for (let n = 1; n <= 8; n++) {
  CHANNELS.push(ch(spark(n), "ignition", `Spark burn, cyl ${n}`, [spark(n)], 0.01,
    { rel: 0.03, pair: spark(n <= 4 ? n + 4 : n - 4) }));
}
CHANNELS.push(
  ch("ITKUM", "temps", "Coolant", ["ITKUM"], 1),
  ch("TEMP_OEL", "temps", "Oil temperature", OILS, 1),
  ch("OELDRUCK", "temps", "Oil pressure", ["OELDRUCK"], 100, { rel: 0.03 }),
  ch("RTANS", "temps", "Intake air", ["RTANS"], 2),
  ch("EGS_GETRIEBEOELTMEPERATUR", "temps", "Gearbox oil", ["EGS_GETRIEBEOELTMEPERATUR"], 1),
  ch("TEMP_NACH_KAT_MODELL", "temps", "After cat, bank 1", ["TEMP_NACH_KAT_MODELL"],
    10, { pair: "TEMP_NACH_KAT_MODELL" + B2 }),
  ch("TEMP_NACH_KAT_MODELL" + B2, "temps", "After cat, bank 2",
    ["TEMP_NACH_KAT_MODELL" + B2], 10, { pair: "TEMP_NACH_KAT_MODELL" }),
  ch("IPLAD", "boost", "Boost pressure", ["IPLAD"], 20, { rel: 0.01 }),
);
const misfire = (n) => `ZAEHLER_AUSSETZERK_ZYL${n}`;
for (let n = 1; n <= 8; n++) {
  CHANNELS.push(ch(misfire(n), "misfires", `Misfires, cyl ${n}`,
    [misfire(n)].concat(n > 4 ? [misfire(n) + B2] : []), 0.5,
    { kind: "count", pair: misfire(n <= 4 ? n + 4 : n - 4) }));
}
CHANNELS.push(ch("IUIBS", "electrical", "Battery voltage", ["IUIBS"], 100));

const BY_KEY = new Map(CHANNELS.map((c) => [c.key, c]));
const GROUP = new Map(GROUPS.map((g) => [g.key, g]));

/** Every channel a summary reads. */
export const NEEDED = [...new Set([RPM, COOLANT, ...OILS, FLOW_B1, FLOW_B2, ILMAR, ...LAMBDAS,
  ...CHANNELS.flatMap((c) => c.names)])];

export const HEALTH_SCHEMA = [
  // One drive's summary: {start, end, warm_s, cells: {cell: [seconds, revs,
  // [[t0, t1], ...] its longest visits]}, channels: {key: {name, unit,
  // cells: {cell: [n, mean, median, p95, min, max] | [increase, revs]}}}}.
  `CREATE TABLE IF NOT EXISTS health_summaries (drive TEXT PRIMARY KEY, start REAL,
     warm_s REAL, version INTEGER, data TEXT NOT NULL)`,
  // Its scores: scores = {key: [score, cells, value, low, high]} (value and
  // the normal band over the scored cells, sample-weighted), cells = {key:
  // {cell: [value, median, spread, z, n]}}.
  `CREATE TABLE IF NOT EXISTS health_scores (drive TEXT PRIMARY KEY, start REAL,
     overall REAL, level INTEGER, scores TEXT NOT NULL, cells TEXT NOT NULL)`,
  // Drives left out of every baseline by hand.
  `CREATE TABLE IF NOT EXISTS health_excluded (drive TEXT PRIMARY KEY, at TEXT, who TEXT)`,
];

// ------------------------------------------------------------------ summaries

/** A channel's samples, oldest first: {t: [...], v: [...]}. */
function track(points) {
  points.sort((a, b) => a[0] - b[0]);
  return { t: points.map((p) => p[0]), v: points.map((p) => p[1]) };
}

/** The value nearest t within `gap` seconds, or null. */
function nearest(tr, t, gap) {
  if (!tr || !tr.t.length) return null;
  let lo = 0, hi = tr.t.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (tr.t[mid] < t) lo = mid + 1; else hi = mid;
  }
  let best = null, bestD = gap;
  for (const i of [lo - 1, lo]) {
    if (i < 0 || i >= tr.t.length) continue;
    const d = Math.abs(tr.t[i] - t);
    if (d <= bestD && Number.isFinite(tr.v[i])) { best = tr.v[i]; bestD = d; }
  }
  return best;
}

function band(edges, x) {
  if (!(x >= edges[0])) return -1;
  for (let i = 1; i < edges.length; i++) if (x < edges[i]) return i - 1;
  return -1;
}

/** Calculated load (%) from throttle air flow (kg/h) through `cylinders`. */
export function loadFromFlow(kgPerHour, rpm, cylinders) {
  const full = (rpm / 60) * (cylinders / 2) * (DISPLACEMENT_L / 8) * AIR_G_PER_L;
  return full > 0 ? (kgPerHour / 3.6) / full * 100 : null;
}

/** The cell ("rpm band.load band") the engine was in at t, warm, or null. */
function conditionsOf(tracks) {
  const rpm = tracks.get(RPM), coolant = tracks.get(COOLANT);
  const oil = OILS.map((n) => tracks.get(n)).find((x) => x && x.t.length);
  const f1 = tracks.get(FLOW_B1), f2 = tracks.get(FLOW_B2), ilmar = tracks.get(ILMAR);
  const lambdas = LAMBDAS.map((n) => tracks.get(n)).filter(Boolean);
  return (t) => {
    const c = nearest(coolant, t, TEMP_FRESH_S);
    if (c === null || c < WARM_COOLANT_C) return null;
    const o = nearest(oil, t, TEMP_FRESH_S);
    if (o !== null && o < WARM_OIL_C) return null;
    const r = nearest(rpm, t, FAST_GAP_S);
    if (r === null || r < RPM_EDGES[0]) return null;
    if (lambdas.some((l) => nearest(l, t, FAST_GAP_S) >= FUEL_CUT_LAMBDA)) return null;
    const a = nearest(f1, t, FAST_GAP_S), b = nearest(f2, t, FAST_GAP_S);
    let load = a !== null && b !== null ? loadFromFlow(a + b, r, 8)
      : a !== null ? loadFromFlow(a, r, 4) : b !== null ? loadFromFlow(b, r, 4) : null;
    if (load === null) load = nearest(ilmar, t, FAST_GAP_S);
    const ri = band(RPM_EDGES, r), li = load === null ? -1 : band(LOAD_EDGES, load);
    return ri < 0 || li < 0 ? null : `${ri}.${li}`;
  };
}

function stats(values) {
  const s = values.slice().sort((a, b) => a - b), n = s.length;
  const mid = n >> 1, median = n % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
  const mean = s.reduce((x, y) => x + y, 0) / n;
  return [n, r6(mean), r6(median), r6(s[Math.max(0, Math.ceil(0.95 * n) - 1)]), s[0], s[n - 1]];
}

const r6 = (x) => (x === null || !Number.isFinite(x) ? null : +x.toFixed(6));

/** One drive's summary from its samples: tracks = Map(name -> {t, v}), or
 * {name: [[t, v], ...]}; units = {name: unit}. Null with no warm time. */
export function summarize(input, units = {}) {
  const tracks = input instanceof Map ? input
    : new Map(Object.entries(input).map(([k, pts]) => [k, track(pts.slice())]));
  const rpm = tracks.get(RPM);
  if (!rpm || !rpm.t.length) return null;
  const cellAt = conditionsOf(tracks);
  // Time and revolutions in each cell, and its visits.
  const cells = {}, runs = {};
  let warm = 0, run = null;
  for (let i = 0; i < rpm.t.length; i++) {
    const t = rpm.t[i], k = cellAt(t);
    const dt = i + 1 < rpm.t.length ? Math.min(STEP_MAX_S, rpm.t[i + 1] - t) : 0;
    if (run && (run.k !== k || t - run.t1 > RUN_GAP_S)) {
      (runs[run.k] = runs[run.k] || []).push([run.t0, run.t1]);
      run = null;
    }
    if (k === null) continue;
    const c = cells[k] = cells[k] || [0, 0];
    c[0] += dt;
    c[1] += (rpm.v[i] / 60) * dt;
    warm += dt;
    if (run) run.t1 = t + dt; else run = { k, t0: t, t1: t + dt };
  }
  if (run) (runs[run.k] = runs[run.k] || []).push([run.t0, run.t1]);
  if (warm <= 0) return null;
  const out = { start: rpm.t[0], end: rpm.t[rpm.t.length - 1], warm_s: +warm.toFixed(1),
    cells: {}, channels: {} };
  for (const [k, c] of Object.entries(cells)) {
    const longest = (runs[k] || []).sort((x, y) => (y[1] - y[0]) - (x[1] - x[0]))
      .slice(0, RUNS_KEPT).map(([a, b]) => [+a.toFixed(1), +b.toFixed(1)]);
    out.cells[k] = [+c[0].toFixed(1), Math.round(c[1]), longest];
  }
  for (const spec of CHANNELS) {
    const name = spec.names.find((n) => tracks.get(n) && tracks.get(n).t.length);
    if (!name) continue;
    const tr = tracks.get(name), per = {};
    if (spec.kind === "count") {
      // Only the cells the counter was read in: no reading isn't no misfire.
      let prev = null;
      for (let i = 0; i < tr.t.length; i++) {
        const v = tr.v[i];
        if (!Number.isFinite(v)) continue;
        const k = cellAt(tr.t[i]);
        if (k !== null) per[k] = (per[k] || 0) + (prev !== null && v > prev ? v - prev : 0);
        prev = v;
      }
      const c = {};
      for (const k of Object.keys(per)) if (out.cells[k]) c[k] = [per[k], out.cells[k][1]];
      if (!Object.keys(c).length) continue;
      out.channels[spec.key] = { name, unit: "per 1,000 rev", cells: c };
      continue;
    }
    for (let i = 0; i < tr.t.length; i++) {
      const v = tr.v[i];
      if (!Number.isFinite(v)) continue;
      const k = cellAt(tr.t[i]);
      if (k !== null) (per[k] = per[k] || []).push(v);
    }
    const c = {};
    for (const [k, vs] of Object.entries(per)) c[k] = stats(vs);
    if (Object.keys(c).length) {
      out.channels[spec.key] = { name, unit: units[name] || "", cells: c };
    }
  }
  return out;
}

// ------------------------------------------------------------------ scores

/** A cell's value for comparing, and its weight, or null when too thin. */
function cellValue(spec, cell) {
  if (spec.kind === "count") {
    const [inc, revs] = cell;
    return revs >= MIN_REVS ? [inc / revs * 1000, Math.min(WEIGHT_MAX, revs / 100)] : null;
  }
  return cell[0] >= MIN_SAMPLES ? [cell[2], Math.min(WEIGHT_MAX, cell[0])] : null;
}

function median(xs) {
  const s = xs.slice().sort((a, b) => a - b), m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

/** 0 normal, 1 watch, 2 alert, for a score in a group. */
export function levelOf(group, score) {
  const g = GROUP.get(group) || GROUPS[0];
  if (score === null || score === undefined) return null;
  const a = Math.abs(score);
  return a >= g.alert ? 2 : a >= g.watch ? 1 : 0;
}

/** Scores for every drive from their summaries, oldest first: rows =
 * [{drive, start, summary}], excluded = Set of drives kept out of the
 * baselines. [{drive, start, overall, level, scores, cells}]. */
export function scoreDrives(rows, excluded = new Set()) {
  const sorted = rows.slice().sort((a, b) => a.start - b.start);
  const history = new Map();          // key -> [{cells: {cell: value}}] healthy only
  const out = [];
  for (const row of sorted) {
    const scores = {}, cells = {};
    let overall = null, level = null;
    for (const spec of CHANNELS) {
      const ch = row.summary.channels[spec.key];
      if (!ch) continue;
      const past = (history.get(spec.key) || []).slice(-BASELINE_DRIVES);
      const mine = {};
      let wsum = 0, zsum = 0, xsum = 0, losum = 0, hisum = 0, allW = 0, allX = 0, n = 0;
      const g = GROUP.get(spec.group);
      for (const [k, cell] of Object.entries(ch.cells)) {
        const cv = cellValue(spec, cell);
        if (!cv) continue;
        const [x, w] = cv;
        mine[k] = x;
        allW += w;
        allX += w * x;
        const base = past.map((p) => p[k]).filter((v) => v !== undefined);
        if (base.length < MIN_BASELINE) {
          (cells[spec.key] = cells[spec.key] || {})[k] = [r6(x), null, null, null, cell[0]];
          continue;
        }
        const med = median(base);
        const mad = median(base.map((v) => Math.abs(v - med)));
        const spread = Math.max(1.4826 * mad, spec.floor, spec.rel * Math.abs(med));
        const z = (x - med) / spread;
        (cells[spec.key] = cells[spec.key] || {})[k] = [r6(x), r6(med), r6(spread), r6(z),
          cell[0]];
        wsum += w; zsum += w * z; xsum += w * x;
        losum += w * (med - g.watch * spread); hisum += w * (med + g.watch * spread);
        n++;
      }
      if (!Object.keys(mine).length) continue;
      const score = wsum ? zsum / wsum : null;
      scores[spec.key] = wsum
        ? [r6(score), n, r6(xsum / wsum), r6(losum / wsum), r6(hisum / wsum)]
        : [null, 0, r6(allX / allW), null, null];
      const lv = levelOf(spec.group, score);
      if (lv !== null) {
        level = Math.max(level ?? 0, lv);
        if (overall === null || Math.abs(score) > Math.abs(overall)) overall = r6(score);
      }
      if (!excluded.has(row.drive) && lv !== 2) {
        if (!history.has(spec.key)) history.set(spec.key, []);
        history.get(spec.key).push(mine);
      }
    }
    out.push({ drive: row.drive, start: row.start, overall, level, scores, cells });
  }
  return out;
}

// ------------------------------------------------------------------ D1

/** Summarize one stored drive from its series and keep it (or drop it when
 * it has no warm time). */
export async function summarizeStored(env, drive) {
  const r = await env.DB.prepare(
    `SELECT channel, t0, unit, data FROM series WHERE drive = ? AND channel IN
     (SELECT value FROM json_each(?)) ORDER BY channel, t0`)
    .bind(drive, JSON.stringify(NEEDED)).all();
  const pts = new Map(), units = {};
  for (const row of r.results || []) {
    if (row.unit) units[row.channel] = row.unit;
    const list = pts.get(row.channel) || [];
    for (const [dt, v] of JSON.parse(row.data)) {
      if (typeof v === "number") list.push([row.t0 + dt, v]);
    }
    pts.set(row.channel, list);
  }
  const tracks = new Map([...pts].map(([k, list]) => [k, track(list)]));
  const s = summarize(tracks, units);
  if (!s || s.warm_s < WARM_MIN_S) {
    await env.DB.prepare("DELETE FROM health_summaries WHERE drive = ?").bind(drive).run();
    return { drive, warm_s: s ? s.warm_s : 0, kept: false };
  }
  await env.DB.prepare(`INSERT OR REPLACE INTO health_summaries (drive, start, warm_s,
    version, data) VALUES (?, ?, ?, ?, ?)`)
    .bind(drive, s.start, s.warm_s, SUMMARY_VERSION, JSON.stringify(s)).run();
  return { drive, warm_s: s.warm_s, kept: true, channels: Object.keys(s.channels).length };
}

/** Work out every drive's scores again from the stored summaries. */
export async function rescore(env) {
  const r = await env.DB.prepare("SELECT drive, start, data FROM health_summaries").all();
  const ex = await env.DB.prepare("SELECT drive FROM health_excluded").all();
  const excluded = new Set((ex.results || []).map((x) => x.drive));
  const scored = scoreDrives((r.results || []).map((x) =>
    ({ drive: x.drive, start: x.start, summary: JSON.parse(x.data) })), excluded);
  const stmts = [env.DB.prepare("DELETE FROM health_scores")];
  for (const s of scored) {
    stmts.push(env.DB.prepare(`INSERT INTO health_scores (drive, start, overall, level,
      scores, cells) VALUES (?, ?, ?, ?, ?, ?)`).bind(s.drive, s.start, s.overall, s.level,
      JSON.stringify(s.scores), JSON.stringify(s.cells)));
  }
  await env.DB.batch(stmts);
  return { drives: scored.length };
}

/** A drive's series just changed: its summary, then everyone's scores. */
export async function updateHealth(env, drive) {
  const s = await summarizeStored(env, drive);
  await rescore(env);
  return s;
}

const REBUILD_BATCH = 3;

/** Summarize stored drives again, REBUILD_BATCH a request, by name after
 * `after`; the scores once the last is done. {done, next}. */
export async function rebuildHealth(env, after) {
  const r = await env.DB.prepare(`SELECT DISTINCT drive FROM series WHERE drive > ?
    ORDER BY drive LIMIT ?`).bind(after || "", REBUILD_BATCH).all();
  const done = [];
  for (const { drive } of r.results || []) done.push(await summarizeStored(env, drive));
  const next = done.length === REBUILD_BATCH ? done[done.length - 1].drive : null;
  if (!next) await rescore(env);
  return { done, next };
}

/** Keep a drive out of the baselines (or let it back in). */
export async function excludeDrive(env, drive, excluded, who) {
  await env.DB.prepare(excluded
    ? "INSERT OR REPLACE INTO health_excluded (drive, at, who) VALUES (?, ?, ?)"
    : "DELETE FROM health_excluded WHERE drive = ?")
    .bind(...(excluded ? [drive, new Date().toISOString(), who || ""] : [drive])).run();
  await rescore(env);
  return { drive, excluded: !!excluded };
}

const iso = (s) => (s === null || s === undefined ? null : new Date(s * 1000).toISOString());

/** The heatmap: settings, channels, drives (oldest first) and each
 * channel's [score, cells, value, low, high] per drive, in drives' order.
 * from/to: ISO dates or times narrowing the drives. */
export async function healthOverview(env, { from = null, to = null } = {}) {
  const lo = from ? Date.parse(from) / 1000 : null, hi = to ? Date.parse(to) / 1000 : null;
  const r = await env.DB.prepare(`SELECT s.drive, s.start, s.overall, s.level, s.scores,
    h.warm_s, e.drive AS excluded FROM health_scores s
    JOIN health_summaries h ON h.drive = s.drive
    LEFT JOIN health_excluded e ON e.drive = s.drive ORDER BY s.start`).all();
  const rows = (r.results || []).filter((x) =>
    (lo === null || !Number.isFinite(lo) || x.start >= lo) &&
    (hi === null || !Number.isFinite(hi) || x.start <= hi + 86400));
  const per = rows.map((x) => JSON.parse(x.scores));
  const units = await channelUnits(env);
  const scores = {};
  for (const c of CHANNELS) {
    if (per.some((p) => p[c.key])) scores[c.key] = per.map((p) => p[c.key] || null);
  }
  return {
    groups: GROUPS,
    channels: CHANNELS.filter((c) => scores[c.key]).map((c) => ({ key: c.key, label: c.label,
      group: c.group, kind: c.kind, pair: c.pair || null,
      unit: c.kind === "count" ? "per 1,000 rev" : units[c.names[0]] || "" })),
    drives: rows.map((x) => ({ drive: x.drive, start: iso(x.start),
      warm_min: +(x.warm_s / 60).toFixed(1), overall: x.overall, level: x.level,
      excluded: !!x.excluded })),
    scores,
    settings: { warm_coolant_c: WARM_COOLANT_C, warm_oil_c: WARM_OIL_C,
      rpm_edges: RPM_EDGES, load_edges: LOAD_EDGES, baseline_drives: BASELINE_DRIVES,
      min_baseline: MIN_BASELINE, min_samples: MIN_SAMPLES, min_revs: MIN_REVS },
  };
}

async function channelUnits(env) {
  const names = CHANNELS.map((c) => c.names[0]);
  const r = await env.DB.prepare(`SELECT channel, MAX(unit) AS unit FROM series
    WHERE channel IN (SELECT value FROM json_each(?)) AND unit != '' GROUP BY channel`)
    .bind(JSON.stringify(names)).all();
  return Object.fromEntries((r.results || []).map((x) => [x.channel, x.unit]));
}

/** One drive and channel: each RPM x load cell's value against its
 * baseline, the drive's longest visit to each cell, and the series name
 * and times for the raw log. */
export async function healthDetail(env, drive, key) {
  const spec = BY_KEY.get(key);
  if (!spec) return null;
  const r = ((await env.DB.prepare(`SELECT s.cells, h.data FROM health_scores s
    JOIN health_summaries h ON h.drive = s.drive WHERE s.drive = ?`).bind(drive).all())
    .results || [])[0];
  if (!r) return null;
  const summary = JSON.parse(r.data), cells = JSON.parse(r.cells)[key] || {};
  const ch = summary.channels[key];
  const grid = {};
  for (const [k, c] of Object.entries(summary.cells)) {
    const v = cells[k];
    grid[k] = { seconds: c[0], revs: c[1], visits: c[2].map(([a, b]) => [iso(a), iso(b)]),
      value: v ? v[0] : null, baseline: v ? v[1] : null, spread: v ? v[2] : null,
      z: v ? v[3] : null, n: v ? v[4] : 0 };
    if (spec.kind !== "count" && ch && ch.cells[k]) {
      const [n, mean, med, p95, min, max] = ch.cells[k];
      Object.assign(grid[k], { n, mean, median: med, p95, min, max });
    }
  }
  return { drive, channel: key, label: spec.label, group: spec.group, kind: spec.kind,
    name: ch ? ch.name : null, unit: ch ? ch.unit : "", start: iso(summary.start),
    end: iso(summary.end), warm_min: +(summary.warm_s / 60).toFixed(1),
    rpm_edges: RPM_EDGES, load_edges: LOAD_EDGES, cells: grid };
}
