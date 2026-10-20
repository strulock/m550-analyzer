"""
n63read.engine - one session with the car, for the app, the CLI and Claude.

A Session connects (borrowing the car from another n63read process that
holds it, else holding it and lending it - see share.open_tester), reads the
watched channels in a loop, keeps the drive log, runs background jobs (a
fault read) between reads, and reconnects when it should. The app's poller,
`n63read log` and Claude's logging and sampling tools are all Sessions, so
they behave the same and never fight over the car:

- whoever holds the car lends it, so a session that starts meanwhile
  borrows it rather than opening a second connection (the car takes two at
  most, and bank 2 needs one of them);
- when the holder stops, a session that was borrowing carries on by taking
  the car over, instead of going offline;
- a session that held the car and lost it (a cable bump, the car dozing
  off) keeps trying while it is logging, so the drive's log carries on.

What a session has to say goes to `out.put(message)`: the app passes its Tk
queue, the CLI a queue it reads, Claude's tools something that keeps count.
Messages are tuples: ("status", "connected" / "reconnecting" /
"disconnected", session), ("sample", seconds, sample), ("error", text,
session), ("joberror", text) and ("logfile", path).
"""

import csv
import os
import queue
import sys
import threading
import time

from n63read import core, diag, share

LOG_FLUSH_SECONDS = 1.0         # a crash or flat battery loses at most this much

# Windows' SetThreadExecutionState flags. The request belongs to the thread
# that makes it and ends with it.
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def keep_awake(on):
    """Ask Windows not to sleep while this thread is connected to the car
    (on=True), or drop the request. The screen may still turn off, and closing
    the lid still follows the power settings. A PC that slept mid-log came
    back to a car still holding the old connection, refusing every request
    for minutes (in-car, 2026-10-19). Elsewhere it does nothing."""
    if sys.platform != "win32":
        return False
    import ctypes
    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
    try:
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
    except (AttributeError, OSError):
        return False


class _Drop:
    def put(self, message):
        pass


def no_answer_text(ip, error):
    """What a failed connect means, for someone at the car."""
    return ("Nothing answered at %s (port %d): %s.\n\n"
            "Check that the IP box holds the car's address, not this PC's "
            "(press Discover to find it), that the ignition is on, and that "
            "Windows Firewall allows n63read on this network."
            % (ip, core.HSFZ_DIAG_PORT, error))


class Session(threading.Thread):
    """One connection to the car and everything that runs on it."""

    def __init__(self, ip, target=core.DME_MASTER, source=core.TESTER_ADDR, timeout=2.0,
                 out=None, layouts=None, bind=None, interval=0.25, batch=True):
        super().__init__(daemon=True)
        self.ip, self.target = ip, target
        # DME channels go to `target`; other modules' channels to their own.
        self.reader = core.ChannelReader(layouts or {}, target, batch=batch)
        self.bind = bind                # the 'via' address, for diagnostics
        self.trace = []                 # first frames on the wire, for diagnostics
        self.source, self.timeout = source, timeout
        self.out_q = out if out is not None else _Drop()
        self.channels = []              # [(name, did, meta)]
        self.lock = threading.Lock()
        self.interval = interval
        # Not `_stop`: that name is a threading.Thread internal, and
        # shadowing it with an Event breaks join()/is_alive().
        self._stop_event = threading.Event()
        self.jobs = queue.Queue()       # callables run on this thread with the socket
        self.steppers = []              # generator jobs, advanced one step per cycle
        self.samples = 0                # read cycles so far
        self.rows = 0                   # rows written to the log so far
        self.stop_after = 0             # rows, then stop (0: until stop())
        self.borrowed = False           # reading through another process's connection
        self.logfile = None
        self.logwriter = None
        self.log_header = None
        self.log_path = None            # file asked for
        self.log_part = 1               # >1 once a channel change has split the log
        self.last_flush = 0.0
        self.log_t0 = None              # when logging started: the log's Time is from here
        self.events = []                # text for the next row's Event cell

    # -- what the session reads -------------------------------------------

    def set_channels(self, chans):
        with self.lock:
            self.channels = list(chans)

    def submit(self, job):
        """Run job(hsfz) on the session's thread, between samples.

        The car gets one connection, so anything else that needs to talk to
        it while connected (the fault reader) goes through here. If the job
        returns a generator, it is advanced one step per cycle, so a long
        job slows logging down but never stops it.
        """
        self.jobs.put(job)

    def stop(self):
        self._stop_event.set()

    # -- the log ------------------------------------------------------------

    def start_log(self, path):
        with self.lock:
            self.log_path, self.log_part = path, 1
            self.log_t0 = time.time()
            self.events = []
            self._open_log(path)

    def mark(self, text):
        """Note something (an alert, a reconnect) in the log's Event column."""
        with self.lock:
            if self.logwriter:
                self.events.append(text)

    def stop_log(self):
        with self.lock:
            if self.logfile:
                self.logfile.close()
            self.logfile = self.logwriter = self.log_header = None

    def _logging(self):
        with self.lock:
            return self.logwriter is not None

    def _open_log(self, path):
        self.logfile = open(path, "w", newline="", encoding="utf-8")
        self.logwriter = csv.writer(self.logfile)
        self.log_header = None
        self.last_flush = time.time()

    def _log_event_now(self, text):
        """A row of its own for an event when no samples are coming (the
        connection just dropped), pushed to disk at once."""
        with self.lock:
            if not self.logwriter:
                return
            if self.log_header is None:
                self.events.append(text)        # no header yet: the first row carries it
                return
            now = time.time()
            blank = {n: (None, "", "") for n in self.log_header}
            self.logwriter.writerow(core.log_row(now - self.log_t0, now, self.log_header,
                                                 blank, text))
            self.logfile.flush()
            os.fsync(self.logfile.fileno())

    def _write_log(self, chans, sample):
        with self.lock:
            if not self.logwriter:
                return
            now = time.time()
            names = [c[0] for c in chans]
            if self.log_header is not None and self.log_header != names:
                # The channel list changed mid-log. A second header row in
                # the same file breaks Excel and pandas, so start a new file.
                self.logfile.close()
                self.log_part += 1
                root, ext = os.path.splitext(self.log_path)
                path = "%s_%d%s" % (root, self.log_part, ext or ".csv")
                self._open_log(path)
                self.out_q.put(("logfile", path))
            if self.log_header is None:
                self.logwriter.writerow(core.log_header(names))
                self.log_header = names
            # Time from the start of the log (a split file carries on the
            # count), the wall clock beside it, and any events since the
            # last row.
            self.logwriter.writerow(core.log_row(now - self.log_t0, now, names, sample,
                                                 "; ".join(self.events)))
            self.events = []
            self.rows += 1
            # Push to disk about once a second, so a crash, sleep or flat
            # battery loses at most that much of the drive.
            if now - self.last_flush >= LOG_FLUSH_SECONDS:
                self.logfile.flush()
                os.fsync(self.logfile.fileno())
                self.last_flush = now

    # -- the loop -------------------------------------------------------------

    def _run_jobs(self, h):
        while True:
            try:
                job = self.jobs.get_nowait()
            except queue.Empty:
                break
            try:
                result = job(h)
            except Exception as e:                                  # noqa: BLE001
                self.out_q.put(("joberror", "background read failed: %s" % e))
                continue
            if hasattr(result, "__next__"):
                self.steppers.append(result)
        # One step of each stepped job per cycle.
        for gen in list(self.steppers):
            try:
                next(gen)
            except StopIteration:
                self.steppers.remove(gen)
            except Exception as e:                                  # noqa: BLE001
                self.steppers.remove(gen)
                self.out_q.put(("joberror", "background read failed: %s" % e))

    def _cycle_until_stopped(self, h, t0):
        while not self._stop_event.is_set():
            cycle = time.time()
            self._run_jobs(h)
            if not self.borrowed:
                h.tidy()                # close bank 2's link when it's idle
            with self.lock:
                chans = list(self.channels)
            if not chans:
                self._stop_event.wait(0.15)
                continue
            elapsed = round(time.time() - t0, 3)
            # The whole cycle under the connection's lock, so requests lent
            # to another program wait for the end of it, not slot in between
            # each read: a fault sweep's 0.5 s waits at empty addresses once
            # stretched a 95-value row to 28 s (in-car, 2026-10-18).
            with h.lock:
                sample = self.reader.read(h, chans)
            self.out_q.put(("sample", elapsed, sample))
            self.samples += 1
            self._write_log(chans, sample)
            if self.stop_after and self.rows >= self.stop_after:
                self.stop()
            self._stop_event.wait(core.rest(self.interval, cycle))

    def run(self):
        t0 = time.time()
        connected = False               # at some point this session
        reason = "stopped"
        lost_at = None                  # when the connection dropped, while it's down
        try:
            while not self._stop_event.is_set():
                try:
                    # Borrow the car from another n63read that holds it, else
                    # hold it ourselves and lend it (share.open_tester).
                    with share.open_tester(self.ip, self.source, self.timeout,
                                           trace=self.trace) as h:
                        self.borrowed = bool(getattr(h, "shared", False))
                        if not connected:
                            diag.record_later("connect", ip=self.ip, result="ok",
                                              seconds=round(time.time() - t0, 3),
                                              bind=self.bind, borrowed=self.borrowed)
                        if not connected:
                            keep_awake(True)    # until this session ends
                        connected = True
                        if lost_at is not None:
                            gap = round(time.time() - lost_at)
                            diag.record("reconnect", ip=self.ip, seconds=gap,
                                        borrowed=self.borrowed)
                            self.mark("reconnected after %d s" % gap)
                            lost_at = None
                        self.out_q.put(("status", "connected", self))
                        self._cycle_until_stopped(h, t0)
                    break                       # stopped
                except Exception as e:                              # noqa: BLE001
                    if self._stop_event.is_set() or not connected:
                        raise
                    # The process we borrowed from has stopped: carry on by
                    # taking the car over, at once. Ours dropped (a cable
                    # bump, the car dozing off): while logging, mark the gap,
                    # keep the file open and keep trying; otherwise give up.
                    took_over = self.borrowed
                    if not (took_over or self._logging()):
                        raise
                    if lost_at is None:
                        lost_at = time.time()
                        diag.record("connection_lost", ip=self.ip, borrowed=took_over,
                                    error="%s: %s" % (type(e).__name__, e))
                        if not took_over:
                            self._log_event_now("connection lost: %s" % e)
                        self.out_q.put(("status", "reconnecting", self))
                    if not took_over:
                        self._stop_event.wait(core.RECONNECT_WAIT)
                    self.borrowed = False
        except Exception as e:                                      # noqa: BLE001
            reason = "%s: %s" % (type(e).__name__, e)
            if connected:
                why = core.connection_problem(e)
                self.out_q.put(("error", "%s\n\n%s" % (e, why) if why else str(e), self))
            else:
                self.out_q.put(("error", no_answer_text(self.ip, e), self))
                diag.connect_failed(self.ip, e, self.bind)
        finally:
            self.stop_log()
            if connected:
                keep_awake(False)
                r = self.reader
                diag.record("session", ip=self.ip, seconds=round(time.time() - t0, 1),
                            samples=self.samples, channels=len(self.channels),
                            reason=reason,
                            refused={"0x%02X:0x%04X" % k: v for k, v in r.refused.items()},
                            silent_modules=["0x%02X" % m for m in r.quiet_until],
                            log=self.log_path, trace=self.trace)
            self.out_q.put(("status", "disconnected", self))
