// What the logger sends the Worker from its card: the drive files (each with
// a .done file of its SHA-256 and size) and the check-ins kept while there
// was no Wi-Fi. The rules for both, without the HTTP and the card, so the
// host test covers them; the sending is in logger/src/main.cpp. No Arduino
// code.
#pragma once
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include <string>

namespace logger {

// ---- drive files ----------------------------------------------------------------
// A finished file's .done line: "<sha256> <size>".
struct Done {
  std::string sha;
  unsigned size = 0;
};

inline bool parseDone(const std::string& line, Done& d) {
  char sha[65] = "";
  unsigned size = 0;
  if (sscanf(line.c_str(), "%64s %u", sha, &size) < 1 || strlen(sha) != 64) return false;
  d.sha = sha;
  d.size = size;
  return true;
}

enum UploadStep {
  UPLOAD_BAD_DONE,   // no .done line worth the name: the file hashed again
  UPLOAD_MISSING,    // the .done without its file
  UPLOAD_EMPTY,      // nothing reached the card (a power cut): removed, not sent
  UPLOAD_SEND,
};

// An empty file has nothing to send and can't be sent: HTTPClient sends a
// stream of size 0 without a length and waits for the file's available() to
// say -1, which it never does, so the upload task hung there (26-27 Oct) and
// everything behind it waited.
inline UploadStep uploadStep(bool doneOk, bool fileThere, size_t fileSize) {
  if (!doneOk) return UPLOAD_BAD_DONE;
  if (!fileThere) return UPLOAD_MISSING;
  return fileSize == 0 ? UPLOAD_EMPTY : UPLOAD_SEND;
}

// A failed upload and the rest of the queue: one that says the way there is
// shut (no connection or reply, the token refused, too many requests) ends
// the round; anything about this file (too large, refused, the Worker failing
// on it) skips just it, so the files behind it still go. A 200 with another
// hash is this file's too.
inline bool uploadStopsRound(int code) {
  return code <= 0 || code == 401 || code == 403 || code == 408 || code == 429;
}

// A file that keeps failing is tried every round at first, then every
// UPLOAD_RETRY_EVERY rounds (about 2 minutes), so it doesn't cost a full
// upload every 15 s for ever.
static const unsigned UPLOAD_FAILS_BEFORE_SPACING = 3, UPLOAD_RETRY_EVERY = 8;
inline bool uploadTryNow(unsigned failures, unsigned round) {
  return failures < UPLOAD_FAILS_BEFORE_SPACING || round % UPLOAD_RETRY_EVERY == 0;
}

// The Worker took the file whole: a 200 whose reply has the file's hash.
inline bool uploadAccepted(int code, const std::string& body, const std::string& sha) {
  return code == 200 && body.find("\"sha256\":\"" + sha + "\"") != std::string::npos;
}

// ---- check-ins kept offline -------------------------------------------------------
// One JSON object a line in BEATS_FILE, sent in batches as
// {"beats":[...]} (POST /api/logger/heartbeats).
static const uint32_t BEATS_MAX_BYTES = 3000000;   // about 12 hours offline; later ones dropped
static const size_t BEATS_BATCH_BYTES = 48000;     // a request (the Worker takes 64 KB)

inline bool beatFits(size_t fileSize, size_t lineSize) {
  return fileSize + lineSize <= BEATS_MAX_BYTES;
}

// One batch, from the file as it's read from where the last batch ended:
// whole lines only, as many as fit (at least one, however long), so a torn
// last line (a power cut mid-write) is never sent.
class BeatBatch {
 public:
  explicit BeatBatch(size_t maxBytes = BEATS_BATCH_BYTES) : max_(maxBytes) {}

  // The file's next bytes: false once the batch is full (the rest is the
  // next batch's).
  bool feed(const uint8_t* b, size_t n) {
    for (size_t i = 0; i < n && !full_; i++) {
      if (b[i] != '\n') { line_ += (char)b[i]; continue; }
      size_t grown = body_.size() + (count_ ? 1 : 0) + line_.size() + 2;   // a comma, "]}"
      if (grown > max_ && count_) { full_ = true; break; }
      body_ += count_++ ? "," : "";
      body_ += line_;
      used_ += line_.size() + 1;
      line_.clear();
    }
    return !full_;
  }
  size_t count() const { return count_; }
  uint32_t used() const { return used_; }         // bytes of the file it took
  std::string body() const { return body_ + "]}"; }

 private:
  size_t max_;
  std::string body_ = "{\"beats\":[", line_;
  size_t count_ = 0;
  uint32_t used_ = 0;
  bool full_ = false;
};

}  // namespace logger
