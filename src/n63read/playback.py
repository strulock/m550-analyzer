"""
n63read.playback - data sources that stand in for the car: made-up demo
data, and replay of a CSV log the GUI recorded earlier.

Both run as threads and push the same ("feedsample", ...) messages the GUI
draws from, so the table and chart work exactly as they do on the car.
No Tk here, so it can be tested without a display.
"""

import csv
import math
import random
import threading
import time

# Real catalog channels, so the demo shows what a watch list really looks
# like, and they stay valid if the user connects with them still watched.
DEMO_CHANNELS = [
    "MITTL_MOTORDREHZAHL_PRO_ZYLSEGM",      # engine speed, 1/min
    "BUS_FAHRPEDALWERT",                    # accelerator pedal, %
    "FZG_GESCHWINDIGKEIT_VA_HA_FASCHWERP",  # vehicle speed, km/h
    "S_LADEDRUCK",                          # boost setpoint, hPa absolute
    "LAMBDA_ISTWERT_SENSOR1_BANK2_2",       # lambda sensor 1, bank 2
    "ITOEL",                                # oil temperature, deg C
]

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

    return {
        "MITTL_MOTORDREHZAHL_PRO_ZYLSEGM": round(jitter(rpm, 0.01)),
        "BUS_FAHRPEDALWERT": round(pedal, 1),
        "FZG_GESCHWINDIGKEIT_VA_HA_FASCHWERP": round(speed, 1),
        "S_LADEDRUCK": round(jitter(boost, 0.01)),
        "LAMBDA_ISTWERT_SENSOR1_BANK2_2": round(jitter(lam, 0.01), 3),
        "ITOEL": round(oil, 1),
    }


def _cell(text):
    """A logged cell back to a value: number if it parses, None if blank."""
    text = text.strip()
    if text == "":
        return None
    try:
        return float(text)
    except ValueError:
        return text                                 # enum text, hex, etc.


def read_log(path):
    """Read a GUI log. Returns (channel names, [(time, {name: value})]).

    Raises ValueError if it is not one of our logs (first column "Time").
    """
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header or header[0] != "Time" or len(header) < 2:
            raise ValueError("not an n63read log: the first column must be 'Time'")
        names = header[1:]
        rows = []
        for line in reader:
            if not line:
                continue
            try:
                t = float(line[0])
            except ValueError:
                continue                            # stray header or junk row
            rows.append((t, {n: _cell(v) for n, v in zip(names, line[1:])}))
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

    def __init__(self, out_q, rows, speed=1.0):
        super().__init__(out_q)
        self.rows = rows
        self.speed = speed
        self.duration = rows[-1][0] - rows[0][0]

    def run(self):
        prev = self.rows[0][0]
        for t, values in self.rows:
            gap = min(MAX_REPLAY_GAP, max(0.0, t - prev)) / self.speed
            if self._stop_event.wait(gap):
                return
            prev = t
            self._emit(t, values, "not in log")
        self.out_q.put(("feeddone", self))
