// Host test of lib/logger: SHA-256, the channel file, decoding, the read
// plan, bursts, the fall-back to one request at a time, the CSV layout, the
// fault reads, the car's clock, the firmware manifest and update start-up,
// the upload rules and kept check-ins, and the full channel check.
//   cd firmware && g++ -std=c++17 -Ilib/logger test/test_logger.cpp -o test_logger && ./test_logger
// Run one case by name: ./test_logger decode
#include <cmath>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "carcheck.h"
#include "carclock.h"
#include "channels.h"
#include "csvlog.h"
#include "faults.h"
#include "wifinote.h"
#include "ota.h"
#include "plan.h"
#include "settings.h"
#include "sha256.h"
#include "upload.h"

using namespace logger;

static int fails = 0;
static const char* current = "";
#define CHECK(x) do { if (!(x)) { printf("FAIL %s line %d: %s\n", current, __LINE__, #x); fails++; } } while (0)

static std::string sha(const std::string& s) {
  Sha256 h;
  h.update(s.data(), s.size());
  char out[65];
  h.finishHex(out);
  return out;
}

static void test_sha256() {
  CHECK(sha("") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
  CHECK(sha("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  CHECK(sha("abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq") ==
        "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1");
  Sha256 h;                                  // a million 'a's, in uneven pieces
  std::string a(997, 'a');
  size_t left = 1000000;
  while (left) { size_t k = left < a.size() ? left : a.size(); h.update(a.data(), k); left -= k; }
  char out[65];
  h.finishHex(out);
  CHECK(strcmp(out, "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0") == 0);
}

static const char* FILE_TEXT =
    "\xEF\xBB\xBFname,module,did,did_size,offset,type,order,mul,div,add,no_value,unit,tier\n"
    "ITOEL,0x12,0x4402,2,0,u16,big,0.75,1,-48,,\xC2\xB0" "C,fast\n"
    "INMOT,0x12,0x4807,2,0,s16,big,1,2,0,,rpm,fast\n"
    "INMOT@0x13,0x13,0x4807,2,0,s16,big,1,2,0,,rpm,fast\n"
    "IMUL1,0x12,0x4A85,2,0,u16,big,1,32768,0,,,medium\n"
    "HU_STATUS_GPS_POSITION.LATITUDE,0x63,0xD039,13,1,gps,big,1,1,0,,,sweep\n"
    "HU_STATUS_GPS_POSITION.LONGITUDE,0x63,0xD039,13,6,gps,big,1,1,0,,,sweep\n"
    "HU_STATUS_GPS_POSITION.HEIGHT,0x63,0xD039,13,11,u16,big,1,1,0,,m,sweep\n"
    "HU_STATUS_SPEED.SPEED_DIFFERENCE_PERCENT,0x63,0xD030,14,4,u16,big,1,1,0,0xFFFF,%,sweep\n"
    "BDC_X,0x40,0xDD00,4,0,f32,big,1,1,0,,,sweep\n"
    "BDC_LE,0x40,0xDD01,2,0,u16,little,1,1,0,,,sweep\n"
    "BDC_S8,0x40,0xDD02,1,0,s8,big,0.5,1,0,,,sweep\n"
    "BDC_TEXT_AFTER,0x40,0xDD03,,2,u8,big,1,1,0,,,sweep\n"
    "BROKEN,0x40,0xZZ,2,0,u16,big,1,1,0,,,sweep\n"
    "TOO_FAR,0x40,0xDD04,2,1,u16,big,1,1,0,,,sweep\n";

static Table table() {
  Table t;
  const char* p = FILE_TEXT;
  while (*p) {
    const char* nl = strchr(p, '\n');
    std::string line(p, nl ? nl - p : strlen(p));
    t.line(line.c_str());
    p = nl ? nl + 1 : p + strlen(p);
  }
  return t;
}

static const Channel& find(const Table& t, const char* name) {
  for (const Channel& c : t.channels) if (strcmp(t.name(c), name) == 0) return c;
  static Channel none{};
  return none;
}

static void test_channel_file() {
  Table t = table();
  CHECK(t.error.empty());
  CHECK(t.channels.size() == 12);
  CHECK(t.skipped == 2);                     // a bad identifier; a value past the reply's end
  CHECK(t.count(FAST) == 3 && t.count(MEDIUM) == 1 && t.count(SLOW) == 8);   // sweep = slow
  const Channel& oil = find(t, "ITOEL");
  CHECK(oil.module == 0x12 && oil.did == 0x4402 && oil.didSize == 2 && oil.type == U16);
  CHECK(oil.tier == FAST && !oil.little && oil.mul == 0.75 && oil.add == -48);
  CHECK(find(t, "INMOT@0x13").module == 0x13);
  CHECK(find(t, "BDC_TEXT_AFTER").didSize == 0);
  CHECK(find(t, "HU_STATUS_SPEED.SPEED_DIFFERENCE_PERCENT").hasNoValue);

  Table bad;
  CHECK(!bad.line("name,module,did\n"));
  CHECK(bad.error == "no column did_size");
}

static void test_decode() {
  Table t = table();
  double v;
  const uint8_t oil[] = {0x00, 0xC0};
  CHECK(decode(find(t, "ITOEL"), oil, 2, v) && v == 96.0);           // 192 * 0.75 - 48
  const uint8_t rpm[] = {0xFF, 0x38};
  CHECK(decode(find(t, "INMOT"), rpm, 2, v) && v == -100.0);         // signed, / 2
  CHECK(!decode(find(t, "INMOT"), rpm, 1, v));                       // short reply
  const uint8_t gps[] = {0x00, 0x00, 0x20, 0x2E, 0x24, 0x32, 0x01, 0x9F, 0x2F, 0x30, 0x05,
                         0x00, 0xC8};
  CHECK(decode(find(t, "HU_STATUS_GPS_POSITION.LATITUDE"), gps, 13, v) &&
        std::fabs(v - 32.776805555555555) < 1e-12);
  CHECK(decode(find(t, "HU_STATUS_GPS_POSITION.LONGITUDE"), gps, 13, v) &&
        std::fabs(v - -97.79668055555555) < 1e-12);
  CHECK(decode(find(t, "HU_STATUS_GPS_POSITION.HEIGHT"), gps, 13, v) && v == 200);
  const uint8_t speed[] = {0, 0, 0, 0, 0xFF, 0xFF, 0, 0, 0, 0, 0, 0, 0, 0};
  CHECK(!decode(find(t, "HU_STATUS_SPEED.SPEED_DIFFERENCE_PERCENT"), speed, 14, v));  // none
  const uint8_t f[] = {0x41, 0x48, 0xF5, 0xC3};
  CHECK(decode(find(t, "BDC_X"), f, 4, v) && std::fabs(v - 12.5600004196167) < 1e-9);
  const uint8_t le[] = {0x34, 0x12};
  CHECK(decode(find(t, "BDC_LE"), le, 2, v) && v == 0x1234);
  const uint8_t s8[] = {0xFE};
  CHECK(decode(find(t, "BDC_S8"), s8, 1, v) && v == -1.0);
}

static void test_plan_batches() {
  Table t = table();
  Plan p;
  p.build(t);
  // Fast: 0x12 0x4402 + 0x4807 together, 0x13 0x4807 on its own (another module).
  size_t at = 0;
  std::vector<size_t> b = p.next(FAST, at, 0);
  CHECK(b.size() == 2 && p.reads[b[0]].module == 0x12 && p.reads[b[1]].did == 0x4807);
  CHECK(p.reads[b[0]].channels.size() == 1);
  b = p.next(FAST, at, 0);
  CHECK(b.size() == 1 && p.reads[b[0]].module == 0x13);
  CHECK(p.next(FAST, at, 0).empty());
  // Sweep: 0x40's three sized reads together, the unsized one alone; 0x63's two.
  at = p.tierStart(SLOW);
  b = p.next(SLOW, at, 0);
  CHECK(b.size() == 3 && p.reads[b[0]].module == 0x40);
  b = p.next(SLOW, at, 0);
  CHECK(b.size() == 1 && p.reads[b[0]].did == 0xDD03);
  b = p.next(SLOW, at, 0);
  CHECK(b.size() == 2 && p.reads[b[0]].module == 0x63);
  CHECK(p.reads[b[1]].channels.size() == 3);        // lat, lon, height share 0xD039
  CHECK(p.next(SLOW, at, 0).empty());
  // Medium: its own read of 0x12.
  at = p.tierStart(MEDIUM);
  b = p.next(MEDIUM, at, 0);
  CHECK(b.size() == 1 && p.reads[b[0]].did == 0x4A85 && p.next(MEDIUM, at, 0).empty());
  // A module that fails batches halves its size, down to one at a time.
  CHECK(p.batchFor(0x40) == MAX_BATCH);
  p.smallerBatches(0x40, MAX_BATCH);
  CHECK(p.batchFor(0x40) == MAX_BATCH / 2);
  p.smallerBatches(0x40, 12);
  p.smallerBatches(0x40, 6);
  CHECK(p.batchFor(0x40) == 3);
  at = p.tierStart(SLOW);
  CHECK(p.next(SLOW, at, 0).size() == 3);
  p.smallerBatches(0x40, 2);                          // a smaller batch too big: half of it
  CHECK(p.batchFor(0x40) == 1);
  at = p.tierStart(SLOW);
  CHECK(p.next(SLOW, at, 0).size() == 1);
  p.smallerBatches(0x40, 1);
  CHECK(p.batchFor(0x40) == 1);
  // Timeouts in a row per module, until it answers.
  CHECK(p.timedOut(0x63) == 1 && p.timedOut(0x63) == 2);
  p.moduleAnswered(0x63);
  CHECK(p.timedOut(0x63) == 1);
}

static void test_plan_rests_failing_reads() {
  Table t = table();
  Plan p;
  p.build(t);
  size_t first = 0;                          // 0x12 0x4402
  for (int i = 0; i < FAILURES_TO_REST; i++) p.failed(first, 1000);
  size_t at = 0;
  std::vector<size_t> b = p.next(FAST, at, 2000);
  CHECK(b.size() == 1 && p.reads[b[0]].did == 0x4807);    // rested one skipped
  at = 0;
  CHECK(p.next(FAST, at, 1000 + REST_MS + 1).size() == 2); // tried again later
  p.answered(first);
  CHECK(p.reads[first].restUntil == 0);
  p.rest(first, 1000, OUT_OF_RANGE_REST_MS);                // refused: rested at once
  at = 0;
  CHECK(p.next(FAST, at, 1000 + REST_MS + 1).size() == 1);
  at = 0;
  CHECK(p.next(FAST, at, 1000 + OUT_OF_RANGE_REST_MS + 1).size() == 2);
}

// One request per module per cycle: fast, then medium and slow in rotation.
static void test_scheduler() {
  Table t = table();
  Plan p;
  p.build(t);
  Scheduler s;
  s.build(p);
  auto dids = [&](const std::vector<size_t>& b) {
    std::vector<uint16_t> d;
    for (size_t i : b) d.push_back(p.reads[i].did);
    return d;
  };
  auto modules = [&](const std::vector<std::vector<size_t>>& c) {
    std::vector<uint8_t> m;
    for (const auto& b : c) m.push_back(p.reads[b[0]].module);
    return m;
  };
  // Cycle 1: 0x12 (two fast, one medium), 0x13 (fast), slow-only 0x40 (its
  // three sized reads; the unsized one waits, its module being asked).
  std::vector<std::vector<size_t>> c = s.cycle(p, 0);
  CHECK((modules(c) == std::vector<uint8_t>{0x12, 0x13, 0x40}));
  CHECK((dids(c[0]) == std::vector<uint16_t>{0x4402, 0x4807, 0x4A85}));
  CHECK((dids(c[2]) == std::vector<uint16_t>{0xDD00, 0xDD01, 0xDD02}));
  CHECK(!s.slowPassDone());                                  // 0x63 not yet
  // Cycle 2: slow-only 0x63, and the unsized 0x40 read alone.
  c = s.cycle(p, 0);
  CHECK((modules(c) == std::vector<uint8_t>{0x12, 0x13, 0x63, 0x40}));
  CHECK(c[3].size() == 1 && p.reads[c[3][0]].did == 0xDD03);
  CHECK(s.slowPassDone());
  CHECK(!s.slowPassDone());                                  // a new pass begins
  // Medium and slow share what's left after the fast reads.
  {
    Table m;
    m.line("name,module,did,did_size,offset,type,order,mul,div,add,no_value,unit,tier");
    m.line("F,0x12,0x0001,2,0,u16,big,1,1,0,,,fast");
    for (int k = 0; k < 40; k++) {
      char line[96];
      snprintf(line, sizeof(line), "M%d,0x12,0x%04X,2,0,u16,big,1,1,0,,,medium", k, 0x100 + k);
      m.line(line);
      snprintf(line, sizeof(line), "S%d,0x12,0x%04X,2,0,u16,big,1,1,0,,,slow", k, 0x200 + k);
      m.line(line);
    }
    Plan mp;
    mp.build(m);
    Scheduler ms;
    ms.build(mp);
    std::vector<std::vector<size_t>> one = ms.cycle(mp, 0);
    size_t med = 0, slw = 0;
    for (size_t i : one[0]) { med += mp.reads[i].tier == MEDIUM; slw += mp.reads[i].tier == SLOW; }
    CHECK(one.size() == 1 && one[0].size() == MAX_BATCH);
    CHECK(med == (MAX_BATCH - 1 + 1) / 2 && slw == MAX_BATCH - 1 - med);
    int cycles = 1;
    while (!ms.slowPassDone() && cycles < 100) { ms.cycle(mp, 0); cycles++; }
    CHECK(cycles <= 4);                                      // 40 slow reads, 11 a cycle
  }
  // A record with fields in two tiers is one read, at the faster tier.
  {
    Table m;
    m.line("name,module,did,did_size,offset,type,order,mul,div,add,no_value,unit,tier");
    m.line("HU_SPEED.GPS,0x63,0xD030,14,0,u16,big,1,1,0,,,medium");
    m.line("HU_SPEED.DIFF,0x63,0xD030,14,4,u16,big,1,1,0,,,slow");
    Plan mp;
    mp.build(m);
    CHECK(mp.reads.size() == 1 && mp.reads[0].tier == MEDIUM && mp.reads[0].channels.size() == 2);
  }
  // A module whose batches shrank below its fast reads gets two requests.
  for (int k = 0; k < 6; k++) p.smallerBatches(0x12, p.batchFor(0x12));
  CHECK(p.batchFor(0x12) == 1);
  c = s.cycle(p, 0);
  CHECK((modules(c)[0] == 0x12 && modules(c)[1] == 0x12));
  CHECK(c[0].size() == 1 && c[1].size() == 1);
}

// The airbag module's temperature and voltage (ALONE) never share a request,
// with each other or anything else; its other reads still batch.
static void test_alone_reads() {
  Table m;
  m.line("name,module,did,did_size,offset,type,order,mul,div,add,no_value,unit,tier");
  m.line("ACSM_SG_INNENTEMPERATUR,0x01,0x4003,2,0,s16,big,1,10,0,,\xC2\xB0" "C,slow");
  m.line("ACSM_SG_SPANNUNG,0x01,0x4007,2,0,u16,big,1,1,0,,mV,slow");
  m.line("ACSM_A,0x01,0x4020,4,0,u32,big,1,1,0,,,slow");
  m.line("ACSM_B,0x01,0x4021,4,0,u32,big,1,1,0,,,slow");
  m.line("ACSM_ONCE,0x01,0x4007,2,0,u16,big,1,1,0,,,slow");
  Plan p;
  p.build(m);
  int alone = 0;
  for (const Read& r : p.reads) alone += r.alone;
  CHECK(alone == 2 && p.reads.size() == 4);
  size_t at = p.tierStart(SLOW);
  std::vector<std::vector<size_t>> got;
  for (std::vector<size_t> b; !(b = p.next(SLOW, at, 0)).empty();) got.push_back(b);
  CHECK(got.size() == 3 && got[0].size() == 1 && got[1].size() == 1 && got[2].size() == 2);
  Scheduler s;
  s.build(p);
  std::vector<uint16_t> seen;
  int together = 0;
  for (int k = 0; k < 6; k++) {
    std::vector<std::vector<size_t>> c = s.cycle(p, 0);
    CHECK(c.size() == 1);                                   // one request to the module a cycle
    for (const std::vector<size_t>& b : c) {
      bool hasAlone = false;
      for (size_t i : b) hasAlone = hasAlone || p.reads[i].alone;
      CHECK(!hasAlone || b.size() == 1);
      if (hasAlone) seen.push_back(p.reads[b[0]].did);
      together += b.size() == 2;
    }
  }
  CHECK(seen.size() == 3 && together == 3);                 // every other turn
  CHECK(std::find(seen.begin(), seen.end(), 0x4003) != seen.end());
  CHECK(std::find(seen.begin(), seen.end(), 0x4007) != seen.end());
}

// Modules still answering are skipped; a module without fast reads sends a
// retry instead of its usual request; at one identifier a request, medium
// and slow take turns; several slow-only modules a cycle.
static void test_scheduler_async() {
  Table m;
  m.line("name,module,did,did_size,offset,type,order,mul,div,add,no_value,unit,tier");
  m.line("F,0x12,0x0001,2,0,u16,big,1,1,0,,,fast");
  m.line("HU_M,0x63,0x0100,2,0,u16,big,1,1,0,,,medium");
  for (int k = 0; k < 4; k++) {
    char line[96];
    snprintf(line, sizeof(line), "HU_S%d,0x63,0x%04X,2,0,u16,big,1,1,0,,,slow", k, 0x200 + k);
    m.line(line);
    snprintf(line, sizeof(line), "A%d,0x40,0x%04X,2,0,u16,big,1,1,0,,,slow", k, 0x300 + k);
    m.line(line);
    snprintf(line, sizeof(line), "B%d,0x30,0x%04X,2,0,u16,big,1,1,0,,,slow", k, 0x400 + k);
    m.line(line);
  }
  Plan p;
  p.build(m);
  Scheduler s;
  s.build(p);
  auto modules = [&](const std::vector<std::vector<size_t>>& c) {
    std::vector<uint8_t> v;
    for (const auto& b : c) v.push_back(p.reads[b[0]].module);
    return v;
  };
  CHECK(s.waits(0x12) && !s.waits(0x63) && !s.waits(0x40));
  // Both slow-only modules in one cycle when allowed; 0x63 busy: skipped.
  std::vector<std::vector<size_t>> c = s.cycle(p, 0, {0x63}, 2);
  CHECK((modules(c) == std::vector<uint8_t>{0x12, 0x30, 0x40}));
  CHECK(!s.slowPassDone());                                   // 0x63 never asked
  c = s.cycle(p, 0, {0x40, 0x30}, 2);
  CHECK((modules(c) == std::vector<uint8_t>{0x12, 0x63}));
  CHECK(s.slowPassDone());
  // A retry goes out on 0x63's next free turn, instead of its usual request.
  std::vector<size_t> half = {c[1][1], c[1][2]};
  s.retry(p, half);
  c = s.cycle(p, 0, {0x63}, 0);
  CHECK((modules(c) == std::vector<uint8_t>{0x12}));
  c = s.cycle(p, 0, {}, 0);
  CHECK((modules(c) == std::vector<uint8_t>{0x12, 0x63}) && c[1] == half);
  c = s.cycle(p, 0, {}, 0);
  CHECK(c.size() == 2 && c[1].size() == 5);                   // back to its usual one
  // A rested read drops out of a retry; a retry of only rested reads is none.
  p.rest(half[0], 0, REST_MS);
  p.rest(half[1], 0, REST_MS);
  s.retry(p, half);
  c = s.cycle(p, 1, {}, 0);
  CHECK(c.size() == 2 && c[1].size() == 3);
  // One identifier a request: medium and slow take turns.
  for (int k = 0; k < 5; k++) p.smallerBatches(0x63, p.batchFor(0x63));
  CHECK(p.batchFor(0x63) == 1);
  int medium = 0, slow = 0;
  for (int k = 0; k < 6; k++) {
    for (const auto& b : s.cycle(p, 1, {}, 0)) {
      if (p.reads[b[0]].module != 0x63) continue;
      CHECK(b.size() == 1);
      (p.reads[b[0]].tier == MEDIUM ? medium : slow)++;
    }
  }
  CHECK(medium == 3 && slow == 3);
}

static void test_split_reply() {
  Table t = table();
  Plan p;
  p.build(t);
  size_t at = p.tierStart(SLOW);
  std::vector<size_t> b = p.next(SLOW, at, 0);            // DD00 (4), DD01 (2), DD02 (1)
  std::vector<const uint8_t*> data;
  std::vector<size_t> len;
  const uint8_t ok[] = {0xDD, 0x02, 0xFE, 0xDD, 0x00, 1, 2, 3, 4};   // DD01 left out
  CHECK(splitReply(ok, sizeof(ok), p.reads, b, data, len));
  CHECK(data[0] == ok + 5 && len[0] == 4 && data[1] == nullptr && data[2] == ok + 2);
  const uint8_t shortr[] = {0xDD, 0x00, 1, 2};
  CHECK(!splitReply(shortr, sizeof(shortr), p.reads, b, data, len));
  const uint8_t stranger[] = {0xDE, 0xAD, 1};
  CHECK(!splitReply(stranger, sizeof(stranger), p.reads, b, data, len));
  std::vector<size_t> one = p.next(SLOW, at, 0);          // unsized DD03
  const uint8_t text[] = {0xDD, 0x03, 'A', 0, 7, 9};
  CHECK(splitReply(text, sizeof(text), p.reads, one, data, len) && len[0] == 4);
  double v;
  CHECK(decode(find(t, "BDC_TEXT_AFTER"), data[0], len[0], v) && v == 7);

  // A late answer: bank 1's last request (oil temperature and engine speed)
  // timed out, and its answer comes while the next one (oil temperature and
  // the trim) waits. Same first identifier, so the client takes it; it lines
  // up with the late request, not this one.
  at = 0;
  std::vector<size_t> last = p.next(FAST, at, 0);         // 4402, 4807
  at = p.tierStart(MEDIUM);
  std::vector<size_t> now = {last[0], p.next(MEDIUM, at, 0)[0]};   // 4402, 4A85
  const uint8_t old[] = {0x44, 0x02, 0, 100, 0x48, 0x07, 0x05, 0xDC};
  const uint8_t fresh[] = {0x44, 0x02, 0, 101, 0x4A, 0x85, 0x80, 0x00};
  CHECK(fitReply(old, sizeof(old), p.reads, now, last, data, len) == Fit::LATE);
  CHECK(fitReply(old, sizeof(old), p.reads, now, {}, data, len) == Fit::OUT_OF_LINE);
  CHECK(fitReply(fresh, sizeof(fresh), p.reads, now, last, data, len) == Fit::LINED);
  CHECK(data[1] == fresh + 6);
  CHECK(fitReply(stranger, sizeof(stranger), p.reads, now, last, data, len) == Fit::OUT_OF_LINE);
}

static void test_clock_and_names() {
  CHECK(clockText(1792413770123LL, -300) == "2026-10-19 07:42:50.123-05:00");
  CHECK(clockText(1792413770123LL, 0) == "2026-10-19 12:42:50.123+00:00");
  CHECK(clockText(1767236400000LL, -360) == "2025-12-31 21:00:00.000-06:00");   // year back
  CHECK(clockText(1767236400000LL, 330) == "2026-01-01 08:30:00.000+05:30");
  CHECK(fileStamp(1792413770123LL, -300) == "2026-10-19_07-42-50");
  CHECK(daysFromCivil(1970, 1, 1) == 0 && daysFromCivil(2026, 10, 19) == 20745);
  CHECK(utcOffsetMinutes(1792413770, 2026, 10, 19, 7, 42, 50) == -300);
  CHECK(utcOffsetMinutes(1767236400, 2026, 1, 1, 8, 30, 0) == 330);
}

static std::string cell(double v, Type t = U16) {
  std::string s;
  appendValue(s, v, t);
  return s;
}

static void test_value_cells() {
  CHECK(cell(96) == "96");
  CHECK(cell(-100) == "-100");
  CHECK(cell(0) == "0");
  CHECK(cell(-0.0) == "0");
  CHECK(cell(12.538854) == "12.538854");
  CHECK(cell(0.023481 * 534) == "12.538854");     // rounded to 6 places
  CHECK(cell(-0.75) == "-0.75");
  CHECK(cell(1003.0078125) == "1003.007813");
  CHECK(cell(4294967295.0) == "4294967295");
  CHECK(cell(0.00001234) == "1.234e-05");          // small: printf
  CHECK(cell(2e15) == "2e+15");                     // large: printf
  CHECK(cell(NAN).empty());
  CHECK(cell(32.776805555555555, GPS) == "32.7768056");
  CHECK(cell(-97.79668055555555, GPS) == "-97.7966806");
}

static void test_csv_rows() {
  Table t = table();
  LogFile fast, sweep;
  fast.setup(t, true);
  sweep.setup(t, false);
  CHECK(fast.header(t) == "Time,Clock,ITOEL,INMOT,INMOT@0x13,IMUL1,Event\n");   // medium too
  CHECK(sweep.columns.size() == 8);
  std::vector<double> v(t.channels.size(), NAN);
  v[0] = 96.0; v[2] = -0.5;
  CHECK(fast.row(t, 1.25, "2026-10-19 07:42:50.123-05:00", v, "start") ==
        "1.250,2026-10-19 07:42:50.123-05:00,96,,-0.5,,start\n");
  v[4] = 32.776805555555555;
  std::string r = sweep.row(t, 2, "", v);
  CHECK(r.rfind("2.000,,32.7768056,", 0) == 0);
  CHECK(r.size() >= 2 && r.substr(r.size() - 2) == ",\n");
}

static void test_fault_parse() {
  // 59 02 FF, then two records: 480A14 failed and stored, 930A17 listed only
  const uint8_t reply[] = {0x59, 0x02, 0xFF, 0x48, 0x0A, 0x14, 0x2F, 0x93, 0x0A, 0x17, 0x50, 0x01};
  std::vector<Dtc> d;
  CHECK(parseDtcs(reply, sizeof(reply), d));
  CHECK(d.size() == 2);                       // the trailing odd byte is ignored
  CHECK(d[0].code == 0x480A14 && d[0].status == 0x2F);
  CHECK(d[1].code == 0x930A17 && d[1].status == 0x50);
  const uint8_t other[] = {0x62, 0x48, 0x07};
  CHECK(!parseDtcs(other, sizeof(other), d));
  const uint8_t none[] = {0x59, 0x02, 0xFF};
  CHECK(parseDtcs(none, sizeof(none), d) && d.empty());
}

static void test_fault_status() {
  CHECK(dtcFailed(0x01) && dtcFailed(0x04) && dtcFailed(0x20));
  CHECK(dtcFailed(0x08));                     // confirmed, tested since the clear
  CHECK(!dtcFailed(0x18));                    // confirmed, but not tested since: the DSC's list
  CHECK(!dtcFailed(0x50) && !dtcFailed(0x00));
  CHECK(dtcState(0x09) == "present, stored");
  CHECK(dtcState(0x01) == "present, not yet stored");
  CHECK(dtcState(0x2C) == "not present, stored");
  CHECK(dtcState(0x80) == "not present, warning lamp requested");
}

static void test_fault_rows() {
  std::vector<Dtc> d = {{0x930A17, 0x50}, {0x480A14, 0x2F}, {0x0A1B2C, 0x09}};
  std::string rows = faultRows("2026-10-25 12:43:10", 65.04, 0x12, 0, d);
  CHECK(rows ==
        "2026-10-25 12:43:10,0x12,\"Engine, master DME\",0A1B2C,0x09,\"present, stored\",,,,,,,,65.0\n"
        "2026-10-25 12:43:10,0x12,\"Engine, master DME\",480A14,0x2F,\"present, stored\",,,,,,,,65.0\n");
  std::vector<Dtc> quiet = {{0x930A17, 0x50}};
  CHECK(faultRows("", 70, 0x29, 0, quiet) ==
        ",0x29,\"Stability control (DSC)\",,,\"no faults (1 listed codes never failed)\",,,,,,,,70.0\n");
  CHECK(faultRows("", 70, 0x29, 0, {}) == ",0x29,\"Stability control (DSC)\",,,\"no faults\",,,,,,,,70.0\n");
  CHECK(faultRows("", 1, 0x99, 0x31, {}) == ",0x99,\"\",,,\"refused (nrc 0x31)\",,,,,,,,1.0\n");
  CHECK(faultRows("", 1, 0x60, -1, {}) ==
        ",0x60,\"Instrument cluster (KOMBI)\",,,\"no reply\",,,,,,,,1.0\n");
  // as many columns as the header
  auto commas = [](const std::string& s) {
    size_t n = 0; bool q = false;
    for (char c : s) { if (c == '"') q = !q; else if (c == ',' && !q) n++; }
    return n;
  };
  std::string one = rows.substr(0, rows.find('\n') + 1);
  CHECK(commas(one) == commas(FAULT_HEADER));
}

static void test_fault_scan() {
  FaultScan s;
  s.start(1000);
  CHECK(s.next(1000) == -1);
  CHECK(s.next(1000 + FaultScan::FIRST_MS - 1) == -1);     // the first minute is the logging's
  uint32_t t = 1000 + FaultScan::FIRST_MS;
  std::vector<int> seen;
  for (size_t i = 0; i < FAULT_MODULE_COUNT; i++) {
    int m = s.next(t);
    CHECK(m >= 0);
    CHECK(s.next(t + 1) == -1);                           // one at a time, a gap between
    seen.push_back(m);
    t += FaultScan::GAP_MS;
  }
  CHECK(seen.size() == FAULT_MODULE_COUNT && seen[0] == 0x01 && seen.back() == 0x78);
  CHECK(s.next(t) == -1);                                 // all read; the DMEs come later
  CHECK(s.fullScanDone());
  CHECK(s.next(t + FaultScan::DME_EVERY_MS - 1) == -1);
  t += FaultScan::DME_EVERY_MS;
  CHECK(s.next(t) == 0x12);
  CHECK(s.next(t + FaultScan::GAP_MS) == 0x13);
  CHECK(s.next(t + 2 * FaultScan::GAP_MS) == -1);
  CHECK(s.next(t + FaultScan::DME_EVERY_MS) == 0x12);
  CHECK(std::string(moduleName(0x40)) == "Body domain controller (BDC)" && !*moduleName(0x99));
}

// A fault's details: the requests, the odometer from the snapshots (past a
// module's own values), the count, the row, and each code once.
static void test_fault_details() {
  uint8_t req[6];
  CHECK(detailRequest(SNAPSHOTS, 0x480A14, req) == 6);
  CHECK(req[0] == 0x19 && req[1] == 0x04 && req[2] == 0x48 && req[3] == 0x0A && req[4] == 0x14 &&
        req[5] == 0xFF);
  // Two records: 1700 odometer, 1701 time, then a module's own 0x4A01 the
  // board can't size, so the second record is found by its header.
  const uint8_t snap[] = {0x59, 0x04, 0x48, 0x0A, 0x14, 0x2F,
                          0x01, 0x03, 0x17, 0x00, 0x00, 0x30, 0x39,     // 12,345 km
                          0x17, 0x01, 0x00, 0x00, 0x10, 0x00,
                          0x4A, 0x01, 0xAA, 0xBB,
                          0x02, 0x03, 0x17, 0x00, 0x00, 0x31, 0x02,     // 12,546 km
                          0x17, 0x01, 0x00, 0x00, 0x20, 0x00,
                          0x4A, 0x01, 0xCC, 0xDD};
  uint32_t first = 0, last = 0;
  CHECK(snapshotKm(snap, sizeof(snap), 0x12, first, last));
  CHECK(first == 12345 && last == 12546);
  const uint8_t one[] = {0x59, 0x04, 0x48, 0x0A, 0x14, 0x2F, 0x01, 0x02,
                         0x17, 0x31, 0x01, 0x17, 0x00, 0x01, 0x00, 0x00};   // class, then 65,536
  CHECK(snapshotKm(one, sizeof(one), 0x40, first, last) && first == 65536 && last == 65536);
  const uint8_t none[] = {0x59, 0x04, 0x48, 0x0A, 0x14, 0x2F};
  CHECK(!snapshotKm(none, sizeof(none), 0x12, first, last));
  const uint8_t unset[] = {0x59, 0x04, 0x48, 0x0A, 0x14, 0x2F, 0x01, 0x01, 0x17, 0x00, 0xFF, 0xFF, 0xFF};
  CHECK(!snapshotKm(unset, sizeof(unset), 0x12, first, last));
  const uint8_t cut[] = {0x59, 0x04, 0x48, 0x0A, 0x14, 0x2F, 0x01, 0x02, 0x17, 0x00, 0x00, 0x30};
  CHECK(!snapshotKm(cut, sizeof(cut), 0x12, first, last));
  CHECK(!snapshotKm(req, sizeof(req), 0x12, first, last));
  const uint8_t ext[] = {0x59, 0x06, 0x48, 0x0A, 0x14, 0x2F, 0x01, 0x40, 0x02, 0x07, 0x03, 0x28};
  CHECK(occurrences(ext, sizeof(ext)) == 7);
  const uint8_t noCount[] = {0x59, 0x06, 0x48, 0x0A, 0x14, 0x2F, 0x01, 0x40};
  CHECK(occurrences(noCount, sizeof(noCount)) == -1);
  CHECK(occurrences(snap, sizeof(snap)) == -1);
  CHECK(detailCell(0, ext, 4) == "5906480A" && detailCell(0x31, ext, 0) == "refused (nrc 0x31)" &&
        detailCell(-1, ext, 0) == "no reply");
  std::string row = detailRow("2026-10-29 08:15:02", 512.5, 0x29, 0x480A14, true, 12345, 12546, 7,
                              "5904", "no reply");
  CHECK(row == "2026-10-29 08:15:02,0x29,\"Stability control (DSC)\",480A14,12345,12546,7,"
               "\"5904\",\"no reply\",512.5\n");
  CHECK(detailRow("", 1, 0x12, 0x1A, false, 0, 0, -1, "", "") ==
        ",0x12,\"Engine, master DME\",00001A,,,,\"\",\"\",1.0\n");
  auto commas = [](const std::string& s) {
    size_t n = 0; bool q = false;
    for (char c : s) { if (c == '"') q = !q; else if (c == ',' && !q) n++; }
    return n;
  };
  CHECK(commas(row) == commas(DETAIL_HEADER));
  DetailQueue q;
  q.add(0x29, 0x480A14);
  q.add(0x12, 0x1A400A);
  q.add(0x29, 0x480A14);                       // named again: once a power-on
  CHECK(q.size() == 2 && q.next()->module == 0x29);
  CHECK(q.next({0x29})->module == 0x12 && !q.next({0x29, 0x12}));
  q.done(0x29, 0x480A14);
  q.add(0x29, 0x480A14);                       // read already: not again
  CHECK(q.size() == 1 && q.next()->code == 0x1A400A);
  q.done(0x12, 0x1A400A);
  CHECK(q.empty() && !q.next());
}

// The periodic tiers: parsed by name, logged in the sweep file, asked once
// each (due tiers only, after openOnce), and when each is due.
static void test_periodic() {
  Table t;
  t.line("name,module,did,did_size,offset,type,order,mul,div,add,no_value,unit,tier");
  t.line("F,0x12,0x0001,2,0,u16,big,1,1,0,,,fast");
  t.line("S,0x12,0x0002,2,0,u16,big,1,1,0,,,slow");
  t.line("ODO,0x12,0x0003,2,0,u16,big,1,1,0,,,drive");
  t.line("TIRE1,0x29,0xDC98,24,5,u16,big,1,1000,0,,bar,daily");
  t.line("TIRE1T,0x29,0xDC98,24,7,u8,big,1,1,-50,,,daily");
  t.line("CODING,0x40,0x1000,,0,u8,big,1,1,0,,,weekly");     // unsized: alone
  t.line("SERVICE,0x40,0x1001,2,0,u16,big,1,1,0,,,monthly");
  t.line("NOPE,0x40,0x1002,2,0,u16,big,1,1,0,,,off");
  t.line("ODD,0x40,0x1003,2,0,u16,big,1,1,0,,,hourly");     // unknown: slow
  CHECK(t.channels.size() == 8 && t.skipped == 1);
  CHECK(t.count(DRIVE) == 1 && t.count(DAILY) == 2 && t.count(WEEKLY) == 1 && t.count(MONTHLY) == 1);
  CHECK(find(t, "ODD").tier == SLOW && periodic(DAILY) && !periodic(SLOW));
  CHECK(std::string(tierName(MONTHLY)) == "monthly");
  LogFile fast, sweep;
  fast.setup(t, true);
  sweep.setup(t, false);
  CHECK(fast.columns.size() == 1 && sweep.columns.size() == 7);

  Plan p;
  p.build(t);
  auto asked = [&](Scheduler& s, int cycles) {
    std::vector<uint16_t> d;
    for (int k = 0; k < cycles; k++) {
      for (const auto& b : s.cycle(p, 0, {}, 4)) for (size_t i : b) d.push_back(p.reads[i].did);
    }
    return d;
  };
  auto count = [](const std::vector<uint16_t>& d, uint16_t did) {
    return (int)std::count(d.begin(), d.end(), did);
  };
  // Due: drive and daily only. Nothing periodic before openOnce.
  Scheduler s;
  s.build(p, (1u << DRIVE) | (1u << DAILY));
  CHECK(s.onceCount() == 2 && s.hasSlow());
  std::vector<uint16_t> d = asked(s, 5);
  CHECK(count(d, 0x0003) == 0 && count(d, 0xDC98) == 0 && count(d, 0x0002) >= 1);
  CHECK(!s.onceDone());
  s.openOnce();
  d = asked(s, 10);
  CHECK(count(d, 0x0003) == 1 && count(d, 0xDC98) == 1);       // once each, one read for both fields
  CHECK(count(d, 0x1000) == 0 && count(d, 0x1001) == 0);       // weekly and monthly not due
  CHECK(s.onceDone() && !s.onceDone());                         // reported once
  // All due: the unsized weekly read goes alone, once.
  Scheduler all;
  all.build(p);
  all.openOnce();
  d = asked(all, 10);
  CHECK(count(d, 0x1000) == 1 && count(d, 0x1001) == 1 && count(d, 0xDC98) == 1);
  CHECK(all.onceDone());
  // No periodic reads: never "done", so no extra sweep row.
  Scheduler none;
  none.build(p, 0);
  none.openOnce();
  asked(none, 3);
  CHECK(none.onceCount() == 0 && !none.onceDone());

  // When each is due. 2026-10-26 08:00 UTC; Central time is UTC-5 then.
  const uint32_t now = 1793001600u;
  CHECK(periodicDue(DRIVE, now, now, -300));
  CHECK(periodicDue(DAILY, 0, now, -300) && periodicDue(WEEKLY, 0, 0, -300));   // never / no clock
  CHECK(!periodicDue(DAILY, now - 3600, now, -300));            // earlier the same local day
  CHECK(periodicDue(DAILY, now - 10 * 3600, now, -300));         // 17:00 local, the day before
  CHECK(!periodicDue(WEEKLY, now - 6 * 86400, now, -300));
  CHECK(periodicDue(WEEKLY, now - 7 * 86400 + 1800, now, -300)); // within the hour's slack
  CHECK(!periodicDue(MONTHLY, now - 29 * 86400, now, -300) && periodicDue(MONTHLY, now - 30 * 86400, now, -300));
}

// A shrunk batch size grows back after enough whole answers, needing twice
// as many each time the bigger size fails; medium keeps a share of a module
// whose fast reads fill its requests.
static void test_batch_recovery() {
  Plan p;
  CHECK(p.batchFor(0x12) == MAX_BATCH && !p.goodBatch(0x12, MAX_BATCH));   // nothing to grow
  p.smallerBatches(0x12, MAX_BATCH);                       // 24 -> 12
  CHECK(p.batchFor(0x12) == MAX_BATCH / 2);
  int n = 0;
  while (!p.goodBatch(0x12, MAX_BATCH / 2) && n < 1000) n++;
  CHECK(n + 1 == RECOVER_AFTER && p.batchFor(0x12) == MAX_BATCH);
  p.smallerBatches(0x12, MAX_BATCH);                       // fails again: twice as long
  n = 0;
  while (!p.goodBatch(0x12, MAX_BATCH / 2) && n < 1000) n++;
  CHECK(n + 1 == 2 * RECOVER_AFTER);
  // Answers smaller than the current size prove nothing.
  p.smallerBatches(0x12, MAX_BATCH);
  for (int k = 0; k < 500; k++) CHECK(!p.goodBatch(0x12, 3));
  CHECK(p.batchFor(0x12) == MAX_BATCH / 2);
  // A module that only takes one at a time comes back to one.
  Plan one;
  for (int k = 0; k < 5; k++) one.smallerBatches(0x40, one.batchFor(0x40));
  CHECK(one.batchFor(0x40) == 1);
  n = 0;
  while (!one.goodBatch(0x40, 1) && n < 100000) n++;
  CHECK(one.batchFor(0x40) == 2);
  one.smallerBatches(0x40, 2);
  CHECK(one.batchFor(0x40) == 1);

  // Ten fast reads, batches of six: every cycle medium gets its share (two
  // of six), and every other cycle a third request carries medium and slow.
  Table t;
  t.line("name,module,did,did_size,offset,type,order,mul,div,add,no_value,unit,tier");
  char line[96];
  for (int k = 0; k < 10; k++) {
    snprintf(line, sizeof(line), "F%d,0x12,0x%04X,2,0,u16,big,1,1,0,,,fast", k, 0x10 + k);
    t.line(line);
  }
  for (int k = 0; k < 30; k++) {
    snprintf(line, sizeof(line), "M%d,0x12,0x%04X,2,0,u16,big,1,1,0,,,medium", k, 0x100 + k);
    t.line(line);
    snprintf(line, sizeof(line), "S%d,0x12,0x%04X,2,0,u16,big,1,1,0,,,slow", k, 0x200 + k);
    t.line(line);
  }
  Plan mp;
  mp.build(t);
  mp.smallerBatches(0x12, MAX_BATCH);
  mp.smallerBatches(0x12, MAX_BATCH / 2);
  CHECK(mp.batchFor(0x12) == 6);
  Scheduler s;
  s.build(mp);
  size_t slowSeen = 0, requests = 0;
  for (int c = 0; c < 4; c++) {
    size_t fast = 0, med = 0, slw = 0;
    std::vector<std::vector<size_t>> reqs = s.cycle(mp, 0);
    for (const auto& b : reqs) {
      CHECK(b.size() <= 6);
      for (size_t i : b) {
        fast += mp.reads[i].tier == FAST;
        med += mp.reads[i].tier == MEDIUM;
        slw += mp.reads[i].tier == SLOW;
      }
    }
    CHECK(fast == 10 && med >= 2);
    slowSeen += slw;
    requests += reqs.size();
  }
  CHECK(slowSeen >= 2 && requests == 10);                   // 3, 2, 3, 2
  // At full size nothing changes: one request, medium and slow halve what's left.
  Plan full;
  full.build(t);
  Scheduler f;
  f.build(full);
  std::vector<std::vector<size_t>> reqs = f.cycle(full, 0);
  CHECK(reqs.size() == 1 && reqs[0].size() == MAX_BATCH);
}

// The ACSM's clock: this car's answer on 2026-10-18, and what's refused.
static void test_car_clock() {
  const uint8_t got[] = {0x16, 0x1f, 0x0a, 0x12, 0xa7, 0x07, 0xea, 0x01};
  CarTime t;
  CHECK(carClock(got, sizeof(got), t));
  CHECK(t.year == 2026 && t.month == 10 && t.day == 18);
  CHECK(t.hour == 22 && t.minute == 31 && t.second == 10);
  uint8_t bad[8];
  memcpy(bad, got, 8);
  bad[7] = 0x00;                                             // the display's clock isn't set
  CHECK(!carClock(bad, 8, t));
  memcpy(bad, got, 8);
  bad[0] = 0xFF;                                             // hour "invalid"
  CHECK(!carClock(bad, 8, t));
  memcpy(bad, got, 8);
  bad[4] = 0x07;                                             // month 0
  CHECK(!carClock(bad, 8, t));
  memcpy(bad, got, 8);
  bad[5] = 0x00; bad[6] = 0x00;                              // year 0
  CHECK(!carClock(bad, 8, t));
  CHECK(!carClock(got, 7, t));                               // short
}

// Bursts: start on the pedal or the boost, end when both drop for a second
// or after ten, and wait five before the next.
static void test_burst() {
  Burst b;
  CHECK(b.update(0, NAN, NAN) == Burst::NONE && !b.on());
  CHECK(b.update(100, 50, 1200) == Burst::NONE);
  CHECK(b.update(200, 85, 1200) == Burst::STARTED && b.on());
  CHECK(b.update(900, 30, 1800) == Burst::NONE);             // boost keeps it going
  CHECK(b.update(1800, 30, 1200) == Burst::NONE);            // 0.9 s clear
  CHECK(b.update(1950, 30, 1200) == Burst::ENDED && !b.on());
  CHECK(b.count() == 1 && b.ms() == 1750);
  CHECK(b.update(3000, 95, 2000) == Burst::NONE);            // within the gap
  CHECK(b.update(6950, 95, 2000) == Burst::STARTED);
  for (uint32_t t = 7000; t < 16950; t += 50) CHECK(b.update(t, 95, 2000) == Burst::NONE);
  CHECK(b.update(16950, 95, 2000) == Burst::ENDED);          // ten seconds at most
  CHECK(b.update(17000, 95, 2000) == Burst::NONE);           // still held: the gap first
  CHECK(b.update(21950, 95, 2000) == Burst::STARTED && b.count() == 3);
  b.reset();
  CHECK(!b.on() && b.count() == 0 && b.ms() == 0);
}

// In a burst only the fast reads go: no medium, slow, periodic or slow-only
// requests, and the medium rotation picks up where it was afterwards.
static void test_scheduler_fast_only() {
  Table t = table();
  Plan p;
  p.build(t);
  Scheduler s;
  s.build(p);
  s.openOnce();
  std::vector<std::vector<size_t>> c = s.cycle(p, 0, {}, 4, true);
  CHECK(c.size() == 2);                                      // 0x12 and 0x13
  for (const auto& b : c) for (size_t i : b) CHECK(p.reads[i].tier == FAST);
  c = s.cycle(p, 0, {0x13}, 4, true);                        // a busy module still skipped
  CHECK(c.size() == 1 && p.reads[c[0][0]].module == 0x12);
  c = s.cycle(p, 0);
  bool medium = false;
  for (const auto& b : c) for (size_t i : b) medium = medium || p.reads[i].tier == MEDIUM;
  CHECK(medium);
}

// The Worker's firmware manifest, and which builds the board takes.
static void test_ota() {
  const std::string sha(64, 'a');
  Manifest m;
  CHECK(parseManifest("{\"version\":\"20261027-0200-abc1234\",\"sha256\":\"" + sha +
                      "\",\"size\":1395456,\"uploaded\":\"2026-10-27T02:00:00.000Z\"}", m));
  CHECK(m.version == "20261027-0200-abc1234" && m.sha256 == sha && m.size == 1395456);
  CHECK(parseManifest("{ \"size\" : 12 , \"sha256\" : \"" + sha + "\", \"version\" : \"v2\" }", m));
  CHECK(m.size == 12 && m.version == "v2");
  CHECK(!parseManifest("{\"error\":\"no firmware published\"}", m));
  CHECK(!parseManifest("{\"version\":\"v\",\"sha256\":\"abc\",\"size\":1}", m));           // short hash
  CHECK(!parseManifest("{\"version\":\"v\",\"sha256\":\"" + std::string(64, 'G') + "\",\"size\":1}", m));
  CHECK(!parseManifest("{\"version\":\"v\",\"sha256\":\"" + sha + "\",\"size\":0}", m));
  CHECK(!parseManifest("{\"version\":\"v\",\"sha256\":\"" + sha + "\",\"size\":\"x\"}", m));
  CHECK(wantUpdate("v1", "v2", ""));
  CHECK(wantUpdate("v2", "v1", ""));                         // going back is publishing the old one
  CHECK(!wantUpdate("v1", "v1", ""));
  CHECK(!wantUpdate("v1", "v2", "v2"));                      // rolled back once: not again
  CHECK(!wantUpdate("dev", "v2", ""));                       // bench builds stay put
  CHECK(!wantUpdate("v1", "", ""));
}

// At power-on: a new build is on trial; the build before a new one that
// didn't prove itself knows it went back; and the card's image is kept only
// while it's still wanted.
static void test_ota_start() {
  CHECK(otaStart(true, "v2", "v2") == OTA_TRIAL);
  CHECK(otaStart(true, "", "v2") == OTA_TRIAL);
  CHECK(otaStart(false, "v2", "v1") == OTA_WENT_BACK);
  CHECK(otaStart(false, "", "v1") == OTA_NORMAL);
  CHECK(otaStart(false, "v1", "v1") == OTA_NORMAL);
  CHECK(!trialOver(false, 0xFFFFFFFF));
  CHECK(!trialOver(true, OTA_TRIAL_MS - 1));
  CHECK(trialOver(true, OTA_TRIAL_MS));
  // Parked without Wi-Fi or the car the trial waits; only time with a chance counts.
  TrialClock tc;
  CHECK(tc.tick(1000, false) == 0);
  CHECK(tc.tick(OTA_TRIAL_MS * 3, false) == 0);
  CHECK(tc.tick(OTA_TRIAL_MS * 3 + 60000, true) == 60000);
  CHECK(tc.tick(OTA_TRIAL_MS * 3 + 90000, false) == 60000);
  CHECK(tc.tick(OTA_TRIAL_MS * 3 + 90000 + OTA_TRIAL_MS - 60000, true) == OTA_TRIAL_MS);
  CHECK(cardImageWanted(true, "v2", "v1", ""));
  CHECK(!cardImageWanted(false, "v2", "v1", ""));            // gone from the card
  CHECK(!cardImageWanted(true, "v2", "v1", "v2"));           // went back from it
  CHECK(!cardImageWanted(true, "v1", "v1", ""));             // installed already
  CHECK(!cardImageWanted(true, "v2", "dev", ""));
}

// A module that keeps timing out is left alone a while, on its own: two in
// a row start 2 s, each further back-off after one doubles to 30 s, an
// answer starts over; other modules aren't touched.
static void test_backoff() {
  Backoff b;
  CHECK(!b.waiting(0x12, 0) && b.modules(0).empty());
  CHECK(!b.timedOut(0x12, 1000));
  b.answered(0x12);                                          // one that answered: count again
  CHECK(!b.timedOut(0x12, 1100));
  CHECK(b.timedOut(0x12, 1200) && b.waitMs(0x12) == BACKOFF_MS);   // until 3201
  CHECK(b.waiting(0x12, 1200 + BACKOFF_MS) && !b.waiting(0x12, 1201 + BACKOFF_MS));
  CHECK(!b.waiting(0x13, 1300) && b.modules(1300) == std::vector<uint8_t>{0x12});
  CHECK(!b.timedOut(0x12, 1300));                            // a late one from before: no change
  uint32_t now = 10000;
  for (uint32_t want : {4000u, 8000u, 16000u, 30000u, 30000u}) {
    CHECK(!b.timedOut(0x12, now));
    CHECK(b.timedOut(0x12, now) && b.waitMs(0x12) == want);
    now += want + 1;                                         // "until" is odd (never 0)
  }
  b.answered(0x12);
  CHECK(!b.waiting(0x12, now) && b.waitMs(0x12) == 0);
  CHECK(!b.timedOut(0x12, now) && b.timedOut(0x12, now) && b.waitMs(0x12) == BACKOFF_MS);
  b.reset();                                                 // a drive starts
  CHECK(b.modules(now).empty());
  uint32_t late = 0xFFFFFFFF - 1000;                         // millis() wraps after 49 days
  b.timedOut(0x40, late);
  CHECK(b.timedOut(0x40, late) && b.waiting(0x40, late + 1500) && !b.waiting(0x40, late + 2000));
}

static void test_upload_rules() {
  // The way there shut ends the round; this file's trouble skips just it.
  CHECK(uploadStopsRound(-1) && uploadStopsRound(0) && uploadStopsRound(401) &&
        uploadStopsRound(403) && uploadStopsRound(429));
  CHECK(!uploadStopsRound(200) && !uploadStopsRound(400) && !uploadStopsRound(413) &&
        !uploadStopsRound(500) && !uploadStopsRound(503));
  // A file that keeps failing is spaced out after three.
  CHECK(uploadTryNow(0, 5) && uploadTryNow(2, 5));
  CHECK(!uploadTryNow(3, 5) && uploadTryNow(3, 8) && uploadTryNow(9, 16));
  const std::string sha(64, 'c');
  Done d;
  CHECK(parseDone(sha + " 1234", d) && d.sha == sha && d.size == 1234);
  CHECK(parseDone(sha + " 1234\r", d) && d.size == 1234);
  CHECK(!parseDone("abc 12", d));
  CHECK(!parseDone("", d));
  CHECK(uploadStep(false, true, 100) == UPLOAD_BAD_DONE);
  CHECK(uploadStep(true, false, 0) == UPLOAD_MISSING);
  CHECK(uploadStep(true, true, 0) == UPLOAD_EMPTY);          // removed: sending one hung (26 Oct)
  CHECK(uploadStep(true, true, 1) == UPLOAD_SEND);
  std::string reply = "{\"name\":\"nt-00066-01.csv\",\"sha256\":\"" + sha + "\",\"processed\":true}";
  CHECK(uploadAccepted(200, reply, sha));
  CHECK(!uploadAccepted(200, reply, std::string(64, 'd')));  // the Worker got something else
  CHECK(!uploadAccepted(500, reply, sha));
  CHECK(!uploadAccepted(-1, "", sha));
}

static std::string batchOf(const std::string& file, size_t max, uint32_t& used, size_t& count,
                           size_t piece = 7) {
  BeatBatch b(max);
  for (size_t i = 0; i < file.size(); i += piece) {
    std::string p = file.substr(i, piece);
    if (!b.feed((const uint8_t*)p.data(), p.size())) break;
  }
  used = b.used();
  count = b.count();
  return b.body();
}

// The check-ins kept offline: whole lines, as many as fit a request, and
// every one sent once whatever the batch size.
static void test_kept_beats() {
  CHECK(beatFits(BEATS_MAX_BYTES - 10, 10));
  CHECK(!beatFits(BEATS_MAX_BYTES - 10, 11));                // full: later ones dropped
  uint32_t used;
  size_t count;
  CHECK(batchOf("{\"a\":1}\n{\"b\":2}\n", 1000, used, count) == "{\"beats\":[{\"a\":1},{\"b\":2}]}");
  CHECK(used == 16 && count == 2);
  CHECK(batchOf("{\"a\":1}\n{\"b\"", 1000, used, count) == "{\"beats\":[{\"a\":1}]}");
  CHECK(used == 8 && count == 1);                            // a torn last line waits
  CHECK(batchOf("", 1000, used, count) == "{\"beats\":[]}" && used == 0 && count == 0);
  // 10 for {"beats":[, 2 for ]}: one 10-byte line fits in 30, two don't.
  std::string two = "0123456789\nabcdefghij\n";
  CHECK(batchOf(two, 30, used, count) == "{\"beats\":[0123456789]}" && used == 11 && count == 1);
  CHECK(batchOf(two.substr(used), 30, used, count) == "{\"beats\":[abcdefghij]}");
  CHECK(batchOf(two, 5, used, count, 100) == "{\"beats\":[0123456789]}");   // too long: alone
  // A day of check-ins, batch after batch from where the last ended.
  std::string file, sent;
  for (int i = 0; i < 500; i++) file += "{\"boot\":" + std::to_string(i) + ",\"x\":\"" +
                                        std::string(i % 37, 'y') + "\"}\n";
  uint32_t sentTo = 0;
  int batches = 0;
  for (;;) {
    std::string body = batchOf(file.substr(sentTo), 1000, used, count, 64);
    if (!count) break;
    CHECK(body.size() <= 1000);
    std::string inner = body.substr(10, body.size() - 12);
    sent += (sent.empty() ? "" : ",") + inner;
    sentTo += used;
    batches++;
  }
  std::string all;
  for (size_t i = 0, j; i < file.size(); i = j + 1) {
    j = file.find('\n', i);
    all += (all.empty() ? "" : ",") + file.substr(i, j - i);
  }
  CHECK(sentTo == file.size() && sent == all && batches > 10);
}

// The full channel check: the Worker's list, each answer judged as the
// app's check does, its row in the app's check CSV, and a silent module
// skipped after three in a row.
static void test_car_check() {
  std::vector<CheckItem> l = parseCheckList(
      "module,did,size,exact\n0x12,0x4029,8,1\r\n0x63,0xd104,2,0\nbad line\n0x1FF,0x1,1,1\n0x40,0x1000,64,1");
  CHECK(l.size() == 3);
  CHECK(l[0].module == 0x12 && l[0].did == 0x4029 && l[0].size == 8 && l[0].exact);
  CHECK(l[1].module == 0x63 && l[1].did == 0xD104 && l[1].size == 2 && !l[1].exact);
  CHECK(l[2].module == 0x40 && l[2].size == 64);              // no newline at the end
  CHECK(parseCheckList("").empty());
  const CheckItem exact = {0x12, 0x4029, 8, true}, atLeast = {0x63, 0xD104, 2, false};
  CHECK(checkStatus(true, false, 8, exact) == CHECK_OK);
  CHECK(checkStatus(true, false, 9, exact) == CHECK_SIZE);
  CHECK(checkStatus(true, false, 7, exact) == CHECK_SIZE);
  CHECK(checkStatus(true, false, 30, atLeast) == CHECK_OK);
  CHECK(checkStatus(true, false, 1, atLeast) == CHECK_SIZE);
  CHECK(checkStatus(false, true, 0, exact) == CHECK_REFUSED);
  CHECK(checkStatus(false, false, 0, exact) == CHECK_NO_ANSWER);
  CHECK(std::string(CHECK_HEADER) == "module,did,job,status,detail,reply_bytes,raw\n");
  CHECK(checkRow(exact, "logger sweep s1", CHECK_OK, 0, 8) ==
        "0x12,0x4029,logger sweep s1,ok,8 bytes,8,\n");
  CHECK(checkRow(exact, "j", CHECK_REFUSED, 0x31, 0) == "0x12,0x4029,j,refused,0x31,,\n");
  CHECK(checkRow(atLeast, "j", CHECK_SIZE, 0, 1) ==
        "0x63,0xD104,j,size,got 1 bytes; expected >=2,1,\n");
  CHECK(checkRow(exact, "j", CHECK_NO_ANSWER, 0, 0) == "0x12,0x4029,j,no answer,,,\n");
  CheckSilence s;
  CHECK(!s.answer(0x40, CHECK_NO_ANSWER) && !s.answer(0x40, CHECK_NO_ANSWER));
  CHECK(!s.answer(0x40, CHECK_REFUSED));                     // it answered: count again
  CHECK(!s.answer(0x40, CHECK_NO_ANSWER) && !s.answer(0x40, CHECK_NO_ANSWER));
  CHECK(!s.answer(0x63, CHECK_NO_ANSWER));                   // per module
  CHECK(s.answer(0x40, CHECK_NO_ANSWER) && s.skipped(0x40) && !s.skipped(0x63));
  CHECK(!s.answer(0x40, CHECK_NO_ANSWER));                   // told once
  for (int i = 0; i < 3; i++) s.answer(0x01, CHECK_NO_ANSWER);
  CHECK(s.list() == "0x01 0x40");
  s.reset();
  CHECK(!s.skipped(0x40) && s.list().empty());
}

static std::string hex(const std::string& s) {
  std::string h;
  char b[3];
  for (unsigned char c : s) { snprintf(b, sizeof(b), "%02x", c); h += b; }
  return h;
}

static void test_settings() {
  Settings s;
  CHECK(settingsEmpty(s));
  std::string key;
  CHECK(settingLine(s, "wifi_ssid=Home Net\r", key) && key == "wifi_ssid" && s.ssid1 == "Home Net");
  CHECK(settingLine(s, "wifi_pass=pa=ss \"word\\", key) && s.pass1 == "pa=ss \"word\\");
  CHECK(!settingLine(s, "wifi_pass=short", key));                      // WPA wants 8-63
  CHECK(settingLine(s, "wifi2_pass=", key) && s.pass2.empty());         // an open network
  CHECK(!settingLine(s, "host=", key));                                 // can't be cleared
  CHECK(!settingLine(s, "host=bad host", key));
  CHECK(settingLine(s, "host=m550.example.com", key));
  CHECK(!settingLine(s, "access_id=abc", key));
  CHECK(settingLine(s, "access_id=0123456789abcdef.access", key));
  CHECK(!settingLine(s, "access_secret=tooshort", key));
  CHECK(!settingLine(s, "colour=blue", key) && !settingLine(s, "no equals", key));
  CHECK(!settingsEmpty(s));
  CHECK(settingField("access_secret")->secret && !settingField("access_id")->secret);

  // A change from the logger page: only newer than the board's, values in hex.
  s.version = 3;
  SettingsChange c;
  CHECK(!settingsChange("{\"ok\":true}", s, c));
  CHECK(!settingsChange("{\"settings_version\":3}", s, c));
  std::string reply = "{\"ok\":true,\"settings_version\":4,\"set_wifi2_ssid\":\"" + hex("Phone") +
                      "\",\"set_wifi2_pass\":\"" + hex("p\"ss,word}") + "\"}";
  CHECK(settingsChange(reply, s, c) && c.bad.empty() && !c.token && c.version == 4);
  CHECK(c.next.ssid2 == "Phone" && c.next.pass2 == "p\"ss,word}" && c.next.ssid1 == "Home Net");
  CHECK(c.next.version == 4);
  reply = "{\"settings_version\":5,\"set_access_id\":\"" + hex("fedcba9876543210.access") +
          "\",\"set_access_secret\":\"" + hex("0123456789abcdef0123") + "\"}";
  CHECK(settingsChange(reply, s, c) && c.token && c.next.clientId == "fedcba9876543210.access");
  reply = "{\"settings_version\":6,\"set_wifi_pass\":\"" + hex("short") + "\"}";
  CHECK(settingsChange(reply, s, c) && c.bad == "wifi_pass");
  reply = "{\"settings_version\":6,\"set_wifi_pass\":\"zz\"}";
  CHECK(settingsChange(reply, s, c) && c.bad == "wifi_pass");
}

// Why the board isn't on Wi-Fi: not in the scan, or seen and refused.
static void test_wifi_note() {
  CHECK(wifiJoinWhy(0, "") == "no answer");
  CHECK(wifiJoinWhy(202, "AUTH_FAIL") == "wrong password?, AUTH_FAIL (202)");
  CHECK(wifiJoinWhy(15, "4WAY_HANDSHAKE_TIMEOUT") == "wrong password?, 4WAY_HANDSHAKE_TIMEOUT (15)");
  CHECK(wifiJoinWhy(2, "AUTH_EXPIRE") == "wrong password?, AUTH_EXPIRE (2)");   // seen on the bench
  CHECK(wifiJoinWhy(205, "CONNECTION_FAIL") == "CONNECTION_FAIL (205)");
  WifiTry home, phone, none;
  home.ssid = "Home";
  phone.ssid = "Phone";
  CHECK(wifiNote(9, {home, phone}) == "Home: not seen; Phone: not seen (9 networks in range)");
  phone.seen = phone.tried = true;
  phone.rssi = -71;
  phone.channel = 6;
  phone.why = wifiJoinWhy(202, "AUTH_FAIL");
  CHECK(wifiNote(1, {home, phone}) ==
        "Home: not seen; Phone: seen -71 dBm ch 6, join failed: wrong password?, AUTH_FAIL (202)"
        " (1 network in range)");
  home.tried = true;                          // the scan failed: both tried blind
  home.why = "no answer";
  CHECK(wifiNote(-1, {home, none}) == "Home: join failed: no answer (the scan failed)");
}

static const struct { const char* name; void (*fn)(); } tests[] = {
  {"wifi_note", test_wifi_note},
  {"settings", test_settings},
  {"car_check", test_car_check},
  {"ota", test_ota},
  {"ota_start", test_ota_start},
  {"backoff", test_backoff},
  {"upload_rules", test_upload_rules},
  {"kept_beats", test_kept_beats},
  {"car_clock", test_car_clock},
  {"burst", test_burst},
  {"scheduler_fast_only", test_scheduler_fast_only},
  {"batch_recovery", test_batch_recovery},
  {"alone_reads", test_alone_reads},
  {"periodic", test_periodic},
  {"sha256", test_sha256},
  {"channel_file", test_channel_file},
  {"decode", test_decode},
  {"plan_batches", test_plan_batches},
  {"plan_rests_failing_reads", test_plan_rests_failing_reads},
  {"scheduler", test_scheduler},
  {"scheduler_async", test_scheduler_async},
  {"split_reply", test_split_reply},
  {"clock_and_names", test_clock_and_names},
  {"value_cells", test_value_cells},
  {"csv_rows", test_csv_rows},
  {"fault_parse", test_fault_parse},
  {"fault_status", test_fault_status},
  {"fault_rows", test_fault_rows},
  {"fault_scan", test_fault_scan},
  {"fault_details", test_fault_details},
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
  printf(fails ? "%d FAILED\n" : "all %d logger tests passed\n", fails ? fails : ran);
  return fails ? 1 : 0;
}
