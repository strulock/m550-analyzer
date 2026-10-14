# Decoding

How a reply's bytes become the value in the table, the log and the chart.
Code: `core.decode`, `core.decode_record`, `core.field_size`,
`core.enum_text`.

## Scalar values

A catalog row gives the data type, byte order and scaling:

```
raw    = int.from_bytes(reply[:width], "big" if L/H == "high" else "little", signed)
value  = raw * MUL / DIV + ADD
```

| `DATENTYP` | Width | Signed |
|---|---|---|
| `unsigned char` / `signed char` | 1 | no / yes |
| `unsigned int` / `signed int` | 2 | no / yes |
| `unsigned long` / `signed long` | 4 | no / yes |
| `int` | 2 | yes (EDIABAS shorthand) |
| `motorola float` / `intel float` | 4 | IEEE 754 float, big- / little-endian, then scaled |
| `string`, `string[n]` | to NUL / n | text, Latin-1, trimmed |
| `data[n]` | n | shown as hex |
| `gps_dms` | 5 | GPS coordinate, see below |
| `BITFIELD` | unknown | not decoded |

`-` in `MUL`/`DIV`/`ADD` means 1 / 1 / 0. A `DIV` of 0 is treated as 1.
Anything not decodable is returned as hex rather than a guessed number.

Verified on the car (these are unit tests):

| Channel | ID | Raw | Value |
|---|---|---|---|
| `ILAG2` lambda bank 2 | `0x588A` | `10 00` | 1.0 |
| `ILAG1` lambda bank 1 | `0x5889` | `0F A0` | 0.9765625 |
| `ILUZ5` uneven running cyl 5 | `0x4A34` | `FF 70` | −9.0 rad/s² |
| `ILUZ5` | `0x4A34` | `00 90` | +9.0 rad/s² |
| `ZAEHLER_AUSSETZERK_ZYL5` misfires cyl 5 | `0x4B31` | `00 02` | 2 |

Identifier blocks are **not contiguous**: cylinders 7–8 of the V8 live in a
separate range (`ILUZ7` is `0x4A43`, not `0x4A36`), so never derive an
identifier by counting.

## Value tables (enumerated values)

If a row's `NAME` names a value table, the scaled number is looked up in it
(`core.enum_text`): `HU_STATUS_GPS` 12 → "3D position". The app keeps the
**number** in logs and charts and shows the **text** in the Status column;
the CLI `read` prints the text. Unmatched values fall back to the number.

Lookups are case-insensitive and compare values as integers (`0x01`, `1` and
`0x0001` are the same). Module tables are prefixed with the module
(`NBTEVO.TGPSSTATUS`) so different modules' same-named tables never collide.

All value tables are written out on each module's
[values page](channels/README.md).

## Multi-value records

Many identifiers return several values in one reply: `0xD030` on the head
unit returns four wheel speeds, a combined speed, GPS speed and their
difference. In a module catalog each value is its own row with the same `ID`
and a `FIELD` number giving its place, and is named `PREFIX_JOB.RESULT`
(`HU_STATUS_SPEED.GPS_SPEED`).

`decode_record()` walks the fields in `FIELD` order, each starting where the
previous ended. So decoding a field needs the size of **every field before
it**:

- an unsized `string` runs to (and includes) its NUL, or the end of the reply;
- a field of unknown size (`BITFIELD`) ends what can be decoded: it and every
  later field read "layout unknown";
- a reply that's too short gives "short reply" for the fields it lacks.

One read fills every field, so watching three values of the same record
costs one request.

The DME catalog doesn't expand its records yet: its 168 identifiers with a
`RES_TABELLE` are shown as hex. Expanding them the same way is a known gap.

### Explicit positions

A hand-decoded job can take bytes that aren't consecutive (the fuel pump
reads one byte of each 3-byte group). Such rows carry a `POS` column giving
the byte offset in the payload; fields without one follow the previous field.
With any `POS`, the expected reply size is only a minimum.

### Expected size and the catalog check

`core.expected_size(fields)` gives the reply length a record should have and
whether it is exact. It is a minimum when the record has an unsized string,
an unknown-size field, or explicit positions. `check_identifier()` compares a
real reply with it: `ok` (fits), `size` (answered, wrong size), `refused`,
`no answer`. See [Modules](modules.md#is-this-the-right-file) for the
per-module verdict.

## GPS coordinates (`gps_dms`)

Five bytes: hemisphere, degrees, minutes, seconds, hundredths of a second.

```
value = deg + min/60 + (sec + hundredths/100) / 3600
negative if hemisphere byte == 0x01 (south / west)
```

Shown to 6 decimal places (`%.4g` would round a coordinate to ~10 km). The
layout comes from disassembling the head unit's `STATUS_GPS_POSITION` job;
see [SGBD format](sgbd-format.md#head-unit-gps-position-status_gps_position).

## Function groups

The picker's *Group by Function* and the [channel lookup](channels/README.md)
sort channels into about 20 groups (GPS & navigation, Gearbox & driveline,
Brakes & tires, Lambda & exhaust, Ignition & misfire, Fuel, Electrical &
battery...) with keyword rules in `core.FUNCTION_GROUPS`, matched against the
code name and the German and English descriptions. **First match wins**,
so specific groups come first: gearbox oil temperature is Gearbox, not Oil
or Temperatures.

A record is grouped as a whole, by its prefixed job name
(`DSC_RDC_MESSDATENBLOCK_1` → Brakes & tires); field descriptions are used
only when the name matches nothing. A field's text can mislead: "wheel
electronics *Identifier*" would otherwise send tire pressures to Versions.
Module prefixes are keywords too (`EGS_` → Gearbox, `EPS_`/`VDP_` → Steering
& suspension), so most of a module lands in one group.

The groups are a guide. About 1,000 of the ~9,900 channels land in Other,
and some land oddly. *Group by Module* is exact.

## Private values

`core.is_sensitive()` flags credentials and identity: Wi-Fi hotspot password
and PIN, the SIM's IMEI/ICCID, cookies, previous navigation destinations (they
include home), paired phone and Wi-Fi device addresses, the service partner.
*Add group*, *Add all shown* and the check's saved raw bytes leave them out;
they can still be added one at a time.
