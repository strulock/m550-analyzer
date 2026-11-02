// What the board's tasks were doing, kept through a reset: the logging
// loop, the check-ins, the uploads and the card's sector reads and writes
// each note where they are in RTC memory (main.cpp), which a watchdog or a
// crash leaves alone, so the next boot can say where it was ("reset_where"
// in the check-ins). Power-on leaves it random: the magic number and the
// printable check keep that out. The text, without the RTC memory, so the
// host test covers it. No Arduino code.
#pragma once
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#include <string>

namespace logger {

static const uint32_t WHERE_MAGIC = 0x57484552;   // "WHER"
static const size_t WHERE_LEN = 40;

struct Where {
  uint32_t magic;
  char loop[WHERE_LEN], beat[WHERE_LEN], upload[WHERE_LEN], disk[WHERE_LEN];
};

// A slot's text, cut to fit. One writer per slot (its task; the disk's is
// under FatFS's lock), so no lock: a reset mid-copy leaves a torn slot,
// which whereText() then shows as far as it's printable.
inline void whereSet(char* slot, const char* text) {
  size_t i = 0;
  for (; text && text[i] && i < WHERE_LEN - 1; i++) slot[i] = text[i];
  slot[i] = 0;
}

inline void whereClear(Where& w) {
  w.magic = WHERE_MAGIC;
  w.loop[0] = w.beat[0] = w.upload[0] = w.disk[0] = 0;
}

// "loop: car reads; beat: check-in send; disk: write 8 at 123456", the
// slots that hold something printable; "" if the memory wasn't set by
// whereClear() (a power-on).
inline std::string whereText(const Where& w) {
  if (w.magic != WHERE_MAGIC) return "";
  std::string out;
  auto add = [&](const char* name, const char* slot) {
    std::string t;
    for (size_t i = 0; i < WHERE_LEN && slot[i]; i++) {
      unsigned char c = slot[i];
      if (c < 0x20 || c > 0x7e) break;
      t += (char)c;
    }
    if (t.empty()) return;
    if (!out.empty()) out += "; ";
    out += name;
    out += ": ";
    out += t;
  };
  add("loop", w.loop);
  add("beat", w.beat);
  add("upload", w.upload);
  add("disk", w.disk);
  return out;
}

// A sector read or write that needed more than one try, or failed, for the
// drive's notes and the check-ins: "write 8 at 123456: ok on try 3 (140 ms;
// card reset)".
inline std::string diskText(bool write, uint32_t sector, unsigned count, int tries, bool ok,
                            uint32_t ms, bool reset) {
  char t[112];
  snprintf(t, sizeof(t), "%s %u at %lu: %s %d (%lu ms%s)", write ? "write" : "read", count,
           (unsigned long)sector, ok ? "ok on try" : "failed after try", tries, (unsigned long)ms,
           reset ? "; card reset" : "");
  return t;
}

}  // namespace logger
