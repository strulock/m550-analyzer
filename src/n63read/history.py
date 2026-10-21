"""
n63read.history - what every drive and every fault read adds up to.

scan() goes through the logs folder (Documents\\BMW Analyzer Logs, and the
folders in it): each drive log becomes one row of statistics - misfires,
uneven running and spark time per cylinder, knock retard and fuel trims per
bank, temperatures, battery, tire pressures, distance - and every saved
fault list adds to the history of each fault: when it first and last
appeared and whether it's still there. It works from the files alone, so
logs from the app, the CLI and Claude all count, and so do logs copied in
from elsewhere.

Results go to the History folder inside the logs folder: drives.csv and
faults.csv for Excel, and index.json, which remembers what each file added
so a rescan only reads new or changed files. No Tk here.
"""

import csv
import datetime
import json
import math
import os

from n63read import core, playback

FOLDER = "History"
DRIVES_CSV = "drives.csv"
FAULTS_CSV = "faults.csv"
INDEX_JSON = "index.json"
INDEX_VERSION = 3           # bump when a summary changes: everything is re-read
MIN_ROWS = 5                # fewer rows than this isn't a drive worth a line
MAX_STEP = 5.0              # s; a longer gap between rows isn't driven through
CYLINDERS = range(1, 9)
B2 = core.BANK2_SUFFIX

LAT = "HU_STATUS_GPS_POSITION.LATITUDE"
LON = "HU_STATUS_GPS_POSITION.LONGITUDE"
SPEEDS = ("FZG_GESCHWINDIGKEIT_VA_HA_FASCHWERP", "IVKMH", "HU_STATUS_SPEED.GPS_SPEED")
ODOMETERS = ("LMV_DATEN_VERTEILERGETRIEBE.MILE_KM", "BDC_ZUSATZINFO_LICHT.KM_STAND_1")


def _per_cylinder(bank1, bank2):
    """Channel names for cylinders 1-8 when each DME reports its own four:
    cylinders 5-8 are the slave's 1-4 (docs/modules.md, bank 2)."""
    return {n: [bank1 % n] if n <= 4 else [bank2 % (n - 4) + B2] for n in CYLINDERS}


# A metric: (key, label, unit, how, channels to try in order). `how` is
# mean, min, max, sd, increase (sum of the counter's rises), or pct (a
# factor's mean as a percentage off 1.0). Units are what the column holds.
METRICS = [
    ("rpm_max", "Engine speed, max", "rpm", "max", ["INMOT", "MITTL_MOTORDREHZAHL_PRO_ZYLSEGM"]),
    ("speed_max", "Speed, max", "km/h", "max", list(SPEEDS)),
    ("boost_max", "Boost pressure, max", "hPa", "max", ["IPLAD", "S_LADEDRUCK"]),
    ("coolant_max", "Coolant temperature, max", "°C", "max", ["ITKUA", "S_TEMP_KUEHLMITTEL"]),
    ("oil_temp_max", "Oil temperature, max", "°C", "max", ["ITOEL"]),
    ("oil_pressure_min", "Oil pressure, min", "hPa", "min", ["OELDRUCK"]),
    ("gearbox_oil_max", "Transmission oil, max", "°C", "max", ["EGS_GETRIEBEOELTMEPERATUR"]),
    ("intake_max", "Intake air, max", "°C", "max", ["RTANS"]),
    ("battery_min", "Battery, min", "V", "min", ["IUIBS"]),
    ("battery_max", "Battery, max", "V", "max", ["IUIBS"]),
    ("knock_b1", "Knock retard, bank 1, mean", "°", "mean",
     ["MITTELW_ZYLIND_ZWSPAET_KR_HALBQUANT"]),
    ("knock_b2", "Knock retard, bank 2, mean", "°", "mean",
     ["MITTELW_ZYLIND_ZWSPAET_KR_HALBQUANT" + B2]),
    ("knock_b1_worst", "Knock retard, bank 1, most", "°", "min",
     ["MITTELW_ZYLIND_ZWSPAET_KR_HALBQUANT"]),
    ("knock_b2_worst", "Knock retard, bank 2, most", "°", "min",
     ["MITTELW_ZYLIND_ZWSPAET_KR_HALBQUANT" + B2]),
    ("trim_b1", "Long-term fuel trim, bank 1", "%", "pct", ["IMUL1"]),
    ("trim_b2", "Long-term fuel trim, bank 2", "%", "pct", ["IMUL1" + B2]),
    ("lambda_b1", "Lambda, bank 1, mean", "", "mean", ["ILAG1"]),
    ("lambda_b2", "Lambda, bank 2, mean", "", "mean", ["ILAG1" + B2, "ILAG2"]),
]
for _n in CYLINDERS:
    METRICS.append(("misfires_%d" % _n, "Misfires, cylinder %d" % _n, "", "increase",
                    ["ZAEHLER_AUSSETZERK_ZYL%d" % _n]
                    + (["ZAEHLER_AUSSETZERK_ZYL%d" % _n + B2] if _n > 4 else [])))
for _n in CYLINDERS:
    METRICS.append(("uneven_%d" % _n, "Uneven running, cylinder %d (spread)" % _n, "rad/s²",
                    "sd", ["ILUZ%d" % _n] + (["ILUZ%d" % _n + B2] if _n > 4 else [])))
_SPARK = _per_cylinder("DIAG_BRENNDAUER_ZUENDFUNKE_CYL%d", "DIAG_BRENNDAUER_ZUENDFUNKE_CYL%d")
for _n in CYLINDERS:
    METRICS.append(("spark_%d" % _n, "Spark burn time, cylinder %d" % _n, "ms", "mean",
                    _SPARK[_n]))
_KNOCK = _per_cylinder("IUKZ%d", "IUKZ%d")
for _n in CYLINDERS:
    METRICS.append(("knock_level_%d" % _n, "Knock sensor level, cylinder %d" % _n, "V*ms",
                    "mean", _KNOCK[_n]))
for _n in range(1, 5):
    block = "DSC_RDC_MESSDATENBLOCK_%d." % _n
    METRICS.append(("tire_%d" % _n, "Tire pressure, sensor %d" % _n, "bar", "mean",
                    [block + "LETZTER_REIFENDRUCKWERT"]))
    METRICS.append(("tire_temp_%d" % _n, "Tire temperature, sensor %d, max" % _n, "°C", "max",
                    [block + "LETZTER_REIFENTEMPERATURWERT"]))

# Scaling from the channel's unit to the column's: battery mV to V.
SCALE = {"battery_min": 0.001, "battery_max": 0.001}

DRIVE_FIELDS = (["file", "start", "minutes", "rows", "km", "odometer_km", "gps_points",
                 "misfires"] + [m[0] for m in METRICS])

# Charts in the History window: (title, unit, [(column, legend)]).
TRENDS = [
    ("Misfires per cylinder", "", [("misfires_%d" % n, "cyl %d" % n) for n in CYLINDERS]),
    ("Uneven running per cylinder (spread)", "rad/s²",
     [("uneven_%d" % n, "cyl %d" % n) for n in CYLINDERS]),
    ("Spark burn time per cylinder", "ms", [("spark_%d" % n, "cyl %d" % n) for n in CYLINDERS]),
    ("Knock retard per bank", "°", [("knock_b1", "bank 1 mean"), ("knock_b2", "bank 2 mean"),
                                    ("knock_b1_worst", "bank 1 most"),
                                    ("knock_b2_worst", "bank 2 most")]),
    ("Long-term fuel trim per bank", "%", [("trim_b1", "bank 1"), ("trim_b2", "bank 2")]),
    ("Tire pressure per sensor", "bar", [("tire_%d" % n, "sensor %d" % n) for n in range(1, 5)]),
    ("Battery", "V", [("battery_min", "min"), ("battery_max", "max")]),
    ("Temperatures, max", "°C", [("coolant_max", "coolant"), ("oil_temp_max", "oil"),
                                 ("gearbox_oil_max", "transmission oil"),
                                 ("intake_max", "intake air")]),
    ("Boost, max", "hPa", [("boost_max", "boost")]),
    ("Distance and odometer", "km", [("km", "drive"), ("odometer_km", "odometer")]),
]

FAULT_FIELDS = ["module", "module_name", "code", "text_en", "now", "first_seen",
                "last_seen", "reads_seen", "reads_present", "last_state", "last_status"]


def folder(logs=None):
    return os.path.join(logs or core.logs_dir(), FOLDER)


# ------------------------------------------------------------------ drives

def _series(rows, name):
    out = []
    for t, values in rows:
        v = values.get(name)
        if isinstance(v, (int, float)) and not (isinstance(v, float) and math.isnan(v)):
            out.append((t, float(v)))
    return out


def _aggregate(how, points):
    values = [v for _, v in points]
    if not values:
        return None
    if how == "mean":
        return sum(values) / len(values)
    if how == "min":
        return min(values)
    if how == "max":
        return max(values)
    if how == "pct":
        return (sum(values) / len(values) - 1.0) * 100.0
    if how == "sd":
        if len(values) < 2:
            return None
        mean = sum(values) / len(values)
        return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))
    if how == "increase":
        # A counter's rises; it can reset (a new drive cycle), so falls don't count.
        return float(sum(max(0.0, b - a) for a, b in zip(values, values[1:])))
    raise ValueError(how)


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def valid_fix(lat, lon):
    """A real position: the head unit sends 0/0 until it has a fix."""
    return (lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180
            and (abs(lat) > 1e-4 or abs(lon) > 1e-4))


def gps_track(rows, lat=LAT, lon=LON):
    """[(t, lat, lon)] of the rows with a real position."""
    out = []
    for t, values in rows:
        a, b = values.get(lat), values.get(lon)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and valid_fix(a, b):
            out.append((t, float(a), float(b)))
    return out


def _distance(rows, names):
    """km driven: the speedometer integrated over time, else the GPS track."""
    for name in SPEEDS:
        if name in names:
            points = _series(rows, name)
            if len(points) >= 2:
                return sum(v * min(t2 - t1, MAX_STEP) / 3600.0
                           for (t1, v), (t2, _) in zip(points, points[1:]) if t2 > t1)
    track = gps_track(rows)
    return sum(haversine_km(a1, o1, a2, o2)
               for (_, a1, o1), (_, a2, o2) in zip(track, track[1:]))


def summarize(names, rows, file="", start=""):
    """One drive's statistics: {column: value}, None where not logged."""
    names = set(names)
    out = {k: None for k in DRIVE_FIELDS}
    times = [t for t, _ in rows]
    out.update(file=file, start=start, rows=len(rows),
               minutes=round((times[-1] - times[0]) / 60.0, 2) if times else 0.0)
    for key, _label, _unit, how, channels in METRICS:
        name = next((c for c in channels if c in names), None)
        if name is None:
            continue
        value = _aggregate(how, _series(rows, name))
        if value is not None:
            out[key] = round(value * SCALE.get(key, 1.0), 4)
    out["km"] = round(_distance(rows, names), 2)
    out["gps_points"] = len(gps_track(rows)) if LAT in names and LON in names else 0
    for name in ODOMETERS:
        if name in names:
            points = _series(rows, name)
            if points:
                out["odometer_km"] = max(v for _, v in points)
                break
    counts = [out["misfires_%d" % n] for n in CYLINDERS if out["misfires_%d" % n] is not None]
    out["misfires"] = sum(counts) if counts else None
    return out


def misfire_text(drive):
    """'cyl 5 x3, cyl 1 x1' for a drive's misfires; '' for none or not logged."""
    parts = [(int(drive.get("misfires_%d" % n) or 0), n) for n in CYLINDERS]
    return ", ".join("cyl %d x%d" % (n, c) for c, n in sorted(parts, reverse=True) if c)


def _first_clock(path):
    """The Clock of a log's first row, else the file's time."""
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            header = next(reader, [])
            if len(header) > 1 and header[1].strip() == "Clock":
                for line in reader:
                    if len(line) > 1 and line[1].strip():
                        return line[1].strip()[:19]
    except (OSError, csv.Error, UnicodeDecodeError):
        pass
    return datetime.datetime.fromtimestamp(os.path.getmtime(path)).strftime(
        "%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------ faults

def _sniff(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        sample = f.read(4096)
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
    except csv.Error:
        return ","


# Status bits that mean a fault really happened (ISO 14229): failed now, this
# cycle, pending, confirmed, or since the last clear. A listed code without
# them never failed: `faults --every-code` lists those too.
FAILED_BITS = 0x2F


def failed(status):
    """True if a fault-list status ("0x28") shows the fault happened; True
    when there's no status to go by."""
    try:
        return bool(int(status, 16) & FAILED_BITS)
    except (TypeError, ValueError):
        return True


def kind(path):
    """'drive', 'faults', 'check' or None, from a CSV's header."""
    try:
        delimiter = _sniff(path)
        with open(path, newline="", encoding="utf-8-sig") as f:
            header = [h.strip().lower() for h in next(csv.reader(f, delimiter=delimiter), [])]
    except (OSError, csv.Error, UnicodeDecodeError):
        return None
    header = [h for h in header if h]
    if not header:
        return None
    if "read_at" in header and "code" in header and "module" in header:
        return "faults"
    if header[:3] == ["module", "did", "job"]:
        return "check"
    if playback._is_time(header[0]):
        return "drive"
    return None


def read_faults(path):
    """A saved fault list's rows: [{read_at, module, module_name, code, status,
    state, text_en}], the "no faults" lines included (they say the module
    was read)."""
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f, delimiter=_sniff(path)):
            row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
            if not row.get("module") or not row.get("read_at"):
                continue
            out.append({k: row.get(k, "") for k in ("read_at", "module", "module_name", "code",
                                                     "status", "state", "text_en")})
    return out


def fault_history(rows):
    """Every fault seen, from fault-list rows of any number of reads:
    [{module, code, text_en, now, first_seen, last_seen, reads_seen, ...}],
    the ones present now first. `now` is the fault in the newest read of its
    module: present, stored (not present), or gone (no longer listed)."""
    seen, latest = {}, {}
    rows = sorted({(r["read_at"], r["module"], r["code"]): r for r in rows}.values(),
                  key=lambda r: r["read_at"])
    for r in rows:
        latest[r["module"]] = max(latest.get(r["module"], ""), r["read_at"])
        if not r["code"] or not failed(r["status"]):
            continue                    # the module was read; this code never failed
        key = (r["module"], r["code"])
        f = seen.setdefault(key, {"module": r["module"], "module_name": r["module_name"],
                                  "code": r["code"], "text_en": r["text_en"],
                                  "first_seen": r["read_at"], "reads_seen": 0,
                                  "reads_present": 0})
        f.update(last_seen=r["read_at"], last_state=r["state"], last_status=r["status"])
        f["module_name"] = r["module_name"] or f["module_name"]
        f["text_en"] = r["text_en"] or f["text_en"]
        f["reads_seen"] += 1
        if r["state"].startswith("present"):
            f["reads_present"] += 1
    for f in seen.values():
        if f["last_seen"] < latest.get(f["module"], ""):
            f["now"] = "gone"
        else:
            f["now"] = "present" if f["last_state"].startswith("present") else "stored"
    order = {"present": 0, "stored": 1, "gone": 2}
    return sorted(seen.values(), key=lambda f: (order[f["now"]], f["module"], f["code"]))


# ------------------------------------------------------------------ the scan

class Summary:
    def __init__(self, drives, faults, folder_path, errors):
        self.drives, self.faults, self.folder, self.errors = drives, faults, folder_path, errors


def _files(logs):
    skip = os.path.normcase(os.path.join(logs, FOLDER))
    for root, dirs, files in os.walk(logs):
        dirs[:] = sorted(d for d in dirs
                         if os.path.normcase(os.path.join(root, d)) != skip)
        for name in sorted(files):
            if name.lower().endswith(".csv"):
                yield os.path.join(root, name)


def load_index(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version") == INDEX_VERSION:
            return data.get("files", {})
    except (OSError, ValueError):
        pass
    return {}


def scan(logs=None, progress=None):
    """Read new and changed files in the logs folder, write drives.csv and
    faults.csv, and return a Summary. `progress(done, total)` is called as
    it goes."""
    logs = logs or core.logs_dir()
    out_dir = folder(logs)
    os.makedirs(out_dir, exist_ok=True)
    index_path = os.path.join(out_dir, INDEX_JSON)
    old = load_index(index_path)
    new, errors = {}, []
    files = list(_files(logs))
    for i, path in enumerate(files, 1):
        rel = os.path.relpath(path, logs)
        st = os.stat(path)
        stamp = [st.st_size, int(st.st_mtime)]
        entry = old.get(rel)
        if entry is None or entry.get("stamp") != stamp:
            entry = {"stamp": stamp, "kind": kind(path)}
            try:
                if entry["kind"] == "drive":
                    names, rows = playback.read_log(path)
                    # Forward slashes: the same name as the file has in the cloud.
                    entry["drive"] = (summarize(names, rows, rel.replace(os.sep, "/"),
                                                _first_clock(path))
                                      if len(rows) >= MIN_ROWS else None)
                elif entry["kind"] == "faults":
                    entry["faults"] = read_faults(path)
            except (OSError, ValueError, csv.Error, UnicodeDecodeError) as e:
                entry = {"stamp": stamp, "kind": None, "error": str(e)}
        if entry.get("error"):
            errors.append("%s: %s" % (rel, entry["error"]))
        new[rel] = entry
        if progress:
            progress(i, len(files))
    tmp = index_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": INDEX_VERSION, "files": new}, f)
    os.replace(tmp, index_path)

    drives = sorted((e["drive"] for e in new.values() if e.get("drive")),
                    key=lambda d: d["start"])
    faults = fault_history([r for e in new.values() for r in e.get("faults") or []])
    _write(os.path.join(out_dir, DRIVES_CSV), DRIVE_FIELDS, drives)
    _write(os.path.join(out_dir, FAULTS_CSV), FAULT_FIELDS, faults)
    return Summary(drives, faults, out_dir, errors)


def _write(path, fields, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: "" if row.get(k) is None else row.get(k) for k in fields})
    os.replace(tmp, path)


def label(key):
    for m in METRICS:
        if m[0] == key:
            return m[1], m[2]
    return key, ""
