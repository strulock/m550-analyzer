// Host test of lib/hsfz against a scripted fake gateway.
//   cd firmware && g++ -std=c++17 -Ilib/hsfz test/host_test.cpp -o host_test && ./host_test
// Run one case by name: ./host_test busy_retry
#include <cmath>
#include <cstdio>
#include <cstring>
#include <deque>
#include <vector>

#include "hsfz.h"

using Bytes = std::vector<uint8_t>;

static Bytes frame(uint16_t ctrl, uint8_t src, uint8_t dst, Bytes payload) {
  uint32_t len = payload.size() + 2;
  Bytes f = {uint8_t(len >> 24), uint8_t(len >> 16), uint8_t(len >> 8), uint8_t(len),
             uint8_t(ctrl >> 8), uint8_t(ctrl), src, dst};
  f.insert(f.end(), payload.begin(), payload.end());
  return f;
}

struct Fake : hsfz::Transport {
  std::deque<uint8_t> in;      // bytes the "car" sends
  std::vector<Bytes> sent;
  uint32_t t = 0;
  bool closed = false;
  void push(const Bytes& b) { in.insert(in.end(), b.begin(), b.end()); }
  bool write(const uint8_t* d, size_t n) override { sent.emplace_back(d, d + n); return true; }
  int read(uint8_t* buf, size_t n, uint32_t timeout_ms) override {
    if (in.empty()) { if (closed) return -1; t += timeout_ms; return 0; }
    size_t k = 0;
    while (k < n && !in.empty() && k < 3) { buf[k++] = in.front(); in.pop_front(); }  // dribble
    t += 1;
    return (int)k;
  }
  uint32_t now_ms() override { return t; }
  void sleep_ms(uint32_t ms) override { t += ms; }
};

static int fails = 0;
static const char* current = "";
#define CHECK(x) do { if (!(x)) { printf("FAIL %s line %d: %s\n", current, __LINE__, #x); fails++; } } while (0)

static uint8_t out[256];
static const uint8_t rpm[] = {0x22, 0x48, 0x07};

// ack echo, chatter from another module, late answer for another DID, pending, then ours
static void test_noise_then_answer() {
  Fake f; hsfz::Client c(f, 0xF4);
  f.push(frame(hsfz::CTRL_ACK, 0xF4, 0x12, {0x22, 0x48, 0x07}));
  f.push(frame(hsfz::CTRL_DIAG, 0x40, 0xF4, {0x62, 0x48, 0x07, 0, 0}));
  f.push(frame(hsfz::CTRL_DIAG, 0x12, 0xF4, {0x62, 0x4A, 0x21, 1, 2}));
  f.push(frame(hsfz::CTRL_DIAG, 0x12, 0xF4, {0x7F, 0x22, 0x78}));
  f.push(frame(hsfz::CTRL_DIAG, 0x12, 0xF4, {0x62, 0x48, 0x07, 0x05, 0xDC}));
  auto r = c.request(0x12, rpm, 3, out, sizeof(out));
  CHECK(r.result == hsfz::OK);
  CHECK(r.len == 5 && out[3] == 0x05 && out[4] == 0xDC);
  Bytes want = frame(hsfz::CTRL_DIAG, 0xF4, 0x12, {0x22, 0x48, 0x07});
  CHECK(f.sent.size() == 1 && f.sent[0] == want);
  hsfz::Channel ch{0x4807, "rpm", 2, true, 1.0f, 2.0f, 0.0f, "rpm"};
  CHECK(std::fabs(hsfz::decode(ch, out + 3) - 750.0f) < 0.01f);  // 0x05DC / 2
}

static void test_negative_response() {
  Fake f; hsfz::Client c(f, 0xF4);
  f.push(frame(hsfz::CTRL_DIAG, 0x12, 0xF4, {0x7F, 0x22, 0x31}));
  auto r = c.request(0x12, rpm, 3, out, sizeof(out));
  CHECK(r.result == hsfz::NEGATIVE && r.nrc == 0x31);
}

// busy, then answered after a resend
static void test_busy_retry() {
  Fake f; hsfz::Client c(f, 0xF4);
  f.push(frame(hsfz::CTRL_DIAG, 0x12, 0xF4, {0x7F, 0x22, 0x21}));
  f.push(frame(hsfz::CTRL_DIAG, 0x12, 0xF4, {0x62, 0x48, 0x07, 0, 2}));
  auto r = c.request(0x12, rpm, 3, out, sizeof(out));
  CHECK(r.result == hsfz::OK && f.sent.size() == 2);
}

// gateway NACK 0x40
static void test_gateway_nack() {
  Fake f; hsfz::Client c(f, 0xF4);
  f.push(frame(0x0040, 0x00, 0x00, {}));
  auto r = c.request(0x12, rpm, 3, out, sizeof(out));
  CHECK(r.result == hsfz::NACK && r.nack == 0x0040);
}

// silence, and a closed connection
static void test_timeout_and_closed() {
  Fake f; hsfz::Client c(f, 0xF4);
  CHECK(c.request(0x12, rpm, 3, out, sizeof(out), 500).result == hsfz::TIMEOUT);
  Fake g; g.closed = true; hsfz::Client d(g, 0xF4);
  CHECK(d.request(0x12, rpm, 3, out, sizeof(out)).result == hsfz::CLOSED);
}

// writes refused locally, nothing sent
static void test_writes_refused() {
  Fake f; hsfz::Client c(f, 0xF4);
  const uint8_t clear[] = {0x14, 0xFF, 0xFF, 0xFF};
  const uint8_t write[] = {0x2E, 0x48, 0x07, 0};
  const uint8_t dtc[] = {0x19, 0x02, 0xFF};
  CHECK(c.request(0x12, clear, 4, out, sizeof(out)).result == hsfz::REFUSED);
  CHECK(c.request(0x12, write, 4, out, sizeof(out)).result == hsfz::REFUSED);
  CHECK(c.request(0x12, dtc, 3, out, sizeof(out)).result == hsfz::REFUSED);
  CHECK(f.sent.empty());
}

static void test_oversized_frame() {
  Fake f; hsfz::Client c(f, 0xF4);
  f.push({0x00, 0x10, 0x00, 0x00, 0x00, 0x01});
  CHECK(c.request(0x12, rpm, 3, out, sizeof(out)).result == hsfz::BAD_FRAME);
}

// multi-identifier split: one left out, then a misaligned reply
static void test_split_reply() {
  hsfz::Channel ch[] = {{0x4807, "a", 2, true, 1, 2, 0, ""}, {0x4A21, "b", 2, true, .1f, 1, -273.14f, ""},
                        {0x460C, "c", 2, false, .023481f, 1, 0, ""}};
  uint8_t body[] = {0x48, 0x07, 0x05, 0xDC, 0x46, 0x0C, 0x02, 0x16};
  int off[3];
  CHECK(hsfz::splitReply(body, sizeof(body), ch, 3, off));
  CHECK(off[0] == 2 && off[1] == -1 && off[2] == 6);
  CHECK(std::fabs(hsfz::decode(ch[2], body + 6) - 12.54f) < 0.01f);  // 534 * 0.023481
  uint8_t bad[] = {0x48, 0x07, 0x05};
  CHECK(!hsfz::splitReply(bad, sizeof(bad), ch, 3, off));
  uint8_t dup[] = {0x48, 0x07, 0, 1, 0x48, 0x07, 0, 1};
  CHECK(!hsfz::splitReply(dup, sizeof(dup), ch, 3, off));
}

// a batch whose first identifier the module leaves out is still ours;
// an answer about an identifier we didn't ask for isn't
static void test_batch_without_first() {
  Fake f; hsfz::Client c(f, 0xF4);
  const uint8_t two[] = {0x22, 0x48, 0x07, 0x4A, 0x21};
  f.push(frame(hsfz::CTRL_DIAG, 0x12, 0xF4, {0x62, 0x46, 0x0C, 0, 1}));
  f.push(frame(hsfz::CTRL_DIAG, 0x12, 0xF4, {0x62, 0x4A, 0x21, 0x0E, 0x2F}));
  auto r = c.request(0x12, two, sizeof(two), out, sizeof(out));
  CHECK(r.result == hsfz::OK && r.len == 5 && out[1] == 0x4A && out[2] == 0x21);
}

static void test_discovery_reply() {
  const char* txt = "DIAGADR10BMWMAC001A37C0FFEEBMWVINWBAJB9C55JB287123";
  Bytes p = {0, 0, 0, 0, 0x00, 0x11};
  p.insert(p.end(), txt, txt + strlen(txt));
  char vin[18];
  CHECK(hsfz::isIdentReply(p.data(), p.size()));
  CHECK(hsfz::vinFromIdent(p.data(), p.size(), vin) && strcmp(vin, "WBAJB9C55JB287123") == 0);
  CHECK(!hsfz::isIdentReply(hsfz::IDENT_PROBE_0, 6));
  CHECK(!hsfz::isIdentReply(hsfz::IDENT_PROBE_2, 8));
}

static const struct { const char* name; void (*fn)(); } tests[] = {
  {"noise_then_answer", test_noise_then_answer},
  {"negative_response", test_negative_response},
  {"busy_retry", test_busy_retry},
  {"gateway_nack", test_gateway_nack},
  {"timeout_and_closed", test_timeout_and_closed},
  {"writes_refused", test_writes_refused},
  {"oversized_frame", test_oversized_frame},
  {"split_reply", test_split_reply},
  {"batch_without_first", test_batch_without_first},
  {"discovery_reply", test_discovery_reply},
};

int main(int argc, char** argv) {
  int ran = 0;
  for (const auto& t : tests) {
    if (argc > 1 && strcmp(argv[1], t.name) != 0) continue;
    current = t.name;
    t.fn();
    ran++;
  }
  if (!ran) { printf("no test named %s\n", argv[1]); return 1; }
  printf(fails ? "%d FAILED\n" : "all %d host tests passed\n", fails ? fails : ran);
  return fails ? 1 : 0;
}
