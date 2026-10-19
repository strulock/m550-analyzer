# In-car checklist

What has been confirmed on the car, what hasn't, and the session that
settles it. Record results at the bottom.

## Status

| Item | Status |
|---|---|
| ENET connection and DME reads | working on the car |
| DME catalog (`dme8ff_r`) | verified: lambda bank 1/2, uneven running, misfire counters |
| Instrument cluster identification (`22 F1 50` at `0x60` → `KOMB_G11`) | confirmed on the car |
| Module scan and fault read across modules | **working** (2026-10-18): 36 modules found and read at the default probe wait |
| Head unit catalog and GPS position layout | **GPS verified** (2026-10-18). GPS speed turned out to be m/s; corrected. Rest of the catalog unverified |
| Transmission, transfer case, DSC, steering, body, suspension, climate, airbag, fuel pump catalogs | **fit** (2026-10-18), all but the fuel pump, which doesn't answer at `0x17` |
| Fuel pump byte layout | **unverified**: decoded from bytecode |
| Head unit is NBT Evo (not ENAVEVO as the G30 map lists at `0x63`) | **confirmed** (2026-10-18): its HMI version reads `NBTevo_ID5_1911`, and the NBT Evo catalog fits |
| BDC is `BDC_G11` at this car's software level (SP2018) | **confirmed** (2026-10-18): 230 of 230 answers the expected size |
| DME logging rate with module channels added | **measured**: one row every 1.9 s with 95 channels (about 20 ms a read), one at a time. Batching not yet tried (`n63read speed`) |

## The session

Ignition on (position 2), parked, ENET adapter connected, on battery charger
if possible (a full check takes a few minutes).

0. **If anything won't connect: run the connection check.** In a terminal
   (as administrator, for the packet capture):
   `n63read-cli.exe diag --capture`. It says step by step what it found and
   ends with a verdict. Then `n63read-cli.exe diag --report report.jsonl`
   and send `report.jsonl` plus the `.pcapng` from
   `%APPDATA%\n63read\diagnostics\`. The GUI records the same journal on
   every Discover and Connect anyway (Diagnostics ▾ → Save report...).
1. **Check every catalog.**
   `n63read check --ip 169.254.x.x --out check.csv`, or in the GUI:
   connect, then *Answers ▾ > Check car now*. Note each module's verdict:
   *fits* / *does NOT fit* / *unclear* / *no identifiers answered*. Keep
   `check.csv`: it holds every raw reply (private values excluded), so
   decoding can be checked and fixed afterwards without the car. In the GUI,
   import it with *Answers ▾ > Import n63read check CSV...* if it was made
   with the CLI.
2. **GPS.** Outdoors with a clear sky:
   `n63read gps --ip 169.254.x.x --watch`. Compare latitude/longitude with a
   phone. Expected: within a few metres, fix "3D position". Wrong
   hemisphere, digits swapped, or a constant offset point to a layout error.
   The raw bytes are on each line.
3. **Fuel pump.** Watch `EKP_STATUS_ADC_BEWERTET.SPANNUNG_KL30`: it should
   read battery voltage (≈ 12.5 V engine off, ≈ 14 V running). Pump
   currents should rise with load.
4. **A short drive log.** In the GUI, tick *Only channels the car answers*,
   add GPS position and speed, gear, transmission oil temperature, a tire
   pressure, lateral acceleration and some DME channels (`ILAG1`, `ILAG2`,
   engine speed), *Start logging*, drive 10 minutes. Afterwards check:
   - the DME columns still update every interval (module channels must not
     slow them);
   - GPS speed tracks wheel/vehicle speed;
   - gear changes line up with engine speed;
   - lateral acceleration is ≈ 0 driving straight and swings in corners.
5. **Look over the picker** on screen: groups, marks, filter.

## What to do with the results

- A module that **doesn't fit**: its `.prg` is the wrong variant. Find the
  right one (`22 F1 50` gives the module's SGBD index; see
  [Protocol](protocol.md#identifying-a-modules-software)), rebuild with
  `tools/module_catalog.py`, check again.
- **Unclear**: look at the `size` rows in `check.csv`. A few mismatched
  records may be layout details; many mean a near-miss variant.
- **GPS or fuel pump wrong**: compare the raw bytes with the layouts in
  [SGBD format](sgbd-format.md#jobs-and-hand-coded-values) and adjust
  `GPS_ROWS` / `EKP_ROWS` in `tools/module_catalog.py` (and the GPS notes in
  `core.py`).
- Update the Status table above and the Status column in
  [Modules](modules.md).

## Results

| Date | Item | Result | Notes |
|---|---|---|---|
| 2026-10-18 | Update to 0.4.0, Discover, Connect | ok | found the car first time |
| 2026-10-18 | Check, DME (`0x12`) | fits: 487 ok, 0 wrong size, 335 refused | stopped after 824 of 1,845 identifiers (at `0x557A`, two unanswered); the rest and the other modules still to do |
| 2026-10-18 | Check, ACSM (`0x01`) | fits: 54 ok, 0 wrong size, 3 refused | |
| 2026-10-18 | Fault read, all modules | ok: 36 modules, texts for nearly all codes | no text for DSC `48073A`-`48073D`, nor for modules `0x04` and `0x25` (no file yet). Telematics `E14600` is in both ATM2 and ATM, so the variant is still open |
| 2026-10-18 | GPS (drive log) | ok | fix "3D position", 6-10 satellites, position and height match the route. **GPS speed was 0.275x vehicle speed: m/s, not km/h.** Fixed (x3.6) |
| 2026-10-18 | 12-minute drive log, 95 channels | ok | 1.9 s a row; gaps of 12-29 s while the check ran alongside. Vehicle speed from DME and DSC agree; gear vs engine speed matches the 8HP ratios; lateral acceleration +-3.4 m/s2 in corners; tire pressure 2.46-2.56 bar |
| 2026-10-18 | Bank 2 | `I_EWG_POS@0x13` reads its own values; `SOLL_POS_EWG` refused | `SOLL_POS_EWG` taken out of the *Boost and wastegates* set. `IUKZ5`/`IUKZ6` and `KNDS_SENS_ROH_KS_DRUCK` refused (requestOutOfRange) |
| 2026-10-18 | Check, second run (0.4.0) | DME 1,165 ok / 0 wrong size; transmission, transfer case, steering fit 100%; DSC and park assist 97%; fuel pump no answer | stopped in the BDC: `0xDA5F` took over 0.5 s, its late answer was taken for `0xDA77`. Fixed in 0.4.1 (late answers skipped), and the check now waits 2 s. Head unit, VDP, IHKA not reached |
| 2026-10-18 | `n63read speed` | one per request 1.2 cycles/s, several per request 6.2 cycles/s, 18 of 18 answered | batching is 5.2x faster on this DME |
| 2026-10-18 | `faults --details` | odometer and vehicle time decode and agree across faults (107,240-111,271 km, rising with time); counters read | the DME's snapshot timed out (its only engine fault, `1A400A`); module-specific values not decoded yet |
| 2026-10-18 | `ILUZ5` at `0x12` and `@0x13` | both answer, with different values (5.0 and 2.44 rad/s2) | one read each, so not yet settled which is cylinder 5; log both together |
| 2026-10-18 | `ILUZ5`-`8` at `0x12` and `@0x13`, 60 rows at idle, batched (`--batch`) | the same values from both: within 0.2 rad/s2 in 49, 34, 52 and 56 of 60 rows; the rest differ where the value changed between the two reads | either DME gives cylinders 5-8, so the *Misfire hunt* set reading them from `0x12` is right. Batched logging on the car: 4.8 rows a second with 9 values from both DMEs |
| 2026-10-18 | Idle log, 13 min, 95 channels | 1.8 s a row; 25-28 s gaps again while the CLI check and fault read ran | bank 2 cylinder 5 misfire counter went to 1 once |
| 2026-10-18 | Check, BDC, head unit, VDP, IHKA (0.4.1) | all fit: BDC 230 ok, head unit 57, VDP 41, IHKA 28; none the wrong size | ran to the end. BDC: `0xDA5F`, `0xDA61`, `0xDA77` didn't answer in 0.5 s, so its last 57 were skipped (next release waits 2 s). Head unit: 37 answered busyRepeatRequest (`0x21`); the next release asks again |
| 2026-10-18 | Check, first run (0.4.0) | stopped in the DME | at `0x557B`, on a late answer to `0x5579`: the same late-reply bug |
| 2026-10-18 | `n63read speed`, second run | 5.1x faster batched | 1.1 cycles/s one per request, 5.6 batched; 18 of 18 answered both ways |
| 2026-10-18 | Logging rate, batched (`--batch`) | DME columns fill every row | 14 DME channels: 0.61 s a row one at a time, 0.12 s batched. Plus 6 module channels: 0.78 and 0.27 s (module reads stay one per request) |
| 2026-10-18 | Freeze frames against the car | match | the DSC's latest snapshot (111,271 km) was about 21 h before the dash read 111,305 km; its vehicle time agrees with the airbag module's clock |
| 2026-10-18 | DME fault details | "no details stored" with a 6 s wait | at 2 s the read timed out; `1A400A` is status `0x20`, never confirmed. Details now wait 6 s |
| 2026-10-18 | Batched CLI log while a check borrowed the connection | no stall | 0.21 s a row, worst gap 0.69 s, the same as without the check. Also no stall in a borrowing log during the app's fault read |
| 2026-10-18 | `ILAG2` at `0x12` | refused (requestOutOfRange) | single read and in logs; it read on 2026-10-14. `ILAG1@0x13` gives bank 2 lambda |
| 2026-10-18 | BDC fault details | odometer and time right in every snapshot | a second snapshot after the BDC's own values went undecoded (it held today's 111,304 km); fixed |
| 2026-10-18 | Not done yet | | the BDC's last 57 identifiers and the head unit's busy ones, on the next release; fuel pump; the DME snapshot |
