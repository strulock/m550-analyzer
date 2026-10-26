/**
 * Sampling rates the logger board should reach with a selection of
 * channels: a model of its scheduler (firmware/lib/logger/plan.h), fitted to
 * the drive of 2026-10-26. The logger channel page shows it, so picking
 * too much shows as slower rows. Estimates, not promises.
 *
 * Fitted: a cycle was about 74 ms with bank 1's 10 fast identifiers in one
 * request of 24, and 94 ms with them in two of 6, so a request to a DME
 * costs about 32 ms plus 1 ms an identifier, and a cycle about 18 ms more.
 * The head unit's GPS came about once a second, one of two medium
 * identifiers it alternates with slow, while at most six requests to such
 * modules were out at once: about 170 ms an answer. The board's check-ins
 * report its real batch sizes and answer times; those win.
 *
 * Plain function, no closures over module scope: the page embeds its source.
 */
export function estimateRates(mods, live) {
  var REQ_MS = 32, ID_MS = 1, CYCLE_MS = 18, MIN_CYCLE_MS = 50, MAX_BATCH = 24;
  var ANSWER_MS = 170, SLOW_ONLY_PER_CYCLE = 4, MAX_LATER = 6;
  live = live || {};
  var batches = live.batches || {}, answers = live.answers || {};
  var batch = function (m) { return batches[m] || MAX_BATCH; };
  var answer = function (m) { return answers[m] || ANSWER_MS; };
  var names = Object.keys(mods);
  // What a module's last request carries besides fast reads, with `left`
  // slots: medium its share (a quarter of a request) or half, whichever is
  // more, slow at least one where there's room beyond the share.
  function split(room, left, medium, slow) {
    var share = medium ? Math.min(medium, Math.ceil(room / 4)) : 0;
    var med = 0;
    if (medium && slow) {
      med = left === 1 ? 0.5 : left > share ? Math.min(left - 1, Math.max(share, Math.ceil(left / 2))) : left;
    } else if (medium) med = left;
    med = Math.min(med, medium);
    var slw = slow ? Math.min(slow, left - med) : 0;
    return { med: med, slow: slw };
  }
  var per = {}, cycle = 0;
  names.forEach(function (m) {
    var n = mods[m];
    if (!n.fast) return;
    var room = batch(m), fastReq = Math.ceil(n.fast / room);
    var left = room - (n.fast - (fastReq - 1) * room);
    var share = n.medium ? Math.min(n.medium, Math.ceil(room / 4)) : 0;
    // Another request: every cycle for medium's share, every other cycle
    // for slow when nothing's left beyond the share. Averaged over two.
    var variants = left < share ? [true, true] : n.slow && left <= share ? [true, false] : [false, false];
    var reqs = 0, ids = 0, med = 0, slw = 0;
    variants.forEach(function (extra) {
      var s = split(room, extra ? room : left, n.medium, n.slow);
      reqs += fastReq + (extra ? 1 : 0);
      ids += n.fast + s.med + s.slow;
      med += s.med;
      slw += s.slow;
    });
    var ms = (reqs * REQ_MS + ids * ID_MS) / 2;
    per[m] = { waited: true, ms: ms, med: med / 2, slow: slw / 2 };
    cycle = Math.max(cycle, ms);
  });
  var cycleMs = Math.max(MIN_CYCLE_MS, cycle + CYCLE_MS);
  var rows = 1000 / cycleMs;
  // Modules without fast reads: one request in flight each, answered in
  // their own time; slow-only ones take turns, a few a cycle; at most
  // MAX_LATER in flight in all.
  var slowOnly = names.filter(function (m) { return !mods[m].fast && !mods[m].medium && mods[m].slow; });
  var later = 0;
  names.forEach(function (m) {
    var n = mods[m];
    if (n.fast || !(n.medium || n.slow)) return;
    var every = Math.max(answer(m), cycleMs);
    if (!n.medium) every = Math.max(every, cycleMs * slowOnly.length / SLOW_ONLY_PER_CYCLE);
    var room = batch(m), s = split(room, room, n.medium, n.slow);
    per[m] = { waited: false, perS: 1000 / every, med: s.med, slow: s.slow };
    later += (1000 / every) * answer(m) / 1000;           // requests in flight, on average
  });
  var squeeze = later > MAX_LATER ? MAX_LATER / later : 1;
  var out = { rowsPerS: rows, cycleMs: cycleMs, medium: {}, slowPassS: 0, slowest: null,
    mediumMin: null, mediumSlowest: null };
  names.forEach(function (m) {
    var n = mods[m], p = per[m];
    if (!p) return;
    var reqPerS = p.waited ? rows : p.perS * squeeze;
    if (n.medium) {
      var r = reqPerS * p.med / n.medium;
      out.medium[m] = r;
      if (out.mediumMin === null || r < out.mediumMin) { out.mediumMin = r; out.mediumSlowest = m; }
    }
    if (n.slow) {
      var pass = p.slow > 0 ? n.slow / (reqPerS * p.slow) : Infinity;
      if (pass > out.slowPassS) { out.slowPassS = pass; out.slowest = m; }
    }
  });
  return out;
}
