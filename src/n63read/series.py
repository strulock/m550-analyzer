"""
n63read.series - a drive log as time series, one per channel, in UTC.

build() reads a log and returns, for each channel, its numeric values with
the moment each was read: seconds since 1970 in UTC. The moment comes from
the log's Clock (with its UTC offset since 0.6.2; an older Clock is this
PC's local time, which this PC can convert) plus Time for sub-second
precision. A log without a Clock (another tool's) gets its start from the
file's time, marked approximate.

The cloud sync uploads these to the Worker, which keeps them in D1 so one
channel can be followed across every drive. No Tk here.
"""

import csv
import os

from n63read import core

CHUNK = 5000                # values per stored piece; long drives split
SKIP = {core.LOG_TIME.lower(), core.LOG_CLOCK.lower(), core.LOG_EVENT.lower()}

_units = None


def unit_of(name):
    """A channel's unit from the catalogs; bank 2 twins take bank 1's."""
    global _units
    if _units is None:
        cat = core.load_catalog()
        cat.update(core.load_module_catalogs())
        _units = {k: (v.get("EINHEIT") or "").strip() for k, v in cat.items()}
    base = name[:-len(core.BANK2_SUFFIX)] if name.endswith(core.BANK2_SUFFIX) else name
    unit = _units.get(base, "")
    return "" if unit in ("-", "0-n") else unit


def _number(text):
    text = (text or "").strip()
    if not text:
        return None
    try:
        v = float(text.replace(",", ".") if text.count(",") == 1 and "." not in text
                  else text)
    except ValueError:
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def build(path):
    """{"start": UTC seconds, "approx": bool, "channels": [{name, unit, points}]}
    for a drive log; points are [(UTC seconds, value)] in time order."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        sample = f.read(8192)
        f.seek(0)
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
        except csv.Error:
            delimiter = ","
        reader = csv.reader(f, delimiter=delimiter)
        header = [h.strip() for h in next(reader, [])]
        if not header:
            raise ValueError("empty log")
        lower = [h.lower() for h in header]
        clock_col = lower.index(core.LOG_CLOCK.lower()) if core.LOG_CLOCK.lower() in lower \
            else None
        cols = [i for i, h in enumerate(header) if i and h and h.lower() not in SKIP]
        series = {i: [] for i in cols}
        start = None
        first_t = None
        for line in reader:
            if not line:
                continue
            t = _number(line[0])
            if t is None:
                continue
            if first_t is None:
                first_t = t
                if clock_col is not None and clock_col < len(line):
                    utc = core.clock_epoch(line[clock_col])
                    if utc is not None:
                        start = utc - t
            for i in cols:
                if i < len(line):
                    v = _number(line[i])
                    if v is not None:
                        series[i].append((t, v))
    approx = start is None
    if approx:
        # No clock: the file was last written as the log ended.
        last = max((p[-1][0] for p in series.values() if p), default=first_t or 0.0)
        start = os.path.getmtime(path) - (last - (first_t or 0.0))
    return {"start": round(start, 3), "approx": approx, "channels": [
        {"name": header[i], "unit": unit_of(header[i]),
         "points": [(round(start + t, 3), v) for t, v in series[i]]}
        for i in cols if series[i]]}


def chunks(channel):
    """A channel's points as stored pieces: {name, unit, t0, t1, n, min,
    max, mean, data}, data being [seconds after t0, value] pairs."""
    pts = channel["points"]
    for k in range(0, len(pts), CHUNK):
        part = pts[k:k + CHUNK]
        t0 = part[0][0]
        values = [v for _, v in part]
        yield {"name": channel["name"], "unit": channel["unit"], "t0": t0,
               "t1": part[-1][0], "n": len(part), "min": min(values), "max": max(values),
               "mean": round(sum(values) / len(values), 6),
               "data": [[round(t - t0, 3), v] for t, v in part]}
