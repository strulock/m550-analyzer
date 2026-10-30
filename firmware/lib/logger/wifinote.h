// Why the board isn't on Wi-Fi, for the check-ins (wifi_note): each known
// network as the last try found it. "not seen" means it wasn't in the
// board's scan, which is 2.4 GHz only: out of range, not advertising (an
// iPhone hotspot with nothing joined, its settings screen closed), or 5 GHz
// only (Maximize Compatibility off) all look the same from here. One that
// was seen and refused says why, from the station's disconnect reason.
// The rules, without the network, so the host test covers them. No Arduino
// code.
#pragma once
#include <stdio.h>
#include <string>
#include <vector>

// The words for a failed join. `reason`: the station's disconnect reason
// (esp_wifi_types_generic.h; 0 if none came before the wait ran out),
// `name` the core's name for it.
inline std::string wifiJoinWhy(int reason, const char* name) {
  if (reason == 0) return "no answer";
  const char* gloss = nullptr;
  switch (reason) {
    case 15: case 202: case 204: gloss = "wrong password?"; break;   // handshake, auth
    case 201: gloss = "gone"; break;                                   // no AP found
    case 210: case 211: gloss = "its security isn't one the board accepts"; break;
    case 200: case 212: gloss = "signal too weak"; break;              // beacon timeout, RSSI
  }
  char num[16];
  snprintf(num, sizeof(num), " (%d)", reason);
  std::string out = name && *name ? name : "reason";
  out += num;
  if (gloss) out = std::string(gloss) + ", " + out;
  return out;
}

struct WifiTry {
  std::string ssid;
  bool seen = false;        // in the scan
  int rssi = 0, channel = 0;
  bool tried = false;       // joined (seen, or the scan failed)
  std::string why;          // wifiJoinWhy, if tried and failed
};

// "Phone: not seen; Home: seen -71 dBm ch 6, join failed: wrong password?
// (6 networks in range)". `scanned`: the scan's count, < 0 if it failed.
inline std::string wifiNote(int scanned, const std::vector<WifiTry>& nets) {
  std::string out;
  char buf[48];
  for (const WifiTry& n : nets) {
    if (n.ssid.empty()) continue;
    if (!out.empty()) out += "; ";
    out += n.ssid + ": ";
    if (scanned >= 0 && !n.seen) {
      out += "not seen";
      continue;
    }
    if (n.seen) {
      snprintf(buf, sizeof(buf), "seen %d dBm ch %d", n.rssi, n.channel);
      out += buf;
    }
    if (n.tried) out += std::string(n.seen ? ", " : "") + "join failed: " + n.why;
  }
  if (scanned < 0) {
    out += " (the scan failed)";
  } else {
    snprintf(buf, sizeof(buf), " (%d network%s in range)", scanned, scanned == 1 ? "" : "s");
    out += buf;
  }
  return out;
}
