// What the logger asks the car for: the channel table grouped into one read
// per (module, identifier), and reads batched into multi-identifier
// 0x22 requests (the spike proved eight at a time on the DMEs, 2.6x
// n63read's rate; MAX_BATCH asks for more). No Arduino code.
//
// A module that can't take a batch (refuses it, times out, or answers out
// of line) halves its batch size, down to one identifier a request; a read
// that keeps failing is rested and tried again now and then, so one dead
// identifier doesn't cost a timeout every cycle.
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
  // A batch of `size` failed: smaller from now on (a batch that was already
  // smaller than the current size says nothing new).
  void smallerBatches(uint8_t module, size_t size) {
    size_t now = batchFor(module);
    if (size >= now && now > 1) batch_[module] = now / 2 ? now / 2 : 1;
  }

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

  // The first index of a tier (reads are sorted by tier).
  size_t tierStart(Tier tier) const {
    size_t i = 0;
    while (i < reads.size() && reads[i].tier < tier) i++;
    return i;
  }

 private:
  std::map<uint8_t, size_t> batch_;
};

// One request per module per cycle, so a cycle is one wave of requests in
// flight together (each module answers one at a time; different modules in
// parallel). A module's request carries all its fast reads, then the next
// medium ones and the next slow ones in rotation, up to its batch size.
// Modules with only slow reads take turns, one a cycle. Reads of unknown
// reply size go alone, one a cycle, when their module isn't asked otherwise.
class Scheduler {
 public:
  void build(const Plan& p) {
    queues_.clear();
    singles_.clear();
    singleAt_ = slowOnlyAt_ = 0;
    for (size_t i = 0; i < p.reads.size(); i++) {
      const Read& r = p.reads[i];
      if (r.size == 0) { singles_.push_back(i); continue; }
      Queue& q = queue(r.module);
      (r.tier == FAST ? q.fast : r.tier == MEDIUM ? q.medium : q.slow).push_back(i);
    }
    for (Queue& q : queues_) q.slowRound = q.slow.empty();
  }

  // This cycle's requests, at most one per module.
  std::vector<std::vector<size_t>> cycle(const Plan& p, uint32_t now) {
    std::vector<std::vector<size_t>> out;
    std::vector<uint8_t> asked;
    std::vector<size_t> slowOnly;
    for (size_t k = 0; k < queues_.size(); k++) {
      Queue& q = queues_[k];
      if (q.fast.empty() && q.medium.empty()) { if (!q.slow.empty()) slowOnly.push_back(k); continue; }
      std::vector<size_t> b;
      size_t bytes = 0, room = p.batchFor(q.module);
      for (size_t i : q.fast) {                    // all of them: more requests if need be
        if (add(p, i, now, b, bytes, room)) continue;
        out.push_back(b);
        b.clear();
        bytes = 0;
        add(p, i, now, b, bytes, room);
      }
      // What's left after the fast reads: medium gets at most half (when
      // there's slow to read), slow the rest, so both keep moving.
      size_t left = room > b.size() ? room - b.size() : 0;
      size_t forMedium = q.slow.empty() ? left : (left + 1) / 2;
      take(p, q.medium, q.mediumAt, now, b, bytes, b.size() + forMedium, nullptr);
      take(p, q.slow, q.slowAt, now, b, bytes, room, &q.slowRound);
      if (!b.empty()) { out.push_back(b); asked.push_back(q.module); }
    }
    if (!slowOnly.empty()) {                                 // one slow-only module a cycle
      Queue& q = queues_[slowOnly[slowOnlyAt_++ % slowOnly.size()]];
      std::vector<size_t> b;
      size_t bytes = 0, room = p.batchFor(q.module);
      take(p, q.slow, q.slowAt, now, b, bytes, room, &q.slowRound);
      if (!b.empty()) { out.push_back(b); asked.push_back(q.module); }
    }
    for (size_t tries = 0; tries < singles_.size(); tries++) {   // one alone, module free
      size_t i = singles_[singleAt_++ % singles_.size()];
      const Read& r = p.reads[i];
      if (std::find(asked.begin(), asked.end(), r.module) != asked.end() || resting(r, now)) continue;
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
    uint8_t module;
    std::vector<size_t> fast, medium, slow;
    size_t mediumAt = 0, slowAt = 0;
    bool slowRound = false;              // every slow read taken since the last pass
  };

  Queue& queue(uint8_t module) {
    for (Queue& q : queues_) if (q.module == module) return q;
    queues_.push_back(Queue{module, {}, {}, {}, 0, 0, false});
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

  std::vector<Queue> queues_;
  std::vector<size_t> singles_;
  size_t singleAt_ = 0, slowOnlyAt_ = 0;
};

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
