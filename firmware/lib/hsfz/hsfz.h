// HSFZ client for the M550i spike: the same framing and reply rules as
// n63read's core.Hsfz (v0.4.5), with no Arduino dependency so it can be
// tested on a PC against a fake car (test/host_test.cpp).
//
// Frame: [length:4 BE][control:2 BE][source:1][target:1][UDS payload...]
// length counts source + target + payload.
#pragma once
#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace hsfz {

constexpr uint16_t DIAG_PORT = 6801;     // TCP
constexpr uint16_t CONTROL_PORT = 6811;  // UDP discovery
constexpr uint8_t TESTER = 0xF4;         // main connection (0x12 and the rest)
constexpr uint8_t TESTER_BANK2 = 0xF5;   // second connection, slave DME 0x13
constexpr uint8_t DME_MASTER = 0x12;
constexpr uint8_t DME_SLAVE = 0x13;

constexpr uint16_t CTRL_DIAG = 0x0001;
constexpr uint16_t CTRL_ACK = 0x0002;    // gateway echoes the request
constexpr uint16_t CTRL_IDENT = 0x0011;  // vehicle identification
constexpr uint32_t MAX_FRAME = 4096;     // our buffer; real frames are small

enum Result : uint8_t {
  OK = 0,
  TIMEOUT,     // nothing that answers this request before the deadline
  CLOSED,      // the TCP connection dropped
  NACK,        // gateway refused: control word 0x0040..0x0044
  NEGATIVE,    // module answered 0x7F <service> <nrc>
  REFUSED,     // not a read-only service; never sent
  BAD_FRAME,   // length field over MAX_FRAME
};

inline const char* resultName(Result r) {
  switch (r) {
    case OK: return "ok";
    case TIMEOUT: return "timeout";
    case CLOSED: return "connection closed";
    case NACK: return "gateway refused (NACK)";
    case NEGATIVE: return "negative response";
    case REFUSED: return "refused locally (not a read service)";
    case BAD_FRAME: return "bad frame";
  }
  return "?";
}

inline const char* nackText(uint16_t c) {
  switch (c) {
    case 0x0040: return "incorrect tester address (car may still hold an earlier connection)";
    case 0x0041: return "incorrect control word";
    case 0x0042: return "incorrect format";
    case 0x0043: return "incorrect destination address";
    case 0x0044: return "message too large";
  }
  return "unknown";
}

// What the client needs from the network. Arduino and the host test each
// supply one.
struct Transport {
  virtual ~Transport() {}
  virtual bool write(const uint8_t* data, size_t len) = 0;
  // Read up to len bytes. >0 bytes read, 0 timed out, -1 connection closed.
  virtual int read(uint8_t* buf, size_t len, uint32_t timeout_ms) = 0;
  virtual uint32_t now_ms() = 0;
  virtual void sleep_ms(uint32_t ms) = 0;
};

struct Reply {
  Result result = TIMEOUT;
  uint8_t nrc = 0;       // NEGATIVE: the response code
  uint16_t nack = 0;     // NACK: the control word
  size_t len = 0;        // OK: bytes copied to out (positive response, from SID+0x40)
};

class Client {
 public:
  Client(Transport& t, uint8_t source) : t_(t), src_(source) {}

  uint8_t source() const { return src_; }

  // Send one UDS request and wait for its positive answer. Only 0x22 and 0x3E
  // leave this function: the logger never writes to the car.
  Reply request(uint8_t target, const uint8_t* req, size_t reqLen,
                uint8_t* out, size_t outCap, uint32_t timeout_ms = 6000) {
    Reply r;
    if (reqLen == 0 || (req[0] != 0x22 && req[0] != 0x3E)) {
      r.result = REFUSED;
      return r;
    }
    const uint8_t service = req[0];
    const size_t echo = service == 0x22 ? 2 : 1;
    if (!send(target, req, reqLen)) { r.result = CLOSED; return r; }

    uint32_t deadline = t_.now_ms() + timeout_ms;
    int busy = 0;
    while ((int32_t)(deadline - t_.now_ms()) > 0) {
      uint16_t control; uint32_t length;
      Result fr = readFrame(deadline, control, length);
      if (fr != OK) { r.result = fr; return r; }

      if (control == CTRL_ACK) continue;                 // our own echo
      if (control >= 0x0040 && control <= 0x0044) {
        r.result = NACK; r.nack = control; return r;
      }
      if (control != CTRL_DIAG || length < 3) continue;
      const uint8_t fsrc = buf_[0], fdst = buf_[1];
      const uint8_t* data = buf_ + 2;
      const size_t dlen = length - 2;
      if (fsrc != target || fdst != src_) continue;      // someone else's

      if (data[0] == 0x7F && dlen >= 3) {
        if (data[1] != service) continue;
        if (data[2] == 0x78) continue;                   // response pending
        if (data[2] == 0x21 && busy < 3) {               // busy: ask again
          busy++;
          t_.sleep_ms(200);
          if (!send(target, req, reqLen)) { r.result = CLOSED; return r; }
          deadline = t_.now_ms() + timeout_ms;
          continue;
        }
        r.result = NEGATIVE; r.nrc = data[2]; return r;
      }
      if (data[0] == service + 0x40 && dlen >= 1 + echo && echoes(req, reqLen, data + 1)) {
        r.len = dlen < outCap ? dlen : outCap;
        memcpy(out, data, r.len);
        r.result = OK;
        return r;
      }
      // a late answer to an earlier request, or chatter: keep waiting
    }
    r.result = TIMEOUT;
    return r;
  }

 private:
  // Whether a positive reply starting at `p` answers this request: 0x3E echoes
  // its sub-function; 0x22 starts with one of the identifiers asked for (a
  // module leaves out ones it doesn't have, the first included).
  static bool echoes(const uint8_t* req, size_t reqLen, const uint8_t* p) {
    if (req[0] != 0x22) return reqLen < 2 || p[0] == req[1];
    for (size_t i = 1; i + 1 < reqLen; i += 2) {
      if (p[0] == req[i] && p[1] == req[i + 1]) return true;
    }
    return false;
  }

  bool send(uint8_t target, const uint8_t* p, size_t n) {
    uint8_t frame[8 + 64];
    if (n > 64) return false;
    const uint32_t len = n + 2;
    frame[0] = len >> 24; frame[1] = len >> 16; frame[2] = len >> 8; frame[3] = len;
    frame[4] = CTRL_DIAG >> 8; frame[5] = CTRL_DIAG & 0xFF;
    frame[6] = src_; frame[7] = target;
    memcpy(frame + 8, p, n);
    return t_.write(frame, 8 + n);
  }

  Result readExact(uint8_t* dst, size_t n, uint32_t deadline) {
    size_t got = 0;
    while (got < n) {
      int32_t left = (int32_t)(deadline - t_.now_ms());
      if (left <= 0) return TIMEOUT;
      int k = t_.read(dst + got, n - got, (uint32_t)left);
      if (k < 0) return CLOSED;
      if (k == 0) return TIMEOUT;
      got += (size_t)k;
    }
    return OK;
  }

  Result readFrame(uint32_t deadline, uint16_t& control, uint32_t& length) {
    uint8_t head[6];
    Result r = readExact(head, 6, deadline);
    if (r != OK) return r;
    length = ((uint32_t)head[0] << 24) | ((uint32_t)head[1] << 16) |
             ((uint32_t)head[2] << 8) | head[3];
    control = (uint16_t)((head[4] << 8) | head[5]);
    if (length > MAX_FRAME) return BAD_FRAME;
    return readExact(buf_, length, deadline);
  }

  Transport& t_;
  uint8_t src_;
  uint8_t buf_[MAX_FRAME];
};

// ---- discovery ------------------------------------------------------------

// The two identification probes n63read sends (zero-length form first).
static const uint8_t IDENT_PROBE_0[6] = {0, 0, 0, 0, 0x00, 0x11};
static const uint8_t IDENT_PROBE_2[8] = {0, 0, 0, 2, 0x00, 0x11, 0, 0};

// True if a UDP datagram is an HSFZ vehicle identification reply (and not
// our own probe heard back).
inline bool isIdentReply(const uint8_t* p, size_t n) {
  if (n == sizeof(IDENT_PROBE_0) && memcmp(p, IDENT_PROBE_0, n) == 0) return false;
  if (n == sizeof(IDENT_PROBE_2) && memcmp(p, IDENT_PROBE_2, n) == 0) return false;
  return n >= 6 && p[4] == 0x00 && p[5] == 0x11;
}

// Copy the 17-character VIN after "BMWVIN" into vin (18 bytes). False if absent.
inline bool vinFromIdent(const uint8_t* p, size_t n, char* vin) {
  static const char tag[] = "BMWVIN";
  for (size_t i = 0; i + 6 + 17 <= n; i++) {
    if (memcmp(p + i, tag, 6) == 0) {
      memcpy(vin, p + i + 6, 17);
      vin[17] = 0;
      return true;
    }
  }
  return false;
}

// ---- channels -------------------------------------------------------------

// Rows copied from n63read's N63TU2_DME_readable_measurements.csv. value =
// raw * mul / div + add, raw big-endian ("High") of the given width.
struct Channel {
  uint16_t did;
  const char* name;
  uint8_t width;
  bool sign;
  float mul, div, add;
  const char* unit;
};

inline float decode(const Channel& c, const uint8_t* raw) {
  int32_t v;
  if (c.width == 1) {
    v = c.sign ? (int32_t)(int8_t)raw[0] : (int32_t)raw[0];
  } else {
    uint16_t u = (uint16_t)((raw[0] << 8) | raw[1]);
    v = c.sign ? (int32_t)(int16_t)u : (int32_t)u;
  }
  return (float)v * c.mul / (c.div == 0 ? 1.0f : c.div) + c.add;
}

// Split the body of a multi-identifier 0x22 reply (after the 0x62). For each
// asked channel, offsets[i] is where its data starts, or -1 if the module
// left it out. False if the reply doesn't line up exactly.
inline bool splitReply(const uint8_t* body, size_t n, const Channel* ch, size_t count,
                       int* offsets) {
  for (size_t i = 0; i < count; i++) offsets[i] = -1;
  size_t i = 0;
  while (i < n) {
    if (i + 2 > n) return false;
    uint16_t did = (uint16_t)((body[i] << 8) | body[i + 1]);
    size_t k = 0;
    while (k < count && ch[k].did != did) k++;
    if (k == count || offsets[k] >= 0) return false;
    if (i + 2 + ch[k].width > n) return false;
    offsets[k] = (int)(i + 2);
    i += 2 + ch[k].width;
  }
  return true;
}

}  // namespace hsfz
