// Checks on the pages' own scripts (loggerpage.js, healthpage.js,
// boardpage.js):
//   node --test "cloud/test/*.test.mjs"

import { test } from "node:test";
import assert from "node:assert/strict";

import { LOGGER_PAGE } from "../src/loggerpage.js";
import { HEALTH_PAGE } from "../src/healthpage.js";
import { BOARD_PAGE } from "../src/boardpage.js";

const PAGES = { LOGGER_PAGE, HEALTH_PAGE, BOARD_PAGE };

// A name that is both a function and a variable: the variable wins once it's
// set, and a call to the function then fails. The logger page's stored
// values stopped with "shown is not a function" (6 Nov): a units helper
// named like the count of channels on show. So no name is both, even where
// the variable is a local that would be harmless.
test("no name on a page is both a function and a variable", () => {
  for (const [name, page] of Object.entries(PAGES)) {
    const fns = new Set([...page.matchAll(/function\s+([A-Za-z_$][\w$]*)\s*\(/g)].map((m) => m[1]));
    const vars = [...page.matchAll(/(?:\bvar\s+|,\s*)([A-Za-z_$][\w$]*)\s*=(?!=)/g)].map((m) => m[1]);
    const both = [...new Set(vars.filter((v) => fns.has(v)))];
    assert.deepEqual(both, [], name);
  }
});

test("each page's scripts parse", () => {
  for (const [name, page] of Object.entries(PAGES)) {
    const scripts = [...page.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
    assert.ok(scripts.length > 0, name);
    for (const js of scripts) assert.doesNotThrow(() => new Function(js), name);
  }
});
