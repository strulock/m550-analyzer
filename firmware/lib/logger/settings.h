// The board's settings: its Wi-Fi networks, the setup access point's
// password, the Worker and the board's Access token, and the time zone.
// Kept in the board's flash (NVS), not built into the firmware, so a
// release builds without logger_secrets.h and the image in R2 holds no
// secrets. They're set once over USB (the terminal's S, which
// firmware/tools/provision.py drives), or from the setup access point's
// page, and changed afterwards from the logger page through the check-ins.
// The rules, without NVS or the network, so the host test covers them. No
// Arduino code.
#pragma once
#include <ctype.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <string>

#include "ota.h"   // jsonField

namespace logger {

struct Settings {
  std::string ssid1, pass1, ssid2, pass2, apPass, host, clientId, clientSecret, tz;
  uint32_t version = 0;   // the last change from the logger page taken
};

struct SettingField {
  const char* key;        // as the terminal, provision.py, the setup page and the Worker name it
  std::string Settings::*member;
  bool secret;            // never printed, never in a check-in
};

static const SettingField SETTING_FIELDS[] = {
    {"wifi_ssid", &Settings::ssid1, false},
    {"wifi_pass", &Settings::pass1, true},
    {"wifi2_ssid", &Settings::ssid2, false},
    {"wifi2_pass", &Settings::pass2, true},
    {"ap_pass", &Settings::apPass, true},
    {"host", &Settings::host, false},
    {"access_id", &Settings::clientId, false},
    {"access_secret", &Settings::clientSecret, true},
    {"tz", &Settings::tz, false},
};

inline const SettingField* settingField(const std::string& key) {
  for (const SettingField& f : SETTING_FIELDS) {
    if (key == f.key) return &f;
  }
  return nullptr;
}

inline bool printable(const std::string& v) {
  for (char c : v) {
    if ((uint8_t)c < 0x20 || (uint8_t)c > 0x7E) return false;
  }
  return true;
}

// A value the board can use: Wi-Fi's limits (an SSID 1-32 bytes, a WPA
// password 8-63, or none for an open network), the access point's 8-63, a
// host name, an Access token's id ("....access") and secret, a POSIX TZ.
// An empty value clears the field (a second network, say), except the host
// and the token, which a board can't do without once it has them.
inline bool settingValid(const std::string& key, const std::string& v) {
  if (!printable(v)) return false;
  if (key == "wifi_ssid" || key == "wifi2_ssid") return v.size() <= 32;
  if (key == "wifi_pass" || key == "wifi2_pass") return v.empty() || (v.size() >= 8 && v.size() <= 63);
  if (key == "ap_pass") return v.empty() || (v.size() >= 8 && v.size() <= 63);
  if (key == "host") {
    if (v.empty() || v.size() > 100) return false;
    for (char c : v) {
      if (!isalnum((unsigned char)c) && c != '.' && c != '-') return false;
    }
    return true;
  }
  if (key == "access_id") {
    return v.size() > 7 && v.size() <= 100 && v.compare(v.size() - 7, 7, ".access") == 0;
  }
  if (key == "access_secret") return v.size() >= 16 && v.size() <= 128;
  if (key == "tz") return !v.empty() && v.size() <= 60 && v.find('"') == std::string::npos;
  return false;
}

// One "key=value" line (the terminal, the setup page): the field it sets in
// `s`; false for an unknown key or a value the board can't use.
inline bool settingLine(Settings& s, const std::string& line, std::string& key) {
  size_t eq = line.find('=');
  if (eq == std::string::npos) return false;
  key = line.substr(0, eq);
  std::string v = line.substr(eq + 1);
  while (!v.empty() && (v.back() == '\r' || v.back() == '\n')) v.pop_back();
  const SettingField* f = settingField(key);
  if (!f || !settingValid(key, v)) return false;
  s.*(f->member) = v;
  return true;
}

inline bool fromHex(const std::string& h, std::string& out) {
  if (h.size() % 2) return false;
  out.clear();
  for (size_t i = 0; i < h.size(); i += 2) {
    unsigned v;
    if (!isxdigit((unsigned char)h[i]) || !isxdigit((unsigned char)h[i + 1]) ||
        sscanf(h.c_str() + i, "%2x", &v) != 1) {
      return false;
    }
    out += (char)v;
  }
  return true;
}

// A change from the logger page, in a check-in's reply: "settings_version"
// and a "set_<key>" for each field it changes, the value in hex (a password
// may hold anything JSON would escape). `next` is the settings with it
// applied; `token` when it brings a new Access token, which the board tries
// before it keeps it. False when the reply has none newer than `now`, or a
// value is bad (the whole change is refused then). Never the host: the page
// doesn't send one, and one in a reply would be saved untried (the token's
// try goes to the old host), so a wrong one cut the board off for good; it
// changes over USB or the setup page only.
struct SettingsChange {
  uint32_t version = 0;
  Settings next;
  bool token = false;
  std::string bad;        // the field refused, if one was
};

inline bool settingsChange(const std::string& reply, const Settings& now, SettingsChange& c) {
  std::string v;
  if (!jsonField(reply, "settings_version", v)) return false;
  unsigned long n = strtoul(v.c_str(), nullptr, 10);
  if (!n || n <= now.version) return false;
  c = SettingsChange();
  c.version = (uint32_t)n;
  c.next = now;
  for (const SettingField& f : SETTING_FIELDS) {
    std::string hex, value;
    if (!jsonField(reply, (std::string("set_") + f.key).c_str(), hex)) continue;
    if (f.member == &Settings::host || !fromHex(hex, value) || !settingValid(f.key, value)) {
      c.bad = f.key;
      return true;
    }
    c.next.*(f.member) = value;
    if (f.member == &Settings::clientId || f.member == &Settings::clientSecret) c.token = true;
  }
  c.next.version = c.version;
  return true;
}

// Whether the board has been set up at all: a network or a Worker to reach.
inline bool settingsEmpty(const Settings& s) {
  return s.ssid1.empty() && s.ssid2.empty() && s.host.empty();
}

}  // namespace logger
