// tanks.js: fill-ups from the logger's drives, and each tank's figures.
//   node --test "cloud/test/*.test.mjs"

import { test } from "node:test";
import assert from "node:assert/strict";

import { tanks } from "../src/tanks.js";

const drive = (start, tank0, tank1, odo0, odo1, km, fuel) => ({ start, tank_start_l: tank0,
  tank_end_l: tank1, odometer_start_km: odo0, odometer_end_km: odo1, km, fuel_l: fuel });

test("a rise in the level between drives starts a tank; the last is open", () => {
  const t = tanks([
    drive("2026-10-25 12:00:00", 20, 15, 1000, 1050, 50, 5.2),
    drive("2026-10-25 08:00:00", 25, 20, 960, 1000, 40, 4.8),   // any order
    drive("2026-10-26 08:00:00", 66, 60, 1052, 1100, 48, 5.0),  // filled: 15 -> 66
    drive("2026-10-26 18:00:00", 61, 57, 1100, 1130, 30, 3.1),  // 1 l up: the sender, not a fill
  ]);
  assert.equal(t.length, 2);
  assert.deepEqual(t.map((k) => [k.start, k.end, k.drives, k.partial, k.open]), [
    ["2026-10-25 08:00:00", "2026-10-25 12:00:00", 2, true, false],
    ["2026-10-26 08:00:00", "2026-10-26 18:00:00", 2, false, true]]);
  const [a, b] = t;
  assert.equal(a.km, 90);                                    // the odometer, 960 -> 1050
  assert.equal(a.fuel_l, 10);                                // the fuel flow added up
  assert.equal(a.fuel_from, "flow");
  assert.equal(a.mpg, Number(((90 / 1.609344) / (10 / 3.785411784)).toFixed(1)));
  assert.equal(b.filled_l, 51);
  assert.deepEqual([b.level_start_l, b.level_end_l, b.km], [66, 57, 78]);
  assert.equal(b.l_per_100km, Number((8.1 / 78 * 100).toFixed(2)));
});

test("no fuel flow: the level's fall; no odometer: the drives' km", () => {
  const [t] = tanks([
    drive("2026-10-25 08:00:00", 60, 50, null, null, 80, null),
    drive("2026-10-25 12:00:00", 50, 40, null, null, 82, null),
  ]);
  assert.deepEqual([t.km, t.fuel_l, t.fuel_from], [162, 20, "level"]);
  assert.equal(t.mpg, Number(((162 / 1.609344) / (20 / 3.785411784)).toFixed(1)));
});

test("drives without the level are left out; none gives no tanks", () => {
  assert.deepEqual(tanks([drive("2026-10-25 08:00:00", null, null, 1, 2, 1, 0.1)]), []);
  assert.deepEqual(tanks([]), []);
  const [t] = tanks([drive("2026-10-25 08:00:00", 30, 29, 1, 1.5, 0.5, 0.1)]);
  assert.equal(t.mpg, null);                                 // under a mile: no economy
});
