"""
n63read.playback - data sources that stand in for the car: made-up demo
data, and replay of a CSV log the GUI recorded earlier.

Both run as threads and push the same ("feedsample", ...) messages the GUI
draws from, so the table and chart work exactly as they do on the car.
No Tk here, so it can be tested without a display.
"""

import bisect
import csv
import math
import random
import re
import threading
import time
from collections.abc import Mapping

from n63read import core

# Real catalog channels, so the demo shows what a watch list really looks
# like, and they stay valid if the user connects with them still watched.
DEMO_CHANNELS = [
    "MITTL_MOTORDREHZAHL_PRO_ZYLSEGM",      # engine speed, 1/min
    "BUS_FAHRPEDALWERT",                    # accelerator pedal, %
    "FZG_GESCHWINDIGKEIT_VA_HA_FASCHWERP",  # vehicle speed, km/h
    "S_LADEDRUCK",                          # boost setpoint, hPa absolute
    "LAMBDA_ISTWERT_SENSOR1_BANK2_2",       # lambda sensor 1, bank 2
    "ITOEL",                                # oil temperature, deg C
    # Head unit (0x63)
    "HU_STATUS_GPS",                        # fix: 12 = 3D position
    "HU_STATUS_GPS_SATINFO.NUMBER_TRACKED_SATS",
    "HU_STATUS_SPEED.GPS_SPEED",            # km/h
    "HU_STATUS_GPS_POSITION.LATITUDE",      # degrees, north positive
    "HU_STATUS_GPS_POSITION.LONGITUDE",     # degrees, east positive
    "HU_STATUS_GPS_POSITION.HEIGHT",        # m
    # Transmission (0x18), DSC (0x29), airbag module's motion sensor (0x01)
    "EGS_ISTGANG.ISTGANG",                  # 3 = gear 1 ... 9 = gear 7, 16 = gear 8
    "EGS_GETRIEBEOELTMEPERATUR",            # deg C
    "DSC_RDC_MESSDATENBLOCK_1.LETZTER_REIFENDRUCKWERT",   # bar
    "ACSM__IMU_WERTE.Y_ACC_SCAL",           # lateral acceleration, m/s^2
]

GEAR_CODES = {1: 3, 2: 4, 3: 5, 4: 6, 5: 7, 6: 8, 7: 9, 8: 16}   # TAB_ISTGANG

# Laps of a 1 km circle at Circuit of the Americas - a public place, not
# anyone's driveway.
DEMO_LAT, DEMO_LON, DEMO_RADIUS_DEG, DEMO_LAP = 30.1346, -97.6359, 0.009, 120.0

DEMO_CYCLE = 30.0       # seconds per idle / pull / cruise / lift loop
MAX_REPLAY_GAP = 2.0    # longest pause honoured between replayed rows


def _pedal(t):
    """Pedal % through one loop: idle, a hard pull, cruise, lift off."""
    p = t % DEMO_CYCLE
    if p < 5:
        return 0.0
    if p < 12:
        return 85.0
    if p < 22:
        return 22.0
    return 0.0


def demo_sample(t, rng=random):
    """Plausible values for every demo channel at t seconds in.

    Returns {name: value}. Shapes are smooth with a little noise, so the
    chart looks like a drive rather than a test pattern.
    """
    p = t % DEMO_CYCLE
    pedal = _pedal(t)
    if p < 5:
        rpm = 650
    elif p < 12:                                    # pull: climbs, then a shift
        rpm = 1500 + (p - 5) * 650 if p < 9.5 else 3200 + (p - 9.5) * 900
    elif p < 22:
        rpm = 2100
    else:                                           # coast back to idle
        rpm = max(650, 2100 - (p - 22) * 400)
    if p < 5:
        speed = 0.0
    elif p < 12:
        speed = (p - 5) * 15
    elif p < 22:
        speed = 105.0
    else:
        speed = max(0.0, 105 - (p - 22) * 13)
    boost = 1000 + 1250 * (pedal / 85.0) ** 1.5
    # Rich under full load, lean on overrun, stoichiometric otherwise.
    lam = 0.86 if pedal > 60 else (1.25 if pedal == 0 and rpm > 700 else 1.0)
    oil = 100 - 60 * math.exp(-t / 120.0)           # warms up over a few minutes

    def jitter(v, frac):
        return v * (1 + rng.uniform(-frac, frac))

    gear = min(8, 1 + int(speed // 18))
    # Cornering on the 1 km lap circle: v^2 / r.
    lateral = (speed / 3.6) ** 2 / (DEMO_RADIUS_DEG * 111_000)

    return {
        "MITTL_MOTORDREHZAHL_PRO_ZYLSEGM": round(jitter(rpm, 0.01)),
        "BUS_FAHRPEDALWERT": round(pedal, 1),
        "FZG_GESCHWINDIGKEIT_VA_HA_FASCHWERP": round(speed, 1),
        "S_LADEDRUCK": round(jitter(boost, 0.01)),
        "LAMBDA_ISTWERT_SENSOR1_BANK2_2": round(jitter(lam, 0.01), 3),
        "ITOEL": round(oil, 1),
        "HU_STATUS_GPS": 12,
        "HU_STATUS_GPS_SATINFO.NUMBER_TRACKED_SATS": 9 + int(t / 7) % 3,
        # GPS reads a touch under the speedometer, as it does in a real car.
        "HU_STATUS_SPEED.GPS_SPEED": round(speed * 0.97),
        "HU_STATUS_GPS_POSITION.LATITUDE": round(
            DEMO_LAT + DEMO_RADIUS_DEG * math.cos(2 * math.pi * t / DEMO_LAP), 7),
        "HU_STATUS_GPS_POSITION.LONGITUDE": round(
            DEMO_LON + DEMO_RADIUS_DEG * math.sin(2 * math.pi * t / DEMO_LAP)
            / math.cos(math.radians(DEMO_LAT)), 7),
        "HU_STATUS_GPS_POSITION.HEIGHT": round(160 + 8 * math.sin(2 * math.pi * t / DEMO_LAP)),
        "EGS_ISTGANG.ISTGANG": GEAR_CODES[gear],
        "EGS_GETRIEBEOELTMEPERATUR": round(85 - 50 * math.exp(-t / 150.0)),
        "DSC_RDC_MESSDATENBLOCK_1.LETZTER_REIFENDRUCKWERT": round(
            2.40 + 0.15 * (1 - math.exp(-t / 300.0)), 3),
        "ACSM__IMU_WERTE.Y_ACC_SCAL": round(jitter(lateral, 0.05) + rng.uniform(-0.05, 0.05), 2),
    }


class Row(Mapping):
    """One log row as {name: value}: a list of values and a column index
    shared by every row, about half the memory of a dict per row."""

    __slots__ = ("index", "values")

    def __init__(self, index, values):
        self.index, self.values = index, values

    def __getitem__(self, name):
        i = self.index[name]
        if i >= len(self.values):
            raise KeyError(name)            # a short row: the cell isn't there
        return self.values[i]

    def __iter__(self):
        return (n for n, i in self.index.items() if i < len(self.values))

    def __len__(self):
        return min(len(self.values), len(self.index))


NUMBER_COMMA = re.compile(r"^[-+]?\d+,\d+$")
CLOCK = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2}(?:[.,]\d+)?)$")


def _cell(text, comma=False):
    """A logged cell back to a value: number if it parses, None if blank.
    `comma`: the file writes decimals with a comma (a semicolon-separated
    export from a German or French Windows)."""
    text = text.strip()
    if text == "":
        return None
    if comma and NUMBER_COMMA.match(text):
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return text                                 # enum text, hex, etc.


def _seconds(text, comma=False):
    """A time cell as seconds: a number, or a clock time hh:mm:ss(.fff)."""
    text = text.strip()
    m = CLOCK.match(text)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + \
            float(m.group(3).replace(",", "."))
    if comma:
        text = text.replace(",", ".")
    return float(text)


def _is_time(name):
    n = name.strip().lower()
    return n.startswith(("time", "zeit", "timestamp")) or n in ("t", "s", "sec", "seconds")


def label_and_unit(name):
    """'Engine speed (rpm)' or 'Boost [psi]' -> ('Engine speed', 'rpm')."""
    m = re.match(r"^(.*?)\s*[(\[]([^)\]]{1,12})[)\]]\s*$", name)
    return (m.group(1).strip() or name, m.group(2).strip()) if m else (name.strip(), "")


def read_log(path):
    """Read a log. Returns (channel names, [(time, {name: value})]), each
    row a read-only mapping.

    Ours (Time first, Clock and Event left out) or another tool's, such as
    BimmerLink's: any CSV whose first column is the time, in seconds or as
    hh:mm:ss, comma- or semicolon-separated. Clock times count from the
    first row. Raises ValueError if the first column isn't a time.
    """
    with open(path, newline="", encoding="utf-8-sig") as f:
        sample = f.read(8192)
        f.seek(0)
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
        except csv.Error:
            delimiter = ","
        comma = delimiter != ","
        reader = csv.reader(f, delimiter=delimiter)
        header = next(reader, None)
        if not header or len(header) < 2 or not _is_time(header[0]):
            raise ValueError("not a log: the first column must be the time "
                             "(named Time, Zeit or the like)")
        cols = [i for i, n in enumerate(header) if i and n not in core.LOG_META and n.strip()]
        names = [header[i].strip() for i in cols]
        index = {n: i for i, n in enumerate(names)}
        rows, first, clock = [], None, None
        for line in reader:
            if not line:
                continue
            try:
                t = _seconds(line[0], comma)
            except ValueError:
                continue                            # stray header or junk row
            if clock is None:
                clock = bool(CLOCK.match(line[0].strip()))
                first = t
            if clock:
                t -= first
                if t < 0:
                    t += 24 * 3600                  # past midnight
            rows.append((t, Row(index, [_cell(line[i], comma) for i in cols if i < len(line)])))
    if not rows:
        raise ValueError("the log has a header but no samples")
    return names, rows


class _Feed(threading.Thread):
    """Base: a thread that pushes samples for the watched channels."""

    kind = ""

    def __init__(self, out_q):
        super().__init__(daemon=True)
        self.out_q = out_q
        self.channels = []
        self.lock = threading.Lock()
        self._stop_event = threading.Event()

    def set_channels(self, chans):
        with self.lock:
            self.channels = [c[0] for c in chans]

    def stop(self):
        self._stop_event.set()

    def _names(self):
        with self.lock:
            return list(self.channels)

    def _emit(self, elapsed, values, missing_note):
        sample = {n: (values[n], "", "") if values.get(n) is not None
                  else (None, "", missing_note if n not in values else "")
                  for n in self._names()}
        self.out_q.put(("feedsample", self, elapsed, sample))


class DemoFeed(_Feed):
    kind = "demo"

    def __init__(self, out_q, interval=0.25):
        super().__init__(out_q)
        self.interval = interval

    def run(self):
        t0 = time.time()
        while not self._stop_event.wait(self.interval):
            t = round(time.time() - t0, 3)
            self._emit(t, demo_sample(t), "no demo data")


class ReplayFeed(_Feed):
    kind = "replay"

    SPEEDS = (1, 2, 4, 8, 16)

    def __init__(self, out_q, rows, speed=1.0):
        super().__init__(out_q)
        self.rows = rows
        self.speed = speed
        self.duration = rows[-1][0] - rows[0][0]
        self.times = [t for t, _ in rows]
        self.paused = False
        self._seek_to = None            # row index to jump to
        self._wake = threading.Event()  # a seek, pause or speed change

    def set_speed(self, speed):
        self.speed = max(0.1, float(speed))
        self._wake.set()

    def seek(self, seconds):
        """Carry on from `seconds` into the log."""
        self._seek_to = min(bisect.bisect_left(self.times, self.times[0] + seconds),
                            len(self.rows) - 1)
        self._wake.set()

    def pause(self, paused):
        self.paused = paused
        self._wake.set()

    def _rest(self, seconds):
        """Wait between rows; a stop, seek, pause or speed change cuts it short."""
        end = time.time() + seconds
        self._wake.clear()
        while not self._stop_event.is_set():
            left = end - time.time()
            if left <= 0 or self._wake.wait(min(left, 0.1)):
                return

    def run(self):
        i = 0
        while i < len(self.rows) and not self._stop_event.is_set():
            if self._seek_to is not None:
                i, self._seek_to = self._seek_to, None
            if self.paused:
                self._rest(0.1)
                continue
            t, values = self.rows[i]
            self._emit(t, values, "not in log")
            if i + 1 < len(self.rows):
                gap = min(MAX_REPLAY_GAP, max(0.0, self.rows[i + 1][0] - t)) / self.speed
                self._rest(gap)
                if self._seek_to is not None or self.paused:
                    continue                # the jump or pause wins over the next row
            i += 1
        if not self._stop_event.is_set():
            self.out_q.put(("feeddone", self))
