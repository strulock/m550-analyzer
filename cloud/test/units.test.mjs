// Tests for the pages' display units (units.js):
//   node --test "cloud/test/*.test.mjs"

import { test } from "node:test";
import assert from "node:assert/strict";

import { displayUnit, withUnits } from "../src/units.js";

const near = (got, want) => assert.ok(Math.abs(got - want) < 1e-6, `${got} vs ${want}`);

test("imperial: temperatures, speeds, distances, pressures, volumes", () => {
  const [f, u] = displayUnit(100, "°C", "imperial");
  near(f, 212);
  assert.equal(u, "°F");
  near(displayUnit(-40, "°C", "imperial")[0], -40);
  near(displayUnit(100, "km/h", "imperial")[0], 62.1371192);
  assert.equal(displayUnit(100, "km/h", "imperial")[1], "mph");
  near(displayUnit(1.609344, "km", "imperial")[0], 1);
  near(displayUnit(1000, "hPa", "imperial")[0], 14.5037738);
  near(displayUnit(1, "bar", "imperial")[0], 14.5037738);
  near(displayUnit(100, "kPa", "imperial")[0], 14.5037738);
  near(displayUnit(3.785411784, "l", "imperial")[0], 1);
  assert.equal(displayUnit(1, "l/h", "imperial")[1], "gal/h");
  near(displayUnit(25.4, "mm", "imperial")[0], 1);
});

test("a difference scales without the offset", () => {
  near(displayUnit(10, "°C", "imperial", true)[0], 18);
  near(displayUnit(10, "°C", "imperial")[0], 50);
});

test("metric, other units and no value pass through", () => {
  assert.deepEqual(displayUnit(90, "°C", "metric"), [90, "°C"]);
  assert.deepEqual(displayUnit(12.5, "V", "imperial"), [12.5, "V"]);
  assert.deepEqual(displayUnit(3, "per 1,000 rev", "imperial"), [3, "per 1,000 rev"]);
  assert.deepEqual(displayUnit(null, "°C", "imperial"), [null, "°F"]);
  assert.deepEqual(displayUnit(5, "", "imperial"), [5, ""]);
});

test("the page text gets the choice, imperial for anything else", () => {
  assert.equal(withUnits('var UNITS = "__UNITS__";', "metric"), 'var UNITS = "metric";');
  assert.equal(withUnits('var UNITS = "__UNITS__";', "<x>"), 'var UNITS = "imperial";');
});

test("displayUnit stands alone, as the pages embed it", () => {
  const f = new Function(`${displayUnit.toString()}; return displayUnit;`)();
  near(f(0, "°C", "imperial")[0], 32);
});
