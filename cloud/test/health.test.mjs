// Drive health (health.js): the warm filter, RPM x load cells, counters as
// rates, and scores against a rolling baseline.
//   node --test "cloud/test/*.test.mjs"

import { test } from "node:test";
import assert from "node:assert/strict";

import { BASELINE_DRIVES, loadFromFlow, restingBefore, scoreDrives, summarize } from "../src/health.js";

// A drive of `seconds` at a steady 1,500 rpm and 40 % load, sampled every
// half second; `at(t)` overrides any channel's value.
function drive(t0, { seconds = 300, at = {} } = {}) {
  const flow = 40 / loadFromFlow(1, 1500, 8) / 2;   // kg/h a bank for 40 %
  const base = {
    INMOT: 1500, MASSENSTROM_DK_BANK1: flow, "MASSENSTROM_DK_BANK1@0x13": flow,
    ITKUM: 100, TEMP_OEL: 100, S_TEMP_KUEHLMITTEL: 60, IMUL1: 1.0,
    ZAEHLER_AUSSETZERK_ZYL1: 0,
  };
  const out = {};
  for (let i = 0; i * 0.5 < seconds; i++) {
    const t = t0 + i * 0.5;
    for (const [k, v] of Object.entries({ ...base, ...at })) {
      const x = at[k] ? at[k](t - t0, base[k]) : v;
      if (x !== null) (out[k] = out[k] || []).push([t, x]);
    }
  }
  return out;
}

test("calculated load from throttle air flow", () => {
  // Bank 1's four cylinders at 1,500 rpm drawing what they hold: 100 %.
  const full = 1500 / 60 * 2 * (4.395 / 8) * 1.184 * 3.6;
  assert.ok(Math.abs(loadFromFlow(full, 1500, 4) - 100) < 1e-9);
  assert.ok(Math.abs(loadFromFlow(full, 1500, 8) - 50) < 1e-9);
});

test("only warm samples count, in their RPM x load cell; the coolant setpoint is ignored", () => {
  // Coolant reaches 95 °C after 100 s; the setpoint (S_TEMP_KUEHLMITTEL) says 60 throughout.
  const s = summarize(drive(1000, { at: { ITKUM: (t) => (t < 100 ? 80 : 100) } }));
  assert.ok(Math.abs(s.warm_s - 200) < 1.5, `warm ${s.warm_s}`);
  assert.deepEqual(Object.keys(s.cells), ["2.2"]);           // 1,500-2,000 rpm, 35-50 %
  const [n, mean, median] = s.channels.IMUL1.cells["2.2"];
  assert.ok(n >= 398 && n <= 401);
  assert.equal(mean, 1);
  assert.equal(median, 1);
  // Oil under 90 °C keeps it out too, where oil was read.
  const cold = summarize(drive(1000, { at: { TEMP_OEL: () => 85 } }));
  assert.equal(cold, null);
  // No oil reading at all: coolant alone decides.
  const noOil = summarize(drive(1000, { at: { TEMP_OEL: () => null } }));
  assert.ok(noOil.warm_s > 290);
  // No coolant reading: never warm.
  assert.equal(summarize(drive(1000, { at: { ITKUM: () => null } })), null);
});

test("ILMAR is the load when there's no air flow; cells split by RPM", () => {
  const s = summarize(drive(0, { at: {
    MASSENSTROM_DK_BANK1: () => null, "MASSENSTROM_DK_BANK1@0x13": () => null,
    ILMAR: () => 25, INMOT: (t) => (t < 150 ? 800 : 2600),
  } }));
  assert.deepEqual(Object.keys(s.cells).sort(), ["0.1", "4.1"]);
  assert.ok(s.cells["0.1"][0] > 140 && s.cells["4.1"][0] > 140);
  // Revolutions: 800 rpm for ~150 s.
  assert.ok(Math.abs(s.cells["0.1"][1] - 2000) < 20);
  // Its longest visit, for the raw log.
  const [[a, b]] = s.cells["0.1"][2];
  assert.ok(a === 0 && b >= 149.5 && b <= 150.5);
});

test("a misfire counter becomes increases and revolutions per cell", () => {
  // Three misfires 100 s in (the counter is cumulative).
  const s = summarize(drive(0, { at: { ZAEHLER_AUSSETZERK_ZYL1: (t) => (t >= 100 ? 3 : 0) } }));
  const [inc, revs] = s.channels.ZAEHLER_AUSSETZERK_ZYL1.cells["2.2"];
  assert.equal(inc, 3);
  assert.ok(Math.abs(revs - 7500) < 20);                     // 1,500 rpm for 300 s
});

// Rows for scoreDrives: each drive's IMUL1 median in cell 1.2.
function rows(values, extra = {}) {
  return values.map((v, i) => ({ drive: `d${i}`, start: i * 1000, summary: {
    channels: { IMUL1: { name: "IMUL1", unit: "", cells: {
      "1.2": [100, v, v, v, v, v], ...(extra[i] || {}) } } } } }));
}

test("scores: no baseline for the first drives, then a robust z against the last ten", () => {
  const vals = [1.00, 1.01, 0.99, 1.00, 1.02, 0.98, 1.00, 1.01, 0.99, 1.00, 1.00, 1.08];
  const out = scoreDrives(rows(vals));
  assert.equal(out.length, vals.length);
  for (const i of [0, 1, 2]) assert.equal(out[i].scores.IMUL1[0], null);   // under 3 drives
  assert.equal(out[3].scores.IMUL1[1], 1);                                  // one cell scored
  const last = out[11].scores.IMUL1;
  // Baseline: d1-d10 (the last ten), median 1.00, MAD 0.01 -> spread 0.0148.
  assert.ok(Math.abs(last[0] - 0.08 / (1.4826 * 0.01)) < 1e-3, `score ${last[0]}`);
  assert.equal(out[11].level, 2);
  assert.equal(last[2], 1.08);                               // the value
  assert.ok(last[3] < 1 && last[4] > 1);                     // the normal band around 1.00
  const cell = out[11].cells.IMUL1["1.2"];
  assert.deepEqual(cell.slice(0, 2), [1.08, 1]);
  assert.equal(BASELINE_DRIVES, 10);
});

test("the spread has a floor, so a channel that never moves doesn't alert on a hair", () => {
  const out = scoreDrives(rows([1, 1, 1, 1, 1.005]));
  assert.ok(Math.abs(out[4].scores.IMUL1[0] - 0.5) < 1e-9);   // 0.005 over the 0.01 floor
  assert.equal(out[4].level, 0);
});

test("alerting drives and drives left out by hand stay out of later baselines", () => {
  const vals = [1, 1, 1, 1, 1.2, 1.2, 1.2];
  // d4 alerts, so d5's baseline is still d0-d3: it alerts too, and so on.
  const out = scoreDrives(rows(vals));
  assert.equal(out[6].level, 2);
  // Left out by hand: d1-d3 out, so d4-d6 have one baseline drive: unscored.
  const ex = scoreDrives(rows(vals), new Set(["d1", "d2", "d3"]));
  assert.equal(ex[4].scores.IMUL1[0], null);
  // Thin cells (under 10 samples) don't count.
  const thin = scoreDrives(rows([1, 1, 1, 1], { 3: { "2.2": [5, 9, 9, 9, 9, 9] } }));
  assert.equal(thin[3].scores.IMUL1[1], 1);
});

test("intake air is scored over the outside air; without an outside reading it isn't scored", () => {
  // Intake 15 °C over the outside air on a 30 °C day and a 15 °C night: the same.
  const hot = summarize(drive(0, { at: { RTANS: () => 45, TEMP_UMG: () => 30 } }));
  const cool = summarize(drive(0, { at: { RTANS: () => 30, TEMP_UMG: () => 15 } }));
  assert.equal(hot.channels.RTANS.cells["2.2"][2], 15);
  assert.equal(cool.channels.RTANS.cells["2.2"][2], 15);
  // The climate control's outside air when the DME's isn't read.
  const ihka = summarize(drive(0, { at: { RTANS: () => 40,
    "IHKA_BUS_IN_SIGNALE_KLIMA.BUS_IN_TEMP_AUSSEN": () => 22 } }));
  assert.equal(ihka.channels.RTANS.cells["2.2"][2], 18);
  assert.equal(summarize(drive(0, { at: { RTANS: () => 40 } })).channels.RTANS, undefined);
});

test("the outside air is one value a drive: coloured by its score, never a drive's level", () => {
  // Its average over the whole drive, cold start included.
  const s = summarize(drive(0, { at: { TEMP_UMG: (t) => (t < 150 ? 10 : 20) } }));
  assert.equal(s.channels.AMBIENT.cells.drive[1], 15);
  const temps = [20, 21, 19, 20, 20, 5];
  const out = scoreDrives(temps.map((v, i) => ({ drive: `d${i}`, start: i * 1000, summary: {
    channels: { AMBIENT: { name: "TEMP_UMG", unit: "°C", cells: { drive: [50, v, v, v, v, v] } },
      IMUL1: { name: "IMUL1", unit: "", cells: { "1.2": [100, 1, 1, 1, 1, 1] } } } } })));
  assert.ok(out[5].scores.AMBIENT[0] < -3);           // a cold night stands out on its row
  assert.equal(out[5].level, 0);                     // but the drive isn't flagged for it
  assert.equal(out[5].overall, 0);
});

test("the battery before the start: one value a drive, scored like a channel", () => {
  const s = summarize(drive(0), {}, { restV: 11.9 });
  assert.deepEqual(s.channels.BATT_REST.cells.drive.slice(0, 3), [1, 11.9, 11.9]);
  assert.equal(summarize(drive(0)).channels.BATT_REST, undefined);
  const volts = [12.4, 12.4, 12.4, 12.4, 11.6];
  const out = scoreDrives(volts.map((v, i) => ({ drive: `d${i}`, start: i * 1000, summary: {
    channels: { BATT_REST: { name: "IUIBS", unit: "V", cells: { drive: [1, v, v, v, v, v] } } } } })));
  assert.equal(out[4].level, 2);                     // 0.8 V down on a 0.1 V floor
});

test("the battery before the start comes from the drive's log, or the ignition-on log just before", async () => {
  const rows = [
    ["logger/2026-11-02_16-46-05.csv", "2026-11-02 16:46:05", { minutes: 0.07, battery_rest_v: 11.86 }],
    ["logger/2026-11-02_16-46-14.csv", "2026-11-02 16:46:14", { minutes: 9.78, battery_rest_v: null }],
    ["logger/2026-11-02_18-20-03.csv", "2026-11-02 18:20:03", { minutes: 5.1, battery_rest_v: null }],
    ["logger/2026-11-03_08-00-00.csv", "2026-11-03 08:00:00", { minutes: 0.1, battery_rest_v: 12.1 }],
    ["logger/2026-11-03_08-30-00.csv", "2026-11-03 08:30:00", { minutes: 5, battery_rest_v: null }],
    ["logger/2026-11-03_09-00-00.csv", "2026-11-03 09:00:00", { minutes: 5, battery_rest_v: 12.3 }],
  ];
  const env = { DB: { prepare: () => ({ bind: (drive) => ({ all: async () => ({ results: rows
    .filter(([f]) => f <= drive).reverse().slice(0, 2)
    .map(([file, start, data]) => ({ file, start, data: JSON.stringify(data) })) }) }) }) } };
  assert.equal(await restingBefore(env, "logger/2026-11-02_16-46-14.csv"), 11.86);  // 5 s after it
  assert.equal(await restingBefore(env, "logger/2026-11-02_18-20-03.csv"), null);   // the one before has none
  assert.equal(await restingBefore(env, "logger/2026-11-03_08-30-00.csv"), null);   // 30 min later
  assert.equal(await restingBefore(env, "logger/2026-11-03_09-00-00.csv"), 12.3);   // its own
  const none = { DB: { prepare: () => { throw new Error("no such table: drives"); } } };
  assert.equal(await restingBefore(none, "logger/x.csv"), null);
});
