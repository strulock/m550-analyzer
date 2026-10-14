# In-car checklist

What has been confirmed on the car, what hasn't, and the session that
settles it. Record results at the bottom.

## Status

| Item | Status |
|---|---|
| ENET connection and DME reads | working on the car |
| DME catalog (`dme8ff_r`) | verified: lambda bank 1/2, uneven running, misfire counters |
| Instrument cluster identification (`22 F1 50` at `0x60` → `KOMB_G11`) | confirmed on the car |
| Module scan and fault read across modules | worth a run; tested against the simulator. If an expected module is missed, raise **Probe wait s** (CLI `--probe-timeout`) and record the value that works |
| Head unit catalog and GPS position layout | **unverified**: decoded from the SGBD's bytecode |
| Transmission, transfer case, DSC, steering, body, suspension, climate, airbag, fuel pump catalogs | **unverified**: right family by name and address; reply sizes not yet checked |
| Fuel pump byte layout | **unverified**: decoded from bytecode |
| Head unit is NBT Evo (not ENAVEVO as the G30 map lists at `0x63`) | **unverified** |
| BDC is `BDC_G11` at this car's software level (SP2018) | **unverified** |
| DME logging rate with module channels added | **unverified** |

## The session

Ignition on (position 2), parked, ENET adapter connected, on battery charger
if possible (a full check takes a few minutes).

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
| | | | |
