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
- [x] Decode modules' own snapshot values: each module file's `FUMWELTTEXTE`
      gives them: 26 modules' own fields (`module_catalog.py --freeze`), names
      in English (symbolic ones word by word), value tables kept per module.
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

- [x] Single-source the version: `pyproject.toml` reads `__version__`
      (`dynamic = ["version"]`), a test keeps it that way, and CI stops a
      release whose tag doesn't match.
- [x] Narrow `permissions: contents: write` to a separate release job that
      only attaches what the build made; everything else is read-only.
- [x] Pin actions by commit SHA (checkout, setup-python, upload/download
      artifact, softprops/action-gh-release), and pin the pip installs
      (`requirements-ci.txt`) and the choco Inno Setup fallback.
- [x] Add a ruff config (pyflakes, pycodestyle, blind excepts) and a lint job.
- [x] Test the declared floor `requires-python = ">=3.9"`: a Linux job runs
      the tests on 3.9 and 3.12.
- [x] Install with `pip install -e .` in CI (plus the pinned tools), and add
      a Linux job for the non-GUI tests.
- [ ] Sign the installer (Authenticode), or sign `SHA256SUMS.txt` with a key
      kept outside GitHub, and have the updater check it before running the
      download (`update.py`).

> **Blocked:** needs a decision: a code-signing certificate (paid, for
> Authenticode), or a signing key kept off GitHub and its public half built
> into the app.

## Phase 9 — Docs and naming

- [x] Cut `PROJECT.md` down to a historical brief: what it set out to do,
      the safety rules, the verified decoding cases, the log format rule.
      The layout, build and protocol details it had are in `docs/`.
- [x] Delete the stray untracked `n63read.spec` and `n63read-cli.spec` at the
      repo root.
- [x] One app name: the window, the package description and the docstring
      say "BMW Analyzer", like the installer and the folder.
- [x] Fix the stale `/DAppVersion=0.3.3` example in
      `installer/BMW-Analyzer.iss`, and the `0.3.4` one in the README.

## Phase 10 — In-car verification

> First session 2026-10-18; results in `docs/in-car-checklist.md`.

- [ ] Work through `docs/in-car-checklist.md` and fill in its Results table.
      Done: discover, fault read, GPS, a drive log, DME and ACSM checks.
      Done 2026-10-18: every catalog fits except the fuel pump (silent).
      Left: the BDC's last 57 identifiers and the head unit's busy ones on
      the next release, and the fuel pump.
- [x] Ask again when a module answers busyRepeatRequest (`0x21`), up to
      three times: the head unit said busy to 37 identifiers.
- [x] Fault details lost a fault's second snapshot (the latest time it set)
      when the first held a module's own values of unknown size: the BDC's
      light switch fault hid today's odometer that way. The parser now finds
      the next record by its number, count and first identifier.
- [x] GPS speed is m/s on the car, not km/h: corrected in the catalog and
      `tools/module_catalog.py` (`CAR_CORRECTIONS`), so the GPS speed
      minus vehicle speed channel is right.
- [x] Find why the check stopped partway: a module answered after the
      read had given up, and that late answer was taken for the next
      identifier ("identifier mismatch: asked 0xDA77 got 0xDA5F"), which
      ended the check. A reply must now echo what was asked (identifier for
      0x22, sub-function for 0x19/0x3E) or it is skipped; a garbled reply
      is noted in the check and it carries on. Likely also the old
      `I_EWG_POS@0x13` "wrong identifier".
- [x] Why a drive log stalls for 12-29 s while a check borrows the
      connection: lent requests slotted in between each read of the
      holder's cycle, and a sweep waits 0.5 s at each empty address, so a
      95-value row took up to 28 s. A cycle now holds the connection to its
      end. (A borrowing log during the app's fault read never stalled over
      0.7 s.) Check at the car on the next release.
- [ ] The DSC's fault list differs between reads: 23:15 listed about 35
      more stored codes (coding, brake pedal travel sensor, vacuum sensor)
      than 18:48 and 23:02, interleaved with the rest rather than cut off.
      Read it twice in a row to see whether it's the car or the read.
- [x] The DME keeps no snapshot for `1A400A` (status 0x20, not confirmed),
      but takes over 2 s to say so: fault details now wait up to 6 s.
- [x] Reconnect after the PC slept: the car answered HSFZ control `0x0040`
      (incorrect tester address) for minutes. Now reported in plain words
      and handled as a lost connection; reconnecting carries on.
- [x] Keep the PC awake while connected (Windows `SetThreadExecutionState`,
      held by each session's thread). The lid still follows Windows'
      setting; the README says how to change it.
- [x] The app raised TclError drawing a sample as it closed for an update:
      it stops drawing once closing.
- [x] The session summary called a module silent if it was quiet even
      once (the car resetting the connection silences everyone); now only
      modules that never answered.
- [x] `SPEED_DIFFERENCE_PERCENT` 65535 is shown as no value.
- [ ] Discover right after the PC wakes or the app updates can find no
      169.254 address yet (the adapter is still coming up): retry for a
      few seconds before saying no ENET link.
- [ ] Spark burn time and knock levels for cylinders 5-8 come from `0x13`
      as its `CYL1`-`CYL4`. Offer them as cylinder 5-8 channels so the
      *Misfire hunt* set can include them.
- [ ] `ILAG2` at `0x12` was refused on 2026-10-18 (it read before). Find
      when it answers, or point bank 2 lambda at `ILAG1@0x13`.
- [x] Log `ILUZ5`-`ILUZ8` at `0x12` and `@0x13` side by side to settle
      which DME reports cylinders 5-8: both give the same values.
- [x] Move each module catalog in `docs/modules.md` from "not yet checked" to
      verified, or record what differs: every catalog fits but the fuel
      pump's (not on this car's bus); three size differences recorded.
- [x] Measure the DME logging rate with module channels added: 1.9 s a row
      for 95 channels, about 20 ms a read.
- [x] Look into the bank-2 gaps: `I_EWG_POS@0x13` now reads bank 2's own
      values. `SOLL_POS_EWG` is refused by this DME; taken out of the
      *Boost and wastegates* set.
- [x] Run `n63read speed` at the car. If the DME takes several identifiers
      per request, consider making batching the default in the app too.
      Measured: 1.2 cycles/s one at a time, 6.2 batched, all 18 answered.
      Now the default in the app, `n63read log` and Claude's tools
      (`--one-at-a-time` to turn it off).
- [x] Check the freeze-frame details against a real fault: odometer and
      vehicle time match the car (2026-10-18).

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

## Phase 12 — Map

- [x] A Map window: OpenStreetMap tiles (cached in `%APPDATA%\n63read\tiles`,
      a policy-conformant User-Agent, attribution, downloads switchable off),
      the GPS route coloured by any watched channel, a marker that follows
      live logging and replay, click to jump the replay, pan, zoom, fit.
- [ ] Offer a channel's alert limits as the colour scale, so a route shows
      where a value left its range.

## Phase 13 — Long-term history

- [x] `history.scan()`: every drive log in the logs folder summed up per
      drive (misfires, uneven running, spark burn time and knock level per
      cylinder; knock retard, trims and lambda per bank; temperatures,
      battery, boost, tire pressures, distance, odometer) and every saved
      fault list folded into a per-fault history (first and last seen,
      reads, present / stored / gone now). Incremental via `index.json`.
- [x] The History window (Drives, Trends, Faults), `drives.csv` and
      `faults.csv`, `n63read history`, and Claude's `drive_history` tool.
- [ ] Odometer per drive: log a mileage channel by default
      (`LMV_DATEN_VERTEILERGETRIEBE.MILE_KM`), or take it from fault reads.

## Phase 14 — Cloud storage and remote access (Cloudflare)

Decided 2026-10-20: the Trulock.com account; everything uploaded (logs with
GPS and VIN, fault lists, summaries) into private storage; remote access
behind Cloudflare Access; uploads automatic whenever the PC is online.

- [x] A Worker in the repo (`cloud/`, no account IDs or keys committed):
      R2 for the raw files, D1 for drive summaries and fault history, an
      upload API for the app, and a remote MCP endpoint (drive_history,
      fault_history, trend, list_files, read_log). Plain JavaScript, no
      packages; tested on Node 24 in CI.
- [x] It checks the Access token itself (audience, issuer, signature,
      expiry) and fails closed without Access configured; the app uploads
      with an Access service token kept DPAPI-encrypted in its settings.
- [x] The app's uploader: new and changed files (by size, time and SHA-256)
      and the History summaries, after a log closes, after a fault list is
      saved and every ten minutes; `n63read sync`.
- [x] Deploy on the Trulock.com account: R2 bucket, D1 database, Worker,
      Access application and service token; `/mcp` in the TrulockMCP
      portal (2026-10-20).
- [ ] More MCP tools in the cloud: compare_banks and find_events over a
      stored log, like the local ones.
- [x] Templates sync both ways through the cloud (merge against the last
      synced state; deletions travel; this PC wins a clash); Claude's
      `list_templates`.
- [x] Every fault scan reads each fault's stored details (snapshots and
      counters) and saves them in the fault CSV (`first_km`, `last_km`,
      `occurrences`, `details` JSON); History and the cloud keep the newest,
      and the cloud's `fault_history` returns them for one code.
- [x] Per-sample time series in the cloud: the app turns each drive log
      into one UTC series per channel (series.py; Clock's offset, or this
      PC's zone for older logs) and uploads it once; D1 keeps a row per
      channel per drive; Claude's `list_channels` and `channel_series`
      (bucketed, or per drive) query it.
- [x] Logs' Clock carries its UTC offset (0.6.2).
