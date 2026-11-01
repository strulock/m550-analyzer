// Commands from the logger page's Board tab (the Worker's boardcmd.js): list
// a folder on the card, delete a file or folder, format the card, upload
// now, update the firmware now, restart. Every check-in's reply carries the
// oldest one the board hasn't done ("cmd_id", "cmd", and "cmd_arg" in hex);
// the board keeps the last one it finished in its flash (so none runs
// twice, a format above all) and says in its check-ins which and how
// (cmd_done, cmd_ok, cmd_result). The rules, without the card or the
// network, so the host test covers them. No Arduino code.
#pragma once
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#include <string>
#include <vector>

#include "ota.h"        // jsonField
#include "settings.h"   // fromHex, printable

namespace logger {

enum CommandKind { CMD_NONE, CMD_LIST, CMD_DELETE, CMD_FORMAT, CMD_UPLOAD, CMD_UPDATE, CMD_RESTART };

struct Command {
  uint32_t id = 0;        // 0: none
  CommandKind kind = CMD_NONE;
  std::string arg;        // a path (list, delete), "discard" (format)
  std::string bad;        // why it can't be done, when it can't
};

inline CommandKind commandKind(const std::string& k) {
  if (k == "list") return CMD_LIST;
  if (k == "delete") return CMD_DELETE;
  if (k == "format") return CMD_FORMAT;
  if (k == "upload") return CMD_UPLOAD;
  if (k == "update") return CMD_UPDATE;
  if (k == "restart") return CMD_RESTART;
  return CMD_NONE;
}

// A path on the card as the Worker allows it: absolute, printable, at most
// 200 bytes, no "..", "//", backslash or trailing "/" (but "/" itself).
inline bool cardPath(const std::string& p) {
  if (p.empty() || p[0] != '/' || p.size() > 200 || !printable(p)) return false;
  if (p.find("//") != std::string::npos || p.find('\\') != std::string::npos) return false;
  if (p.size() > 1 && p.back() == '/') return false;
  size_t at = 0;
  while (at < p.size()) {
    size_t end = p.find('/', at + 1);
    if (end == std::string::npos) end = p.size();
    if (p.compare(at, end - at, "/..") == 0) return false;
    at = end;
  }
  return true;
}

// The command in a check-in's reply, if it's one the board hasn't done
// (above `done`). A malformed one comes back with `bad` set: the board
// says so as its result rather than leaving it waiting for good.
inline bool commandIn(const std::string& reply, uint32_t done, Command& c) {
  std::string v, kind, hex;
  if (!jsonField(reply, "cmd_id", v)) return false;
  unsigned long n = strtoul(v.c_str(), nullptr, 10);
  if (!n || n <= done) return false;
  c = Command();
  c.id = (uint32_t)n;
  jsonField(reply, "cmd", kind);
  c.kind = commandKind(kind);
  if (c.kind == CMD_NONE) { c.bad = "unknown command " + kind.substr(0, 20); return true; }
  if (jsonField(reply, "cmd_arg", hex) && !fromHex(hex, c.arg)) { c.bad = "bad argument"; return true; }
  if ((c.kind == CMD_LIST || c.kind == CMD_DELETE) && !cardPath(c.arg)) {
    c.bad = "not a path on the card";
  } else if (c.kind == CMD_DELETE && c.arg == "/") {
    c.bad = "the whole card is format";
  }
  return true;
}

// Whether a command waits for the board to be between drives (not logging,
// not parked with the car): what touches the card's files or restarts it.
inline bool commandBetweenDrives(CommandKind k) {
  return k == CMD_DELETE || k == CMD_FORMAT || k == CMD_RESTART;
}

// A format refused: while files wait to upload, unless asked to discard them.
inline std::string formatRefusal(size_t waiting, const std::string& arg) {
  if (waiting && arg != "discard") {
    return std::to_string(waiting) + (waiting == 1 ? " file waits" : " files wait") +
           " to upload; format with discard to lose them";
  }
  return "";
}

// A JSON string, quotes and all: a card's file names can hold anything.
inline void jsonString(std::string& j, const std::string& s) {
  j += '"';
  for (unsigned char ch : s) {
    if (ch == '"' || ch == '\\') { j += '\\'; j += (char)ch; }
    else if (ch < 0x20) {
      char u[8];
      snprintf(u, sizeof(u), "\\u%04x", ch);
      j += u;
    } else {
      j += (char)ch;
    }
  }
  j += '"';
}

struct CardEntry {
  std::string name;
  uint64_t size = 0;
  bool dir = false;
};

// A folder's listing for POST /api/logger/card: its first entries (at most
// what the Worker keeps), how many there are, and the card's type and size.
static const size_t LISTING_MAX = 200;

inline std::string listingJson(uint32_t id, const std::string& path,
                               const std::vector<CardEntry>& entries, size_t count,
                               const char* type, double sizeMb, double usedMb) {
  std::string j = "{\"id\":" + std::to_string(id) + ",\"path\":";
  jsonString(j, path);
  j += ",\"count\":" + std::to_string(count) + ",\"entries\":[";
  for (size_t i = 0; i < entries.size() && i < LISTING_MAX; i++) {
    if (i) j += ',';
    j += '[';
    jsonString(j, entries[i].name);
    j += ',' + std::to_string((unsigned long long)entries[i].size) + (entries[i].dir ? ",true]" : ",false]");
  }
  char card[96];
  snprintf(card, sizeof(card), "],\"card\":{\"type\":\"%s\",\"size_mb\":%.0f,\"used_mb\":%.0f}}",
           type, sizeMb, usedMb);
  j += card;
  return j;
}

}  // namespace logger
