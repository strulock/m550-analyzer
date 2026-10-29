// drive.js (the Worker's port of series.build and history.summarize) against
// what Python makes of the same logs: cloud/test/parity, written by
// tests/test_cloud_parity.py.
//   node --test "cloud/test/*.test.mjs"

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { buildSeries, CHUNK, clockEpoch, isDriveLog, localStamp, loggerExtras, processLog, pyRound,
  readColumns, readLog } from "../src/drive.js";

const parity = new URL("./parity/", import.meta.url);
const expected = JSON.parse(readFileSync(new URL("expected.json", parity), "utf8"));

/** Equal, numbers to within a hair (libm and V8 can differ in the last bit). */
function close(actual, wanted, path = "") {
  if (typeof wanted === "number" && typeof actual === "number") {
    assert.ok(Math.abs(actual - wanted) <= 1e-9 * Math.max(1, Math.abs(wanted)),
      `${path}: ${actual} != ${wanted}`);
  } else if (Array.isArray(wanted)) {
    assert.ok(Array.isArray(actual), `${path}: not a list`);
    assert.equal(actual.length, wanted.length, `${path}: length`);
    wanted.forEach((w, i) => close(actual[i], w, `${path}[${i}]`));
  } else if (wanted && typeof wanted === "object") {
    assert.deepEqual(Object.keys(actual).sort(), Object.keys(wanted).sort(), `${path}: keys`);
    for (const k of Object.keys(wanted)) close(actual[k], wanted[k], `${path}.${k}`);
  } else {
    assert.equal(actual, wanted, path);
  }
}

/** Every chunk of a series, channel after channel. */
const chunksOf = (s) => [...s.channels()].flat();

/** Chunks as Python writes them: data as a list, no unit. */
const asPython = (chunks) => chunks.map(({ unit, json, ...c }) => ({ ...c, data: JSON.parse(json) }));

for (const name of Object.keys(expected)) {
  test(`parity with Python: ${name}`, () => {
    const bytes = readFileSync(new URL(name, parity));
    for (const input of [new Uint8Array(bytes), bytes.toString("utf8")]) {
      const { series, drive } = processLog(input, `logger/${name}`, 0);
      close(drive, expected[name].drive, "drive");
      close({ start: series.start, approx: series.approx, chunks: asPython(chunksOf(series)) },
        expected[name].series, "series");
    }
  });
}

test("a log without a Clock ends at the upload and is marked approximate", () => {
  const { series, drive } = processLog(
    "Time,INMOT\n0,700\n1,710\n2,720\n3,730\n4,740\n10,750\n", "logger/x.csv", 1000);
  assert.equal(series.approx, true);
  assert.equal(series.start, 990);
  assert.equal(chunksOf(series)[0].t0, 990);
  assert.deepEqual(JSON.parse(chunksOf(series)[0].json)[0], [0, 700]);
  // The start in the car's time zone, as the logs' own clocks give it.
  assert.equal(drive.start, "1969-12-31 18:16:30");
  const utc = processLog("Time,INMOT\n0,700\n1,710\n2,720\n3,730\n4,740\n10,750\n",
    "logger/x.csv", 1000, "UTC");
  assert.equal(utc.drive.start, "1970-01-01 00:16:30");
});

test("local stamps follow daylight saving", () => {
  assert.equal(localStamp(Date.UTC(2026, 9, 26, 1, 35) / 1000), "2026-10-25 20:35:00");   // CDT
  assert.equal(localStamp(Date.UTC(2026, 11, 1, 1, 35) / 1000), "2026-11-30 19:35:00");   // CST
});

test("logger extras: the tank level and odometer at each end, and whether the start is known", () => {
  // The level in the first and last minute (medians: the sender sloshes),
  // the odometer from a sweep file's first and last row.
  const fast = "Time,Clock,IKTFS\n" + [0, 20, 40, 60, 600, 620, 640, 660].map((t, k) =>
    `${t},2026-10-26 08:00:00.000-05:00,${[40, 41, 40, 39, 30, 31, 30, 30][k]}`).join("\n") + "\n";
  // Each half of the tank from whichever module's channel the log has (the
  // board's snapshots at the start, Park and engine-off).
  const sweep = "Time,Clock,EGS_KILOMETERSTAND,FUELLST_TANKH_L,BDC_TANK_FUELLSTAND_RECHTS\n" +
    "0,,111650,20,21\n300,,111655,,\n650,,111660,15,16\n";
  const x = loggerExtras([readLog(fast), readLog(sweep)]);
  assert.deepEqual(x, { start_approx: false, tank_start_l: 40, tank_end_l: 30,
    tank_left_start: 20, tank_left_end: 15, tank_right_start: 21, tank_right_end: 16,
    odometer_start_km: 111650, odometer_end_km: 111660,
    rate_fast: null, rate_medium: null, slow_pass_s: null });
  const none = loggerExtras(readLog("Time,INMOT\n0,700\n1,710\n"));
  assert.deepEqual(none, { start_approx: true, tank_start_l: null, tank_end_l: null,
    tank_left_start: null, tank_left_end: null, tank_right_start: null, tank_right_end: null,
    odometer_start_km: null, odometer_end_km: null,
    rate_fast: null, rate_medium: null, slow_pass_s: null });
});

test("logger extras: the rate each tier got, from the board's notes", () => {
  const log = "Time,Clock,INMOT,Event\n" +
    "0,2026-10-28 08:00:00.000-05:00,700,start\n" +
    "60,2026-10-28 08:01:00.000-05:00,700,stats 14.1 rows/s; tier rates: fast 14.10/s medium 2.00/s slow pass 40 s\n" +
    "120,2026-10-28 08:02:00.000-05:00,700,tier rates: fast 13.90/s medium 1.00/s slow pass 50 s\n" +
    "180,2026-10-28 08:03:00.000-05:00,700,tier rates: fast 9.00/s\n";
  const x = loggerExtras(readLog(log));
  assert.deepEqual([x.rate_fast, x.rate_medium, x.slow_pass_s], [12.33, 1.5, 45]);
});

test("a clock that fills in later dates the drive from its first row", () => {
  // The logger booted away from Wi-Fi: no Clock until it joined, 90 s in.
  const { series, drive } = processLog("Time,Clock,INMOT,Event\n" +
    "0.5,,700,start\n30.5,,710,\n60.5,,720,\n" +
    "90.5,2026-10-25 12:55:20.500-05:00,730,\n120.5,2026-10-25 12:55:50.500-05:00,740,\n",
    "logger/nt-00035-01.csv", 0);
  assert.equal(series.approx, false);
  assert.equal(series.start, Date.UTC(2026, 9, 25, 17, 53, 50) / 1000);   // 90.5 s before
  assert.equal(chunksOf(series)[0].t0, Date.UTC(2026, 9, 25, 17, 53, 50, 500) / 1000);
  assert.equal(drive.start, "2026-10-25 12:53:50");     // local, the first row's
});

test("too few rows: series kept, no drive", () => {
  const { series, drive } = processLog("Time,INMOT\n0,700\n1,710\n", "logger/x.csv", 0);
  assert.equal(chunksOf(series).length, 1);
  assert.equal(drive, null);
});

test("long channels split into pieces of CHUNK values", () => {
  let csv = "Time,Clock,INMOT\n";
  for (let i = 0; i < CHUNK * 2 + 3; i++) csv += `${i / 10},2026-10-25 07:00:00Z,${i}\n`;
  const { series } = processLog(csv, "logger/x.csv", 0);
  assert.deepEqual(chunksOf(series).map((p) => p.n), [CHUNK, CHUNK, 3]);
  assert.equal([...series.channels()].length, 1);
  assert.equal(chunksOf(series)[1].t0, pyRound(Date.UTC(2026, 9, 25, 7) / 1000 + CHUNK / 10, 3));
  assert.equal(chunksOf(series)[2].mean, CHUNK * 2 + 1);
  assert.equal(JSON.parse(chunksOf(series)[2].json).length, 3);
});

test("rounding ties to even on the exact value, as Python does", () => {
  assert.equal(pyRound(0.125, 2), 0.12);
  assert.equal(pyRound(0.375, 2), 0.38);
  assert.equal(pyRound(2.5), 2);
  assert.equal(pyRound(3.5), 4);
  assert.equal(pyRound(-2.5), -2);
  assert.equal(pyRound(2.675, 2), 2.67);            // just below the half as a double
  assert.equal(pyRound(1.0005, 3), 1.0);            // ditto
  assert.equal(pyRound(1761393600.0625, 3), 1761393600.062);
  assert.equal(pyRound(1761393600.0635, 3), 1761393600.063);    // below the half as a double
});

test("clocks: offset, Z, and none (unknown local time)", () => {
  assert.equal(clockEpoch("2026-10-19 07:42:50.250-05:00"), Date.UTC(2026, 9, 19, 12, 42, 50) / 1000 + 0.25);
  assert.equal(clockEpoch("2026-10-19T12:42:50Z"), Date.UTC(2026, 9, 19, 12, 42, 50) / 1000);
  assert.equal(clockEpoch("2026-10-19 07:42:50"), null);
  assert.equal(clockEpoch("07:42:50"), null);
});

test("series skip Event and Clock, keep numbers, drop text; quotes and CRLF", () => {
  const s = buildSeries(readColumns('Time,Clock,INMOT,GANG,Event\r\n' +
    '0,2026-10-19 07:42:50+00:00,700,P,"start, odometer 1 km"\r\n1,,710,D,\r\n'), 0);
  assert.deepEqual([...new Set(chunksOf(s).map((c) => c.name))], ["INMOT"]);
  assert.deepEqual(JSON.parse(chunksOf(s)[0].json).map((p) => p[1]), [700, 710]);
  assert.equal(s.approx, false);
});

test("the converter's slip and the gear ratio from the EGS's input and output speeds", () => {
  const s = buildSeries(readColumns("Time,Clock,INMOT,EGS_EINAUSGANG_DREHZAHL.EINGANGSDREHZAHL," +
    "EGS_EINAUSGANG_DREHZAHL.AUSGANGSDREHZAHL\n" +
    "0,2026-10-19 07:42:50+00:00,1627,908,909\n1,,1255,1254,585\n2,,812,,\n3,,700,0,0\n"), 0);
  const series = (name) => JSON.parse(chunksOf(s).find((c) => c.name === name).json).map((p) => p[1]);
  assert.deepEqual(series("TC_SLIP"), [719, 1, 700]);
  assert.deepEqual(series("GEAR_RATIO"), [0.999, 2.144]);
  const plain = readColumns("Time,INMOT\n0,700\n");
  assert.deepEqual(plain.header, ["Time", "INMOT"]);
});

test("drive logs are told from other CSVs by the header", () => {
  assert.equal(isDriveLog("Time,INMOT\n0,1\n"), true);
  assert.equal(isDriveLog(new TextEncoder().encode("﻿Time;INMOT\n")), true);
  assert.equal(isDriveLog("read_at,module,code\n"), false);
  assert.equal(isDriveLog(""), false);
});
