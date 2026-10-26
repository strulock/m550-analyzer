// What the logger asks the car for: the channel table grouped into one read
// per (module, identifier), and reads batched into multi-identifier
// 0x22 requests (the spike proved eight at a time on the DMEs, 2.6x
// n63read's rate; MAX_BATCH asks for more). No Arduino code.
//
// A module that can't take a batch (says it's too long or badly formed,
// answers out of line, or keeps timing out) halves its batch size, down to
// one identifier a request; a read that keeps failing, or that the module
// refuses, is rested and tried again now and then, so one dead identifier
// doesn't cost a request every cycle.
#pragma once
#include <stdint.h>

#include <algorithm>
#include <map>
#include <vector>

#include "channels.h"

namespace logger {

struct Read {
  uint8_t module;
  uint16_t did;
  uint16_t size;               // reply data for this identifier, 0 if not fixed
  Tier tier;
  std::vector<uint32_t> channels;   // indices into Table::channels
  uint8_t failures = 0;        // in a row
  uint32_t restUntil = 0;      // ms; skipped until then
};

constexpr size_t MAX_BATCH = 24;         // identifiers per request, to start (HSFZ fits 31)
constexpr size_t MAX_REPLY = 3000;       // bytes a batch may expect back (hsfz buffer 4096)
constexpr uint8_t FAILURES_TO_REST = 3;
constexpr uint32_t REST_MS = 60000;
constexpr uint32_t OUT_OF_RANGE_REST_MS = 600000;   // an identifier the module says it hasn't
constexpr uint8_t TIMEOUTS_TO_SHRINK = 2;           // in a row, before a module's batches shrink

class Plan {
 public:
  std::vector<Read> reads;

  void build(const Table& t) {
    reads.clear();
    // One read per identifier, at the fastest tier any of its channels is
    // in: a record's fields can sit in different tiers (the head unit's
    // speed record: GPS speed medium, the rest slow), and a request naming
    // one identifier twice can't be matched up with its answer.
    std::map<uint32_t, size_t> at;           // (module, did) -> read
    for (uint32_t i = 0; i < t.channels.size(); i++) {
      const Channel& c = t.channels[i];
      uint32_t key = ((uint32_t)c.module << 16) | c.did;
      auto it = at.find(key);
      if (it == at.end()) {
        Read r;
        r.module = c.module; r.did = c.did; r.size = c.didSize; r.tier = c.tier;
        at[key] = reads.size();
        reads.push_back(r);
        it = at.find(key);
      }
      Read& r = reads[it->second];
      if (c.tier < r.tier) r.tier = c.tier;
      r.channels.push_back(i);
    }
    std::sort(reads.begin(), reads.end(), [](const Read& a, const Read& b) {
      if (a.tier != b.tier) return a.tier < b.tier;
      if (a.module != b.module) return a.module < b.module;
      return a.did < b.did;
    });
  }

  // How many identifiers this module takes in one request: MAX_BATCH,
  // halved each time it fails a batch of that size, down to one.
  size_t batchFor(uint8_t module) const {
    auto it = batch_.find(module);
    return it == batch_.end() ? MAX_BATCH : it->second;
  }
  // A batch of `size` was too big for the module: from now on at most half
  // of it (and never more than half the current size).
  void smallerBatches(uint8_t module, size_t size) {
    size_t now = std::min(batchFor(module), size);
    if (now > 1) batch_[module] = now / 2;
  }

  // A module that didn't answer: how many times in a row now. Any answer
  // starts the count again.
  uint8_t timedOut(uint8_t module) { return ++timeouts_[module]; }
  void moduleAnswered(uint8_t module) { timeouts_[module] = 0; }

  // The next request from `reads[from..]` of one tier: consecutive reads of
  // one module, up to its batch size (a read of unknown size goes alone).
  // Returns the read indices; `from` moves past them. Rested reads are
  // skipped. Empty when the tier has no more.
  std::vector<size_t> next(Tier tier, size_t& from, uint32_t now) const {
    std::vector<size_t> out;
    size_t bytes = 0;
    for (; from < reads.size(); from++) {
      const Read& r = reads[from];
      if (r.tier != tier) { if (out.empty()) continue; break; }
      if (r.restUntil && (int32_t)(r.restUntil - now) > 0) continue;
      if (!out.empty()) {
        const Read& first = reads[out[0]];
        if (r.module != first.module || first.size == 0 || r.size == 0 ||
            out.size() >= batchFor(first.module) || bytes + 2 + r.size > MAX_REPLY) {
          break;
        }
      }
      out.push_back(from);
      bytes += 2 + r.size;
      if (r.size == 0) { from++; break; }
    }
    return out;
  }

  void failed(size_t i, uint32_t now) {
    Read& r = reads[i];
    if (++r.failures >= FAILURES_TO_REST) {
      r.restUntil = (now + REST_MS) | 1;
      r.failures = 0;
    }
  }
  void answered(size_t i) { reads[i].failures = 0; reads[i].restUntil = 0; }
  void rest(size_t i, uint32_t now, uint32_t ms) {
    reads[i].restUntil = (now + ms) | 1;
    reads[i].failures = 0;
  }

  // The first index of a tier (reads are sorted by tier).
  size_t tierStart(Tier tier) const {
    size_t i = 0;
    while (i < reads.size() && reads[i].tier < tier) i++;
    return i;
  }

 private:
  std::map<uint8_t, size_t> batch_;
  std::map<uint8_t, uint8_t> timeouts_;
};

// One request per module per cycle, so a cycle is one wave of requests in
// flight together (each module answers one at a time; different modules in
// parallel). A module's request carries all its fast reads, then the next
// medium ones and the next slow ones in rotation, up to its batch size.
// Modules with only slow reads take turns, `slowOnlyMax` a cycle. Reads of
// unknown reply size go alone, one a cycle, when their module isn't asked
// otherwise. A module still answering an earlier request (`busy`) is
// skipped; batches to ask again (halves of one the module couldn't take)
// go out on its next turn.
//
// Periodic reads (once a drive, daily, weekly, monthly) are asked once each,
// those of tiers in `due` only, once openOnce() is called (a minute into the
// drive, as the fault reads): ahead of slow in a module's request, in the
// slow-only turns too.
class Scheduler {
 public:
  void build(const Plan& p, uint32_t due = ~0u) {
    queues_.clear();
    singles_.clear();
    onceSingles_.clear();
    singleAt_ = slowOnlyAt_ = 0;
    onceOpen_ = onceReported_ = false;
    onceCount_ = 0;
    for (size_t i = 0; i < p.reads.size(); i++) {
      const Read& r = p.reads[i];
      if (periodic(r.tier)) {
        if (!(due & (1u << r.tier))) continue;
        onceCount_++;
        if (r.size == 0) onceSingles_.push_back(i);
        else queue(r.module).once.push_back(i);
        continue;
      }
      if (r.size == 0) { singles_.push_back(i); continue; }
      Queue& q = queue(r.module);
      (r.tier == FAST ? q.fast : r.tier == MEDIUM ? q.medium : q.slow).push_back(i);
    }
    for (Queue& q : queues_) q.slowRound = q.slow.empty();
  }

  // Start asking the periodic reads.
  void openOnce() { onceOpen_ = true; }
  // How many periodic reads this drive asks.
  size_t onceCount() const { return onceCount_; }
  // True once, when every periodic read has been asked (or was resting).
  bool onceDone() {
    if (!onceOpen_ || onceReported_ || !onceCount_ || !onceSingles_.empty()) return false;
    for (const Queue& q : queues_) if (!q.once.empty()) return false;
    onceReported_ = true;
    return true;
  }
  // Whether any module has slow reads (a sweep row a pass).
  bool hasSlow() const {
    for (const Queue& q : queues_) if (!q.slow.empty()) return true;
    return false;
  }

  // Whether the logger waits for this module's answers each cycle (it has
  // fast reads); the others are collected when they come.
  bool waits(uint8_t module) const {
    for (const Queue& q : queues_) if (q.module == module) return !q.fast.empty();
    return false;
  }

  // Ask these again on the module's next turn.
  void retry(const Plan& p, const std::vector<size_t>& batch) {
    if (!batch.empty()) queue(p.reads[batch[0]].module).retries.push_back(batch);
  }

  // This cycle's requests: one per module (more for a module whose fast
  // reads or retries don't fit in one), none to a busy module.
  std::vector<std::vector<size_t>> cycle(const Plan& p, uint32_t now,
                                         const std::vector<uint8_t>& busy = {},
                                         size_t slowOnlyMax = 1) {
    std::vector<std::vector<size_t>> out;
    std::vector<uint8_t> asked;
    auto in = [](const std::vector<uint8_t>& v, uint8_t m) {
      return std::find(v.begin(), v.end(), m) != v.end();
    };
    for (Queue& q : queues_) {
      if (in(busy, q.module)) continue;
      // Retries: a module the logger waits for sends them alongside its
      // usual request; any other sends one instead of it.
      while (!q.retries.empty()) {
        std::vector<size_t> b;
        for (size_t i : q.retries.front()) if (!resting(p.reads[i], now)) b.push_back(i);
        q.retries.erase(q.retries.begin());
        if (b.empty()) continue;
        out.push_back(b);
        asked.push_back(q.module);
        if (q.fast.empty()) break;
      }
      if (q.fast.empty() && in(asked, q.module)) continue;
      if (q.fast.empty() && q.medium.empty()) continue;   // slow and periodic only: below
      std::vector<size_t> b;
      size_t bytes = 0, room = p.batchFor(q.module);
      for (size_t i : q.fast) {                    // all of them: more requests if need be
        if (add(p, i, now, b, bytes, room)) continue;
        out.push_back(b);
        b.clear();
        bytes = 0;
        add(p, i, now, b, bytes, room);
      }
      // Fast reads filled the request: every other cycle another one for
      // medium and slow, so they don't stop.
      if (b.size() >= room && (!q.medium.empty() || !q.slow.empty()) &&
          (q.extraTurn = !q.extraTurn)) {
        out.push_back(b);
        b.clear();
        bytes = 0;
      }
      // What's left: medium gets at most half when there's slow to read
      // (one slot left: medium and slow take turns), slow the rest.
      size_t left = room > b.size() ? room - b.size() : 0;
      size_t forMedium = left;
      if (!q.slow.empty() && !q.medium.empty()) {
        forMedium = left == 1 ? ((q.slowTurn = !q.slowTurn) ? 0 : 1) : (left + 1) / 2;
      }
      take(p, q.medium, q.mediumAt, now, b, bytes, b.size() + forMedium, nullptr);
      if (onceOpen_) takeOnce(p, q.once, now, b, bytes, room);
      take(p, q.slow, q.slowAt, now, b, bytes, room, &q.slowRound);
      if (!b.empty()) { out.push_back(b); asked.push_back(q.module); }
    }
    // A periodic read of unknown size, alone: ahead of the slow-only turns
    // (which could take its module every cycle), or as an extra request to
    // a module the logger waits for.
    bool alone = false;
    for (size_t k = 0; onceOpen_ && k < onceSingles_.size(); k++) {
      size_t i = onceSingles_[k];
      const Read& r = p.reads[i];
      if (in(busy, r.module) || (in(asked, r.module) && !waits(r.module))) continue;
      onceSingles_.erase(onceSingles_.begin() + k);
      if (resting(r, now)) { k--; continue; }
      out.push_back({i});
      if (!in(asked, r.module)) asked.push_back(r.module);
      alone = true;
      break;
    }
    // Slow-only modules in turn, up to slowOnlyMax not otherwise asked.
    size_t picked = 0;
    for (size_t n = 0; n < queues_.size() && picked < slowOnlyMax; n++) {
      Queue& q = queues_[slowOnlyAt_++ % queues_.size()];
      bool once = onceOpen_ && !q.once.empty();
      if (!q.fast.empty() || !q.medium.empty() || (q.slow.empty() && !once)) continue;
      if (in(busy, q.module) || in(asked, q.module)) continue;
      std::vector<size_t> b;
      size_t bytes = 0, room = p.batchFor(q.module);
      if (once) takeOnce(p, q.once, now, b, bytes, room);
      take(p, q.slow, q.slowAt, now, b, bytes, room, &q.slowRound);
      if (!b.empty()) { out.push_back(b); asked.push_back(q.module); picked++; }
    }
    for (size_t tries = 0; !alone && tries < singles_.size(); tries++) {   // one alone, module free
      size_t i = singles_[singleAt_++ % singles_.size()];
      const Read& r = p.reads[i];
      if (in(asked, r.module) || in(busy, r.module) || resting(r, now)) continue;
      out.push_back({i});
      break;
    }
    return out;
  }

  // True once every module's slow reads have all come round since the last
  // true (one row of the sweep file); a module without slow reads counts.
  bool slowPassDone() {
    for (const Queue& q : queues_) if (!q.slowRound) return false;
    for (Queue& q : queues_) q.slowRound = q.slow.empty();
    return true;
  }

 private:
  struct Queue {
    uint8_t module = 0;
    std::vector<size_t> fast, medium, slow;
    std::vector<size_t> once;            // periodic reads still to ask this drive
    std::vector<std::vector<size_t>> retries;
    size_t mediumAt = 0, slowAt = 0;
    bool slowRound = false;              // every slow read taken since the last pass
    bool slowTurn = false, extraTurn = false;
  };

  Queue& queue(uint8_t module) {
    for (Queue& q : queues_) if (q.module == module) return q;
    queues_.push_back(Queue{});
    queues_.back().module = module;
    return queues_.back();
  }

  static bool resting(const Read& r, uint32_t now) {
    return r.restUntil && (int32_t)(r.restUntil - now) > 0;
  }

  // Add read i if there's room: true if added or skipped (resting), false
  // when the request is full.
  static bool add(const Plan& p, size_t i, uint32_t now, std::vector<size_t>& b, size_t& bytes,
                  size_t room) {
    const Read& r = p.reads[i];
    if (resting(r, now)) return true;
    if (b.size() >= room || bytes + 2 + r.size > MAX_REPLY) return false;
    b.push_back(i);
    bytes += 2 + r.size;
    return true;
  }

  // From `list` at `at`, in rotation, as many as fit, each at most once a
  // cycle; `round` set when the list comes back to its start.
  static void take(const Plan& p, const std::vector<size_t>& list, size_t& at, uint32_t now,
                   std::vector<size_t>& b, size_t& bytes, size_t room, bool* round) {
    for (size_t n = 0; n < list.size(); n++) {
      if (at >= list.size()) { at = 0; if (round) *round = true; }
      if (!add(p, list[at], now, b, bytes, room)) return;
      if (++at >= list.size()) { at = 0; if (round) *round = true; }
    }
  }

  // From the front of `list`, as many as fit, each taken off it (a resting
  // one too: it isn't waited for).
  static void takeOnce(const Plan& p, std::vector<size_t>& list, uint32_t now,
                       std::vector<size_t>& b, size_t& bytes, size_t room) {
    size_t k = 0;
    while (k < list.size()) {
      if (resting(p.reads[list[k]], now)) { k++; continue; }
      if (!add(p, list[k], now, b, bytes, room)) break;
      k++;
    }
    list.erase(list.begin(), list.begin() + k);
  }

  std::vector<Queue> queues_;
  std::vector<size_t> singles_, onceSingles_;
  size_t singleAt_ = 0, slowOnlyAt_ = 0;
  bool onceOpen_ = false, onceReported_ = false;
  size_t onceCount_ = 0;
};

// Whether a periodic tier is due this drive: `last` when it was last read
// and `now`, in epoch seconds (0: never, or the clock is unknown: due),
// `offsetMin` the local time's offset from UTC (a day is a local day).
inline bool periodicDue(Tier t, uint32_t last, uint32_t now, int offsetMin) {
  if (t == DRIVE || !last || !now || now < last) return true;
  if (t == DAILY) {
    int64_t off = (int64_t)offsetMin * 60;
    return ((int64_t)now + off) / 86400 != ((int64_t)last + off) / 86400;
  }
  uint32_t days = t == WEEKLY ? 7 : 30;
  return now - last >= days * 86400u - 3600u;   // an hour's slack: drives aren't to the minute
}

// Split the body of a 0x22 reply (after the 0x62) to the reads asked for.
// data[k] / len[k]: where read k's data starts and how long it is, or null
// if the module left it out. False if the reply doesn't line up exactly.
inline bool splitReply(const uint8_t* body, size_t n, const std::vector<Read>& reads,
                       const std::vector<size_t>& asked, std::vector<const uint8_t*>& data,
                       std::vector<size_t>& len) {
  data.assign(asked.size(), nullptr);
  len.assign(asked.size(), 0);
  if (asked.size() == 1 && reads[asked[0]].size == 0) {   // unsized: the rest is its
    if (n < 2 || ((body[0] << 8) | body[1]) != reads[asked[0]].did) return false;
    data[0] = body + 2;
    len[0] = n - 2;
    return true;
  }
  size_t i = 0;
  while (i < n) {
    if (i + 2 > n) return false;
    uint16_t did = (uint16_t)((body[i] << 8) | body[i + 1]);
    size_t k = 0;
    while (k < asked.size() && reads[asked[k]].did != did) k++;
    if (k == asked.size() || data[k]) return false;
    size_t size = reads[asked[k]].size;
    if (i + 2 + size > n) return false;
    data[k] = body + i + 2;
    len[k] = size;
    i += 2 + size;
  }
  return true;
}

}  // namespace logger
