<img src="src/n63read/data/n63read.png" alt="" width="96" align="right">

# m550-analyzer

`n63read` reads live engine data and fault memory from a 2018 BMW M550i (G30,
N63TU2 / N63B44O2) over the OBD Ethernet (ENET) port, using BMW's HSFZ
protocol. It is pure Python standard library, with a CLI and a Tkinter GUI,
and ships as a single Windows exe. Besides the engine it reads eleven more
modules: head unit (GPS), transmission, transfer case, DSC, steering, body,
suspension, climate, airbag module, fuel pump and park assist.

**Documentation:** [docs/](docs/README.md) covers the protocol, the modules
and which BMW files fit this car, the `.prg` format and how values are
decoded, data sources, the in-car checklist, a glossary, and a
[lookup of every channel](docs/channels/README.md).

## Why

BimmerLink does not expose the Bank 2 channels for this DME. This tool reads
the DME's own measurement identifiers directly, 1,845 of them, to find out
what Bank 2 data is actually readable.

## Hardware

- A **USB ENET adapter** (it appears as a network interface, not a COM port),
  or a WiFi ENET adapter.
- Ignition in **position 2**.

## Network setup

The car runs no DHCP server.

- Leave the adapter on automatic: Windows self-assigns a `169.254.x.x`
  link-local address after 30–60 s.
- Or set a static `169.254.100.100` / `255.255.0.0`, no gateway.

**The broadcast gotcha.** Discovery broadcasts leave via the default route, so
with WiFi up the request never reaches the car. Pin it to the ENET interface:
pick the `169.254.x.x` address in the GUI's **via** dropdown, or pass
`--bind 169.254.x.x` on the CLI. Also set that network to **Private** and
allow the exe (or Python) through Windows Firewall, or the reply is dropped.

## Safety

The tool sends **only** these UDS services:

| Service | Name | Effect |
|---|---|---|
| `0x22` | ReadDataByIdentifier | reads one measurement |
| `0x19` | ReadDTCInformation | reads fault memory |
| `0x3E` | TesterPresent | keep-alive; used for module scans |

`core.ALLOWED_SERVICES` enforces this and `Hsfz.request()` refuses anything
else before a byte is sent. Clearing faults (`0x14`), writing (`0x2E`),
actuator control (`0x2F`) and routines (`0x31`) are all blocked, and the
248 SGBD entries that use them are left out of the shipped catalog. If a
future feature needs any of them, it gets a separate, explicit opt-in path
with a loud warning — not a wider allow-list.

## Install

Download `BMW-Analyzer-Setup.exe` from the
[latest release](https://github.com/strulock/m550-analyzer/releases/latest)
and run it. It installs for your user only (no administrator prompt) into
`%LOCALAPPDATA%\Programs\BMW Analyzer`, and adds to the Start menu:

- **BMW Analyzer**, the app;
- **BMW Analyzer Logs**, the folder `Documents\BMW Analyzer Logs`, where drive
  logs, fault lists and diagnostics reports are saved by default;
- **BMW Analyzer command prompt**, a terminal in the install folder for
  `n63read-cli.exe`.

A desktop icon is optional. The app keeps itself up to date (see
[Updates](#updates)), and Settings > Apps shows the version it's on.
Uninstalling removes the program but never the logs folder. Expect a
SmartScreen warning: the installer is unsigned.

`BMW-Analyzer-portable.zip`, also on each release, is the same folder for
running without installing: unzip it anywhere and run `n63read.exe` from
it. It tells you when there's a newer version but doesn't update itself.

#### Antivirus

The app is a folder (`n63read.exe`, `n63read-cli.exe` and `_internal\`), not
a single self-unpacking exe; those get flagged by antivirus heuristics far
more often. If yours still quarantines it (Trend Micro's `TRX` detections
are machine-learning guesses), restore it from quarantine and add an
exclusion for `%LOCALAPPDATA%\Programs\BMW Analyzer`, or ask whoever
manages the antivirus to. Reporting the file to the vendor as a false
positive clears it for everyone. The exes are unsigned; the checksums of
each release's files are in its `SHA256SUMS.txt`.

## Usage

### GUI

Run `n63read.exe`. Choose the `169.254.x.x` interface under **via**, press
**Discover**, then **Connect**. Filter the channel list, add channels to the
watch table, and press **Start logging** to write a CSV. File dialogs open
in `Documents\BMW Analyzer Logs`.

- **Scan modules** sweeps addresses `0x00`–`0x7F` with TesterPresent and
  lists the modules that answer.
- **Faults** reads the fault memory of every module and shows it. It uses the
  modules from the last scan, or sweeps first if there wasn't one (about a
  minute). While logging, the fault list is saved next to the log as
  `<log>_faults_<time>.csv`; otherwise you're asked where to save it.
  Select a fault and press **Details** (or double-click it) to read what the
  module stored with it: the odometer and vehicle time when it set, the fault
  class, the car's power state, and how often it has occurred. Values of the
  module's own it can't decode yet are shown raw.
- **Discover** that finds a car answering only DoIP (the newer protocol) says
  what that usually means and offers to save a diagnostics report to send.
- **Probe wait s** (default 0.5) is how long Scan modules and Faults wait at
  each address before deciding nothing is there. A sweep costs about this
  times 128 seconds. If a module you expect is missing from a scan, raise it
  to 1 or 2 and scan again. The CLI equivalent is `--probe-timeout` on
  `scan` and `faults --all`.
- **Open log...** replays a CSV through the table and chart at the speed it
  was recorded (pauses over 2 s are shortened). A bar under the toolbar
  pauses it, runs it at up to 16x, and jumps anywhere with its slider;
  **Whole log into graphs** puts every row in the graphs at once instead.
  Other tools' logs open too, BimmerLink's among them: any CSV whose first
  column is the time (seconds or hh:mm:ss), comma- or semicolon-separated.
  Their columns show under their own names, with the unit from the header
  ("Engine speed (rpm)").

- **Templates ▾** saves the watch list under a name and brings it back for
  the next run (it replaces the watch list). **Delete** removes one. They're
  kept in `%APPDATA%\n63read\templates.json`, and `n63read log --template
  NAME` logs one from the command line. **Ready-made sets** has five to start
  from: *Misfire hunt* (engine speed, uneven running and misfire counters for
  all eight cylinders, and each cylinder's difference from the rest),
  *Boost and wastegates, both banks*, *Lambda and trims, both banks*,
  *Temperatures*, and *Drive log with GPS*.
- **Compare banks** folds each engine channel's bank-2 twin into its bank-1
  row, with **Bank 2** and **Δ 1−2** columns, and adds the twin of every
  engine channel you watch. The chart draws bank 2 dashed on bank 1's scale,
  and a pop-out graph draws both. Unticked, bank 2 is a row of its own again.
- **Alert...** sets a low and a high limit for the selected channel, in the
  units shown. Outside them, the row turns red, the PC beeps once, and the
  log's Event column says so. Limits are remembered.
- **Graph** (or double-click a row) opens the selected channels in windows of
  their own, live. Each has a time window (30 s to 1 h, or everything) in
  seconds, minutes or hours; a value scale that fits what's shown or a
  fixed range you type in; and, where it makes sense, other units (°C/°F/K,
  hPa/mbar/bar/kPa/psi, km/h/mph, V/mV, A/mA, kg/h/g/s, Nm/lb-ft...).
  Hover to read a point off; **Pause** holds the view while data keeps
  coming; the line under the graph gives min, mean, max and last of what's
  shown. Graphs follow the language, units and theme settings. They keep the last 20,000 points per channel.

- **Add all shown** adds every channel the filter shows, except private head
  unit values (Wi-Fi password and PIN, SIM identity, previous destinations,
  paired devices), which can still be added one at a time.

**Settings** (top right) has, in one place:

- **Language**: English or Deutsch, as below.
- **Units**: metric or imperial. Imperial shows °F, psi, mph, miles, lb-ft,
  lb/min and inches in the watch table, the chart and the graphs. Logs keep
  the car's own (metric) units, so they compare between runs.
- **Theme**: light, dark, or like Windows.
- **Update automatically** and **Record connection diagnostics**, also in
  the version and Diagnostics menus.

Hover over the chart to read off every line's value, in the displayed
units, at that moment.

**Language** (top right, and in Settings) switches channel descriptions,
value texts in the Status column, fault descriptions and the pop-out graphs
between English and Deutsch (BMW's original German). The filter searches both, whichever is shown, and the
choice is remembered (`%APPDATA%\n63read\settings.json`). Code names
(`ILAG2`, `EGS_ISTGANG.ISTGANG`) stay as they are in both: they're the
column names in logs.

The channel picker is a tree. **Group by Module** shows module ▸ function ▸
channels; **Group by Function** shows function (GPS, gearbox, brakes & tires,
lambda, misfire, fuel, boost, oil, cooling, electrical...) ▸ module ▸
channels. Function groups come from keywords in each channel's name and
description, so they are a guide, not gospel. Values a module returns in one
reply stay together as a sub-group. Select a group and press **Add to watch**
to add all of it (private values excepted); double-click adds one channel.
Typing in the filter opens the matching groups when there are few matches.

**Only channels the car answers** hides everything this car has not been
seen to answer. Each channel is marked ✓ (answers), ≠ (answers, but not the
size the catalog expects), ✗ (refused: not on this car) or – (no answer).
What the car answers is learned from:

- live reads while connected,
- **Answers ▾ > Check car now**, which asks for every catalogued identifier,
  like `n63read check` (a few minutes; while logging, logging slows but
  doesn't stop), and
- **Answers ▾ > Import n63read check CSV...** for a check run from the CLI.

It is saved in `%APPDATA%\n63read\availability.json`, so one check carries
over to later sessions. **Forget recorded answers** clears it.

The chart draws the rows selected in the table, or the first six numeric
channels if none are, each on its own scale; the legend gives each line's
range. Enumerated values (GPS fix, gear) are logged and charted as numbers,
with their meaning in the Status column.

Until you connect, the table and chart run on made-up **demo data** (engine
speed, pedal, road speed, boost setpoint, bank 2 lambda, oil temperature,
GPS fix, satellites, speed, latitude, longitude and height, gear,
transmission oil temperature, a tire pressure and lateral acceleration),
marked "DEMO DATA" on the chart. It stops the moment you press **Connect** or
open a log, and its points are dropped so they never mix with real ones.

**Calculated** channels (a group of their own in the picker) are worked out
from others every cycle: lambda and lambda control factor, bank 1 minus bank
2; boost pressure minus its target; GPS speed minus vehicle speed; and each
cylinder's uneven running minus the mean of all eight. Adding one adds what it
needs; they show, chart, log and alert like any channel.

A log has `Time` first (seconds from the start of the log, as in a BimmerLink
export), then `Clock` (the date and time with its offset from UTC, such as
`2026-10-19 07:42:50.123-05:00`, to line it up with video or other logs, across
time zones and daylight-saving changes), one column per channel, and `Event` last: alerts, and the connection
dropping and coming back. If the connection drops while logging (a cable
bump, the car going to sleep), the log stays open, the gap is marked, and the
app keeps trying to reconnect and carries on in the same file. Replay skips
`Clock` and `Event`.

While connected to the car, n63read (the app, the CLI and Claude's sessions)
keeps Windows from going to sleep; the screen can still turn off. Closing
the lid still follows Windows' setting: to log with the lid shut, set *When I
close the lid* to *Do nothing* (Control Panel > Power Options), or run
`powercfg /setdcvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0` and
`powercfg /setactive SCHEME_CURRENT` (on battery; `setacvalueindex` for
plugged in). A PC that sleeps mid-log comes back to a car still holding the
old connection; it lets go within a few minutes and the log carries on.

The log is written to disk about once a second, so a crash or flat battery
loses at most a second. Changing the watched channels while logging starts a
new file (`run_2.csv`, `run_3.csv`, ...) rather than mixing column layouts in
one file; `Time` carries on counting. A fault read while connected shares the
connection: logging slows down during it but doesn't stop.

#### Map

**Map** (beside Graph) shows the route on a street map, coloured by any
watched channel: speed, boost, uneven running, coolant temperature... A
white marker follows the car while logging and the replay while replaying;
click the route during a replay to jump to that moment. Drag to move the
map, scroll or **+**/**−** to zoom, **Fit route** to see it all; **Follow**
keeps the marker in view. For a channel with alert limits (**Alert...**),
**By alert limits** colours the route green inside them, blue below and red
above, so it shows where on the road a value left its range. It needs the head unit's GPS position
(`HU_STATUS_GPS_POSITION.LATITUDE` and `.LONGITUDE`; the Map button offers
to watch them). Logs from other tools work too if they have latitude and
longitude columns.

The street map is OpenStreetMap's, downloaded as needed and kept in
`%APPDATA%\n63read\tiles`, so a route you've driven before shows without
internet later; without tiles the route draws on a plain background.
Downloading tells the tile server roughly where the map is: *Settings > Map*
switches it off.

#### History

**History** (top bar) adds up every drive log and fault list in the logs
folder, from the app, the CLI and Claude alike, and logs copied in from
elsewhere:

- **Drives:** one row a drive: date, odometer (read from the car as each log
  starts, and noted in its first row's `Event`), length, distance, top speed, misfires
  (and on which cylinders), knock retard and fuel trims per bank, peak
  temperatures, battery, tire pressures. Double-click one to replay it.
- **Trends:** a statistic across drives, one line per cylinder or bank:
  misfires, uneven running, spark burn time, knock retard, fuel trims, tire
  pressures, battery, temperatures, boost, distance.
- **Faults:** every fault ever read, with when it first and last appeared,
  how many reads listed it, and whether it's present, stored or gone now.

The same tables are saved as `drives.csv` and `faults.csv` in
`Documents\BMW Analyzer Logs\History` for Excel; `n63read history` prints
them, and Claude's `drive_history` tool reads them. Only new or changed
files are read again, so it stays quick as logs pile up. A statistic is
blank for a drive that didn't log its channel.

#### Cloud copy

Optionally, the app keeps a copy of the logs folder in your own Cloudflare
account and Claude can read it from anywhere: a small Worker (in
[`cloud/`](cloud/README.md)) stores the logs in R2 and the History in D1,
behind Cloudflare Access, and serves read-only MCP tools (`drive_history`,
`fault_history`, `trend`, `list_files`, `read_log`, `compare_banks`,
`find_events`, `list_templates`, and
`list_channels` / `channel_series` over a time-series store holding every
value of every drive with its UTC time, so one channel can be followed across
all drives). The
app's channel templates sync too, both ways, so a template saved on one PC
appears on the others. Once deployed, enter
its URL and an Access service token under *Settings > Cloud*; the app then
uploads new and changed logs after each drive and fault read, and whatever
it couldn't send offline once the PC is online again (`n63read sync` does
it by hand). Nothing is uploaded until it's set up.

### Other modules: head unit, transmission, chassis, body, climate

Besides the DME, the channel list holds the readable values of eleven more
modules, generated from their SGBD files by `tools/module_catalog.py` and
labelled by module in the picker:

| Module (address) | File | Prefix | Values | Highlights |
|---|---|---|---|---|
| Head unit (0x63) | `nbtevo` | `HU_` | 717 | GPS position, fix, satellites, GPS speed |
| Transmission, ZF 8HP (0x18) | `gszfb1` | `EGS_` | 3,526 | gear, oil temperature, input/output speed, converter lock-up, selector, paddles |
| DSC (0x29) | `dsc_g30n` | `DSC_` | 868 | tire pressure and temperature per wheel, wheel speeds, brake pressures, pedal travel |
| Airbag module (0x01) | `acsm5` | `ACSM_` | 565 | acceleration and yaw rate (`ACSM__IMU_WERTE.*`), seat occupancy |
| Body controller (0x40) | `bdc_g11` | `BDC_` | 1,357 | fuel tank levels, ride height, drive mode, rain and light sensors |
| Steering, EPS (0x30) | `eps_g11` | `EPS_` | 66 | pinion / steering angle, steering torque, rack travel |
| Suspension, VDP (0x76) | `vdp_g11` | `VDP_` | 538 | ride height per corner, damper valve currents, wheel acceleration, roll stabilisation |
| Transfer case, xDrive (0x19) | `lmv_g11` | `LMV_` | 224 | clutch torque and status, clutch temperature model |
| Climate, IHKA (0x78) | `ihka_g11` | `IHKA_` | 154 | climate temperature sensors, A/C compressor speed and state |
| Fuel pump, EKP (0x17) | `ekp301` | `EKP_` | 13 | pump motor voltages and currents, battery voltage, tank senders, temperatures |
| Park assist, PMA (0x2C) | `pma_20` | `PMA_` | 89 | distance at every parking sensor, parking space size, reverse gear |

Most of the transmission's values are shift adaptation tables. Their fault
texts are in the fault file too, so their codes now come with a description.

Useful channels for a drive log:

| What | Channel |
|---|---|
| Latitude, longitude (degrees), height (m) | `HU_STATUS_GPS_POSITION.LATITUDE` / `.LONGITUDE` / `.HEIGHT` |
| GPS fix (searching, 2D, 3D...); position valid | `HU_STATUS_GPS`; `HU_STATUS_GPS_POSITION.VALIDITY` |
| GPS speed vs. wheel speeds | `HU_STATUS_SPEED.GPS_SPEED`; `DSC_GESCHWINDIGKEIT_RAD.*` |
| Satellites, accuracy (PDOP / HDOP / VDOP) | `HU_STATUS_GPS_SATINFO.*`, `HU_STATUS_GPS_DILUTION_OF_POSITION.*` |
| Gear; transmission oil temperature | `EGS_ISTGANG.ISTGANG`; `EGS_GETRIEBEOELTMEPERATUR` |
| Transmission input / output speed; converter lock-up | `EGS_EINAUSGANG_DREHZAHL.*`; `EGS_WANDLERKUPPLUNG` |
| Lateral / longitudinal acceleration, yaw rate | `ACSM__IMU_WERTE.Y_ACC_SCAL` / `.X_ACC_SCAL` / `.Z_RATE_SCAL` |
| Tire pressure and temperature, per wheel | `DSC_RDC_MESSDATENBLOCK_1`...`_4.LETZTER_REIFENDRUCKWERT` / `...TEMPERATURWERT` |
| Brake pressure; brake pedal travel | `DSC_DSC_DRUCKSENSOREN.*`; `DSC_BREMSPEDALWEGSENSOR.WEG_IST` |
| Fuel level, left / right tank (Ohm) | `BDC_TANK_FUELLSTAND_LINKS` / `_RECHTS` |
| Steering angle; steering torque | `EPS_EPS_RITZELWINKELSENSOR.*`; `EPS_EPS_MOMENTENSENSOR.*` |
| Ride height per corner; damper currents | `VDP_STATUS_HOEHENSTAENDE_LESEN.*`; `VDP_VDC_2_VENTILE_STROM.*` |
| xDrive front clutch torque | `LMV_LMV_MOMENT.*` |
| Fuel pump current; voltage at the pump | `EKP_STATUS_ADC_BEWERTET.MOTOR_STROM_A`...; `.SPANNUNG_KL30` |

These channels are read alongside the DME on one connection, but not every
cycle. What moves while driving (GPS, speeds, gear, brakes, acceleration)
refreshes every second, everything else every 10 seconds, at most six module
reads per cycle, and the last value fills the log in between. That keeps the
DME rate up. A module read waits at most half a second, an identifier the
module refuses as unsupported is not asked for again, and a module that
stops answering is left alone for 30 seconds. Engine channels get the same
care: one that never answers waits at most a second and is then left alone,
so a sleeping bank-2 DME doesn't slow the rest of the log.

The GPS position and the fuel pump's values come from hand-coded jobs, not
tables, so their layouts were worked out by disassembling
`STATUS_GPS_POSITION` (see `core.py`) and `STATUS_ADC_BEWERTET` (see
`tools/module_catalog.py`).
`n63read gps --watch` prints the raw bytes next to the decoded position.

**None of these catalogs has been checked against the car yet.** BMW ships
several variants of each module's file, and a wrong one decodes plausible
nonsense. `n63read check` settles it: it reads every catalogued identifier
on every module (the DME too) and compares each reply's size with what the
file predicts, then says per module whether the file fits. It only reads
(0x22). With `--out`, every reply goes to a CSV, except private values.

### Updates

The app keeps itself up to date from this repository's GitHub releases.
About 10 seconds after it starts, and every 6 hours while it's open, it
checks for a newer release. If there is one:

1. It downloads that release's `BMW-Analyzer-Setup.exe` to
   `%LOCALAPPDATA%\n63read\updates` and checks its SHA-256 against the
   digest GitHub publishes (or the release's `SHA256SUMS.txt`). A mismatch
   discards the download and changes nothing.
2. **Restart to update** runs it silently, closes n63read, and the installer
   starts it again when it's done. Otherwise it installs when you close
   n63read, ready for next time.
3. Not while another n63read from the same folder runs (Claude's
   connection, a command-line `log`): the installer can't replace files in
   use, and a half-finished update leaves the app unable to start. **Restart
   to update** offers to close them; closing the app waits for next time;
   `n63read update` says what to close.

If the app ever says "Failed to start embedded python interpreter", an
update didn't finish: close n63read and Claude and run the latest
`BMW-Analyzer-Setup.exe`.

To roll back, install an older release's setup over it.

It only takes a strictly newer version, never downloads while connected to
the car, and stays quiet when there's no internet (the car's network has
none). The footer, bottom right, shows the version; when an update is ready
it says so, and its menu has **Restart to update**, **Check for updates
now**, **Automatic updates** (on/off) and **Release notes**. Only the
installed copy updates itself; the portable one says "portable" in the
footer and only tells you.

From a terminal: `n63read update --check`, `n63read update` and
`n63read --version`.

### Claude

Claude can use n63read directly: check the connection to the car, read
channels, fault memory and GPS, and review your drive logs and the
diagnostics journal. It gets the same read-only commands as the CLI, nothing
more. You need [Claude Code or Claude Desktop](https://claude.ai/download)
on the same PC, signed in, and internet alongside the car's link (Wi-Fi or
a phone hotspot).

**Diagnostics ▾ Connect to Claude...** sets it up: it adds n63read to Claude
Code (for all your projects) and to Claude Desktop, whichever are installed,
and offers to start Claude Code in the logs folder. Restart Claude Desktop
afterwards. Then ask, for example, "use n63read to check the connection to
my car" or "review my last drive log".

From a terminal: `n63read-cli mcp --register` (or `--unregister`). By hand,
the server is `n63read-cli.exe mcp`, an MCP server on stdin/stdout:
`claude mcp add -s user n63read -- "%LOCALAPPDATA%\Programs\BMW Analyzer\n63read-cli.exe" mcp`.
Uninstalling removes it from Claude again.

Its tools: `status`, `connection_check`, `discover`, `scan_modules`,
`read_channels`, `sample` (a few seconds of channels, summarised),
`start_log` / `stop_log` (a drive log, named channels or a template),
`read_faults`, `read_gps`, `check_modules`, `search_channels`,
`list_files`, `read_log` (also a time window, chosen columns, averaged
rows), `compare_banks` (bank 1 against bank 2 in a log), `find_events`
(alerts, limits crossed, misfire counters rising) and `diagnostics_journal`.
The file tools only read the logs folder and the journal. Each tool call is
noted in the journal.

### Bank 2 and the car's connections

The N63 has an engine computer per bank: `0x12` for bank 1, `0x13` for bank 2
(cylinders 5–8), with the same channel names. Every engine channel is listed
again as `NAME@0x13` for bank 2; in the GUI they're under **Engine bank 2
(DME 0x13)**.

The car takes two diagnostic connections at a time. n63read uses one for
bank 1 and every other module, and opens the second for bank 2 only while
you're reading bank 2. Whichever n63read holds the car - the app, a CLI log,
a quick command, Claude - lends it to the others, and they borrow it instead
of opening their own, so the app, the CLI and Claude all work at the same
time. When the one holding it finishes, whoever is still reading carries on
with the car itself: Claude finishing a log doesn't knock a test run in the
app offline, and the other way round. If the car drops a connection because
something else holds one (another diagnostic tool, say), n63read says so.

### Connection diagnostics

Every Discover, Connect, session and car check is recorded to a journal in
`%APPDATA%\n63read\diagnostics\` (one file a day, kept 30 days), so a
connection that doesn't work can be diagnosed afterwards:

- **Snapshot:** the PC's network adapters and drivers, interfaces,
  addresses, network profiles (Private/Public), firewall profiles and
  n63read's firewall rules, plus `ipconfig /all`, `arp -a` and `route print`.
- **Discover:** every request sent and every reply received, with timing,
  plus the devices seen on the car's link.
- **Failed connect:** the exact error, and whether anything listens on 6801
  (HSFZ) or 13400 (DoIP) at that address.
- **Session:** how long it ran, samples read, refused identifiers, silent
  modules, why it ended, and the first 400 frames on the wire, raw.
- **Verdict:** what most likely went wrong, in plain words.

**Diagnostics ▾** in the toolbar switches recording on or off, saves a
report of the last 7 days to send on, and opens the folder. Run n63read as
administrator and **Capture packets for 60 s** records the traffic itself
(Windows' `pktmon`) as a `.pcapng` beside the journal. It captures all
traffic on the PC for that minute, so close other apps first.

From a terminal, `n63read diag` does the whole check step by step: it finds
the PC's ENET address, takes the snapshot, runs Discover, tests the ports on
whatever answers or is seen on the link, says hello (`3E 00`) to the gateway
and the DME if HSFZ answers, prints a verdict and records it all. Add
`--capture` (as administrator) to record the packets too, or
`--report FILE` to save the journal for sending.

The journal holds addresses, MAC addresses and the car's identification
reply, which includes its VIN. It stays on this PC unless you send it.

### CLI

```
n63read discover --bind 169.254.x.x          find the car
n63read scan --ip 169.254.x.x                which modules answer
n63read read --ip 169.254.x.x ILAG1 ILAG2    read channels once
n63read read --ip 169.254.x.x IUKZ5@0x13     bank 2: the same channel from the second DME
n63read faults --ip 169.254.x.x -v           DME fault memory, decoded (codes that
                                             failed; --every-code for all)
n63read faults --ip 169.254.x.x --all --out faults.csv
                                             every module, saved to CSV, each fault
                                             with what was stored with it (odometer,
                                             time, conditions, how often); quicker
                                             without: --no-details
n63read log --ip 169.254.x.x --template "Idle check"
                                             log a template saved in the app, or
                                             a ready-made set ("Misfire hunt")
n63read templates                            list the saved templates
n63read log --ip 169.254.x.x ILAG1 ILAG2 ILUZ5 ILUZ7
                                             log to Documents\BMW Analyzer Logs
                                             (or --out run.csv; --samples 100 stops
                                             after 100; --one-at-a-time reads one
                                             engine value per request)
n63read speed --ip 169.254.x.x               engine values a second: one per request
                                             vs several per request
n63read gps --ip 169.254.x.x --watch         head unit GPS fix and position
n63read check --ip 169.254.x.x --out check.csv
                                             does each module's catalog fit?
n63read sync                                 upload new logs and the history to your
                                             cloud Worker (cloud/README.md)
n63read history                              every drive and fault in the logs folder,
                                             summed up (also History\drives.csv)
n63read selftest                             every data file bundled and loading
n63read diag                                 connection check, step by step, recorded
n63read diag --capture                       ...and record the packets (administrator)
n63read diag --report report.jsonl           save the recent journal to send
n63read mcp --register                       let Claude use n63read (see Claude)
n63read update                               install the latest release
n63read update --check                       only say whether there is one
```

`read` and `log` take the other modules' channels (`HU_...`, `EGS_...`...)
too; they go to their own module whatever `--target` says.

The fault CSV has one row per fault, plus a row for each module that had no
faults, refused, or didn't reply, so the file shows everything that was
checked.

`--target` selects a module (default `0x12`, master DME). The log CSV is
`Time`, `Clock`, one column per channel, then `Event`, as in the app; `Time`
first keeps the shape of a BimmerLink export. A connection that drops while
logging is retried every 2 seconds and the log carries on (Ctrl-C stops it).

Channel blocks are **not contiguous**. Cylinders 7–8 of the V8 sit in a
separate range, so don't assume sequential identifiers:

| Measurement | ARG | ID |
|---|---|---|
| Uneven running, cyl 1–6 | `ILUZ1`–`ILUZ6` | `0x4A30`–`0x4A35` |
| Uneven running, cyl 7–12 | `ILUZ7`–`ILUZ12` | `0x4A43`–`0x4A48` |
| Misfire counter, cyl 1–3 | `ZAEHLER_AUSSETZERK_ZYL1`–`3` | `0x4B23`–`0x4B25` |
| Misfire counter, cyl 4–9 | `…ZYL4`–`ZYL9` | `0x4B30`–`0x4B35` |
| Lambda actual, bank 1 / 2 | `ILAG1` / `ILAG2` | `0x5889` / `0x588A` |

## Development

```
py -m venv .venv
.venv\Scripts\pip install -e .[dev,build]
.venv\Scripts\pip install -r requirements-ci.txt   # the exact tool versions CI uses
.venv\Scripts\pytest
.venv\Scripts\ruff check src tests tools
build.bat
```

The version is set in one place, `src/n63read/__init__.py`; `pyproject.toml`
reads it from there, and a release tag (`v0.4.0`) must match it or CI stops.

`build.bat` runs PyInstaller on `BMW-Analyzer.spec`, which writes one
folder, `dist\BMW Analyzer\`: `n63read.exe` (GUI), `n63read-cli.exe` and the
`_internal\` they share. The CLI is a console exe, or it would have no
stdout. The installer is `installer\BMW-Analyzer.iss`
([Inno Setup 6](https://jrsoftware.org/isinfo.php)):
`iscc /DAppVersion=<version> installer\BMW-Analyzer.iss` writes
`dist\BMW-Analyzer-Setup.exe`. CI lints, runs the tests on Linux (Python 3.9
and 3.12) and Windows, builds on every push, zips the folder as
`BMW-Analyzer-portable.zip`, installs, reinstalls (as an update does) and
uninstalls silently as a smoke test, and attaches the setup, the zip and
`SHA256SUMS.txt` to the release when a `v*` tag is pushed. Its actions are
pinned to exact commits and its tools to exact versions
(`requirements-ci.txt`), and only the release job may write to the
repository. Keep the installer's `AppId`: upgrades and the uninstall entry
depend on it.

The data CSVs live in `src/n63read/data/`. At runtime they are found in
the PyInstaller bundle, the package, next to the exe, or the working
directory, so an updated CSV can be dropped next to the exe without
rebuilding.

`PROJECT.md` has the original brief and the safety rules; [docs/](docs/README.md)
is the reference. After changing a catalog, run `python tools/translate.py`
and `python tools/channel_docs.py`; tests fail if either is out of date.

## Data provenance

The CSVs were extracted from BMW SGBD (`.prg`) files, mainly `dme8ff_r.prg`;
the module catalogs (`<MODULE>_measurements.csv`, `<MODULE>_enum_tables.csv`)
and their fault texts from `nbtevo`, `gszfb1`, `acsm5`, `dsc_g30n`,
`bdc_g11`, `eps_g11`, `vdp_g11`, `lmv_g11`, `ihka_g11`, `ekp301` and `pma_20.prg`, by
`tools/module_catalog.py`.
They hold identifier numbers, units and scaling factors: the interface facts
you need to talk to hardware you own. BMW's `.prg` files and SP-Daten are not
in this repo and must not be committed.

The English text columns are a mechanical glossary translation, generated
by `tools/translate.py` from `tools/glossary.py`. German is the source of
truth. To fix a translation, add the term to the glossary and run
`python tools/translate.py`; don't edit the English columns by hand.

## License

MIT for the code.
