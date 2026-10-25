"""
n63read.loggerchannels - the channel file for the logger board (firmware/).

Every channel this car answers, written out so the board can read and
decode it without the catalogs: which module and identifier, where in the
reply, the data type and scaling, and the name it logs under (the app's own
column names, so History and the cloud treat the board's logs like the
app's). No Tk here.

Two tiers. "fast" channels are read on every cycle: the drive log, fuel
economy and misfire templates by default. "sweep" is everything else that
answers, read in rotation and logged to the drive's second file.

What answers comes from the app's record (Answers > Check car now, kept in
availability.json), `n63read check --out` CSVs, or every check the cloud
holds (the app uploads them with the drive logs). Only "ok" counts: an
identifier whose reply isn't the catalog's size would decode wrongly. The
slave DME runs the master's software, so a bank 2 channel counts as
answering where the master's does, unless the slave was checked itself.
"""

import csv

from n63read import core

FAST_TEMPLATES = ("Drive log with GPS", "Fuel economy", "Misfire hunt")

# Catalog data type -> the board's: width and signedness, or a float, or the
# head unit's 5-byte coordinates. Anything else (strings, data[n], bitfields)
# isn't a number to log.
TYPES = {"unsigned char": "u8", "signed char": "s8", "unsigned int": "u16",
         "signed int": "s16", "int": "s16", "unsigned long": "u32", "signed long": "s32",
         "motorola float": "f32", "intel float": "f32", core.GPS_DMS: "gps"}

HEADER = ["name", "module", "did", "did_size", "offset", "type", "order", "mul", "div",
          "add", "no_value", "unit", "tier"]


def merge_answers(answers):
    """One {(module, DID): status} from several checks, oldest first. An
    identifier that answered "ok" in any of them stays "ok": a module also
    refuses or stays silent while it wakes up, or is skipped after three
    silent identifiers in a row (seen: the head unit refused 70 identifiers
    on 2026-10-18 and answered 57 of them two days later), and the board
    rests a read that keeps failing anyway. Otherwise the newest result."""
    out = {}
    for one in answers:
        for key, status in one.items():
            if out.get(key) != "ok":
                out[key] = status
    return out


def fast_names(templates=FAST_TEMPLATES):
    """The fast tier: the templates' channels, calculated ones replaced by
    their inputs (the board logs readings; the app works out the rest)."""
    names = []
    for t in templates:
        names.extend(core.BUILTIN_TEMPLATES[t])
    return [n for n in core.with_inputs(names) if n not in core.DERIVED]


def _number(meta, key, default):
    v = core._scale(meta, key, default)
    return "%.10g" % v


def _offsets(fields):
    """{ARG: offset} for a record's fields up to the first one whose size
    can't be known in advance (the rest have no fixed place)."""
    out, pos = {}, 0
    for f in fields:
        pos = core._field_pos(f, pos)
        size = core.field_size((f.get("DATENTYP") or "").strip().lower(), b"", 0)
        if size is None:
            break
        out[f["ARG"]] = pos
        pos += size
    return out


def channels(catalog, avail, fast=None):
    """The channel file's rows, as dicts in HEADER order: fast tier first,
    then by module, identifier and offset.

    `catalog`: the full catalog with bank 2 twins (cli._catalog()).
    `avail`: {(module, DID): status}. Fast channels are kept whatever it says
    (the templates are known to work); the rest only where it says "ok".
    """
    fast = set(fast_names() if fast is None else fast)
    layouts = core.record_layouts(catalog)
    offsets = {key: _offsets(fields) for key, fields in layouts.items()}
    out = []
    for name, meta in catalog.items():
        if core.is_derived(meta) or core.is_sensitive(name):
            continue
        kind = TYPES.get((meta.get("DATENTYP") or "").strip().lower())
        if kind is None:
            continue
        did = int(meta["ID"], 16)
        if (meta.get("MODULE") or "").strip():
            module = int(meta["MODULE"], 16)
            key = (module, did)
            offset = offsets[key].get(name)
            if offset is None:
                continue                        # after a field of unknown size
            size, exact = core.expected_size(layouts[key])
            status = avail.get(key)
        else:
            module = core.channel_target(meta, core.DME_MASTER)
            offset = 0
            size, exact = core.expected_size([meta])
            status = avail.get((module, did))
            if status is None and module == core.DME_SLAVE:
                status = avail.get((core.DME_MASTER, did))
        tier = "fast" if name in fast else "sweep"
        if tier == "sweep" and status != "ok":
            continue
        no_value = core.NO_VALUE.get(name)
        out.append({
            "name": name, "module": "0x%02X" % module, "did": "0x%04X" % did,
            "did_size": size if exact else "", "offset": offset, "type": kind,
            "order": "little" if ((meta.get("L/H") or "").strip().lower() == "low"
                                  or (meta.get("DATENTYP") or "").strip().lower()
                                  == "intel float") else "big",
            "mul": _number(meta, "MUL", 1.0), "div": _number(meta, "DIV", 1.0),
            "add": _number(meta, "ADD", 0.0),
            "no_value": "" if no_value is None else "0x%X" % no_value,
            "unit": "" if (meta.get("EINHEIT") or "").strip() == "-"
            else (meta.get("EINHEIT") or "").strip(),
            "tier": tier})
    out.sort(key=lambda r: (r["tier"] != "fast", int(r["module"], 16), int(r["did"], 16),
                            r["offset"], r["name"]))
    return out


def write(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HEADER)
        w.writeheader()
        w.writerows(rows)


def summary(rows):
    """{module: {"fast": n, "sweep": n, "identifiers": n}} for a report."""
    out = {}
    for r in rows:
        m = out.setdefault(r["module"], {"fast": 0, "sweep": 0, "ids": set()})
        m[r["tier"]] += 1
        m["ids"].add(r["did"])
    return {k: {"fast": v["fast"], "sweep": v["sweep"], "identifiers": len(v["ids"])}
            for k, v in sorted(out.items())}
