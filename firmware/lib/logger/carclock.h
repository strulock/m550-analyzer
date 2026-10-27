// The car's own clock, for a drive that starts before the board has the
// time from the internet: the airbag module (ACSM, 0x01) gives the clock the
// dashboard shows, local time, at identifier 0x4029 (_BORDZEIT_AKTUELL).
// No Arduino code, so the host test covers it.
//
// Its 8 bytes, as read from this car on 2026-10-18 (161f0a12a707ea01 at
// 22:31:10 that Sunday): hour, minute, second, day of month, month (high
// nibble) and weekday (low, Monday 1), year (two bytes, most significant
// first), and whether the display's clock is set (bit 0).
#pragma once
#include <stddef.h>
#include <stdint.h>

namespace logger {

constexpr uint8_t CLOCK_MODULE = 0x01;
constexpr uint16_t CLOCK_DID = 0x4029;
constexpr size_t CLOCK_SIZE = 8;

struct CarTime {
  int year, month, day, hour, minute, second;
};

// The reply's data (after 0x62 and the identifier): true and `t` filled when
// it's a set clock with a real date.
inline bool carClock(const uint8_t* d, size_t n, CarTime& t) {
  if (n < CLOCK_SIZE || !(d[7] & 1)) return false;
  t.hour = d[0];
  t.minute = d[1];
  t.second = d[2];
  t.day = d[3];
  t.month = d[4] >> 4;
  t.year = (d[5] << 8) | d[6];
  return t.hour < 24 && t.minute < 60 && t.second < 60 && t.day >= 1 && t.day <= 31 &&
         t.month >= 1 && t.month <= 12 && t.year >= 2024 && t.year < 2100;
}

}  // namespace logger
