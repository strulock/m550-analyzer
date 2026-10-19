# Modules

The car is a 2018 BMW M550i xDrive: G30 chassis, N63TU2 (N63B44O2) V8,
ZF 8HP transmission, xDrive. Its modules sit behind the central gateway at
1-byte addresses. For each, the app needs the matching **SGBD** (`.prg`)
file, from which `tools/module_catalog.py` builds a catalog.

## What the app reads

| Address | Module | SGBD | Catalog prefix | Channels | Status |
|---|---|---|---|---|---|
| `0x12` | Engine, master DME (Bosch, N63TU2) | `dme8ff_r` | *(none)* | 1,845 | **verified**: lambda, uneven running and misfire values read on the car; all 1,845 identifiers checked on 2026-10-18: 1,165 answered, every one the expected size; 680 refused |
| `0x63` | Head unit, NBT Evo | `nbtevo` | `HU_` | 717 | **GPS verified** (2026-10-18): fix, satellites, position and height right on a drive. GPS speed is m/s, not the km/h the file says; corrected. Other values not yet checked |
| `0x18` | Transmission, ZF 8HP (EGS) | `gszfb1` | `EGS_` | 3,526 | **fits** (2026-10-18): 131 of 131 answers the expected size. Gear verified against the 8HP ratios |
| `0x19` | Transfer case, xDrive (LMV) | `lmv_g11` | `LMV_` | 224 | **fits** (2026-10-18): 20 of 20 |
| `0x29` | Stability control (DSC), EBC460 SP2018 | `dsc_g30n` | `DSC_` | 868 | **fits** (2026-10-18): 60 of 62 the expected size (`0x4007` CPU statistics and `0xD80D` button records differ); tire pressure plausible |
| `0x30` | Electric power steering (EPS) | `eps_g11` | `EPS_` | 66 | **fits** (2026-10-18): 22 of 22 |
| `0x40` | Body domain controller (BDC) | `bdc_g11` | `BDC_` | 1,357 | probably right (see below); gear from the bus reads; not yet checked |
| `0x76` | Suspension, vertical dynamics (VDP) | `vdp_g11` | `VDP_` | 538 | not yet checked |
| `0x78` | Climate control (IHKA) | `ihka_g11` | `IHKA_` | 154 | not yet checked |
| `0x01` | Airbag / crash safety (ACSM) | `acsm5` | `ACSM_` | 565 | **fits** (2026-10-18): all 54 identifiers that answered are the expected size; lateral acceleration plausible |
| `0x17` | Fuel pump control (EKP) | `ekp301` | `EKP_` | 13 | hand-decoded; **no answer** at `0x17` on 2026-10-18 (engine running): absent at that address, or asleep |
| `0x2C` | Park assist (PMA / PDC) | `pma_20` | `PMA_` | 89 | **fits** (2026-10-18): 30 of 31 (`0xD67C` sensor signal paths: 36 bytes, file says 32) |

Fault texts for all of these, plus the gateway (`ZGW_01`, `ZGW_SP18`) and
instrument cluster (`KOMB_G11`, `KOMB01`, `KOMB25`), are in
`BMW_fault_code_texts.csv`; see [Data sources](data-sources.md).

Full channel lists: [channel lookup](channels/README.md).

## Highlights per module

| Module | Worth logging |
|---|---|
| DME | lambda per bank (`ILAG1`/`ILAG2`), uneven running per cylinder (`ILUZ1`–`ILUZ8`), misfire counters, boost, temperatures, everything engine |
| Head unit | GPS latitude / longitude / height (`HU_STATUS_GPS_POSITION.*`), fix (`HU_STATUS_GPS`), satellites, GPS speed vs wheel speeds |
| Transmission | gear (`EGS_ISTGANG.ISTGANG`), oil temperature, input/output shaft speed, converter lock-up, selector, paddles; shift adaptation tables (most of its 3,526 values) |
| Transfer case | front clutch torque (`LMV_LMV_MOMENT.*`), clutch temperature model, usage histograms |
| DSC | tire pressure and temperature per wheel (`DSC_RDC_MESSDATENBLOCK_1..4`), wheel speeds, brake pressures, pedal travel, parking brake |
| Steering | steering (pinion) angle, steering torque, rack travel |
| Body | fuel level left/right tank, ride height, drive mode switch (`BDC_STATUS_FES_MODUS`), rain/light sensors, parking brake, reverse switch |
| Suspension | ride height per corner, damper valve currents, wheel accelerations, active roll stabilisation |
| Climate | climate temperature sensors, A/C compressor speed and state |
| Airbag module | 3-axis acceleration and yaw/roll/pitch rate from its IMU (`ACSM__IMU_WERTE.*`), seat occupancy |
| Fuel pump | pump motor phase voltages and currents, battery voltage at the pump, tank senders, temperatures |
| Park assist | distance at every parking sensor, front and rear (`PMA_PDC_*_ABSTAND_WERT`, cm; 253 = no object), sensor ring-down times, parking space length/depth, reverse gear, trailer |

## Choosing the file for a module

BMW keeps many SGBD variants per module: one per hardware generation,
platform and software level. A catalog from the wrong variant decodes
**plausible nonsense** without any error. For example, the older N63TU engine
file maps `ILUZ5` to `0x4A43`, where this car's is `0x4A34`. So:

1. **Read the file's `ECU:` line** (see
   [SGBD format](sgbd-format.md#identifying-a-file)). It names the module and
   often the engine or platform.
2. **Confirm the address** from its fault texts: the *Energiesparmode aktiv*
   code is `02AA00`, where `AA` is the address.
3. **Prefer the platform family.** The G30 belongs to the "35up" G11 family
   (G11/G12 7 Series, G30/G31/G32 5 and 6 Series, G01/G02 X3/X4), so `_G11`
   files are the first candidates, and `G30_module_sgbd_map.csv` lists the
   exact file where it has a G30 row.
4. **Check it against the car** (below).

### Is this the right file?

`n63read check --ip ... --out check.csv` (or *Answers ▾ > Check car now*)
reads every identifier in every catalog and compares each reply's length with
what the catalog's record layout predicts. Per module it reports:

| Verdict | Rule |
|---|---|
| **fits** | ≥ 90% of the identifiers that answered had the expected size |
| **does NOT fit** | < 50% did |
| **unclear** | in between, so look at the size mismatches in the CSV |
| **no identifiers answered** | module absent or asleep, or a very wrong file |

Refusals (`requestOutOfRange`) don't count against a file: SGBDs describe
every option the module can have, and this car's module won't support all of
them. Three unanswered identifiers in a row skip the rest of that module.

## Decisions made, and why

| File | Decision | Reason |
|---|---|---|
| `dme8ff_r` | **used** (DME) | catalog verified on the car |
| `nbtevo` | **used** (head unit) | NBT Evo is this car's head unit. The repo's G30 map lists `ENAVEVO` (Entry Nav Evolution) at `0x63`, which is a different, cheaper unit; the check will confirm |
| `gszfb1` | **used** (transmission) | `ECU: Getriebesteuergerät 8HPxx`, address `0x18` |
| `acsm5` | **used** (airbag) | address `0x01`; ACSM 5 is the G-series generation |
| `dsc_g30n` | **used** (DSC) | named in the G30 map: "EBC460 SP2018" at `0x29` |
| `bdc_g11` | **used** (body) | the G30 is in the G11 family; `BDC` is F-series and `BDC_G05` is the next generation (G05/G20). Its fault-text wording replaced the other BDC variants' where they differed (109 codes). Some doubt remains because this car's DSC is at software level SP2018; the check settles it |
| `eps_g11`, `vdp_g11`, `lmv_g11`, `ihka_g11` | **used** | `_G11` family; addresses `0x30`, `0x76`, `0x19`, `0x78` confirmed from their fault codes |
| `ekp301` | **used** (fuel pump) | UDS-era file at `0x17`; no channel table, so its one live identifier was decoded by hand |
| `pma_20` | **used** (park assist) | `ECU: Parkmanöver-Assistent`, address `0x2C` |
| `n63tu_r0`, `n63tu_l0` | **rejected** | MEVD17.2.8 for the older N63TU (≈2012–2016 F-series, master + slave DME). Same channel names map to different identifiers (`ILUZ5` = `0x4A43` there, `0x4A34` here), and many shared identifiers have different types or offsets. Kept only as a list of 61 identifiers worth probing (battery voltage `0x460A`, alternator data, oil sensor raw values, target oil pressure `0x5862`, lambda sensor internal resistance, odometer `0x58A1`, water pump target speed `0x5906`), with unknown scaling |
| `dsc_25` | **rejected** | a different DSC variant; `dsc_g30n` is the one the G30 map names |
| `zgw_01` | **not catalogued** | gateway: one readable value; its fault texts were already in the fault file |
| `ekpm60_3` | **rejected** | fuel pump module of the E60 (2003–2010), pre-UDS file format |

### Bank 2 and the slave DME

The project began because BimmerLink doesn't show bank 2 values for this
engine. This car has two engine computers: the master DME at `0x12` (bank 1)
and the slave at `0x13` (bank 2), with the same identifier table.

- A few bank 2 values have identifiers of their own on the master: `ILAG2`
  (`0x588A`, lambda bank 2) reads from `0x12`.
- Most don't. The catalog's `_BANK2` / `_2` identifiers (for example
  `SPG_POS_SENS_EWG_BANK2`, `0x4655`) are refused on both DMEs. Bank 2's
  values come from `0x13` under the bank-1 identifier: `SPG_POS_SENS_EWG`
  (`0x4653`) is bank 1's wastegate position voltage at `0x12` and bank 2's at
  `0x13` (4.37 V and 4.19 V at idle, so not an echo). Knock levels for
  cylinders 5–8 (`IUKZ5`...) likewise answer nothing at `0x12`.
- n63read lists every DME channel a second time as `NAME@0x13` (the GUI's
  *Engine bank 2* group), read from the slave over its own connection; see
  [protocol](protocol.md#two-connections).
- Not readable on either DME in the default session: `SOLL_POS_EWG`
  (`0x4659`, requestOutOfRange again on 2026-10-18), nor the fuel
  low-pressure raw value `KNDS_SENS_ROH_KS_DRUCK` (`0x5463`).
- `I_EWG_POS` (`0x5927`) at `0x13` once replied with the wrong identifier
  (`0x4653`). On 2026-10-18 it answered throughout a 12-minute drive with
  its own values (0-70 %, different from bank 1's), so bank 2's wastegate
  position reads as `I_EWG_POS@0x13`.

When a bank 2 value looks unavailable, try the bank-1 name with `@0x13`
before concluding it can't be read.

## Modules not covered yet

ARS active roll stabilisation as its own module (`0x04`/`0x25`; the
suspension module reports some roll signals), the instrument
cluster's live values (only its fault texts are in), and the gateway.

Fault texts only (no live values yet), added with
`tools/module_catalog.py --faults-only`:

| Address | Module | File | Notes |
|---|---|---|---|
| `0x56` | Roof function centre (FZD) | `fzd_g11` | address confirmed by its `025600` code |
| `0x5D` | Camera-based driver assistance (KAFAS) | `kafasg11` | `025D00` |
| `0x61` | Telematics | `atm02`, then `atm`, then `tcb4g` | all three are `0x61` variants and the car's isn't known yet; their texts agree on all but 3 codes, where `atm02`'s wording is kept |
| `0x6D` | Driver's seat module (SM_FA) | `fas_g11` | `026D00` |
| `0x6E` | Passenger seat module | `bfs_g11` | `026E00` |
| `0x60` | Instrument cluster | `komb_g11` | fills in after the earlier cluster files |
| `0x21` | Front radar, active cruise control | `frr_g11` | only on cars with the option |
| `0x22` | Optional-equipment module (SAS) | `sas_g11` | |
| `0x2B` | Rear-axle steering (Integral Active Steering) | `hsr_g11` | only on cars with the option |
| `0x2C` | Ultrasonic park sensors | `uss_g11` | same address as `PMA_20`, whose texts stay first |
| `0x2E` | Power control unit | `pcu_g11` | |
| `0x08` | Lane-change warning | `hc2_g11` | only on cars with the option |
| `0x0D` | Rear function module (tailgate) | `hkfm_g11` | |
| `0x4F` | Light effect manager | `lem_g11` | |
| `0x5E` | Gear selector | `gws_g11`, `gws_g11b` | the two agree on every code |

Each file's address was confirmed by its energy-saving-mode code (`02AA00`).
Texts for a module the car doesn't have are never shown. Supplied but left
out: `fah_g11`/`bfh_g11` (the 7 Series' rear seats), `obc_g11` (a plug-in
hybrid's charger), `dsc_g11` (`dsc_g30n` is the DSC the G30 map names) and
the E-series and hybrid-battery files (`sm46*`, `sm53*`, `smg*`, `sme*`).
