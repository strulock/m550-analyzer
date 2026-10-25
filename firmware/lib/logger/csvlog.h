// The logger's CSV files, in n63read's drive-log layout: Time (seconds from
// the start), Clock (local time with its UTC offset, to the millisecond),
// one column per channel under the app's own names, and Event. History, the
// app and the cloud Worker read them like the app's logs. No Arduino code.
#pragma once
#include <math.h>
#include <stdint.h>
#include <stdio.h>

#include <string>
#include <vector>

#include "channels.h"

namespace logger {

// Days since 1970-01-01 to year, month, day (Howard Hinnant's civil_from_days).
inline void civil(int64_t days, int& y, int& m, int& d) {
  days += 719468;
  int64_t era = (days >= 0 ? days : days - 146096) / 146097;
  unsigned doe = (unsigned)(days - era * 146097);
  unsigned yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
  int64_t yy = (int64_t)yoe + era * 400;
  unsigned doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
  unsigned mp = (5 * doy + 2) / 153;
  d = (int)(doy - (153 * mp + 2) / 5 + 1);
  m = (int)(mp < 10 ? mp + 3 : mp - 9);
  y = (int)(yy + (m <= 2));
}

// Year, month, day to days since 1970-01-01 (days_from_civil).
inline int64_t daysFromCivil(int y, int m, int d) {
  y -= m <= 2;
  int64_t era = (y >= 0 ? y : y - 399) / 400;
  unsigned yoe = (unsigned)(y - era * 400);
  unsigned doy = (153 * (unsigned)(m + (m > 2 ? -3 : 9)) + 2) / 5 + (unsigned)d - 1;
  unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
  return era * 146097 + (int64_t)doe - 719468;
}

// Minutes the local time is ahead of UTC, from the local calendar time a
// clock shows at UTC second `epoch` (what localtime_r gives on the board).
inline int utcOffsetMinutes(int64_t epoch, int y, int mo, int d, int h, int mi, int s) {
  int64_t asUtc = daysFromCivil(y, mo, d) * 86400 + h * 3600 + mi * 60 + s;
  return (int)((asUtc - epoch) / 60);
}

struct Local { int y, mo, d, h, mi, s, ms; };

inline Local local(int64_t epochMs, int offsetMin) {
  int64_t t = epochMs + (int64_t)offsetMin * 60000;
  int64_t days = t >= 0 ? t / 86400000 : (t - 86399999) / 86400000;
  int64_t rem = t - days * 86400000;
  Local l;
  civil(days, l.y, l.mo, l.d);
  l.h = (int)(rem / 3600000); l.mi = (int)(rem / 60000 % 60);
  l.s = (int)(rem / 1000 % 60); l.ms = (int)(rem % 1000);
  return l;
}

// "2026-10-25 07:42:50.123-05:00", as core.log_clock writes it.
inline std::string clockText(int64_t epochMs, int offsetMin) {
  Local l = local(epochMs, offsetMin);
  char buf[40];
  int off = offsetMin < 0 ? -offsetMin : offsetMin;
  snprintf(buf, sizeof(buf), "%04d-%02d-%02d %02d:%02d:%02d.%03d%c%02d:%02d", l.y, l.mo, l.d,
           l.h, l.mi, l.s, l.ms, offsetMin < 0 ? '-' : '+', off / 60, off % 60);
  return buf;
}

// "2026-10-25_07-42-50": a drive's file name (without .csv), local time.
inline std::string fileStamp(int64_t epochMs, int offsetMin) {
  Local l = local(epochMs, offsetMin);
  char buf[24];
  snprintf(buf, sizeof(buf), "%04d-%02d-%02d_%02d-%02d-%02d", l.y, l.mo, l.d, l.h, l.mi, l.s);
  return buf;
}

// A value as a cell: blank for none, coordinates to 7 places (as n63read
// rounds them), the rest to 6 decimal places without trailing zeros. The
// sweep writes a thousand or more a row, and the ESP32's printf takes about
// 50 us each, so ordinary values are formatted here; very large or very
// small ones still go through snprintf.
inline void appendValue(std::string& out, double v, Type t) {
  if (isnan(v)) return;
  char buf[32];
  double a = fabs(v);
  if (t == GPS || a >= 1e12 || (a < 1e-4 && a != 0)) {
    snprintf(buf, sizeof(buf), t == GPS ? "%.7f" : "%.10g", v);
    out += buf;
    return;
  }
  int64_t micro = (int64_t)llround(a * 1e6);
  if (micro == 0) { out += '0'; return; }
  char* p = buf + sizeof(buf);
  *--p = 0;
  int64_t whole = micro / 1000000, frac = micro % 1000000;
  if (frac) {
    int digits = 6;
    while (frac % 10 == 0) { frac /= 10; digits--; }
    for (int i = 0; i < digits; i++) { *--p = (char)('0' + frac % 10); frac /= 10; }
    *--p = '.';
  }
  do { *--p = (char)('0' + whole % 10); whole /= 10; } while (whole);
  if (v < 0) *--p = '-';
  out += p;
}

// One of a drive's two files: its columns (channel indices, in table order)
// and the rows as they're written.
class LogFile {
 public:
  std::vector<uint32_t> columns;

  void setup(const Table& t, bool fast) {
    columns.clear();
    for (uint32_t i = 0; i < t.channels.size(); i++) {
      if (t.channels[i].fast == fast) columns.push_back(i);
    }
  }

  std::string header(const Table& t) const {
    std::string h = "Time,Clock";
    for (uint32_t i : columns) { h += ','; h += t.name(t.channels[i]); }
    h += ",Event\n";
    return h;
  }

  // values: per channel index (the whole table), NaN where not read.
  // event: no commas, quotes or newlines (the logger's own short notes).
  std::string row(const Table& t, double seconds, const std::string& clock,
                  const std::vector<double>& values, const char* event = "") const {
    std::string r;
    r.reserve(16 * columns.size() + 48);
    char buf[24];
    snprintf(buf, sizeof(buf), "%.3f", seconds);
    r += buf;
    r += ',';
    r += clock;
    for (uint32_t i : columns) {
      r += ',';
      appendValue(r, values[i], t.channels[i].type);
    }
    r += ',';
    r += event;
    r += '\n';
    return r;
  }
};

}  // namespace logger
