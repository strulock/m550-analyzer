# BMW Analyzer — plan

Work plan for `n63read` / BMW Analyzer after 0.3.8. It comes from a full review
of the repo (performance, security and vehicle safety, features, code quality,
tests and docs) on 2026-10-16. Phases are in priority order. Each item names
the file and line it starts from, as of 0.3.8.

## Where things stand

- **Vehicle safety holds.** The only write to the diagnostic socket is
  `Hsfz._send`, reached only through `Hsfz.request`, which refuses any service
  outside `ALLOWED_SERVICES = {0x19, 0x22, 0x3E}` (`core.py:64`). Every route
  (CLI, GUI, MCP, share host, diag) ends there, and `test_protocol.py` checks
  that writes, resets, DTC clears and routines are refused. Nothing in this plan
  widens that set.
- **Tests:** 372 pass in about 65 s. Coverage is 85% (core 92%, gui 84%,
  mcp 83%, cli 78%, update 75%, diag 73%). pyflakes is clean.
- **On the car:** only the DME catalog has been checked. The other 11 module
  catalogs, the head-unit GPS layout, and the logging rate with module channels
  added are still unverified (`docs/in-car-checklist.md`).

## Decisions

- **The diagnostics journal stays unredacted.** The VIN, GPS readings and
  network details (MAC addresses, ARP, `ipconfig`) are kept in `diag.report`
  and the MCP `diagnostics_journal` tool by choice (decided 2026-10-16). Don't
  add redaction there.

## Phase 1 — Bugs that lose data or freeze the app

- [x] `_drain` stops for good if any message handler raises: only
      `queue.Empty` is caught, so `self.after(60, self._drain)` is skipped
      (`gui.py:1644-1792`). Reschedule in `finally` and wrap each message in
      its own `try`.
- [x] A disconnect or error while logging leaves `logging_to` and the
      "Stop logging" button stale (`gui.py:1692-1699`, `1785-1789`, and manual
      Disconnect at `1484-1489`). Later fault reads are saved next to the dead
      log. Reset the log state in every disconnect path.
- [x] Closing the window while logging can lose the last second: `_close`
      stops the daemon poller but never joins it (`gui.py:1914-1926`). Join
      with a timeout before `destroy()`.
- [x] Starting a log with an empty watch list writes a 0-byte CSV that replay
      then rejects (`gui.py:152-154`). Refuse to start, or write the header.
- [x] A scan swallows every exception per address (`gui.py:1455`). Catch only
      `HsfzError` and show anything else.
- [x] A late status or error message from a replaced connection dropped the
      new one. Messages now carry the poller that sent them.

## Phase 2 — Hardening

- [x] Limit the HSFZ reply length: the 4-byte length is trusted as-is
      (`core.py:202-205`), so a device on the LAN can make `recv` try to
      allocate up to 4 GB. Refuse `length > 0x10000` with `HsfzError`, and
      read into a `bytearray` instead of `buf += chunk`.
- [x] MCP `read_channels` appends `names` straight to the CLI arguments
      (`mcp.py:205-209`), so a name like `--source` overrides the tester
      address. Put `--` before the names and reject names starting with `-`.
- [x] Guard drive-log CSVs against formula injection: prefix `'` to text cells
      starting with `= + - @`, tab or CR (`gui.py:201-203`, `cli.py:278`).
- [x] Make discovery stricter: require a well-formed `CTRL_IDENT` reply
      (`core.py:681-683`), prefer 169.254.x.x sources, and show the VIN for
      the user to confirm before filling the IP box (`gui.py:1704-1722`).
- [x] Cap the line length `share.Host._client` reads before it checks the
      token (`share.py:95`).
- [x] Remove the longitude hint in the comment at `core.py:630`.

## Phase 3 — Logging speed and long-run performance

- [x] Back off a DME or bank-2 channel that never answers. Today each one
      costs the full 2 s timeout on every cycle (`core.py:1109-1145`), so one
      silent `@0x13` channel drops the whole log under 0.5 Hz. Add a
      per-target `quiet_until`, a `refused` set and a 0.5 s per-read timeout,
      as `_read_record` already does.
- [x] Hold the requested rate: the interval sleep is added after the read
      (`gui.py:162`, `cli.py:286`). Sleep `max(0, interval - elapsed)`.
- [x] Redraw the chart once per `_drain`, not once per queued sample, and
      throttle hover redraws (`gui.py:1650-1652`, `1823-1912`).
- [x] Keep point history only for channels that have a pop-out graph, or use
      `array('d')` ring buffers. Today each watched channel can grow to about
      2.4 MB (`gui.py:26`, `1180`, `1298`, `1822`).
- [x] Skip a pop-out graph redraw when nothing changed, and thin the points to
      about 2 per pixel column (`graph.py:291-364`).
- [x] Work out each watch entry's availability key once, and call `tree.set`
      only when the shown text changed (`gui.py:883-891`, `1046-1055`,
      `1795-1813`). Also stops the 10 s picker rebuild hitch
      (`gui.py:917`, `1074-1075`).
- [x] Use a set for the duplicate check in "Add all", which is O(n²) over
      ~11.8k channels (`gui.py:1175`).
- [x] Load replay logs in a worker thread, or stream the rows; store each row
      as a list. A 1 h, 100-channel log takes about 2 s and 84 MB on the Tk
      thread (`gui.py:1345`, `playback.py:133-155`).
- [x] Cache catalog loads in the MCP server, and stream `read_log` with
      running min/max/mean (`mcp.py:237-238`, `296-325`).

## Phase 4 — Bank 1 vs Bank 2 and robust logging

- [x] Bank comparison view: pair each `NAME` with `NAME@0x13` automatically,
      show them side by side with a delta column, and overlay them on one
      graph (lambda, wastegate, knock, fuel trims).
- [x] Built-in channel sets: misfire hunt, boost/wastegate both banks,
      lambda/trims both banks, thermal, drive log with GPS.
- [x] Auto-reconnect while logging. Done in the same file rather than a
      `_partN` one: the gap is marked in the Event column. A dropped socket
      is now `ConnectionLost`, which the reader passes on instead of turning
      it into blank values.
- [x] Add a wall-clock column to logs, keeping `Time` first for BimmerLink.
      Measure `Time` from log start in the GUI, as the CLI does
      (`gui.py:130`, `156` vs `cli.py:260`).
- [x] Alerts: per-channel min/max with a row highlight, a beep and a log
      marker.
- [x] Derived channels: bank lambda delta, per-cylinder uneven-running
      deviation from the mean, boost actual minus setpoint, GPS speed vs wheel
      speed.

## Phase 5 — Diagnostics depth

- [x] Freeze frames: read `0x19 04/06` (still service 0x19, read-only) and
      decode them. `BMW_freeze_frame_fields.csv` turned out to be mostly
      neighbouring tables picked up by the first extraction, without the
      odometer (`0x1700`); it was rebuilt from `DTCSNAPSHOTIDENTIFIER`, which
      every module file supplied agrees on. Shown under Details in the fault
      window, by `faults --details`, and by the MCP `read_faults` tool.
- [x] Multi-identifier `0x22` reads: built, off by default, and
      self-checking (a reply is used only if every echo lines up; any doubt
      turns batching off for that DME). `n63read speed` measures both ways
      and `n63read log --batch` uses it. Measuring at the car is in Phase 10.
- [ ] Decode modules' own snapshot values: each module file's `FUMWELTTEXTE`
      gives them, but the texts need English first (`tools/translate.py`).
- [x] A clearer DoIP path: what a DoIP-only answer usually means, offered
      with a diagnostics report, in the app, `discover` and `diag`.

## Phase 6 — Replay and MCP

- [x] Replay speed control from 1x to 16x and a scrub bar. `speed` exists in
      `playback.py:196-219` but isn't exposed.
- [x] Load a log straight into graphs without real-time replay.
- [x] Open BimmerLink CSVs in replay: any CSV with the time first (seconds
      or hh:mm:ss, comma or semicolon, decimal commas). Not yet tried on a
      real BimmerLink export.
- [x] MCP `read_log` with `start`, `end`, `columns` and `resample`.
- [x] MCP `compare_banks(log)` and `find_events` (threshold crossings, misfire
      counter increments).
- [x] MCP `start_log`, `stop_log` and `sample(names, seconds)` through the
      share host.
- [x] Have MCP tools call core functions that return data instead of scraping
      CLI output (`mcp.py:147-170`): `read_channels` and the new tools do.
      The report tools (`connection_check`, `discover`, `scan_modules`,
      `read_faults`, `read_gps`, `check_modules`) keep the CLI's report on
      purpose: the report is what they return.

## Phase 7 — Code structure, coordinated sessions and tests

- [x] One engine: `engine.Session` (connect, read loop, drive log, jobs,
      reconnect) for the app's poller, `n63read log` and Claude's
      `start_log`/`sample` (replaces the planned `poller.py`).
- [x] Sessions never knock each other off: every holder of the car lends it
      (one-off commands and Claude's tools too), a machine-wide lock stops
      two processes connecting at once, borrowed requests take the
      connection's lock, and a borrower takes the car over when the holder
      stops. `tests/test_coordination.py`.
- [x] Turn `_drain` into a handler table, and move the decisions that don't
      need Tk into plain functions (`core.discovery_outcome`).
- [x] One module scan: `core.scan`, used by the CLI and the app.
- [x] Log the silent failures (Documents and logs folders, stopping a
      process for an update, saving settings and answers) to the journal.
- [x] Replace the fixed sleeps in `test_faults.py` and `test_diag.py` with
      waits on a condition.
- [x] Tests for the `_drain` crash path, close-while-logging, the live
      `op_faults` and `op_update` paths, and `update` download/install.
- [x] `n63read log --samples N`: stop after N samples.

## Phase 8 — Tooling, CI and supply chain

- [ ] Single-source the version: `pyproject.toml:7` and `__init__.py:3` are
      set separately. Use `dynamic = ["version"]` and check that the release
      tag matches.
- [ ] Narrow `permissions: contents: write` to the release job; pull-request
      runs don't need it (`build.yml:9-10`).
- [ ] Pin actions by commit SHA, especially `softprops/action-gh-release`, and
      pin the pip and choco installs.
- [ ] Add a ruff config and a lint step. `.gitignore` already lists
      `.ruff_cache/`.
- [ ] Test the declared floor `requires-python = ">=3.9"`, or raise it.
- [ ] Install with `pip install -e .[dev]` in CI, and add a Linux job for the
      non-GUI tests.
- [ ] Sign the installer (Authenticode), or sign `SHA256SUMS.txt` with a key
      kept outside GitHub, and have the updater check it before running the
      download (`update.py:112-128`, `171`, `251`).

## Phase 9 — Docs and naming

- [ ] Cut `PROJECT.md` down to a historical brief. Its layout, entry points
      and onefile build command no longer match the code
      (`PROJECT.md:19-20`, `157-169`, `215-230`).
- [ ] Delete the stray untracked `n63read.spec` and `n63read-cli.spec` at the
      repo root.
- [ ] One app name: the window title says "N63TU2 DME Reader"
      (`gui.py:238`), while the installer and folder say "BMW Analyzer".
- [ ] Fix the stale `/DAppVersion=0.3.3` example in
      `installer/BMW-Analyzer.iss:2`.

## Phase 10 — In-car verification

> **Blocked:** needs the car.

- [ ] Work through `docs/in-car-checklist.md` and fill in its Results table.
- [ ] Move each module catalog in `docs/modules.md` from "not yet checked" to
      verified, or record what differs.
- [ ] Measure the DME logging rate with module channels added, before and
      after Phase 3.
- [ ] Look into the bank-2 gaps: `SOLL_POS_EWG` is unreadable and
      `I_EWG_POS@0x13` replies with the wrong identifier
      (`docs/modules.md:118-120`).
- [ ] Run `n63read speed` at the car. If the DME takes several identifiers
      per request, consider making batching the default in the app too.
- [ ] Check the freeze-frame details against a real fault: odometer, time and
      counters should match the car.

## Phase 11 — More BMW files

- [x] Re-extract the DME's value tables. `BMW_enum_tables.csv` was made by
      the first extractor, which read each table's position from the
      directory entry before its name, so each table's texts sit under the
      next table's name: "Status terminal 15" and "Condition for detected
      knocks" show gear texts, "COMM status FA CAN" shows engine-stall
      reasons. It can't be shifted back reliably: tables that weren't kept
      sit between the ones that were. The module catalogs use the newer
      extractor and aren't affected. Done from `dme8ff_r.prg` with
      `tools/module_catalog.py --dme`: the 22 value tables the catalog uses.
- [x] Fault texts for 0x56 (FZD, `fzd_g11`), 0x5D (KAFAS, `kafasg11`) and
      0x61 (telematics: `atm02`, `atm`, `tcb4g` merged, ATM2's wording first).
- [x] Fault texts for 0x6D, the driver's seat module (`fas_g11`), and for
      the other G11-family modules a G30 can have (passenger seat, cluster,
      radar, rear steering, park sensors, gear selector...): 10,662 texts.
- [ ] Find out which telematics variant the car has (its 0x61 codes in a
      fault read will say), and drop the other two files' texts if they clash.
- [x] The DME's own snapshot values: 1,538 fields from its `FUMWELTTEXTE`,
      for both DMEs (`dme8ff_l` has the same table).
