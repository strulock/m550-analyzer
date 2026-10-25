// m550 logger: logs the car to the SD card and uploads each drive to the
// cloud Worker, which keeps it with the app's logs (History, the series, MCP).
//
// Board: Waveshare ESP32-S3-ETH on the car's ENET cable, powered with the
// ignition. Each boot:
//   1. Recover: a drive the last power cut left open is closed and queued.
//   2. Find the car (HSFZ discovery) and log while it answers: the fast
//      channels every cycle to drives/<start>.csv, everything else in
//      rotation, one row a pass, to drives/<start>.sweep.csv.
//   3. Meanwhile, on the other core: whenever Wi-Fi is up, upload finished
//      files, compare the SHA-256 the Worker reports with the board's own,
//      and delete a file only when they match.
//
// What to read comes from channels.csv on the card (n63read logger-channels),
// else the built-in fast tier. Read-only: the HSFZ client refuses anything
// but UDS 0x22 and 0x3E.
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
#include <WiFi.h>
#include <errno.h>
#include <esp_heap_caps.h>
#include <lwip/sockets.h>
#include <sys/time.h>
#include <time.h>

#include <algorithm>
#include <atomic>
#include <mutex>
#include <string>
#include <vector>

#include "channels.h"
#include "csvlog.h"
#include "default_channels.h"
#include "hsfz.h"
#include "plan.h"
#include "sha256.h"

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
static const uint32_t SEARCH_MS = 3000;      // between discovery tries
static const uint32_t FLUSH_MS = 2000;       // a power cut loses at most this much
static const uint32_t SWEEP_MS = 25;         // sweep time per cycle, after the fast reads
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

// The table and plan make thousands of small allocations (a vector per read,
// strings while parsing). Left in internal RAM they break it into pieces too
// small for a TLS handshake (seen: 123 KB free, largest block 17 KB, uploads
// failing "SSL - Memory allocation failed"), so while loading every
// allocation goes to PSRAM.
static void loadChannelsInto();

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
    while (ok && f.available()) {
      int c = f.read();
      if (c == '\n' || c < 0) { ok = table.line(line.c_str()); line.clear(); }
      else line += (char)c;
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
  out.printf("[chan] %u channels (%u fast, %u sweep) in %u reads, from %s%s\n",
             (unsigned)table.channels.size(), (unsigned)table.count(true),
             (unsigned)table.count(false), (unsigned)plan.reads.size(),
             channelsFromCard ? "the card" : "the built-in table",
             table.skipped ? " (some rows skipped)" : "");
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

static DriveFile fastFile, sweepFile;
static String driveName;
static uint32_t driveStartMs = 0, lastOkMs = 0, lastFlushMs = 0, lastCycleMs = 0;
static uint32_t fastRows = 0, sweepRows = 0, cycles = 0, cycleStatsMs = 0;
static float cycleRate = 0;
static uint32_t tFast = 0, tSweep = 0, tWrite = 0;     // ms spent per stats window
static float msFast = 0, msSweep = 0, msWrite = 0;     // per cycle, last window
static uint32_t sweepPassMs = 0, sweepPassStart = 0;
static size_t sweepAt = 0;
static std::vector<double> values;
static LogFile fastLayout, sweepLayout;

// A file left without a .done (the power went mid-drive): hash what's there
// and queue it like any other.
static void recoverDrives() {
  std::lock_guard<std::recursive_mutex> g(sdLock);
  if (!SD.exists(DRIVES)) SD.mkdir(DRIVES);
  File dir = SD.open(DRIVES);
  std::vector<String> open;
  for (File f = dir.openNextFile(); f; f = dir.openNextFile()) {
    String name = f.name();
    f.close();
    if (name.endsWith(".csv") && !SD.exists(String(DRIVES) + "/" + name + ".done")) {
      open.push_back(name);
    }
  }
  dir.close();
  static uint8_t buf[4096];
  for (const String& name : open) {
    String path = String(DRIVES) + "/" + name;
    File f = SD.open(path);
    Sha256 sha;
    uint32_t size = 0;
    for (int n; (n = f.read(buf, sizeof(buf))) > 0;) { sha.update(buf, n); size += n; }
    f.close();
    char hex[65];
    sha.finishHex(hex);
    File d = SD.open(path + ".done", FILE_WRITE);
    if (d) { d.printf("%s %u\n", hex, (unsigned)size); d.close(); }
    out.printf("[sd] %s was left open (power cut); closed at %u bytes\n", name.c_str(),
               (unsigned)size);
  }
}

static void startDrive() {
  // A drive started at home usually has Wi-Fi: give the clock a moment.
  for (int i = 0; i < 50 && WiFi.isConnected() && !timeKnown(); i++) delay(100);
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
  driveStartMs = lastOkMs = lastFlushMs = lastCycleMs = cycleStatsMs = millis();
  fastRows = sweepRows = cycles = 0;
  sweepAt = plan.sweepStart();
  sweepPassStart = millis();
  out.printf("[drive] %s started%s%s\n", driveName.c_str(), timeKnown() ? "" : " (clock unknown)",
             ok ? "" : ": FILES COULD NOT BE OPENED");
}

static void endDrive(const char* why) {
  fastFile.finish();
  sweepFile.finish();
  disconnectCar();
  out.printf("[drive] %s ended (%s): %u rows, %u sweep rows, %.1f min\n", driveName.c_str(), why,
             (unsigned)fastRows, (unsigned)sweepRows, (millis() - driveStartMs) / 60000.0f);
  driveName = "";
}

// ---- one request -------------------------------------------------------------
static bool connectionLost = false;

static void ask(const std::vector<size_t>& batch) {
  const Read& first = plan.reads[batch[0]];
  hsfz::Client* cl = first.module == hsfz::DME_SLAVE ? bank2Client : mainClient;
  if (!cl) { for (size_t i : batch) plan.failed(i, millis()); return; }
  uint8_t req[1 + 2 * MAX_BATCH];
  req[0] = 0x22;
  for (size_t k = 0; k < batch.size(); k++) {
    req[1 + 2 * k] = plan.reads[batch[k]].did >> 8;
    req[2 + 2 * k] = plan.reads[batch[k]].did & 0xFF;
  }
  hsfz::Reply r = cl->request(first.module, req, 1 + 2 * batch.size(), rx, sizeof(rx), REQUEST_MS);
  const uint32_t now = millis();
  if (r.result == hsfz::CLOSED) { connectionLost = true; return; }
  std::vector<const uint8_t*> data;
  std::vector<size_t> len;
  if (r.result == hsfz::OK && splitReply(rx + 1, r.len - 1, plan.reads, batch, data, len)) {
    lastOkMs = now;
    for (size_t k = 0; k < batch.size(); k++) {
      if (!data[k]) { plan.failed(batch[k], now); continue; }
      plan.answered(batch[k]);
      for (uint32_t ci : plan.reads[batch[k]].channels) {
        double v;
        if (decode(table.channels[ci], data[k], len[k], v)) values[ci] = v;
      }
    }
    return;
  }
  if (r.result == hsfz::OK) lastOkMs = now;       // it answered, just not in line
  if (batch.size() > 1) {
    // The module can't take this batch: one identifier at a time from now on.
    plan.noBatches(first.module);
    out.printf("[plan] module 0x%02X: one identifier per request (%s)\n", first.module,
               r.result == hsfz::OK ? "reply out of line" : hsfz::resultName(r.result));
    return;
  }
  plan.failed(batch[0], now);
}

// ---- the logging cycle -------------------------------------------------------
static void cycle() {
  const uint32_t start = millis();
  for (uint32_t i : fastLayout.columns) values[i] = NAN;
  size_t at = 0;
  for (std::vector<size_t> b; !connectionLost && !(b = plan.next(true, at, millis())).empty();) {
    ask(b);
  }
  const uint32_t afterFast = millis();
  tFast += afterFast - start;
  const double secs = (millis() - driveStartMs) / 1000.0;
  if (!fastLayout.columns.empty()) {
    fastFile.add(fastLayout.row(table, secs, clockNow(), values,
                                fastRows == 0 ? (std::string("start VIN ") + vin).c_str() : ""));
    fastRows++;
  }
  // The sweep, a few requests a cycle; a finished pass is one row.
  const uint32_t sweepFrom = millis();
  tWrite += sweepFrom - afterFast;
  while (!connectionLost && !sweepLayout.columns.empty() && millis() - sweepFrom < SWEEP_MS) {
    std::vector<size_t> b = plan.next(false, sweepAt, millis());
    if (b.empty()) {
      sweepFile.add(sweepLayout.row(table, (millis() - driveStartMs) / 1000.0, clockNow(), values));
      sweepRows++;
      for (uint32_t i : sweepLayout.columns) values[i] = NAN;
      sweepPassMs = millis() - sweepPassStart;
      sweepPassStart = millis();
      sweepAt = plan.sweepStart();
      break;
    }
    ask(b);
  }
  const uint32_t flushFrom = millis();
  tSweep += flushFrom - sweepFrom;
  if (millis() - lastFlushMs >= FLUSH_MS) {
    fastFile.flush();
    sweepFile.flush();
    lastFlushMs = millis();
  }
  tWrite += millis() - flushFrom;
  cycles++;
  if (millis() - cycleStatsMs >= 5000) {
    cycleRate = cycles * 1000.0f / (millis() - cycleStatsMs);
    msFast = (float)tFast / cycles; msSweep = (float)tSweep / cycles; msWrite = (float)tWrite / cycles;
    tFast = tSweep = tWrite = 0;
    cycles = 0;
    cycleStatsMs = millis();
  }
  uint32_t spent = millis() - start;
  if (spent < MIN_CYCLE_MS) delay(MIN_CYCLE_MS - spent);
}

// ---- uploads (core 0) --------------------------------------------------------
static volatile bool uploadNow = false;
static std::atomic<uint32_t> uploadedFiles{0};
static String lastUpload = "none yet";     // under sdLock: the upload task sets it

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
  char sha[65] = "";
  unsigned size = 0;
  {
    std::lock_guard<std::recursive_mutex> g(sdLock);
    File d = SD.open(path + ".done");
    if (d) {
      String s = d.readStringUntil('\n');
      d.close();
      sscanf(s.c_str(), "%64s %u", sha, &size);
    }
  }
  if (strlen(sha) != 64) { out.printf("[up] %s: bad .done file\n", name.c_str()); return false; }
  File f;
  {
    std::lock_guard<std::recursive_mutex> g(sdLock);
    f = SD.open(path);
  }
  if (!f) { out.printf("[up] %s: missing\n", name.c_str()); return false; }
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
    int code = http.sendRequest("PUT", &f, f.size());
    String body = code > 0 ? http.getString() : String();
    http.end();
    String want = String("\"sha256\":\"") + sha + "\"";
    ok = code == 200 && body.indexOf(want) >= 0;
    if (ok) {
      int p = body.indexOf("\"processed\":");
      out.printf("[up] %s: %u bytes in %.1f s, checked%s\n", name.c_str(), size,
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
static const uint32_t WIFI_RETRY_MS = 30000;       // off both: try again
static const uint32_t WIFI_HOME_CHECK_MS = 120000; // on the backup: look for home

static bool join(const char* ssid, const char* pass, uint32_t ms) {
  WiFi.begin(ssid, pass);
  uint32_t t0 = millis();
  while (!WiFi.isConnected() && millis() - t0 < ms) delay(100);
  return WiFi.isConnected();
}

static bool joinPreferred() {
#ifdef WIFI_SSID
  if (join(WIFI_SSID, WIFI_PASS, 8000)) return true;
#ifdef WIFI_SSID2
  WiFi.disconnect();
  if (join(WIFI_SSID2, WIFI_PASS2, 8000)) return true;
#endif
#endif
  return false;
}

// Keep a network up, home first (from this task, between uploads, so the
// logging on the other core never waits for a scan or a join).
static void keepWifi() {
  static uint32_t lastTry = 0, lastHomeCheck = 0;
  static bool mdns = false;
  if (!wifiKnown) return;
  if (!WiFi.isConnected()) {
    if (lastTry && millis() - lastTry < WIFI_RETRY_MS) return;
    lastTry = millis();
    if (joinPreferred() && !mdns) mdns = MDNS.begin(HOSTNAME);
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

static void uploadTask(void*) {
  uint32_t last = 0;
  for (;;) {
    vTaskDelay(pdMS_TO_TICKS(500));
    keepWifi();
    if (!uploadNow && millis() - last < UPLOAD_EVERY_MS) continue;
    uploadNow = false;
    last = millis();
    if (!sdOk || !WiFi.isConnected() || !strlen(WORKER_HOST)) continue;
    for (const String& name : pending()) {
      if (!WiFi.isConnected() || !uploadOne(name)) break;
    }
  }
}

// ---- Wi-Fi -------------------------------------------------------------------
static void onEvent(arduino_event_id_t e) {
  switch (e) {
    case ARDUINO_EVENT_ETH_CONNECTED:
      linkUp = true;
      out.printf("[eth] link up %d Mbit/s\n", ETH.linkSpeed());
      break;
    case ARDUINO_EVENT_ETH_DISCONNECTED:
      linkUp = false;
      out.println("[eth] link down");
      break;
    case ARDUINO_EVENT_WIFI_STA_GOT_IP:
      // Wi-Fi is the default route (the clock and uploads need the internet);
      // the car is reached on its own subnet, 169.254.0.0/16, over Ethernet.
      configTzTime(TZ_INFO, "pool.ntp.org", "time.google.com");
      out.printf("[wifi] %s, terminal on %s:%u\n", WiFi.SSID().c_str(),
                 WiFi.localIP().toString().c_str(), TERMINAL_PORT);
      break;
    default: break;
  }
}

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
  if (joinPreferred()) {
    MDNS.begin(HOSTNAME);
    return;
  }
  out.println("[wifi] no known network in range; logging only, uploads when one is");
#endif
#ifdef AP_PASS
  WiFi.mode(WIFI_AP_STA);                    // keep trying the networks for uploads
  WiFi.softAP(HOSTNAME, AP_PASS);
  out.printf("[wifi] access point %s, terminal on %s:%u\n", HOSTNAME,
             WiFi.softAPIP().toString().c_str(), TERMINAL_PORT);
#endif
}

// ---- terminal commands -------------------------------------------------------
enum State { WAIT_LINK, SEARCH, LOGGING };
static State state = WAIT_LINK;
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
    out.printf("; per cycle: fast reads %.0f ms, sweep %.0f ms, rows and files %.0f ms",
               msFast, msSweep, msWrite);
  }
  out.println();
  size_t rested = 0;
  for (const Read& r : plan.reads) rested += r.restUntil != 0;
  out.printf("[status] channels %u fast, %u sweep (%s), %u reads, %u resting\n",
             (unsigned)table.count(true), (unsigned)table.count(false),
             channelsFromCard ? "card" : "built in", (unsigned)plan.reads.size(), (unsigned)rested);
  out.printf("[status] car %s%s%s, link %s\n", carIp.toString().c_str(), vin[0] ? " VIN " : "",
             vin, linkUp ? "up" : "down");
  String last;
  {
    std::lock_guard<std::recursive_mutex> g(sdLock);
    last = lastUpload;
  }
  out.printf("[status] clock %s, wifi %s %s, uploads to %s: %u done, last %s\n",
             timeKnown() ? clockNow().c_str() : "unknown",
             WiFi.isConnected() ? WiFi.SSID().c_str() : "off",
             WiFi.isConnected() ? WiFi.localIP().toString().c_str() : "",
             strlen(WORKER_HOST) ? WORKER_HOST : "(none set)", (unsigned)uploadedFiles,
             last.c_str());
  out.printf("[status] SD %s, %.1f of %.1f MB used; PSRAM %u KB free; internal %u KB free, "
             "largest block %u KB\n",
             sdOk ? "ok" : "MISSING", sdOk ? SD.usedBytes() / 1e6 : 0.0,
             sdOk ? SD.totalBytes() / 1e6 : 0.0, (unsigned)(ESP.getFreePsram() / 1024),
             (unsigned)(heap_caps_get_free_size(MALLOC_CAP_INTERNAL) / 1024),
             (unsigned)(heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL) / 1024));
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

// The first and last lines of a file, each cut to 240 characters.
static void showFile(const String& path) {
  std::lock_guard<std::recursive_mutex> g(sdLock);
  File f = SD.open(path);
  if (!f) { out.printf("[view] %s: none\n", path.c_str()); return; }
  out.printf("[view] %s, %u bytes\n", path.c_str(), (unsigned)f.size());
  auto line = [&](size_t max) {
    String s = f.readStringUntil('\n');
    out.printf("  %s%s\n", s.substring(0, max).c_str(), s.length() > max ? " ..." : "");
  };
  for (int i = 0; i < 3 && f.available(); i++) line(240);
  if (f.size() > 2048) {
    f.seek(f.size() - 1024);
    f.readStringUntil('\n');                 // to a line start
    String last, s;
    while (f.available()) { s = f.readStringUntil('\n'); if (s.length()) last = s; }
    out.printf("  ...\n  %s%s\n", last.substring(0, 240).c_str(), last.length() > 240 ? " ..." : "");
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

// ---- setup and loop ----------------------------------------------------------
void setup() {
  Serial.begin(115200);
  // With the cable in a PC that isn't reading the port, each write would
  // wait 100 ms for room and the logging with it; drop the output instead.
  Serial.setTxTimeoutMs(0);
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 3000) delay(10);
  Preferences prefs;
  prefs.begin("logger");
  bootCount = prefs.getUInt("boots", 0) + 1;
  prefs.putUInt("boots", bootCount);
  prefs.end();
  out.printf("\nm550 logger (read-only), boot %u, PSRAM %u KB\n", (unsigned)bootCount,
             (unsigned)(ESP.getPsramSize() / 1024));

  sdSpi.begin(SD_SCK, SD_MISO, SD_MOSI, SD_CS);
  sdOk = SD.begin(SD_CS, sdSpi, 20000000);
  if (sdOk) {
    out.printf("[sd] %.1f GB card, %.1f MB used\n", SD.cardSize() / 1e9, SD.usedBytes() / 1e6);
    recoverDrives();
  } else {
    out.println("[sd] NO CARD (or not FAT): logging needs one");
  }
  loadChannels();

  Network.onEvent(onEvent);
  if (ETH.begin(ETH_PHY_W5500, 1, ETH_CS, ETH_INT, ETH_RST, SPI2_HOST, ETH_SCLK, ETH_MISO,
                ETH_MOSI)) {
    ETH.config(MY_IP, NO_GW, MASK);
  } else {
    out.println("[eth] W5500 did not start");
  }
  startWifi();
  xTaskCreatePinnedToCore(uploadTask, "upload", 12288, nullptr, 1, nullptr, 0);
  help();
}

void loop() {
  serviceTerminal();
  switch (state) {
    case WAIT_LINK:
      if (linkUp) state = SEARCH;
      else delay(50);
      break;
    case SEARCH: {
      static uint32_t lastTry = 0;
      if (!linkUp) { state = WAIT_LINK; break; }
      if (!sdOk || millis() - lastTry < SEARCH_MS) { delay(50); break; }
      lastTry = millis();
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
      out.printf("[car] %s%s%s\n", carIp.toString().c_str(), vin[0] ? " VIN " : "", vin);
      rememberCar(true);
      connectionLost = false;
      endRequested = false;
      startDrive();
      state = LOGGING;
      break;
    }
    case LOGGING:
      cycle();
      if (endRequested) { endDrive("ended from the terminal"); state = SEARCH; }
      else if (connectionLost) { endDrive("connection closed"); state = SEARCH; }
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
}
