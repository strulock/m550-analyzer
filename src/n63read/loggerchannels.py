"""
n63read.loggerchannels - the channel file for the logger board (firmware/).

Every channel this car answers, written out so the board can read and
decode it without the catalogs: which module and identifier, where in the
reply, the data type and scaling, and the name it logs under (the app's own
column names, so History and the cloud treat the board's logs like the
app's). No Tk here.

Three tiers, for the sampling rate where it matters (wastegate, boost and
knock under load):
- "fast": every cycle, aiming at 10-20 a second - engine speed, boost,
  wastegates, lambda and knock retard per bank, throttle, pedal, gear, speed.
- "medium": in rotation, a few times a second - trims, uneven running,
  misfire counters, spark burn time, fuel flow, GPS and the like.
- "slow": everything else that answers, in rotation, one row a pass in the
  drive's second file.
A template saved in the app as "Logger fast" or "Logger medium" replaces
that tier's list.

What answers comes from the app's record (Answers > Check car now, kept in
availability.json), `n63read check --out` CSVs, or every check the cloud
holds (the app uploads them with the drive logs). Only "ok" counts: an
identifier whose reply isn't the catalog's size would decode wrongly. The
slave DME runs the master's software, so a bank 2 channel counts as
answering where the master's does, unless the slave was checked itself.
"""

import csv

from n63read import core

B2 = core.BANK2_SUFFIX
TIERS = ("fast", "medium", "slow")
TEMPLATE_FAST, TEMPLATE_MEDIUM = "Logger fast", "Logger medium"

# The sampling-rate spec (2026-10-25): what wastegate, boost and knock
# analysis needs every cycle, with both banks where each DME has its own.
FAST = (["INMOT", "IPLAD", "S_LADEDRUCK"]
        + [n + s for n in ("I_EWG_POS", "TV_EWG", "ILAG1", "MITTELW_ZYLIND_ZWSPAET_KR_HALBQUANT")
           for s in ("", B2)]
        + ["IWDKL", "BUS_FAHRPEDALWERT", "EGS_ISTGANG.ISTGANG",
           "FZG_GESCHWINDIGKEIT_VA_HA_FASCHWERP"])

# The drive log, fuel economy and misfire templates (what the fast tier was
# before, and what History needs), plus trims, intake air, oil pressure and
# the wastegate position sensors.
MEDIUM_TEMPLATES = ("Drive log with GPS", "Fuel economy", "Misfire hunt")
MEDIUM_EXTRA = ([n + s for n in ("IMUL1", "SPG_POS_SENS_EWG", "LADEDRUCK_REGELDIFFERENZ_BANKSPEZ")
                 for s in ("", B2)] + ["RTANS", "OELDRUCK"])

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


def _readings(names):
    """Calculated channels replaced by their inputs (the board logs
    readings; the app works out the rest), each once, in order."""
    return [n for n in core.with_inputs(names) if n not in core.DERIVED]


def tier_lists(templates=None):
    """(fast, medium) channel names: the built-in lists, or the app's
    templates "Logger fast" and "Logger medium" where they exist. A channel
    in both is fast."""
    templates = templates or {}
    fast = _readings(templates.get(TEMPLATE_FAST) or FAST)
    if templates.get(TEMPLATE_MEDIUM):
        medium = templates[TEMPLATE_MEDIUM]
    else:
        medium = [n for t in MEDIUM_TEMPLATES for n in core.BUILTIN_TEMPLATES[t]] + MEDIUM_EXTRA
    taken = set(fast)
    return fast, [n for n in _readings(medium) if n not in taken]


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


def _decodable(catalog, avail):
    """(row, meta, status) for every channel the board can read and decode:
    `row` holds HEADER's columns but the tier, `status` is what `avail`
    says of its identifier (the slave DME taking the master's answer where
    it wasn't checked itself)."""
    layouts = core.record_layouts(catalog)
    offsets = {key: _offsets(fields) for key, fields in layouts.items()}
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
        no_value = core.NO_VALUE.get(name)
        row = {
            "name": name, "module": "0x%02X" % module, "did": "0x%04X" % did,
            "did_size": size if exact else "", "offset": offset, "type": kind,
            "order": "little" if ((meta.get("L/H") or "").strip().lower() == "low"
                                  or (meta.get("DATENTYP") or "").strip().lower()
                                  == "intel float") else "big",
            "mul": _number(meta, "MUL", 1.0), "div": _number(meta, "DIV", 1.0),
            "add": _number(meta, "ADD", 0.0),
            "no_value": "" if no_value is None else "0x%X" % no_value,
            "unit": "" if (meta.get("EINHEIT") or "").strip() == "-"
            else (meta.get("EINHEIT") or "").strip()}
        yield row, meta, status


def channels(catalog, avail, fast=None, medium=None):
    """The channel file's rows, as dicts in HEADER order: by tier, then by
    module, identifier and offset.

    `catalog`: the full catalog with bank 2 twins (cli._catalog()).
    `avail`: {(module, DID): status}. Fast and medium channels are kept
    whatever it says (chosen on purpose; the board rests one that doesn't
    answer); the slow tier only where it says "ok".
    """
    default_fast, default_medium = tier_lists()
    fast = set(default_fast if fast is None else fast)
    medium = set(default_medium if medium is None else medium) - fast
    out = []
    for row, _meta, status in _decodable(catalog, avail):
        name = row["name"]
        tier = "fast" if name in fast else "medium" if name in medium else "slow"
        if tier == "slow" and status != "ok":
            continue
        out.append({**row, "tier": tier})
    out.sort(key=lambda r: (TIERS.index(r["tier"]), int(r["module"], 16), int(r["did"], 16),
                            r["offset"], r["name"]))
    return out


# The cloud's copy, for the logger channel page (m550 Worker, /logger): every
# channel the board can decode, what the car answered, and the built-in fast
# and medium lists as the first preset. The page picks each channel's tier;
# the Worker writes the board's channels.csv from it.
CATALOG_COLUMNS = HEADER[:-1] + ["info", "group"]


def _info(meta):
    for key in ("INFO_EN", "JOB_INFO_EN", "INFO"):
        text = (meta.get(key) or "").strip()
        if text and text != "-":
            return text
    return ""


def catalog_payload(catalog, avail, fast=None, medium=None):
    """The JSON body for PUT /api/logger/catalog: {columns, rows, answers,
    modules, defaults}. `answers` is keyed "0xMM:0xDDDD", one entry per
    identifier the car was asked for (the slave DME's taking the master's
    answer where it wasn't checked itself), so the page can show what answers."""
    default_fast, default_medium = tier_lists()
    fast = list(default_fast if fast is None else fast)
    medium = [n for n in (default_medium if medium is None else medium) if n not in set(fast)]
    rows, answers = [], {}
    for row, meta, status in _decodable(catalog, avail):
        rows.append([row[c] for c in HEADER[:-1]] + [_info(meta), meta.get("JOB") or ""])
        if status is not None:
            answers["%s:%s" % (row["module"], row["did"])] = status
    rows.sort(key=lambda r: (int(r[1], 16), int(r[2], 16), r[4], r[0]))
    names = {r[0] for r in rows}
    defaults = {n: "fast" for n in fast if n in names}
    defaults.update({n: "medium" for n in medium if n in names and n not in defaults})
    modules = {"0x%02X" % m: label for m, label in core.MODULE_LABELS.items()}
    modules.update({"0x%02X" % core.DME_MASTER: "DME (bank 1)",
                    "0x%02X" % core.DME_SLAVE: "DME (bank 2)"})
    return {"version": 1, "columns": CATALOG_COLUMNS, "rows": rows, "answers": answers,
            "modules": modules, "defaults": defaults}


def write(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HEADER)
        w.writeheader()
        w.writerows(rows)


def summary(rows):
    """{module: {"fast": n, "medium": n, "slow": n, "identifiers": n}}."""
    out = {}
    for r in rows:
        m = out.setdefault(r["module"], {t: 0 for t in TIERS} | {"ids": set()})
        m[r["tier"]] += 1
        m["ids"].add(r["did"])
    return {k: {**{t: v[t] for t in TIERS}, "identifiers": len(v["ids"])}
            for k, v in sorted(out.items())}
