"""
n63read.gui - Windows-friendly GUI for reading N63TU2 DME measurements over ENET.

Requires: Python 3 only. Tkinter ships with the standard Windows installer.

Run:  n63read-gui   (or: python -m n63read.gui)
"""

import csv
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import webbrowser

from n63read import __version__, core, diag, playback, update

CSV_NAME = core.DEFAULT_CSV         # resolved via core.resource_path()
PLOT_POINTS = 240
UPDATE_FIRST_CHECK_MS = 10000       # first update check after start
CHART_LINES = 6                     # lines drawn at once; select rows to choose
DME_LABEL = "Engine (DME)"
CHECK_BATCH = 10                    # identifiers checked per poll cycle while logging
# Picker marks for what the car said about a channel's identifier.
ANSWER_MARKS = {"ok": "\u2713  ", "size": "\u2260  ", "refused": "\u2717  ",
                "no answer": "\u2013  "}
# Negative responses meaning the identifier does not exist on this car.
REFUSALS = {core.NRC[c] for c in (0x11, 0x12, 0x31)}
LOG_FLUSH_SECONDS = 1.0
COLORS = ["#2d7dd2", "#d1495b", "#1b998b", "#e8a53e", "#8367c7", "#3f88c5"]


# --------------------------------------------------------------- poll thread

class Poller(threading.Thread):
    """Owns the socket. Polls watched channels and pushes samples to a queue."""

    def __init__(self, ip, target, source, timeout, out_q, layouts=None, bind=None):
        super().__init__(daemon=True)
        self.ip, self.target = ip, target
        # DME channels go to `target`; other modules' channels to their own.
        self.reader = core.ChannelReader(layouts or {}, target)
        self.bind = bind                # the 'via' address, for diagnostics
        self.trace = []                 # first frames on the wire, for diagnostics
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

    def run(self):
        t0 = time.time()
        connected = False
        samples, reason = 0, "stopped"
        try:
            with core.Hsfz(self.ip, source=self.source, timeout=self.timeout,
                           trace=self.trace) as h:
                connected = True
                diag.record_later("connect", ip=self.ip, result="ok",
                                  seconds=round(time.time() - t0, 3), bind=self.bind)
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
                    samples += 1
                    self._write_log(elapsed, chans, sample)
                    time.sleep(self.interval)
        except Exception as e:                                      # noqa: BLE001
            reason = "%s: %s" % (type(e).__name__, e)
            if connected:
                self.out_q.put(("error", str(e)))
            else:
                self.out_q.put(("error", no_answer_text(self.ip, e)))
                diag.connect_failed(self.ip, e, self.bind)
        finally:
            self.stop_log()
            if connected:
                r = self.reader
                diag.record("session", ip=self.ip, seconds=round(time.time() - t0, 1),
                            samples=samples, channels=len(self.channels), reason=reason,
                            refused={"0x%02X:0x%04X" % k: v for k, v in r.refused.items()},
                            silent_modules=["0x%02X" % m for m in r.quiet_until],
                            log=self.log_path, trace=self.trace)
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


def no_answer_text(ip, error):
    """What a failed connect means, for someone at the car."""
    return ("Nothing answered at %s (port %d): %s.\n\n"
            "Check that the IP box holds the car's address, not this PC's "
            "(press Discover to find it), that the ignition is on, and that "
            "Windows Firewall allows n63read on this network."
            % (ip, core.HSFZ_DIAG_PORT, error))


# ---------------------------------------------------------------------- app

class App(tk.Tk):
    def _set_icon(self):
        """The logo in the title bar and taskbar; no logo is no error."""
        try:
            if sys.platform == "win32":
                self.iconbitmap(default=core.resource_path("n63read.ico"))
            else:
                self._icon = tk.PhotoImage(file=core.resource_path("n63read.png"))
                self.iconphoto(True, self._icon)
        except (tk.TclError, OSError):
            pass

    def __init__(self):
        super().__init__()
        self.title("N63TU2 DME Reader")
        self._set_icon()
        self.geometry("1060x680")
        self.minsize(900, 560)

        self.settings = core.load_settings()
        self._updating = False
        self._update_ready = None       # version downloaded, waiting for a restart
        self._update_setup = None       # ...and its verified installer
        self._update_url = None
        threading.Thread(target=update.tidy, daemon=True).start()
        threading.Thread(target=core.logs_dir, daemon=True).start()
        diag.enabled = self.settings.get("diagnostics", True)
        threading.Thread(target=diag.prune, daemon=True).start()
        self._capture = None
        self.lang = self.settings.get("language", "en")
        if self.lang not in core.LANGUAGES:
            self.lang = "en"
        self.catalog = self._load_catalog()
        self.layouts = core.record_layouts(self.catalog)
        self._info, self._order = self._index_catalog()
        self.avail = core.load_availability()   # (module, DID) -> what the car said
        self._avail_dirty = False
        self._checking = None                    # stop flag of a running check
        self._no_enet_address = False            # last Discover found no 169.254.x.x
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
        self.after(10000, self._save_answers)
        self.after(UPDATE_FIRST_CHECK_MS, self._update_tick)
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
        # Other modules (head unit, transmission, DSC...): optional, so a
        # missing file only hides that module's channels.
        cat.update(core.load_module_catalogs())
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

        # How long Scan modules and Faults wait at each address. Raise it at
        # the car if modules that should be there are missed.
        ttk.Label(bar, text="Probe wait s").pack(side="left", padx=(14, 2))
        self.probe_var = tk.StringVar(value="%g" % core.PROBE_TIMEOUT)
        ttk.Entry(bar, textvariable=self.probe_var, width=5).pack(side="left")

        self.connect_btn = ttk.Button(bar, text="Connect", command=self._toggle)
        self.connect_btn.pack(side="left", padx=12)

        self.status = ttk.Label(bar, text="\u25cf not connected", foreground="#999")
        self.status.pack(side="left")

        self.lang_var = tk.StringVar(value=core.LANGUAGES[self.lang])
        lang = ttk.Combobox(bar, textvariable=self.lang_var, width=8, state="readonly",
                            values=list(core.LANGUAGES.values()))
        lang.pack(side="right", padx=(8, 0))
        lang.bind("<<ComboboxSelected>>", lambda e: self._set_language())
        self.diag_btn = ttk.Menubutton(bar, text="Diagnostics \u25be")
        dmenu = tk.Menu(self.diag_btn, tearoff=False)
        self.diag_var = tk.BooleanVar(value=diag.enabled)
        dmenu.add_checkbutton(label="Record connection diagnostics", variable=self.diag_var,
                              command=self._set_diagnostics)
        dmenu.add_command(label="Capture packets for 60 s (administrator)",
                          command=self._capture_packets,
                          state="normal" if diag.is_admin() else "disabled")
        dmenu.add_separator()
        dmenu.add_command(label="Save report...", command=self._save_diag_report)
        dmenu.add_command(label="Open diagnostics folder", command=self._open_diag_folder)
        self.diag_btn["menu"] = dmenu
        self.diag_btn.pack(side="right", padx=(4, 0))
        ttk.Button(bar, text="Faults", command=self._faults).pack(side="right", padx=(4, 0))
        ttk.Button(bar, text="Scan modules", command=self._scan).pack(side="right")
        self.replay_btn = ttk.Button(bar, text="Open log...", command=self._toggle_replay)
        self.replay_btn.pack(side="right", padx=(0, 4))

        # --- footer: version and updates, small, bottom right
        foot = ttk.Frame(self)
        foot.pack(side="bottom", fill="x", padx=8, pady=(0, 2))
        self.version_btn = ttk.Menubutton(foot, text=self._version_text(),
                                          style="Footer.TMenubutton")
        ttk.Style(self).configure("Footer.TMenubutton", foreground="#888",
                                  font=("TkDefaultFont", 8), padding=(2, 0))
        vmenu = tk.Menu(self.version_btn, tearoff=False)
        vmenu.add_command(label="Check for updates now", command=self._check_updates_now)
        self.autoupdate_var = tk.BooleanVar(value=self.settings.get("auto_update", True))
        vmenu.add_checkbutton(label="Automatic updates", variable=self.autoupdate_var,
                              command=self._set_auto_update)
        vmenu.add_command(label="Release notes", command=lambda: webbrowser.open(
            self._update_url or update.RELEASES_PAGE))
        vmenu.add_separator()
        vmenu.add_command(label="Restart to update", command=self._restart_to_update,
                          state="disabled")
        self.version_menu = vmenu
        self.version_btn["menu"] = vmenu
        self.version_btn.pack(side="right")

        # --- body: picker | watch
        body = ttk.Panedwindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        left = ttk.Frame(body)
        body.add(left, weight=1)
        head = ttk.Frame(left)
        head.pack(fill="x")
        self.count_label = ttk.Label(head, text="Channels (%d)" % len(self.catalog))
        self.count_label.pack(side="left")
        self.answers_btn = ttk.Menubutton(head, text="Answers \u25be")
        menu = tk.Menu(self.answers_btn, tearoff=False)
        menu.add_command(label="Check car now", command=self._check_car)
        menu.add_command(label="Stop check", command=self._stop_check)
        menu.add_command(label="Import n63read check CSV...", command=self._import_check)
        menu.add_separator()
        menu.add_command(label="Forget recorded answers", command=self._forget_answers)
        self.answers_btn["menu"] = menu
        self.answers_btn.pack(side="right")

        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._refilter_soon())
        ttk.Entry(left, textvariable=self.filter_var).pack(fill="x", pady=3)
        opts = ttk.Frame(left)
        opts.pack(fill="x")
        ttk.Label(opts, text="Group by").pack(side="left")
        self.group_var = tk.StringVar(value="Module")
        group = ttk.Combobox(opts, textvariable=self.group_var, width=9, state="readonly",
                             values=("Module", "Function"))
        group.pack(side="left", padx=(4, 8))
        group.bind("<<ComboboxSelected>>", lambda e: self._refilter())
        self.only_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Only channels the car answers", variable=self.only_var,
                        command=self._refilter).pack(side="left")

        pick = ttk.Frame(left)
        pick.pack(fill="both", expand=True, pady=(3, 0))
        self.picker = ttk.Treeview(pick, show="tree", selectmode="extended")
        scroll = ttk.Scrollbar(pick, orient="vertical", command=self.picker.yview)
        self.picker.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.picker.pack(side="left", fill="both", expand=True)
        self.picker.bind("<<TreeviewOpen>>", self._fill_open_node)
        self.picker.bind("<Double-Button-1>", self._pick_double)
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

    # -- updates ---------------------------------------------------------

    def _version_text(self):
        where = (" (source)" if not update.running_exe()
                 else "" if update.installed() else " (portable)")
        return "n63read %s%s" % (__version__, where)

    def _set_auto_update(self):
        self.settings["auto_update"] = self.autoupdate_var.get()
        try:
            core.save_settings(self.settings)
        except OSError:
            pass

    def _update_tick(self):
        """Check now and then; never while connected (try again soon)."""
        if self.autoupdate_var.get() and not self._update_ready:
            if self.poller:
                self.after(5 * 60 * 1000, self._update_tick)
                return
            self._start_update(manual=False)
        self.after(update.CHECK_EVERY * 1000, self._update_tick)

    def _check_updates_now(self):
        if self._update_ready:
            self._restart_to_update()
            return
        self._start_update(manual=True)

    def _start_update(self, manual):
        if self._updating:
            return
        self._updating = True
        self.version_btn.config(text=self._version_text() + " \u00b7 checking...")

        def work():
            try:
                if update.running_exe():
                    info = update.update()
                else:                           # from source: report only
                    info = update.check()
                setup = info.get("setup")
                diag.record("update", state="downloaded" if setup else "checked",
                            current=info["current"], latest=info["latest"])
                self.q.put(("update", "ready" if setup else
                            "newer" if info["newer"] else "current",
                            info["latest"], info.get("url"), manual, setup))
            except Exception as e:                                  # noqa: BLE001
                diag.record("update", state="failed", error=str(e))
                self.q.put(("update", "failed", str(e), None, manual))
        threading.Thread(target=work, daemon=True).start()

    def _show_update(self, state, detail, url, manual, setup=None):
        """Footer (and, for a manual check, a message) for an update result."""
        self._updating = False
        self._update_url = url or self._update_url
        text = self._version_text()
        if state == "ready":
            self._update_ready = detail
            self._update_setup = setup
            text += " \u00b7 %s ready \u2014 restart to update" % detail
            self.version_menu.entryconfig("Restart to update", state="normal")
            self.version_btn.configure(style="FooterReady.TMenubutton")
            ttk.Style(self).configure("FooterReady.TMenubutton", foreground="#2d7dd2",
                                      font=("TkDefaultFont", 8), padding=(2, 0))
        elif state == "newer":
            text += " \u00b7 %s available" % detail
        elif state == "failed":
            text += " \u00b7 update check failed"
        self.version_btn.config(text=text)
        if manual:
            if state == "current":
                messagebox.showinfo("Updates", "n63read %s is the latest version." % __version__)
            elif state == "ready":
                if messagebox.askyesno("Updates", "Version %s is downloaded and verified.\n\n"
                                       "Install it now? n63read closes, updates and starts "
                                       "again. Otherwise it installs when you close "
                                       "n63read." % detail):
                    self._restart_to_update()
            elif state == "newer":
                messagebox.showinfo("Updates", "Version %s is available:\n%s"
                                    % (detail, url or update.RELEASES_PAGE))
            else:
                messagebox.showerror("Updates", "Could not update: %s" % detail)

    def _restart_to_update(self):
        if not self._update_setup:
            return
        if self.poller and not messagebox.askyesno(
                "Restart", "This disconnects from the car%s. Restart now?"
                % (" and stops logging" if self.logging_to else "")):
            return
        try:
            update.install(self._update_setup, relaunch=True)
        except update.UpdateError as e:
            messagebox.showerror("Restart", "Could not update: %s" % e)
            return
        self._update_setup = None           # started; don't run it again on close
        self._close()

    # -- diagnostics -----------------------------------------------------

    def _set_diagnostics(self):
        diag.enabled = self.diag_var.get()
        self.settings["diagnostics"] = diag.enabled
        try:
            core.save_settings(self.settings)
        except OSError:
            pass

    def _capture_packets(self):
        """Record the packets themselves (pktmon) for the next minute."""
        if self._capture and not self._capture.done.is_set():
            messagebox.showinfo("Capture", "A capture is already running.")
            return
        cap = diag.Capture(60)
        try:
            cap.start()
        except Exception as e:                                      # noqa: BLE001
            messagebox.showerror("Capture", "Could not start the capture: %s" % e)
            return
        self._capture = cap
        self.status.config(text="\u25cf capturing packets for 60 s: Discover and "
                                "Connect now", foreground="#b8860b")
        messagebox.showinfo(
            "Capture", "Recording all network traffic on this PC for 60 seconds.\n\n"
            "Now press Discover, then Connect. The capture is saved to the "
            "diagnostics folder; send it with a report (Diagnostics > Save report).")

        def finish():
            cap.done.wait()
            if cap.result:
                self.q.put(("note", "capture saved: %s" % os.path.basename(cap.result)))
            else:
                self.q.put(("joberror", "The packet capture could not be saved; see "
                                        "the diagnostics journal for pktmon's output."))
        threading.Thread(target=finish, daemon=True).start()

    def _save_diag_report(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".jsonl", filetypes=[("Diagnostics report", "*.jsonl")],
            initialdir=core.logs_dir(),
            initialfile=time.strftime("n63read_diagnostics_%Y-%m-%d_%H-%M.jsonl"))
        if not path:
            return
        try:
            n = diag.report(path)
        except OSError as e:
            messagebox.showerror("Diagnostics", "Could not save: %s" % e)
            return
        messagebox.showinfo(
            "Diagnostics", "Saved %d entries from the last 7 days to\n%s\n\n"
            "Packet captures, if any, are .pcapng files in the diagnostics folder.\n"
            "The report includes addresses and the car's VIN." % (n, path))

    def _open_diag_folder(self):
        folder = diag.journal_dir()
        os.makedirs(folder, exist_ok=True)
        try:
            os.startfile(folder)                                    # Windows
        except (AttributeError, OSError):
            messagebox.showinfo("Diagnostics", folder)

    # -- picker ----------------------------------------------------------

    def _describe(self, meta):
        """The description in the display language, else the other one."""
        first, second = ("INFO", "INFO_EN") if self.lang == "de" else ("INFO_EN", "INFO")
        text = (meta.get(first) or "").strip()
        if text in ("", "-"):
            text = (meta.get(second) or "").strip()
        return "" if text == "-" else text

    def _enum_lang(self):
        return "text_de" if self.lang == "de" else "text_en"

    def _set_language(self):
        """Switch descriptions and value texts between English and German."""
        code = next(c for c, n in core.LANGUAGES.items() if n == self.lang_var.get())
        if code == self.lang:
            return
        self.lang = code
        self.settings["language"] = code
        try:
            core.save_settings(self.settings)
        except OSError:
            pass                        # the choice still applies this session
        self._info, self._order = self._index_catalog()
        self._refilter()
        for name, _did, meta in self.watch:
            if self.tree.exists(name):
                self.tree.item(name, text=self._describe(meta) or name)
                self.tree.set(name, "note", "")     # refilled in this language

    def _index_catalog(self):
        """Per channel: module label, function group, record, label, search text.

        Done once: classifying 8,900 channels takes about half a second.
        The values a module returns in one reply (a record) stay together,
        so a record is classified once, by its prefixed job name and first
        field.
        """
        info, record_group = {}, {}
        for name, meta in self.catalog.items():
            english = self._describe(meta)
            label = "%s  -  %s" % (english[:70], name) if english else name
            if meta.get("MODULE"):
                module = int(meta["MODULE"], 16)
                module_label = core.MODULE_LABELS.get(module, "0x%02X" % module)
                key = (module, meta["ID"])
                prefix_job = name.split(".")[0]
                if key not in record_group:
                    # The job name says what a record is; a field's text
                    # ("wheel electronics identifier") can mislead, so it
                    # only decides when the name says nothing.
                    group = core.function_group(prefix_job)
                    if group == core.OTHER_GROUP:
                        group = core.function_group(
                            prefix_job, meta.get("INFO"), meta.get("INFO_EN"))
                    record_group[key] = group
                func = record_group[key]
                record = prefix_job if "." in name else None
            else:
                module_label = DME_LABEL
                func = core.function_group(name, meta.get("INFO"), english)
                record = None
            # Search both languages and the code name, whichever is shown.
            hay = " ".join((label, module_label, func, meta.get("INFO") or "",
                            meta.get("INFO_EN") or "")).lower()
            info[name] = (module_label, func, record, label, hay)
        # Undescribed channels sort last and show just their code name.
        order = sorted(info, key=lambda n: (not self._describe(self.catalog[n]),
                                            info[n][3].lower()))
        return info, order

    def _avail_key(self, name):
        meta = self.catalog[name]
        if meta.get("MODULE"):
            return int(meta["MODULE"], 16), int(meta["ID"], 16)
        return self._dme_target(), int(meta["ID"], 16)

    def _dme_target(self):
        try:
            return int(self.target_var.get(), 0)
        except ValueError:
            return core.DME_MASTER

    def _answer_mark(self, name):
        return ANSWER_MARKS.get(self.avail.get(self._avail_key(name)), "     ")

    def _refilter_soon(self):
        """Typing in the filter rebuilds the tree once the typing pauses."""
        if getattr(self, "_refilter_job", None):
            self.after_cancel(self._refilter_job)
        self._refilter_job = self.after(250, self._refilter)

    def _refilter(self):
        self._refilter_job = None
        term = self.filter_var.get().lower().strip()
        by_module = self.group_var.get() == "Module"
        only = self.only_var.get()
        was_open = {i for i in self._all_nodes() if self.picker.item(i, "open")}
        self.picker.delete(*self.picker.get_children())
        self._members, self._pending = {}, {}
        self._expand = False

        groups, shown = {}, 0
        for name in self._order:
            module, func, record, _, hay = self._info[name]
            if term and term not in hay:
                continue
            if only and self.avail.get(self._avail_key(name)) not in core.ANSWERS:
                continue
            top, sub = (module, func) if by_module else (func, module)
            groups.setdefault(top, {}).setdefault(sub, {}).setdefault(record, []).append(name)
            shown += 1
        total = len(self.catalog)
        self.count_label.config(text="Channels (%d of %d)" % (shown, total)
                                if shown != total else "Channels (%d)" % total)
        if not groups:
            text = ("No answers recorded yet: Answers > Check car now, or connect "
                    "and watch some channels" if only and not self.avail
                    else "Nothing matches")
            self.picker.insert("", "end", iid="none", text=text)
            return

        expand = self._expand = bool(term) and shown <= 300
        top_kind, sub_kind = ("module", "function") if by_module else ("function", "module")
        for top in sorted(groups, key=lambda g: self._group_sort(g, top_kind)):
            top_iid = "g|" + top
            subs = groups[top]
            self._members[top_iid] = [n for sub in subs.values()
                                      for names in sub.values() for n in names]
            self.picker.insert("", "end", iid=top_iid,
                               text="%s  (%d)" % (top, len(self._members[top_iid])),
                               open=expand or top_iid in was_open)
            for sub in sorted(subs, key=lambda g: self._group_sort(g, sub_kind)):
                sub_iid = "%s|%s" % (top_iid, sub)
                self._members[sub_iid] = [n for ns in subs[sub].values() for n in ns]
                self._pending[sub_iid] = subs[sub]
                self.picker.insert(top_iid, "end", iid=sub_iid,
                                   text="%s  (%d)" % (sub, len(self._members[sub_iid])))
                self.picker.insert(sub_iid, "end", iid=sub_iid + "|...", text="...")
                if expand or sub_iid in was_open:
                    self._fill(sub_iid)
                    self.picker.item(sub_iid, open=True)

    @staticmethod
    def _group_sort(name, kind):
        """Modules: DME first, then A-Z. Functions: FUNCTION_GROUPS order, Other last.

        `kind` matters: "Climate" is both a module and a function group.
        """
        if kind == "module":
            return (0, "") if name == DME_LABEL else (1, name)
        order = [g for g, _ in core.FUNCTION_GROUPS]
        return (len(order), "") if name not in order else (order.index(name), "")

    def _all_nodes(self, parent=""):
        for iid in self.picker.get_children(parent):
            yield iid
            yield from self._all_nodes(iid)

    def _fill_open_node(self, _event=None):
        iid = self.picker.focus()
        if iid in self._pending:
            self._fill(iid)

    def _fill(self, iid):
        """Insert a group's channels the first time it opens."""
        records = self._pending.pop(iid, None)
        if records is None:
            return
        self.picker.delete(*self.picker.get_children(iid))
        for record in sorted(records, key=lambda r: (r is not None, r or "")):
            names = records[record]
            parent = iid
            if record is not None and len(self.layouts.get(
                    self._avail_key(names[0]), ())) > 1:
                # Values one reply returns together stay together.
                parent = "%s|%s" % (iid, record)
                self._members[parent] = names
                self.picker.insert(iid, "end", iid=parent, open=self._expand,
                                   text="%s  (%d)" % (record, len(names)))
            for name in names:
                self.picker.insert(parent, "end", iid="c|" + name,
                                   text=self._answer_mark(name) + self._info[name][3])

    def _picked(self):
        """Selected channel names, and whether a whole group was selected."""
        names, group = [], False
        for iid in self.picker.selection():
            if iid.startswith("c|"):
                names.append(iid[2:])
            elif iid in self._members:
                names.extend(self._members[iid])
                group = True
        seen = set()
        return [n for n in names if not (n in seen or seen.add(n))], group

    def _pick_double(self, event):
        iid = self.picker.identify_row(event.y)
        if iid.startswith("c|"):
            self._add_names([iid[2:]])

    def _add(self):
        names, group = self._picked()
        if group:
            self._add_checked(names, "Add group")
        else:
            self._add_names(names)

    def _add_all(self):
        """Every channel the filter shows, minus the private ones."""
        tops = [i for i in self.picker.get_children() if i in self._members]
        self._add_checked([n for t in tops for n in self._members[t]], "Add all")

    def _add_checked(self, names, title):
        """Add many at once: skip private values, confirm a long list."""
        keep = [n for n in names if not core.is_sensitive(n)]
        skipped = len(names) - len(keep)
        if len(keep) > 40 and not messagebox.askyesno(
                title, "Add %d channels to the watch list?\n\n"
                "Channels on other modules (head unit, transmission, DSC...) "
                "refresh every 1-10 s, a few per cycle, so DME channels keep "
                "their rate." % len(keep)):
            return
        self._add_names(keep)
        if skipped:
            messagebox.showinfo(
                title, "Skipped %d private channel(s): Wi-Fi password and PIN, "
                "SIM identity, previous destinations, paired devices. Add them "
                "one at a time if you need them." % skipped)

    # -- what the car answers --------------------------------------------

    def _learn(self, sample):
        """Record, from live reads, which identifiers this car answers."""
        for name, (value, _unit, note) in sample.items():
            if name not in self.catalog:
                continue
            status = "ok" if value is not None else "refused" if note in REFUSALS else None
            key = self._avail_key(name)
            if status and self.avail.get(key) != status:
                self.avail[key] = status
                self._avail_dirty = True

    def _save_answers(self):
        """Every few seconds: save what was learned, and show it in the picker."""
        if self._avail_dirty:
            self._avail_dirty = False
            try:
                core.save_availability(self.avail)
            except OSError:
                pass                    # a read-only profile shouldn't stop logging
            self._refresh_marks()
        self.after(10000, self._save_answers)

    def _refresh_marks(self):
        """Update the marks in place; rebuild only if they decide what shows.

        A rebuild every few seconds while connected would drop the
        selection someone is in the middle of making.
        """
        if self.only_var.get():
            self._refilter()
            return
        for iid in self._all_nodes():
            if iid.startswith("c|"):
                name = iid[2:]
                self.picker.item(iid, text=self._answer_mark(name) + self._info[name][3])

    def _answers_changed(self, note):
        try:
            core.save_availability(self.avail)
        except OSError as e:
            note += " (not saved: %s)" % e
        self._avail_dirty = False
        self._refilter()
        self.status.config(text="\u25cf " + note, foreground="#2d7dd2")

    def _check_car(self):
        """Ask the car for every catalogued identifier, as `n63read check` does."""
        if self._checking:
            messagebox.showinfo("Check car", "A check is already running.")
            return
        ip = self.ip_var.get().strip()
        if not ip and not self.poller:
            messagebox.showinfo("Check car", "Enter or discover an IP first.")
            return
        if not self.poller and self._own_address(ip, "Check car"):
            return
        layouts = core.check_layouts(self.catalog, self._dme_target())
        if not messagebox.askyesno(
                "Check car", "Ask the car for all %d identifiers?\n\n"
                "It only reads, takes a few minutes, and the answers are kept "
                "for next time. While connected, logging slows down during the "
                "check but doesn't stop." % len(layouts)):
            return
        stop = self._checking = {"stop": False}
        total = len(layouts)

        reported = []

        def job(h):
            finished = False
            try:
                for i, (key, status) in enumerate(core.check_steps(h, layouts), 1):
                    self.q.put(("checkres", key, status, i, total))
                    if stop["stop"]:
                        break
                    if i % CHECK_BATCH == 0:
                        yield               # let a sample through
                finished = True
            finally:
                # Also on a failed read or a disconnect mid-check, or the
                # next "Check car now" would say one is still running.
                reported.append(True)
                self.q.put(("checkdone", stop["stop"] or not finished))

        if self.poller:
            self.poller.submit(job)
            return

        def work():
            try:
                with core.Hsfz(ip, source=0xF4, timeout=2.0) as h:
                    for _ in job(h):
                        pass
            except Exception as e:                                  # noqa: BLE001
                self.q.put(("joberror", "check failed: %s" % e))
                if not reported:                # never got as far as the job
                    self.q.put(("checkdone", True))

        threading.Thread(target=work, daemon=True).start()

    def _stop_check(self):
        if self._checking:
            self._checking["stop"] = True

    def _import_check(self):
        path = filedialog.askopenfilename(
            title="n63read check CSV", filetypes=[("CSV", "*.csv"), ("All files", "*.*")],
            initialdir=core.logs_dir())
        if not path:
            return
        try:
            found = core.import_check_csv(path)
        except (OSError, ValueError, csv.Error) as e:
            messagebox.showerror("Import", "Could not read %s:\n\n%s"
                                 % (os.path.basename(path), e))
            return
        self.avail.update(found)
        answered = sum(1 for v in found.values() if v in core.ANSWERS)
        self._answers_changed("imported %d answers (%d answered)" % (len(found), answered))

    def _forget_answers(self):
        if self.avail and messagebox.askyesno(
                "Forget answers", "Forget what the car answered (%d identifiers)?"
                % len(self.avail)):
            self.avail.clear()
            self._answers_changed("recorded answers forgotten")

    def _add_names(self, names):
        for name in names:
            if any(n == name for n, _, _ in self.watch):
                continue
            meta = self.catalog[name]
            self.watch.append((name, int(meta["ID"], 16), meta))
            self.series[name] = []
            self.tree.insert("", "end", iid=name, text=self._describe(meta) or name,
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
            self.tree.insert("", "end", iid=name, text=self._describe(meta) or name,
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
            filetypes=[("CSV log", "*.csv"), ("All files", "*.*")],
            initialdir=core.logs_dir())
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
        """List local IPv4s and keep 'via' on one that exists. Returns them.

        The ENET adapter self-assigns 169.254.x.x, and picks a new one when
        the link resets (unplugged, ignition cycled), so a remembered
        address can vanish. Then, as for "auto", take the adapter's current
        address; only with no adapter address at all fall back to "auto".
        """
        try:
            addrs = core.local_ipv4s()
        except Exception:                                           # noqa: BLE001
            addrs = []
        self.bind_combo["values"] = ["auto"] + addrs
        link = core.link_local(addrs)
        if self.bind_var.get() not in addrs:
            self.bind_var.set(link[0] if link else "auto")
        return addrs

    def _discover(self):
        # Re-read the addresses first: the one in 'via' may have gone since
        # start-up (WinError 10049 when sending from it).
        before = self.bind_var.get()
        addrs = self._refresh_interfaces()
        bind = self.bind_var.get()
        self._no_enet_address = not core.link_local(addrs)
        if before not in ("auto", bind):
            note = "\u25cf %s is gone; searching via %s..." % (before, bind)
        else:
            note = "\u25cf searching..."
        self.status.config(text=note, foreground="#b8860b")
        self.update_idletasks()

        def work():
            # The answer goes to the window at once; the slow network
            # snapshot for the diagnostics journal is taken after.
            try:
                diag.discover(3.0, None if bind == "auto" else bind,
                              on_result=lambda hits, near:
                              self.q.put(("discovered", hits, near)))
            except Exception as e:                                  # noqa: BLE001
                self.q.put(("joberror", "discover failed: %s" % e))

        threading.Thread(target=work, daemon=True).start()

    def _probe_wait(self, title):
        """The Probe wait box as seconds, or None after telling the user why not."""
        try:
            return core.parse_probe_timeout(self.probe_var.get())
        except ValueError:
            lo, hi = core.PROBE_TIMEOUT_RANGE
            messagebox.showerror(title, "Probe wait must be a number of seconds "
                                        "between %g and %g." % (lo, hi))
            return None

    def _scan(self):
        ip = self.ip_var.get().strip()
        if not ip:
            messagebox.showinfo("Scan", "Enter or discover an IP first.")
            return
        wait = self._probe_wait("Scan")
        if wait is None:
            return
        win = tk.Toplevel(self)
        win.title("Module scan")
        box = tk.Text(win, width=56, height=22)
        box.pack(fill="both", expand=True)
        lo, hi = core.SWEEP_RANGE
        box.insert("end", "Sweeping 0x%02X-0x%02X with TesterPresent, waiting %g s "
                          "per address (up to %d s)...\n\n"
                   % (lo, hi, wait, round(core.sweep_seconds(wait))))
        self.modules = []

        # Tk widgets may only be touched from the main thread, so the worker
        # hands lines to the event pump instead of calling box.after itself.
        def emit(text):
            self.q.put(("scanline", box, text))

        def work():
            try:
                with core.Hsfz(ip, source=0xF4, timeout=wait) as h:
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

    def _own_address(self, ip, title):
        """True (after saying so) if `ip` is this PC's, not the car's.

        Easy to do: ipconfig shows the adapter's 169.254.x.x, and the car's
        address looks just the same.
        """
        try:
            mine = ip in core.local_ipv4s()
        except Exception:                                           # noqa: BLE001
            return False
        if mine:
            messagebox.showinfo(
                title, "%s is this PC's own address (the ENET adapter).\n\n"
                "The car has its own 169.254.x.x address. Pick this one under "
                "'via' and press Discover to find the car's." % ip)
        return mine

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
        if self._own_address(ip, "Connect"):
            return
        try:
            target = int(self.target_var.get(), 0)
            interval = float(self.interval_var.get())
        except ValueError:
            messagebox.showerror("Connect", "Target must be hex (0x12); interval a number.")
            return

        self._stop_feed()
        self.status.config(text="\u25cf connecting...", foreground="#b8860b")
        via = self.bind_var.get()
        self.poller = Poller(ip, target, 0xF4, 2.0, self.q, self.layouts,
                             bind=None if via == "auto" else via)
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
        if not self.poller and self._own_address(ip, "Faults"):
            return
        wait = self._probe_wait("Faults")
        if wait is None:
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
            msg.config(text="Finding modules first (up to %d s at %g s per address)..."
                       % (round(core.sweep_seconds(wait)), wait))

        def job(h):
            """Generator: one round trip per step, reporting as it goes."""
            for kind, value in core.fault_steps(h, modules or None, probe_timeout=wait):
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
                                           r["state"], (r["text_de"] or r["text_en"])
                                           if self.lang == "de" else
                                           (r["text_en"] or r["text_de"])))
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
                initialdir=os.path.dirname(self.logging_to) if self.logging_to
                else core.logs_dir(),
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
            initialdir=core.logs_dir(),
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
                    self._learn(msg[2])
                    self._show_sample(msg[2])

                elif kind == "checkres":
                    _, key, status, i, total = msg
                    if status in core.ANSWERS or status == "refused" \
                            or key not in self.avail:
                        self.avail[key] = status
                    if i % 50 == 0 or i == total:
                        self.status.config(text="\u25cf checking %d / %d" % (i, total),
                                           foreground="#b8860b")

                elif kind == "checkdone":
                    self._checking = None
                    by_module = {}
                    for (module, _did), status in self.avail.items():
                        counts = by_module.setdefault("0x%02X" % module, {})
                        counts[status] = counts.get(status, 0) + 1
                    diag.record("check", stopped=msg[1], modules=by_module)
                    answered = sum(1 for v in self.avail.values() if v in core.ANSWERS)
                    self._answers_changed("check %s: %d identifiers answer"
                                          % ("stopped" if msg[1] else "done", answered))

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
                    near = msg[2] if len(msg) > 2 else []
                    hsfz = [h for h in hits if core.reply_protocol(h[1]) == "HSFZ"]
                    if hits and not hsfz:
                        self.ip_var.set(hits[0][0])
                        self.status.config(text="\u25cf car speaks DoIP", foreground="#c0392b")
                        messagebox.showinfo(
                            "Discover",
                            "The car at %s answered with DoIP (port 13400), not "
                            "HSFZ, which is what this tool speaks. Please report "
                            "this: DoIP support can be added." % hits[0][0])
                    elif not hits and near:
                        self.ip_var.set(near[0])
                        self.status.config(text="\u25cf no reply; %s is on the link"
                                           % near[0], foreground="#b8860b")
                        messagebox.showinfo(
                            "Discover",
                            "The car didn't answer Discover, but %s is on the car's "
                            "link, so it's probably the car. It's in the IP box: "
                            "press Connect.%s" % (near[0], "\n\nAlso seen: " +
                                                  ", ".join(near[1:]) if near[1:] else ""))
                    elif not hits:
                        messagebox.showinfo(
                            "Discover",
                            "No reply.\n\n"
                            + ("This PC has no 169.254.x.x (ENET) address yet: "
                               "the adapter isn't connected or Windows hasn't "
                               "set it up (it takes 30-60 s after plugging in).\n\n"
                               if self._no_enet_address else "")
                            + "1. Ignition on (position 2)\n"
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

                elif kind == "update":
                    self._show_update(*msg[1:])

                elif kind == "note":
                    # Information for the status line only; not a state change.
                    self.status.config(text="● " + msg[1], foreground="#2d7dd2")

                elif kind == "joberror":
                    # A failed fault read, check or discovery: say so, but
                    # it is not a lost connection, so leave the poller be.
                    if not self.poller:
                        self.status.config(text="● error", foreground="#c0392b")
                    messagebox.showerror("Error", msg[1])

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
                note = core.enum_text(value, meta, self.enums, self._enum_lang()) or ""
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
        self._stop_check()
        if self._avail_dirty:
            try:
                core.save_availability(self.avail)
            except OSError:
                pass
        if self.poller:
            self.poller.stop()
        self._stop_feed()
        if self._update_setup:              # a downloaded update installs on the way out
            try:
                update.install(self._update_setup)
            except update.UpdateError:
                pass
        self.destroy()


def _clock(seconds):
    seconds = max(0, int(seconds))
    return "%d:%02d" % (seconds // 60, seconds % 60)


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
