/**
 * Tanks of fuel, the way the trip meter is reset at every fill-up: the
 * logger's drives in order, a new tank wherever the tank level at a drive's
 * start is well above where the last drive left it. Each tank's distance,
 * fuel and economy.
 *
 * From each logger drive row (drive.js loggerExtras): tank_start_l and
 * tank_end_l (the DME's level, whole litres), odometer_start_km and
 * odometer_end_km, and the drive's own km and fuel_l (the DMEs' fuel flow).
 * Distance is the odometer's difference across the tank where both ends
 * have one, else the drives' km added up. Fuel is the fuel flow added up
 * where every drive has it (the meter the economy figures use), else the
 * level's fall.
 */

export const FILL_MIN_L = 8;                 // a rise this big between drives is a fill-up
const KM_PER_MILE = 1.609344;
const L_PER_GALLON = 3.785411784;

const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);
const round = (v, n) => (v === null ? null : Number(v.toFixed(n)));

/** [{start, end, drives, filled_l, level_start_l, level_end_l, km, miles,
 * fuel_l, gallons, fuel_from, mpg, l_per_100km, partial, open}], oldest
 * first, from drive rows in any order. `partial`: the first tank, whose
 * fill-up came before the logs; `open`: the tank in the car now. */
export function tanks(drives) {
  const rows = drives.filter((d) => d && typeof d.start === "string" &&
    (num(d.tank_start_l) !== null || num(d.tank_end_l) !== null))
    .sort((a, b) => (a.start < b.start ? -1 : a.start > b.start ? 1 : 0));
  const out = [];
  let cur = null, lastLevel = null;
  for (const d of rows) {
    const startLevel = num(d.tank_start_l) ?? num(d.tank_end_l);
    const filled = lastLevel !== null && startLevel - lastLevel >= FILL_MIN_L
      ? startLevel - lastLevel : null;
    if (!cur || filled !== null) {
      cur = { drives: [], filled_l: filled, partial: !out.length && filled === null };
      out.push(cur);
    }
    cur.drives.push(d);
    lastLevel = num(d.tank_end_l) ?? startLevel;
  }
  return out.map((t, i) => summary(t, i === out.length - 1));
}

function summary(t, open) {
  const ds = t.drives, first = ds[0], last = ds[ds.length - 1];
  const levelStart = num(first.tank_start_l) ?? num(first.tank_end_l);
  const levelEnd = num(last.tank_end_l) ?? num(last.tank_start_l);
  const odoStart = num(first.odometer_start_km) ?? num(first.odometer_end_km);
  const odoEnd = num(last.odometer_end_km) ?? num(last.odometer_start_km);
  let km = odoStart !== null && odoEnd !== null && odoEnd >= odoStart ? odoEnd - odoStart : null;
  if (km === null || (km === 0 && ds.some((d) => num(d.km)))) {
    km = ds.reduce((s, d) => s + (num(d.km) ?? 0), 0);
  }
  let fuel = null, from = null;
  if (ds.every((d) => num(d.fuel_l) !== null)) {
    fuel = ds.reduce((s, d) => s + d.fuel_l, 0);
    from = "flow";
  } else if (levelStart !== null && levelEnd !== null && levelStart > levelEnd) {
    fuel = levelStart - levelEnd;
    from = "level";
  }
  const miles = km / KM_PER_MILE, gallons = fuel === null ? null : fuel / L_PER_GALLON;
  return {
    start: first.start, end: last.start, drives: ds.length,
    filled_l: round(t.filled_l, 1), level_start_l: levelStart, level_end_l: levelEnd,
    km: round(km, 1), miles: round(miles, 1), fuel_l: round(fuel, 2),
    gallons: round(gallons, 2), fuel_from: from,
    mpg: gallons && miles >= 1 ? round(miles / gallons, 1) : null,
    l_per_100km: fuel && km >= 1 ? round(fuel / km * 100, 2) : null,
    partial: t.partial, open,
  };
}
