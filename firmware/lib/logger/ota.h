// Over-the-air updates: reading the Worker's firmware manifest and deciding
// whether to take the build it offers. No Arduino code, so the host test
// covers it; the downloading and installing are in logger/src/main.cpp.
#pragma once
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include <string>

namespace logger {

// A string or number field of a flat JSON object, as text: true and `out`
// set when the key is there. Enough for the manifest the Worker writes
// ({"version":"...","sha256":"...","size":123,"uploaded":"..."}), not JSON in
// general (no escapes in values, no nesting).
inline bool jsonField(const std::string& j, const char* key, std::string& out) {
  std::string k = std::string("\"") + key + "\"";
  size_t p = j.find(k);
  if (p == std::string::npos) return false;
  p = j.find(':', p + k.size());
  if (p == std::string::npos) return false;
  p = j.find_first_not_of(" \t\r\n", p + 1);
  if (p == std::string::npos) return false;
  if (j[p] == '"') {
    size_t e = j.find('"', p + 1);
    if (e == std::string::npos) return false;
    out = j.substr(p + 1, e - p - 1);
    return true;
  }
  size_t e = j.find_first_of(",} \t\r\n", p);
  out = j.substr(p, e == std::string::npos ? std::string::npos : e - p);
  return !out.empty();
}

struct Manifest {
  std::string version, sha256;
  size_t size = 0;
};

// The Worker's manifest: false unless it has a version, a SHA-256 of 64 hex
// digits and a size.
inline bool parseManifest(const std::string& j, Manifest& m) {
  std::string size;
  if (!jsonField(j, "version", m.version) || !jsonField(j, "sha256", m.sha256) ||
      !jsonField(j, "size", size)) {
    return false;
  }
  if (m.version.empty() || m.sha256.size() != 64) return false;
  for (char c : m.sha256) {
    if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
  }
  char* end = nullptr;
  long n = strtol(size.c_str(), &end, 10);
  if (*end || n <= 0) return false;
  m.size = (size_t)n;
  return true;
}

// Whether to take the build `offered`: a board running `own` takes any other
// version (newer or older: publishing an older build is how to go back),
// except one that was tried and rolled back (`bad`). An unversioned build
// ("dev", flashed from the IDE for bench work) takes none, so it isn't
// replaced under you.
inline bool wantUpdate(const std::string& own, const std::string& offered,
                       const std::string& bad) {
  return own != "dev" && !offered.empty() && offered != own && offered != bad;
}

// A new build is on trial until it checks in with the Worker or reaches the
// car; one that does neither this long after power-on goes back.
static const uint32_t OTA_TRIAL_MS = 5 * 60 * 1000;

enum OtaStart {
  OTA_NORMAL,
  OTA_TRIAL,       // a new build, not kept yet
  OTA_WENT_BACK,   // the build installed didn't prove itself: this is the one before
};

// At power-on: `pendingVerify` the bootloader's mark on the running build
// (new and not yet kept), `installed` the version last installed ("" once
// kept), `own` this build's.
inline OtaStart otaStart(bool pendingVerify, const std::string& installed,
                         const std::string& own) {
  if (pendingVerify) return OTA_TRIAL;
  if (!installed.empty() && installed != own) return OTA_WENT_BACK;
  return OTA_NORMAL;
}

inline bool trialOver(bool trial, uint32_t upMs) { return trial && upMs >= OTA_TRIAL_MS; }

// Whether the image waiting on the card (version `card`) is still to be
// installed; if not, it's removed.
inline bool cardImageWanted(bool onCard, const std::string& card, const std::string& own,
                            const std::string& bad) {
  return onCard && wantUpdate(own, card, bad);
}

}  // namespace logger
