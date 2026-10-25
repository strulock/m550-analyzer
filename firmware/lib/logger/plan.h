// What the logger asks the car for: the channel table grouped into one read
// per (module, identifier), and reads batched into multi-identifier 0x22
// requests the way the spike proved on the DMEs (eight at a time, 2.6x
// n63read's rate). No Arduino code.
//
// A module that can't take a batch (refuses it, times out, or answers out
// of line) drops to one identifier a request; a read that keeps failing is
// rested and tried again now and then, so one dead identifier doesn't cost
// a timeout every cycle.
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
  bool fast;
  std::vector<uint32_t> channels;   // indices into Table::channels
  uint8_t failures = 0;        // in a row
  uint32_t restUntil = 0;      // ms; skipped until then
};

constexpr size_t MAX_BATCH = 8;          // identifiers per request
constexpr size_t MAX_REPLY = 3000;       // bytes a batch may expect back (hsfz buffer 4096)
constexpr uint8_t FAILURES_TO_REST = 3;
constexpr uint32_t REST_MS = 60000;

class Plan {
 public:
  std::vector<Read> reads;

  void build(const Table& t) {
    reads.clear();
    std::map<uint32_t, size_t> at;           // (fast, module, did) -> read
    for (uint32_t i = 0; i < t.channels.size(); i++) {
      const Channel& c = t.channels[i];
      uint32_t key = ((uint32_t)!c.fast << 24) | ((uint32_t)c.module << 16) | c.did;
      auto it = at.find(key);
      if (it == at.end()) {
        Read r;
        r.module = c.module; r.did = c.did; r.size = c.didSize; r.fast = c.fast;
        at[key] = reads.size();
        reads.push_back(r);
        it = at.find(key);
      }
      reads[it->second].channels.push_back(i);
    }
    // A channel can share an identifier across tiers: the fast read covers it.
    std::sort(reads.begin(), reads.end(), [](const Read& a, const Read& b) {
      if (a.fast != b.fast) return a.fast;
      if (a.module != b.module) return a.module < b.module;
      return a.did < b.did;
    });
  }

  // How many identifiers this module takes in one request (MAX_BATCH until
  // it fails a batch).
  size_t batchFor(uint8_t module) const {
    auto it = single_.find(module);
    return it == single_.end() ? MAX_BATCH : 1;
  }
  void noBatches(uint8_t module) { single_[module] = true; }

  // The next request from `reads[from..]` of one tier: consecutive reads of
  // one module, up to its batch size (a read of unknown size goes alone).
  // Returns the read indices; `from` moves past them. Rested reads are
  // skipped. Empty when the tier has no more.
  std::vector<size_t> next(bool fast, size_t& from, uint32_t now) const {
    std::vector<size_t> out;
    size_t bytes = 0;
    for (; from < reads.size(); from++) {
      const Read& r = reads[from];
      if (r.fast != fast) { if (out.empty()) continue; break; }
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

  // The first index of the sweep tier.
  size_t sweepStart() const {
    size_t i = 0;
    while (i < reads.size() && reads[i].fast) i++;
    return i;
  }

 private:
  std::map<uint8_t, bool> single_;
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
