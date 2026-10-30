# n63read cloud

A Cloudflare Worker that keeps a copy of the car's logs and history and
serves it to Claude from anywhere:

- **R2** holds every drive log and fault list the app uploads.
- **D1** holds the drive and fault summaries (the app's History).
- **`/mcp`** is a remote MCP server with read-only tools over that copy:
  `drive_history`, `fault_history` (with each fault's stored details on
  request), `trend`, `list_files`, `read_log`, `compare_banks` and
  `find_events` (over one stored log), `list_channels`, `channel_series`,
  `list_templates`, `logger_status`, `tank_history`, `drive_faults` (one
  logger drive's fault reads with their texts), `channel_check` (the full
  channel check, and what each module answers) and `drive_health` (each
  drive's health scores, or one drive and channel's RPM x load cells). The
  `logger_*` tools read
  and change the logger's channel presets: `logger_channels` (each channel's
  tier, filtered by module, identifier range, name, tier, or `values`:
  "same", never changed across the stored drives, or "rises", only goes up),
  `logger_set_tiers` (one channel or a whole module, range or `values` set
  at a time, e.g. every never-changing channel to monthly; a dry run says
  what would change), `logger_presets` (the presets, the active
  one and its channel file; a preset's versions), `logger_save_preset`,
  `logger_publish` (make one active: the board takes it within 10 minutes)
  and `logger_rollback`. Every change to a preset, from these or the page,
  keeps what it replaced as a version (the last 50). Nothing writes to the
  car.
- **Templates:** the app's saved channel sets, merged both ways on every sync.
- **Time series:** every value of every drive log with its UTC time, one row
  per channel per drive in D1 (`series`), so `list_channels` and
  `channel_series` can follow a channel across every drive, averaged into
  time buckets when long, or as per-drive min, mean and max. Two series are
  worked out where a log has the transmission's own speeds (EGS 0xDA2A):
  `TC_SLIP`, the torque converter's slip (engine speed less input speed,
  rpm), and `GEAR_RATIO` (input over output speed).
- **One name per log:** an installation whose logs sit in a `logs` folder
  sends `logs/<name>` for what the others send as `<name>`; the Worker drops
  the `logs/` from uploads, series and drive history (`appKey`), so a drive
  two installations have is stored and counted once.
- **The logger board** (`firmware/`) uploads its drive logs under `logger/`,
  two files a drive: `<start>.csv` (the fast and medium channels, every
  cycle) and `<start>.sweep.csv` (the slow channels, one row a pass).
  The Worker works out their time series and drive summary itself
  (`src/drive.js`, a port of the app's `series.py` and `history.summarize`),
  both files counting as one drive under the first's name, so they show in
  `drive_history`, `trend` and the series tools without the PC. The app's
  history sync replaces only its own drives, and each PC's sync only its own
  drives and faults (the app sends its PC's name).
  A drive whose log has no clock at all (the board had neither the internet
  nor the car's clock) is dated by its upload less its length, in the car's
  time zone (`TIME_ZONE`, default `America/Chicago`) like the logs' own
  clocks, and marked `start_approx`. A logger drive's row also has the tank
  level and the odometer at its start and end (`tank_start_l`, `tank_end_l`,
  `odometer_start_km`, `odometer_end_km`; each tank half too,
  `tank_left_start`...; the board reads them together at the start, at Park
  and at engine-off), and the rate each channel tier got (`rate_fast`,
  `rate_medium`: reads a second per identifier; `slow_pass_s`; from the
  board's "tier rates" notes), from which `src/tanks.js` works
  out each tank of fuel: a new one wherever the level rises 8 litres or more
  between drives (a fill-up), with its miles, gallons and mpg.
  Its fault reads during a drive (`<start>.faults.csv`) go into fault
  history under source `logger`, merged as the app's own reads are: first
  and last seen, reads listing it, present, stored or gone. The board sends
  codes only; the texts come from n63read's table, which the app uploads once
  to `ref/BMW_fault_code_texts.csv` (again when it changes). Each failed
  code's details, read while the car is in P (`<stamp>.details.csv`: the
  odometer the module stored when it first and last set, how often it
  occurred, both replies in hex), go into `fault_details`
  (`faultdetails.js`); with the app's own `first_km`/`last_km` they give
  each fault on the page and in `fault_history` its `first_km`, `last_km`,
  `occurrences`, and `first_date`/`last_date` (`{after, by}`): the drive
  in which the car's odometer passed that reading, or the drives either side
  (`after` null: before the first drive). Where a row has no odometer the
  Worker walks the stored snapshot reply itself (`snapshotKm`, the board's
  walk), so details saved by older firmware still get one. Drive odometers
  that don't fit are left out of the dating (`plausibleOdometers`: the
  longest run in which none goes backwards or further than 250 km/h allows,
  so one misread anywhere can't move every date). A fault with no date has
  no odometer to give: on this car the DSC keeps no snapshot for nearly all
  its codes, a code never confirmed (status `0x20`) has none, and the xDrive
  stores power state but no odometer.
- **Drive health** (`health.js`, the logger page's Health tab at
  `/logger/health`, `healthpage.js`): each drive boiled down per channel
  and compared with that channel's own recent drives, so drift stands out.
  Only samples with the engine fully warm count (coolant `ITKUM` at least
  95 °C and, where it was read, oil `TEMP_OEL`, else `ITOEL`, at least
  90 °C; the setpoint `S_TEMP_KUEHLMITTEL` is never used), and not overrun
  fuel cut (either bank's lambda at 1.5 or more). Each goes into an RPM x
  load cell (RPM 400, 900, 1,500, 2,000, 2,500, 3,000, 4,000, 8,000; load 0,
  20, 35, 50, 70, 100, 400 %). Load is the SAE J1979 calculated load, what
  `ILMAR` reports, worked out from the throttle air flow
  (`MASSENSTROM_DK_BANK1`, both banks) and RPM, because `ILMAR` is read
  only every half minute or so; `ILMAR` where there's no air flow. Per
  channel and cell (`health_summaries`): count, mean, median, p95, min,
  max; the misfire counters as increases and revolutions, compared as a rate
  per 1,000 revolutions. The baseline per channel and cell is the median and
  MAD of the last 10 healthy drives' values (healthy: not left out by hand
  in `health_excluded`, and that channel not alerting in them); each cell
  gets a robust z-score (the spread at least a per-channel floor, so a
  channel that never moves doesn't alert on a hair), and the channel's score
  is the cells' sample-weighted mean (`health_scores`): watch from 2,
  alert from 3, per group in `GROUPS`. A cell needs 10 samples (a counter
  1,000 revolutions) and 3 baseline drives, and a drive a minute of warm
  running. Channels and groups (fueling, ignition and knock, temperatures
  and oil, boost, misfires, electrical) are `CHANNELS` in `health.js`.
  Worked out as each drive's series arrive (a failure there never fails the
  upload); `POST /api/health/rebuild` does every stored drive again.
- **Cloudflare Access** guards the whole hostname, and the Worker checks the
  Access token itself on every request, so it fails closed if Access is
  missing or misconfigured. The app signs in with an Access service token;
  you and MCP portals sign in with your Access login. A token the Worker
  refuses on `/mcp` gets a 401 naming Access's resource metadata
  (`WWW-Authenticate: Bearer ... resource_metadata=...`), so an MCP client
  signs in again or refreshes; elsewhere a 403. A token signed with a key the
  Worker hasn't cached fetches Access's keys again (at most every 5 minutes),
  and expiry allows a minute's clock difference.

Plain JavaScript, no packages: `src/worker.js`, `src/drive.js` and the modules beside them. Tests run
on Node 24: `node --test "cloud/test/*.test.mjs"` (from the repo root).
`cloud/test/parity` holds two logs and what the Python code makes of them;
both test suites check against it, so the port can't drift. After changing
a summary or the series on purpose: `python tests/test_cloud_parity.py`.

## What it holds

Everything the app logs: drive logs with GPS positions, fault lists with the
VIN where a module reports it, and the summaries. The bucket and database are
private to your account; only requests that pass Access reach them.

## Deploy

You need Node.js and a Cloudflare account with a Zero Trust organization.
Run these in `cloud/`; nothing here is created until you run it.

1. **Storage.**

   ```sh
   npx wrangler login
   npx wrangler r2 bucket create n63read-logs
   npx wrangler d1 create n63read
   ```

   Copy `wrangler.example.jsonc` to `wrangler.jsonc` (git ignores it) and
   put the D1 database ID in it. The tables are made on first use.

2. **Worker.**

   ```sh
   npx wrangler deploy
   ```

   It answers at `https://n63read-cloud.<your-subdomain>.workers.dev`, but
   refuses everything until Access is set up.

3. **Access.** In the Cloudflare dashboard, *Zero Trust > Access >
   Applications > Add an application > Self-hosted*:
   - Domain: the Worker's hostname (or a custom domain routed to it).
   - Policy 1, *Allow*: your email (or your identity provider group).
   - Policy 2, *Service Auth*: a service token made for the app
     (*Access > Service credentials > Service tokens > Create*). Copy its
     client ID and secret; the secret is shown once.
   - *Advanced settings*: turn on **Managed OAuth**, so MCP clients and portals
     can sign in through Access without a browser cookie.
   - Copy the application's *Application Audience (AUD) tag*.

   Then give the Worker the audience and your team domain:

   ```sh
   npx wrangler secret put ACCESS_AUD           # the AUD tag
   npx wrangler secret put ACCESS_TEAM_DOMAIN   # https://<team>.cloudflareaccess.com
   ```

4. **The app.** *Settings > Cloud*: the Worker URL, the service token's
   client ID and secret, *Save*, then *Sync now*. Or from the CLI:

   ```sh
   n63read sync --url https://n63read-cloud.<sub>.workers.dev --client-id <ID> --client-secret <SECRET>
   ```

   The secret is kept encrypted for your Windows user. With *Upload logs
   automatically* on, the app uploads after each log and fault read and every
   ten minutes, so anything logged offline goes up once the PC is online.

5. **Claude.** Add `https://<worker host>/mcp` to your MCP server portal
   (*Zero Trust > Access controls > MCP Portals > MCP servers*, OAuth
   credentials *Automatic*), or to any
   MCP client that can log in through Access. The tools then read the cloud
   copy, with or without the laptop.

## API

All requests need an Access token (a login, or the service token headers
`CF-Access-Client-Id` and `CF-Access-Client-Secret`).

| Request | Does |
|---|---|
| `PUT /api/files/<path>` | store a log (CSV body); replies with its size and SHA-256. Under `logger/`, a drive log also gets its series and drive row, and the reply says so: `processed`, `channels`, `approx_time` (no Clock), `drive` (start, minutes, km), or `processed: false` and a `reason`. A `logger/…faults.csv` goes into fault history instead: `fault_reads` applied, `modules`, `present`, `stored`; a `logger/…details.csv` into fault details: `details`, `with_km` |
| `GET /api/files` | list stored files: key, size, SHA-256, upload time |
| `GET /api/files/<path>` | the file back |
| `PUT /api/history` | replace one installation's summaries: `{"source": "<PC name>", "drives": [...], "faults": [...]}`. Only that source's drives and faults are replaced; other PCs', the board's (`logger/`) and those from apps that send no source (kept as `legacy`) stay. History lists a log once and each fault as the installation that saw it last has it |
| `PUT /api/series/<path>` | replace one drive's values: `{"start", "approx", "chunks": [{"name", "unit", "t0", "t1", "n", "min", "max", "mean", "data"}]}` |
| `GET /api/series` | channels; `?channel=X` its series (`start`, `end`, `points`), `&by_drive=1` per-drive stats |
| `GET /api/templates` | the templates, deleted ones marked |
| `PUT /api/templates` | apply changes: `{"changes": [{"name": ..., "channels": [...] or null}]}` |
| `POST /api/logger/heartbeat` | the logger board's check-in, at power-on (`"event": "boot"`) and every 10 s while it has Wi-Fi: a JSON object (`boot`, `reset`, `state`: waiting for link, searching, logging or parked; `speed_kmh`, `rpm`, `gear`, `battery_v`, `coolant_c`, `oil_c`, `lat`, `lon`, `faults`, `fault_codes` (when they change), `pending`, `oldest_pending_min`, `up_tries`, `up_failed`, `up_error`, `card_errors`, ...) of at most 4 KB, kept 30 days. Replies `sweep`: the full channel sweep the board should run (its id), or null |
| `POST /api/logger/heartbeats` | check-ins the board kept while it had no Wi-Fi: `{"beats": [...]}`, at most 200 and 64 KB. Each is filed under its own `clock`; one without a clock, older than 30 days or already stored is skipped. Replies `stored`, `skipped` |
| `GET`, `PUT`, `DELETE /api/logger/settings` | the logger board's settings (boardsettings.js): GET what its last check-in says it has (networks, the start of its token's id, `settings_version`), the change waiting or how the last went; PUT `{key: value}` (`wifi_ssid`, `wifi_pass`, `wifi2_ssid`, `wifi2_pass`, `ap_pass`, `access_id` with `access_secret`, `tz`; "" clears) for its next check-in, a person's Access login only; DELETE takes it back. The check-in's reply carries it (`settings_version`, `set_<key>` in hex) until the board says it has it (`settings_version`) or refused it (`settings_failed`, `settings_error`); then the values are dropped. None is ever returned |
| `GET /api/logger` | whether the board is on (a check-in in the last 30 s), its last check-in (with `fault_list`: its `fault_codes`, or its power-on's newest, with each code's module and text, and where known `first_km`, `last_km`, `occurrences`, `first_date`, `last_date`), each power-on in the last `?hours=` (default 24): first and last check-in, why it booted, top speed, hottest coolant and oil, check-ins kept offline, and `wifi_note`, the board's last word on why Wi-Fi wouldn't join (the logger page shows the last check-in's); and `last_drive`: the last logger drive's rate per tier |
| `GET /api/logger/tanks` | `{"tanks": [...]}`, oldest first: each tank's first and last drive, drives, litres added, level at its start and now, km and miles, litres and gallons (`fuel_from`: the fuel flow, else the level's fall), mpg and l/100 km; the last is `open`, the first may be `partial` |
| `PUT /api/logger/firmware?version=V` | publish the logger's firmware for updates over the air (`firmware/tools/publish_firmware.py`): an ESP32 app image of at most 6 MB, `V` not "dev". One build is kept; replies with its manifest |
| `GET /api/logger/firmware` | the manifest: `{"version", "sha256", "size", "uploaded"}` (404 when nothing is published); also in `GET /api/logger` as `firmware` |
| `GET /api/logger/firmware.bin` | the published image |
| `POST /api/logger/sweep` | ask for a full channel sweep on the next drive (`sweep.js`): the board asks every catalog identifier once, as the app's Check car now does. Replies `{"pending": {"id", "requested", ...}, "created"}`; one already waiting is kept |
| `GET /api/logger/sweep` | `{"pending", "last"}`: the one waiting or under way (`seen`, `next`, `of`: how far the board got; `silent`: modules it skipped; `counts`), and the last one done (`done`, `counts`, `newly_ok`); also in `GET /api/logger` as `sweep` |
| `DELETE /api/logger/sweep` | cancel the waiting one: `{"cancelled": id or null}` |
| `GET /api/logger/check-list` | the board's sweep list: `module,did,size,exact`, each catalog identifier once with the reply size its layout needs |
| `POST /api/logger/resummarize` | work the logger's drive rows out again from their stored files (after the summary learns something), five a request: `{"done": [{"file", "start"}], "next"}`; send `?after=<next>` until `next` is null |
| `GET /logger` | the logger page: the car from the board's last check-in (state, speed, coolant and oil, warning above 115 and 140 °C, its position on a map; refreshed every 10 s), each tank since its fill-up, the full channel sweep (ask for one, with a warning; how far it got; which modules didn't answer; the last one), and every channel the board can decode, what the car answers, a tier for each, presets, each channel's newest stored value (flagged when it looks wrong: `values.js` `oddValue`, a raw value at its type's limit such as 0xFFFF, or one no car could have for its unit such as -273 °C), and the rates the selection should get (`estimate.js`: a model of the board's scheduler fitted to a real drive, using the batch sizes and answer times from its last check-in) |
| `PUT /api/logger/catalog` | the app's channel catalog (`n63read loggerchannels.catalog_payload`): `{"columns", "rows", "answers", "modules", "defaults"}`. Answers merge with the stored ones (an identifier that answered "ok" stays "ok"); the first one makes the preset "Default" (`defaults`, everything else slow) and makes it active |
| `GET /api/logger/catalog` | the stored catalog |
| `GET /api/logger/presets` | `{"active", "presets": [{"name", "rest", "tiers", "updated"}], "file": {"preset", "etag", "counts"}}`. `tiers` is `{channel: tier}` (fast, medium, slow, drive, daily, weekly, monthly, off); `rest` ("slow" or "off") is the tier of every channel not named that the car answers, except that the tire blocks (`DSC_RDC_MESSDATENBLOCK_1`-`4`), tank level and odometer (`logger.js` `PER_DRIVE`) are read at least once a drive unless named off |
| `PUT /api/logger/presets` | `{"changes": [{"name", "rest", "tiers"} or {"name", "deleted": true}], "active": name}`, both optional; the active preset can't be deleted. Each preset changed or deleted keeps what it was as a version (MCP `logger_presets`, `logger_rollback`) |
| `GET /api/logger/channels` | the board's `channels.csv` from the active preset, with an `ETag` (`If-None-Match` gets a 304) and `X-Preset`. Whatever the preset says: an identifier its module said it hasn't (refused with 0x31 in a check or a drive: `absent` in the catalog's answers) or one that misdecodes on this car (`logger.js` `UNDECODABLE`: the airbag module's `_IMU_WERTE`) is off, and what the board's own logic reads (`BOARD_NEEDS`: the selector, engine speed, speed, `ITKUM`, `IUIBS`) is at least medium |
| `GET /api/logger/values` | each channel's newest stored value: `{"columns": ["t", "value", "n", "lo", "hi", "drives", "rises"], "values": {channel: [...]}}`, when (UTC seconds), the value, its count, min and max over every stored drive, how many drives, and `rises` (1: in two drives or more, it went up and never down, within a drive or from one to the next: an odometer, a counter). The logger page filters on it ("Only goes up") and on min = max ("Never changed"). Kept in `channel_values`, brought up to date as a drive's series are stored |
| `GET /logger/health` | the Health tab: a heatmap of every drive's health scores (channels by group down the side, drives along the bottom; filters by group, dates and flagged channels), and for a channel and drive its value across drives with the normal band and the other bank's (or matching cylinder's) beside it, the drive's RPM x load cells against the baseline, and the drive's raw values at its longest time in a cell; a drive can be left out of the baselines |
| `GET /api/health` | the scores: `groups` (with `watch` and `alert`), `channels`, `drives` (oldest first: `start`, `warm_min`, `overall`, `level` 0-2, `excluded`), `scores` (`{channel: [[score, cells, value, low, high] or null per drive]}`) and `settings`; `?from=`, `?to=` (dates) narrow the drives |
| `GET /api/health/cell?drive=&channel=` | one drive and channel: each RPM x load cell's seconds, revolutions, longest visits (UTC), value, baseline median, spread, z and samples (and the cell's mean, median, p95, min and max) |
| `POST /api/health/exclude` | `{"drive", "excluded"}`: leave a drive out of the baselines (or let it back in) and score every drive again; a person's login only |
| `POST /api/health/rebuild` | summarize stored drives again from their series, three a request, then score them all: `{"done", "next"}`; send `?after=<next>` until `next` is null; a person's login only |
| `POST /mcp` | MCP (JSON-RPC 2.0, streamable HTTP with JSON replies) |
| `GET /api/status` | who you're signed in as |
