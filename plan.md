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
- [x] Fault dates: the head unit's (and gateway's) snapshots start with their
      control-unit address (`0x1707`, 1 byte), which stopped the board's walk
      before the odometer; sized now, and the cloud walks stored snapshots
      itself so older details get theirs. Most other codes without a date have
      none to give: the DSC kept no snapshot for 44 of its 45, and the xDrive
      stores no odometer. Drive odometers that can't be right (an app log's
      48,554 km six minutes before 111,610) are left out of the dating, so a
      fault older than the drives reads "by" the first one, not "on" it.
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
- [x] Discover right after the PC wakes or the app updates can find no
      169.254 address yet (the adapter is still coming up): the app and the
      CLI now wait up to 10 s for it before searching.
- [x] Spark burn time and knock levels for cylinders 5-8 come from `0x13`
      as its `CYL1`-`CYL4`: those twins now say which cylinder they are
      ("Bank 2: ... cylinder 5"), and *Misfire hunt* includes all eight.
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
- [x] Offer a channel's alert limits as the colour scale, so a route shows
      where a value left its range: *By alert limits* (green within, blue
      below, red above).

## Phase 13 — Long-term history

- [x] `history.scan()`: every drive log in the logs folder summed up per
      drive (misfires, uneven running, spark burn time and knock level per
      cylinder; knock retard, trims and lambda per bank; temperatures,
      battery, boost, tire pressures, distance, odometer) and every saved
      fault list folded into a per-fault history (first and last seen,
      reads, present / stored / gone now). Incremental via `index.json`.
- [x] The History window (Drives, Trends, Faults), `drives.csv` and
      `faults.csv`, `n63read history`, and Claude's `drive_history` tool.
- [x] Odometer per drive: each log reads the mileage once as it starts
      (the transfer case's bus copy, else the suspension module's EEPROM
      copy) and notes "odometer N km" in its first row's Event; History
      takes it for the drive.

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
- [x] More MCP tools in the cloud: compare_banks and find_events over a
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

## Phase 15 — The logger board

The ESP32-S3 board in the car (`firmware/`), the cloud's side of it
(`cloud/`) and the logger page. The logger's own history is in the git log
and `firmware/README.md`; this records the notes reviewed on 2026-10-27
after the first week of drives, and what's left.

### Done (2026-10-28, PRs #87-#106)

- [x] Coolant from the engine's sensor (`ITKUM`, DME 0x4300), not the
      radiator outlet's (`ITKUA`, 52 °C with the oil at 97 °C) (#87).
- [x] Battery from the battery sensor (`IUIBS`) first, the DME's `IUADW`
      as the fallback; both medium (#88).
- [x] The airbag module's `_IMU_WERTE` (0x4101) left out: its answer on this
      car is an event record, not accelerations (#89).
- [x] An identifier a module refuses with 0x31 rests the whole drive and is
      "absent" in the cloud's catalog, off in every channel file (#91).
- [x] Check-ins carry the failed fault codes; the Worker adds their texts
      (#92).
- [x] Check-ins on their own task, every 10 s by the clock; a memory check
      before each TLS session (#93).
- [x] The first check-in sooner (one scan picks the network); check-ins from
      before the clock was known kept and dated later (#94).
- [x] Uploads: the backlog at power-on, a drive's files as it ends, paced
      while logging; no firmware install while files wait (#95).
- [x] One name per drive: the board reconnects when the gateway closes the
      connection, in the same files (#96).
- [x] A module that times out is backed off on its own (2 s, doubling to
      30 s); the rest keep overlapping (#97).
- [x] The full channel check only while the car stands (#98).
- [x] Odometer and tank (whole and halves) snapshots at the start, at Park
      and at engine-off; the selector (P R N D M) in the medium tier and the
      check-ins; what the board's logic reads at least medium in every
      channel file (#99).
- [x] A drive ends parked (P, engine off, 60 s), noted; the board waits
      parked for the next; every way a drive ends documented (#100).
- [x] The rate each tier got, noted each minute and averaged per drive;
      the logger page shows the last drive's beside its estimates (#101).
- [x] MCP tools for the channel catalog: list, set (one or in bulk), save,
      publish, versions and rollback (#102, #106).
- [x] MCP `drive_faults` and `channel_check` (#103).
- [x] `/mcp` answers a refused token with a 401 naming Access's resource
      metadata; Access's keys fetched again on an unknown key id (#104).
- [x] The bank 2 duplicates (`MOTORLEISTUNG_EFFEKTIV`, `ZW_AUSGABE_BANK2`,
      `LUFTMASSENFLUSS_GEF_BANK2` on 0x13) off the fast tier of the Default
      preset (data, through `logger_set_tiers`; version 1 keeps the before).
- [x] Checked, no change needed: TrulockIOT reaches Cloudflare (the board
      uploaded and checked in over it on 27-28 Oct); the seven tiers keyed by
      name (bank 2's carry `@0x13`) were already there; the damage counters
      (ACSM 0x407F, 0x4080) were already "once a drive" in the preset.
- [x] Tests: the cloud tests' fake Worker reads a body before refusing it
      (a WinError 10053 flake on Windows) (#105).

### Still to do

- [ ] **On the car:** the first drives on this firmware should show one file
      per drive (a "reconnected" note where the gateway closes the
      connection), snapshots at Park and engine-off with the odometer
      moving, "end: parked..." when the car is left in P with the ignition
      on, "left alone" notes instead of minutes at a third of the rows when
      the DME times out, the tier-rate notes, and the first check-in within
      seconds on the hotspot. Check memory (`largest_kb` in the check-ins)
      with an upload and a check-in at once.
- [ ] **Power after the engine stops:** the board runs only while it has
      power (ignition here). A supply that holds a minute or two after the
      ignition (or constant power with a sleep) would let the last drive's
      files go up before the car is left (note 1).
- [ ] **MCP sign-in:** reconnect the TrulockMCP connector once and see the
      one-day sign-in renew without a prompt. If it still doesn't, the cause
      is in Access (the application's session duration, the MCP portal's
      OAuth), not the Worker. On 28 Oct, the evening after #104, the
      portal (mcp.trulock.com) still asked to sign in again too often:
      look at the Access application's session duration and the portal's
      token lifetime and refresh.
- [ ] The burst (pedal or boost) needs `BUS_FAHRPEDALWERT` and `IPLAD`
      read often, and the Default preset has neither on fast: add them to
      `BOARD_NEEDS`, or to the preset.
- [ ] The airbag module's 0x4101 layout on this car, if its accelerations
      are wanted (a raw reply, compared with a known bump).
- [ ] Park and engine-off in the same cycle make one snapshot (noted
      "engine off"); fine for the odometer, but the note names only one.
- [ ] The logger page has no versions or rollback yet (the MCP tools do).
- [ ] The stray two-row drives from before #96 (e.g. 2026-10-27_19-49-56,
      20-03-49) are still in R2 and History.
- [x] **Drives logged minutes into nothing (28 Oct).** 2026-10-28_19-51-00
      ran until 20:00 and its file ends at 19:53:08; the check-in at 19:53:18
      found the card with no space and no files (it came and went after).
      A card error leaves FatFS's open file failing every write after it,
      and the old `flush()` counted what `fwrite` buffered, so the `.done`
      said 787,590 bytes where the card had 405,571 (11-47-20: 1,030,910 and
      655,776): nothing shrank, and power cuts weren't it (a reset mid-drive
      on the bench lost nothing). Each flush is now checked on the card and a
      failed one reopens the file and writes again (#114); the check-ins
      count them (`card_errors`). On the bench, with a check-in's card work
      and constant reads on the other core, a flush failed about every 4
      minutes at 20 MHz and at 10 MHz alike; every file's hash matched its
      `.done` after.
- [x] The list of 29 Oct (#116): the check-ins report uploads tried and
      failed, the last failure and the oldest waiting file's age; the
      check-in built in a kept buffer and its fault codes sent only when
      they change; the transmission's input and output speeds (EGS 0xDA2A)
      medium, with the converter's slip (`TC_SLIP`) and the gear ratio
      (`GEAR_RATIO`) worked out in the cloud; app logs named once
      (`appKey`), the `logs/` copies removed.
- [x] Module 0x40 "answering in 550 ms" (29 Oct): every module's
      `answer_ms` is the cycle's time as the cycles see it (0x40's 81 ms when
      the reads took 75, 213 when they took 211), so 0x40 wasn't the slow one
      and asking it less wouldn't raise the rows. The rows fell from about 11
      a second to 4.5 a minute into each drive of 28 Oct, on 0.10.0: the DMEs'
      batches shrinking after timeouts, which #110 (0.10.1) stops. Check the
      next drive's stats notes on 0.10.3 or later.
- [x] The board's settings out of the firmware (#119): Wi-Fi, the setup
      access point, the Worker, the Access token and the time zone in its
      flash (`lib/logger/settings.h`), set once over USB
      (`firmware/tools/provision.py`) or from the access point's page, and
      changed from the logger page's Board settings through the check-ins (a
      new token tried before it's kept). Published builds never include
      `logger_secrets.h`, so a release builds without it and the image in R2
      holds no secrets.
- [x] The review of 29 Oct, items 1-5 (#123): firmware can't be published
      with the board's own token (the one that checks in) nor written to its
      R2 keys, and publish_firmware.py uses the app's token; an upload the
      Worker refuses or fails on skips that file instead of stopping the
      queue, spaced out after three failures; a drive file's retry writes
      from the checked end and cuts off a torn tail; a Wi-Fi change to the
      network the board is on is tried before it's kept; a check-in's answer
      settles only the change it saw. Also: a malformed `S` line is no
      longer echoed whole.
- [x] From that review (and the evening of 29 Oct): `logger_publish` with
      no name is refused (it cleared the active preset); a `logger_set_tiers`
      range or module that isn't hex is refused (it selected every channel);
      the TLS room check and handshake are one step under a lock, so the
      check-in and upload tasks can't both start one on room for one; a
      trial firmware's 5 minutes count only with Wi-Fi or the car's link up;
      the snapshot stops waiting once the car's connection closes. Check-ins
      kept offline carry the fault codes only when they change (every one
      did), and say why Wi-Fi wouldn't join (`wifi_note`).
- [x] Bench checks of 30 Oct: the Wi-Fi note against the live Worker (a
      missing network, a wrong password, kept check-ins sent on return);
      provision.py now sends a line at a time (the whole file at once was
      lost); AUTH_EXPIRE reads "wrong password?"; saves of the app's JSON
      and CSV files are tried again while Windows holds the file (a flaky
      test, and the same could fail a sync); the logger page shows the
      Wi-Fi note.
- [x] The page's "Never changed" and "Only goes up" filters for the MCP too:
      `logger_channels` and `logger_set_tiers` take `values` ("same",
      "rises"), so a chat can list them or move them all to a slower tier.
- [ ] Still open from that review: an Ed25519 signature on the firmware
      image, checked by the board; provision.py and C escapes;
      `logs/logger/` names; the Wi-Fi terminal without a password.
- [ ] On 29 Oct the board lost power three times while driving (power-on
      resets at 37 and 50 km/h, 8:41-8:49 pm): the USB cable or the car's
      port, if it wasn't unplugged by hand.
- [ ] Try a change from the logger page's Board settings (a person's login
      only): a backup network, then a new Access token (make it, wait for
      "taken", then revoke the old).
- [ ] Watch `card_errors` in the car's check-ins: many more than the bench's
      one in 4 minutes would point at the card or its power (a new card, or
      the supply) rather than the firmware.
- [x] 38 of the app's logs from 14-20 Oct are in R2 twice: under `logs/`
      (an upload on 21 Oct, 02:38 UTC) and at the top, where the app's
      sync puts them (16:35 the same day), byte for byte the same size. MCP
      `channel_series` and anything else that walks every file counts each
      of those drives twice. Delete the `logs/` copies (and their rows in
      D1), and have the Worker skip a file whose name it already has
      without the prefix.
