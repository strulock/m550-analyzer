// A full channel check ("Full channel sweep" on the logger page): every
// identifier in the cloud's catalog asked once, as the app's Answers > Check
// car now does, whether the car answered it before or not. The Worker sends
// the list (GET /api/logger/check-list); the board asks it from a minute
// into a drive, a few identifiers a cycle beside the fast reads, keeps its
// place across drives, and writes the answers as the app's check CSV
// (<drive>.check.csv), which the Worker merges into the catalog's answers.
// Only 0x22: read-only like everything else. No Arduino code.
#pragma once
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#include <string>
#include <vector>

namespace logger {

struct CheckItem {
  uint8_t module;
  uint16_t did;
  uint16_t size;     // the reply's data, from the catalog's layout
  bool exact;        // false: at least `size` (an identifier of no fixed size)
};

// The Worker's list: "module,did,size,exact" lines after a header, e.g.
// "0x12,0x4029,8,1". Lines that don't parse are skipped.
inline std::vector<CheckItem> parseCheckList(const std::string& text) {
  std::vector<CheckItem> out;
  size_t at = 0;
  while (at < text.size()) {
    size_t end = text.find('\n', at);
    if (end == std::string::npos) end = text.size();
    std::string line = text.substr(at, end - at);
    at = end + 1;
    unsigned m, d, size, exact;
    char tail;
    if (sscanf(line.c_str(), "%x,%x,%u,%u%c", &m, &d, &size, &exact, &tail) < 4) continue;
    if (m > 0xFF || d > 0xFFFF || size > 0xFFFF) continue;
    out.push_back({(uint8_t)m, (uint16_t)d, (uint16_t)size, exact != 0});
  }
  return out;
}

// The app's statuses (core.check_identifier).
enum CheckStatus { CHECK_OK, CHECK_SIZE, CHECK_REFUSED, CHECK_NO_ANSWER };

inline const char* checkStatusName(CheckStatus s) {
  switch (s) {
    case CHECK_OK: return "ok";
    case CHECK_SIZE: return "size";
    case CHECK_REFUSED: return "refused";
    default: return "no answer";
  }
}

// One answer: `answered` a positive reply with `dataLen` bytes after the
// identifier, `refused` a negative one; neither is no answer.
inline CheckStatus checkStatus(bool answered, bool refused, size_t dataLen, const CheckItem& it) {
  if (refused) return CHECK_REFUSED;
  if (!answered) return CHECK_NO_ANSWER;
  bool fits = it.exact ? dataLen == it.size : dataLen >= it.size;
  return fits ? CHECK_OK : CHECK_SIZE;
}

// The check CSV's columns (core.CHECK_HEADER).
static const char* const CHECK_HEADER = "module,did,job,status,detail,reply_bytes,raw\n";

// One row; `job` names the request ("logger sweep <id>"), `detail` the
// negative response code or the size seen.
inline std::string checkRow(const CheckItem& it, const std::string& job, CheckStatus s,
                            uint8_t nrc, size_t dataLen) {
  char detail[48] = "";
  if (s == CHECK_REFUSED) snprintf(detail, sizeof(detail), "0x%02X", nrc);
  else if (s == CHECK_SIZE) {
    snprintf(detail, sizeof(detail), "got %u bytes; expected %s%u", (unsigned)dataLen,
             it.exact ? "" : ">=", (unsigned)it.size);
  } else if (s == CHECK_OK) snprintf(detail, sizeof(detail), "%u bytes", (unsigned)dataLen);
  char row[160];
  snprintf(row, sizeof(row), "0x%02X,0x%04X,%s,%s,%s,%s,\n", it.module, it.did, job.c_str(),
           checkStatusName(s), detail,
           s == CHECK_OK || s == CHECK_SIZE ? std::to_string(dataLen).c_str() : "");
  return row;
}

// A module that leaves this many identifiers in a row unanswered is
// skipped for the rest of the check (absent, asleep or not on this car),
// as the app's check does, so it costs seconds rather than minutes.
static const uint8_t CHECK_SILENT_TO_SKIP = 3;

class CheckSilence {
 public:
  // An answer from `module`: true when this one made it skipped.
  bool answer(uint8_t module, CheckStatus s) {
    if (skipped_[module]) return false;
    silent_[module] = s == CHECK_NO_ANSWER ? silent_[module] + 1 : 0;
    if (silent_[module] < CHECK_SILENT_TO_SKIP) return false;
    skipped_[module] = true;
    return true;
  }
  bool skipped(uint8_t module) const { return skipped_[module]; }
  // The skipped modules, "0x40 0x63".
  std::string list() const {
    std::string s;
    for (int m = 0; m < 256; m++) {
      if (!skipped_[m]) continue;
      char t[8];
      snprintf(t, sizeof(t), "%s0x%02X", s.empty() ? "" : " ", m);
      s += t;
    }
    return s;
  }
  void reset() {
    for (int m = 0; m < 256; m++) { silent_[m] = 0; skipped_[m] = false; }
  }

 private:
  uint8_t silent_[256] = {};
  bool skipped_[256] = {};
};

}  // namespace logger
