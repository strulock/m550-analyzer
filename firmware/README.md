# Firmware

C++ for the always-plugged-in drive logger (ESP32-S3 + W5500). It ports the
part of n63read a logger needs; nothing here is part of the Python package or
the Windows build.

| Path | What |
| --- | --- |
| `lib/hsfz/` | HSFZ client, discovery parsing, decoder. No Arduino code; same rules as `src/n63read/core.py` (`Hsfz`, `Tester`, `decode`, `split_reply`) |
| `lib/logger/` | The logger's channel file, decoding (as `core.decode`), read plan, bursts and a module's back-off when it stops answering, CSV layout, fault reads, the car's clock, SHA-256, the firmware update decisions (`ota.h`), the upload and kept check-in rules (`upload.h`) and the full channel check (`carcheck.h`). No Arduino code |
| `test/host_test.cpp`, `test/test_logger.cpp` | Run `lib/hsfz` against a scripted fake gateway, and `lib/logger`, on a PC (CI job `firmware`) |
| `tools/bench_car.py` | Fake ENET gateway for bench tests over a real cable (discovery + reads, both DMEs; any module with `--channels`; `--delay MS` per answer; `--slow`, `--bad`, `--single` to act like the car's slower and pickier modules; `--park`, `--drive`, `--close`, `--quiet` for the drive's events: parked with the engine off, driving again, the gateway closing the connection, a module going quiet) |
| `tools/send_channels.py` | Load a channel file into the logger over Wi-Fi |
| `tools/publish_firmware.py` | Build the logger with a version and publish it for updates over the air, or flash it by USB |
| `logger/` | The logger: logs the car to the SD card, uploads each drive to the cloud Worker |
| `spike/` | Phase 0 bench tool: can a W5500 on an ESP32-S3 talk HSFZ to both DMEs, and how fast |

Read-only, as n63read: the client refuses anything but UDS 0x22, 0x3E and
0x19 02 (the fault list; never 0x14, which clears faults), and the host test
checks that nothing else is ever sent.

When `core.py` changes how it frames, routes or decodes, change `lib/hsfz` in
the same PR.

## Host test

    cd firmware
    g++ -std=c++17 -Wall -Ilib/hsfz test/host_test.cpp -o host_test && ./host_test
    g++ -std=c++17 -Wall -Ilib/logger test/test_logger.cpp -o test_logger && ./test_logger

One function per case; `./host_test busy_retry` runs just that one.

## Logger

Waveshare ESP32-S3-ETH (16 MB flash, 8 MB PSRAM), a FAT-formatted microSD
card, the car's ENET cable, and USB power that comes on with the ignition.

1. **Settings.** The board keeps its Wi-Fi networks (a second as a backup),
   the setup access point's password, the Worker's host
   (`m550.trulock.com`), an Access service token made for the board, and the
   time zone in its flash (`lib/logger/settings.h`), not in the firmware: a
   release builds without them, the image in R2 holds none, and updates
   keep them. Set them once (after step 3, or whenever the flash is
   erased), either way:
   - **USB:** copy `logger/src/logger_secrets.example.h` to
     `logger_secrets.h` (gitignored), fill it in, and
     `python firmware/tools/provision.py --usb COM4` sends it (the terminal's
     `S`: `key=value` lines, then a dot; USB only, and the board echoes only
     the keys). `--set wifi2_ssid=Phone --set wifi2_pass=...` sends just
     those. The board saves them and restarts.
   - **Phone:** a board with no settings opens the access point
     `m550-logger` (password `m550-setup`) from boot, and a board with
     settings opens it, with its own password, when no known network is in
     range. `http://192.168.4.1/` there is a form for every setting
     (passwords never shown; blank keeps), saved, then a restart. It
     answers only over the access point, and once the board is set up it
     no longer offers the host or the token (a page that could change the
     host would hand the token to whatever host it named): those change
     over USB, or the token from the logger page. The fixed `m550-setup`
     opens only a board with no settings, which has nothing to give away.

   After that, the logger page's **Board settings** changes the networks,
   the token, the access point's password and the time zone (not the
   host): the Worker hands the change to the board in its next check-in's
   reply and drops it once the board says it has it. A new token is tried
   with a check-in before the board keeps it, so a wrong one can't lock it
   out; make the new one, wait for "taken", then revoke the old. A change
   to the network the board is on is tried at once, and the old settings
   kept if no network joins with the new (a change to one out of range
   can't be tried; the board stays on the one it has). A build
   made on a PC with `logger_secrets.h` (the IDE's, or a bench build) starts
   a board with empty settings from it.
2. **Channels.** Pick them on the logger channel page, `https://<worker>/logger`
   (the app opens it: Answers > Logger channels, or Settings > Cloud): every
   channel the board can decode, filtered to what the car answers, a tier for
   each, saved as presets. The board downloads the active preset's
   `channels.csv` (`GET /api/logger/channels`) soon after Wi-Fi comes up and
   every 10 minutes, only when it changed, and uses it from the next drive.
   The tiers: **fast** (every cycle), **medium** (a few times a second),
   **slow** (in rotation), and the periodic ones, read once in a drive
   they're due: **drive** (every drive), **daily** (the first drive of the
   day), **weekly** and **monthly** (7 or 30 days after the last read; the
   board keeps when). The first preset, "Default", is the built-in lists:
   fast (boost, wastegates, lambda and knock per bank, RPM, throttle, pedal,
   gear, speed), medium (the drive log, fuel economy and misfire templates
   plus trims, uneven running, wastegate sensors and oil pressure), and slow,
   everything else the car answers. A preset whose other channels are off
   still reads the tire blocks (`DSC_RDC_MESSDATENBLOCK_1`-`4`: pressure,
   temperature, wheel), the tank level and the odometer once a drive, for
   History's tire columns and the fill-ups, unless it names them off.
   Offline, `n63read logger-channels --out channels.csv` still writes a file
   (fast, medium and slow only) to copy to the card's root or send over Wi-Fi:
   `python firmware/tools/send_channels.py <address> channels.csv`. A file sent
   that way stays until the active preset changes. Without a file the board
   reads its built-in fast and medium tiers.
3. **Flash.** `python firmware/tools/publish_firmware.py --usb COM4` (the
   board's port) builds it with a version and flashes it by USB; from then
   on, updates come over the air (below). Uploading from the PlatformIO IDE
   works too, but makes a "dev" build, which takes no updates.

Each flush of a drive's file (every 2 s) is checked on the card: a card
error leaves FatFS's open file failing every write after it (28 Oct, two
drives logged minutes into nothing), so a flush that didn't land opens the
file again and writes the rest from what the card has, noted in the drive
("card: ... write failed so opened again"); the check-ins count them
(`card_errors`, this boot). While the card won't open it, the rows wait in
memory (up to 1 MB).

Each boot it closes a drive a power cut left open, then looks for the car
(the address it had last time first, then discovery) and logs while it
answers:

- `drives/<start>.csv`: the fast and medium channels, one row a cycle (a
  medium cell is empty in cycles that didn't read it).
- `drives/<start>.sweep.csv`: the slow channels, one row each time all of
  them have come round; the periodic ones (from a minute in) in a row of
  their own once all are read; and at the end, a row with whatever came in
  since the last.

`<start>` is the local time. The clock comes from the internet when Wi-Fi
is up; when it isn't (driving away from home and the hotspot), from the
car's own clock, the airbag module's (`0x01`, identifier `0x4029`, the time
the dashboard shows, to the second; `lib/logger/carclock.h`), read once
before the drive starts. Only if neither answers is it `nt-<boot>-<n>`,
dated by the Worker from its upload.

How a drive ends (each noted in its last row's Event column, "end: <why>"):

- **Parked:** the selector in P (`EGS_ISTGANG.FAHRSTUFE`) and the engine
  off (`INMOT` under 100 rpm), both read in the last 30 s, for 60 s: "end:
  parked with the engine off for 60 s". The board stays connected and
  *parked*, asking the selector and engine speed every 2 s; the engine
  starting (400 rpm) or the selector leaving P starts a new drive. Found
  parked at power-on (after a reboot or a firmware update in the driveway),
  it waits the same way instead of starting a drive.
- **The car stops answering** for 10 s (the ignition off, the gateway
  asleep), or **the power goes** (the drive is closed and queued at the
  next boot).
- **The connection closes** and doesn't come back: the board connects
  again and carries on in the same files ("connection closed; reconnected
  in 0.6 s"), so a drive keeps one name; only if that fails for 15 s (or
  20 times in a drive) does it end. Or the **Ethernet link** goes.
- **From the terminal** (`e`), or new channels sent over it.

Snapshots: the odometer and the fuel tank, the whole (`IKTFS`) and each
half (`FUELLST_TANKH_L`/`_R`, `BDC_TANK_FUELLSTAND_LINKS`/`_RECHTS`), are
read together 5 s into a drive, whenever the selector goes into P
(`EGS_ISTGANG.FAHRSTUFE`), and when the engine stops (`INMOT` from 400 rpm
to under 100), at most one every 10 s; each is noted ("snapshot: park") and
gets a sweep row of its own. In a preset they're read once a drive or in the
slow rotation, so a drive's first and last could be one reading (the 27 Oct
7:50 drive's odometer didn't move). The Worker keeps each drive's first and
last of them (`tank_start_l`, `tank_left_start`, `odometer_end_km`, ...),
and whatever a preset says, it reads the selector, engine speed, speed,
`ITKUM` and `IUIBS` at least medium (`logger.js` `BOARD_NEEDS`). The
check-ins report the selector (`gear`: P R N D M). Whenever Wi-Fi is up, the other core uploads
finished files to `logger/` on the Worker, compares the SHA-256 the Worker
reports with the board's own, and only then deletes them. The board's own
is the `.done`'s, written as the file closed, or else that of the bytes it
just sent, all of the file (a `.done` from the power-cut recovery was once
wrong where the file wasn't). A file that fails (a 200 with another hash, or the
Worker refusing or failing on it: 400, 413, 5xx) is tried again at the next
round, and the files after it still go; after three failures it's tried
every eighth round (about 2 minutes). A failure on the way there (no memory
for TLS, no connection, the token refused: 401, 403, 408, 429), or the Worker
failing on two files running, leaves the rest for the next round. The Worker turns
each pair into one drive in History and the time series (cloud/README.md).
The backlog goes first thing at power-on (as soon as Wi-Fi is up, before
a drive needs the board), and a drive's files the moment it ends, while
there's still power; while a drive is logging, uploads are paced (about
80 KB/s) so the card and core 0 stay mostly the logging's. A firmware update
waiting on the card isn't installed while files wait to go up (for up to 10
minutes idle). The board never sleeps: how long it has after the engine
stops is down to its power, which comes with the ignition here; a supply
that stays on a minute or two after (or constant power) lets the last
drive go up before the car is left.

Each cycle sends one request to every module that has fast or medium reads
(and to slow-only modules in turn), both DMEs side by side on their own
connections. A module's request carries all its fast identifiers, then the
next medium and slow ones in rotation, up to 24 identifiers. Medium gets a
quarter of a request each cycle, or half of what the fast reads leave if
that's more, and slow the rest (at one identifier a request they take
turns). When the fast reads leave less than medium's quarter, another
request that cycle carries it; when they leave nothing beyond it, slow gets
another request every other cycle. Only modules with fast channels (the DMEs, the
gearbox) are waited for: the rest (head unit, body modules), which can take
most of a second, are sent alongside and their answers collected in
whichever cycle they come, up to six at once, given up after 3 s.

The module's error code decides what happens to a batch it won't answer:

- too long or badly formed (0x13, 0x14), or an answer out of line: the
  module's batches halve, down to one, and the halves are asked again;
- out of range (0x31): one identifier in it is one the module hasn't; the
  halves are asked again until it's found, and only that one rests (10 min);
- any other refusal: the batch's identifiers rest for a minute;
- no answer: counts against the identifiers (three in a row rest them), and
  two timeouts in a row halve the module's batches, but only while other
  modules on the connection answer. When none has answered since the
  request went, the connection stalled: nothing counts against the module
  (the stats note counts these as `stalls`). On 28 Oct stalls that hit the
  bank 1 DME, the gearbox and the head unit at the same moment took bank 1
  down to one identifier a request and the drive to a third of its rows.
- an answer that lines up with the module's previous request, the one that
  timed out, is that request's answer come late, not a sign the batch is
  too big: it's dropped and counted (`late answers`), and the batch size
  stays. Bank 1's requests all start with the same fast identifiers, so
  such an answer gets past the client, and it was halving bank 1's batches
  every few seconds after each stall.

A few identifiers go in requests of their own, every other turn of their
module (`ALONE` in `lib/logger/plan.h`): the airbag module's internal
temperature (0x4003) and supply voltage (0x4007), asked in one request,
both came back with one of the two values (1,424 "°C" while the voltage was
14.24 V).

A module whose batches halved tries twice the size again after 50 whole
answers at its current size, and needs twice as many each time the bigger
size fails again, so one answer out of line (the bank 1 DME's, on 26 Oct)
no longer costs it the rest of the drive, and a module that only takes one
identifier at a time is rarely asked for more.

A module that doesn't answer twice in a row is left alone for 2 s and
noted ("module 0x12 not answering: left alone 2 s"); each further time it
times out after one, twice as long, up to 30 s, and any answer starts it
over. Its fault and channel-check reads wait too. Every other module carries
on as before, overlapped: the board used to put every connection on one
request at a time for minutes when the waited-for requests timed out, and a
few seconds of the bank 1 DME not answering (26 and 27 Oct) cost the rest
of the drive two thirds of its rows. The stats note lists the modules left
alone.

Each minute a second note gives the rate each tier actually got ("tier
rates: fast 14.10/s medium 2.00/s slow pass 40 s": reads a second per
identifier, and the slow pass's length), so a change of preset can be
checked against reality: the Worker averages them into the drive's row
(`rate_fast`, `rate_medium`, `slow_pass_s`; MCP `drive_history`, `trend`),
and the logger page shows the last drive's beside its estimates.

Bursts: when the pedal passes 80% or the boost 1,700 hPa (`IPLAD`, about
0.7 bar over the air outside), the board asks for the fast tier alone, as
fast as it answers (up to 20 rows a second), until both have been below for
a second or for 10 s at most; the next can't start for 5 s, so the other
tiers still get through on a long pull. No fault reads meanwhile. The notes
mark each burst and its end, and the stats line counts them.

The log's `note` column records the start (with the channel file: the
preset and its ETag, `manual`, `card` or the built-in table, and where the
clock came from), a stats line every minute (rate, read time, slow pass,
resting and refused reads, requests collected later and how many timed out,
smaller batches, bursts), batch changes, refusals (once a minute per
module), bursts and the reset reason. On the bench (`--delay 30`, five
modules answering in 0.5-0.9 s, three bad identifiers and two modules that
take one identifier a request; 15 fast, 51 medium and 9,219 slow channels)
it logs 12-16 rows a second with a slow pass every 10-11 s.

From a minute into a drive the board also reads fault memory, one module
at a time between cycles: every module n63read knows on this car once a
drive (`lib/logger/faults.h`, kept equal to `core.KNOWN_MODULES` by
`tests/test_logger_faults.py`), then both DMEs every three minutes. Each read
adds rows to `drives/<start>.faults.csv` in n63read's fault-list format (plus
`time_s`), uploaded with the drive: a row per failed code, or one saying the
module has none, refused or didn't answer. Codes only; the texts and stored
details stay with the app and the Worker. A DME's fault list is waited for
(milliseconds); any other module's is collected when it comes, like its
reads, so a module that doesn't answer no longer holds up the log. Status
(`s`) and the check-ins give how many codes failed in the modules' last reads,
and which (`fault_codes`, "0x12:1A400A 0x40:804415"; the Worker adds their
texts for the logger page and `logger_status`). The codes go only when they
change, at boot, in a check-in kept offline, and every 5 minutes; the Worker
takes the power-on's newest for a check-in without them.

The same task checks in with the Worker once at power-on and every 10 s
while Wi-Fi is up: a small JSON report with the boot number and why it
booted (a brownout shows there), uptime, what it's doing, the speed, rpm,
battery voltage, coolant and oil temperature (`ITKUM`, the engine's coolant,
else `ITKUA`, the radiator outlet's; `ITOEL`) and the GPS
position when the channel table has them and has read them in the last 30 s
(null otherwise; battery is the battery sensor's `IUIBS`, in the medium tier, else the
DME's `IUADW`), files
waiting and the oldest's age (`oldest_pending_min`, from its name), the
uploads tried and failed this boot and the last failure with how long ago
(`up_tries`, `up_failed`, `up_error`, `up_error_min`), card write failures
(`card_errors`), Wi-Fi and memory, and which channel
file it's running (`channels`: the active preset's ETag, `manual` for one
sent over the terminal, `card` or `built-in`), which the logger channel page
compares with the active preset. From the last minute's stats it adds the
modules on smaller batches (`batches`, "0x12:6 0x18:1"), each collected-later
module's answer time (`answer_ms`, "0x63:170": as the cycles see it, so
never much under the reads per cycle, and every module's rises with them),
the slow pass and the reads
per cycle, which the page's rate estimates use. The check-ins (and keeping
Wi-Fi up) have a task of their own on core 0, every 10 s by the clock, so an
upload or a download doesn't hold one up; each waits for room for a TLS
session (about 40 KB of internal RAM) when the other task has one open. A
failed one is skipped. Each is a new TLS connection,
about 2 MB an hour on a phone hotspot. With no Wi-Fi, each check-in goes to
`beats.jsonl` on the card instead (made before the board knows the time,
from the internet or the car, they're held in memory, up to 15 minutes'
worth, and dated and written once it does), up to about 12
hours' worth, and at the next connection they're sent 48 KB at a time
(`POST /api/logger/heartbeats`), filed under the time each was made, and
the file deleted. `logger_status` (MCP), `GET /api/logger` and the logger
page (`/logger`: the car's state, temperatures and position) show the last
one and each power-on.

Terminal: USB at 115200, or TCP 23 over Wi-Fi as the spike's. Keys: `s`
status (rates and time per cycle), `l` files waiting, `u` upload now, `e` end
the drive, `v` the newest files' first and last lines, `C` take a channel
file (`send_channels.py` does this), `X` delete waiting files (after bench
runs; asks first), `h` help.

### Updates over the air

    python firmware/tools/publish_firmware.py

builds the logger with a version (the UTC time and the commit, e.g.
`20261027-0215-706ddb6`; uncommitted changes are refused unless
`--allow-dirty`) and publishes it to the Worker (`PUT /api/logger/firmware`,
with the Access token from `CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET`,
else the n63read app's (Settings > Cloud); the host from `--host`,
`N63_WORKER_HOST`, the app's, or `WORKER_HOST` in `logger_secrets.h`). The
Worker refuses firmware from the board's own token (the one that checks in),
so a token taken from a board can't put code on it, and refuses writes to
the firmware's R2 keys through `/api/files/`. The build never includes
`logger_secrets.h` (`LOGGER_NO_SECRETS`). The Worker keeps one build.

The board asks for it soon after Wi-Fi comes up and every 10 minutes. For a
version other than its own (an older one too: publishing the old build is
how to go back), it downloads the image to the card, during a drive as well,
and checks its size and SHA-256. It installs it into the other app slot at
the next power-on, before it looks for the car (about 10 s, once), or after
a minute idle on the bench, and restarts into it.

A new build is on trial until it checks in with the Worker or reaches the
car. If it does neither within 5 minutes, or crashes or hangs before that,
the bootloader goes back to the build before, which then won't take that
version again (publish another). An image that won't install is refused the
same way. The check-ins carry `fw` (the version) and `ota` (what the updater
is doing: downloading, downloaded, trying, went back, failed), and the
logger page shows them with the published version. A "dev" build (from the
IDE) takes no updates, so bench work isn't replaced under you.

Tested on the bench: `--usb COM4` once; then publish (it updates and keeps
the new build); publish with `--flag OTA_TEST_SILENT` (a build that never
checks in: after 5 minutes it goes back); publish a build with bytes changed
in its middle (it downloads, its hash being the one published, but the
install's image check refuses it and it stays on its build).

### Full channel sweep

"Sweep on the next drive" on the logger page asks the board to check every
identifier in the cloud's catalog once, whether the car answered it before
or not, as the app's Answers > Check car now does (0x22 only). The request
comes in a check-in's reply (`sweep`: its id) and is kept in flash; the
upload task downloads the list (`GET /api/logger/check-list`) to the card.
The next drive starts it a minute in, with every periodic tier due as well:
three identifiers a cycle on the DMEs, waited for beside the fast reads, and
one at a time, collected later, for the other modules; none during a burst,
and only while the car stands (under 3 km/h, or idling with no speed read):
on the move it cut the 27 Oct 7:50 drive to about 1.9 rows a second against
3.3-4. It pauses and carries on with a note each time. The fast tier keeps
going at a lower rate meanwhile.

The answers go into `<drive>.check.csv` (the app's check CSV), uploaded with
the drive; the Worker merges them into the catalog's answers, so whatever
answers now goes into the channel file. The place is saved every 25
identifiers: a drive that ends first notes how far it got and the next one
carries on. A module that leaves three identifiers in a row unanswered is
skipped and noted in the drive log; the check-ins carry `sweep_next`,
`sweep_of` and `sweep_silent`, and the page warns about it. Once all are
asked, `sweep_done` tells the Worker, which clears the request and shows
when it finished. `s` on the terminal says where a sweep is.

## Spike

Board: Waveshare ESP32-S3-ETH.

1. VS Code + PlatformIO extension. Open **`firmware/spike`** (the folder with
   `platformio.ini`), not the repo root.
2. Plug the board in by USB-C and Upload. The first build downloads
   Arduino-ESP32 3.3 through the pioarduino platform. If no port shows up:
   hold BOOT, tap RESET, release BOOT, upload again.
3. Serial monitor at 115200. With no cable: `[eth] W5500 up`, then `no link`
   after 15 s.

### On the bench first

With native USB the boot messages often scroll past before the monitor opens:
press `s` for status or `a` to run everything again.

1. **Board alive**: `s` shows the MAC, our address 169.254.100.100 and
   `link down`. That proves the W5500 answers over SPI.
2. **Link**: cable from the board to a home switch or router. Expect
   `[eth] LINK UP 100 Mbit/s full duplex`; unplug for `link down`. Discovery
   finds nothing on a home network, as expected. A switch fixes up crossed
   pairs on its side, so this doesn't test the car's wiring.
3. **Dry run against a bench car**: cable the board straight to the PC's
   Ethernet port (or a USB Ethernet dongle), no router. Run
   `python firmware/tools/bench_car.py` and allow Python through the Windows
   firewall on Public networks (a direct cable counts as public). Wait about
   a minute for Windows to give the port a 169.254.x.y address, then press
   `a`. Expect discovery with VIN `WBAFAKEBENCH00001`, all seven channels
   from 0x12 and 0x13 (about 745 rpm swinging, 90 / 91.5 C coolant, 12.6 V),
   then a batch rate. The bench car prints each connection and flags any
   request other than a read.

The bench rate is not the car's: the car's DMEs take a few milliseconds per
answer, the bench car none.

For the card, build with `-DLOGGER_NO_UPLOAD -DLOGGER_SD_STRESS`
(`PLATFORMIO_BUILD_FLAGS`): another task on core 0 does a check-in's card
work every 10 s and reads the finished files through again and again,
checking each against its `.done` ("matches" or "DIFFERS"), and `K` breaks
the drive file's handle under it as a card error does. Delete the bench
files afterwards (`X`, then `y`), with the bench car stopped, before a real
build goes on.

### In the car

Ignition on (engine off is fine), close n63read, ENET cable from
OBD to the board's RJ45, laptop on USB-C, press RESET. A pass, in order:

1. `[eth] LINK UP 100 Mbit/s full duplex`
2. `[disc] HSFZ reply from 169.254.x.y ... VIN ...`
3. 0x12 then 0x13: tester present, then 7 channels with value, raw hex and ms
4. `[batch] ... cycles/s (n63read: 6.2)`

Keys: `a` run all, `s` status, `d` discover, `r` read once, `b` batch test, `x` close connections,
`h` help. Type `x` before unplugging, and unplug the cable when done: it holds
pin 8 high and keeps the gateway awake. Then read the same channels with
n63read to compare (the spike uses both of the car's tester connections, so
the two can't run at once).

### Result, 2026-10-24

Passed in the car (engine idling, board on a power bank, driven over Wi-Fi):

- Link 100 Mbit/s full duplex through the ENET cable: the W5500's missing
  auto-MDIX is not a problem with this cable.
- Discovery found the gateway at 169.254.49.40 with the car's VIN.
- Both DMEs on their own connections, 17-52 ms per single read.
- Batch: 16.2 cycles/s (n63read: 6.2), 0 failures in 20 s, slowest 63 ms.
- Rail pressure 0x5621 got NRC 0x31 from both DMEs, so it's out of the
  channel list. Values not yet compared with n63read.

### Terminal over Wi-Fi

The same keys and output are on Wi-Fi, TCP port 23, so the board can sit in
the car on a USB power supply (the car's USB port or a power bank) while you
watch from the bench. Copy `spike/src/wifi_secrets.example.h` to
`wifi_secrets.h` next to it (gitignored) and fill in either or both:

- `WIFI_SSID` / `WIFI_PASS`: a network that reaches the car, e.g. home Wi-Fi
  in the garage. Then, from `firmware/spike`:
  `pio device monitor --port socket://m550-spike.local:23`
- `AP_PASS`: used when that network isn't in range at boot. The board starts
  an access point `m550-spike`; join it, then
  `pio device monitor --port socket://192.168.4.1:23`

The board decides at boot, so power it up where it will stay. Connecting
replays the last 8 KB of output (the boot run, or whatever a dropped
connection missed), then prints status and help. One terminal at a time; a
new one takes over.

There's no password: anyone on that network can connect, see the VIN and
live values, and run the tests. That's deliberate for a bench tool; the
client still sends nothing but reads and tester present.

- `.local` names don't cross VLANs: if the PC and the IoT network are on
  different subnets, use the address `s` shows (or the router's DHCP list).
  A DHCP reservation keeps it fixed.
- Closing a USB serial connection resets the board (the ESP32-S3's native USB
  treats it as a reset), and the boot run takes about 30 s, during which the
  Wi-Fi terminal accepts but doesn't answer. Not an issue in the car, where
  nothing is on USB.

Set `CAR_IP_OVERRIDE` in `spike/src/main.cpp` to skip discovery if the
broadcast fails but n63read shows the car's address.

| Symptom | Meaning |
| --- | --- |
| No link in 15 s | Pair mapping or the W5500's lack of auto-MDIX. Check the laptop gets a link on the same cable |
| Link, no discovery reply | Broadcast not leaving, or ignition off. Try `CAR_IP_OVERRIDE` |
| `gateway refused 0x0040` | The car still holds another tester session: wait a few minutes |
| 0x12 works, 0x13 connect fails | Another tool holds the second connection (the car allows two) |
| Values differ from n63read | Compare the raw hex; the channel rows come from `N63TU2_DME_readable_measurements.csv` |
