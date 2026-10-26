// Fault memory during a drive: when to ask which module, its 59 02 answer
// split into codes, and the fault file's rows (n63read's fault CSV, so the
// app and the Worker read it as they read the app's own). No Arduino code.
//
// Only UDS 0x19 02 (reportDTCByStatusMask) is sent: it lists the codes. The
// details (0x19 04 and 06) and the texts stay with n63read and the Worker.
#pragma once
#include <stdint.h>
#include <stdio.h>

#include <algorithm>
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
