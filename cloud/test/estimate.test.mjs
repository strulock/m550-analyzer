// The logger channel page's rate estimates (estimate.js), against what the
// drive of 2026-10-26 measured.
import { test } from "node:test";
import assert from "node:assert/strict";

import { estimateRates } from "../src/estimate.js";
import { LOGGER_PAGE } from "../src/loggerpage.js";

// Close to the car's Default preset: identifiers by read tier per module.
const CAR = {
  "0x12": { fast: 10, medium: 30, slow: 1300 }, "0x13": { fast: 4, medium: 12, slow: 1300 },
  "0x18": { fast: 1, medium: 0, slow: 100 }, "0x63": { fast: 0, medium: 2, slow: 60 },
  "0x40": { fast: 0, medium: 0, slow: 250 }, "0x29": { fast: 0, medium: 0, slow: 60 },
  "0x01": { fast: 0, medium: 0, slow: 50 }, "0x76": { fast: 0, medium: 0, slow: 35 },
  "0x2C": { fast: 0, medium: 0, slow: 40 }, "0x19": { fast: 0, medium: 0, slow: 19 },
  "0x30": { fast: 0, medium: 0, slow: 20 }, "0x78": { fast: 0, medium: 0, slow: 25 },
};
// What the drive's stats line said about batch sizes (bank 1 aside).
const SIZES = { "0x18": 1, "0x63": 1, "0x19": 2, "0x29": 1, "0x2C": 1, "0x40": 1, "0x76": 1 };
const copy = (o) => JSON.parse(JSON.stringify(o));
const near = (got, want, tol, what) =>
  assert.ok(Math.abs(got - want) <= tol, `${what}: ${got.toFixed(2)}, wanted ${want} ± ${tol}`);

test("full batches: about 74 ms a cycle, as the drive's first minute", () => {
  const e = estimateRates(CAR, { batches: SIZES });
  near(e.cycleMs, 74, 4, "cycle ms");
  near(e.rowsPerS, 13.5, 0.7, "rows/s");
  assert.ok(e.medium["0x13"] > e.medium["0x12"]);                 // bank 2 has fewer fast reads
  near(e.medium["0x63"], 1, 0.3, "GPS reads/s");                  // about once a second
  assert.equal(e.mediumSlowest, "0x63");
});

test("bank 1 at six a request: slower rows, but medium keeps its share", () => {
  const e = estimateRates(CAR, { batches: { ...SIZES, "0x12": 6 } });
  assert.ok(e.rowsPerS < 10 && e.rowsPerS > 7.5, String(e.rowsPerS));
  near(e.medium["0x12"], 0.74, 0.15, "bank 1 medium/s");          // the old scheduler: about 0.3
});

test("more fast channels slow every row; more medium slows only medium", () => {
  const base = estimateRates(CAR, { batches: SIZES });
  const fast = copy(CAR);
  fast["0x12"].fast = 40;
  assert.ok(estimateRates(fast, { batches: SIZES }).rowsPerS < base.rowsPerS * 0.7);
  const medium = copy(CAR);
  medium["0x12"].medium = 200;
  const m = estimateRates(medium, { batches: SIZES });
  assert.equal(m.rowsPerS, base.rowsPerS);
  assert.ok(m.medium["0x12"] < base.medium["0x12"] / 4);
});

test("the slow pass is set by the slowest module, and measured answer times win", () => {
  const e = estimateRates(CAR, { batches: SIZES });
  assert.equal(e.slowest, "0x40");                                  // 250 identifiers, one at a time
  const slowBody = estimateRates(CAR, { batches: SIZES, answers: { "0x40": 1000 } });
  assert.ok(slowBody.slowPassS > e.slowPassS * 2);
  const none = estimateRates({ "0x12": { fast: 3, medium: 0, slow: 0 } }, {});
  near(none.rowsPerS, 1000 / (32 + 3 + 18), 0.01, "three fast reads");   // one request
  assert.ok(none.rowsPerS <= 20);                                   // never past the 50 ms floor
  assert.equal(none.slowest, null);
});

test("the page carries the estimator", () => {
  assert.match(LOGGER_PAGE, /function estimateRates\(mods, live\)/);
  assert.doesNotMatch(LOGGER_PAGE, /\$\{/);                          // nothing left uninterpolated
});

// Wrangler bundles with esbuild keeping names, which wraps the functions
// inside the embedded ones in __name(fn, "name"); the page defines it, or
// it stops with "__name is not defined" (seen live, 2026-10-27). Here the
// first script runs with a function wrapped that way, as the bundle has it.
test("the page's embedded functions run as the bundler leaves them", async () => {
  const vm = await import("node:vm");
  const first = LOGGER_PAGE.match(/<script>([\s\S]*?)<\/script>/)[1];
  const wrapped = first + "\nvar probe = /* @__PURE__ */ __name(function () { return 1; }, \"probe\");";
  const ctx = { CAR };
  vm.runInNewContext(wrapped + "\nthis.rows = estimateRates(CAR, { batches: {} }).rowsPerS;" +
    "\nthis.probe = probe(); this.per = perDrive(\"IKTFS\");", ctx);
  assert.ok(ctx.rows > 0);
  assert.equal(ctx.probe, 1);
  assert.equal(ctx.per, true);
});
