// m550 logger: logs the car to the SD card and uploads each drive to the
// cloud Worker, which keeps it with the app's logs (History, the series, MCP).
//
// Board: Waveshare ESP32-S3-ETH on the car's ENET cable, powered with the
// ignition. Each boot:
//   1. Recover: a drive the last power cut left open is closed and queued.
//   2. Find the car (HSFZ discovery) and log while it answers: the fast and
//      medium channels every cycle to drives/<start>.csv, the slow ones in
//      rotation, one row a pass, to drives/<start>.sweep.csv; from a minute
//      in, each module's fault list in turn, and the DMEs' every three
//      minutes, to drives/<start>.faults.csv. Only modules with fast
//      channels are waited for each cycle; the others' answers are
//      collected when they come. Under full throttle or high boost, a burst:
//      the fast tier alone for up to 10 s. Without the time from the
//      internet, the car's own clock dates the drive.
//   3. Meanwhile, on the other core: whenever Wi-Fi is up, upload finished
//      files (the backlog at power-on, a drive's files as it ends, paced
//      while one is logging), compare the SHA-256 the Worker reports with
//      the board's own, and delete a file only when they match; check in
//      every 10 s from a task of its own (kept on
//      the card while there's no Wi-Fi, and sent when there is); fetch a
//      newer firmware the Worker offers to the card, installed at the next
//      power-on (or after a minute idle), kept only once it checks in or
//      reaches the car.
//
// What to read comes from channels.csv on the card (n63read logger-channels),
// else the built-in fast tier. Read-only: the HSFZ client refuses anything
// but UDS 0x22, 0x3E and 0x19 02 (the fault list; never a clear).
//
// Terminal: USB serial at 115200, and TCP 23 over Wi-Fi (logger_secrets.h):
//   s status   l files waiting to upload   u upload now   e end this drive
//   v view the newest files   C load a channels.csv sent over the terminal
//   X delete the files waiting to upload (asks first)
//   T queue a small test file (not a drive) to try uploads   h help

#include <Arduino.h>
#include <ESPmDNS.h>
#include <ETH.h>
#include <FS.h>
#include <HTTPClient.h>
#include <NetworkClient.h>
#include <NetworkClientSecure.h>
#include <NetworkServer.h>
#include <NetworkUdp.h>
#include <Preferences.h>
#include <SD.h>
#include <SPI.h>
#include <Update.h>
#include <WiFi.h>
#include <errno.h>
#include <esp_heap_caps.h>
#include <esp_ota_ops.h>
#include <esp_sntp.h>
#include <lwip/sockets.h>
#include <sys/time.h>
#include <time.h>

#include <algorithm>
#include <atomic>
#include <map>
#include <mutex>
#include <string>
#include <vector>

#include "carcheck.h"
#include "carclock.h"
#include "channels.h"
#include "csvlog.h"
#include "faults.h"
#include "default_channels.h"
#include "hsfz.h"
#include "ota.h"
#include "plan.h"
#include "sha256.h"
#include "upload.h"

#if __has_include("logger_secrets.h")
#include "logger_secrets.h"
#endif
#if !defined(WORKER_HOST) || defined(LOGGER_NO_UPLOAD)
// No Worker, or a bench build (PLATFORMIO_BUILD_FLAGS=-DLOGGER_NO_UPLOAD):
// log only, so bench drives never reach the cloud.
#undef WORKER_HOST
#define WORKER_HOST ""
#endif
#ifndef TZ_INFO
#define TZ_INFO "UTC0"
#endif
// This build's version: firmware/tools/publish_firmware.py sets it; an IDE
// build is "dev", which never takes an update (lib/logger/ota.h).
#ifndef FW_VERSION
#define FW_VERSION "dev"
#endif

using namespace logger;

// ---- board -------------------------------------------------------------------
static const int ETH_MISO = 12, ETH_MOSI = 11, ETH_SCLK = 13;
static const int ETH_CS = 14, ETH_INT = 10, ETH_RST = 9;
static const int SD_CS = 4, SD_MOSI = 6, SD_MISO = 5, SD_SCK = 7;   // Waveshare wiki

static const IPAddress MY_IP(169, 254, 100, 100);
static const IPAddress MASK(255, 255, 0, 0);
static const IPAddress NO_GW(0, 0, 0, 0);

static const char* HOSTNAME = "m550-logger";
static const uint16_t TERMINAL_PORT = 23;
static const char* CHANNELS_FILE = "/channels.csv";
static const char* DRIVES = "/drives";

static const uint32_t REQUEST_MS = 1500;     // a reply takes 3-60 ms; more is a dead read
static const uint32_t CAR_GONE_MS = 10000;   // nothing answered this long: the drive is over
static const uint32_t SEARCH_MS = 1000;      // between tries (the car is usually there at once)
static const uint32_t FLUSH_MS = 2000;       // a power cut loses at most this much
static const uint32_t MIN_CYCLE_MS = 50;     // at most 20 fast rows a second
static const uint32_t UPLOAD_EVERY_MS = 15000;

// ---- console: USB serial plus one Wi-Fi terminal (as the spike's) ------------
// The terminal never holds up logging: a line its socket can't take at once
// is dropped (USB and the replay keep everything), and a terminal whose far
// end has closed is let go. NetworkClient::write retries for up to ten
// seconds, so it isn't used.
struct Console : Print {
  NetworkClient remote;
  uint8_t line[256];
  size_t used = 0;
  uint8_t hist[8192];
  size_t head = 0;
  bool wrapped = false;
  std::recursive_mutex lock;
  size_t write(uint8_t b) override { return write(&b, 1); }
  size_t write(const uint8_t* d, size_t n) override {
    std::lock_guard<std::recursive_mutex> g(lock);
    Serial.write(d, n);
    for (size_t i = 0; i < n; i++) {
      hist[head++] = d[i];
      if (head == sizeof(hist)) { head = 0; wrapped = true; }
    }
    for (size_t i = 0; i < n && remote.connected(); i++) {
      if (used >= sizeof(line)) flush();
      line[used++] = d[i];
      if (d[i] == '\n') flush();
    }
    return n;
  }
  void flush() override {
    std::lock_guard<std::recursive_mutex> g(lock);
    if (used && remote.connected()) sendNow(line, used);
    used = 0;
  }
  // Send without waiting: false (and the terminal let go) on an error other
  // than a full send buffer, which just loses this piece.
  bool sendNow(const uint8_t* d, size_t n) {
    int fd = remote.fd();
    if (fd < 0) return false;
    ssize_t k = send(fd, d, n, MSG_DONTWAIT);
    if (k < 0 && errno != EAGAIN && errno != EWOULDBLOCK) { remote.stop(); return false; }
    return true;
  }
  // A larger piece (the replay), waiting up to `ms` for room.
  void sendAll(const uint8_t* d, size_t n, uint32_t ms) {
    int fd = remote.fd();
    uint32_t end = millis() + ms;
    while (n && fd >= 0 && (int32_t)(end - millis()) > 0) {
      ssize_t k = send(fd, d, n, MSG_DONTWAIT);
      if (k > 0) { d += k; n -= k; continue; }
      if (k < 0 && errno != EAGAIN && errno != EWOULDBLOCK) { remote.stop(); return; }
      delay(5);
    }
  }
  // Let the terminal go if its far end has closed.
  void checkRemote() {
    std::lock_guard<std::recursive_mutex> g(lock);
    int fd = remote.fd();
    if (fd < 0 || !remote.connected()) return;
    uint8_t b;
    ssize_t r = recv(fd, &b, 1, MSG_PEEK | MSG_DONTWAIT);
    if (r == 0 || (r < 0 && errno != EAGAIN && errno != EWOULDBLOCK)) remote.stop();
  }
  int read() {
    std::lock_guard<std::recursive_mutex> g(lock);
    if (Serial.available()) return Serial.read();
    if (remote.connected() && remote.available()) return remote.read();
    return -1;
  }
  // As many waiting bytes as fit, from USB or the terminal; 0 if none.
  size_t readSome(uint8_t* buf, size_t n) {
    std::lock_guard<std::recursive_mutex> g(lock);
    int a = Serial.available();
    if (a > 0) return Serial.readBytes(buf, (size_t)a < n ? (size_t)a : n);
    a = remote.available();
    if (a > 0) { int k = remote.read(buf, (size_t)a < n ? (size_t)a : n); return k > 0 ? k : 0; }
    return 0;
  }
  void attach(NetworkClient& c) {
    std::lock_guard<std::recursive_mutex> g(lock);
    if (remote.connected()) remote.stop();
    used = 0;
    remote = c;
    remote.setNoDelay(true);
    static const char start[] = "---- recent output ----\n", end[] = "---- live ----\n";
    sendAll((const uint8_t*)start, sizeof(start) - 1, 500);
    if (wrapped) sendAll(hist + head, sizeof(hist) - head, 1000);
    sendAll(hist, head, 1000);
    sendAll((const uint8_t*)end, sizeof(end) - 1, 500);
  }
};

static Console out;
static NetworkServer terminal(TERMINAL_PORT);
static SPIClass sdSpi(HSPI);
static std::recursive_mutex sdLock;          // directory changes: logging vs uploads
static bool sdOk = false;
static volatile bool linkUp = false;
static uint32_t bootCount = 0;

// ---- channels and plan -------------------------------------------------------
static Table table;
static Plan plan;
static bool channelsFromCard = false;
// Where the loaded table came from, for the check-ins: the cloud's ETag for
// the active preset, "manual" (sent over the terminal), "card" (a file from
// before downloads) or "built-in". The upload task reads it.
static std::mutex tagLock;
static std::string channelsTag = "built-in";
static std::string channelsPreset;           // the preset's name, for a downloaded file
static std::string otaStatus;                // the firmware updater's state, for the check-ins
static void otaKeep(const char* why);        // a new firmware proved itself (below)
// From the last stats note, for the check-ins (the logger channel page's rate
// estimates): the smaller batch sizes ("0x12:6 0x18:1"), each collected-later
// module's answer time ("0x63:260"), the slow pass and the reads per cycle.
// Under tagLock too.
static std::string liveBatches, liveAnswers;
static float liveSlowPassS = NAN, liveReadsMs = NAN;
static std::atomic<bool> channelsDownloaded{false};   // a new file is on the card

// The few values the check-ins report, kept by the logging core as they are
// read (the sweep's too) and read by the upload task. The first name the
// table has wins: the DME's speed, else the head unit's GPS speed. Doubles:
// a float's 24 bits put a coordinate a metre or two out.
enum Live { SPEED, RPM, BATTERY, COOLANT, OIL, LAT, LON, LIVE_N };
static const char* const LIVE_NAMES[LIVE_N][2] = {
    {"FZG_GESCHWINDIGKEIT_VA_HA_FASCHWERP", "HU_STATUS_SPEED.GPS_SPEED"},
    {"INMOT", nullptr},
    {"IUIBS", "IUADW"},                      // mV (the battery sensor), else V (the DME's own)
    {"ITKUM", "ITKUA"},                      // °C: the engine's, else the radiator outlet's
    {"ITOEL", nullptr},                      // °C
    {"HU_STATUS_GPS_POSITION.LATITUDE", nullptr},
    {"HU_STATUS_GPS_POSITION.LONGITUDE", nullptr},
};
static int liveCh[LIVE_N];
static double liveScale[LIVE_N];          // to the check-in's unit (IUIBS: mV to V)
static std::atomic<double> liveValue[LIVE_N];
static std::atomic<uint32_t> liveAt[LIVE_N];
static const uint32_t LIVE_FRESH_MS = 30000;  // older: not reported
// What starts a burst (lib/logger/plan.h): the pedal and the boost.
static int pedalCh = -1, boostCh = -1;

static int channelNamed(const char* want) {
  for (size_t i = 0; i < table.channels.size(); i++) {
    if (!strcmp(table.name(table.channels[i]), want)) return (int)i;
  }
  return -1;
}

static void findLive() {
  for (int k = 0; k < LIVE_N; k++) {
    liveCh[k] = -1;
    liveAt[k] = 0;
    liveScale[k] = 1;
    for (const char* want : LIVE_NAMES[k]) {
      if (!want || liveCh[k] >= 0) continue;
      liveCh[k] = channelNamed(want);
      if (liveCh[k] >= 0 && !strcmp(want, "IUIBS")) liveScale[k] = 0.001;
    }
  }
  pedalCh = channelNamed("BUS_FAHRPEDALWERT");
  boostCh = channelNamed("IPLAD");
}

static void noteLive(uint32_t ch, double v, uint32_t now) {
  for (int k = 0; k < LIVE_N; k++) {
    if (liveCh[k] == (int)ch) { liveValue[k] = v * liveScale[k]; liveAt[k] = now ? now : 1; }
  }
}

static double live(Live k) {
  uint32_t at = liveAt[k];
  return at && millis() - at < LIVE_FRESH_MS ? liveValue[k].load() : NAN;
}

// The table and plan make thousands of small allocations (a vector per read,
// strings while parsing). Left in internal RAM they break it into pieces too
// small for a TLS handshake (seen: 123 KB free, largest block 17 KB, uploads
// failing "SSL - Memory allocation failed"), so while loading every
// allocation goes to PSRAM.
static void loadChannelsInto();

static size_t periodicCount() {
  return table.count(DRIVE) + table.count(DAILY) + table.count(WEEKLY) + table.count(MONTHLY);
}

static void loadChannels() {
  heap_caps_malloc_extmem_enable(0);
  loadChannelsInto();
  heap_caps_malloc_extmem_enable(4096);       // back to the default
}

static void loadChannelsInto() {
  table = Table();
  File f;
  if (sdOk) f = SD.open(CHANNELS_FILE);
  if (f) {
    std::string line;
    bool ok = true;
    static uint8_t buf[4096];                // a byte at a time takes seconds for 9,000 rows
    for (int n; ok && (n = f.read(buf, sizeof(buf))) > 0;) {
      for (int i = 0; i < n && ok; i++) {
        if (buf[i] == '\n') { ok = table.line(line.c_str()); line.clear(); }
        else line += (char)buf[i];
      }
    }
    if (ok && !line.empty()) ok = table.line(line.c_str());
    f.close();
    if (!ok || table.channels.empty()) {
      out.printf("[chan] %s unusable (%s); using the built-in fast tier\n", CHANNELS_FILE,
                 table.error.empty() ? "no rows" : table.error.c_str());
      table = Table();
    } else {
      channelsFromCard = true;
    }
  }
  if (!channelsFromCard) {
    const char* p = DEFAULT_CHANNELS;
    while (*p) {
      const char* nl = strchr(p, '\n');
      table.line(std::string(p, nl - p).c_str());
      p = nl + 1;
    }
  }
  plan.build(table);
  findLive();
  std::string tag = "built-in", preset;
  if (channelsFromCard) {
    Preferences prefs;
    prefs.begin("logger", true);
    tag = prefs.isKey("chsrc") ? prefs.getString("chsrc").c_str() : "card";
    if (prefs.isKey("chname")) preset = prefs.getString("chname").c_str();
    prefs.end();
  }
  {
    std::lock_guard<std::mutex> g(tagLock);
    channelsTag = tag;
    channelsPreset = preset;
  }
  out.printf("[chan] %u channels (%u fast, %u medium, %u slow, %u periodic) in %u reads, from %s "
             "(%s)%s\n",
             (unsigned)table.channels.size(), (unsigned)table.count(FAST),
             (unsigned)table.count(MEDIUM), (unsigned)table.count(SLOW), (unsigned)periodicCount(),
             (unsigned)plan.reads.size(), channelsFromCard ? "the card" : "the built-in table",
             tag.c_str(), table.skipped ? " (some rows skipped)" : "");
}

// ---- time --------------------------------------------------------------------
static bool timeKnown() { return time(nullptr) > 1700000000; }

static int64_t nowEpochMs() {
  struct timeval tv;
  gettimeofday(&tv, nullptr);
  return (int64_t)tv.tv_sec * 1000 + tv.tv_usec / 1000;
}

static int offsetMinutes(time_t t) {
  struct tm lt;
  localtime_r(&t, &lt);
  return utcOffsetMinutes(t, lt.tm_year + 1900, lt.tm_mon + 1, lt.tm_mday, lt.tm_hour,
                          lt.tm_min, lt.tm_sec);
}

static std::string clockNow() {
  if (!timeKnown()) return "";
  int64_t ms = nowEpochMs();
  return clockText(ms, offsetMinutes((time_t)(ms / 1000)));
}

// ---- the car -----------------------------------------------------------------
struct TcpTransport : hsfz::Transport {
  NetworkClient c;
  bool write(const uint8_t* d, size_t n) override { return c.connected() && c.write(d, n) == n; }
  int read(uint8_t* buf, size_t n, uint32_t timeout_ms) override {
    uint32_t end = millis() + timeout_ms;
    while ((int32_t)(end - millis()) > 0) {
      int a = c.available();
      if (a > 0) return c.read(buf, (size_t)a < n ? (size_t)a : n);
      if (!c.connected()) return -1;
      delay(1);
    }
    return 0;
  }
  uint32_t now_ms() override { return millis(); }
  void sleep_ms(uint32_t ms) override { delay(ms); }
  size_t available() override { int a = c.available(); return a > 0 ? (size_t)a : 0; }
};

static TcpTransport tMain, tBank2;
static hsfz::Client* mainClient = nullptr;
static hsfz::Client* bank2Client = nullptr;
static IPAddress carIp;
static char vin[18] = "";
static uint8_t rx[4096];

static bool discover() {
  NetworkUDP udp;
  if (!udp.begin(MY_IP, 0)) return false;
  const IPAddress dests[] = {IPAddress(169, 254, 255, 255), IPAddress(255, 255, 255, 255)};
  for (const IPAddress& d : dests) {
    udp.beginPacket(d, hsfz::CONTROL_PORT);
    udp.write(hsfz::IDENT_PROBE_0, sizeof(hsfz::IDENT_PROBE_0));
    udp.endPacket();
    udp.beginPacket(d, hsfz::CONTROL_PORT);
    udp.write(hsfz::IDENT_PROBE_2, sizeof(hsfz::IDENT_PROBE_2));
    udp.endPacket();
  }
  bool found = false;
  uint32_t end = millis() + 1500;
  while (!found && (int32_t)(end - millis()) > 0) {
    int n = udp.parsePacket();
    if (n <= 0) { delay(5); continue; }
    int k = udp.read(rx, sizeof(rx));
    if (k <= 0 || !hsfz::isIdentReply(rx, k)) continue;
    carIp = udp.remoteIP();
    if (!hsfz::vinFromIdent(rx, k, vin)) vin[0] = 0;
    found = true;
  }
  udp.stop();
  return found;
}

// The car's address and VIN, kept between boots: save (true) after a drive
// starts, load (false) before looking. False when nothing is kept.
static bool rememberCar(bool save) {
  Preferences prefs;
  prefs.begin("logger", !save);
  bool ok = true;
  if (save) {
    prefs.putUInt("carip", (uint32_t)carIp);
    prefs.putString("vin", vin);
  } else {
    uint32_t ip = prefs.getUInt("carip", 0);
    ok = ip != 0;
    if (ok) {
      carIp = IPAddress(ip);
      String v = prefs.getString("vin", "");
      strlcpy(vin, v.c_str(), sizeof(vin));
    }
  }
  prefs.end();
  return ok;
}

static bool connectOne(TcpTransport& t, hsfz::Client*& cl, uint8_t source, uint32_t timeoutMs) {
  t.c.stop();
  if (!t.c.connect(carIp, hsfz::DIAG_PORT, timeoutMs)) return false;
  t.c.setNoDelay(true);
  if (!cl) cl = new hsfz::Client(t, source);
  else cl->clearStash();                      // nothing from the last connection
  return true;
}

static void disconnectCar() {
  tMain.c.stop();
  tBank2.c.stop();
}

static bool needBank2() {
  for (const Read& r : plan.reads) if (r.module == hsfz::DME_SLAVE) return true;
  return false;
}

// The time from the car's clock (lib/logger/carclock.h), when the internet
// hasn't given it: a drive that starts away from Wi-Fi is then still named
// and dated by the time. The car's clock is local time and to the second;
// the internet's replaces it whenever Wi-Fi comes up.
static volatile bool clockFromCar = false;   // until the internet's (setup)

static void setClockFromCar() {
  const uint8_t req[] = {0x22, CLOCK_DID >> 8, CLOCK_DID & 0xFF};
  hsfz::Reply r = mainClient->request(CLOCK_MODULE, req, sizeof(req), rx, sizeof(rx), REQUEST_MS);
  CarTime t;
  if (r.result != hsfz::OK || r.len < 3 || rx[0] != 0x62 || rx[1] != req[1] || rx[2] != req[2] ||
      !carClock(rx + 3, r.len - 3, t)) {
    out.printf("[time] the car's clock didn't answer (%d); the drive starts without the time\n",
               (int)r.result);
    return;
  }
  struct tm lt = {};
  lt.tm_year = t.year - 1900;
  lt.tm_mon = t.month - 1;
  lt.tm_mday = t.day;
  lt.tm_hour = t.hour;
  lt.tm_min = t.minute;
  lt.tm_sec = t.second;
  lt.tm_isdst = -1;                          // the time zone (TZ_INFO) says
  struct timeval tv = {mktime(&lt), 0};
  if (timeKnown()) return;                   // the internet's came in meanwhile
  settimeofday(&tv, nullptr);
  clockFromCar = true;
  out.printf("[time] from the car's clock: %s\n", clockNow().c_str());
}

// ---- a drive's files ---------------------------------------------------------
struct DriveFile {
  File f;
  String path;
  Sha256 sha;
  uint32_t size = 0;
  std::string pending;
  bool open = false;

  bool begin(const String& p, const std::string& header) {
    path = p;
    sha.reset();
    size = 0;
    pending = header;
    {
      std::lock_guard<std::recursive_mutex> g(sdLock);
      f = SD.open(path, FILE_WRITE);
    }
    open = (bool)f;
    return open && flush();
  }
  void add(const std::string& text) { if (open) pending += text; }
  bool flush() {
    if (!open || pending.empty()) return open;
    size_t n = f.write((const uint8_t*)pending.data(), pending.size());
    sha.update(pending.data(), n);
    size += n;
    bool ok = n == pending.size();
    pending.clear();
    f.flush();
    return ok;
  }
  // Close and mark finished: <file>.done holds "<sha256> <bytes>", which is
  // what the uploader checks the Worker's reply against.
  void finish() {
    if (!open) return;
    flush();
    f.close();
    open = false;
    char hex[65];
    sha.finishHex(hex);
    std::lock_guard<std::recursive_mutex> g(sdLock);
    File d = SD.open(path + ".done", FILE_WRITE);
    if (d) { d.printf("%s %u\n", hex, (unsigned)size); d.close(); }
  }
};

enum State { WAIT_LINK, SEARCH, LOGGING };
static State state = WAIT_LINK;

static DriveFile fastFile, sweepFile, faultFile;
static FaultScan faultScan;
static String driveName;
static uint32_t driveStartMs = 0, lastOkMs = 0, lastFlushMs = 0, lastCycleMs = 0;
static uint32_t fastRows = 0, sweepRows = 0, cycles = 0, cycleStatsMs = 0;
static uint32_t reconnects = 0;             // this drive (reconnect())
static float cycleRate = 0;
static uint32_t tFast = 0, tWrite = 0;                 // ms spent per stats window
static float msFast = 0, msWrite = 0;                  // per cycle, last window
static uint32_t sweepPassMs = 0, sweepPassStart = 0;
static uint32_t dueTiers = 0;               // this drive's periodic tiers that are due, a bit each
static uint32_t onceRowAt = 0;              // ms: the periodic reads' sweep row then (0: none)
static Scheduler scheduler;                 // this drive's rotation through the tiers
static Burst burst;                         // full throttle or high boost: the fast tier alone
static std::vector<double> values;
static LogFile fastLayout, sweepLayout;
// Uploads (core 0): now, rather than at the next round (a drive just ended,
// Wi-Fi just came up, or `u`); and how many files wait, which the firmware
// updater doesn't restart under.
static volatile bool uploadNow = false;
static std::atomic<uint32_t> uploadsWaiting{0};

// Notes for the next fast row's Event column: the board's own story (boot,
// rates, plan changes, the end) kept in the log, since its terminal output
// is lost with the power. No commas: the column is CSV.
static std::string note;
static const uint32_t STATS_MS = 60000;
static uint32_t lastStatsNoteMs = 0;

static void addNote(const std::string& text) {
  if (!note.empty()) note += "; ";
  note += text;
}

static const char* resetReason() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON: return "power on";
    case ESP_RST_BROWNOUT: return "brownout";
    case ESP_RST_SW: return "software reset";
    case ESP_RST_PANIC: return "crash";
    case ESP_RST_INT_WDT: case ESP_RST_TASK_WDT: case ESP_RST_WDT: return "watchdog";
    case ESP_RST_EXT: return "reset pin";
    case ESP_RST_USB: return "usb reset";
    case ESP_RST_DEEPSLEEP: return "wake";
    default: return "other reset";
  }
}

// Files left without a .done (the power went mid-drive), listed at boot
// before a new drive starts, and hashed and queued by the upload task, so
// logging doesn't wait for it.
static std::vector<String> leftovers;

static void listLeftovers() {
  std::lock_guard<std::recursive_mutex> g(sdLock);
  if (!SD.exists(DRIVES)) SD.mkdir(DRIVES);
  File dir = SD.open(DRIVES);
  for (File f = dir.openNextFile(); f; f = dir.openNextFile()) {
    String name = f.name();
    f.close();
    if (name.endsWith(".csv") && !SD.exists(String(DRIVES) + "/" + name + ".done")) {
      leftovers.push_back(name);
    }
  }
  dir.close();
}

// The lock is held only to open and to write the .done: reading under it
// would keep the new drive from opening its files for the second or two a
// megabyte takes (FatFS itself is reentrant).
static void recoverDrives() {
  static uint8_t buf[4096];
  std::vector<String> open;
  open.swap(leftovers);
  for (const String& name : open) {
    String path = String(DRIVES) + "/" + name;
    File f;
    {
      std::lock_guard<std::recursive_mutex> g(sdLock);
      f = SD.open(path);
    }
    Sha256 sha;
    uint32_t size = 0;
    for (int n; (n = f.read(buf, sizeof(buf))) > 0;) { sha.update(buf, n); size += n; }
    f.close();
    char hex[65];
    sha.finishHex(hex);
    std::lock_guard<std::recursive_mutex> g(sdLock);
    File d = SD.open(path + ".done", FILE_WRITE);
    if (d) { d.printf("%s %u\n", hex, (unsigned)size); d.close(); }
    out.printf("[sd] %s was left open (power cut); closed at %u bytes\n", name.c_str(),
               (unsigned)size);
  }
}

static void resetRequests();
static uint32_t duePeriodic();
static void sweepRow(double secs);
static void checkStart();
static void checkEnd();
static bool checkRunning();

static void startDrive() {
  // With Wi-Fi up already, give the clock a moment; otherwise it fills in
  // the Clock column once Wi-Fi joins.
  for (int i = 0; i < 20 && WiFi.isConnected() && !timeKnown(); i++) delay(100);
  if (!timeKnown()) setClockFromCar();
  static uint32_t seq = 0;
  char base[40];
  if (timeKnown()) {
    int64_t ms = nowEpochMs();
    snprintf(base, sizeof(base), "%s", fileStamp(ms, offsetMinutes((time_t)(ms / 1000))).c_str());
  } else {
    snprintf(base, sizeof(base), "nt-%05u-%02u", (unsigned)bootCount, (unsigned)++seq);
  }
  driveName = base;
  values.assign(table.channels.size(), NAN);
  fastLayout.setup(table, true);
  sweepLayout.setup(table, false);
  String dir = String(DRIVES) + "/";
  bool ok = fastFile.begin(dir + driveName + ".csv", fastLayout.header(table));
  if (ok && !sweepLayout.columns.empty()) {
    ok = sweepFile.begin(dir + driveName + ".sweep.csv", sweepLayout.header(table));
  }
  driveStartMs = lastOkMs = lastFlushMs = lastCycleMs = cycleStatsMs = lastStatsNoteMs = millis();
  fastRows = sweepRows = cycles = reconnects = 0;
  burst.reset();
  note.clear();
  // Which channel file, so the analysis knows what was meant to be logged:
  // the preset and its ETag, "manual" (sent over the terminal), "card" or
  // the built-in table.
  std::string from = "the built-in table";
  if (channelsFromCard) {
    std::lock_guard<std::mutex> g(tagLock);
    from = channelsPreset.empty() ? channelsTag : "preset " + channelsPreset + " " + channelsTag;
  }
  std::replace(from.begin(), from.end(), ',', ' ');       // the note is a CSV cell
  std::replace(from.begin(), from.end(), ';', ' ');
  char start[256];
  snprintf(start, sizeof(start),
           "start VIN %s; boot %u after %s; up %.1f s; firmware %s; %u channels from %s%s", vin,
           (unsigned)bootCount, resetReason(), millis() / 1000.0f, FW_VERSION,
           (unsigned)table.channels.size(), from.c_str(),
           !timeKnown() ? "; clock unknown" : clockFromCar ? "; clock from the car" : "");
  addNote(start);
  otaKeep("reached the car");
  checkStart();
  dueTiers = duePeriodic();
  // A full channel check reads every periodic tier too, due or not.
  if (checkRunning()) dueTiers |= (1u << DRIVE) | (1u << DAILY) | (1u << WEEKLY) | (1u << MONTHLY);
  onceRowAt = 0;
  scheduler.build(plan, dueTiers);
  if (scheduler.onceCount()) {
    std::string which;
    for (int t = DRIVE; t <= MONTHLY; t++) {
      if (!(dueTiers & (1u << t)) || !table.count((Tier)t)) continue;
      which += std::string(" ") + tierName((Tier)t);
    }
    char text[96];
    snprintf(text, sizeof(text), "periodic reads due:%s (%u identifiers)", which.c_str(),
             (unsigned)scheduler.onceCount());
    addNote(text);
  }
  resetRequests();
  faultScan.start(millis());
  sweepPassStart = millis();
  out.printf("[drive] %s started%s%s\n", driveName.c_str(), timeKnown() ? "" : " (clock unknown)",
             ok ? "" : ": FILES COULD NOT BE OPENED");
}

static void endDrive(const char* why) {
  checkEnd();
  if (fastFile.open && !fastLayout.columns.empty()) {
    addNote(std::string("end: ") + why);
    fastFile.add(fastLayout.row(table, (millis() - driveStartMs) / 1000.0, clockNow(), values,
                                note.c_str()));
    note.clear();
  }
  // Whatever the sweep columns hold since the last row (a pass cut short,
  // periodic reads): a last row, so it isn't lost.
  bool held = false;
  for (uint32_t i : sweepLayout.columns) held = held || !isnan(values[i]);
  if (sweepFile.open && held) sweepRow((millis() - driveStartMs) / 1000.0);
  fastFile.finish();
  sweepFile.finish();
  faultFile.finish();
  resetRequests();
  disconnectCar();
  out.printf("[drive] %s ended (%s): %u rows, %u sweep rows, %.1f min\n", driveName.c_str(), why,
             (unsigned)fastRows, (unsigned)sweepRows, (millis() - driveStartMs) / 60000.0f);
  driveName = "";
  uploadNow = true;                          // while there's still power to send it

}

// ---- requests ----------------------------------------------------------------
static bool connectionLost = false;
// Requests in flight together: at most one per module (a module answers one
// at a time), on both connections, several to different modules on one
// connection. A module that keeps timing out is left alone for a while
// (lib/logger/plan.h, Backoff); the rest carry on.
static Backoff backoff;

// The slave DME is on its own connection (tester 0xF5); everything else on
// the main one. Requests on the two can be in flight together.
static hsfz::Client* clientFor(uint8_t module) {
  return module == hsfz::DME_SLAVE ? bank2Client : mainClient;
}

static uint8_t moduleOf(const std::vector<size_t>& batch) { return plan.reads[batch[0]].module; }

static size_t buildRequest(const std::vector<size_t>& batch, uint8_t* req) {
  req[0] = 0x22;
  for (size_t k = 0; k < batch.size(); k++) {
    req[1 + 2 * k] = plan.reads[batch[k]].did >> 8;
    req[2 + 2 * k] = plan.reads[batch[k]].did & 0xFF;
  }
  return 1 + 2 * batch.size();
}

// A note when a module's batches get smaller, and at most one a minute per
// module when it refuses reads (the stats line counts the rest).
static uint32_t refusedNoteMs[256];
static uint32_t refusedReads = 0;                 // this drive

static void smaller(uint8_t module, size_t size, const char* why) {
  size_t was = plan.batchFor(module);
  plan.smallerBatches(module, size);
  if (plan.batchFor(module) == was) return;
  char text[96];
  snprintf(text, sizeof(text), "module 0x%02X %u identifiers per request (%s)", module,
           (unsigned)plan.batchFor(module), why);
  out.printf("[plan] %s\n", text);
  addNote(text);
}

static void retryHalves(const std::vector<size_t>& batch) {
  size_t half = batch.size() / 2;
  scheduler.retry(plan, std::vector<size_t>(batch.begin(), batch.begin() + half));
  scheduler.retry(plan, std::vector<size_t>(batch.begin() + half, batch.end()));
}

// What came back for a batch (its answer is in rx). The module's error code
// says what to do: too long or badly formed (0x13, 0x14), or an answer out
// of line, means the batch is too big for it; out of range (0x31) means one
// identifier in it is one the module hasn't, found by asking the halves
// again, and rested for the rest of the drive (noted, so the Worker turns it
// off in the channel file for good); anything else refuses the reads, which
// rest. No answer at all counts against the reads, and against the batch
// size when it keeps happening.
static void process(const std::vector<size_t>& batch, const hsfz::Reply& r) {
  const uint8_t module = moduleOf(batch);
  const uint32_t now = millis();
  if (r.result == hsfz::CLOSED) { connectionLost = true; return; }
  if (r.result == hsfz::OK || r.result == hsfz::NEGATIVE) {
    lastOkMs = now;                                       // it answered, whatever it said
    plan.moduleAnswered(module);
    backoff.answered(module);
  }
  std::vector<const uint8_t*> data;
  std::vector<size_t> len;
  if (r.result == hsfz::OK && splitReply(rx + 1, r.len - 1, plan.reads, batch, data, len)) {
    if (plan.goodBatch(module, batch.size())) {
      char text[96];
      snprintf(text, sizeof(text), "module 0x%02X %u identifiers per request again", module,
               (unsigned)plan.batchFor(module));
      out.printf("[plan] %s\n", text);
      addNote(text);
    }
    for (size_t k = 0; k < batch.size(); k++) {
      if (!data[k]) { plan.failed(batch[k], now); continue; }
      plan.answered(batch[k]);
      for (uint32_t ci : plan.reads[batch[k]].channels) {
        double v;
        if (!decode(table.channels[ci], data[k], len[k], v)) continue;
        values[ci] = v;
        noteLive(ci, v, now);
      }
    }
    return;
  }
  const bool tooBig = r.result == hsfz::OK ||
                      (r.result == hsfz::NEGATIVE && (r.nrc == 0x13 || r.nrc == 0x14));
  if (tooBig && batch.size() > 1) {
    char why[40];
    if (r.result == hsfz::OK) snprintf(why, sizeof(why), "reply out of line");
    else snprintf(why, sizeof(why), "negative response 0x%02X", r.nrc);
    smaller(module, batch.size(), why);
    retryHalves(batch);
    return;
  }
  if (r.result == hsfz::OK) { plan.failed(batch[0], now); return; }   // one, out of line
  if (r.result == hsfz::NEGATIVE && (tooBig || r.nrc == 0x31)) {
    if (batch.size() > 1) { retryHalves(batch); return; }            // which one?
    const bool absent = r.nrc == 0x31;
    plan.rest(batch[0], now, absent ? ABSENT_REST_MS : OUT_OF_RANGE_REST_MS);
    refusedReads++;
    char text[64];
    snprintf(text, sizeof(text), "module 0x%02X hasn't 0x%04X (0x%02X)", module,
             plan.reads[batch[0]].did, r.nrc);
    out.printf("[plan] %s: rested\n", text);
    if (absent) addNote(text);                            // the Worker's cue (sweep.js)
    return;
  }
  if (r.result == hsfz::NEGATIVE) {
    for (size_t i : batch) plan.rest(i, now, REST_MS);
    refusedReads += batch.size();
    if (now - refusedNoteMs[module] >= STATS_MS || !refusedNoteMs[module]) {
      refusedNoteMs[module] = now | 1;
      char text[96];
      snprintf(text, sizeof(text), "module 0x%02X refused %u identifiers (0x%02X): rested", module,
               (unsigned)batch.size(), r.nrc);
      out.printf("[plan] %s\n", text);
      addNote(text);
    }
    return;
  }
  for (size_t i : batch) plan.failed(i, now);           // no answer
  if (r.result == hsfz::TIMEOUT && backoff.timedOut(module, now)) {
    char text[80];
    snprintf(text, sizeof(text), "module 0x%02X not answering: left alone %.0f s", module,
             backoff.waitMs(module) / 1000.0f);
    out.printf("[plan] %s\n", text);
    addNote(text);
  }
  if (r.result == hsfz::TIMEOUT && plan.timedOut(module) >= TIMEOUTS_TO_SHRINK &&
      batch.size() > 1) {
    smaller(module, batch.size(), "timeouts");
    plan.moduleAnswered(module);                          // count again at the new size
  }
}

// ---- requests not waited for -------------------------------------------------
// A module with no fast reads (the head unit, the body modules, ...) can take
// most of a second to answer, and the fast row shouldn't wait for it: its
// request is sent with the cycle's and its answer collected in whichever
// cycle it comes (the hsfz client keeps it meanwhile). A fault read to such
// a module goes the same way.
struct Pending {
  hsfz::Client* cl;
  uint8_t target;
  uint8_t req[1 + 2 * MAX_BATCH];
  size_t n;
  std::vector<size_t> batch;                      // empty: a fault read or a check read
  uint32_t sentMs;
  bool check = false;                             // a full channel check's read
};
static std::vector<Pending> pendingReqs;
static float answerMs[256];                       // per module, collected-later answers, averaged
static const uint32_t LATER_MS = 3000;            // given up on after this
static const size_t MAX_LATER = 6;                // in flight at once (hsfz keeps 8 answers)
static const size_t SLOW_ONLY_PER_CYCLE = 4;
static uint32_t laterSent = 0, laterTimeouts = 0; // this stats window
static int faultWanted = -1;                      // held from reads until its fault list goes

static bool waitingOn(uint8_t module) {
  for (const Pending& p : pendingReqs) if (p.target == module) return true;
  return false;
}

static void faultAnswer(uint8_t m, const hsfz::Reply& r);
static void checkAnswer(const hsfz::Reply& r);

static bool sendLater(uint8_t module, const uint8_t* req, size_t n, const std::vector<size_t>& batch,
                      bool check = false) {
  hsfz::Client* cl = clientFor(module);
  if (!cl) return false;
  Pending p;
  p.cl = cl;
  p.target = module;
  memcpy(p.req, req, n);
  p.n = n;
  p.batch = batch;
  p.sentMs = millis();
  p.check = check;
  hsfz::Reply r;
  if (!cl->start(module, p.req, n, r)) {
    if (r.result == hsfz::CLOSED) connectionLost = true;
    return false;
  }
  pendingReqs.push_back(p);
  laterSent++;
  return true;
}

static void sendLater(const std::vector<size_t>& batch) {
  uint8_t req[1 + 2 * MAX_BATCH];
  size_t n = buildRequest(batch, req);
  if (!sendLater(moduleOf(batch), req, n, batch) && !connectionLost) {
    for (size_t i : batch) plan.failed(i, millis());
  }
}

// Whatever has come in, without waiting; requests past LATER_MS given up.
static void collect() {
  for (size_t k = 0; k < pendingReqs.size() && !connectionLost;) {
    Pending& p = pendingReqs[k];
    hsfz::Reply r;
    bool done = p.cl->poll(p.target, p.req, p.n, rx, sizeof(rx), r);
    if (!done && millis() - p.sentMs >= LATER_MS) {
      p.cl->forget(p.target);
      r = hsfz::Reply();
      r.result = hsfz::TIMEOUT;
      laterTimeouts++;
      done = true;
    }
    if (!done) { k++; continue; }
    if (!p.batch.empty() && (r.result == hsfz::OK || r.result == hsfz::NEGATIVE)) {
      float ms = millis() - p.sentMs;                   // as the cycles see it
      float& a = answerMs[p.target];
      a = a > 0 ? 0.8f * a + 0.2f * ms : ms;
    }
    Pending got = p;
    pendingReqs.erase(pendingReqs.begin() + k);
    if (got.check) checkAnswer(r);
    else if (got.batch.empty()) faultAnswer(got.target, r);
    else process(got.batch, r);
  }
}

// A drive starts or ends: nothing in flight, nothing assumed about the
// gateway.
static void resetRequests() {
  pendingReqs.clear();
  backoff.reset();
  refusedReads = laterSent = laterTimeouts = 0;
  memset(refusedNoteMs, 0, sizeof(refusedNoteMs));
  faultWanted = -1;
}

// ---- requests waited for -----------------------------------------------------
static void askWave(const std::vector<const std::vector<size_t>*>& wave) {
  struct Sent { hsfz::Client* cl; uint8_t req[1 + 2 * MAX_BATCH]; size_t n; uint8_t target;
                hsfz::Reply r; bool ok; };
  std::vector<Sent> sent(wave.size());
  for (size_t i = 0; i < wave.size(); i++) {
    Sent& s = sent[i];
    s.target = moduleOf(*wave[i]);
    s.cl = clientFor(s.target);
    s.n = buildRequest(*wave[i], s.req);
    s.ok = s.cl && s.cl->start(s.target, s.req, s.n, s.r);
    if (!s.cl) s.r.result = hsfz::TIMEOUT;
  }
  for (size_t i = 0; i < wave.size() && !connectionLost; i++) {
    Sent& s = sent[i];
    hsfz::Reply r = s.ok ? s.cl->finish(s.target, s.req, s.n, rx, sizeof(rx), REQUEST_MS) : s.r;
    process(*wave[i], r);
  }
}

// Send `batches` in waves: each wave takes the next batch of every module
// not yet in it.
static void askAll(std::vector<const std::vector<size_t>*> batches) {
  while (!batches.empty() && !connectionLost) {
    std::vector<const std::vector<size_t>*> wave, rest;
    for (const std::vector<size_t>* b : batches) {
      bool clash = false;
      for (const std::vector<size_t>* w : wave) {
        clash = clash || moduleOf(*w) == moduleOf(*b);
      }
      (clash ? rest : wave).push_back(b);
    }
    askWave(wave);
    batches.swap(rest);
  }
}

// ---- periodic reads ----------------------------------------------------------
// Once a drive, daily, weekly, monthly: which are due comes from when each
// was last read (kept between boots); they're asked from a minute in, as the
// fault reads, and get a sweep row of their own once all are in.
static const uint32_t ONCE_AFTER_MS = 60000;
static const char* const PERIODIC_KEYS[] = {"pd_daily", "pd_weekly", "pd_monthly"};

static uint32_t duePeriodic() {
  uint32_t due = 1u << DRIVE;
  uint32_t now = timeKnown() ? (uint32_t)time(nullptr) : 0;
  int off = now ? offsetMinutes((time_t)now) : 0;
  Preferences prefs;
  prefs.begin("logger", true);
  for (int t = DAILY; t <= MONTHLY; t++) {
    if (periodicDue((Tier)t, prefs.getUInt(PERIODIC_KEYS[t - DAILY], 0), now, off)) due |= 1u << t;
  }
  prefs.end();
  return due;
}

// Every periodic read asked: remember when, for the tiers that were due
// (only with the clock known; otherwise they're read again next drive).
static void periodicDone() {
  std::string which;
  bool known = timeKnown();
  Preferences prefs;
  prefs.begin("logger");
  for (int t = DRIVE; t <= MONTHLY; t++) {
    if (!(dueTiers & (1u << t)) || !table.count((Tier)t)) continue;
    which += which.empty() ? "" : " ";
    which += tierName((Tier)t);
    if (t >= DAILY && known) prefs.putUInt(PERIODIC_KEYS[t - DAILY], (uint32_t)time(nullptr));
  }
  prefs.end();
  addNote("periodic reads done: " + which);
  out.printf("[drive] periodic reads done (%s)\n", which.c_str());
}

static void sweepRow(double secs) {
  if (sweepLayout.columns.empty()) return;
  sweepFile.add(sweepLayout.row(table, secs, clockNow(), values));
  sweepRows++;
  for (uint32_t i : sweepLayout.columns) values[i] = NAN;
}

// ---- the logging cycle -------------------------------------------------------
// Each cycle: one request per module (the scheduler packs each module's fast
// reads with the next medium and slow ones). The ones to modules with fast
// reads are waited for, all in flight together; the rest are sent alongside
// and collected when they come.
static void cycle() {
  const uint32_t start = millis();
  // A burst (lib/logger/plan.h) from the last cycle's pedal and boost.
  const float pedal = pedalCh >= 0 ? values[pedalCh] : NAN;
  const float boost = boostCh >= 0 ? values[boostCh] : NAN;
  Burst::Change bc = burst.update(start, pedal, boost);
  if (bc != Burst::NONE) {
    char text[64];
    if (bc == Burst::STARTED) snprintf(text, sizeof(text), "burst (pedal %.0f%% boost %.0f hPa)", pedal, boost);
    else snprintf(text, sizeof(text), "burst over");
    addNote(text);
  }
  for (uint32_t i : fastLayout.columns) values[i] = NAN;     // fast and medium cells
  if (millis() - driveStartMs >= ONCE_AFTER_MS) scheduler.openOnce();
  collect();
  std::vector<uint8_t> busy;
  for (const Pending& p : pendingReqs) busy.push_back(p.target);
  if (faultWanted >= 0) busy.push_back((uint8_t)faultWanted);
  for (uint8_t m : backoff.modules(millis())) busy.push_back(m);   // left alone a while
  size_t slowOnly = pendingReqs.size() >= MAX_LATER ? 0 : MAX_LATER - pendingReqs.size();
  if (slowOnly > SLOW_ONLY_PER_CYCLE) slowOnly = SLOW_ONLY_PER_CYCLE;
  std::vector<std::vector<size_t>> requests =
      scheduler.cycle(plan, millis(), busy, slowOnly, burst.on());
  std::vector<const std::vector<size_t>*> waited;
  for (const std::vector<size_t>& b : requests) {
    if (scheduler.waits(moduleOf(b))) waited.push_back(&b);
    else sendLater(b);
  }
  askAll(waited);
  collect();
  const uint32_t afterReads = millis();
  tFast += afterReads - start;
  const double secs = (afterReads - driveStartMs) / 1000.0;
  if (!fastLayout.columns.empty()) {
    if (millis() - lastStatsNoteMs >= STATS_MS) {
      lastStatsNoteMs = millis();
      size_t rested = 0;
      for (const Read& rd : plan.reads) rested += rd.restUntil != 0;
      std::string smallerList;
      for (const Read& rd : plan.reads) {
        char m[16];
        snprintf(m, sizeof(m), "0x%02X:%u", rd.module, (unsigned)plan.batchFor(rd.module));
        if (plan.batchFor(rd.module) < MAX_BATCH && smallerList.find(m) == std::string::npos) {
          smallerList += smallerList.empty() ? "" : " ";
          smallerList += m;
        }
      }
      std::string alone;
      for (uint8_t m : backoff.modules(millis())) {
        char a[8];
        snprintf(a, sizeof(a), "%s0x%02X", alone.empty() ? "" : " ", m);
        alone += a;
      }
      char text[300];
      snprintf(text, sizeof(text),
               "stats %.1f rows/s; reads %.0f ms; files %.0f ms; slow pass %.0f s; %u resting "
               "(%u refused); collected later %u (%u timed out); left alone: %s; smaller "
               "batches: %s; bursts %u (%.0f s)",
               cycleRate, msFast, msWrite, sweepPassMs / 1000.0f, (unsigned)rested,
               (unsigned)refusedReads, (unsigned)laterSent, (unsigned)laterTimeouts,
               alone.empty() ? "none" : alone.c_str(),
               smallerList.empty() ? "none" : smallerList.c_str(), (unsigned)burst.count(),
               burst.ms() / 1000.0f);
      addNote(text);
      laterSent = laterTimeouts = 0;
      std::string answers;
      for (int m = 0; m < 256; m++) {
        if (answerMs[m] <= 0) continue;
        char a[20];
        snprintf(a, sizeof(a), "%s0x%02X:%.0f", answers.empty() ? "" : " ", m, answerMs[m]);
        answers += a;
      }
      std::lock_guard<std::mutex> g(tagLock);
      liveBatches = smallerList.empty() ? "none" : smallerList;
      liveAnswers = answers;
      liveSlowPassS = sweepPassMs ? sweepPassMs / 1000.0f : NAN;
      liveReadsMs = msFast;
    }
    fastFile.add(fastLayout.row(table, secs, clockNow(), values, note.c_str()));
    note.clear();
    fastRows++;
  }
  // Every module's slow reads all come round: one row of the sweep file. The
  // periodic reads all asked: one more, once their answers are in.
  if (scheduler.slowPassDone() && scheduler.hasSlow()) {
    sweepRow(secs);
    sweepPassMs = millis() - sweepPassStart;
    sweepPassStart = millis();
  }
  if (scheduler.onceDone()) {
    periodicDone();
    onceRowAt = (millis() + LATER_MS + 500) | 1;
  }
  if (onceRowAt && (int32_t)(millis() - onceRowAt) >= 0) {
    onceRowAt = 0;
    sweepRow(secs);
  }
  if (millis() - lastFlushMs >= FLUSH_MS) {
    fastFile.flush();
    sweepFile.flush();
    lastFlushMs = millis();
  }
  tWrite += millis() - afterReads;
  cycles++;
  if (millis() - cycleStatsMs >= 5000) {
    cycleRate = cycles * 1000.0f / (millis() - cycleStatsMs);
    msFast = (float)tFast / cycles; msWrite = (float)tWrite / cycles;
    tFast = tWrite = 0;
    cycles = 0;
    cycleStatsMs = millis();
  }
  uint32_t spent = millis() - start;
  if (spent < MIN_CYCLE_MS) delay(MIN_CYCLE_MS - spent);
}

// ---- fault memory ------------------------------------------------------------
// One module's fault list when FaultScan says, between cycles, a row per
// failed code (or one saying there are none) in <drive>.faults.csv. A DME's
// is waited for (it answers in milliseconds); any other module's is
// collected when it comes, like its reads. A module still answering a read
// is held from new ones until its fault list has gone.
static const uint32_t FAULT_MS = 3000;
static uint16_t failedBy[256];               // failed codes per module, last read
static std::atomic<int> faultsNow{-1};       // their sum; -1 until the first read
static std::map<uint8_t, std::string> failedCodes;   // those codes by module, "1A400A 804415"
                                                     // (under tagLock)

static void readFaults() {
  if (connectionLost || burst.on()) return;
  if (faultWanted < 0) faultWanted = faultScan.next(millis());
  if (faultWanted < 0 || waitingOn((uint8_t)faultWanted) ||
      backoff.waiting((uint8_t)faultWanted, millis())) {
    return;
  }
  const int m = faultWanted;
  faultWanted = -1;
  hsfz::Client* cl = clientFor((uint8_t)m);
  if (!cl) return;
  if (!scheduler.waits((uint8_t)m)) {
    sendLater((uint8_t)m, DTC_REQUEST, sizeof(DTC_REQUEST), {});
    return;
  }
  faultAnswer((uint8_t)m,
              cl->request((uint8_t)m, DTC_REQUEST, sizeof(DTC_REQUEST), rx, sizeof(rx), FAULT_MS));
}

// A fault list's answer (in rx).
static void faultAnswer(uint8_t m, const hsfz::Reply& r) {
  if (r.result == hsfz::CLOSED) { connectionLost = true; return; }
  std::vector<Dtc> dtcs;
  int result = -1;
  if (r.result == hsfz::OK && parseDtcs(rx, r.len, dtcs)) result = 0;
  else if (r.result == hsfz::NEGATIVE) result = r.nrc;
  if (result >= 0) lastOkMs = millis();
  if (!faultFile.open) {
    faultFile.begin(String(DRIVES) + "/" + driveName + ".faults.csv", FAULT_HEADER);
    if (faultsNow < 0) { memset(failedBy, 0, sizeof(failedBy)); faultsNow = 0; }
  }
  std::string at = timeKnown() ? clockNow().substr(0, 19) : "";
  faultFile.add(faultRows(at, (millis() - driveStartMs) / 1000.0, m, result, dtcs));
  faultFile.flush();
  if (result != 0) return;
  uint16_t failed = 0;
  std::string codes;
  for (const Dtc& d : dtcs) {
    if (!dtcFailed(d.status)) continue;
    failed++;
    char c[8];
    snprintf(c, sizeof(c), "%s%06X", codes.empty() ? "" : " ", (unsigned)(d.code & 0xFFFFFF));
    codes += c;
  }
  {
    std::lock_guard<std::mutex> g(tagLock);
    if (codes.empty()) failedCodes.erase(m);
    else failedCodes[m] = codes;
  }
  if (failed != failedBy[m]) {
    char text[64];
    snprintf(text, sizeof(text), "faults 0x%02X: %u", m, (unsigned)failed);
    addNote(text);
    out.printf("[faults] module 0x%02X: %u failed codes\n", m, (unsigned)failed);
  }
  failedBy[m] = failed;
  int sum = 0;
  for (uint16_t n : failedBy) sum += n;
  faultsNow = sum;
}

// ---- full channel check -----------------------------------------------------
// lib/logger/carcheck.h. The Worker asks for one in a check-in's reply
// ("sweep": its id, set from the logger page); the upload task (core 0)
// keeps the request and downloads the list to the card (CHECK_LIST), and
// the next drive asks the list from a minute in, a few identifiers a cycle
// beside the fast reads (none in a burst), one collected later at a time
// for a module the fast reads don't wait on. Its place is kept, so a drive
// that ends first leaves the rest to the next. Keys in "logger": chkid (the
// request), chklist (the id the card's list is for), chknext (where it got
// to), chksilent (modules skipped), chkdone (finished, until the Worker
// has it).
static const char* CHECK_LIST = "/check.csv";
static const char* CHECK_DOWNLOAD = "/check.dl";
static const size_t CHECK_PER_CYCLE = 3;          // waited for (the DMEs) a cycle
static const uint32_t CHECK_SAVE_EVERY = 25;      // answers between saves of the place
static const uint32_t CHECK_LIST_RETRY_MS = 60000;
static std::string checkWanted, checkDone, checkSilentText;   // under tagLock
static std::atomic<bool> checkListReady{false};  // the card's list is the request's
static std::atomic<bool> checkCancel{false};     // the request went (core 0 says)
static std::atomic<uint32_t> checkNextNow{0}, checkTotal{0};  // for the check-ins
// This drive's:
static std::vector<CheckItem> checkItems;
static std::string checkId;                      // "": none this drive
static uint32_t checkNext = 0, checkSaved = 0, checkAsked = 0;
static bool checkInFlight = false;
static CheckSilence checkSilence;
static DriveFile checkFile;

static bool checkRunning() { return !checkId.empty(); }

// At power-on: a request kept from before, and whether its list is on the card.
static void checkBoot() {
  Preferences prefs;
  prefs.begin("logger", true);
  std::string id = prefs.getString("chkid", "").c_str();
  std::string list = prefs.getString("chklist", "").c_str();
  std::string done = prefs.getString("chkdone", "").c_str();
  std::string silent = prefs.getString("chksilent", "").c_str();
  prefs.end();
  checkListReady = !id.empty() && list == id && sdOk && SD.exists(CHECK_LIST);
  std::lock_guard<std::mutex> g(tagLock);
  checkWanted = id;
  checkDone = done;
  checkSilentText = silent;
}

// The modules skipped so far in this request, the earlier drives' with this one's.
static std::string checkSilentAll() {
  std::string all;
  {
    std::lock_guard<std::mutex> g(tagLock);
    all = checkSilentText;
  }
  std::string now = checkSilence.list();
  size_t at = 0;
  while (at < now.size()) {
    size_t end = now.find(' ', at);
    if (end == std::string::npos) end = now.size();
    std::string m = now.substr(at, end - at);
    if (all.find(m) == std::string::npos) all += (all.empty() ? "" : " ") + m;
    at = end + 1;
  }
  return all;
}

static void checkSave() {
  checkFile.flush();
  if (checkCancel) return;                       // the request went: nothing to keep
  std::string silent = checkSilentAll();
  Preferences prefs;
  prefs.begin("logger");
  prefs.putUInt("chknext", checkNext);
  prefs.putString("chksilent", silent.c_str());
  prefs.end();
  std::lock_guard<std::mutex> g(tagLock);
  checkSilentText = silent;
  checkSaved = checkNext;
}

static void checkStart() {
  checkItems.clear();
  checkId.clear();
  checkInFlight = false;
  checkSilence.reset();
  checkCancel = false;
  std::string want;
  {
    std::lock_guard<std::mutex> g(tagLock);
    want = checkWanted;
  }
  if (want.empty()) return;
  if (!checkListReady) {
    addNote("full channel check " + want + " waits for its list (Wi-Fi)");
    return;
  }
  std::string text;
  {
    std::lock_guard<std::recursive_mutex> g(sdLock);
    File f = SD.open(CHECK_LIST);
    if (f) {
      text.reserve(f.size());
      static uint8_t buf[1024];
      for (int n; (n = f.read(buf, sizeof(buf))) > 0;) text.append((const char*)buf, n);
      f.close();
    }
  }
  checkItems = parseCheckList(text);
  if (checkItems.empty()) { addNote("full channel check " + want + ": the list is empty"); return; }
  Preferences prefs;
  prefs.begin("logger", true);
  checkNext = checkSaved = prefs.getUInt("chknext", 0);
  prefs.end();
  if (checkNext > checkItems.size()) checkNext = checkSaved = 0;
  checkId = want;
  checkAsked = 0;
  checkNextNow = checkNext;
  checkTotal = checkItems.size();
  char note2[128];
  int n = snprintf(note2, sizeof(note2), "full channel check %s: %u identifiers", checkId.c_str(),
                   (unsigned)checkItems.size());
  if (checkNext && n > 0 && (size_t)n < sizeof(note2)) {
    snprintf(note2 + n, sizeof(note2) - n, " from %u", (unsigned)checkNext);
  }
  addNote(note2);
  out.printf("[check] %s\n", note2);
}

// Every identifier asked: the file closed, the request done (the Worker
// hears it in the next check-in).
static void checkFinish() {
  checkFile.finish();
  std::string silent = checkSilentAll();
  char text[160];
  snprintf(text, sizeof(text), "full channel check %s done: %u identifiers%s%s", checkId.c_str(),
           (unsigned)checkItems.size(), silent.empty() ? "" : "; no answer from ",
           silent.c_str());
  addNote(text);
  out.printf("[check] %s\n", text);
  Preferences prefs;
  prefs.begin("logger");
  prefs.remove("chkid");
  prefs.remove("chknext");
  prefs.putString("chkdone", checkId.c_str());
  prefs.putString("chksilent", silent.c_str());
  prefs.end();
  {
    std::lock_guard<std::mutex> g(tagLock);
    if (checkWanted == checkId) checkWanted.clear();
    checkDone = checkId;
    checkSilentText = silent;
  }
  checkId.clear();
  checkItems.clear();
}

// The drive ends: where the check got to, for the next.
static void checkEnd() {
  if (!checkRunning()) return;
  checkSave();
  char text[128];
  snprintf(text, sizeof(text), "full channel check %s: %u of %u asked; continues next drive",
           checkId.c_str(), (unsigned)checkNext, (unsigned)checkItems.size());
  addNote(text);
  out.printf("[check] %s\n", text);
  checkFile.finish();
  checkId.clear();
  checkItems.clear();
}

static void checkAnswer(const hsfz::Reply& r) {
  checkInFlight = false;
  if (r.result == hsfz::CLOSED) { connectionLost = true; return; }   // asked again next drive
  if (!checkRunning() || checkNext >= checkItems.size()) return;
  const CheckItem& it = checkItems[checkNext];
  // rx: 0x62, the identifier, its data.
  bool answered = r.result == hsfz::OK && r.len >= 3 && ((rx[1] << 8) | rx[2]) == it.did;
  size_t dataLen = answered ? r.len - 3 : 0;
  CheckStatus st = checkStatus(answered, r.result == hsfz::NEGATIVE, dataLen, it);
  if (st != CHECK_NO_ANSWER) lastOkMs = millis();
  if (!checkFile.open) {
    checkFile.begin(String(DRIVES) + "/" + driveName + ".check.csv", CHECK_HEADER);
  }
  checkFile.add(checkRow(it, "logger sweep " + checkId, st, r.nrc, dataLen));
  checkNext++;
  checkAsked++;
  checkNextNow = checkNext;
  if (checkSilence.answer(it.module, st)) {
    char text[96];
    snprintf(text, sizeof(text), "full channel check: module 0x%02X didn't answer %u in a row; "
             "skipped", it.module, (unsigned)CHECK_SILENT_TO_SKIP);
    addNote(text);
    out.printf("[check] %s\n", text);
  }
  if (checkNext - checkSaved >= CHECK_SAVE_EVERY) checkSave();
}

// Between cycles: the next few identifiers of the check.
static void readCheck() {
  if (checkCancel.exchange(false) && checkRunning()) {
    addNote("full channel check " + checkId + " cancelled");
    checkFile.finish();
    checkId.clear();
    checkItems.clear();
  }
  if (!checkRunning() || connectionLost || burst.on() || checkInFlight) return;
  if (millis() - driveStartMs < ONCE_AFTER_MS) return;
  for (size_t asked = 0; asked < CHECK_PER_CYCLE && !connectionLost && !checkInFlight;) {
    if (checkNext >= checkItems.size()) { checkFinish(); return; }
    const CheckItem& it = checkItems[checkNext];
    if (checkSilence.skipped(it.module)) {               // no row, as the app's check
      checkNext++;
      checkNextNow = checkNext;
      continue;
    }
    if (waitingOn(it.module) || faultWanted == it.module || backoff.waiting(it.module, millis())) {
      return;
    }
    hsfz::Client* cl = clientFor(it.module);
    uint8_t req[3] = {0x22, (uint8_t)(it.did >> 8), (uint8_t)it.did};
    if (!cl) {                                        // bank 2's connection was refused
      checkAnswer(hsfz::Reply());
      asked++;
      continue;
    }
    if (!scheduler.waits(it.module)) {
      if (pendingReqs.size() >= MAX_LATER) return;
      if (sendLater(it.module, req, sizeof(req), {}, true)) checkInFlight = true;
      else if (!connectionLost) checkAnswer(hsfz::Reply());
      return;
    }
    checkAnswer(cl->request(it.module, req, sizeof(req), rx, sizeof(rx), REQUEST_MS));
    asked++;
  }
}

// ---- uploads (core 0) --------------------------------------------------------
static std::atomic<uint32_t> uploadedFiles{0};
static String lastUpload = "none yet";     // under sdLock: the upload task sets it

// Room for a TLS session: mbedTLS wants about 40 KB of internal RAM, its
// 16 KB input record buffer in one piece (17 KB largest block failed, 45 KB
// works), and the check-ins (their own task) and the uploads and downloads
// (this one) can each want one at once. Waits up to `ms` for it.
static const size_t TLS_BLOCK = 24 * 1024, TLS_FREE = 48 * 1024;
static bool tlsRoom(uint32_t ms) {
  uint32_t t0 = millis();
  while (heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL) < TLS_BLOCK ||
         heap_caps_get_free_size(MALLOC_CAP_INTERNAL) < TLS_FREE) {
    if (millis() - t0 >= ms) return false;
    vTaskDelay(pdMS_TO_TICKS(100));
  }
  return true;
}

// A drive's file read out for its upload. While a drive is logging the
// uploads are low priority: a few KB at a time with pauses (about 80 KB/s),
// so the card, its SPI bus and core 0 stay mostly the logging's. Between
// drives, as fast as it goes.
static const size_t PACE_BYTES = 8192;
static const uint32_t PACE_MS = 100;
struct PacedFile : Stream {
  File& f;
  size_t sinceRest = 0;
  explicit PacedFile(File& file) : f(file) {}
  int available() override { return f.available(); }
  int read() override { return f.read(); }
  int peek() override { return f.peek(); }
  size_t readBytes(char* b, size_t n) override {
    if (state == LOGGING && sinceRest >= PACE_BYTES) {
      vTaskDelay(pdMS_TO_TICKS(PACE_MS));
      sinceRest = 0;
    }
    size_t got = f.read((uint8_t*)b, n);
    sinceRest += got;
    return got;
  }
  size_t write(uint8_t) override { return 0; }
};

// Finished files (those with a .done), oldest name first.
static std::vector<String> pending() {
  std::vector<String> names;
  std::lock_guard<std::recursive_mutex> g(sdLock);
  File dir = SD.open(DRIVES);
  if (!dir) return names;
  for (File f = dir.openNextFile(); f; f = dir.openNextFile()) {
    String name = f.name();
    f.close();
    if (name.endsWith(".csv.done")) names.push_back(name.substring(0, name.length() - 5));
  }
  dir.close();
  std::sort(names.begin(), names.end(), [](const String& a, const String& b) { return a < b; });
  return names;
}

static bool uploadOne(const String& name) {
  String path = String(DRIVES) + "/" + name;
  Done done;
  bool doneOk = false;
  File f;
  {
    std::lock_guard<std::recursive_mutex> g(sdLock);
    File d = SD.open(path + ".done");
    if (d) {
      doneOk = parseDone(d.readStringUntil('\n').c_str(), done);
      d.close();
    }
    if (doneOk) f = SD.open(path);
  }
  switch (uploadStep(doneOk, (bool)f, f ? f.size() : 0)) {   // lib/logger/upload.h
  case UPLOAD_BAD_DONE:
    out.printf("[up] %s: bad .done file\n", name.c_str());
    return false;
  case UPLOAD_MISSING:
    out.printf("[up] %s: missing\n", name.c_str());
    return false;
  case UPLOAD_EMPTY: {
    f.close();
    std::lock_guard<std::recursive_mutex> g(sdLock);
    SD.remove(path);
    SD.remove(path + ".done");
    out.printf("[up] %s: empty, removed\n", name.c_str());
    return true;
  }
  case UPLOAD_SEND:
    break;
  }
  if (!tlsRoom(10000)) {
    f.close();
    out.printf("[up] %s: not enough memory for TLS; later\n", name.c_str());
    return false;
  }
  NetworkClientSecure tls;
  tls.useBuiltinCACertBundle();
  HTTPClient http;
  http.setTimeout(30000);
  http.setConnectTimeout(10000);
  bool ok = false;
  String url = String("https://") + WORKER_HOST + "/api/files/logger/" + name;
  if (http.begin(tls, url)) {
#ifdef ACCESS_CLIENT_ID
    http.addHeader("CF-Access-Client-Id", ACCESS_CLIENT_ID);
    http.addHeader("CF-Access-Client-Secret", ACCESS_CLIENT_SECRET);
#endif
    http.addHeader("Content-Type", "text/csv");
    uint32_t t0 = millis();
    PacedFile paced(f);
    int code = http.sendRequest("PUT", &paced, f.size());
    String body = code > 0 ? http.getString() : String();
    http.end();
    ok = uploadAccepted(code, body.c_str(), done.sha);
    if (ok) {
      int p = body.indexOf("\"processed\":");
      out.printf("[up] %s: %u bytes in %.1f s, checked%s\n", name.c_str(), done.size,
                 (millis() - t0) / 1000.0f,
                 p < 0 ? "" : body.substring(p + 12, p + 17) == "false" ? ", not processed"
                                                                         : ", processed");
    } else {
      char tlsErr[100] = "";
      if (code < 0) tls.lastError(tlsErr, sizeof(tlsErr));
      out.printf("[up] %s: %s %d %s%s%s\n", name.c_str(), code == 200 ? "hash differs:" : "failed:",
                 code, code < 0 ? HTTPClient::errorToString(code).c_str()
                                : body.substring(0, 160).c_str(),
                 tlsErr[0] ? "; TLS: " : "", tlsErr);
    }
  }
  f.close();
  if (ok) {
    std::lock_guard<std::recursive_mutex> g(sdLock);
    SD.remove(path);
    SD.remove(path + ".done");
    uploadedFiles++;
    lastUpload = name;
  }
  return ok;
}

// The networks the board knows (logger_secrets.h): WIFI_SSID first, and
// WIFI_SSID2 as a backup (a phone hotspot uses mobile data, so home wins
// whenever it's in range).
static bool wifiKnown = false;
static const uint32_t WIFI_RETRY_MS = 10000;       // off both: look again
static const uint32_t WIFI_HOME_CHECK_MS = 120000; // on the backup: look for home

static bool join(const char* ssid, const char* pass, uint32_t ms) {
  WiFi.begin(ssid, pass);
  uint32_t t0 = millis();
  while (!WiFi.isConnected() && millis() - t0 < ms) delay(100);
  return WiFi.isConnected();
}

// One scan (about 2 s) says which known network is in range, and only that
// one is joined: waiting 8 s on each in turn held the first check-in back
// minutes at a power-on away from home. A scan that fails tries both.
static bool joinPreferred() {
#ifdef WIFI_SSID
  int n = WiFi.scanNetworks();
  bool home = n < 0, backup = n < 0;
  for (int i = 0; i < n; i++) {
    home = home || WiFi.SSID(i) == WIFI_SSID;
#ifdef WIFI_SSID2
    backup = backup || WiFi.SSID(i) == WIFI_SSID2;
#endif
  }
  WiFi.scanDelete();
  if (home && join(WIFI_SSID, WIFI_PASS, 10000)) return true;
#ifdef WIFI_SSID2
  if (backup) {
    WiFi.disconnect();
    if (join(WIFI_SSID2, WIFI_PASS2, 10000)) return true;
  }
#endif
#endif
  (void)backup;
  return false;
}

// Keep a network up, home first (from this task, between uploads, so the
// logging on the other core never waits for a scan or a join).
static void keepWifi() {
  static uint32_t lastTry = 0, lastHomeCheck = 0;
  static bool mdns = false, tried = false;
  if (!wifiKnown) return;
  if (!WiFi.isConnected()) {
    if (tried && millis() - lastTry < WIFI_RETRY_MS) return;
    lastTry = millis();
    bool joined = joinPreferred();
    if (joined && !mdns) mdns = MDNS.begin(HOSTNAME);
#ifdef AP_PASS
    if (!joined && !tried) {
      WiFi.mode(WIFI_AP_STA);                // the terminal, while the networks are away
      WiFi.softAP(HOSTNAME, AP_PASS);
      out.printf("[wifi] no known network in range; access point %s, terminal on %s:%u\n",
                 HOSTNAME, WiFi.softAPIP().toString().c_str(), TERMINAL_PORT);
    }
#endif
    tried = true;
    return;
  }
#if defined(WIFI_SSID) && defined(WIFI_SSID2)
  if (WiFi.SSID() != WIFI_SSID2 || millis() - lastHomeCheck < WIFI_HOME_CHECK_MS) return;
  lastHomeCheck = millis();
  int n = WiFi.scanNetworks();             // the connection stays up meanwhile
  bool home = false;
  for (int i = 0; i < n; i++) home = home || WiFi.SSID(i) == WIFI_SSID;
  WiFi.scanDelete();
  if (home) {
    out.printf("[wifi] %s in range again; moving off the backup\n", WIFI_SSID);
    WiFi.disconnect();
    joinPreferred();
  }
#endif
}

// ---- check-ins (core 0) ------------------------------------------------------
// A small JSON report to the Worker (POST /api/logger/heartbeat) once at
// power-on and every 10 s while Wi-Fi is up, so the cloud knows the board is
// on, and what it's doing, between uploads. One that fails is skipped; a
// long upload delays them. With no Wi-Fi (and the time known), each is kept
// on the card instead (BEATS_FILE, "buffered": true) and sent at the next
// connection, in batches (POST /api/logger/heartbeats), which the Worker
// files under the time each was made.
static const uint32_t BEAT_MS = 10000;
static std::atomic<uint32_t> beatsSent{0};
static const char* BEATS_FILE = "/beats.jsonl";
static const int BEATS_BATCHES_PER_TURN = 4;        // then the drive uploads get a turn
static const size_t MAX_FAULT_CODES_TEXT = 400;      // a check-in's fault_codes, about 25 codes
static uint32_t beatsKept = 0;

static void jsonText(std::string& j, const char* key, const char* v) {
  j += ",\"";
  j += key;
  j += "\":\"";
  for (const char* p = v; *p; p++) {
    if (*p != '"' && *p != '\\' && (uint8_t)*p >= 0x20) j += *p;
  }
  j += '"';
}

static void jsonNumber(std::string& j, const char* key, double v, int decimals) {
  char s[48];
  if (isnan(v)) snprintf(s, sizeof(s), ",\"%s\":null", key);
  else snprintf(s, sizeof(s), ",\"%s\":%.*f", key, decimals, v);
  j += s;
}

static std::string beatJson(bool boot, bool buffered = false) {
  static const char* const states[] = {"waiting for link", "searching", "logging"};
  std::string j = "{\"boot\":" + std::to_string(bootCount);
  jsonText(j, "event", boot ? "boot" : "beat");
  if (buffered) j += ",\"buffered\":true";
  jsonText(j, "reset", resetReason());
  jsonNumber(j, "up_s", millis() / 1000.0, 0);
  State s = state;
  jsonText(j, "state", states[s]);
  if (s == LOGGING) {
    jsonNumber(j, "drive_min", (millis() - driveStartMs) / 60000.0, 1);
    jsonNumber(j, "rows", fastRows, 0);
    jsonNumber(j, "rows_per_s", cycleRate, 1);
  }
  jsonNumber(j, "speed_kmh", live(SPEED), 1);
  jsonNumber(j, "rpm", live(RPM), 0);
  jsonNumber(j, "battery_v", live(BATTERY), 2);
  jsonNumber(j, "coolant_c", live(COOLANT), 1);
  jsonNumber(j, "oil_c", live(OIL), 1);
  double lat = live(LAT), lon = live(LON);
  if (lat == 0 && lon == 0) lat = lon = NAN;   // the head unit before a fix
  jsonNumber(j, "lat", lat, 6);
  jsonNumber(j, "lon", lon, 6);
  int faults = faultsNow;
  jsonNumber(j, "faults", faults < 0 ? NAN : faults, 0);
  if (faults > 0) {                          // which: "0x12:1A400A 0x40:804415"
    std::string codes;
    {
      std::lock_guard<std::mutex> g(tagLock);
      for (const auto& [m, list] : failedCodes) {
        if (codes.size() >= MAX_FAULT_CODES_TEXT) break;
        size_t at = 0;
        while (at < list.size()) {
          size_t end = list.find(' ', at);
          if (end == std::string::npos) end = list.size();
          char one[20];
          snprintf(one, sizeof(one), "%s0x%02X:%s", codes.empty() ? "" : " ", m,
                   list.substr(at, end - at).c_str());
          codes += one;
          at = end + 1;
        }
      }
    }
    jsonText(j, "fault_codes", codes.c_str());
  }
  j += linkUp ? ",\"link\":true" : ",\"link\":false";
  jsonText(j, "vin", vin);
  jsonText(j, "fw", FW_VERSION);
  if (timeKnown()) jsonText(j, "clock", clockNow().c_str());
  jsonText(j, "wifi", WiFi.SSID().c_str());
  jsonNumber(j, "rssi", WiFi.RSSI(), 0);
  jsonNumber(j, "pending", sdOk ? pending().size() : 0, 0);
  jsonNumber(j, "uploaded", uploadedFiles, 0);
  jsonNumber(j, "sd_free_mb", sdOk ? (SD.totalBytes() - SD.usedBytes()) / 1e6 : NAN, 0);
  jsonNumber(j, "heap_kb", heap_caps_get_free_size(MALLOC_CAP_INTERNAL) / 1024, 0);
  jsonNumber(j, "largest_kb", heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL) / 1024, 0);
  {
    std::lock_guard<std::mutex> g(tagLock);
    jsonText(j, "channels", channelsTag.c_str());
    if (!otaStatus.empty()) jsonText(j, "ota", otaStatus.c_str());
    if (!checkWanted.empty()) {
      jsonText(j, "sweep", checkWanted.c_str());
      jsonNumber(j, "sweep_next", checkNextNow, 0);
      jsonNumber(j, "sweep_of", checkTotal ? (double)checkTotal : NAN, 0);
    }
    if (!checkDone.empty()) jsonText(j, "sweep_done", checkDone.c_str());
    if (!checkSilentText.empty()) jsonText(j, "sweep_silent", checkSilentText.c_str());
    if (!liveBatches.empty()) jsonText(j, "batches", liveBatches.c_str());
    if (!liveAnswers.empty()) jsonText(j, "answer_ms", liveAnswers.c_str());
    jsonNumber(j, "slow_pass_s", liveSlowPassS, 0);
    jsonNumber(j, "reads_ms", liveReadsMs, 0);
  }
  return j + "}";
}

static int postBeat(const std::string& body, const char* path = "/api/logger/heartbeat",
                    std::string* reply = nullptr) {
  if (!tlsRoom(3000)) return -1;
  NetworkClientSecure tls;
  tls.useBuiltinCACertBundle();
  HTTPClient http;
  http.setTimeout(8000);
  http.setConnectTimeout(5000);
  if (!http.begin(tls, String("https://") + WORKER_HOST + path)) return -1;
#ifdef ACCESS_CLIENT_ID
  http.addHeader("CF-Access-Client-Id", ACCESS_CLIENT_ID);
  http.addHeader("CF-Access-Client-Secret", ACCESS_CLIENT_SECRET);
#endif
  http.addHeader("Content-Type", "application/json");
  int code = http.POST((uint8_t*)body.data(), body.size());
  if (reply && code == 200) *reply = http.getString().c_str();
  http.end();
  return code;
}

// Check-ins made before the board knows the time (no Wi-Fi yet, and no car
// to take the clock from): held here with when they were made, then dated
// and kept on the card once the clock is known, so a power-on's first
// minutes aren't lost. The oldest go first past MAX_HELD_BEATS.
struct HeldBeat {
  uint32_t ms;
  std::string json;
};
static std::vector<HeldBeat> heldBeats;
static const size_t MAX_HELD_BEATS = 90;            // 15 minutes

static void appendBeat(const std::string& line) {
  std::lock_guard<std::recursive_mutex> g(sdLock);
  File f = SD.open(BEATS_FILE, FILE_APPEND);
  if (!f) return;
  if (beatFits(f.size(), line.size())) {
    f.write((const uint8_t*)line.data(), line.size());
    beatsKept++;
  }
  f.close();
}

// The held check-ins, dated now that the clock is known, onto the card.
static void keepHeldBeats() {
  if (heldBeats.empty() || !sdOk || !timeKnown()) return;
  for (const HeldBeat& h : heldBeats) {
    int64_t ms = nowEpochMs() - (int64_t)(millis() - h.ms);
    std::string clock = clockText(ms, offsetMinutes((time_t)(ms / 1000)));
    std::string line = h.json.substr(0, h.json.size() - 1) + ",\"clock\":\"" + clock + "\"}\n";
    appendBeat(line);
  }
  out.printf("[beat] %u check-ins from before the clock was known kept\n",
             (unsigned)heldBeats.size());
  heldBeats.clear();
}

// A check-in made with no Wi-Fi: onto the card (the Worker files it under
// its clock), or held until the clock is known.
static void keepBeat() {
  if (!sdOk) return;
  if (!timeKnown()) {
    if (heldBeats.size() >= MAX_HELD_BEATS) heldBeats.erase(heldBeats.begin());
    heldBeats.push_back({millis(), beatJson(false, true)});
    return;
  }
  keepHeldBeats();
  appendBeat(beatJson(false, true) + "\n");
}

// The kept check-ins, a few batches a turn; the file goes once all are in.
// `sentTo`: how far this boot got, so a failed batch is tried again from
// there (a reboot starts over; the Worker skips ones it has).
static void sendKeptBeats() {
  static uint32_t sentTo = 0;
  static uint8_t buf[2048];
  for (int n = 0; n < BEATS_BATCHES_PER_TURN && WiFi.isConnected(); n++) {
    BeatBatch batch;                           // lib/logger/upload.h
    uint32_t size = 0;
    {
      std::lock_guard<std::recursive_mutex> g(sdLock);
      File f = SD.open(BEATS_FILE);
      if (!f) { sentTo = 0; return; }
      size = f.size();
      f.seek(sentTo);
      bool more = true;
      while (more && f.available()) {
        int k = f.read(buf, sizeof(buf));
        if (k <= 0) break;
        more = batch.feed(buf, k);
      }
      f.close();
    }
    if (!batch.count()) {                      // all sent (or only a torn last line)
      std::lock_guard<std::recursive_mutex> g(sdLock);
      SD.remove(BEATS_FILE);
      if (beatsKept || sentTo) {
        out.printf("[beat] sent the check-ins kept offline (%u bytes)\n", (unsigned)size);
      }
      sentTo = beatsKept = 0;
      return;
    }
    int code = postBeat(batch.body(), "/api/logger/heartbeats");
    if (code != 200) {
      out.printf("[beat] kept check-ins: send failed (%d); trying again later\n", code);
      return;
    }
    sentTo += batch.used();
  }
}

// A check-in's reply: the full channel check the Worker wants ("sweep": its
// id, or none). A new one is kept and its list fetched; none while one is
// kept is a cancel; none once one has finished is the Worker having it.
static void checkReply(const std::string& body) {
  std::string want;
  if (!jsonField(body, "sweep", want) || want == "null") want.clear();
  for (char c : want) {
    if (!isalnum((unsigned char)c) && c != '-' && c != '_' && c != '.') return;
  }
  if (want.size() > 40) return;
  std::lock_guard<std::mutex> g(tagLock);
  Preferences prefs;
  prefs.begin("logger");
  if (!checkDone.empty() && want != checkDone) {
    prefs.remove("chkdone");
    prefs.remove("chksilent");
    checkDone.clear();
    checkSilentText.clear();
  }
  if (want != checkWanted && want != checkDone) {
    prefs.remove("chknext");
    prefs.remove("chksilent");
    checkSilentText.clear();
    if (!checkWanted.empty()) checkCancel = true;
    if (want.empty()) {
      prefs.remove("chkid");
      out.printf("[check] full channel check %s cancelled\n", checkWanted.c_str());
    } else {
      prefs.putString("chkid", want.c_str());
      out.printf("[check] full channel check %s asked for: on the next drive\n", want.c_str());
    }
    checkWanted = want;
    checkListReady = false;
  }
  prefs.end();
}

// Every BEAT_MS by the clock: a check-in that takes a second or two (a TLS
// handshake each) doesn't push the next one back, and one that took longer
// than BEAT_MS isn't made up with a burst.
static void beatIfDue() {
  static uint32_t next = 0;
  static bool tried = false, booted = false, failing = false;
  if (!strlen(WORKER_HOST)) return;
#ifdef OTA_TEST_SILENT
  return;                                  // a test build that never checks in: it must go back
#endif
  if (tried && (int32_t)(millis() - next) < 0) return;
  next = tried && millis() - next < BEAT_MS ? next + BEAT_MS : millis() + BEAT_MS;
  tried = true;
  if (!WiFi.isConnected()) { keepBeat(); return; }
  keepHeldBeats();                         // sent with the kept ones below
  std::string reply;
  int code = postBeat(beatJson(!booted), "/api/logger/heartbeat", &reply);
  if (code == 200) {
    beatsSent++;
    checkReply(reply);
    otaKeep("checked in");
    if (!booted) out.printf("[beat] checked in (boot %u)\n", (unsigned)bootCount);
    else if (failing) out.println("[beat] checking in again");
    booted = true;
    failing = false;
    if (sdOk) sendKeptBeats();
  } else if (!failing) {
    out.printf("[beat] check-in failed: %d %s\n", code,
               code < 0 ? HTTPClient::errorToString(code).c_str() : "");
    failing = true;
  }
}

// ---- channels from the cloud (core 0) -----------------------------------------
// The Worker makes channels.csv from the active preset on the logger channel
// page (GET /api/logger/channels). The board asks soon after Wi-Fi comes up
// and every 10 minutes, with the ETag of the last one it took, so it
// downloads only a changed file. A new file replaces the card's and is used
// from the next drive. One sent over the terminal stays until the active
// preset changes.
static const uint32_t CHANNELS_CHECK_MS = 10 * 60 * 1000;
static const char* CHANNELS_DOWNLOAD = "/channels.dl";

static void fetchChannels() {
  NetworkClientSecure tls;
  tls.useBuiltinCACertBundle();
  HTTPClient http;
  http.setTimeout(30000);
  http.setConnectTimeout(10000);
  if (!http.begin(tls, String("https://") + WORKER_HOST + "/api/logger/channels")) return;
#ifdef ACCESS_CLIENT_ID
  http.addHeader("CF-Access-Client-Id", ACCESS_CLIENT_ID);
  http.addHeader("CF-Access-Client-Secret", ACCESS_CLIENT_SECRET);
#endif
  String have;
  {
    Preferences prefs;
    prefs.begin("logger", true);
    if (prefs.isKey("cltag")) have = prefs.getString("cltag");
    prefs.end();
  }
  if (have.length()) http.addHeader("If-None-Match", "\"" + have + "\"");
  const char* keep[] = {"ETag", "X-Preset"};
  http.collectHeaders(keep, 2);
  int code = http.GET();
  if (code == 304) { http.end(); return; }
  if (code != 200) {
    String why = code < 0 ? HTTPClient::errorToString(code) : http.getString().substring(0, 120);
    out.printf("[chan] channel check failed: %d %s\n", code, why.c_str());
    http.end();
    return;
  }
  String etag = http.header("ETag");
  etag.replace("\"", "");
  String preset;                             // the Worker sends it URL-encoded
  String sent = http.header("X-Preset");
  for (unsigned i = 0; i < sent.length(); i++) {
    if (sent[i] == '%' && i + 2 < sent.length()) {
      preset += (char)strtol(sent.substring(i + 1, i + 3).c_str(), nullptr, 16);
      i += 2;
    } else {
      preset += sent[i];
    }
  }
  int expect = http.getSize();
  File f;
  {
    std::lock_guard<std::recursive_mutex> g(sdLock);
    f = SD.open(CHANNELS_DOWNLOAD, FILE_WRITE);
  }
  if (!f) { http.end(); out.println("[chan] can't write the download to the card"); return; }
  int got = http.writeToStream(&f);
  f.close();
  http.end();
  bool ok = got > 0 && (expect < 0 || got == expect) && etag.length();
  if (ok) {
    std::lock_guard<std::recursive_mutex> g(sdLock);
    File c = SD.open(CHANNELS_DOWNLOAD);
    char head[6] = "";
    if (c) { c.read((uint8_t*)head, 5); c.close(); }
    ok = !strcmp(head, "name,");
  }
  std::lock_guard<std::recursive_mutex> g(sdLock);
  if (!ok) {
    SD.remove(CHANNELS_DOWNLOAD);
    out.printf("[chan] download incomplete (%d of %d bytes); card unchanged\n", got, expect);
    return;
  }
  SD.remove(CHANNELS_FILE);
  SD.rename(CHANNELS_DOWNLOAD, CHANNELS_FILE);
  Preferences prefs;
  prefs.begin("logger");
  prefs.putString("cltag", etag);
  prefs.putString("chsrc", etag);
  prefs.putString("chname", preset);
  prefs.end();
  channelsDownloaded = true;
  out.printf("[chan] downloaded preset %s (%s, %d bytes); used from the next drive\n",
             preset.c_str(), etag.c_str(), got);
}

static void channelsIfDue() {
  static uint32_t last = 0;
  static bool tried = false;
  if (!sdOk || !WiFi.isConnected() || !strlen(WORKER_HOST)) return;
  if (tried && millis() - last < CHANNELS_CHECK_MS) return;
  tried = true;
  last = millis();
  fetchChannels();
}

// ---- firmware updates over the air -------------------------------------------
// The Worker offers one build (GET /api/logger/firmware: version, SHA-256,
// size; firmware/tools/publish_firmware.py publishes it). The upload task
// checks soon after Wi-Fi comes up and every 10 minutes and, for a version
// it wants (lib/logger/ota.h), downloads the image to the card and checks
// its hash: the card, not the flash, so a drive's logging isn't held up by
// flash writes. It's installed into the other app slot at the next power-on,
// before the car is looked for (about 10 s), or once the board has been idle
// a minute (on the bench), and the board restarts into it.
//
// A new build is on trial until it checks in with the Worker or reaches the
// car; if it does neither within OTA_TRIAL_MS, or crashes or hangs first, the
// bootloader goes back to the last build, which then won't take that version
// again. Keys in "logger": otasd/otasha (a checked image on the card), otato
// (the version just installed), otabad (one that went back).
static const char* FW_FILE = "/fw.bin";
static const char* FW_DOWNLOAD = "/fw.dl";
static const uint32_t OTA_CHECK_MS = 10 * 60 * 1000;
static const uint32_t OTA_IDLE_MS = 60000;
static const uint32_t OTA_IDLE_MAX_MS = 10 * 60000;
static std::atomic<bool> otaReady{false};     // a checked image on the card
static std::atomic<bool> otaTrial{false};     // this build is new and not kept yet

static void setOta(const std::string& s) {
  std::lock_guard<std::mutex> g(tagLock);
  otaStatus = s;
}

// The Arduino core marks a new build good as it starts unless this says it
// will decide itself (otaKeep). The core's default is a weak C function and
// no header declares it, so this must be extern "C" to replace it: without,
// a new build was kept at once and couldn't go back (found on the bench).
extern "C" bool verifyRollbackLater() { return true; }

static void otaKeep(const char* why) {
  if (!otaTrial.exchange(false)) return;
  esp_ota_mark_app_valid_cancel_rollback();
  Preferences prefs;
  prefs.begin("logger");
  prefs.remove("otato");
  prefs.end();
  setOta("");
  out.printf("[ota] firmware %s kept (%s)\n", FW_VERSION, why);
}

static void otaForget() {                      // the card's image, gone or not wanted
  std::lock_guard<std::recursive_mutex> g(sdLock);
  SD.remove(FW_FILE);
  Preferences prefs;
  prefs.begin("logger");
  prefs.remove("otasd");
  prefs.remove("otasha");
  prefs.end();
  otaReady = false;
}

// At boot, before the car: whether this build is on trial, whether the last
// one went back, and whether an image waits on the card.
static void otaBoot() {
  esp_ota_img_states_t st;
  const esp_partition_t* run = esp_ota_get_running_partition();
  Preferences prefs;
  prefs.begin("logger");
  String to = prefs.getString("otato", "");
  bool pending = run && esp_ota_get_state_partition(run, &st) == ESP_OK &&
                 st == ESP_OTA_IMG_PENDING_VERIFY;
  OtaStart start = otaStart(pending, to.c_str(), FW_VERSION);   // lib/logger/ota.h
  if (start == OTA_TRIAL) {
    otaTrial = true;
    setOta(std::string("trying ") + FW_VERSION);
    out.printf("[ota] firmware %s on trial: kept once it checks in or reaches the car\n", FW_VERSION);
  } else if (start == OTA_WENT_BACK) {
    prefs.putString("otabad", to);
    prefs.remove("otato");
    setOta(std::string("went back from ") + to.c_str());
    out.printf("[ota] firmware %s didn't prove itself; back on %s\n", to.c_str(), FW_VERSION);
  }
  String sd = prefs.getString("otasd", ""), bad = prefs.getString("otabad", "");
  prefs.end();
  if (!sd.length()) return;
  if (cardImageWanted(sdOk && SD.exists(FW_FILE), sd.c_str(), FW_VERSION, bad.c_str())) {
    otaReady = true;
  } else {
    otaForget();
  }
}

// The card's image into the other app slot, checking its hash again on the
// way, then a restart into it. A failure marks the version bad.
static void otaInstall() {
  Preferences prefs;
  prefs.begin("logger", true);
  std::string version = prefs.getString("otasd", "").c_str();
  std::string want = prefs.getString("otasha", "").c_str();
  prefs.end();
  setOta("installing " + version);
  out.printf("[ota] installing firmware %s (running %s)\n", version.c_str(), FW_VERSION);
  static uint8_t buf[4096];
  bool ok = false;
  std::string why;
  {
    std::lock_guard<std::recursive_mutex> g(sdLock);
    File f = SD.open(FW_FILE);
    ok = f && f.size() > 0 && Update.begin(f.size());
    if (!ok) why = f ? Update.errorString() : "no image on the card";
    Sha256 sha;
    while (ok && f.available()) {
      int k = f.read(buf, sizeof(buf));
      if (k <= 0) break;
      sha.update(buf, k);
      ok = Update.write(buf, k) == (size_t)k;
      if (!ok) why = Update.errorString();
    }
    if (f) f.close();
    char hex[65];
    sha.finishHex(hex);
    if (ok && want != hex) { ok = false; why = "the card's copy changed"; }
    if (ok) {
      ok = Update.end();                       // checks the image, and boots it next
      if (!ok) why = Update.errorString();
    } else if (Update.isRunning()) {
      Update.abort();
    }
  }
  otaForget();
  prefs.begin("logger");
  if (!ok) {
    prefs.putString("otabad", version.c_str());
    prefs.end();
    setOta("install of " + version + " failed: " + why);
    out.printf("[ota] install of %s failed (%s); staying on %s\n", version.c_str(), why.c_str(),
               FW_VERSION);
    return;
  }
  prefs.putString("otato", version.c_str());
  prefs.end();
  out.printf("[ota] installed %s; restarting into it\n", version.c_str());
  delay(300);
  ESP.restart();
}

// Back to the last build if this one hasn't proved itself in time.
static void otaTrialCheck() {
  if (!trialOver(otaTrial, millis())) return;
  out.printf("[ota] firmware %s didn't check in or reach the car in %u min; going back\n", FW_VERSION,
             (unsigned)(OTA_TRIAL_MS / 60000));
  delay(300);
  esp_ota_mark_app_invalid_rollback_and_reboot();
}

static int workerGet(HTTPClient& http, NetworkClientSecure& tls, const char* path) {
  if (!tlsRoom(10000)) return -1;
  tls.useBuiltinCACertBundle();
  http.setTimeout(30000);
  http.setConnectTimeout(10000);
  if (!http.begin(tls, String("https://") + WORKER_HOST + path)) return -1;
#ifdef ACCESS_CLIENT_ID
  http.addHeader("CF-Access-Client-Id", ACCESS_CLIENT_ID);
  http.addHeader("CF-Access-Client-Secret", ACCESS_CLIENT_SECRET);
#endif
  return http.GET();
}

// The image to the card, hashed as it comes; kept only if size and hash match.
static bool otaDownload(const Manifest& m) {
  NetworkClientSecure tls;
  HTTPClient http;
  int code = workerGet(http, tls, "/api/logger/firmware.bin");
  if (code != 200) { http.end(); out.printf("[ota] download: %d\n", code); return false; }
  File f;
  {
    std::lock_guard<std::recursive_mutex> g(sdLock);
    SD.remove(FW_DOWNLOAD);
    f = SD.open(FW_DOWNLOAD, FILE_WRITE);
  }
  if (!f) { http.end(); return false; }
  NetworkClient* s = http.getStreamPtr();
  static uint8_t buf[4096];
  Sha256 sha;
  size_t got = 0;
  uint32_t lastData = millis();
  while (got < m.size && (http.connected() || s->available()) && millis() - lastData < 30000) {
    size_t a = s->available();
    if (!a) { delay(5); continue; }
    size_t want = std::min(std::min(a, sizeof(buf)), m.size - got);
    int k = s->readBytes(buf, want);
    if (k <= 0) continue;
    if (f.write(buf, k) != (size_t)k) break;
    sha.update(buf, k);
    got += k;
    lastData = millis();
  }
  f.close();
  http.end();
  char hex[65];
  sha.finishHex(hex);
  std::lock_guard<std::recursive_mutex> g(sdLock);
  if (got != m.size || m.sha256 != hex) {
    SD.remove(FW_DOWNLOAD);
    out.printf("[ota] download of %s: %u of %u bytes, hash %s\n", m.version.c_str(), (unsigned)got,
               (unsigned)m.size, m.sha256 == hex ? "ok" : "differs");
    return false;
  }
  SD.remove(FW_FILE);
  SD.rename(FW_DOWNLOAD, FW_FILE);
  Preferences prefs;
  prefs.begin("logger");
  prefs.putString("otasd", m.version.c_str());
  prefs.putString("otasha", m.sha256.c_str());
  prefs.end();
  return true;
}

static void otaCheckIfDue() {
  static uint32_t last = 0;
  static bool tried = false;
  if (!strcmp(FW_VERSION, "dev") || otaReady || otaTrial || !sdOk || !WiFi.isConnected() ||
      !strlen(WORKER_HOST)) {
    return;
  }
  if (tried && millis() - last < OTA_CHECK_MS) return;
  tried = true;
  last = millis();
  NetworkClientSecure tls;
  HTTPClient http;
  int code = workerGet(http, tls, "/api/logger/firmware");
  std::string body = code == 200 ? http.getString().c_str() : "";
  http.end();
  Manifest m;
  if (code != 200 || !parseManifest(body, m)) return;   // 404: nothing published
  Preferences prefs;
  prefs.begin("logger", true);
  std::string bad = prefs.getString("otabad", "").c_str();
  prefs.end();
  if (!wantUpdate(FW_VERSION, m.version, bad)) return;
  out.printf("[ota] firmware %s offered (running %s); downloading\n", m.version.c_str(), FW_VERSION);
  setOta("downloading " + m.version);
  if (!otaDownload(m)) { setOta("download of " + m.version + " failed"); return; }
  otaReady = true;
  setOta("downloaded " + m.version + "; installs at the next power-on");
  out.printf("[ota] %s on the card; installs at the next power-on or after a minute idle\n",
             m.version.c_str());
}

// The full channel check's list, to the card, once a check is asked for.
static void checkListIfDue() {
  static uint32_t last = 0;
  static bool tried = false;
  if (checkListReady || !sdOk || !WiFi.isConnected() || !strlen(WORKER_HOST)) return;
  std::string want;
  {
    std::lock_guard<std::mutex> g(tagLock);
    want = checkWanted;
  }
  if (want.empty() || (tried && millis() - last < CHECK_LIST_RETRY_MS)) return;
  tried = true;
  last = millis();
  NetworkClientSecure tls;
  HTTPClient http;
  int code = workerGet(http, tls, "/api/logger/check-list");
  if (code != 200) {
    out.printf("[check] list: %d %s\n", code, code < 0 ? HTTPClient::errorToString(code).c_str()
                                                       : http.getString().substring(0, 120).c_str());
    http.end();
    return;
  }
  int expect = http.getSize();
  File f;
  {
    std::lock_guard<std::recursive_mutex> g(sdLock);
    f = SD.open(CHECK_DOWNLOAD, FILE_WRITE);
  }
  if (!f) { http.end(); return; }
  int got = http.writeToStream(&f);
  f.close();
  http.end();
  std::lock_guard<std::recursive_mutex> g(sdLock);
  if (got <= 0 || (expect >= 0 && got != expect)) {
    SD.remove(CHECK_DOWNLOAD);
    out.printf("[check] list incomplete (%d of %d bytes)\n", got, expect);
    return;
  }
  SD.remove(CHECK_LIST);
  SD.rename(CHECK_DOWNLOAD, CHECK_LIST);
  Preferences prefs;
  prefs.begin("logger");
  prefs.putString("chklist", want.c_str());
  prefs.end();
  checkListReady = true;
  out.printf("[check] list for %s on the card (%d bytes)\n", want.c_str(), got);
}

// Wi-Fi and the check-ins (core 0), on a task of their own so an upload,
// a channel file or a firmware download never holds a check-in up (they
// came 17 s apart under load on 27 Oct).
static void beatTask(void*) {
  for (;;) {
    keepWifi();
    beatIfDue();
    vTaskDelay(pdMS_TO_TICKS(200));
  }
}

static void uploadTask(void*) {
  uint32_t last = 0;
  bool first = true;                       // the backlog at power-on, as soon as Wi-Fi is up
  if (sdOk) recoverDrives();               // the last drive's files, if the power cut it
  for (;;) {
    vTaskDelay(pdMS_TO_TICKS(500));
    channelsIfDue();
    otaCheckIfDue();
    checkListIfDue();
    if (!first && !uploadNow && millis() - last < UPLOAD_EVERY_MS) continue;
    last = millis();
    if (!sdOk || !WiFi.isConnected() || !strlen(WORKER_HOST)) continue;
    first = uploadNow = false;
    std::vector<String> names = pending();
    uploadsWaiting = names.size();
    for (const String& name : names) {
      if (!WiFi.isConnected() || !uploadOne(name)) break;
      uploadsWaiting = uploadsWaiting - 1;
    }
  }
}

// ---- Wi-Fi -------------------------------------------------------------------
static void onEvent(arduino_event_id_t e) {
  switch (e) {
    case ARDUINO_EVENT_ETH_CONNECTED:
      linkUp = true;
      out.printf("[eth] link up %d Mbit/s (%u ms after boot)\n", ETH.linkSpeed(),
                 (unsigned)millis());
      break;
    case ARDUINO_EVENT_ETH_DISCONNECTED:
      linkUp = false;
      out.println("[eth] link down");
      break;
    case ARDUINO_EVENT_WIFI_STA_GOT_IP:
      uploadNow = true;
      // Wi-Fi is the default route (the clock and uploads need the internet);
      // the car is reached on its own subnet, 169.254.0.0/16, over Ethernet.
      configTzTime(TZ_INFO, "pool.ntp.org", "time.google.com");
      out.printf("[wifi] %s, terminal on %s:%u\n", WiFi.SSID().c_str(),
                 WiFi.localIP().toString().c_str(), TERMINAL_PORT);
      break;
    default: break;
  }
}

// Set Wi-Fi up; the upload task joins in the background (keepWifi), so a
// boot logs within seconds instead of waiting for a network.
static void startWifi() {
  setenv("TZ", TZ_INFO, 1);
  tzset();
#if defined(WIFI_SSID) || defined(AP_PASS)
  terminal.begin();                          // answers on whichever network comes up
  terminal.setNoDelay(true);
#endif
#ifdef WIFI_SSID
  WiFi.setHostname(HOSTNAME);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(false);              // keepWifi decides which network
  wifiKnown = true;
#elif defined(AP_PASS)
  WiFi.mode(WIFI_AP);
  WiFi.softAP(HOSTNAME, AP_PASS);
#endif
}

// ---- terminal commands -------------------------------------------------------
static bool endRequested = false;

static void help() {
  out.println("s status  l files waiting to upload  u upload now  e end this drive  v view the files");
  out.println("C load channels.csv (send it, then a line with a dot)  X delete waiting files");
  out.println("T queue an upload test file  h help");
}

static void status() {
  static const char* names[] = {"waiting for the Ethernet link", "looking for the car", "logging"};
  out.printf("[status] %s", names[state]);
  if (state == LOGGING) {
    out.printf(" %s: %.1f min, %u rows (%.1f/s), %u sweep rows (pass %.1f s)", driveName.c_str(),
               (millis() - driveStartMs) / 60000.0f, (unsigned)fastRows, cycleRate,
               (unsigned)sweepRows, sweepPassMs / 1000.0f);
    out.printf("; per cycle: reads %.0f ms, rows and files %.0f ms; %u modules left alone",
               msFast, msWrite, (unsigned)backoff.modules(millis()).size());
  }
  out.println();
  size_t rested = 0;
  for (const Read& r : plan.reads) rested += r.restUntil != 0;
  std::string tag;
  {
    std::lock_guard<std::mutex> g(tagLock);
    tag = channelsTag;
  }
  out.printf("[status] channels %u fast, %u medium, %u slow, %u periodic (%s), %u reads, "
             "%u resting%s\n",
             (unsigned)table.count(FAST), (unsigned)table.count(MEDIUM), (unsigned)table.count(SLOW),
             (unsigned)periodicCount(), tag.c_str(), (unsigned)plan.reads.size(), (unsigned)rested,
             channelsDownloaded ? "; a new file is waiting for the next drive" : "");
  out.printf("[status] car %s%s%s, link %s\n", carIp.toString().c_str(), vin[0] ? " VIN " : "",
             vin, linkUp ? "up" : "down");
  int faults = faultsNow;
  if (faults < 0) out.println("[status] faults: not read yet (from a minute into a drive)");
  else out.printf("[status] faults: %d failed codes in the modules' last reads%s\n", faults,
                  faultScan.fullScanDone() ? "" : " (first pass still running)");
  String last;
  {
    std::lock_guard<std::recursive_mutex> g(sdLock);
    last = lastUpload;
  }
  out.printf("[status] clock %s, wifi %s %s, uploads to %s: %u done, last %s; %u check-ins\n",
             timeKnown() ? clockNow().c_str() : "unknown",
             WiFi.isConnected() ? WiFi.SSID().c_str() : "off",
             WiFi.isConnected() ? WiFi.localIP().toString().c_str() : "",
             strlen(WORKER_HOST) ? WORKER_HOST : "(none set)", (unsigned)uploadedFiles,
             last.c_str(), (unsigned)beatsSent);
  out.printf("[status] SD %s, %.1f of %.1f MB used; PSRAM %u KB free; internal %u KB free, "
             "largest block %u KB\n",
             sdOk ? "ok" : "MISSING", sdOk ? SD.usedBytes() / 1e6 : 0.0,
             sdOk ? SD.totalBytes() / 1e6 : 0.0, (unsigned)(ESP.getFreePsram() / 1024),
             (unsigned)(heap_caps_get_free_size(MALLOC_CAP_INTERNAL) / 1024),
             (unsigned)(heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL) / 1024));
  std::string ota;
  {
    std::lock_guard<std::mutex> g(tagLock);
    ota = otaStatus;
  }
  out.printf("[status] firmware %s%s%s%s\n", FW_VERSION, ota.empty() ? "" : ": ", ota.c_str(),
             strcmp(FW_VERSION, "dev") ? "" : " (an IDE build: takes no updates)");
  std::string want, done;
  {
    std::lock_guard<std::mutex> g(tagLock);
    want = checkWanted;
    done = checkDone;
  }
  if (!want.empty()) {
    out.printf("[status] full channel check %s: %s, %u of %u asked\n", want.c_str(),
               checkListReady ? "list on the card" : "list not downloaded yet",
               (unsigned)checkNextNow, (unsigned)checkTotal);
  } else if (!done.empty()) {
    out.printf("[status] full channel check %s done; the Worker hasn't heard yet\n", done.c_str());
  }
}

static void listPending() {
  std::vector<String> names = pending();
  out.printf("[files] %u waiting to upload\n", (unsigned)names.size());
  for (const String& n : names) {
    std::lock_guard<std::recursive_mutex> g(sdLock);
    File f = SD.open(String(DRIVES) + "/" + n);
    out.printf("  %s %u bytes\n", n.c_str(), f ? (unsigned)f.size() : 0);
    if (f) f.close();
  }
}

// Delete the files waiting to upload (bench runs, say), after a "y" within
// ten seconds. The drive being logged isn't touched.
// Queue a small file that isn't a drive log, to try the upload path: the
// Worker stores it (logger/upload-test-<n>.csv) and answers "not a drive
// log", so nothing reaches History. Then u sends it straight away.
static void uploadTest() {
  char name[48];
  snprintf(name, sizeof(name), "%s/upload-test-%u.csv", DRIVES, (unsigned)bootCount);
  std::string text = "note\nupload test from the m550 logger, boot " + std::to_string(bootCount) +
                     (timeKnown() ? ", " + clockNow() : std::string()) + "\n";
  Sha256 sha;
  sha.update(text.data(), text.size());
  char hex[65];
  sha.finishHex(hex);
  std::lock_guard<std::recursive_mutex> g(sdLock);
  File f = SD.open(name, FILE_WRITE);
  if (!f) { out.println("[up] can't write the test file"); return; }
  f.write((const uint8_t*)text.data(), text.size());
  f.close();
  File d = SD.open(String(name) + ".done", FILE_WRITE);
  if (d) { d.printf("%s %u\n", hex, (unsigned)text.size()); d.close(); }
  out.printf("[up] queued %s (%u bytes); u to send it now\n", name, (unsigned)text.size());
}

static void discardPending() {
  std::vector<String> names = pending();
  out.printf("[files] delete %u files waiting to upload? y to confirm\n", (unsigned)names.size());
  uint32_t t0 = millis();
  int k = -1;
  while (millis() - t0 < 10000 && (k = out.read()) < 0) delay(10);
  if (k != 'y') { out.println("[files] kept"); return; }
  std::lock_guard<std::recursive_mutex> g(sdLock);
  for (const String& n : names) {
    SD.remove(String(DRIVES) + "/" + n);
    SD.remove(String(DRIVES) + "/" + n + ".done");
  }
  out.printf("[files] deleted %u\n", (unsigned)names.size());
}

// A long line's start and end (the Event column, with the board's notes, is
// last).
static void showLine(const String& s) {
  if (s.length() <= 240) out.printf("  %s\n", s.c_str());
  else out.printf("  %s ... %s\n", s.substring(0, 140).c_str(), s.substring(s.length() - 220).c_str());
}

// The first and last lines of a file.
static void showFile(const String& path) {
  std::lock_guard<std::recursive_mutex> g(sdLock);
  File f = SD.open(path);
  if (!f) { out.printf("[view] %s: none\n", path.c_str()); return; }
  out.printf("[view] %s, %u bytes\n", path.c_str(), (unsigned)f.size());
  for (int i = 0; i < 3 && f.available(); i++) showLine(f.readStringUntil('\n'));
  if (f.size() > 2048) {
    f.seek(f.size() > 70000 ? f.size() - 70000 : 0);   // a sweep row is up to ~60 KB
    f.readStringUntil('\n');                 // to a line start
    String last, s;
    while (f.available()) { s = f.readStringUntil('\n'); if (s.length()) last = s; }
    out.println("  ...");
    showLine(last);
  }
  f.close();
}

static void view() {
  String base;
  if (state == LOGGING) {
    fastFile.flush();
    sweepFile.flush();
    base = String(DRIVES) + "/" + driveName;
  } else {
    std::vector<String> names = pending();
    if (names.empty()) { out.println("[view] no drive files"); return; }
    String n = names.back();
    base = String(DRIVES) + "/" + n.substring(0, n.indexOf('.'));
  }
  showFile(base + ".csv");
  showFile(base + ".sweep.csv");
}

// Take a new channels.csv over the terminal: its lines, then one with only
// a dot. It replaces the card's and applies from the next drive on.
static volatile bool reloadRequested = false;

static void receiveChannels() {
  out.println("[chan] send channels.csv, then a line with only a dot");
  std::lock_guard<std::recursive_mutex> g(sdLock);
  File f = SD.open("/channels.tmp", FILE_WRITE);
  if (!f) { out.println("[chan] can't write to the card"); return; }
  std::string line;
  uint32_t last = millis();
  size_t lines = 0;
  bool done = false;
  static uint8_t buf[1460];
  while (!done && millis() - last < 15000) {
    size_t n = out.readSome(buf, sizeof(buf));
    if (!n) { delay(1); continue; }
    last = millis();
    for (size_t i = 0; i < n && !done; i++) {
      char k = (char)buf[i];
      if (k == '\r') continue;
      if (k != '\n') { line += k; continue; }
      if (line == ".") done = true;
      else { f.write((const uint8_t*)line.data(), line.size()); f.write('\n'); lines++; }
      line.clear();
    }
  }
  f.close();
  if (!done) { SD.remove("/channels.tmp"); out.println("[chan] timed out; card unchanged"); return; }
  SD.remove(CHANNELS_FILE);
  SD.rename("/channels.tmp", CHANNELS_FILE);
  Preferences prefs;                         // until the cloud's active preset changes
  prefs.begin("logger");
  prefs.putString("chsrc", "manual");
  prefs.remove("chname");
  prefs.end();
  out.printf("[chan] %u lines saved\n", (unsigned)lines);
  reloadRequested = true;
}

static void serviceTerminal() {
  out.checkRemote();
  NetworkClient c = terminal.accept();
  if (c) {
    out.attach(c);
    out.println("\nm550 logger (read-only), Wi-Fi terminal");
    status();
    help();
  }
  out.flush();
  int k = out.read();
  switch (k) {
    case 's': status(); break;
    case 'l': listPending(); break;
    case 'u': uploadNow = true; out.println("[up] uploading now"); break;
    case 'e': endRequested = true; break;
    case 'v': view(); break;
    case 'C': receiveChannels(); break;
    case 'X': discardPending(); break;
    case 'T': uploadTest(); break;
    case 'h': help(); break;
    default: break;
  }
}

// ---- the connection closed mid-drive -------------------------------------------
// The gateway closed a connection (27 Oct: 0.2 s into each drive, then fine
// a second later, which left a stray two-row drive under one name and the
// real one under the next second's). Connect again and carry on in the same
// files, so a drive keeps one name; only if that fails for RECONNECT_MS (or
// the link goes) does the drive end.
static const uint32_t RECONNECT_MS = 15000;
static const uint32_t MAX_RECONNECTS = 20;   // a drive; past that it ends and starts again

static bool reconnect() {
  const uint32_t t0 = millis();
  if (++reconnects > MAX_RECONNECTS) return false;
  disconnectCar();
  resetRequests();                           // whatever was in flight went with it
  checkInFlight = false;
  while (millis() - t0 < RECONNECT_MS && linkUp) {
    if (connectOne(tMain, mainClient, hsfz::TESTER, 2000)) {
      if (needBank2() && !connectOne(tBank2, bank2Client, hsfz::TESTER_BANK2, 2000)) {
        out.println("[car] second connection refused on reconnecting; bank 2 skipped");
      }
      connectionLost = false;
      lastOkMs = millis();
      char text[64];
      snprintf(text, sizeof(text), "connection closed; reconnected in %.1f s",
               (millis() - t0) / 1000.0f);
      addNote(text);
      out.printf("[car] %s\n", text);
      return true;
    }
    delay(500);
  }
  return false;
}

// ---- setup and loop ----------------------------------------------------------
void setup() {
  Serial.begin(115200);
  // With the cable in a PC that isn't reading the port, each write would
  // wait 100 ms for room and the logging with it; drop the output instead.
  // No waiting for a USB host either: in the car there isn't one, and every
  // second of boot is a second not logged after an engine restart.
  Serial.setTxTimeoutMs(0);
  Preferences prefs;
  prefs.begin("logger");
  bootCount = prefs.getUInt("boots", 0) + 1;
  prefs.putUInt("boots", bootCount);
  prefs.end();
  out.printf("\nm550 logger (read-only), firmware %s, boot %u after %s, PSRAM %u KB\n", FW_VERSION,
             (unsigned)bootCount, resetReason(), (unsigned)(ESP.getPsramSize() / 1024));

  sntp_set_time_sync_notification_cb([](struct timeval*) { clockFromCar = false; });

  // Ethernet first: the link negotiates while the card and channels load.
  Network.onEvent(onEvent);
  if (ETH.begin(ETH_PHY_W5500, 1, ETH_CS, ETH_INT, ETH_RST, SPI2_HOST, ETH_SCLK, ETH_MISO,
                ETH_MOSI)) {
    ETH.config(MY_IP, NO_GW, MASK);
  } else {
    out.println("[eth] W5500 did not start");
  }

  sdSpi.begin(SD_SCK, SD_MISO, SD_MOSI, SD_CS);
  sdOk = SD.begin(SD_CS, sdSpi, 20000000);
  if (sdOk) {
    out.printf("[sd] %.1f GB card, %.1f MB used\n", SD.cardSize() / 1e9, SD.usedBytes() / 1e6);
    listLeftovers();
  } else {
    out.println("[sd] NO CARD (or not FAT): logging needs one");
  }
  otaBoot();
  if (otaReady) otaInstall();                // a new build on the card: in before the car
  checkBoot();
  loadChannels();
  startWifi();
  xTaskCreatePinnedToCore(beatTask, "beat", 10240, nullptr, 2, nullptr, 0);
  xTaskCreatePinnedToCore(uploadTask, "upload", 12288, nullptr, 1, nullptr, 0);
  out.printf("[boot] ready in %u ms\n", (unsigned)millis());
  help();
}

void loop() {
  serviceTerminal();
  // Firmware: a build on trial goes back if it hasn't proved itself in time;
  // one waiting on the card goes in once the board has been idle a minute.
  static uint32_t idleSince = 0;
  otaTrialCheck();
  if (state == LOGGING) idleSince = millis();
  // Not while files wait to go up, which may be the last minute of power
  // after a drive (OTA_IDLE_MAX_MS: unless that's gone on and on).
  else if (otaReady && millis() - idleSince >= OTA_IDLE_MS &&
           (!uploadsWaiting || millis() - idleSince >= OTA_IDLE_MAX_MS)) {
    otaInstall();
  }
  switch (state) {
    case WAIT_LINK:
      if (linkUp) state = SEARCH;
      else delay(50);
      break;
    case SEARCH: {
      static uint32_t lastTry = 0;
      static bool tried = false;
      if (!linkUp) { state = WAIT_LINK; break; }
      if (!sdOk || (tried && millis() - lastTry < SEARCH_MS)) { delay(50); break; }
      lastTry = millis();
      tried = true;
      // The gateway keeps its link-local address from drive to drive: try the
      // last one first, then ask (discovery reaches it by the subnet broadcast).
      bool connected = rememberCar(false) && connectOne(tMain, mainClient, hsfz::TESTER, 1000);
      if (!connected && !(discover() && connectOne(tMain, mainClient, hsfz::TESTER, 3000))) break;
      if (needBank2() && !connectOne(tBank2, bank2Client, hsfz::TESTER_BANK2, 3000)) {
        out.println("[car] second connection refused (another tool holds it?); bank 2 skipped");
      }
      uint8_t tp[] = {0x3E, 0x00};
      if (mainClient->request(hsfz::DME_MASTER, tp, 2, rx, sizeof(rx), REQUEST_MS).result !=
          hsfz::OK) {
        disconnectCar();
        break;
      }
      out.printf("[car] %s%s%s (%u ms after boot)\n", carIp.toString().c_str(),
                 vin[0] ? " VIN " : "", vin, (unsigned)millis());
      rememberCar(true);
      connectionLost = false;
      endRequested = false;
      startDrive();
      state = LOGGING;
      break;
    }
    case LOGGING:
      cycle();
      readFaults();
      readCheck();
      if (endRequested) { endDrive("ended from the terminal"); state = SEARCH; }
      else if (connectionLost && !reconnect()) { endDrive("connection closed"); state = SEARCH; }
      else if (millis() - lastOkMs > CAR_GONE_MS) { endDrive("the car stopped answering"); state = SEARCH; }
      else if (!linkUp) { endDrive("Ethernet link lost"); state = WAIT_LINK; }
      break;
  }
  if (reloadRequested) {
    reloadRequested = false;
    if (state == LOGGING) { endDrive("new channels"); state = SEARCH; }
    channelsFromCard = false;
    loadChannels();
  }
  if (channelsDownloaded && state != LOGGING) {   // from the cloud: between drives
    channelsDownloaded = false;
    channelsFromCard = false;
    loadChannels();
  }
}
