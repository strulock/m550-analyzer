"""
n63read.gui - Windows-friendly GUI for reading N63TU2 DME measurements over ENET.

Requires: Python 3 only. Tkinter ships with the standard Windows installer.

Run:  n63read-gui   (or: python -m n63read.gui)
"""

import csv
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from n63read import core, playback

CSV_NAME = core.DEFAULT_CSV         # resolved via core.resource_path()
PLOT_POINTS = 240
CHART_LINES = 6                     # lines drawn at once; select rows to choose
LOG_FLUSH_SECONDS = 1.0
COLORS = ["#2d7dd2", "#d1495b", "#1b998b", "#e8a53e", "#8367c7", "#3f88c5"]


# --------------------------------------------------------------- poll thread

class Poller(threading.Thread):
    """Owns the socket. Polls watched channels and pushes samples to a queue."""

    def __init__(self, ip, target, source, timeout, out_q, layouts=None):
        super().__init__(daemon=True)
        self.ip, self.target = ip, target
        # DME channels go to `target`; head unit channels to their own module.
        self.reader = core.ChannelReader(layouts or {}, target)
        self.source, self.timeout = source, timeout
        self.out_q = out_q
        self.channels = []          # [(name, did, meta)]
        self.lock = threading.Lock()
        self.interval = 0.25
        # Not `_stop`: that name is a threading.Thread internal, and
        # shadowing it with an Event breaks join()/is_alive().
        self._stop_event = threading.Event()
        self.jobs = queue.Queue()   # callables run on this thread with the socket
        self.steppers = []          # generator jobs, advanced one step per cycle
        self.logfile = None
        self.logwriter = None
        self.log_header = None
        self.log_path = None        # file the user picked
        self.log_part = 1           # >1 once a channel change has split the log
        self.last_flush = 0.0

    def set_channels(self, chans):
        with self.lock:
            self.channels = list(chans)

    def submit(self, job):
        """Run job(hsfz) on the poll thread, between samples.

        The car gets one connection, so anything else that needs to talk to
        it while connected (the fault reader) goes through here. If the job
        returns a generator, it is advanced one step per poll cycle, so a
        long job slows logging down but never stops it.
        """
        self.jobs.put(job)

    def start_log(self, path):
        with self.lock:
            self.log_path, self.log_part = path, 1
            self._open_log(path)

    def _open_log(self, path):
        self.logfile = open(path, "w", newline="", encoding="utf-8")
        self.logwriter = csv.writer(self.logfile)
        self.log_header = None
        self.last_flush = time.time()

    def stop_log(self):
        with self.lock:
            if self.logfile:
                self.logfile.close()
            self.logfile = self.logwriter = self.log_header = None

    def stop(self):
        self._stop_event.set()

    def _run_jobs(self, h):
        while True:
            try:
                job = self.jobs.get_nowait()
            except queue.Empty:
                break
            try:
                result = job(h)
            except Exception as e:                                  # noqa: BLE001
                self.out_q.put(("error", "background read failed: %s" % e))
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
                self.out_q.put(("error", "background read failed: %s" % e))

    def run(self):
        t0 = time.time()
        try:
            with core.Hsfz(self.ip, source=self.source, timeout=self.timeout) as h:
                self.out_q.put(("status", "connected"))
                while not self._stop_event.is_set():
                    self._run_jobs(h)
                    with self.lock:
                        chans = list(self.channels)
                    if not chans:
                        time.sleep(0.15)
                        continue

                    elapsed = round(time.time() - t0, 3)
                    sample = self.reader.read(h, chans)

                    self.out_q.put(("sample", elapsed, sample))
                    self._write_log(elapsed, chans, sample)
                    time.sleep(self.interval)
        except Exception as e:                                      # noqa: BLE001
            self.out_q.put(("error", str(e)))
        finally:
            self.stop_log()
            self.out_q.put(("status", "disconnected"))

    def _write_log(self, elapsed, chans, sample):
        with self.lock:
            if not self.logwriter:
                return
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
                self.logwriter.writerow(["Time"] + names)
                self.log_header = names
            self.logwriter.writerow(
                [elapsed] + [("" if sample[n][0] is None else sample[n][0]) for n in names]
            )
            # Push to disk about once a second, so a crash, sleep or flat
            # battery loses at most that much of the drive.
            now = time.time()
            if now - self.last_flush >= LOG_FLUSH_SECONDS:
                self.logfile.flush()
                os.fsync(self.logfile.fileno())
                self.last_flush = now


# ---------------------------------------------------------------------- app

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("N63TU2 DME Reader")
        self.geometry("1060x680")
        self.minsize(900, 560)

        self.catalog = self._load_catalog()
        self.layouts = core.record_layouts(self.catalog)
        self.enums = core.load_enum_tables()
        self.watch = []                      # [(name, did, meta)]
        self.series = {}                     # name -> [values]
        self.q = queue.Queue()
        self.poller = None
        self.modules = []                    # addresses found by the last scan
        self.logging_to = None
        self.feed = None                     # DemoFeed or ReplayFeed while not connected

        self._build()
        self._start_demo()
        self.after(60, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._close)

    # -- data ------------------------------------------------------------

    def _load_catalog(self):
        path = core.resource_path(CSV_NAME)
        if not os.path.exists(path):
            messagebox.showerror(
                "Missing catalog",
                "%s not found.\n\nIt is searched for next to the script or "
                "executable, and in the working directory." % CSV_NAME)
            raise SystemExit(1)
        cat = {}
        with open(path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                cat[row["ARG"].strip()] = row
        # Head unit (GPS etc.): optional, so a missing file only hides them.
        cat.update(core.load_head_unit_catalog())
        return cat

    # -- layout ----------------------------------------------------------

    def _build(self):
        bar = ttk.Frame(self, padding=8)
        bar.pack(fill="x")

        ttk.Label(bar, text="IP").pack(side="left")
        self.ip_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self.ip_var, width=18).pack(side="left", padx=(4, 8))

        ttk.Button(bar, text="Discover", command=self._discover).pack(side="left")

        ttk.Label(bar, text="via").pack(side="left", padx=(10, 2))
        self.bind_var = tk.StringVar(value="auto")
        self.bind_combo = ttk.Combobox(bar, textvariable=self.bind_var, width=16,
                                       state="readonly")
        self.bind_combo.pack(side="left")
        self._refresh_interfaces()
        ttk.Button(bar, text="\u21bb", width=3,
                   command=self._refresh_interfaces).pack(side="left", padx=2)

        ttk.Label(bar, text="Target").pack(side="left", padx=(14, 2))
        self.target_var = tk.StringVar(value="0x12")
        ttk.Entry(bar, textvariable=self.target_var, width=7).pack(side="left")

        ttk.Label(bar, text="Interval s").pack(side="left", padx=(14, 2))
        self.interval_var = tk.StringVar(value="0.25")
        ttk.Entry(bar, textvariable=self.interval_var, width=6).pack(side="left")

        self.connect_btn = ttk.Button(bar, text="Connect", command=self._toggle)
        self.connect_btn.pack(side="left", padx=12)

        self.status = ttk.Label(bar, text="\u25cf not connected", foreground="#999")
        self.status.pack(side="left")

        ttk.Button(bar, text="Faults", command=self._faults).pack(side="right", padx=(4, 0))
        ttk.Button(bar, text="Scan modules", command=self._scan).pack(side="right")
        self.replay_btn = ttk.Button(bar, text="Open log...", command=self._toggle_replay)
        self.replay_btn.pack(side="right", padx=(0, 4))

        # --- body: picker | watch
        body = ttk.Panedwindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        left = ttk.Frame(body)
        body.add(left, weight=1)
        ttk.Label(left, text="Channels (%d)" % len(self.catalog)).pack(anchor="w")
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._refilter())
        ttk.Entry(left, textvariable=self.filter_var).pack(fill="x", pady=3)
        self.picker = tk.Listbox(left, selectmode="extended", activestyle="none")
        self.picker.pack(fill="both", expand=True)
        self.picker.bind("<Double-Button-1>", lambda e: self._add())
        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=4)
        ttk.Button(btns, text="Add to watch  \u2192", command=self._add).pack(
            side="left", fill="x", expand=True)
        ttk.Button(btns, text="Add all shown", command=self._add_all).pack(
            side="left", padx=(4, 0))

        right = ttk.Frame(body)
        body.add(right, weight=2)

        self.tree = ttk.Treeview(right, columns=("v", "u", "arg", "id", "note"),
                                 show="tree headings", height=10)
        self.tree.heading("#0", text="Channel")
        for c, t, w in (("v", "Value", 100), ("u", "Unit", 60),
                        ("arg", "Name", 150), ("id", "DID", 70),
                        ("note", "Status", 140)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.column("#0", width=260)
        self.tree.pack(fill="both", expand=True)

        row = ttk.Frame(right)
        row.pack(fill="x", pady=4)
        ttk.Button(row, text="Remove", command=self._remove).pack(side="left")
        ttk.Button(row, text="Clear", command=self._clear).pack(side="left", padx=4)
        self.log_btn = ttk.Button(row, text="Start logging", command=self._toggle_log)
        self.log_btn.pack(side="right")
        self.logging_to = None

        self.canvas = tk.Canvas(right, height=200, bg="#fbfbfb",
                                highlightthickness=1, highlightbackground="#ddd")
        self.canvas.pack(fill="x", pady=(6, 0))

        self._refilter()

    # -- picker ----------------------------------------------------------

    @staticmethod
    def _english(meta):
        """English description, falling back to BMW's German where it's blank."""
        text = (meta.get("INFO_EN") or "").strip()
        if text in ("", "-"):
            text = (meta.get("INFO") or "").strip()
        return "" if text == "-" else text

    def _refilter(self):
        term = self.filter_var.get().lower().strip()
        self.picker.delete(0, "end")
        self._visible = []
        # Undescribed channels sort last and show just their code name.
        rows = sorted(self.catalog.items(),
                      key=lambda kv: (not self._english(kv[1]),
                                      self._english(kv[1]).lower(), kv[0]))
        for name, meta in rows:
            english = self._english(meta)
            label = "%s  -  %s" % (english[:70], name) if english else name
            if meta.get("MODULE"):
                label = "Head unit: " + label
            # Search the German and the code name too: the English is a
            # glossary translation and sometimes leaves words untranslated.
            haystack = " ".join((label, meta.get("INFO") or "")).lower()
            if not term or term in haystack:
                self.picker.insert("end", label)
                self._visible.append(name)

    def _add(self):
        self._add_names([self._visible[i] for i in self.picker.curselection()])

    def _add_all(self):
        """Every channel the filter shows, minus the private ones."""
        names = [n for n in self._visible if not core.is_sensitive(n)]
        skipped = len(self._visible) - len(names)
        if len(names) > 40 and not messagebox.askyesno(
                "Add all", "Add %d channels to the watch list?\n\n"
                "Head unit channels refresh every 1-10 s, a few per cycle, so "
                "DME channels keep their rate." % len(names)):
            return
        self._add_names(names)
        if skipped:
            messagebox.showinfo(
                "Add all", "Skipped %d private channel(s): Wi-Fi password and PIN, "
                "SIM identity, previous destinations, paired devices. Add them "
                "one at a time if you need them." % skipped)

    def _add_names(self, names):
        for name in names:
            if any(n == name for n, _, _ in self.watch):
                continue
            meta = self.catalog[name]
            self.watch.append((name, int(meta["ID"], 16), meta))
            self.series[name] = []
            self.tree.insert("", "end", iid=name, text=self._english(meta) or name,
                             values=("", meta["EINHEIT"], name, meta["ID"], ""))
        self._push_channels()

    def _remove(self):
        for name in self.tree.selection():
            self.watch = [c for c in self.watch if c[0] != name]
            self.series.pop(name, None)
            self.tree.delete(name)
        self._push_channels()

    def _clear(self):
        self.watch.clear()
        self.series.clear()
        self.tree.delete(*self.tree.get_children())
        self._push_channels()

    def _push_channels(self):
        if self.poller:
            self.poller.set_channels(self.watch)
        if self.feed:
            self.feed.set_channels(self.watch)

    def _set_watch(self, names):
        """Replace the watch list with these catalog channels."""
        self._clear()
        for name in names:
            meta = self.catalog[name]
            self.watch.append((name, int(meta["ID"], 16), meta))
            self.series[name] = []
            self.tree.insert("", "end", iid=name, text=self._english(meta) or name,
                             values=("", meta["EINHEIT"], name, meta["ID"], ""))
        self._push_channels()

    # -- demo and replay -------------------------------------------------

    def _start_demo(self):
        """Made-up data so the table and chart show something before connecting."""
        self._set_watch([n for n in playback.DEMO_CHANNELS if n in self.catalog])
        self._start_feed(playback.DemoFeed(self.q))
        self.status.config(text="\u25cf demo data - not connected", foreground="#b8860b")

    def _start_feed(self, feed):
        self._stop_feed()
        self.feed = feed
        feed.set_channels(self.watch)
        feed.start()

    def _stop_feed(self):
        """Stop demo or replay, and drop its points so they never mix with real ones."""
        if not self.feed:
            return
        if self.feed.kind == "replay":
            self.replay_btn.config(text="Open log...")
        self.feed.stop()
        self.feed = None
        for s in self.series.values():
            s.clear()
        self._plot()

    def _toggle_replay(self):
        if self.feed and self.feed.kind == "replay":
            self._stop_feed()
            self.status.config(text="\u25cf replay stopped", foreground="#999")
            return
        if self.poller:
            messagebox.showinfo("Replay", "Disconnect first.")
            return
        path = filedialog.askopenfilename(
            filetypes=[("CSV log", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        try:
            names, rows = playback.read_log(path)
        except (OSError, ValueError, csv.Error) as e:
            messagebox.showerror("Replay", "Could not read %s:\n\n%s"
                                 % (os.path.basename(path), e))
            return
        known = [n for n in names if n in self.catalog]
        if not known:
            messagebox.showerror("Replay", "None of the log's columns are known channels.")
            return
        if len(known) < len(names):
            messagebox.showwarning(
                "Replay", "Skipping columns not in the channel catalog:\n\n"
                + ", ".join(n for n in names if n not in self.catalog))
        self._stop_feed()
        self._set_watch(known)
        feed = playback.ReplayFeed(self.q, rows)
        feed.name_shown = os.path.basename(path)
        self._start_feed(feed)
        self.replay_btn.config(text="Stop replay")

    # -- connection ------------------------------------------------------

    def _refresh_interfaces(self):
        """List local IPv4s. The ENET adapter self-assigns 169.254.x.x."""
        try:
            addrs = core.local_ipv4s()
        except Exception:                                           # noqa: BLE001
            addrs = []
        self.bind_combo["values"] = ["auto"] + addrs
        if self.bind_var.get() not in (["auto"] + addrs):
            self.bind_var.set("auto")
        elif addrs and self.bind_var.get() == "auto":
            link = [a for a in addrs if a.startswith("169.254.")]
            if link:
                self.bind_var.set(link[0])

    def _discover(self):
        self.status.config(text="\u25cf searching...", foreground="#b8860b")
        self.update_idletasks()

        def work():
            bind = self.bind_var.get()
            bind = None if bind == "auto" else bind
            try:
                hits = core.discover(3.0, bind)
            except Exception as e:                                  # noqa: BLE001
                self.q.put(("error", str(e)))
                return
            self.q.put(("discovered", hits))

        threading.Thread(target=work, daemon=True).start()

    def _scan(self):
        ip = self.ip_var.get().strip()
        if not ip:
            messagebox.showinfo("Scan", "Enter or discover an IP first.")
            return
        win = tk.Toplevel(self)
        win.title("Module scan")
        box = tk.Text(win, width=56, height=22)
        box.pack(fill="both", expand=True)
        lo, hi = core.SWEEP_RANGE
        box.insert("end", "Sweeping 0x%02X-0x%02X with TesterPresent...\n\n" % (lo, hi))
        self.modules = []

        # Tk widgets may only be touched from the main thread, so the worker
        # hands lines to the event pump instead of calling box.after itself.
        def emit(text):
            self.q.put(("scanline", box, text))

        def work():
            try:
                with core.Hsfz(ip, source=0xF4, timeout=core.PROBE_TIMEOUT) as h:
                    for t in range(lo, hi + 1):
                        try:
                            h.tester_present(t)
                        except core.NegativeResponse as e:
                            line = "0x%02X  responds (negative: %s)\n" % (
                                t, core.NRC.get(e.code, "0x%02X" % e.code))
                        except core.HsfzError:
                            continue
                        except Exception:                           # noqa: BLE001
                            continue
                        else:
                            line = "0x%02X  responds\n" % t
                        emit(line)
                        self.q.put(("module", t))
                emit("\nDone. Faults will read these modules.\n")
            except Exception as e:                                  # noqa: BLE001
                emit("\nError: %s\n" % e)

        threading.Thread(target=work, daemon=True).start()

    def _toggle(self):
        if self.poller:
            self.poller.stop()
            self.poller = None
            self.connect_btn.config(text="Connect")
            return

        ip = self.ip_var.get().strip()
        if not ip:
            messagebox.showinfo("Connect", "Enter an IP, or press Discover.")
            return
        try:
            target = int(self.target_var.get(), 0)
            interval = float(self.interval_var.get())
        except ValueError:
            messagebox.showerror("Connect", "Target must be hex (0x12); interval a number.")
            return

        self._stop_feed()
        self.status.config(text="\u25cf connecting...", foreground="#b8860b")
        self.poller = Poller(ip, target, 0xF4, 2.0, self.q, self.layouts)
        self.poller.interval = max(0.05, interval)
        self.poller.set_channels(self.watch)
        self.poller.start()
        self.connect_btn.config(text="Disconnect")

    # -- faults ----------------------------------------------------------

    def _faults(self):
        """Read fault memory from every module, show it, and save it to CSV."""
        ip = self.ip_var.get().strip()
        if not ip and not self.poller:
            messagebox.showinfo("Faults", "Enter or discover an IP first.")
            return

        win = tk.Toplevel(self)
        win.title("Fault memory")
        win.geometry("980x460")
        msg = ttk.Label(win, padding=6)
        msg.pack(fill="x")
        cols = (("module", "Module", 60), ("name", "Name", 190), ("code", "Code", 70),
                ("state", "State", 190), ("text", "Description", 440))
        tree = ttk.Treeview(win, columns=[c[0] for c in cols], show="headings")
        for c, t, w in cols:
            tree.heading(c, text=t)
            tree.column(c, width=w, anchor="w")
        tree.pack(fill="both", expand=True, padx=6)
        row = ttk.Frame(win, padding=6)
        row.pack(fill="x")
        save_btn = ttk.Button(row, text="Save CSV...", state="disabled")
        save_btn.pack(side="right")
        win.fault_rows = []
        save_btn.config(command=lambda: self._save_faults(win, ask=True))
        win.parts = {"msg": msg, "tree": tree, "save": save_btn}

        modules = list(self.modules)
        if modules:
            msg.config(text="Reading %d module(s) found by the last scan..." % len(modules))
        else:
            msg.config(text="Finding modules first (about a minute)...")

        def job(h):
            """Generator: one round trip per step, reporting as it goes."""
            for kind, value in core.fault_steps(h, modules or None):
                if kind == "found":
                    self.q.put(("module", value))
                elif kind == "finding":
                    self.q.put(("faultmsg", win, "Finding modules... 0x%02X" % value))
                elif kind == "reading":
                    self.q.put(("faultmsg", win, "Read faults from 0x%02X" % value))
                elif kind == "done":
                    self.q.put(("faults_done", win, value))
                yield

        if self.poller:
            # Share the live connection, a step at a time between samples,
            # so a running log keeps recording (more slowly) during the read.
            self.poller.submit(job)
            return

        def work():
            try:
                with core.Hsfz(ip, source=0xF4, timeout=2.0) as h:
                    for _ in job(h):
                        pass
            except Exception as e:                                  # noqa: BLE001
                self.q.put(("faultmsg", win, "Error: %s" % e))

        threading.Thread(target=work, daemon=True).start()

    def _show_faults(self, win, rows):
        parts = win.parts
        win.fault_rows = rows
        tree = parts["tree"]
        for r in rows:
            tree.insert("", "end", values=(r["module"], r["module_name"], r["code"],
                                           r["state"], r["text_en"] or r["text_de"]))
        n_faults = sum(1 for r in rows if r["code"])
        n_mods = len({r["module"] for r in rows})
        parts["msg"].config(text="%d fault(s) across %d module(s)." % (n_faults, n_mods))
        parts["save"].config(state="normal")
        self._save_faults(win, ask=not self.logging_to)

    def _save_faults(self, win, ask):
        """Save next to the drive log when logging; otherwise ask where."""
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        if not ask and self.logging_to:
            root, _ = os.path.splitext(self.logging_to)
            path = "%s_faults_%s.csv" % (root, stamp)
        else:
            path = filedialog.asksaveasfilename(
                parent=win, defaultextension=".csv", filetypes=[("CSV", "*.csv")],
                initialdir=os.path.dirname(self.logging_to) if self.logging_to else None,
                initialfile="n63_faults_%s.csv" % stamp)
            if not path:
                return
        try:
            core.write_fault_csv(path, win.fault_rows)
        except OSError as e:
            messagebox.showerror("Faults", "Could not save: %s" % e, parent=win)
            return
        msg = win.parts["msg"]
        msg.config(text=msg.cget("text").split("  Saved")[0] + "  Saved to %s" % path)

    # -- logging ---------------------------------------------------------

    def _toggle_log(self):
        if not self.poller:
            messagebox.showinfo("Logging", "Connect first.")
            return
        if self.logging_to:
            self.poller.stop_log()
            self.logging_to = None
            self.log_btn.config(text="Start logging")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")],
            initialfile=time.strftime("n63_%Y-%m-%d_%H-%M-%S.csv"))
        if not path:
            return
        self.poller.start_log(path)
        self.logging_to = path
        self.log_btn.config(text="Stop logging")

    # -- event pump ------------------------------------------------------

    def _drain(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]

                if kind == "sample":
                    self._show_sample(msg[2])

                elif kind == "feedsample":
                    _, feed, elapsed, sample = msg
                    if feed is not self.feed:
                        continue                # left over from a stopped feed
                    self._show_sample(sample)
                    if feed.kind == "replay":
                        self.status.config(
                            text="● replaying %s  %s / %s" % (
                                feed.name_shown, _clock(elapsed - feed.rows[0][0]),
                                _clock(feed.duration)),
                            foreground="#2d7dd2")

                elif kind == "feeddone":
                    if msg[1] is self.feed:
                        self.feed = None
                        self.replay_btn.config(text="Open log...")
                        self.status.config(text="● replay finished", foreground="#999")

                elif kind == "status":
                    ok = msg[1] == "connected"
                    self.status.config(
                        text="\u25cf " + msg[1],
                        foreground="#2a9d3f" if ok else "#999")
                    if not ok:
                        self.connect_btn.config(text="Connect")
                        self.poller = None

                elif kind == "discovered":
                    hits = msg[1]
                    if not hits:
                        messagebox.showinfo(
                            "Discover",
                            "No reply.\n\n"
                            "1. Ignition on (position 2)\n"
                            "2. USB ENET adapter plugged into car and PC\n"
                            "3. Pick the 169.254.x.x address in 'via' - an "
                            "auto broadcast leaves over WiFi instead\n"
                            "4. Allow Python through Windows Firewall, and set "
                            "that network to Private")
                        self.status.config(text="\u25cf not connected", foreground="#999")
                    else:
                        self.ip_var.set(hits[0][0])
                        self.status.config(
                            text="\u25cf found %d" % len(hits), foreground="#2a9d3f")

                elif kind == "module":
                    if msg[1] not in self.modules:
                        self.modules.append(msg[1])

                elif kind == "faultmsg":
                    _, win, text = msg
                    if win.winfo_exists():
                        win.parts["msg"].config(text=text)

                elif kind == "faults_done":
                    _, win, rows = msg
                    if win.winfo_exists():
                        self._show_faults(win, rows)

                elif kind == "logfile":
                    self.logging_to = msg[1]
                    self.status.config(text="● channels changed - logging to %s"
                                       % os.path.basename(msg[1]), foreground="#2a9d3f")

                elif kind == "scanline":
                    _, box, text = msg
                    if box.winfo_exists():
                        box.insert("end", text)
                        box.see("end")

                elif kind == "error":
                    self.status.config(text="\u25cf error", foreground="#c0392b")
                    messagebox.showerror("Connection", msg[1])
                    self.connect_btn.config(text="Connect")
                    self.poller = None
        except queue.Empty:
            pass
        self.after(60, self._drain)

    def _show_sample(self, sample):
        for name, (value, unit, note) in sample.items():
            if not self.tree.exists(name):
                continue
            meta = self.catalog.get(name, {})
            shown = "" if value is None else (
                "%.4g" % value if isinstance(value, float) else value)
            if isinstance(value, float) and meta.get("EINHEIT") == "deg":
                shown = "%.6f" % value          # %.4g would round a coordinate to ~10 km
            if not note:
                # Enumerated values stay numbers (they log and plot); the
                # words go in Status.
                note = core.enum_text(value, meta, self.enums) or ""
            self.tree.set(name, "v", shown)
            self.tree.set(name, "note", note)
            if isinstance(value, (int, float)):
                s = self.series.setdefault(name, [])
                s.append(float(value))
                del s[:-PLOT_POINTS]
        self._plot()

    # -- chart -----------------------------------------------------------

    def _plot(self):
        c = self.canvas
        c.delete("all")
        w = c.winfo_width() or 700
        h = c.winfo_height() or 200
        pad = 28

        if self.feed:
            c.create_text(w - pad, h - 8, anchor="e", fill="#c8c8c8",
                          font=("TkDefaultFont", 9, "bold"),
                          text="DEMO DATA" if self.feed.kind == "demo" else "REPLAY")

        # Chart the rows selected in the table, else the first few numeric
        # ones. Each line has its own scale, so engine speed (thousands),
        # lambda (about 1) and latitude can share the chart; the legend gives
        # each line's range.
        chosen = [n for n in self.tree.selection() if len(self.series.get(n, [])) > 1]
        if not chosen:
            chosen = [n for n, v in self.series.items() if len(v) > 1]
        active = [(n, self.series[n]) for n in chosen[:CHART_LINES]]
        if not active:
            c.create_text(w // 2, h // 2, text="no numeric data yet", fill="#aaa")
            return

        for frac in (0.0, 0.5, 1.0):
            y = pad + (h - 2 * pad) * frac
            c.create_line(pad, y, w - pad, y, fill="#e8e8e8")

        col_w = max(150, (w - 2 * pad) // 3)
        for i, (name, vals) in enumerate(active):
            color = COLORS[i % len(COLORS)]
            lo, hi = min(vals), max(vals)
            if hi - lo < 1e-9:
                lo, hi = lo - 1, hi + 1
            span = hi - lo
            step = (w - 2 * pad) / max(1, PLOT_POINTS - 1)
            pts = []
            start = PLOT_POINTS - len(vals)
            for k, v in enumerate(vals):
                x = pad + (start + k) * step
                y = pad + (h - 2 * pad) * (1 - (v - lo) / span)
                pts += [x, y]
            if len(pts) >= 4:
                c.create_line(*pts, fill=color, width=2, smooth=False)
            label = "%s  %.5g–%.5g" % (name.split(".")[-1][:18], lo, hi)
            c.create_text(pad + (i % 3) * col_w, 8 + (i // 3) * 11, anchor="w",
                          fill=color, font=("TkDefaultFont", 7), text=label)

    def _close(self):
        if self.poller:
            self.poller.stop()
        self._stop_feed()
        self.destroy()


def _clock(seconds):
    seconds = max(0, int(seconds))
    return "%d:%02d" % (seconds // 60, seconds % 60)


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
