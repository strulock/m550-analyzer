# Data sources

Everything the app knows about the car is in `src/n63read/data/`, and all of
it was extracted from BMW SGBD (`.prg`) files. The `.prg` files themselves
are BMW's, are **not** in this repo, and must not be committed. The CSVs hold
only interface facts (identifier numbers, units, scaling, texts) needed to
read hardware the owner possesses.

German is the source of truth for every text; English columns are generated.

## Files

| File | Rows | From | Made by |
|---|---|---|---|
| `N63TU2_DME_readable_measurements.csv` | 1,845 | `dme8ff_r.prg` (master DME; `dme8ff_l` carries a near-identical table) | `tools/sgbd_tables.py` plus manual extraction (before this repo's history); verified on the car |
| `BMW_fault_code_texts.csv` | 9,014 | DME (`dme8ff_r`, `_l`), `BDC`, `BDC_G05`, `BDC_G11`, `ZGW_01`, `ZGW_SP18`, `KOMB_G11`, `KOMB01`, `KOMB25`, then every module catalog, then `fzd_g11`, `kafasg11`, `atm02`, `atm`, `tcb4g` (`--faults-only`) | merged by `tools/module_catalog.py` |
| `BMW_dtc_status_byte.csv` | 92 | DME | statusOfDTC byte → BMW's text |
| `BMW_enum_tables.csv` | 480 | DME (`dme8ff_r`; `dme8ff_l` has the same tables) | the 22 value tables the DME catalog refers to. Made by `tools/module_catalog.py --dme`. The first version (162 tables) came from an older extractor that read each table's position from the directory entry before its name, so each table's texts sat under the next table's name; replaced in October 2026 |
| `BMW_freeze_frame_fields.csv` | 1,548 | `DTCSNAPSHOTIDENTIFIER` (the same in every UDS module file supplied; one row for the DSC's shorter `0x1769`), and the DME's `FUMWELTTEXTE` | BMW's standard snapshot fields (odometer, time, SAE code, fault class, power state) for every module, then the DME's own 1,538 for `0x12` and `0x13`, from `tools/module_catalog.py --dme`. Rebuilt in October 2026: the first extraction had picked up neighbouring tables and lost `0x1700` |
| `G30_module_sgbd_map.csv` | 13 | BMW's variant list (G30 rows) | address → SGBD index → file |
| `<MODULE>_measurements.csv` | see [Modules](modules.md) | `nbtevo`, `gszfb1`, `acsm5`, `dsc_g30n`, `bdc_g11`, `eps_g11`, `vdp_g11`, `lmv_g11`, `ihka_g11`, `ekp301`, `pma_20` | `tools/module_catalog.py` |
| `<MODULE>_enum_tables.csv` | | the same | `tools/module_catalog.py` |

The `.prg` files for the modules were supplied from an EDIABAS `Ecu` folder
during the October 2026 work on this repo. Which files fit this car, and
which were rejected, is in [Modules](modules.md#decisions-made-and-why).

## Module catalog columns

`ARG, MODULE, ID, FIELD, RESULTNAME, INFO, INFO_EN, EINHEIT, L/H, DATENTYP,
NAME, MUL, DIV, ADD, JOB, POS, JOB_INFO, JOB_INFO_EN`

- `ARG`: channel name, `<PREFIX><JOB>` for a single value or
  `<PREFIX><JOB>.<RESULT>` for a record field (`STAT_` and `_WERT` stripped)
- `MODULE`: address (`0x63`); its presence marks a module channel
- `FIELD`: place in the record; `POS`: explicit byte offset, rarely set
- `NAME`: value table, prefixed with the catalog name (`NBTEVO.TGPSSTATUS`)
- `JOB`: the SGBD job (`STATUS_GPS_POSITION`)
- `JOB_INFO` / `JOB_INFO_EN`: what a record is, from the job's own `INFO` in
  `SG_FUNKTIONEN` (written by hand for the two hand-decoded jobs); the GUI
  names a record's folder with it. Empty for single values. The English is
  curated in `tools/translations_en.csv` like the rest.

The DME catalog has the SGBD's own column set (`ARG, ID, RESULTNAME, INFO,
INFO_EN, EINHEIT, LABEL, L/H, DATENTYP, NAME, MUL, DIV, ADD, SG_ADR, SERVICE,
ARG_TABELLE, RES_TABELLE`) and no `MODULE`.

## Regenerating

```
python tools/module_catalog.py C:\EDIABAS\Ecu\nbtevo.prg ...   # catalogs + fault texts
python tools/module_catalog.py --dme C:\EDIABAS\Ecu\dme8ff_r.prg   # DME value tables, snapshot fields
python tools/translate.py                                      # English columns
python tools/channel_docs.py                                   # docs/channels/
```

Each step is idempotent: running the catalog tool again over the same files
changes nothing. Tests fail if the English columns (`translate.py --check`)
or the channel lookup (`channel_docs.py --check`) fall out of step with the
catalogs.

### How fault texts merge

The fault file is keyed by code alone, and `source_module` lists which files
a code's text came from (`DME; DME-slave`, `BDC-G05; BDC-G11`). For each code
in a module's `FORTTEXTE`:

| Case | Result |
|---|---|
| code not in the file | added, tagged with this module |
| same code, same German text | this module's tag appended |
| same code, different text, **same module family** (another car's BDC) | this file's text wins: it is this car's variant (109 BDC codes) |
| same code, different text, **different module** | left alone; the first module's text stays (e.g. `801230` on the DME vs the climate module) |

A limitation remains: the app looks up a fault's text by code only, not by
which module reported it. Codes mostly embed the module's address, so
collisions are rare.

## Translation

`tools/translate.py` regenerates each file's English column from the German.
For each text it uses, in order:

1. **The curated translation** in `tools/translations_en.csv` (`de,en`), keyed
   by the exact German text. Every channel description and value text in
   the catalogs has one: about 8,000 texts translated by hand into plain
   English, using one shared terminology list (Istwert → actual value,
   Laufunruhe → uneven running, Höhenstand → ride height...). Identifiers,
   signal names, units and abbreviations are kept as they are.
2. **The glossary** in `tools/glossary.py`, as a fallback: whole-word and
   phrase matches, German endings stripped, compounds split into known terms
   (`Kuehlmitteltemperatur` → "coolant temperature"). A word it can't fully
   account for stays German. Fault texts still use only this.

When a catalog gains text (a new module, a regenerated SGBD), `python
tools/translate.py --uncurated` lists the channel and value texts with no
curated translation. Add them to `translations_en.csv`; a test fails until
you do. Never edit an English column in the data files by hand: it is
overwritten on the next run. To improve a translation, edit
`translations_en.csv` and rerun.

The app can show either language: see the README's *Language* setting.
