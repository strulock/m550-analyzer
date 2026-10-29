// Fault memory during a drive: when to ask which module, its 59 02 answer
// split into codes, and the fault file's rows (n63read's fault CSV, so the
// app and the Worker read it as they read the app's own). No Arduino code.
//
// UDS 0x19 02 (reportDTCByStatusMask) lists the codes. Each failed code's
// details, 0x19 04 (its snapshots: the odometer when it set, and when it last
// did) and 0x19 06 (how often it occurred), are read only while the car is
// in P (DetailQueue, main.cpp), to <stamp>.details.csv: the odometer and the
// count here, the replies whole for the Worker. The texts stay with n63read
// and the Worker.
#pragma once
#include <stdint.h>
#include <stdio.h>

#include <algorithm>
#include <set>
#include <string>
#include <vector>

namespace logger {

// n63read core.KNOWN_MODULES: the modules confirmed on this car
// (docs/modules.md). tests/test_logger_faults.py keeps the two the same.
struct Module { uint8_t address; const char* name; };
static const Module FAULT_MODULES[] = {
    {0x01, "Airbag / crash safety (ACSM)"},
    {0x06, "Surround view / reversing camera (ICAM2)"},
    {0x08, "Lane-change warning (HC2)"},
    {0x0D, "Rear function module (HKFM)"},
    {0x10, "Central gateway (ZGW)"},
    {0x12, "Engine, master DME"},
    {0x13, "Engine, slave DME (bank 2)"},
    {0x17, "Fuel pump control (EKP)"},
    {0x18, "Transmission, ZF 8HP (EGS)"},
    {0x19, "Transfer case, xDrive (LMV)"},
    {0x21, "Front radar, active cruise (FRR)"},
    {0x22, "Optional-equipment module (SAS)"},
    {0x29, "Stability control (DSC)"},
    {0x2B, "Rear-axle steering (HSR)"},
    {0x2C, "Park assist (PMA / PDC)"},
    {0x2E, "Power control unit (PCU)"},
    {0x30, "Electric power steering (EPS)"},
    {0x40, "Body domain controller (BDC)"},
    {0x4F, "Light effect manager (LEM)"},
    {0x56, "Roof function centre (FZD)"},
    {0x5D, "Camera driver assistance (KAFAS)"},
    {0x5E, "Gear selector (GWS)"},
    {0x60, "Instrument cluster (KOMBI)"},
    {0x61, "Telematics (ATM)"},
    {0x63, "Head unit (NBT Evo)"},
    {0x6D, "Driver's seat module (FAS)"},
    {0x6E, "Passenger seat module (BFS)"},
    {0x76, "Suspension, vertical dynamics (VDP)"},
    {0x78, "Climate control (IHKA)"},
};
constexpr size_t FAULT_MODULE_COUNT = sizeof(FAULT_MODULES) / sizeof(FAULT_MODULES[0]);

inline const char* moduleName(uint8_t address) {
  for (const Module& m : FAULT_MODULES) if (m.address == address) return m.name;
  return "";
}

// Every code the module holds, whatever its state.
static const uint8_t DTC_REQUEST[] = {0x19, 0x02, 0xFF};

struct Dtc {
  uint32_t code;     // 3 bytes, as in BMW_fault_code_texts.csv
  uint8_t status;    // ISO 14229 statusOfDTC
};

// A positive answer from its 0x59: 59 02 <availability mask>, then a
// 4-byte record per code (3 code bytes, status). False if it isn't one.
inline bool parseDtcs(const uint8_t* p, size_t n, std::vector<Dtc>& out) {
  out.clear();
  if (n < 3 || p[0] != 0x59 || p[1] != 0x02) return false;
  for (size_t i = 3; i + 4 <= n; i += 4) {
    out.push_back({(uint32_t)p[i] << 16 | (uint32_t)p[i + 1] << 8 | p[i + 2], p[i + 3]});
  }
  return true;
}

// As n63read's dtc_failed: failed now, this or last cycle, or since the
// memory was cleared. "Confirmed" on a code whose test hasn't run since the
// clear doesn't count (the DSC lists every code it can raise that way).
inline bool dtcFailed(uint8_t status) {
  if (status & 0x25) return true;
  return (status & 0x08) && !(status & 0x10);
}

// As n63read's dtc_status_summary.
inline std::string dtcState(uint8_t status) {
  bool present = status & 0x01, stored = status & 0x0C;
  std::string s = present && stored ? "present, stored"
                  : present         ? "present, not yet stored"
                  : stored          ? "not present, stored"
                                    : "not present";
  if (status & 0x80) s += ", warning lamp requested";
  return s;
}

// n63read's FAULT_CSV_HEADER, and time_s (seconds into the drive) for reads
// made before the board knew the time.
static const char* const FAULT_HEADER =
    "read_at,module,module_name,code,status,state,text_en,text_de,catalog_source,"
    "first_km,last_km,occurrences,details,time_s\n";

// One module's read as fault-file rows: a row per failed code, or one
// saying why there are none. readAt is the local time (empty if unknown).
// result: 0 answered, else the negative response code (nrc > 0) or -1 for
// no answer.
inline std::string faultRows(const std::string& readAt, double timeS, uint8_t module,
                             int result, const std::vector<Dtc>& dtcs) {
  char base[160];
  snprintf(base, sizeof(base), "%s,0x%02X,\"%s\",", readAt.c_str(), module, moduleName(module));
  char t[24];
  snprintf(t, sizeof(t), "%.1f", timeS);
  const std::string tail = std::string(",,,,,,,,") + t + "\n";  // text_en .. details empty
  if (result != 0) {
    char why[40];
    if (result > 0) snprintf(why, sizeof(why), "refused (nrc 0x%02X)", result);
    else snprintf(why, sizeof(why), "no reply");
    return std::string(base) + ",,\"" + why + "\"" + tail;
  }
  std::vector<Dtc> failed;
  for (const Dtc& d : dtcs) if (dtcFailed(d.status)) failed.push_back(d);
  std::sort(failed.begin(), failed.end(),
            [](const Dtc& a, const Dtc& b) { return a.code < b.code; });
  if (failed.empty()) {
    char none[64];
    if (dtcs.empty()) snprintf(none, sizeof(none), "no faults");
    else snprintf(none, sizeof(none), "no faults (%u listed codes never failed)", (unsigned)dtcs.size());
    return std::string(base) + ",,\"" + none + "\"" + tail;
  }
  std::string out;
  for (const Dtc& d : failed) {
    char row[64];
    snprintf(row, sizeof(row), "%06X,0x%02X,", (unsigned)d.code, d.status);
    out += std::string(base) + row + "\"" + dtcState(d.status) + "\"" + tail;
  }
  return out;
}

// ---- a fault's details --------------------------------------------------------

constexpr uint8_t SNAPSHOTS = 0x04, EXTENDED = 0x06;   // the 0x19 sub-functions

// 19 <sub> <code> FF: every record the module keeps for the code.
inline size_t detailRequest(uint8_t sub, uint32_t code, uint8_t* req) {
  req[0] = 0x19;
  req[1] = sub;
  req[2] = code >> 16;
  req[3] = code >> 8;
  req[4] = code;
  req[5] = 0xFF;
  return 6;
}

// The bytes of BMW's standard snapshot identifiers, the same in every module
// on this car (n63read's BMW_freeze_frame_fields.csv); 0 for a module's own,
// whose sizes the board doesn't know.
inline size_t standardFieldBytes(uint8_t module, uint16_t id) {
  switch (id) {
    case 0x1700: return 3;                          // odometer, km
    case 0x1701: return 4;                          // vehicle time, s
    case 0x1702: return 3;                          // SAE code
    case 0x1731: return 1;                          // fault class
    case 0x1750: return 1;                          // power state, base network
    case 0x1751: return 3;                          // power state, sub-networks
    case 0x1768: return 4;                          // odometer (extended)
    case 0x1769: return module == 0x29 ? 4 : 6;     // vehicle time (extended)
    case 0xFFFF: return 3;                          // unknown identifier
    default: return 0;
  }
}

// The odometer in a 59 04 reply's snapshots (identifier 0x1700, as n63read's
// detail_columns): the lowest and highest, the first time the code set and
// the latest. A record runs into a module's own values the board can't size;
// the next record starts with its number, the same count and the same first
// identifier, so the walk carries on from there. False: not a 59 04 reply,
// or no odometer in it.
inline bool snapshotKm(const uint8_t* p, size_t n, uint8_t module, uint32_t& firstKm,
                       uint32_t& lastKm) {
  if (n < 6 || p[0] != 0x59 || p[1] != SNAPSHOTS) return false;
  bool any = false;
  size_t i = 6;
  while (i + 2 <= n) {
    const uint8_t number = p[i], count = p[i + 1];
    const size_t start = i + 2;
    i = start;
    for (unsigned k = 0; k < count; k++) {
      if (i + 2 > n) return any;
      const uint16_t id = (uint16_t)(p[i] << 8 | p[i + 1]);
      const size_t size = standardFieldBytes(module, id);
      if (!size || i + 2 + size > n) {
        // On to the next record, if there is one.
        size_t next = 0;
        for (size_t j = i; start + 2 <= n && j + 4 <= n; j++) {
          if (p[j] == (uint8_t)(number + 1) && p[j + 1] == count && p[j + 2] == p[start] &&
              p[j + 3] == p[start + 1]) {
            next = j;
            break;
          }
        }
        if (!next) return any;
        i = next;
        break;
      }
      if (id == 0x1700) {
        const uint32_t km = (uint32_t)p[i + 2] << 16 | (uint32_t)p[i + 3] << 8 | p[i + 4];
        if (km != 0xFFFFFF) {
          if (!any || km < firstKm) firstKm = km;
          if (!any || km > lastKm) lastKm = km;
          any = true;
        }
      }
      i += 2 + size;
    }
  }
  return any;
}

// How many times the code occurred (record 0x02, HFK) in a 59 06 reply; -1
// if it isn't there. Records 0x01 to 0x03 are a byte each; any other ends the
// walk.
inline int occurrences(const uint8_t* p, size_t n) {
  if (n < 6 || p[0] != 0x59 || p[1] != EXTENDED) return -1;
  for (size_t i = 6; i + 2 <= n; i += 2) {
    if (p[i] == 0x02) return p[i + 1];
    if (p[i] != 0x01 && p[i] != 0x03) break;
  }
  return -1;
}

static const char* const DETAIL_HEADER =
    "read_at,module,module_name,code,first_km,last_km,occurrences,snapshots,extended,time_s\n";

// One reply as hex, or why there isn't one: result as faultRows'.
inline std::string detailCell(int result, const uint8_t* p, size_t n) {
  if (result > 0) {
    char why[32];
    snprintf(why, sizeof(why), "refused (nrc 0x%02X)", result);
    return why;
  }
  if (result < 0) return "no reply";
  static const char* const DIGITS = "0123456789ABCDEF";
  std::string s;
  for (size_t i = 0; i < n; i++) { s += DIGITS[p[i] >> 4]; s += DIGITS[p[i] & 15]; }
  return s;
}

// A code's details row: its odometer and count (empty when not known), and
// both replies (detailCell).
inline std::string detailRow(const std::string& readAt, double timeS, uint8_t module,
                             uint32_t code, bool haveKm, uint32_t firstKm, uint32_t lastKm,
                             int count, const std::string& snapshots,
                             const std::string& extended) {
  char head[160], km[32] = ",", times[12] = "", t[24];
  snprintf(head, sizeof(head), "%s,0x%02X,\"%s\",%06X,", readAt.c_str(), module,
           moduleName(module), (unsigned)(code & 0xFFFFFF));
  if (haveKm) snprintf(km, sizeof(km), "%u,%u", (unsigned)firstKm, (unsigned)lastKm);
  if (count >= 0) snprintf(times, sizeof(times), "%d", count);
  snprintf(t, sizeof(t), "%.1f", timeS);
  return std::string(head) + km + "," + times + ",\"" + snapshots + "\",\"" + extended + "\"," +
         t + "\n";
}

// Which failed codes' details to read: each code once a power-on, as the
// fault lists name them, the two requests in turn.
class DetailQueue {
 public:
  struct Item { uint8_t module; uint32_t code; };

  // A fault list's failed codes.
  void add(uint8_t module, uint32_t code) {
    const uint64_t key = (uint64_t)module << 24 | (code & 0xFFFFFF);
    if (!seen_.insert(key).second) return;
    items_.push_back({module, code & 0xFFFFFF});
  }
  bool empty() const { return items_.empty(); }
  size_t size() const { return items_.size(); }
  // The next code, `skip` aside: a module that's busy just now.
  const Item* next(const std::vector<uint8_t>& skip = {}) const {
    for (const Item& it : items_) {
      if (std::find(skip.begin(), skip.end(), it.module) == skip.end()) return &it;
    }
    return nullptr;
  }
  // That code's two replies are in (or given up on).
  void done(uint8_t module, uint32_t code) {
    for (size_t i = 0; i < items_.size(); i++) {
      if (items_[i].module == module && items_[i].code == code) {
        items_.erase(items_.begin() + i);
        return;
      }
    }
  }

 private:
  std::vector<Item> items_;
  std::set<uint64_t> seen_;
};

// When to read whose faults in a drive: every module once, one at a time,
// starting a minute in (the drive's first minute is the logging's); then
// the two DMEs every three minutes, so an engine fault shows up the drive
// it sets in.
class FaultScan {
 public:
  static constexpr uint32_t FIRST_MS = 60000;      // after the drive starts
  static constexpr uint32_t GAP_MS = 2000;         // between two reads
  static constexpr uint32_t DME_EVERY_MS = 180000;

  void start(uint32_t now) {
    start_ = now;
    last_ = now;
    at_ = 0;
    queue_.clear();
    for (const Module& m : FAULT_MODULES) queue_.push_back(m.address);
    full_ = false;
  }

  // The module to read now, or -1 for none this time.
  int next(uint32_t now) {
    if (now - start_ < FIRST_MS || now - last_ < GAP_MS) return -1;
    if (at_ == queue_.size()) {
      if (!full_) { full_ = true; dmeAt_ = now + DME_EVERY_MS; }
      if ((int32_t)(now - dmeAt_) < 0) return -1;
      queue_.assign({0x12, 0x13});
      at_ = 0;
      dmeAt_ = now + DME_EVERY_MS;
    }
    last_ = now;
    return queue_[at_++];
  }

  bool fullScanDone() const { return full_; }

 private:
  uint32_t start_ = 0, last_ = 0, dmeAt_ = 0;
  std::vector<uint8_t> queue_;
  size_t at_ = 0;
  bool full_ = false;
};

}  // namespace logger
