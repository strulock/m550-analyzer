# n63read — BMW N63TU2 DME reader (ENET / HSFZ)

Task brief for setting this up as a repo and producing a Windows executable.

## What this is

A dependency-free Python tool that reads live engine measurements from a
2018 BMW M550i (G30, N63TU2 / N63B44O2) over the OBD Ethernet (ENET)
connection, using BMW's HSFZ protocol. It exists because BimmerLink does
not expose Bank 2 channels for this DME, and the goal is to determine
whether that data is readable at all.

Two entry points:

- `n63read.py` — CLI (discover / scan / read / log)
- `n63gui.py` — Tkinter GUI (connection, channel picker, live table, strip
  chart, CSV logging)

Measurement metadata comes from `N63TU2_DME_readable_measurements.csv`,
extracted from BMW's SGBD file `dme8ff_r.prg` (the master DME; the slave
`dme8ff_l.prg` carries a near-identical table) — 1,845 identifiers with
units, data types, and MUL/DIV/ADD scaling. Every other data file below
comes from `dme8ff_r.prg` too, except the fault texts, which merge several
modules.

## Data files

All generated from BMW SGBD (`.prg`) files by XOR-0xF7 deobfuscation and
table extraction. Every text column exists in German (as stored) and English
(mechanically translated — see *Translation* below).

```
N63TU2_DME_readable_measurements.csv  1,845 live measurements (service 0x22)
BMW_fault_code_texts.csv              5,680 fault codes, DME/BDC/ZGW/cluster
BMW_dtc_status_byte.csv               92 statusOfDTC byte values
BMW_freeze_frame_fields.csv           204 environmental-condition fields
BMW_enum_tables.csv                   162 named value→text tables, 1,448 entries
G30_module_sgbd_map.csv               13 confirmed G30 module→SGBD rows
```

### Fault code texts

Merged from `dme8ff_r`, `dme8ff_l`, `BDC`, `BDC_G05`, `BDC_G11`, `ZGW_01`,
`ZGW_SP18`, `KOMB_G11`, `KOMB01`, `KOMB25`. The `source_module` column names
which file(s) a code came from. Where variants disagree, prefer the one
matching the G30 map.

Coverage gaps: ACSM (0x01), ARS (0x04 / 0x25), transfer box (0x19),
PMA (0x2C), headunit (0x63). Those SGBDs have not been extracted.

### DTC status byte

Service `0x19` returns each DTC with an 8-bit status. Decode it or you cannot
distinguish an active fault from a healed one. The table maps the byte to
BMW's own wording, e.g. `0x05` = fault currently present and already stored,
`0x04` = not currently present but already stored.

### Freeze-frame fields

`FS_LESEN_DETAIL` returns the conditions at the moment a fault set. Fields
carry the same `MUL` / `DIV` / `ADD` scaling as live measurements, so the
existing decoder applies unchanged. `0x1700` KM_STAND (odometer) and `0x1701`
ABS_ZEIT (absolute time) are the two that matter most for correlating a
fault against a drive.

### Enumeration tables

Status and mode channels return bare integers; these tables carry the text.
162 tables, 1,448 entries, each with its real SGBD name.

A measurement's `RES_TABELLE` column names its table directly, so the decoder
can resolve a raw value to text with a dictionary lookup. All 168
table-referencing measurements in the catalog resolve.

**Match table names case-insensitively.** The SGBD directory stores them
uppercased (`RES_0X1061_R`) while `SG_Funktionen` references them in mixed
case (`RES_0x1061_R`). A case-sensitive lookup silently resolves nothing.

Decode order for a measurement with a `RES_TABELLE`: apply MUL/DIV/ADD as
usual, then look the result up in the table; fall back to the number if
there is no matching row.

`sgbd_tables.py` carries the directory parser — see *SGBD table directory*.

### Translation

English columns are produced by longest-match-first term substitution using
the ~400-entry BMW diagnostic glossary in `tools/glossary.py`, not by a
translation service. Output is
accurate on formulaic text (the large majority — "short circuit to supply
voltage", "message X missing, receiver Y, sender Z") and degrades on prose.
German is the source of truth; when a row reads oddly in English, check the
German column. The glossary ships so the English is reproducible and
correctable — add a term and regenerate. Switching to a real translation API
would improve the long-prose rows and is worth doing if the tool ever ships
publicly.

## ECU jobs

From the DME SGBD. Read-only:

`FS_LESEN`, `FS_LESEN_DETAIL`, `FS_LESEN_PERMANENT`, `IS_LESEN`,
`IS_LESEN_DETAIL`, `STATUS_LESEN`, `STATUS_BLOCK_LESEN`, `SVK_LESEN`,
`CBS_INFO`, `CBS_DATEN_LESEN`, `SENSOREN_IDENT_LESEN`, `HERSTELLINFO_LESEN`,
`SERIENNUMMER_LESEN`, `PRUEFSTEMPEL_LESEN`.

State-changing — keep blocked:

`FS_LOESCHEN`, `IS_LOESCHEN`, `FS_SPERREN`, `STEUERN`, `STEUERN_IO`,
`STEUERN_ROUTINE`, `STEUERGERAETE_RESET`, `CBS_RESET`,
`PRUEFSTEMPEL_SCHREIBEN`.

`STATUS_BLOCK_LESEN` is worth implementing: it reads several channels per
round trip instead of one, which matters for live logging rates.

## Existing files (already written — do not regenerate)

```
n63read.py                              CLI + protocol core
n63gui.py                               Tkinter GUI
sgbd_tables.py                          SGBD table directory parser (tools/)
glossary.py                             DE→EN term table (tools/)
N63TU2_DME_readable_measurements.csv    measurement catalog (1,845 rows)
BMW_fault_code_texts.csv                fault code catalog (5,680 rows)
BMW_dtc_status_byte.csv                 statusOfDTC decoder (92 rows)
BMW_freeze_frame_fields.csv             FS_LESEN_DETAIL field catalog (204)
BMW_enum_tables.csv                     value→text enumerations (1,448)
G30_module_sgbd_map.csv                 module address → SGBD file (13)
```

`n63gui.py` imports `n63read` and resolves data files through
`n63read.resource_path()`.

## Target repo layout

```
n63read/
├── README.md
├── LICENSE                 MIT
├── .gitignore              Python + PyInstaller
├── pyproject.toml          metadata only; no runtime dependencies
├── build.bat               one-shot Windows build
├── src/
│   └── n63read/
│       ├── __init__.py
│       ├── core.py         from n63read.py
│       ├── cli.py          argparse entry point
│       ├── gui.py          from n63gui.py
│       └── data/
│           └── N63TU2_DME_readable_measurements.csv
└── .github/
    └── workflows/
        └── build.yml       build the exe on windows-latest, attach to release
```

If restructuring into `src/` causes friction, keeping the files flat at the
repo root is acceptable — the tool works either way. The only hard
requirement is that the six data CSVs are locatable at runtime both when
running from source and when frozen; `n63read.resource_path()` already
handles both cases plus the exe's own folder and the working directory.

## Requirements

- Python 3.9+
- **No third-party runtime dependencies.** Standard library only
  (`socket`, `struct`, `csv`, `argparse`, `threading`, `queue`, `tkinter`).
  Keep it that way — this runs on a laptop in a car.
- PyInstaller is a build-time dependency only.

## Resource path handling

Implemented as `n63read.resource_path(name)`, used by the CLI and the GUI.
Search order: PyInstaller's `sys._MEIPASS`, the module's own directory, the
executable's folder, then the working directory — so a user can drop an
updated CSV next to the exe without rebuilding. The file names are listed in
`n63read.DATA_FILES`.

## Build

```bat
pip install pyinstaller
pyinstaller --onefile --windowed ^
  --name n63read ^
  --add-data "N63TU2_DME_readable_measurements.csv;." ^
  --add-data "BMW_fault_code_texts.csv;." ^
  --add-data "BMW_dtc_status_byte.csv;." ^
  --add-data "BMW_enum_tables.csv;." ^
  --add-data "BMW_freeze_frame_fields.csv;." ^
  --add-data "G30_module_sgbd_map.csv;." ^
  n63gui.py
```

Notes:

- `--windowed` suppresses the console for the GUI. If you also ship the CLI,
  build it separately **without** `--windowed`, or it will have no stdout.
- On non-Windows hosts the `--add-data` separator is `:` rather than `;`.
- All six CSVs must be bundled. Missing lookup files degrade gracefully
  (`faults` prints raw codes) rather than crashing, which makes an omission
  easy to miss — check `dist/` output against `n63read.DATA_FILES`.
- Output lands in `dist/n63read.exe`.
- Expect SmartScreen warnings on an unsigned binary. Code signing is out of
  scope.

`build.bat` should wrap the above and exit non-zero on failure.

## GitHub Actions

Build on `windows-latest`, Python 3.12, no dependency install beyond
PyInstaller. Upload `dist/n63read.exe` as a workflow artifact, and attach it
to the release when the trigger is a tag push.

## README content

Cover, concisely:

1. **What it does** and why (BimmerLink's Bank 2 gap).
2. **Hardware** — a USB ENET adapter (presents as a network interface, not a
   COM port) or a WiFi ENET adapter.
3. **Network setup** — the car runs no DHCP server. Leave the adapter on
   automatic and Windows self-assigns a 169.254.x.x link-local address after
   30–60 s; or set a static 169.254.100.100 / 255.255.0.0 with no gateway.
   Ignition to position 2.
4. **The broadcast gotcha** — discovery broadcasts leave via the default
   route, so with WiFi up the request never reaches the car. Use the `via`
   dropdown in the GUI or `--bind` on the CLI to pin it to the 169.254.x.x
   interface. Also: set that network to Private and allow the binary through
   Windows Firewall, or the reply is dropped.
5. **Usage** — the five CLI subcommands (`discover`, `scan`, `read`,
   `faults`, `log`) and the GUI workflow.
6. **Safety** — see below. Put this above the usage section, not buried.

## Safety constraints — preserve these

The tool sends **only** UDS services `0x22` (ReadDataByIdentifier), `0x19`
(ReadDTCInformation) and `0x3E` (TesterPresent). `core.ALLOWED_SERVICES`
enforces this and `Hsfz.request()` raises on anything else.

`0x19` is read-only and is implemented in `Hsfz.read_dtcs()` using
subfunction `0x02` (reportDTCByStatusMask), surfaced as the `faults`
subcommand. It cannot clear, write, or command anything — clearing is `0x14`,
which stays blocked.

**Do not relax this.** Service `0x2F` (InputOutputControl) and `0x31`
(RoutineControl) can command actuators and trigger ECU routines; `0x2E`
writes. The source SGBD defines 248 such entries, and they are deliberately
excluded from the shipped CSV. If a future feature needs them, it needs a
separate explicit opt-in path and a loud warning, not a widened allow-list.

Any address scan should use TesterPresent only.

## Protocol notes (for anyone touching `core.py`)

BMW F/G series use **HSFZ**, not ISO 13400 DoIP. Differences that matter:

| | HSFZ | DoIP |
|---|---|---|
| TCP port | 6801 | 13400 |
| Discovery | UDP broadcast 6811 | UDP 13400 |
| Header | 4-byte length + 2-byte control | 8 bytes |
| Addressing | 1 byte | 2 bytes |
| Routing activation | not required | required |

Frame: `[length:4 BE][control:2 BE][source:1][target:1][uds payload]`.
`length` counts source and target plus payload (payload length + 2).

Control words: `0x0001` diagnostic request/response, `0x0002` acknowledge
(the gateway echoes the request — skip it), `0x0011` vehicle identification.

Tester source address `0xF4`. Known targets: `0x12` master DME, `0x40` BDC,
`0x10` central gateway, `0x60` instrument cluster.

A module's variant can be identified at runtime: `22 F1 50` returns its SGBD
index, which looks up in `G30_module_sgbd_map.csv`. Confirmed for this car:
cluster `0x60` → index `0F24C0` → `KOMB_G11`.

Handle `0x7F <service> 0x78` (responsePending) by continuing to read rather
than erroring. A `0x7F ... 0x31` (requestOutOfRange) means the identifier
isn't implemented on that module — this is a meaningful result, not a
failure, and the GUI surfaces it in the status column.

## Decoding

Each catalog row carries `DATENTYP` (width and signedness), `L/H` (byte
order; `High` means MSB first), and `MUL` / `DIV` / `ADD`:

```
value = int.from_bytes(raw[:width], order, signed) * MUL / DIV + ADD
```

Unknown or non-scalar types (`data[n]`, strings) return hex rather than a
guessed number.

Verified cases to keep as unit tests:

| ARG | ID | raw | expected |
|---|---|---|---|
| `ILAG2` | `0x588A` | `1000` | 1.0 (lambda) |
| `ILAG1` | `0x5889` | `0FA0` | 0.9765625 |
| `ILUZ5` | `0x4A34` | `FF70` | −9.0 rad/s² |
| `ILUZ5` | `0x4A34` | `0090` | +9.0 rad/s² |
| `ZAEHLER_AUSSETZERK_ZYL5` | `0x4B31` | `0002` | 2 |

Add a `tests/` directory with these as pytest cases. They need no hardware.

## Channel identifiers worth knowing

Note the blocks are **not contiguous** — a loop that assumes sequential
identifiers past cylinder 6 silently drops the back half of a V8.

| Measurement | ARG | ID |
|---|---|---|
| Uneven running, cyl 1–6 | `ILUZ1`–`ILUZ6` | `0x4A30`–`0x4A35` |
| Uneven running, cyl 7–12 | `ILUZ7`–`ILUZ12` | `0x4A43`–`0x4A48` |
| Misfire counter, cyl 1–3 | `ZAEHLER_AUSSETZERK_ZYL1`–`3` | `0x4B23`–`0x4B25` |
| Misfire counter, cyl 4–9 | `…ZYL4`–`ZYL9` | `0x4B30`–`0x4B35` |
| Lambda actual, bank 1 | `ILAG1` | `0x5889` |
| Lambda actual, bank 2 | `ILAG2` | `0x588A` |

## Log output format

The logger writes `Time` (seconds, float) plus one column per channel —
deliberately the same shape as BimmerLink CSV exports, so existing pandas
analysis works unchanged. Don't change this without a reason.

## Licensing

MIT for this code.

The measurement CSV is derived from BMW's SGBD data. It contains identifier
numbers, units, and scaling factors — interface facts needed to communicate
with hardware the owner possesses. Do not commit BMW's `.prg` files
themselves, and do not redistribute SP-Daten. Note the provenance in the
README and leave it there.

## SGBD table directory

Tables inside a `.prg` are not self-describing — the data blocks sit
back-to-back with no names. A directory elsewhere in the file maps names to
offsets. Entry layout, little-endian:

```
[offset:u32][0:u32][cols:u32][rows:u32][name:NUL-terminated, zero-padded]
```

`offset` points at the table's column-header row; the body follows as
`cols × rows` NUL-terminated fields. Locate entries by scanning for
identifier-like strings and reading the 16 bytes before each, rejecting any
where the second word is non-zero or `cols`/`rows` are out of range.

The DME SGBD yields 753 tables this way, including `SG_FUNKTIONEN` itself,
289 `RES_*` result tables, and 162 `WERT`/`TEXT` enumerations.

`sgbd_tables.py` implements `table_directory(data)` and
`read_table(data, off, cols, rows)`. It is pure stdlib and lives under
`tools/` — a build-time extractor, not part of the runtime.
