#!/usr/bin/env python3
"""
hu_catalog.py - build the head unit channel catalog from nbtevo.prg.

  python tools/hu_catalog.py path/to/nbtevo.prg

Writes, into src/n63read/data/:

  NBTEVO_head_unit_measurements.csv   one row per readable value
  NBTEVO_enum_tables.csv              value -> text tables those rows name

Every identifier in SG_FUNKTIONEN that service 0x22 can read is included.
Many return a record of several values (a RES_ table); each value becomes
its own channel, named HU_<job>.<result>, with FIELD giving its place in the
record. The reader splits the reply in that order, so a channel can only be
decoded if every field before it has a known size.

The GPS position (0xD039) is read by a hand-coded job rather than a table,
so its rows are added by hand from a disassembly of STATUS_GPS_POSITION; see
core.py.

The table directory here is [name: 64 bytes][offset][0][cols][rows], i.e.
the numbers follow the name. sgbd_tables.table_directory() reads the 16
bytes before each name instead, which in this file pairs every table with
its neighbour's contents, so this script has its own directory reader.
"""

import csv
import os
import re
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from sgbd_tables import read_table  # noqa: E402
from translate import translate      # noqa: E402

DATA = os.path.join(HERE, "..", "src", "n63read", "data")
OUT = "NBTEVO_head_unit_measurements.csv"
ENUM_OUT = "NBTEVO_enum_tables.csv"
MODULE = "0x63"
PREFIX = "HU_"
ENUM_PREFIX = "NBTEVO."

HEADER = ["ARG", "MODULE", "ID", "FIELD", "RESULTNAME", "INFO", "INFO_EN",
          "EINHEIT", "L/H", "DATENTYP", "NAME", "MUL", "DIV", "ADD", "JOB"]

# 0xD039, STATUS_GPS_POSITION. Layout in core.py's head unit GPS section.
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


def short(result):
    """STAT_GPS_SPEED_WERT -> GPS_SPEED. _TEXT and _DATA stay: they say what it is."""
    result = re.sub(r"^STAT_+", "", result)
    return re.sub(r"_WERT$", "", result)


def enum_name(name):
    name = (name or "").strip()
    return "-" if name in ("", "-") else ENUM_PREFIX + name.upper()


def build(prg):
    tabs = tables(deobfuscate(prg))
    head, funcs = tabs["SG_FUNKTIONEN"]
    col = {c: i for i, c in enumerate(head)}
    rows, used_enums = [], set()

    def add(arg, did, field, result, info, unit, lh, dtype, name, mul, div, add_, job):
        rows.append({"ARG": arg, "MODULE": MODULE, "ID": did, "FIELD": field,
                     "RESULTNAME": result, "INFO": info, "INFO_EN": translate(info),
                     "EINHEIT": unit, "L/H": lh, "DATENTYP": dtype,
                     "NAME": enum_name(name), "MUL": mul, "DIV": div, "ADD": add_,
                     "JOB": job})
        if name not in ("", "-"):
            used_enums.add(name.upper())

    for f in funcs:
        if "22" not in f[col["SERVICE"]].split(";"):
            continue
        job, did = f[col["ARG"]], f[col["ID"]].upper().replace("0X", "0x")
        res = f[col["RES_TABELLE"]]
        if res not in ("", "-"):
            rhead, fields = tabs[res.upper()]
            rc = {c: i for i, c in enumerate(rhead)}
            for i, r in enumerate(fields):
                # Bitfields have no result name, only the table describing them.
                label = r[rc["RESULTNAME"]] if r[rc["RESULTNAME"]] not in ("", "-") \
                    else r[rc["NAME"]]
                add("%s%s.%s" % (PREFIX, job, short(label)), did, i,
                    r[rc["RESULTNAME"]], r[rc["INFO"]], r[rc["EINHEIT"]], r[rc["L/H"]],
                    r[rc["DATENTYP"]], r[rc["NAME"]], r[rc["MUL"]], r[rc["DIV"]],
                    r[rc["ADD"]], job)
        elif f[col["RESULTNAME"]] not in ("", "-"):
            add(PREFIX + job, did, 0, f[col["RESULTNAME"]], f[col["INFO"]],
                f[col["EINHEIT"]], f[col["L/H"]], f[col["DATENTYP"]], f[col["NAME"]],
                f[col["MUL"]], f[col["DIV"]], f[col["ADD"]], job)

    for i, (suffix, result, info, unit, dtype, name) in enumerate(GPS_ROWS):
        add("%sSTATUS_GPS_POSITION.%s" % (PREFIX, suffix), "0xD039", i, result, info,
            unit, "high", dtype, name, "1.0", "1.0", "0.0", "STATUS_GPS_POSITION")

    names = [r["ARG"] for r in rows]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        sys.exit("duplicate channel names: %s" % ", ".join(sorted(dupes)))

    enums = []
    for name in sorted(used_enums):
        if name not in tabs:
            print("note: enum table %s not found" % name, file=sys.stderr)
            continue
        ehead, erows = tabs[name]
        if "WERT" not in ehead or "TEXT" not in ehead:
            continue
        w, t = ehead.index("WERT"), ehead.index("TEXT")
        for e in erows:
            enums.append([ENUM_PREFIX + name, e[w], e[t], translate(e[t])])
    return rows, enums


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        sys.exit(__doc__)
    rows, enums = build(argv[0])
    with open(os.path.join(DATA, OUT), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=HEADER, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(DATA, ENUM_OUT), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["table_name", "value", "text_de", "text_en"])
        w.writerows(enums)
    print("%d channels from %d identifiers, %d enum rows"
          % (len(rows), len({r["ID"] for r in rows}), len(enums)))


if __name__ == "__main__":
    main()
