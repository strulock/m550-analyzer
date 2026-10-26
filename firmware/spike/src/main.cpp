// m550 HSFZ spike: does a W5500 on an ESP32-S3 talk to the M550i's DMEs?
//
// Board: Waveshare ESP32-S3-ETH. Plug the car's ENET cable into its RJ45,
// USB-C to the laptop, ignition on, then open the serial monitor (115200).
// It runs every test once at boot; type a letter to run one again:
//
//   a  run everything again
//   s  status: link, addresses, open connections
//   d  discover the car (UDP 6811)
//   r  read test channels once from 0x12 and 0x13
//   b  batch rate test, 20 s
//   x  close the car connections (do this before unplugging)
//   h  help
//
// The same terminal is on Wi-Fi, TCP port 23, so the board can sit in the car
// on a USB power supply: see wifi_secrets.example.h.
//
// Read-only: the HSFZ client refuses anything but UDS 0x22, 0x3E and 0x19 02
// (the fault list; the spike sends only the first two).

#include <Arduino.h>
#include <ESPmDNS.h>
#include <ETH.h>
#include <NetworkClient.h>
#include <NetworkServer.h>
#include <NetworkUdp.h>
#include <SPI.h>
#include <WiFi.h>

#include <mutex>

#include "hsfz.h"

#if __has_include("wifi_secrets.h")
#include "wifi_secrets.h"
#endif

// ---- Waveshare ESP32-S3-ETH pins (Waveshare wiki) ---------------------------
static const int ETH_MISO = 12, ETH_MOSI = 11, ETH_SCLK = 13;
static const int ETH_CS = 14, ETH_INT = 10, ETH_RST = 9;

// Our address on the car's link-local network. The car's gateway picks its own
// 169.254.x.y; anything /16 that isn't the car's works.
static const IPAddress MY_IP(169, 254, 100, 100);
static const IPAddress MASK(255, 255, 0, 0);
static const IPAddress NO_GW(0, 0, 0, 0);

// Set to the car's address to skip discovery, e.g. IPAddress(169,254,x,y).
static IPAddress CAR_IP_OVERRIDE(0, 0, 0, 0);

// Test channels. All 2-byte, so the batch test can split replies exactly.
static const hsfz::Channel CH[] = {
    {0x4807, "Engine speed", 2, true, 1.0f, 2.0f, 0.0f, "rpm"},
    {0x4A21, "Coolant temp (filtered)", 2, true, 0.1f, 1.0f, -273.14f, "C"},
    {0x460C, "Battery voltage (ADC)", 2, false, 0.023481f, 1.0f, 0.0f, "V"},
    {0x4402, "Oil temp after filter", 2, false, 0.75f, 1.0f, -48.0f, "C"},
    {0x4AB1, "Vehicle speed", 2, true, 0.01f, 1.0f, 0.0f, "km/h"},
    // Rail pressure 0x5621 dropped: both DMEs answer NRC 0x31 in the car.
    {0x4201, "Ambient pressure", 2, false, 10.0f, 256.0f, 0.0f, "hPa"},
    {0x5545, "Accelerator pedal", 2, false, 0.025f, 1.0f, 0.0f, "%"},
};
static const size_t NCH = sizeof(CH) / sizeof(CH[0]);

// ---- console: USB serial plus one Wi-Fi terminal ----------------------------
static const char* HOSTNAME = "m550-spike";
static const uint16_t TERMINAL_PORT = 23;

// Wi-Fi output goes a line at a time: one packet per printf fragment (two
// bytes each in hexdump) fills the send queue and stalls the tests.
// Network events print from another task, hence the lock.
// The last 8 KB is kept and replayed to each new terminal, so the boot run
// and anything lost to a dropped connection can still be read.
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
    if (used && remote.connected() && remote.write(line, used) != used) remote.stop();
    used = 0;
  }
  int read() {
    std::lock_guard<std::recursive_mutex> g(lock);
    if (Serial.available()) return Serial.read();
    if (remote.connected() && remote.available()) return remote.read();
    return -1;
  }
  void attach(NetworkClient& c) {
    std::lock_guard<std::recursive_mutex> g(lock);
    if (remote.connected()) remote.stop();
    used = 0;
    remote = c;
    remote.setNoDelay(true);
    static const char start[] = "---- recent output ----\n", end[] = "---- live ----\n";
    remote.write((const uint8_t*)start, sizeof(start) - 1);
    if (wrapped) remote.write(hist + head, sizeof(hist) - head);
    remote.write(hist, head);
    remote.write((const uint8_t*)end, sizeof(end) - 1);
  }
};

static Console out;
static NetworkServer terminal(TERMINAL_PORT);

// ---- transport over an Arduino NetworkClient --------------------------------
struct TcpTransport : hsfz::Transport {
  NetworkClient c;
  bool write(const uint8_t* d, size_t n) override {
    return c.connected() && c.write(d, n) == n;
  }
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
static hsfz::Client* main12 = nullptr;
static hsfz::Client* bank2 = nullptr;
static IPAddress carIp;
static volatile bool linkUp = false;
static uint8_t rx[512];

static void hexdump(const uint8_t* p, size_t n) {
  for (size_t i = 0; i < n; i++) out.printf("%02X", p[i]);
}

static void onEvent(arduino_event_id_t e) {
  switch (e) {
    case ARDUINO_EVENT_ETH_START: out.println("[eth] started"); break;
    case ARDUINO_EVENT_ETH_CONNECTED:
      linkUp = true;
      out.printf("[eth] LINK UP %d Mbit/s %s duplex\n", ETH.linkSpeed(),
                 ETH.fullDuplex() ? "full" : "half");
      break;
    case ARDUINO_EVENT_ETH_DISCONNECTED:
      linkUp = false;
      out.println("[eth] link down");
      break;
    case ARDUINO_EVENT_WIFI_STA_GOT_IP:
      // Wi-Fi would otherwise take the default route, and the discovery
      // broadcast to 255.255.255.255 with it.
      ETH.setDefault();
      out.printf("[wifi] %s, terminal on %s:%u (%s.local)\n", WiFi.SSID().c_str(),
                 WiFi.localIP().toString().c_str(), TERMINAL_PORT, HOSTNAME);
      break;
    default: break;
  }
}

// Join WIFI_SSID if it's in range, else start our own access point with
// AP_PASS. Neither defined: no Wi-Fi, USB only.
static void startWifi() {
#if defined(WIFI_SSID) || defined(AP_PASS)
  WiFi.setHostname(HOSTNAME);
#endif
#ifdef WIFI_SSID
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // power save holds every ACK for a beacon: terminal crawls
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  out.printf("[wifi] joining %s\n", WIFI_SSID);
  uint32_t t0 = millis();
  while (!WiFi.isConnected() && millis() - t0 < 10000) delay(100);
  if (WiFi.isConnected()) {
    MDNS.begin(HOSTNAME);
    terminal.begin();
    terminal.setNoDelay(true);
    return;
  }
  out.printf("[wifi] %s not in range\n", WIFI_SSID);
  WiFi.disconnect(true);
#endif
#ifdef AP_PASS
  WiFi.mode(WIFI_AP);
  WiFi.softAP(HOSTNAME, AP_PASS);
  terminal.begin();
  terminal.setNoDelay(true);
  out.printf("[wifi] access point %s, terminal on %s:%u\n", HOSTNAME,
             WiFi.softAPIP().toString().c_str(), TERMINAL_PORT);
#elif !defined(WIFI_SSID)
  out.println("[wifi] off: no src/wifi_secrets.h");
#endif
}

// Take a new terminal connection, replacing any older one.
static bool acceptTerminal() {
  NetworkClient c = terminal.accept();
  if (!c) return false;
  out.attach(c);
  out.println("\nm550 HSFZ spike (read-only), Wi-Fi terminal");
  return true;
}

// ---- tests ------------------------------------------------------------------
static bool discover() {
  if (CAR_IP_OVERRIDE != IPAddress(0, 0, 0, 0)) {
    carIp = CAR_IP_OVERRIDE;
    out.printf("[disc] using fixed car address %s\n", carIp.toString().c_str());
    return true;
  }
  NetworkUDP udp;
  if (!udp.begin(MY_IP, 0)) { out.println("[disc] udp.begin failed"); return false; }
  const IPAddress dests[] = {IPAddress(169, 254, 255, 255), IPAddress(255, 255, 255, 255)};
  for (const IPAddress& d : dests) {
    udp.beginPacket(d, hsfz::CONTROL_PORT);
    udp.write(hsfz::IDENT_PROBE_0, sizeof(hsfz::IDENT_PROBE_0));
    udp.endPacket();
    udp.beginPacket(d, hsfz::CONTROL_PORT);
    udp.write(hsfz::IDENT_PROBE_2, sizeof(hsfz::IDENT_PROBE_2));
    udp.endPacket();
  }
  out.println("[disc] probes sent to 6811, listening 3 s");
  bool found = false;
  uint32_t end = millis() + 3000;
  while ((int32_t)(end - millis()) > 0) {
    int n = udp.parsePacket();
    if (n <= 0) { delay(5); continue; }
    int k = udp.read(rx, sizeof(rx));
    if (k <= 0) continue;
    IPAddress from = udp.remoteIP();
    if (!hsfz::isIdentReply(rx, k)) {
      if (from != MY_IP) out.printf("[disc] ignored %d bytes from %s\n", k, from.toString().c_str());
      continue;
    }
    char vin[18];
    out.printf("[disc] HSFZ reply from %s, %d bytes", from.toString().c_str(), k);
    if (hsfz::vinFromIdent(rx, k, vin)) out.printf(", VIN %s", vin);
    out.println();
    out.print("[disc] raw: "); hexdump(rx, k); out.println();
    if (!found) { carIp = from; found = true; }
  }
  udp.stop();
  if (!found) out.println("[disc] FAIL: no HSFZ reply. Ignition on? Link up?");
  return found;
}

static bool openConn(TcpTransport& t, hsfz::Client*& cl, uint8_t source) {
  if (cl && t.c.connected()) return true;
  t.c.stop();
  uint32_t t0 = millis();
  if (!t.c.connect(carIp, hsfz::DIAG_PORT, 3000)) {
    out.printf("[tcp] connect to %s:6801 as 0x%02X FAILED\n", carIp.toString().c_str(), source);
    return false;
  }
  t.c.setNoDelay(true);
  delete cl;
  cl = new hsfz::Client(t, source);
  out.printf("[tcp] connected as tester 0x%02X in %lu ms\n", source, millis() - t0);
  return true;
}

static void closeAll() {
  tMain.c.stop();
  tBank2.c.stop();
  delete main12; main12 = nullptr;
  delete bank2; bank2 = nullptr;
  out.println("[tcp] car connections closed");
}

static void report(const char* what, const hsfz::Reply& r) {
  out.printf("  %s: %s", what, hsfz::resultName(r.result));
  if (r.result == hsfz::NEGATIVE) out.printf(" NRC 0x%02X", r.nrc);
  if (r.result == hsfz::NACK) out.printf(" 0x%04X (%s)", r.nack, hsfz::nackText(r.nack));
  out.println();
}

static void readOnce(hsfz::Client& cl, uint8_t target) {
  uint8_t tp[] = {0x3E, 0x00};
  uint32_t t0 = millis();
  hsfz::Reply r = cl.request(target, tp, sizeof(tp), rx, sizeof(rx));
  out.printf("[0x%02X] tester present: %s (%lu ms)\n", target,
             hsfz::resultName(r.result), millis() - t0);
  for (size_t i = 0; i < NCH; i++) {
    uint8_t req[] = {0x22, (uint8_t)(CH[i].did >> 8), (uint8_t)CH[i].did};
    t0 = millis();
    r = cl.request(target, req, sizeof(req), rx, sizeof(rx));
    uint32_t ms = millis() - t0;
    if (r.result == hsfz::OK && r.len >= 3 + CH[i].width) {
      out.printf("[0x%02X] 0x%04X %-26s %10.2f %-5s raw ", target, CH[i].did, CH[i].name,
                 hsfz::decode(CH[i], rx + 3), CH[i].unit);
      hexdump(rx + 3, r.len - 3);
      out.printf("  (%lu ms)\n", ms);
    } else {
      char what[48];
      snprintf(what, sizeof(what), "[0x%02X] 0x%04X %s", target, CH[i].did, CH[i].name);
      report(what, r);
    }
  }
}

static void readTest() {
  if (!openConn(tMain, main12, hsfz::TESTER)) return;
  readOnce(*main12, hsfz::DME_MASTER);
  out.println("[bank2] opening the second connection for the slave DME");
  if (!openConn(tBank2, bank2, hsfz::TESTER_BANK2)) return;
  readOnce(*bank2, hsfz::DME_SLAVE);
}

// One 8-identifier request per DME per cycle, for 20 s.
static void batchTest() {
  if (!openConn(tMain, main12, hsfz::TESTER)) return;
  bool useBank2 = openConn(tBank2, bank2, hsfz::TESTER_BANK2);
  uint8_t req[1 + 2 * NCH];
  req[0] = 0x22;
  for (size_t i = 0; i < NCH; i++) { req[1 + 2 * i] = CH[i].did >> 8; req[2 + 2 * i] = CH[i].did; }

  out.printf("[batch] %u identifiers per request, 0x12%s, 20 s\n", (unsigned)NCH,
             useBank2 ? " + 0x13" : " only");
  uint32_t start = millis(), worst = 0, cycles = 0, fails = 0;
  float lastRpm = NAN;
  while (millis() - start < 20000) {
    hsfz::Client* order[2] = {main12, useBank2 ? bank2 : nullptr};
    uint8_t targets[2] = {hsfz::DME_MASTER, hsfz::DME_SLAVE};
    bool ok = true;
    for (int k = 0; k < 2 && ok; k++) {
      if (!order[k]) continue;
      uint32_t t0 = millis();
      hsfz::Reply r = order[k]->request(targets[k], req, sizeof(req), rx, sizeof(rx));
      uint32_t ms = millis() - t0;
      if (ms > worst) worst = ms;
      if (r.result != hsfz::OK) {
        char what[24];
        snprintf(what, sizeof(what), "[batch 0x%02X]", targets[k]);
        report(what, r);
        ok = false;
        break;
      }
      int off[NCH];
      if (!hsfz::splitReply(rx + 1, r.len - 1, CH, NCH, off)) {
        out.printf("[batch 0x%02X] reply did not line up: ", targets[k]);
        hexdump(rx, r.len); out.println();
        ok = false;
        break;
      }
      if (k == 0 && off[0] >= 0) lastRpm = hsfz::decode(CH[0], rx + 1 + off[0]);
    }
    if (ok) cycles++; else { fails++; if (fails > 5) break; delay(200); }
  }
  float secs = (millis() - start) / 1000.0f;
  out.printf("[batch] %lu cycles in %.1f s = %.1f cycles/s (n63read: 6.2), slowest request %lu ms, %lu failures, last rpm %.0f\n",
             cycles, secs, cycles / secs, worst, fails, lastRpm);
}

static void help() {
  out.println("a run all  d discover  r read once  b batch rate test  s status  x close connections  h help");
}

static void status() {
  out.printf("[status] link %s", linkUp ? "UP" : "down");
  if (linkUp) out.printf(" %d Mbit/s %s duplex", ETH.linkSpeed(), ETH.fullDuplex() ? "full" : "half");
  out.printf(", MAC %s, our address %s, car %s, connections: main %s, bank 2 %s\n",
             ETH.macAddress().c_str(), MY_IP.toString().c_str(),
             carIp == IPAddress(0, 0, 0, 0) ? "not found yet" : carIp.toString().c_str(),
             tMain.c.connected() ? "open" : "closed", tBank2.c.connected() ? "open" : "closed");
  if (WiFi.isConnected())
    out.printf("[status] wifi %s, %s, %d dBm\n", WiFi.SSID().c_str(),
               WiFi.localIP().toString().c_str(), WiFi.RSSI());
  else if (WiFi.getMode() & WIFI_AP)
    out.printf("[status] wifi access point %s, %s, %u joined\n", HOSTNAME,
               WiFi.softAPIP().toString().c_str(), WiFi.softAPgetStationNum());
  else
    out.println("[status] wifi off");
}

static void runAll() {
  if (!linkUp) { out.println("[run] no Ethernet link; skipping"); return; }
  if (!discover()) return;
  readTest();
  batchTest();
  out.println("[run] done. Type x before unplugging, or a letter to rerun a step.");
  help();
}

void setup() {
  Serial.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 4000) delay(10);
  out.println("\nm550 HSFZ spike (read-only)");

  Network.onEvent(onEvent);
  bool eth = ETH.begin(ETH_PHY_W5500, 1, ETH_CS, ETH_INT, ETH_RST, SPI2_HOST, ETH_SCLK, ETH_MISO, ETH_MOSI);
  if (eth) {
    ETH.config(MY_IP, NO_GW, MASK);
    ETH.setDefault();
    out.printf("[eth] W5500 up, MAC %s, our address %s\n", ETH.macAddress().c_str(),
               MY_IP.toString().c_str());
  } else {
    out.println("[eth] W5500 did not start: check the board and pin map");
  }
  startWifi();
  if (!eth) return;

  t0 = millis();
  while (!linkUp && millis() - t0 < 15000) delay(50);
  if (!linkUp) {
    out.println("[eth] FAIL: no link in 15 s. Ignition on? Cable fully seated?");
    out.println("      If the laptop gets a link on the same cable, the pair mapping is the suspect.");
    return;
  }
  delay(500);
  runAll();
}

void loop() {
  if (acceptTerminal()) { status(); help(); }
  out.flush();
  int c = out.read();
  if (c < 0) { delay(20); return; }
  switch (c) {
    case 'a': runAll(); break;
    case 's': status(); break;
    case 'd': discover(); break;
    case 'r': if (carIp != IPAddress(0, 0, 0, 0) || discover()) readTest(); break;
    case 'b': if (carIp != IPAddress(0, 0, 0, 0) || discover()) batchTest(); break;
    case 'x': closeAll(); break;
    case 'h': help(); break;
    default: break;
  }
}
