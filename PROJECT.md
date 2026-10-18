# BMW Analyzer (n63read): the original brief

This is what the project set out to do, and the rules it keeps. How it works
now - modules, protocol, decoding, data files, building - is in
[docs/](docs/README.md) and the [README](README.md); the work plan is in
[plan.md](plan.md).

## What it is for

A dependency-free Python tool that reads live engine values and fault memory
from a 2018 BMW M550i (G30, N63TU2 / N63B44O2) over the OBD Ethernet (ENET)
port, using BMW's HSFZ protocol. It started because BimmerLink does not
expose the bank 2 channels of this engine, and the first question was whether
that data could be read at all. (It can: bank 2's DME at `0x13` answers the
same identifiers as bank 1's at `0x12`; see
[docs/modules.md](docs/modules.md#bank-2-and-the-slave-dme).)

Measurement metadata comes from BMW's SGBD (`.prg`) files: the DME's
`dme8ff_r.prg` and, since, the other modules' files. Which files fit this car
is in [docs/modules.md](docs/modules.md); where each data file comes from is
in [docs/data-sources.md](docs/data-sources.md).

## Safety constraints - preserve these

The tool sends **only** UDS services `0x22` (ReadDataByIdentifier), `0x19`
(ReadDTCInformation) and `0x3E` (TesterPresent). `core.ALLOWED_SERVICES`
enforces this and `Hsfz.request()` raises on anything else, for the app, the
CLI, Claude's tools and requests lent between processes alike.

`0x19` is read-only: reportDTCByStatusMask (`0x02`), snapshots (`0x04`) and
extended data (`0x06`). It cannot clear, write or command anything; clearing
is `0x14`, which stays blocked.

**Do not relax this.** Service `0x2F` (InputOutputControl) and `0x31`
(RoutineControl) can command actuators and trigger ECU routines; `0x2E`
writes. The SGBDs define hundreds of such jobs, and they are deliberately
left out of the shipped catalogs. If a future feature needs them, it needs a
separate explicit opt-in path and a loud warning, not a widened allow-list.

Any address scan uses TesterPresent only.

## Verified decoding cases

Kept as unit tests (`tests/test_decode.py`); they need no hardware.

| ARG | ID | raw | expected |
|---|---|---|---|
| `ILAG2` | `0x588A` | `1000` | 1.0 (lambda) |
| `ILAG1` | `0x5889` | `0FA0` | 0.9765625 |
| `ILUZ5` | `0x4A34` | `FF70` | −9.0 rad/s² |
| `ILUZ5` | `0x4A34` | `0090` | +9.0 rad/s² |
| `ZAEHLER_AUSSETZERK_ZYL5` | `0x4B31` | `0002` | 2 |

## Identifiers are not contiguous

A loop that assumes sequential identifiers past cylinder 6 silently drops the
back half of the V8:

| Measurement | ARG | ID |
|---|---|---|
| Uneven running, cyl 1–6 | `ILUZ1`–`ILUZ6` | `0x4A30`–`0x4A35` |
| Uneven running, cyl 7–12 | `ILUZ7`–`ILUZ12` | `0x4A43`–`0x4A48` |
| Misfire counter, cyl 1–3 | `ZAEHLER_AUSSETZERK_ZYL1`–`3` | `0x4B23`–`0x4B25` |
| Misfire counter, cyl 4–9 | `…ZYL4`–`ZYL9` | `0x4B30`–`0x4B35` |
| Lambda actual, bank 1 | `ILAG1` | `0x5889` |
| Lambda actual, bank 2 | `ILAG2` | `0x588A` |

## Log format

A log's first column is `Time` (seconds), the same shape as a BimmerLink CSV
export, so existing analysis works unchanged. `Clock` and `Event` columns
were added later (`Clock` second, `Event` last). Don't move `Time` without a
reason.

## Licensing

MIT for this code.

The catalogs are derived from BMW's SGBD data: identifier numbers, units,
scaling factors and texts - interface facts needed to talk to hardware the
owner possesses. Do not commit BMW's `.prg` files themselves, and do not
redistribute SP-Daten. The provenance is noted in
[docs/data-sources.md](docs/data-sources.md).
