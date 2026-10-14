# m550-analyzer

`n63read` reads live engine data and fault memory from a 2018 BMW M550i (G30,
N63TU2 / N63B44O2) over the OBD Ethernet (ENET) port, using BMW's HSFZ
protocol. It is pure Python standard library, with a CLI and a Tkinter GUI,
and ships as a single Windows exe.

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

## Usage

### GUI

Run `n63read.exe`. Choose the `169.254.x.x` interface under **via**, press
**Discover**, then **Connect**. Filter the channel list, add channels to the
watch table, and press **Start logging** to write a CSV.

- **Scan modules** sweeps addresses `0x00`–`0x7F` with TesterPresent and
  lists the modules that answer.
- **Faults** reads the fault memory of every module and shows it. It uses the
  modules from the last scan, or sweeps first if there wasn't one (about a
  minute). While logging, the fault list is saved next to the log as
  `<log>_faults_<time>.csv`; otherwise you're asked where to save it.
- **Open log...** replays a CSV the GUI logged earlier through the table and
  chart at the speed it was recorded (pauses over 2 s are shortened).

- **Add all shown** adds every channel the filter shows, except private head
  unit values (Wi-Fi password and PIN, SIM identity, previous destinations,
  paired devices), which can still be added one at a time.

The chart draws the rows selected in the table, or the first six numeric
channels if none are, each on its own scale; the legend gives each line's
range. Enumerated values (GPS fix, gear) are logged and charted as numbers,
with their meaning in the Status column.

Until you connect, the table and chart run on made-up **demo data** (engine
speed, pedal, road speed, boost setpoint, bank 2 lambda, oil temperature,
and GPS fix, satellites, speed, latitude, longitude and height),
marked "DEMO DATA" on the chart. It stops the moment you press **Connect** or
open a log, and its points are dropped so they never mix with real ones.

The log is written to disk about once a second, so a crash or flat battery
loses at most a second. Changing the watched channels while logging starts a
new file (`run_2.csv`, `run_3.csv`, ...) rather than mixing column layouts in
one file. A fault read while connected shares the connection: logging slows
down during it but doesn't stop.

### Head unit and GPS

The channel list also holds the head unit's (NBT Evo, address `0x63`)
readable values, named `HU_...` and labelled "Head unit:" in the picker:
717 values from 100 identifiers, generated from `nbtevo.prg` by
`tools/hu_catalog.py`. The useful ones for driving:

| What | Channel |
|---|---|
| Latitude, longitude (degrees), height (m) | `HU_STATUS_GPS_POSITION.LATITUDE` / `.LONGITUDE` / `.HEIGHT` |
| Position valid | `HU_STATUS_GPS_POSITION.VALIDITY` (0 = valid) |
| GPS fix (searching, 2D, 3D...) | `HU_STATUS_GPS` |
| GPS speed; four wheel speeds | `HU_STATUS_SPEED.GPS_SPEED`; `.WHEEL1_SENSOR_SPEED`... |
| Satellites tracked / visible, per-satellite signal | `HU_STATUS_GPS_SATINFO.*` |
| Position accuracy (PDOP / HDOP / VDOP) | `HU_STATUS_GPS_DILUTION_OF_POSITION.*` |
| Gear direction (reverse / forward / neutral) | `HU_STATUS_DIRECTION.DIRECTION` |
| Head unit temperatures (amplifier, gyro, drive...) | `HU_STATUS_ANALOG_TEMPERATUR.*` |
| Display light sensor, backlight temperature, brightness | `HU_CID_*`, `HU_STATUS_SENSOR_WERTE.*` |
| Radio frequency, signal quality, field strength | `HU_FREQUENZ.FREQUENZ`, `HU_STATUS_ANT_QFS.*` |

Head unit channels are read alongside the DME on one connection, but not
every cycle: GPS, speed and direction refresh every second, everything else
every 10 seconds, at most four head unit reads per cycle, and the last value
fills the log in between. That keeps the DME rate up. An identifier the head
unit refuses as unsupported is not asked for again, and if the head unit
stops answering it is left alone for 30 seconds.

The GPS position comes from a hand-coded job, not a table, so its layout was
worked out by disassembling `STATUS_GPS_POSITION`; see `core.py`. It has not
been checked against the car yet: `n63read gps --watch` prints the raw bytes
next to the decoded position for that.

### CLI

```
n63read discover --bind 169.254.x.x          find the car
n63read scan --ip 169.254.x.x                which modules answer
n63read read --ip 169.254.x.x ILAG1 ILAG2    read channels once
n63read faults --ip 169.254.x.x -v           DME fault memory, decoded
n63read faults --ip 169.254.x.x --all --out faults.csv
                                             every module, saved to CSV
n63read log --ip 169.254.x.x --out run.csv ILAG1 ILAG2 ILUZ5 ILUZ7
n63read gps --ip 169.254.x.x --watch         head unit GPS fix and position
```

`read` and `log` take head unit channels (`HU_...`) too; they go to `0x63`
whatever `--target` says.

The fault CSV has one row per fault, plus a row for each module that had no
faults, refused, or didn't reply, so the file shows everything that was
checked.

`--target` selects a module (default `0x12`, master DME). The log CSV is
`Time` plus one column per channel, the same shape as a BimmerLink export.

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
.venv\Scripts\pytest
build.bat
```

`build.bat` writes `dist\n63read.exe` (GUI) and `dist\n63read-cli.exe`. The
CLI is built without `--windowed`, or it would have no stdout. CI builds both
on every push and attaches them to the release when a `v*` tag is pushed.
Expect a SmartScreen warning: the binary is unsigned.

The eight data CSVs live in `src/n63read/data/`. At runtime they are found in
the PyInstaller bundle, the package, next to the exe, or the working
directory, so an updated CSV can be dropped next to the exe without
rebuilding.

`PROJECT.md` has the protocol notes, data file descriptions and design brief.

## Data provenance

The CSVs were extracted from BMW SGBD (`.prg`) files, mainly `dme8ff_r.prg`;
the two `NBTEVO_*` files from `nbtevo.prg`, by `tools/hu_catalog.py`.
They hold identifier numbers, units and scaling factors: the interface facts
you need to talk to hardware you own. BMW's `.prg` files and SP-Daten are not
in this repo and must not be committed.

The English text columns are a mechanical glossary translation, generated
by `tools/translate.py` from `tools/glossary.py`. German is the source of
truth. To fix a translation, add the term to the glossary and run
`python tools/translate.py`; don't edit the English columns by hand.

## License

MIT for the code.
