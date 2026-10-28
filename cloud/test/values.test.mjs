// What the logger page flags as a wrong-looking value (values.js oddValue).
import { test } from "node:test";
import assert from "node:assert/strict";

import { oddValue } from "../src/values.js";
import { LOGGER_PAGE } from "../src/loggerpage.js";

const ch = (type, unit = "", mul = 1, div = 1, add = 0) => ({ type, unit, mul, div, add });

test("a raw value at its type's limit is flagged, scaled or not", () => {
  assert.match(oddValue(255, ch("u8")), /0xFF/);
  assert.match(oddValue(65535, ch("u16", "mV")), /0xFFFF/);
  assert.match(oddValue(4294967295, ch("u32", "km")), /0xFFFFFFFF/);
  assert.match(oddValue(6553.5, ch("u16", "", 1, 10)), /0xFFFF/);          // raw / 10
  assert.match(oddValue(215, ch("u8", "°C", 1, 1, -40)), /0xFF/);          // raw - 40
  assert.match(oddValue(32767, ch("s16")), /limit/);
  assert.match(oddValue(-128, ch("s8")), /limit/);
  assert.equal(oddValue(254, ch("u8")), null);
  assert.equal(oddValue(-1, ch("s16")), null);
  assert.equal(oddValue(255.5, ch("u16")), null);                          // not a raw step
  assert.equal(oddValue(3.4e38, ch("f32")), null);                         // floats: no limit test
});

test("a value no car could have for its unit is flagged", () => {
  assert.match(oddValue(-273.14, ch("s16", "°C", 0.01)), /-60 to 1100 °C/);
  assert.match(oddValue(400, ch("u16", "V")), /0 to 60 V/);
  assert.equal(oddValue(92, ch("u8", "°C", 1, 1, -48)), null);
  assert.equal(oddValue(14.1, ch("u16", "V", 0.001)), null);
  assert.equal(oddValue(1e9, ch("u32", "DATA")), null);                    // no range for it
  assert.equal(oddValue(null, ch("u8")), null);
});

test("the page carries the test and asks for the values", () => {
  assert.match(LOGGER_PAGE, /function oddValue\(v, ch\)/);
  assert.match(LOGGER_PAGE, /\/api\/logger\/values/);
  assert.doesNotMatch(LOGGER_PAGE, /\$\{/);
});
