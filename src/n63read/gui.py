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

from n63read import core

CSV_NAME = core.DEFAULT_CSV         # resolved via core.resource_path()
PLOT_POINTS = 240
COLORS = ["#2d7dd2", "#d1495b", "#1b998b", "#e8a53e", "#8367c7", "#3f88c5"]


# --------------------------------------------------------------- poll thread

class Poller(threading.Thread):
    """Owns the socket. Polls watched channels and pushes samples to a queue."""

    def __init__(self, ip, target, source, timeout, out_q):
        super().__init__(daemon=True)
        self.ip, self.target = ip, target
        self.source, self.timeout = source, timeout
        self.out_q = out_q
        self.channels = []          # [(name, did, meta)]
        self.lock = threading.Lock()
        self.interval = 0.25
        # Not `_stop`: that name is a threading.Thread internal, and
        # shadowing it with an Event breaks join()/is_alive().
        self._stop_event = threading.Event()
        self.logfile = None
        self.logwriter = None
        self.log_header = None

    def set_channels(self, chans):
        with self.lock:
            self.channels = list(chans)

    def start_log(self, path):
        with self.lock:
            self.logfile = open(path, "w", newline="", encoding="utf-8")
            self.logwriter = csv.writer(self.logfile)
            self.log_header = None

    def stop_log(self):
        with self.lock:
            if self.logfile:
                self.logfile.close()
            self.logfile = self.logwriter = self.log_header = None

    def stop(self):
        self._stop_event.set()

    def run(self):
        t0 = time.time()
        try:
            with core.Hsfz(self.ip, source=self.source, timeout=self.timeout) as h:
                self.out_q.put(("status", "connected"))
                while not self._stop_event.is_set():
                    with self.lock:
                        chans = list(self.channels)
                    if not chans:
                        time.sleep(0.15)
                        continue

                    sample, elapsed = {}, round(time.time() - t0, 3)
                    for name, did, meta in chans:
                        try:
                            value, unit = core.decode(h.read_did(self.target, did), meta)
                            sample[name] = (value, unit, "")
                        except core.NegativeResponse as e:
                            sample[name] = (None, "", core.NRC.get(e.code, "0x%02X" % e.code))
                        except Exception as e:                      # noqa: BLE001
                            sample[name] = (None, "", str(e)[:28])

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
            if self.log_header != names:
                self.logwriter.writerow(["Time"] + names)
                self.log_header = names
            self.logwriter.writerow(
                [elapsed] + [("" if sample[n][0] is None else sample[n][0]) for n in names]
            )


# ---------------------------------------------------------------------- app

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("N63TU2 DME Reader")
        self.geometry("1060x680")
        self.minsize(900, 560)

        self.catalog = self._load_catalog()
        self.watch = []                      # [(name, did, meta)]
        self.series = {}                     # name -> [values]
        self.q = queue.Queue()
        self.poller = None

        self._build()
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

        ttk.Button(bar, text="Scan modules", command=self._scan).pack(side="right")

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
        ttk.Button(left, text="Add to watch  \u2192", command=self._add).pack(fill="x", pady=4)

        right = ttk.Frame(body)
        body.add(right, weight=2)

        self.tree = ttk.Treeview(right, columns=("v", "u", "id", "note"),
                                 show="tree headings", height=10)
        self.tree.heading("#0", text="Channel")
        for c, t, w in (("v", "Value", 110), ("u", "Unit", 70),
                        ("id", "DID", 80), ("note", "Status", 150)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.column("#0", width=250)
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

    def _refilter(self):
        term = self.filter_var.get().lower().strip()
        self.picker.delete(0, "end")
        self._visible = []
        for name, meta in sorted(self.catalog.items()):
            label = "%s  -  %s" % (name, meta["INFO"][:60])
            if not term or term in label.lower():
                self.picker.insert("end", label)
                self._visible.append(name)
                if len(self._visible) >= 800:
                    break

    def _add(self):
        for i in self.picker.curselection():
            name = self._visible[i]
            if any(n == name for n, _, _ in self.watch):
                continue
            meta = self.catalog[name]
            self.watch.append((name, int(meta["ID"], 16), meta))
            self.series[name] = []
            self.tree.insert("", "end", iid=name, text=name,
                             values=("", meta["EINHEIT"], meta["ID"], ""))
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
        box.insert("end", "Sweeping 0x00-0x60 with TesterPresent...\n\n")

        # Tk widgets may only be touched from the main thread, so the worker
        # hands lines to the event pump instead of calling box.after itself.
        def emit(text):
            self.q.put(("scanline", box, text))

        def work():
            try:
                with core.Hsfz(ip, source=0xF4, timeout=1.0) as h:
                    for t in range(0x00, 0x61):
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
                emit("\nDone.\n")
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

        self.status.config(text="\u25cf connecting...", foreground="#b8860b")
        self.poller = Poller(ip, target, 0xF4, 2.0, self.q)
        self.poller.interval = max(0.05, interval)
        self.poller.set_channels(self.watch)
        self.poller.start()
        self.connect_btn.config(text="Disconnect")

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
                    _, elapsed, sample = msg
                    for name, (value, unit, note) in sample.items():
                        if not self.tree.exists(name):
                            continue
                        shown = "" if value is None else (
                            "%.4g" % value if isinstance(value, float) else value)
                        self.tree.set(name, "v", shown)
                        self.tree.set(name, "note", note)
                        if isinstance(value, (int, float)):
                            s = self.series.setdefault(name, [])
                            s.append(float(value))
                            del s[:-PLOT_POINTS]
                    self._plot()

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

    # -- chart -----------------------------------------------------------

    def _plot(self):
        c = self.canvas
        c.delete("all")
        w = c.winfo_width() or 700
        h = c.winfo_height() or 200
        pad = 28

        active = [(n, v) for n, v in self.series.items() if len(v) > 1]
        if not active:
            c.create_text(w // 2, h // 2, text="no numeric data yet", fill="#aaa")
            return

        lo = min(min(v) for _, v in active)
        hi = max(max(v) for _, v in active)
        if hi - lo < 1e-9:
            lo, hi = lo - 1, hi + 1
        span = hi - lo

        for frac in (0.0, 0.5, 1.0):
            y = pad + (h - 2 * pad) * frac
            c.create_line(pad, y, w - pad, y, fill="#e8e8e8")
            c.create_text(pad - 4, y, anchor="e", font=("TkDefaultFont", 7),
                          fill="#888", text="%.3g" % (hi - span * frac))

        for i, (name, vals) in enumerate(active):
            color = COLORS[i % len(COLORS)]
            step = (w - 2 * pad) / max(1, PLOT_POINTS - 1)
            pts = []
            start = PLOT_POINTS - len(vals)
            for k, v in enumerate(vals):
                x = pad + (start + k) * step
                y = pad + (h - 2 * pad) * (1 - (v - lo) / span)
                pts += [x, y]
            if len(pts) >= 4:
                c.create_line(*pts, fill=color, width=2, smooth=False)
            c.create_text(pad + 6 + i * 130, 12, anchor="w", fill=color,
                          font=("TkDefaultFont", 8), text=name[:16])

    def _close(self):
        if self.poller:
            self.poller.stop()
        self.destroy()


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
