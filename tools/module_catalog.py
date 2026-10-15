#!/usr/bin/env python3
"""
module_catalog.py - build a module's channel catalog from its SGBD (.prg).

  python tools/module_catalog.py path/to/nbtevo.prg [more.prg ...]

The module is recognised from the file name (see MODULES). For each one this
writes, into src/n63read/data/:

  <NAME>_measurements.csv   one row per readable value
  <NAME>_enum_tables.csv    value -> text tables those rows name

and merges the module's fault texts (FORTTEXTE) into BMW_fault_code_texts.csv,
adding codes it lacks and the module's tag to codes it already has. Where
another variant of the same module (another car's BDC) words a code
differently, this car's variant wins; a code another module already uses
for something else is left as it is. Run tools/translate.py afterwards to
fill the English columns of the fault file, then tools/channel_docs.py to
update the channel lookup in docs/channels/. See docs/sgbd-format.md.

Every identifier in SG_FUNKTIONEN that service 0x22 can read is included.
Many return a record of several values (a RES_ table); each value becomes
its own channel, named <PREFIX><job>.<result>, with FIELD giving its place in
the record. The reader splits the reply in that order, so a channel can only
be decoded if every field before it has a known size.

The head unit's GPS position (0xD039) and the fuel pump's values (0x4010)
are read by hand-coded jobs rather than tables, so their rows are added by
hand from disassemblies of STATUS_GPS_POSITION (see core.py) and
STATUS_ADC_BEWERTET (see EKP_ROWS). A POS column gives a field's offset when
the job skips bytes; otherwise fields follow one another.

The table directory here is [name: 64 bytes][offset][0][cols][rows], i.e.
the numbers follow the name. sgbd_tables.table_directory() reads the 16
bytes before each name instead, which pairs every table with its
neighbour's contents, so this script has its own directory reader.
"""

import csv
import io
import os
import re
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from sgbd_tables import read_table  # noqa: E402
from translate import translate      # noqa: E402

DATA = os.path.join(HERE, "..", "src", "n63read", "data")
FAULT_CSV = "BMW_fault_code_texts.csv"

# file name key -> (catalog name, module address, channel prefix, fault tag)
MODULES = {
    "nbtevo": ("NBTEVO", "0x63", "HU_", "NBTEVO"),
    "gszfb1": ("GSZFB1", "0x18", "EGS_", "GSZFB1"),
    "acsm5": ("ACSM5", "0x01", "ACSM_", "ACSM5"),
    "dsc_g30n": ("DSC_G30N", "0x29", "DSC_", "DSC-G30N"),
    "bdc_g11": ("BDC_G11", "0x40", "BDC_", "BDC-G11"),
    "eps_g11": ("EPS_G11", "0x30", "EPS_", "EPS-G11"),
    "vdp_g11": ("VDP_G11", "0x76", "VDP_", "VDP-G11"),
    "lmv_g11": ("LMV_G11", "0x19", "LMV_", "LMV-G11"),
    "ihka_g11": ("IHKA_G11", "0x78", "IHKA_", "IHKA-G11"),
    "ekp301": ("EKP301", "0x17", "EKP_", "EKP301"),
    "pma_20": ("PMA_20", "0x2C", "PMA_", "PMA-20"),
}

HEADER = ["ARG", "MODULE", "ID", "FIELD", "RESULTNAME", "INFO", "INFO_EN",
          "EINHEIT", "L/H", "DATENTYP", "NAME", "MUL", "DIV", "ADD", "JOB", "POS",
          "JOB_INFO", "JOB_INFO_EN"]
# What the hand-decoded jobs below are, as the SGBD would have described them.
GPS_JOB_INFO = "Aktuelle GPS-Position: Gültigkeit, Breite, Länge, Höhe"
EKP_JOB_INFO = "Kraftstoffpumpe: Spannungen, Ströme, Temperaturen, Tankgeber"

# Head unit 0xD039, STATUS_GPS_POSITION. Layout in core.py's GPS section.
GPS_ROWS = [
    ("VALIDITY", "STAT_GPS_POSITION_VALIDITY", "Gueltigkeit der GPS Position",
     "-", "unsigned char", "TGpsPositionValidity"),
    ("LATITUDE", "STAT_GPS_POSITION_LATITUDE", "GPS Breitengrad (Nord positiv)",
     "deg", "gps_dms", "-"),
    ("LONGITUDE", "STAT_GPS_POSITION_LONGITUDE", "GPS Laengengrad (Ost positiv)",
     "deg", "gps_dms", "-"),
    ("HEIGHT", "STAT_GPS_POSITION_HEIGHT", "GPS Hoehe", "m", "unsigned int", "-"),
]


# Fuel pump 0x4010, STATUS_ADC_BEWERTET. ekp301.prg has no SG_FUNKTIONEN; this
# hand-coded job reads one scaled byte out of each 3-byte group of the reply,
# at these payload offsets (its own indices minus the 3-byte 62 40 10 header).
# Temperatures are the byte - 50; a 0 byte means no value (shows -50).
EKP_ROWS = [
    # suffix, result, German description, unit, offset, MUL, ADD
    ("MOTOR_SPANNUNG_A", "Kraftstoffpumpe Spannung Phase A", "V", 2, "0.1", "0.0"),
    ("MOTOR_SPANNUNG_B", "Kraftstoffpumpe Spannung Phase B", "V", 5, "0.1", "0.0"),
    ("MOTOR_SPANNUNG_C", "Kraftstoffpumpe Spannung Phase C", "V", 8, "0.1", "0.0"),
    ("MOTOR_STROM_A", "Kraftstoffpumpe Strom Phase A", "A", 11, "0.1", "0.0"),
    ("TEMPERATUR_A", "Temperatur A (0 = kein Wert)", "°C", 14, "1.0", "-50.0"),
    ("SPANNUNG_TDS", "Spannung Tankdrucksensor", "V", 17, "0.1", "0.0"),
    ("SPANNUNG_KL30", "Spannung Klemme 30", "V", 20, "0.1", "0.0"),
    ("TEMPERATUR_B", "Temperatur B (0 = kein Wert)", "°C", 29, "1.0", "-50.0"),
    ("MOTOR_STROM_B", "Kraftstoffpumpe Strom Phase B", "A", 32, "0.1", "0.0"),
    ("TANK_GEBER_1", "Tankgeber 1", "V", 35, "0.1", "0.0"),
    ("TANK_GEBER_2", "Tankgeber 2", "V", 38, "0.1", "0.0"),
    ("TEMPERATUR_C", "Temperatur C (0 = kein Wert)", "°C", 41, "1.0", "-50.0"),
    ("MOTOR_STROM_C", "Kraftstoffpumpe Strom Phase C", "A", 44, "0.1", "0.0"),
]


def deobfuscate(path):
    with open(path, "rb") as f:
        return bytes(b ^ 0xF7 for b in f.read())


def tables(d):
    """Table name -> (header, rows)."""
    out = {}
    for m in re.finditer(rb"[A-Za-z_][A-Za-z0-9_]{2,62}\x00", d):
        s = m.start()
        if s + 80 > len(d) or any(d[m.end():s + 64]):
            continue                    # a name field is NUL-padded to 64
        off, zero, cols, rows = struct.unpack_from("<IIII", d, s + 64)
        if zero or not (0 < cols < 40) or not (0 < rows < 50000) or not (0 < off < len(d)):
            continue
        out[m.group()[:-1].decode("latin-1").upper()] = read_table(d, off, cols, rows)
    return out


def module_for(path):
    """MODULES entry whose key is in the file name (uploads carry a hash prefix)."""
    base = os.path.basename(path).lower()
    for key in sorted(MODULES, key=len, reverse=True):
        if re.search(r"(^|[^a-z0-9])%s\.prg$" % re.escape(key), base):
            return key
    sys.exit("don't know which module %s is; add it to MODULES" % path)


def short(result):
    """STAT_GPS_SPEED_WERT -> GPS_SPEED. _TEXT and _DATA stay: they say what it is."""
    result = re.sub(r"^STAT_+", "", result)
    return re.sub(r"_WERT$", "", result)


def build(tabs, name, module, prefix):
    """Catalog rows and enum rows for one module."""
    enum_prefix = name + "."
    # Older-style files (the fuel pump) have no SG_FUNKTIONEN; rows come
    # from hand-decoded jobs below.
    head, funcs = tabs.get("SG_FUNKTIONEN", (["ARG", "ID", "RESULTNAME", "SERVICE",
                                                "RES_TABELLE"], []))
    col = {c: i for i, c in enumerate(head)}
    rows, used_enums = [], set()

    def enum_name(n):
        n = (n or "").strip()
        return "-" if n in ("", "-") else enum_prefix + n.upper()

    def add(arg, did, field, result, info, unit, lh, dtype, ename, mul, div, add_, job,
            pos="", job_info=""):
        # JOB_INFO: what the whole record is (the job's own description), the
        # name its fields are grouped under; empty for single values.
        rows.append({"ARG": arg, "MODULE": module, "ID": did, "FIELD": field,
                     "RESULTNAME": result, "INFO": info, "INFO_EN": translate(info),
                     "EINHEIT": unit, "L/H": lh, "DATENTYP": dtype,
                     "NAME": enum_name(ename), "MUL": mul, "DIV": div, "ADD": add_,
                     "JOB": job, "POS": pos, "JOB_INFO": job_info,
                     "JOB_INFO_EN": translate(job_info)})
        if ename not in ("", "-"):
            used_enums.add(ename.upper())

    for f in funcs:
        if "22" not in f[col["SERVICE"]].split(";"):
            continue
        job, did = f[col["ARG"]], f[col["ID"]].upper().replace("0X", "0x")
        res = f[col["RES_TABELLE"]]
        if res not in ("", "-"):
            if res.upper() not in tabs:
                print("note: %s: result table %s missing, skipped" % (job, res), file=sys.stderr)
                continue
            rhead, fields = tabs[res.upper()]
            rc = {c: i for i, c in enumerate(rhead)}
            for i, r in enumerate(fields):
                # Bitfields have no result name, only the table describing them.
                label = r[rc["RESULTNAME"]] if r[rc["RESULTNAME"]] not in ("", "-") \
                    else r[rc["NAME"]]
                add("%s%s.%s" % (prefix, job, short(label)), did, i,
                    r[rc["RESULTNAME"]], r[rc["INFO"]], r[rc["EINHEIT"]], r[rc["L/H"]],
                    r[rc["DATENTYP"]], r[rc["NAME"]], r[rc["MUL"]], r[rc["DIV"]],
                    r[rc["ADD"]], job, job_info=f[col["INFO"]] if "INFO" in col else "")
        elif f[col["RESULTNAME"]] not in ("", "-"):
            add(prefix + job, did, 0, f[col["RESULTNAME"]], f[col["INFO"]],
                f[col["EINHEIT"]], f[col["L/H"]], f[col["DATENTYP"]], f[col["NAME"]],
                f[col["MUL"]], f[col["DIV"]], f[col["ADD"]], job)

    if name == "NBTEVO":
        for i, (suffix, result, info, unit, dtype, ename) in enumerate(GPS_ROWS):
            add("%sSTATUS_GPS_POSITION.%s" % (prefix, suffix), "0xD039", i, result, info,
                unit, "high", dtype, ename, "1.0", "1.0", "0.0", "STATUS_GPS_POSITION",
                job_info=GPS_JOB_INFO)

    if name == "EKP301":
        for i, (suffix, info, unit, pos, mul, add_) in enumerate(EKP_ROWS):
            add("%sSTATUS_ADC_BEWERTET.%s" % (prefix, suffix), "0x4010", i,
                "STAT_" + suffix, info, unit, "high", "unsigned char", "-", mul, "1.0",
                add_, "STATUS_ADC_BEWERTET", pos, job_info=EKP_JOB_INFO)

    names = [r["ARG"] for r in rows]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        sys.exit("%s: duplicate channel names: %s" % (name, ", ".join(dupes)))

    enums = []
    for ename in sorted(used_enums):
        if ename not in tabs:
            print("note: %s: enum table %s not found" % (name, ename), file=sys.stderr)
            continue
        ehead, erows = tabs[ename]
        if "WERT" not in ehead or "TEXT" not in ehead:
            continue
        w, t = ehead.index("WERT"), ehead.index("TEXT")
        for e in erows:
            enums.append([enum_prefix + ename, e[w], e[t], translate(e[t])])
    return rows, enums


def family(tag):
    """BDC-G11 -> BDC: the module a fault-file tag belongs to."""
    return tag.split("-")[0]


def merge_faults(tabs, tag):
    """Add the module's fault texts to the fault file.

    Returns (added, tagged, reworded, kept): new codes, codes that gained this
    module's tag, codes reworded from another variant of this module, and
    codes another module already uses for something else (left alone).
    """
    if "FORTTEXTE" not in tabs:
        return 0, 0, 0, 0
    path = os.path.join(DATA, FAULT_CSV)
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    by_code = {r["code"]: r for r in rows}
    head, ftexts = tabs["FORTTEXTE"]
    added = tagged = clashes = kept = 0
    for r in ftexts:
        code = r[0].upper().replace("0X", "").zfill(6)
        text = r[1].strip()
        if not re.fullmatch(r"[0-9A-F]{6}", code) or code == "FFFFFF":
            continue                    # table end marker, not a fault
        row = by_code.get(code)
        if row is None:
            by_code[code] = {"code": code, "text_de": text, "text_en": "",
                             "source_module": tag}
            added += 1
        elif tag not in row["source_module"].split("; "):
            if row["text_de"] == text:
                row["source_module"] += "; " + tag
                tagged += 1
            elif family(tag) in {family(t) for t in row["source_module"].split("; ")}:
                # Another variant of the same module (another car's BDC)
                # words this code differently. This car's variant wins.
                row.update(text_de=text, text_en="", source_module=tag)
                clashes += 1
            else:
                # A different module uses the same code for something else.
                # The file is keyed by code alone, so the first one stays.
                kept += 1
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=["code", "text_de", "text_en", "source_module"],
                       lineterminator="\n")
    w.writeheader()
    w.writerows(by_code[c] for c in sorted(by_code))
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        f.write(buf.getvalue())
    return added, tagged, clashes, kept


def write_csv(path, header, rows, dicts=True):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        if dicts:
            w = csv.DictWriter(f, fieldnames=header, lineterminator="\n")
            w.writeheader()
        else:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(header)
        w.writerows(rows)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    # --catalogs-only: rewrite the measurement catalogs, leave the enum
    # tables and the shared fault texts alone.
    catalogs_only = "--catalogs-only" in argv
    argv = [a for a in argv if a != "--catalogs-only"]
    if not argv:
        sys.exit(__doc__)
    for prg in argv:
        name, module, prefix, tag = MODULES[module_for(prg)]
        tabs = tables(deobfuscate(prg))
        rows, enums = build(tabs, name, module, prefix)
        write_csv(os.path.join(DATA, "%s_measurements.csv" % name), HEADER, rows)
        if catalogs_only:
            print("%-9s %s  %4d channels" % (name, module, len(rows)))
            continue
        write_csv(os.path.join(DATA, "%s_enum_tables.csv" % name),
                  ["table_name", "value", "text_de", "text_en"], enums, dicts=False)
        added, tagged, clashes, kept = merge_faults(tabs, tag)
        print("%-9s %s  %4d channels from %3d identifiers, %4d enum rows; faults: "
              "%d added, %d tagged, %d reworded, %d kept (other module's)"
              % (name, module, len(rows), len({r["ID"] for r in rows}), len(enums),
                 added, tagged, clashes, kept))


if __name__ == "__main__":
    main()
