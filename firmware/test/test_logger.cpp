// Host test of lib/logger: SHA-256, the channel file, decoding, the read
// plan and the CSV layout.
//   cd firmware && g++ -std=c++17 -Ilib/logger test/test_logger.cpp -o test_logger && ./test_logger
// Run one case by name: ./test_logger decode
#include <cmath>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "channels.h"
#include "csvlog.h"
#include "plan.h"
#include "sha256.h"

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
  CHECK(t.channels.size() == 11);
  CHECK(t.skipped == 2);                     // a bad identifier; a value past the reply's end
  CHECK(t.count(true) == 3 && t.count(false) == 8);
  const Channel& oil = find(t, "ITOEL");
  CHECK(oil.module == 0x12 && oil.did == 0x4402 && oil.didSize == 2 && oil.type == U16);
  CHECK(oil.fast && !oil.little && oil.mul == 0.75 && oil.add == -48);
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
  std::vector<size_t> b = p.next(true, at, 0);
  CHECK(b.size() == 2 && p.reads[b[0]].module == 0x12 && p.reads[b[1]].did == 0x4807);
  CHECK(p.reads[b[0]].channels.size() == 1);
  b = p.next(true, at, 0);
  CHECK(b.size() == 1 && p.reads[b[0]].module == 0x13);
  CHECK(p.next(true, at, 0).empty());
  // Sweep: 0x40's three sized reads together, the unsized one alone; 0x63's two.
  at = p.sweepStart();
  b = p.next(false, at, 0);
  CHECK(b.size() == 3 && p.reads[b[0]].module == 0x40);
  b = p.next(false, at, 0);
  CHECK(b.size() == 1 && p.reads[b[0]].did == 0xDD03);
  b = p.next(false, at, 0);
  CHECK(b.size() == 2 && p.reads[b[0]].module == 0x63);
  CHECK(p.reads[b[1]].channels.size() == 3);        // lat, lon, height share 0xD039
  CHECK(p.next(false, at, 0).empty());
  // A module that can't take batches goes one at a time.
  p.noBatches(0x40);
  at = p.sweepStart();
  CHECK(p.next(false, at, 0).size() == 1);
}

static void test_plan_rests_failing_reads() {
  Table t = table();
  Plan p;
  p.build(t);
  size_t first = 0;                          // 0x12 0x4402
  for (int i = 0; i < FAILURES_TO_REST; i++) p.failed(first, 1000);
  size_t at = 0;
  std::vector<size_t> b = p.next(true, at, 2000);
  CHECK(b.size() == 1 && p.reads[b[0]].did == 0x4807);    // rested one skipped
  at = 0;
  CHECK(p.next(true, at, 1000 + REST_MS + 1).size() == 2); // tried again later
  p.answered(first);
  CHECK(p.reads[first].restUntil == 0);
}

static void test_split_reply() {
  Table t = table();
  Plan p;
  p.build(t);
  size_t at = p.sweepStart();
  std::vector<size_t> b = p.next(false, at, 0);            // DD00 (4), DD01 (2), DD02 (1)
  std::vector<const uint8_t*> data;
  std::vector<size_t> len;
  const uint8_t ok[] = {0xDD, 0x02, 0xFE, 0xDD, 0x00, 1, 2, 3, 4};   // DD01 left out
  CHECK(splitReply(ok, sizeof(ok), p.reads, b, data, len));
  CHECK(data[0] == ok + 5 && len[0] == 4 && data[1] == nullptr && data[2] == ok + 2);
  const uint8_t shortr[] = {0xDD, 0x00, 1, 2};
  CHECK(!splitReply(shortr, sizeof(shortr), p.reads, b, data, len));
  const uint8_t stranger[] = {0xDE, 0xAD, 1};
  CHECK(!splitReply(stranger, sizeof(stranger), p.reads, b, data, len));
  std::vector<size_t> one = p.next(false, at, 0);          // unsized DD03
  const uint8_t text[] = {0xDD, 0x03, 'A', 0, 7, 9};
  CHECK(splitReply(text, sizeof(text), p.reads, one, data, len) && len[0] == 4);
  double v;
  CHECK(decode(find(t, "BDC_TEXT_AFTER"), data[0], len[0], v) && v == 7);
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
  CHECK(fast.header(t) == "Time,Clock,ITOEL,INMOT,INMOT@0x13,Event\n");
  CHECK(sweep.columns.size() == 8);
  std::vector<double> v(t.channels.size(), NAN);
  v[0] = 96.0; v[2] = -0.5;
  CHECK(fast.row(t, 1.25, "2026-10-19 07:42:50.123-05:00", v, "start") ==
        "1.250,2026-10-19 07:42:50.123-05:00,96,,-0.5,start\n");
  v[3] = 32.776805555555555;
  std::string r = sweep.row(t, 2, "", v);
  CHECK(r.rfind("2.000,,32.7768056,", 0) == 0);
  CHECK(r.size() >= 2 && r.substr(r.size() - 2) == ",\n");
}

static const struct { const char* name; void (*fn)(); } tests[] = {
  {"sha256", test_sha256},
  {"channel_file", test_channel_file},
  {"decode", test_decode},
  {"plan_batches", test_plan_batches},
  {"plan_rests_failing_reads", test_plan_rests_failing_reads},
  {"split_reply", test_split_reply},
  {"clock_and_names", test_clock_and_names},
  {"value_cells", test_value_cells},
  {"csv_rows", test_csv_rows},
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
