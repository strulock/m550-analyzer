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
another variant (another car's module) words a code differently, this car's
module wins. Run
tools/translate.py afterwards to fill the English columns of the fault file.

Every identifier in SG_FUNKTIONEN that service 0x22 can read is included.
Many return a record of several values (a RES_ table); each value becomes
its own channel, named <PREFIX><job>.<result>, with FIELD giving its place in
the record. The reader splits the reply in that order, so a channel can only
be decoded if every field before it has a known size.

The head unit's GPS position (0xD039) is read by a hand-coded job rather than
a table, so its rows are added by hand from a disassembly of
STATUS_GPS_POSITION; see core.py.

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
}

HEADER = ["ARG", "MODULE", "ID", "FIELD", "RESULTNAME", "INFO", "INFO_EN",
          "EINHEIT", "L/H", "DATENTYP", "NAME", "MUL", "DIV", "ADD", "JOB"]

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
    head, funcs = tabs["SG_FUNKTIONEN"]
    col = {c: i for i, c in enumerate(head)}
    rows, used_enums = [], set()

    def enum_name(n):
        n = (n or "").strip()
        return "-" if n in ("", "-") else enum_prefix + n.upper()

    def add(arg, did, field, result, info, unit, lh, dtype, ename, mul, div, add_, job):
        rows.append({"ARG": arg, "MODULE": module, "ID": did, "FIELD": field,
                     "RESULTNAME": result, "INFO": info, "INFO_EN": translate(info),
                     "EINHEIT": unit, "L/H": lh, "DATENTYP": dtype,
                     "NAME": enum_name(ename), "MUL": mul, "DIV": div, "ADD": add_,
                     "JOB": job})
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
                    r[rc["ADD"]], job)
        elif f[col["RESULTNAME"]] not in ("", "-"):
            add(prefix + job, did, 0, f[col["RESULTNAME"]], f[col["INFO"]],
                f[col["EINHEIT"]], f[col["L/H"]], f[col["DATENTYP"]], f[col["NAME"]],
                f[col["MUL"]], f[col["DIV"]], f[col["ADD"]], job)

    if name == "NBTEVO":
        for i, (suffix, result, info, unit, dtype, ename) in enumerate(GPS_ROWS):
            add("%sSTATUS_GPS_POSITION.%s" % (prefix, suffix), "0xD039", i, result, info,
                unit, "high", dtype, ename, "1.0", "1.0", "0.0", "STATUS_GPS_POSITION")

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


def merge_faults(tabs, tag):
    """Add the module's fault texts to the fault file. Returns (added, tagged, reworded)."""
    if "FORTTEXTE" not in tabs:
        return 0, 0, 0
    path = os.path.join(DATA, FAULT_CSV)
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    by_code = {r["code"]: r for r in rows}
    head, ftexts = tabs["FORTTEXTE"]
    added = tagged = clashes = 0
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
            else:
                # Another variant (another car) words this code differently.
                # Every module here is this car's own, so its text wins.
                row.update(text_de=text, text_en="", source_module=tag)
                clashes += 1
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=["code", "text_de", "text_en", "source_module"],
                       lineterminator="\n")
    w.writeheader()
    w.writerows(by_code[c] for c in sorted(by_code))
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        f.write(buf.getvalue())
    return added, tagged, clashes


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
    if not argv:
        sys.exit(__doc__)
    for prg in argv:
        name, module, prefix, tag = MODULES[module_for(prg)]
        tabs = tables(deobfuscate(prg))
        rows, enums = build(tabs, name, module, prefix)
        write_csv(os.path.join(DATA, "%s_measurements.csv" % name), HEADER, rows)
        write_csv(os.path.join(DATA, "%s_enum_tables.csv" % name),
                  ["table_name", "value", "text_de", "text_en"], enums, dicts=False)
        added, tagged, clashes = merge_faults(tabs, tag)
        print("%-9s %s  %4d channels from %3d identifiers, %4d enum rows; faults: "
              "%d added, %d tagged, %d reworded"
              % (name, module, len(rows), len({r["ID"] for r in rows}), len(enums),
                 added, tagged, clashes))


if __name__ == "__main__":
    main()
