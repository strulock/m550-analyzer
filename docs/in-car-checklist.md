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
| Transmission, transfer case, DSC, steering, body, suspension, climate, airbag, fuel pump catalogs | **unverified**: right family by name and address; reply sizes not yet checked |
| Fuel pump byte layout | **unverified**: decoded from bytecode |
| Head unit is NBT Evo (not ENAVEVO as the G30 map lists at `0x63`) | **likely**: the NBT Evo catalog's GPS and fault texts fit; identification not yet read |
| BDC is `BDC_G11` at this car's software level (SP2018) | **unverified** |
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
| 2026-10-18 | Not done yet | | rest of the check, `n63read speed`, `faults --details`, fuel pump voltage, `ILUZ5`-`8` at `0x12` vs `@0x13` side by side |
