// The logger's channel table: n63read's `logger-channels` file (channels.csv
// on the SD card), parsed, and each value decoded the way n63read's
// core.decode does. No Arduino code, so the host test covers it.
//
// Columns (by name, so new ones can be added): name, module, did, did_size,
// offset, type, order, mul, div, add, no_value, unit, tier.
#pragma once
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include <string>
#include <vector>

namespace logger {

enum Type : uint8_t { U8, S8, U16, S16, U32, S32, F32, GPS };

inline int typeWidth(Type t) {
  switch (t) {
    case U8: case S8: return 1;
    case U16: case S16: return 2;
    case U32: case S32: case F32: return 4;
    case GPS: return 5;
  }
  return 0;
}

struct Channel {
  uint32_t name;        // offset of its NUL-terminated name in Table::names
  uint8_t module;
  uint16_t did;
  uint16_t didSize;     // the identifier's whole reply, 0 if not fixed
  uint16_t offset;      // this value's place in it
  Type type;
  bool little;          // byte order "little"; else most significant first
  bool fast;            // tier "fast": every cycle; else the sweep
  bool hasNoValue;
  uint32_t noValue;     // the raw value (big-endian) that means "none"
  double mul, div, add;
};

// One CSV line's fields; quotes allowed.
inline std::vector<std::string> csvFields(const char* line) {
  std::vector<std::string> out(1);
  bool quoted = false;
  for (const char* p = line; *p && *p != '\n' && *p != '\r'; p++) {
    if (quoted) {
      if (*p == '"' && p[1] == '"') { out.back() += '"'; p++; }
      else if (*p == '"') quoted = false;
      else out.back() += *p;
    } else if (*p == '"') quoted = true;
    else if (*p == ',') out.emplace_back();
    else out.back() += *p;
  }
  return out;
}

class Table {
 public:
  std::vector<Channel> channels;
  std::vector<char> names;
  int skipped = 0;              // rows that didn't parse
  std::string error;            // why the header was refused

  const char* name(const Channel& c) const { return names.data() + c.name; }

  // Feed the file a line at a time; the first is the header. False if the
  // header lacks a column the board needs (and `error` says which).
  bool line(const char* text) {
    std::vector<std::string> f = csvFields(text);
    if (col_.empty()) return header(f);
    if (f.size() == 1 && f[0].empty()) return true;      // blank line
    Channel c{};
    if (!parse(f, c)) { skipped++; return true; }
    c.name = (uint32_t)names.size();
    const std::string& n = f[col_[NAME]];
    names.insert(names.end(), n.begin(), n.end());
    names.push_back(0);
    channels.push_back(c);
    return true;
  }

  size_t count(bool fast) const {
    size_t n = 0;
    for (const Channel& c : channels) n += c.fast == fast;
    return n;
  }

 private:
  enum Col { NAME, MODULE, DID, DID_SIZE, OFFSET, TYPE, ORDER, MUL, DIV, ADD, NO_VALUE, TIER, NCOL };

  bool header(const std::vector<std::string>& f) {
    static const char* want[NCOL] = {"name", "module", "did", "did_size", "offset", "type",
                                     "order", "mul", "div", "add", "no_value", "tier"};
    col_.assign(NCOL, -1);
    for (size_t i = 0; i < f.size(); i++) {
      std::string h = f[i];
      if (i == 0 && h.size() >= 3 && (uint8_t)h[0] == 0xEF) h = h.substr(3);   // BOM
      for (int k = 0; k < NCOL; k++) if (h == want[k]) col_[k] = (int)i;
    }
    for (int k = 0; k < NCOL; k++) {
      if (col_[k] < 0) { error = std::string("no column ") + want[k]; col_.clear(); return false; }
    }
    return true;
  }

  static bool num(const std::string& s, double& out) {
    if (s.empty()) return false;
    char* end;
    out = strtod(s.c_str(), &end);
    return *end == 0 && isfinite(out);
  }

  static bool hex(const std::string& s, uint32_t& out) {
    if (s.empty()) return false;
    char* end;
    out = (uint32_t)strtoul(s.c_str(), &end, 0);
    return *end == 0;
  }

  bool parse(const std::vector<std::string>& f, Channel& c) const {
    for (int k = 0; k < NCOL; k++) if (col_[k] >= (int)f.size()) return false;
    const std::string& name = f[col_[NAME]];
    uint32_t module, did, size = 0, offset;
    if (name.empty() || !hex(f[col_[MODULE]], module) || module > 0xFF ||
        !hex(f[col_[DID]], did) || did > 0xFFFF || !hex(f[col_[OFFSET]], offset)) {
      return false;
    }
    if (!f[col_[DID_SIZE]].empty() && !hex(f[col_[DID_SIZE]], size)) return false;
    static const char* types[] = {"u8", "s8", "u16", "s16", "u32", "s32", "f32", "gps"};
    int t = -1;
    for (int k = 0; k < 8; k++) if (f[col_[TYPE]] == types[k]) t = k;
    if (t < 0) return false;
    c.module = (uint8_t)module;
    c.did = (uint16_t)did;
    c.didSize = (uint16_t)size;
    c.offset = (uint16_t)offset;
    c.type = (Type)t;
    if (size && offset + typeWidth(c.type) > size) return false;
    c.little = f[col_[ORDER]] == "little";
    c.fast = f[col_[TIER]] == "fast";
    if (!num(f[col_[MUL]], c.mul) || !num(f[col_[DIV]], c.div) || !num(f[col_[ADD]], c.add)) {
      return false;
    }
    if (c.div == 0) c.div = 1;
    c.hasNoValue = hex(f[col_[NO_VALUE]], c.noValue);
    return true;
  }

  std::vector<int> col_;
};

// The head unit's 5-byte coordinate (n63read core._dms): the first byte is
// the ninth bit of a two's-complement degree count.
inline double dms(const uint8_t* b) {
  int whole = ((b[0] & 0x01) << 8) | b[1];
  if (b[0] & 0x01) whole -= 0x200;
  double deg = abs(whole) + b[2] / 60.0 + (b[3] + b[4] / 100.0) / 3600.0;
  return whole < 0 ? -deg : deg;
}

// One value from its identifier's reply data (after the identifier itself),
// `n` bytes of it. False when the reply is too short or holds the channel's
// "no value": the cell stays blank, as in n63read's logs.
inline bool decode(const Channel& c, const uint8_t* data, size_t n, double& out) {
  const int w = typeWidth(c.type);
  if ((size_t)c.offset + w > n) return false;
  const uint8_t* p = data + c.offset;
  if (c.hasNoValue) {
    uint32_t be = 0;
    for (int i = 0; i < w && i < 4; i++) be = (be << 8) | p[i];
    if (be == c.noValue) return false;
  }
  if (c.type == GPS) { out = dms(p); return true; }
  uint32_t u = 0;
  for (int i = 0; i < w; i++) u = (u << 8) | p[c.little ? w - 1 - i : i];
  double raw;
  switch (c.type) {
    case U8: case U16: case U32: raw = u; break;
    case S8: raw = (int8_t)u; break;
    case S16: raw = (int16_t)u; break;
    case S32: raw = (int32_t)u; break;
    case F32: { float f; memcpy(&f, &u, 4); raw = f; break; }
    default: return false;
  }
  out = raw * c.mul / c.div + c.add;
  return isfinite(out);
}

}  // namespace logger
