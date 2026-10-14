# SGBD (`.prg`) format

An SGBD ("Steuergeräte-Beschreibungsdatei", control unit description file)
is BMW's per-module diagnostic definition, run by EDIABAS (the runtime under
ISTA, E-Sys, INPA). It contains **tables** (what can be read, units, scaling,
value texts, fault texts) and **jobs** (compiled bytecode that builds a
request, sends it, and turns the reply into named results).

They live in an EDIABAS install's `Ecu` folder (`C:\EDIABAS\Ecu\`). They are
BMW's and are **not** committed here; only the facts extracted from them are.

Tooling: `tools/module_catalog.py` (tables → catalogs, fault texts),
`tools/sgbd_tables.py` (the original directory reader, used for the DME).

## Obfuscation

Every byte is XOR `0xF7`. Undo that first; everything below refers to the
de-obfuscated bytes.

```python
data = bytes(b ^ 0xF7 for b in open(path, "rb").read())
```

## Identifying a file

Job and result metadata are stored as text, so the module's own description
can be read directly. Search for `ECU:` followed by `ORIGIN:` on the next line
(other text also contains `ECU:`, e.g. "returnControlToECU"), and
`REVISION:`:

```
ECU:Motorelektronik ME VD17.2   8 Zylinder N63 TÜ Master
REVISION:12.002
```

| File | `ECU:` line |
|---|---|
| `nbtevo` | Next Big Thing Evolution |
| `gszfb1` | Getriebesteuergerät 8HPxx |
| `bdc_g11` | Body Domain Controller |
| `dsc_g30n` | Dynamische Stabilitätskontrolle EBC460 SP2018 |
| `acsm5` | Airbag-Elektronik |
| `eps_g11` | Elektrische Lenkung |
| `vdp_g11` | Vertikal Dynamik Plattform |
| `lmv_g11` | Längsmomentverteilung |
| `ihka_g11` | Klimaautomatik |
| `ekp301` | Steuergerät Kraftstoffpumpe |
| `n63tu_r0` | Motorelektronik ME VD17.2 8 Zylinder N63 TÜ Master (rejected) |
| `ekpm60_3` | EKPM2-Modul (E60/PL2) (rejected) |

### Finding a module's address

Fault texts (`FORTTEXTE`) include a generic *"Energiesparmode aktiv"*
(energy-saving mode active) code of the form `0x02AA00`, where `AA` is the
module's diagnostic address. It held for every file checked: DME `021200`,
gateway `021000`, ACSM `020100`, transmission `021800`, DSC `022900`, EPS
`023000`, VDP `027600`, IHKA `027800`, LMV `021900`, fuel pump `021700`,
park assist `022C00`.

## Tables

### The table directory

Table bodies sit back to back without names; a directory maps names to
offsets. Each directory entry is 80 bytes, little-endian:

```
[name: 64 bytes, NUL-padded][offset: u32][0: u32][cols: u32][rows: u32]
```

`offset` points at the table's header row. The body that follows is
`cols × (rows + 1)` NUL-terminated strings: one header row of column names,
then the rows.

> **Directory pitfall.** In every file extracted in this project (`nbtevo`,
> `gszfb1`, `acsm5`, `dsc_g30n`, `bdc_g11`, `eps_g11`, `vdp_g11`, `lmv_g11`,
> `ihka_g11`, `ekp301`) the four numbers **follow** the name. The original
> `tools/sgbd_tables.table_directory()` reads the 16 bytes **before** each
> name, which works if the numbers precede it. On these files that pairs
> every table with its **neighbour's** contents: `RES_0XD031_D` showed the
> speed fields that belong to `0xD030`, and `TGPSSTATUS` showed gear
> positions. `module_catalog.tables()` reads the numbers after the name and
> requires the name field's padding to be zero.
>
> The DME catalog came from `sgbd_tables.py` and is verified on the car, so
> either `dme8ff_r` is laid out the other way or its extraction is correct
> for another reason. Before trusting either reader on a new file, look at a
> table whose contents you can judge, such as a value table.

### `SG_FUNKTIONEN`: what can be read

One row per job/identifier. The columns that matter:

| Column | Meaning |
|---|---|
| `ARG` | job / channel name, e.g. `STATUS_SPEED` |
| `ID` | the identifier (DID), e.g. `0xD030` |
| `RESULTNAME` | name of the value, for a single-value identifier |
| `INFO` | German description |
| `EINHEIT` | unit (`°C`, `km/h`, `0-n` for enumerated, `HEX`, `TEXT`) |
| `L/H` | byte order: `high` = most significant byte first |
| `DATENTYP` | `unsigned char`/`int`/`long`, `signed ...`, `string`, `data[n]`, `BITFIELD` |
| `NAME` | the value table for an enumerated value |
| `MUL` / `DIV` / `ADD` | scaling |
| `SERVICE` | UDS services: `22` read; `2E` write; `2F` control; `31` routine; combinations like `22;2E` |
| `RES_TABELLE` | if set, the reply is a **record**: this `RES_...` table lists its fields in order |
| `ARG_TABELLE` | arguments for write/control jobs (never used here) |

Only rows whose `SERVICE` includes `22` are catalogued.

### `RES_...` tables: multi-value records

Columns `RESULTNAME, EINHEIT, L/H, DATENTYP, MASKE, NAME, MUL, DIV, ADD,
INFO`. One row per field, **in reply order**. Fields are consecutive: each
starts where the previous one ended. A bitfield field has `RESULTNAME` `-`
and names its bit table in `NAME`.

### Value tables

Columns `WERT` (value, `0x..` or decimal) and `TEXT`. Referenced from a
channel's `NAME`. Directory names are upper-cased while `SG_FUNKTIONEN`
references them in mixed case, so match case-insensitively, and compare
values as integers.

### `FORTTEXTE`: fault texts

`ORT` (the code, `0x` + 6 hex digits), `ORTTEXT` (German text), and more.
Other fault tables (`FUMWELTTEXTE` environment conditions, `FARTTEXTE` fault
types, `IORTTEXTE` info memory) aren't used yet.

## Jobs and hand-coded values

Some values are produced by **jobs** that aren't table-driven: the head
unit's GPS position, and everything on the fuel pump (`ekp301` has no
`SG_FUNKTIONEN` at all). To catalogue them, the job's bytecode was
disassembled to find the request it sends and which reply bytes become which
result.

### The job directory

Entries of 68 (`0x44`) bytes:

```
[job name: 64 bytes, NUL-padded][code offset: u32]
```

Find them by scanning for NUL-padded names whose entry is followed (or
preceded) by another entry exactly `0x44` bytes away. A job's code runs from
its offset to the next job's.

### BEST/2 bytecode

EDIABAS jobs are compiled BEST/2. Each instruction:

```
[opcode: u8][modes: u8][operand 1][operand 2]
```

The mode byte's high nibble describes operand 1, the low nibble operand 2:

| Mode | Operand | Bytes |
|---|---|---|
| 0 | none | 0 |
| 1–4 | register | 1 |
| 5 / 6 / 7 | immediate 8 / 16 / 32-bit | 1 / 2 / 4 |
| 8 | immediate string: `u16` length + bytes | 2 + n |
| 9 | `reg[imm16]` | 3 |
| 10 | `reg[reg]` | 2 |
| 11 | `reg[reg + imm16]` | 4 |
| 12 | `reg[imm16] : imm16` (slice) | 5 |
| 13 | `reg[imm16] : reg` | 4 |
| 14 | `reg[reg] : imm16` | 4 |
| 15 | `reg[reg] : reg` | 4 |

Opcodes follow the ediabaslib order: `0x00 move`, `0x01 clear`, `0x02 comp`,
`0x03 subb`, `0x04 adds`, `0x05 mult`, `0x06 divs`, … `0x2A xsend`, …
`0x34 ergb` … `0x39 ergs` … `0x7E strcat` …. A disassembler of about 40
lines is enough. Decoding stays aligned through a whole job, and that is
the check that the mode table is right.

### Reading a job

1. **The request** is an immediate string moved into the send buffer. Search
   the job's code for `03 00 22 hi lo`: a 3-byte string starting with service
   `0x22`.
2. **The reply** lands in a register (here `r1f`) as the full UDS response,
   `62 <DID hi> <DID lo> data...`. Jobs keep a cursor (here `r1c[2]`) set to
   3, past that header, and read bytes as `move r00, r1d[r19]` after
   `adds r18, r19` with a constant. **Byte offset in the reply = constant;
   payload offset = constant − 3.**
3. **The result** is named by the string in the next `ergs` / `ergb`
   (`STAT_GPS_POSITION_LATITUDE`), and the arithmetic in between gives the
   scaling: `divs` / `mult` by 10 for one decimal place, `subb #50` for a
   −50 offset, units appended as text (`" V"`, `" A"`, `" °C"`).

### Head unit GPS position: `STATUS_GPS_POSITION`

Sends `22 D0 39`. Payload (after the 3-byte header):

| Offset | Field |
|---|---|
| 0 | validity: `0x00` valid, `0x01` not valid (value table `TGpsPositionValidity`) |
| 1–5 | latitude: hemisphere (`0x00` N / `0x01` S), degrees, minutes, seconds, hundredths of a second |
| 6–10 | longitude: hemisphere (`0x00` E / `0x01` W), degrees, minutes, seconds, hundredths |
| 11–12 | height, unsigned big-endian; unit not stated, presumably metres |

Decoded as `deg + min/60 + (sec + hundredths/100)/3600`, negative for S/W
(data type `gps_dms`). The fix status is a separate table-driven identifier,
`0xD038` (`HU_STATUS_GPS`): searching, 2D, 3D....

Its neighbour `STATUS_GPS_TIME` sends `22 D0 33` (GPS date/time); not
catalogued.

### Fuel pump: `STATUS_ADC_BEWERTET`

Sends `22 40 10`. The reply is grouped in threes; the job uses one scaled
byte from each group, at these payload offsets:

| Offset | Value | Scaling |
|---|---|---|
| 2 / 5 / 8 | pump motor voltage, phases A / B / C | × 0.1 V |
| 11 / 32 / 44 | pump motor current, phases A / B / C | × 0.1 A |
| 14 / 29 / 41 | temperatures A / B / C | − 50 °C; a raw 0 means no value |
| 17 | tank pressure sensor (TDS) voltage | × 0.1 V |
| 20 | battery voltage at the pump (terminal 30) | × 0.1 V |
| 35 / 38 | tank level senders 1 / 2 | × 0.1 V |

Because the job skips bytes, these rows carry an explicit `POS` in the
catalog (see [Decoding](decoding.md#explicit-positions)). The other two
bytes of each group are presumably raw ADC readings (`STATUS_ADC_ROHWERTE`,
the job's raw twin, reads the same identifier).

## Generating a catalog from a new file

1. Add the file to `MODULES` in `tools/module_catalog.py`: catalog name,
   address, channel prefix, fault-file tag.
2. `python tools/module_catalog.py path\to\file.prg`
3. Add the name to `core.MODULE_CATALOGS`, `MODULE_LABELS`/`KNOWN_MODULES`,
   the loop in `build.bat`, and `COLUMNS` in `tools/translate.py`.
4. `python tools/translate.py`, then `python tools/channel_docs.py`.
5. In the car: `n63read check --module 0xAA`.
