/**
 * The units the logger pages show: imperial (the default: °F, mph, miles,
 * psi, gallons, lb·ft) or metric (the car's own units, as the logs store
 * them). One choice for every page and visitor, kept in D1 and set on the
 * Board tab (GET/PUT /api/logger/display). Display only: the logs, the
 * summaries, health scores and the MCP tools keep the car's units.
 */

export const UNIT_SYSTEMS = ["imperial", "metric"];
export const DEFAULT_UNITS = "imperial";

export const PREFS_SCHEMA = [
  `CREATE TABLE IF NOT EXISTS prefs (key TEXT PRIMARY KEY, value TEXT, at TEXT, who TEXT)`,
];

export class UnitsError extends Error {}

/** A value in the car's unit as the pages show it: [value, unit]. `delta`:
 * a difference (a temperature over another), scaled without the offset.
 * Self-contained, so the pages embed it as text (Function.toString). */
export function displayUnit(v, unit, system, delta) {
  var MI = 0.621371192, PSI = 0.0145037738, GAL = 0.264172052;
  var T = { "°C": ["°F", 1.8, 32], "km/h": ["mph", MI], "km": ["mi", MI], "m": ["ft", 3.2808399],
    "cm": ["in", 0.393700787], "mm": ["in", 0.0393700787], "hPa": ["psi", PSI], "mbar": ["psi", PSI],
    "kPa": ["psi", PSI * 10], "bar": ["psi", PSI * 1000], "MPa": ["psi", PSI * 10000],
    "Nm": ["lb·ft", 0.737562149], "kg/h": ["lb/h", 2.20462262], "l": ["gal", GAL], "L": ["gal", GAL],
    "l/h": ["gal/h", GAL], "L/h": ["gal/h", GAL] };
  var t = system === "metric" ? null : T[unit];
  if (!t) return [v, unit];
  return [v == null ? v : v * t[1] + (delta ? 0 : t[2] || 0), t[0]];
}

export async function getUnits(env) {
  try {
    const r = ((await env.DB.prepare("SELECT value FROM prefs WHERE key = 'units'").all())
      .results || [])[0];
    return r && UNIT_SYSTEMS.includes(r.value) ? r.value : DEFAULT_UNITS;
  } catch {
    return DEFAULT_UNITS;                   // no table yet: the default
  }
}

export async function putUnits(env, body, who) {
  const units = body && body.units;
  if (!UNIT_SYSTEMS.includes(units)) throw new UnitsError(`units: ${UNIT_SYSTEMS.join(" or ")}`);
  await env.DB.prepare(`INSERT INTO prefs (key, value, at, who) VALUES ('units', ?, ?, ?)
    ON CONFLICT (key) DO UPDATE SET value = excluded.value, at = excluded.at, who = excluded.who`)
    .bind(units, new Date().toISOString(), who || null).run();
  return { units };
}

/** A page with the units filled in where it has "__UNITS__". */
export function withUnits(page, units) {
  return page.replace('"__UNITS__"', JSON.stringify(UNIT_SYSTEMS.includes(units) ? units : DEFAULT_UNITS));
}
