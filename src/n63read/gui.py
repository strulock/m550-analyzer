"""
n63read.gui - Windows-friendly GUI for reading N63TU2 DME measurements over ENET.

Requires: Python 3 only. Tkinter ships with the standard Windows installer.

Run:  n63read-gui   (or: python -m n63read.gui)
"""

import csv
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

import webbrowser

from n63read import (__version__, cloud, core, diag, engine, graph, history, historywin,
                     maps,
                     playback, share, theme, update)

CSV_NAME = core.DEFAULT_CSV         # resolved via core.resource_path()
PLOT_POINTS = 240
HISTORY_POINTS = 20000          # timed points kept per channel for pop-out graphs
UPDATE_FIRST_CHECK_MS = 10000       # first update check after start
CHART_LINES = 6                     # lines drawn at once; select rows to choose
DME_LABEL = "Engine (DME)"
BANK2_LABEL = "Engine bank 2 (DME 0x13)"
RECORD_LABEL_MAX = 70                   # characters of a record's description shown
CHART_NAME_MAX = 30                     # ...and of a line's name on the chart
CHECK_BATCH = 10                    # identifiers checked per poll cycle while logging
# Picker marks for what the car said about a channel's identifier.
ANSWER_MARKS = {"ok": "\u2713  ", "size": "\u2260  ", "refused": "\u2717  ",
                "no answer": "\u2013  "}
# Negative responses meaning the identifier does not exist on this car.
REFUSALS = {core.NRC[c] for c in (0x11, 0x12, 0x31)}
COLORS = ["#2d7dd2", "#d1495b", "#1b998b", "#e8a53e", "#8367c7", "#3f88c5"]
GPS_LAT, GPS_LON = history.LAT, history.LON
# What the map colours the route by at first, the first one watched.
MAP_DEFAULTS = list(history.SPEEDS) + ["INMOT", "MITTL_MOTORDREHZAHL_PRO_ZYLSEGM"]


class _MapSource:
    """What the map window draws, from the app: during a replay the whole
    log's route with the marker where the replay is; otherwise the route so
    far, from the watched GPS channels."""

    def __init__(self, app):
        self.app = app

    def _replay(self):
        feed = self.app.feed
        if feed is None or feed.kind != "replay" or getattr(feed, "whole", False):
            return None
        return feed

    def gps_names(self):
        names = [n for n, _, _ in self.app.watch]
        lat = GPS_LAT if GPS_LAT in names else next(
            (n for n in names if "LATITUDE" in n.upper()), None)
        lon = GPS_LON if GPS_LON in names else next(
            (n for n in names if "LONGITUDE" in n.upper()), None)
        return lat, lon

    def track(self):
        lat, lon = self.gps_names()
        if not lat or not lon:
            return []
        feed = self._replay()
        if feed is not None:
            if getattr(feed, "map_track", None) is None:
                feed.map_track = history.gps_track(feed.rows, lat, lon)
            return feed.map_track
        a, o = self.app.history.get(lat), self.app.history.get(lon)
        if not a or not o:
            return []
        lons = dict(o)
        return [(t, la, lons[t]) for t, la in a
                if t in lons and history.valid_fix(la, lons[t])]

    def channels(self):
        skip = set(self.gps_names())
        return [(name, self.app._describe(meta) or name)
                for name, _did, meta in self.app.watch if name not in skip]

    def values(self, name):
        feed = self._replay()
        if feed is not None:
            cache = getattr(feed, "map_values", None)
            if cache is None:
                cache = feed.map_values = {}
            if name not in cache:
                cache[name] = [(t, float(v.get(name))) for t, v in feed.rows
                               if isinstance(v.get(name), (int, float))]
            return cache[name]
        h = self.app.history.get(name)
        return list(h) if h else []

    def shown(self, name, value):
        meta = self.app.catalog.get(name, {})
        unit = self.app._shown_unit(meta)
        try:
            value = graph.converter(meta.get("EINHEIT"), unit)(value)
        except ValueError:
            unit = meta.get("EINHEIT") or ""
        return "%.4g" % value, unit

    def now(self):
        lat, _ = self.gps_names()
        h = self.app.history.get(lat) if lat else None
        last = h.last() if h else None
        return last[0] if last else None

    @property
    def seek(self):
        feed = self._replay()
        if feed is None:
            return None

        def go(t):
            offset = max(0.0, t - feed.rows[0][0])
            feed.seek(offset)
            self.app._scrub_shown(offset)
        return go

    def default(self):
        names = [n for n, _, _ in self.app.watch]
        return next((n for n in MAP_DEFAULTS if n in names), None)

    def limits(self, name):
        """The channel's alert limits (car's units), or None."""
        return self.app.alerts.get(name)


# --------------------------------------------------------------- poll thread

class Poller(engine.Session):
    """The app's session with the car (see engine.Session): its messages go
    to the Tk event queue."""

    def __init__(self, ip, target, source, timeout, out_q, layouts=None, bind=None):
        super().__init__(ip, target, source, timeout, out=out_q, layouts=layouts, bind=bind)


no_answer_text = engine.no_answer_text


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
        self.title("BMW Analyzer")
        self._set_icon()
        self.geometry("1060x680")
        self.minsize(900, 560)

        self.settings = core.load_settings()
        self._updating = False
        self._update_ready = None       # version downloaded, waiting for a restart
        self._closing = False           # set by _close: stop drawing
        self._update_setup = None       # ...and its verified installer
        self._update_url = None
        threading.Thread(target=update.tidy, daemon=True).start()
        threading.Thread(target=core.logs_dir, daemon=True).start()
        diag.enabled = self.settings.get("diagnostics", True)
        threading.Thread(target=diag.prune, daemon=True).start()
        self._capture = None
        self.lang = self.settings.get("language", "en")
        self.units = self.settings.get("units", "metric")         # or "imperial"
        self.theme_name = self.settings.get("theme", "light")     # light, dark, system
        self.palette = theme.palette(self.theme_name)
        self.chart_hover = None                                   # x over the chart
        if self.lang not in core.LANGUAGES:
            self.lang = "en"
        self.catalog = self._load_catalog()
        self.layouts = core.record_layouts(self.catalog)
        self._info, self._order = self._index_catalog()
        self.avail = core.load_availability()   # (module, DID) -> what the car said
        self._avail_dirty = False
        self._checking = None                    # stop flag of a running check
        self._check_rows, self._check_jobs = [], {}   # its answers, for the check CSV
        self._no_enet_address = False            # last Discover found no 169.254.x.x
        self.enums = core.load_enum_tables()
        self.watch = []                      # [(name, did, meta)]
        self.series = {}                     # name -> [values]
        self.history = {}                    # name -> graph.History of (seconds, value)
        self.graphs = {}                     # name -> its pop-out GraphWindow
        self._cells = {}                     # (name, column) -> text the table shows
        self._chart_dirty = False            # a sample came in; redraw after the drain
        self._hover_job = None
        self._keys = {}                      # (name, DME address) -> availability key
        self._dme = core.DME_MASTER          # the DME address box, as a number
        self._marks_for = None               # answered keys the picker was last built for
        self._pairs = []                     # [(bank 1, bank 2)] both watched
        self._hidden = set()                 # bank-2 rows folded into bank 1
        self._numbers = {}                   # name -> value as shown, for the bank delta
        self.alerts = {k: tuple(v) for k, v in (self.settings.get("alerts") or {}).items()}
        self._alerting = {}                  # name -> "low"/"high" while out of range
        self.compare_var = tk.BooleanVar(value=self.settings.get("compare_banks", False))
        self.q = queue.Queue()
        self.poller = None
        self.modules = []                    # addresses found by the last scan
        self.logging_to = None
        self.feed = None                     # DemoFeed or ReplayFeed while not connected
        self.map_window = None
        self.history_window = None
        self.tiles = maps.TileCache(fetch=self.settings.get("map_tiles", True))
        self.cloud = cloud.AutoSync(self.settings).start()      # idle until set up

        self._build()
        self.palette = theme.apply(self, self.theme_name)
        self._style_tags()
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
        return core.add_derived(core.add_bank2(cat))

    # -- layout ----------------------------------------------------------

    def _build(self):
        bar = self._top_bar = ttk.Frame(self, padding=8)
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
        self.target_var.trace_add("write", lambda *a: self._target_changed())
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
        ttk.Button(bar, text="Settings", command=self._open_settings).pack(
            side="right", padx=(8, 0))
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
        dmenu.add_separator()
        dmenu.add_command(label="Connect to Claude...", command=self._connect_claude)
        self.diag_btn["menu"] = dmenu
        self.diag_btn.pack(side="right", padx=(4, 0))
        ttk.Button(bar, text="History", command=self._open_history).pack(side="right",
                                                                         padx=(4, 0))
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

        # "b2" and "d" (bank 2 and the difference) only show while comparing
        # banks; they come last so the rows' positional values stay put.
        self.tree = ttk.Treeview(right, columns=("v", "u", "arg", "id", "note", "b2", "d"),
                                 show="tree headings", height=10)
        self.tree.heading("#0", text="Channel")
        for c, t, w in (("v", "Value", 100), ("u", "Unit", 60),
                        ("arg", "Name", 150), ("id", "DID", 70),
                        ("note", "Status", 140), ("b2", "Bank 2", 90),
                        ("d", "Δ 1−2", 80)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.column("#0", width=260)
        self._show_columns()
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-Button-1>", lambda e: self._pop_out(
            [self.tree.identify_row(e.y)] if self.tree.identify_row(e.y) else None))

        row = ttk.Frame(right)
        row.pack(fill="x", pady=4)
        ttk.Button(row, text="Remove", command=self._remove).pack(side="left")
        ttk.Button(row, text="Clear", command=self._clear).pack(side="left", padx=4)
        ttk.Button(row, text="Graph", command=self._pop_out).pack(side="left")
        ttk.Button(row, text="Map", command=self._open_map).pack(side="left", padx=(4, 0))
        ttk.Button(row, text="Alert...", command=self._edit_alert).pack(side="left", padx=(4, 0))
        ttk.Checkbutton(row, text="Compare banks", variable=self.compare_var,
                        command=self._compare_changed).pack(side="left", padx=(8, 0))
        self.templates_btn = ttk.Menubutton(row, text="Templates \u25be")
        self.templates_menu = tk.Menu(self.templates_btn, tearoff=False,
                                      postcommand=self._fill_templates_menu)
        self.templates_btn["menu"] = self.templates_menu
        self.templates_btn.pack(side="left", padx=4)
        self.template_name = ""
        self.log_btn = ttk.Button(row, text="Start logging", command=self._toggle_log)
        self.log_btn.pack(side="right")
        self.logging_to = None

        self.canvas = tk.Canvas(right, height=200, bg="#fbfbfb",
                                highlightthickness=1, highlightbackground="#ddd")
        self.canvas.pack(fill="x", pady=(6, 0))
        self.canvas.bind("<Motion>", lambda e: self._chart_hover(e.x))
        self.canvas.bind("<Leave>", lambda e: self._chart_hover(None))
        self._build_replay_bar()

        self._refilter()

    # -- updates ---------------------------------------------------------

    def _version_text(self):
        where = (" (source)" if not update.running_exe()
                 else "" if update.installed() else " (portable)")
        return "n63read %s%s" % (__version__, where)

    def _set_auto_update(self):
        self._save_setting("auto_update", self.autoupdate_var.get())

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
        # The installer can't replace files another n63read is using (Claude's
        # connection, a command-line log); a half-done update breaks the app.
        others = update.others_running()
        if others:
            if not messagebox.askyesno(
                    "Restart", "Other n63read programs are running from this folder:\n\n%s"
                    "\n\nThey have to close for the update. Close them and update now? "
                    "(Claude reconnects to n63read when you restart it.)"
                    % "\n".join("  %s (process %d)" % (name, pid) for pid, name in others)):
                return
            update.stop([pid for pid, _ in others])
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
        self._save_setting("diagnostics", diag.enabled)

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

    def _connect_claude(self):
        """Add n63read's MCP server to Claude Code and Claude Desktop."""
        from n63read import mcp
        self.status.config(text="\u25cf setting up Claude...", foreground="#2d7dd2")

        def work():
            try:
                done = mcp.register()
            except Exception as e:                                  # noqa: BLE001
                done = [("Claude", False, str(e))]
            self.q.put(("claude", done))
        threading.Thread(target=work, daemon=True).start()

    def _show_claude(self, done):
        from n63read import mcp
        self.status.config(text="\u25cf Claude set up" if any(ok for _, ok, _ in done)
                           else "\u25cf Claude not set up", foreground="#2d7dd2")
        if not done:
            messagebox.showinfo(
                "Connect to Claude",
                "Neither Claude Code nor Claude Desktop is installed on this PC.\n\n"
                "Install one (claude.ai/download), sign in, and choose Connect to "
                "Claude again. Claude will then be able to check the connection to "
                "the car, read it, and review your logs, read-only.")
            return
        lines = ["%s: %s%s" % (where, "" if ok else "FAILED - ", message)
                 for where, ok, message in done]
        diag.record("claude", state="registered", result=lines)
        claude = mcp._claude_code()
        ok_code = any(ok for where, ok, _ in done if where == "Claude Code")
        text = "\n".join(lines) + "\n\nRestart Claude Desktop to pick it up."
        if ok_code and claude and messagebox.askyesno(
                "Connect to Claude", text + "\n\nStart Claude Code now, in the logs folder?"):
            try:
                subprocess.Popen(
                    [claude, "Use the n63read tools: check the status, then review the "
                             "recent diagnostics and logs, and tell me what you find."],
                    cwd=core.logs_dir(),
                    creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
            except OSError as e:
                messagebox.showerror("Connect to Claude", "Could not start Claude Code: %s" % e)
        elif not ok_code:
            (messagebox.showinfo if any(ok for _, ok, _ in done)
             else messagebox.showerror)("Connect to Claude", text)

    # -- picker ----------------------------------------------------------

    def _describe(self, meta):
        """The description in the display language, else the other one."""
        first, second = ("INFO", "INFO_EN") if self.lang == "de" else ("INFO_EN", "INFO")
        text = (meta.get(first) or "").strip()
        if text in ("", "-"):
            text = (meta.get(second) or "").strip()
        return "" if text == "-" else text

    def _record_label(self, record, meta):
        """A record's folder: what the module says it is, then its job name."""
        first, second = (("JOB_INFO", "JOB_INFO_EN") if self.lang == "de"
                         else ("JOB_INFO_EN", "JOB_INFO"))
        text = (meta.get(first) or meta.get(second) or "").strip()
        if not text or text == "-" or text.upper() == text:
            return record                   # no description, or just the name again
        if len(text) > RECORD_LABEL_MAX:
            text = text[:RECORD_LABEL_MAX].rsplit(" ", 1)[0].rstrip(",;:") + "\u2026"
        return "%s  -  %s" % (text, record)

    def _enum_lang(self):
        return "text_de" if self.lang == "de" else "text_en"

    def _set_language(self):
        """Switch descriptions and value texts between English and German."""
        code = next(c for c, n in core.LANGUAGES.items() if n == self.lang_var.get())
        if code == self.lang:
            return
        self.lang = code
        self._save_setting("language", code)
        self._info, self._order = self._index_catalog()
        self._refilter()
        for name, _did, meta in self.watch:
            if self.tree.exists(name):
                self.tree.item(name, text=self._describe(meta) or name)
                self._set_cell(name, "note", "")    # refilled in this language
        for name, window in list(self.graphs.items()):
            if window.winfo_exists():
                window.set_language(code, self._describe(self.catalog.get(name, {})))
        self._plot()

    # -- settings --------------------------------------------------------

    def _shown_unit(self, meta):
        """The unit a channel is displayed in, for the chosen unit system."""
        return graph.display_unit(meta.get("EINHEIT"), self.units)

    def _shown_value(self, value, meta):
        """A number in the displayed unit (the logged one stays the car's)."""
        try:
            return graph.converter(meta.get("EINHEIT"), self._shown_unit(meta))(value)
        except ValueError:
            return value

    def _open_settings(self):
        if getattr(self, "settings_window", None) and self.settings_window.winfo_exists():
            self.settings_window.lift()
            return
        win = self.settings_window = tk.Toplevel(self)
        win.title("Settings")
        win.resizable(False, False)
        win.transient(self)
        body = ttk.Frame(win, padding=14)
        body.pack(fill="both", expand=True)

        def group(title, row):
            ttk.Label(body, text=title, font=("TkDefaultFont", 9, "bold")).grid(
                row=row, column=0, sticky="nw", padx=(0, 18), pady=(0, 10))
            frame = ttk.Frame(body)
            frame.grid(row=row, column=1, sticky="w", pady=(0, 10))
            return frame

        f = group("Language", 0)
        for name in core.LANGUAGES.values():
            ttk.Radiobutton(f, text=name, value=name, variable=self.lang_var,
                            command=self._set_language).pack(side="left", padx=(0, 10))
        f = group("Units", 1)
        self.units_var = tk.StringVar(value=self.units)
        for value, text in (("metric", "Metric (\u00b0C, bar, km/h)"),
                            ("imperial", "Imperial (\u00b0F, psi, mph)")):
            ttk.Radiobutton(f, text=text, value=value, variable=self.units_var,
                            command=lambda: self._set_units(self.units_var.get())).pack(
                anchor="w")
        ttk.Label(f, text="Logs keep the car's own units.", foreground=self.palette["muted"]
                  ).pack(anchor="w")
        f = group("Theme", 2)
        self.theme_var = tk.StringVar(value=self.theme_name)
        for value, text in (("light", "Light"), ("dark", "Dark"), ("system", "Like Windows")):
            ttk.Radiobutton(f, text=text, value=value, variable=self.theme_var,
                            command=lambda: self._set_theme(self.theme_var.get())).pack(
                side="left", padx=(0, 10))
        f = group("Updates", 3)
        ttk.Checkbutton(f, text="Update automatically", variable=self.autoupdate_var,
                        command=self._set_auto_update).pack(anchor="w")
        f = group("Diagnostics", 4)
        ttk.Checkbutton(f, text="Record connection diagnostics", variable=self.diag_var,
                        command=self._set_diagnostics).pack(anchor="w")
        f = group("Map", 5)
        self.tiles_var = tk.BooleanVar(value=self.settings.get("map_tiles", True))
        ttk.Checkbutton(f, text="Download street map tiles (OpenStreetMap)",
                        variable=self.tiles_var, command=self._set_map_tiles).pack(anchor="w")
        ttk.Label(f, text="Downloading tells the tile server roughly where the map is. "
                          "Tiles already downloaded still show without it.",
                  foreground=self.palette["muted"], wraplength=360).pack(anchor="w")
        f = group("Cloud", 6)
        self.cloud_url_var = tk.StringVar(value=self.settings.get("cloud_url", ""))
        self.cloud_id_var = tk.StringVar(value=self.settings.get("cloud_client_id", ""))
        self.cloud_secret_var = tk.StringVar(value=cloud.config(self.settings)[2])
        self.cloud_auto_var = tk.BooleanVar(value=self.settings.get("cloud_auto", True))
        for label, var, show in (("Worker URL", self.cloud_url_var, ""),
                                 ("Access client ID", self.cloud_id_var, ""),
                                 ("Access client secret", self.cloud_secret_var, "*")):
            line = ttk.Frame(f)
            line.pack(anchor="w", fill="x")
            ttk.Label(line, text=label, width=18).pack(side="left")
            ttk.Entry(line, textvariable=var, width=46, show=show).pack(side="left")
        line = ttk.Frame(f)
        line.pack(anchor="w", pady=(4, 0))
        ttk.Checkbutton(line, text="Upload logs automatically", variable=self.cloud_auto_var,
                        command=self._save_cloud).pack(side="left")
        ttk.Button(line, text="Save", command=self._save_cloud).pack(side="left", padx=(10, 4))
        ttk.Button(line, text="Sync now", command=self._sync_now).pack(side="left")
        self.cloud_status = ttk.Label(f, text=self.cloud.status, wraplength=420,
                                      foreground=self.palette["muted"])
        self.cloud_status.pack(anchor="w")
        self._cloud_status_tick(win)
        ttk.Button(body, text="Close", command=win.destroy).grid(
            row=7, column=1, sticky="e")
        theme.apply(self, self.theme_name)

    def _save_cloud(self):
        self.settings["cloud_url"] = self.cloud_url_var.get().strip()
        self.settings["cloud_client_id"] = self.cloud_id_var.get().strip()
        self.settings["cloud_client_secret"] = cloud.protect(self.cloud_secret_var.get().strip())
        self._save_setting("cloud_auto", bool(self.cloud_auto_var.get()))
        if cloud.configured(self.settings) and self.cloud.status == "not set up":
            self.cloud.status = "waiting"

    def _sync_now(self):
        self._save_cloud()
        if not cloud.configured(self.settings):
            self.cloud_status.config(text="Set the Worker URL and the service token first.")
            return
        threading.Thread(target=self.cloud.run_once, daemon=True).start()

    def _cloud_status_tick(self, win):
        if win.winfo_exists():
            self.cloud_status.config(text=self.cloud.status)
            win.after(1000, lambda: self._cloud_status_tick(win))

    def _set_map_tiles(self):
        on = bool(self.tiles_var.get())
        self._save_setting("map_tiles", on)
        self.tiles.fetch = on

    def _save_setting(self, key, value):
        self.settings[key] = value
        try:
            core.save_settings(self.settings)
        except OSError as e:            # it still applies this session
            diag.record("problem", where="saving settings", error=str(e))

    def _set_units(self, system):
        if system not in ("metric", "imperial") or system == self.units:
            return
        self.units = system
        self._save_setting("units", system)
        for name, _did, meta in self.watch:
            if self.tree.exists(name):
                self.tree.set(name, "u", self._shown_unit(meta))
                self._set_cell(name, "v", "")           # refilled in the new unit
        for name, window in list(self.graphs.items()):
            if window.winfo_exists():
                window.set_unit(self._shown_unit(self.catalog.get(name, {})))
        self._plot()

    def _set_theme(self, name):
        if name not in theme.THEMES:
            return
        self.theme_name = name
        self._save_setting("theme", name)
        self.palette = theme.apply(self, name)
        self._style_tags()
        for window in list(self.graphs.values()) + [self.map_window]:
            if window is not None and window.winfo_exists():
                window.set_colors(self.palette)
        self._plot()

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
                module_label = (core.CALC_LABEL if core.is_derived(meta) else
                                BANK2_LABEL if meta.get("TARGET") else DME_LABEL)
                func = core.function_group(name, meta.get("INFO"), english)
                record = None
            # Search both languages and the code name, whichever is shown.
            hay = " ".join((label, module_label, func, meta.get("INFO") or "",
                            meta.get("INFO_EN") or "", meta.get("JOB_INFO") or "",
                            meta.get("JOB_INFO_EN") or "")).lower()
            info[name] = (module_label, func, record, label, hay)
        # Undescribed channels sort last and show just their code name.
        order = sorted(info, key=lambda n: (not self._describe(self.catalog[n]),
                                            info[n][3].lower()))
        return info, order

    def _avail_key(self, name):
        # Asked for every channel on every sample and every picker rebuild:
        # worked out once, not by parsing the ID and reading a Tk variable.
        target = self._dme
        key = self._keys.get((name, target))
        if key is None:
            meta = self.catalog[name]
            key = (core.channel_target(meta, target), int(meta["ID"], 16))
            self._keys[(name, target)] = key
        return key

    def _dme_target(self):
        try:
            return int(self.target_var.get(), 0)
        except ValueError:
            return core.DME_MASTER

    def _target_changed(self):
        self._dme = self._dme_target()

    def _answer_mark(self, name):
        return ANSWER_MARKS.get(self.avail.get(self._avail_key(name)), "     ")

    def _refilter_soon(self):
        """Typing in the filter rebuilds the tree once the typing pauses."""
        if getattr(self, "_refilter_job", None):
            self.after_cancel(self._refilter_job)
        self._refilter_job = self.after(250, self._refilter)

    def _answers(self, name):
        """Whether the car answers this channel; a calculated one, all its inputs."""
        if name in core.DERIVED:
            return all(self._answers(n) for n in core.DERIVED[name][3] if n in self.catalog)
        return self.avail.get(self._avail_key(name)) in core.ANSWERS

    def _answered(self):
        return frozenset(k for k, v in self.avail.items() if v in core.ANSWERS)

    def _refilter(self):
        self._refilter_job = None
        self._marks_for = self._answered()
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
            if only and not self._answers(name):
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
            return ((0, "") if name == DME_LABEL else (0, "~") if name == BANK2_LABEL
                    else (0, "~~") if name == core.CALC_LABEL else (1, name))
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
        labels = {r: self._record_label(r, self.catalog[records[r][0]])
                  for r in records if r is not None}
        for record in sorted(records, key=lambda r: (r is not None,
                                                     (labels.get(r) or "").lower())):
            names = records[record]
            parent = iid
            if record is not None and len(self.layouts.get(
                    self._avail_key(names[0]), ())) > 1:
                # Values one reply returns together stay together.
                parent = "%s|%s" % (iid, record)
                self._members[parent] = names
                self.picker.insert(iid, "end", iid=parent, open=self._expand,
                                   text="%s  (%d)" % (labels[record], len(names)))
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
            meta = self.catalog.get(name)
            if meta is None or core.is_derived(meta) or meta.get("LOGONLY"):
                continue                        # worked out, not asked of the car
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
            except OSError as e:        # a read-only profile shouldn't stop logging
                diag.record("problem", where="saving answers", error=str(e))
            self._refresh_marks()
        self.after(10000, self._save_answers)

    def _refresh_marks(self):
        """Update the marks in place; rebuild only if they decide what shows.

        A rebuild every few seconds while connected would drop the
        selection someone is in the middle of making.
        """
        if self.only_var.get():
            # Rebuild only when what answers changed, not every few seconds.
            if self._answered() != self._marks_for:
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
        # Every answer, for the check CSV saved at the end (and synced).
        self._check_rows = []
        self._check_jobs = {key: fields[0].get("JOB") or fields[0]["ARG"]
                            for key, fields in layouts.items()}

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
                with share.open_tester(ip, 0xF4, 2.0) as h:
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

    def _with_extras(self, names):
        """What to watch for `names`: a calculated channel brings its inputs,
        and while comparing banks an engine channel brings its bank-2 twin."""
        out = []
        for name in core.with_inputs(names):
            out.append(name)
            twin = self._twin_to_add(name)
            if twin:
                out.append(twin)
        return out

    def _twin_to_add(self, name):
        if not self.compare_var.get():
            return None
        meta = self.catalog.get(name, {})
        if meta.get("MODULE") or meta.get("TARGET") or core.is_derived(meta):
            return None
        twin = core.bank_twin(name)
        return twin if twin in self.catalog else None

    def _add_names(self, names):
        watched = {n for n, _, _ in self.watch}     # "Add all" passes thousands
        for name in self._with_extras(names):
            if name in watched or name not in self.catalog:
                continue
            watched.add(name)
            self._add_row(name)
        self._push_channels()

    def _add_row(self, name):
        self._forget_cells(name)
        meta = self.catalog[name]
        self.watch.append((name, int(meta["ID"], 16), meta))
        self.series[name] = []
        self.history[name] = graph.History(HISTORY_POINTS)
        self.tree.insert("", "end", iid=name, text=self._describe(meta) or name,
                         values=("", self._shown_unit(meta), name, meta["ID"], ""))

    def _remove(self):
        gone = set(self.tree.selection())
        if self.compare_var.get():
            # A bank-2 twin is folded into its bank-1 row: it goes with it.
            gone |= {b2 for b1, b2 in self._pairs if b1 in gone}
        for name in gone:
            self.series.pop(name, None)
            self._forget_history(name)
            if self.tree.exists(name):
                self.tree.delete(name)
        self.watch = [c for c in self.watch if c[0] not in gone]
        self._push_channels()

    def _clear(self):
        for name in list(self.history):
            self._forget_history(name)
        names = [n for n, _, _ in self.watch]
        self.watch.clear()
        self.series.clear()
        # Folded-in bank-2 rows are detached: get_children() doesn't list them.
        self.tree.delete(*[n for n in names if self.tree.exists(n)])
        self.tree.delete(*self.tree.get_children())
        self._push_channels()

    def _forget_history(self, name):
        self.history.pop(name, None)
        self._forget_cells(name)
        window = self.graphs.pop(name, None)
        if window is not None:
            try:
                window.close()
            except tk.TclError:
                pass

    # -- pop-out graphs ----------------------------------------------------

    def _pop_out(self, names=None):
        """A window per chosen channel (the selected rows, else the first)."""
        names = [n for n in (names or self.tree.selection() or self.tree.get_children()[:1])
                 if n in self.history]
        for i, name in enumerate(names):
            window = self.graphs.get(name)
            if window is not None and window.winfo_exists():
                window.lift()
                continue
            meta = self.catalog.get(name, {})
            # Comparing banks: bank 2 drawn over bank 1 in the same window.
            twin = dict(self._pairs).get(name) if self.compare_var.get() else None
            window = graph.GraphWindow(self, name, self._describe(meta), meta.get("EINHEIT"),
                                       self.history[name], COLORS[i % len(COLORS)],
                                       lang=self.lang, show_unit=self._shown_unit(meta),
                                       colors=self.palette, overlay=self.history.get(twin))
            self.graphs[name] = window
        return [self.graphs[n] for n in names]

    # -- map and history ---------------------------------------------------

    def _open_map(self):
        if self.map_window is not None and self.map_window.winfo_exists():
            self.map_window.refresh_channels()
            self.map_window.lift()
            return
        source = _MapSource(self)
        lat, lon = source.gps_names()
        if not lat or not lon:
            if self.feed and self.feed.kind == "replay":
                messagebox.showinfo("Map", "This log has no GPS position to draw.")
                return
            if not messagebox.askyesno(
                    "Map", "The map needs the head unit's GPS position. Watch "
                           "%s and %s?" % (GPS_LAT, GPS_LON)):
                return
            self._add_names([n for n in (GPS_LAT, GPS_LON) if n in self.catalog])
        self.map_window = maps.MapWindow(self, source, tiles=self.tiles, lang=self.lang,
                                         colors=self.palette)

    def _open_history(self):
        if self.history_window is not None and self.history_window.winfo_exists():
            self.history_window.lift()
            self.history_window.rescan()
            return
        self.history_window = historywin.HistoryWindow(self, on_open=self._open_log,
                                                       colors=self.palette)

    # -- templates ---------------------------------------------------------

    def _fill_templates_menu(self):
        m = self.templates_menu
        m.delete(0, "end")
        m.add_command(label="Save watch list as template...", command=self._save_template,
                      state="normal" if self.watch else "disabled")
        builtin = tk.Menu(m, tearoff=False)
        for name, names in core.BUILTIN_TEMPLATES.items():
            builtin.add_command(label="%s  (%d)" % (name, len(names)),
                                command=lambda n=name: self._load_builtin(n))
        m.add_cascade(label="Ready-made sets", menu=builtin)
        templates = core.load_templates()
        if not templates:
            m.add_command(label="(no templates saved yet)", state="disabled")
            return
        m.add_separator()
        for name in sorted(templates, key=str.lower):
            m.add_command(label="%s  (%d)" % (name, len(templates[name])),
                          command=lambda n=name: self._load_template(n))
        delete = tk.Menu(m, tearoff=False)
        for name in sorted(templates, key=str.lower):
            delete.add_command(label=name, command=lambda n=name: self._delete_template(n))
        m.add_separator()
        m.add_cascade(label="Delete", menu=delete)

    def _save_template(self):
        if not self.watch:
            return
        name = simpledialog.askstring(
            "Save template", "Name for these %d channels:" % len(self.watch),
            initialvalue=self.template_name, parent=self)
        name = (name or "").strip()
        if not name:
            return
        if name in core.load_templates() and not messagebox.askyesno(
                "Save template", "Replace the template \"%s\"?" % name):
            return
        try:
            core.save_template(name, [n for n, _, _ in self.watch])
        except OSError as e:
            messagebox.showerror("Save template", "Could not save: %s" % e)
            return
        self.template_name = name
        self.status.config(text="\u25cf saved template \"%s\" (%d channels)"
                           % (name, len(self.watch)), foreground="#2d7dd2")

    def _load_template(self, name):
        names = core.load_templates().get(name, [])
        known = [n for n in names if n in self.catalog]
        self._set_watch(known)
        self.template_name = name
        missing = len(names) - len(known)
        self.status.config(text="\u25cf template \"%s\": %d channels%s" % (
            name, len(known), ", %d no longer in the catalog" % missing if missing else ""),
            foreground="#2d7dd2")

    def _load_builtin(self, name):
        """A ready-made set: misfire hunt, both banks' boost or lambda..."""
        known = [n for n in core.BUILTIN_TEMPLATES.get(name, []) if n in self.catalog]
        self._set_watch(known)
        self.template_name = ""
        self.status.config(text="● %s: %d channels" % (name, len(self.watch)),
                           foreground="#2d7dd2")

    def _delete_template(self, name):
        if messagebox.askyesno("Delete template", "Delete the template \"%s\"?" % name):
            core.delete_template(name)

    def _push_channels(self):
        names = [n for n, _, _ in self.watch]
        self._pairs = core.bank_pairs(names)
        self._arrange_rows()
        if self.poller:
            self.poller.set_channels(self.watch)
        if self.feed:
            self.feed.set_channels(self.watch)

    def _set_watch(self, names):
        """Replace the watch list with these catalog channels."""
        self._clear()
        watched = set()
        for name in self._with_extras(names):
            if name in watched or name not in self.catalog:
                continue
            watched.add(name)
            self._add_row(name)
        self._push_channels()

    # -- comparing banks ---------------------------------------------------

    def _compare_changed(self):
        self._save_setting("compare_banks", self.compare_var.get())
        if self.compare_var.get():
            self._add_names([n for n, _, _ in self.watch])    # brings the twins
        else:
            self._push_channels()

    def _arrange_rows(self):
        """While comparing, each bank-2 twin is folded into its bank-1 row
        (Bank 2 and difference columns); otherwise it's a row of its own,
        just below bank 1."""
        hide = {b2 for _, b2 in self._pairs} if self.compare_var.get() else set()
        for name in self._hidden - hide:
            if self.tree.exists(name):
                b1 = name[:-len(core.BANK2_SUFFIX)]
                at = self.tree.index(b1) + 1 if self.tree.exists(b1) else "end"
                self.tree.move(name, "", at)
        for name in hide - self._hidden:
            self.tree.detach(name)
        self._hidden = hide
        self._show_columns()

    def _show_columns(self):
        compare = self.compare_var.get()
        self.tree.configure(displaycolumns=("v", "b2", "d", "u", "arg", "id", "note")
                            if compare else ("v", "u", "arg", "id", "note"))

    def _show_banks(self, sample):
        """Fill each compared bank-1 row's Bank 2 and difference cells."""
        for b1, b2 in self._pairs:
            if b1 not in sample and b2 not in sample:
                continue
            self._set_cell(b1, "b2", self._cells.get((b2, "v"), ""))
            n1, n2 = self._numbers.get(b1), self._numbers.get(b2)
            self._set_cell(b1, "d", "" if n1 is None or n2 is None else "%+.4g" % (n1 - n2))

    # -- alerts ------------------------------------------------------------

    def _edit_alert(self):
        """Set a low and high limit for the selected channel, in shown units."""
        name = next(iter(self.tree.selection()), None)
        if not name:
            messagebox.showinfo("Alert", "Select a watched channel first.")
            return
        meta = self.catalog.get(name, {})
        unit = self._shown_unit(meta)
        to_shown = self._converter(meta.get("EINHEIT"), unit)
        lo, hi = self.alerts.get(name, (None, None))
        win = tk.Toplevel(self)
        win.title("Alert")
        win.transient(self)
        win.resizable(False, False)
        ttk.Label(win, padding=(10, 10, 10, 4), wraplength=360,
                  text="Highlight the row, beep and mark the log when\n%s\ngoes outside "
                  "these limits. Leave a box empty for no limit." % (
                      self._describe(meta) or name)).grid(row=0, column=0, columnspan=3)
        boxes = {}
        for r, (label, value) in enumerate((("Below", lo), ("Above", hi)), start=1):
            ttk.Label(win, text=label).grid(row=r, column=0, sticky="e", padx=(10, 4))
            var = tk.StringVar(value="" if value is None else "%g" % to_shown(value))
            ttk.Entry(win, textvariable=var, width=12).grid(row=r, column=1, pady=2)
            ttk.Label(win, text=unit).grid(row=r, column=2, sticky="w", padx=(4, 10))
            boxes[label] = var

        def save():
            try:
                limits = self._limits_from(boxes["Below"].get(), boxes["Above"].get(), meta)
            except ValueError:
                messagebox.showerror("Alert", "Enter numbers, or leave a box empty.",
                                     parent=win)
                return
            self._set_alert(name, limits)
            win.destroy()

        row = ttk.Frame(win, padding=10)
        row.grid(row=3, column=0, columnspan=3, sticky="e")
        ttk.Button(row, text="Save", command=save).pack(side="right")
        ttk.Button(row, text="Cancel", command=win.destroy).pack(side="right", padx=4)
        return win

    def _converter(self, src, dst):
        try:
            return graph.converter(src, dst)
        except ValueError:
            return lambda v: v

    def _limits_from(self, below, above, meta):
        """(low, high) in the car's units from what was typed in shown units."""
        back = self._converter(self._shown_unit(meta), meta.get("EINHEIT"))

        def value(text):
            text = text.strip().replace(",", ".")
            return None if not text else back(float(text))
        return value(below), value(above)

    def _set_alert(self, name, limits):
        if limits == (None, None):
            self.alerts.pop(name, None)
        else:
            self.alerts[name] = limits
        self._alerting.pop(name, None)
        if self.tree.exists(name):
            self.tree.item(name, tags=())
        self._save_setting("alerts", {k: list(v) for k, v in self.alerts.items()})

    def _check_alerts(self, sample):
        for name, limits in self.alerts.items():
            if name not in sample or not self.tree.exists(name):
                continue
            value = sample[name][0]
            state = core.alert_state(value, limits)
            if state == self._alerting.get(name):
                continue
            self._alerting[name] = state
            self.tree.item(name, tags=("alert",) if state else ())
            if state:
                meta = self.catalog.get(name, {})
                unit = self._shown_unit(meta)
                text = "%s %s %g %s (%g)" % (
                    name, "below" if state == "low" else "above",
                    self._shown_value(limits[0] if state == "low" else limits[1], meta),
                    unit, self._shown_value(value, meta))
                self.bell()
                self.status.config(text="● alert: " + text, foreground="#c0392b")
                if self.poller:
                    self.poller.mark(text)

    def _style_tags(self):
        self.tree.tag_configure("alert", background=self.palette.get("alert", "#f8d7da"))

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
            self.replay_bar.pack_forget()
        self.feed.stop()
        self.feed = None
        for s in self.series.values():
            s.clear()
        for h in self.history.values():
            h.clear()
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
        if path:
            self._open_log(path)

    def _open_log(self, path):
        """Replay the log at `path` (the History window opens one this way)."""
        if self.poller:
            messagebox.showinfo("Replay", "Disconnect first.")
            return
        if self.feed and self.feed.kind == "replay":
            self._stop_feed()
        # An hour's log takes seconds to read: do it off the Tk thread, so
        # the window keeps drawing, and carry on in _replay_loaded.
        self.status.config(text="\u25cf reading %s..." % os.path.basename(path),
                           foreground="#2d7dd2")
        self.replay_btn.config(state="disabled")

        def work():
            try:
                result = playback.read_log(path)
            except (OSError, ValueError, csv.Error) as e:
                result = e
            self.q.put(("replayload", path, result))
        threading.Thread(target=work, daemon=True).start()

    def _replay_loaded(self, path, result):
        self.replay_btn.config(state="normal")
        self.status.config(text="\u25cf not connected", foreground="#999")
        if isinstance(result, Exception):
            messagebox.showerror("Replay", "Could not read %s:\n\n%s"
                                 % (os.path.basename(path), result))
            return
        if self.poller:
            return                          # connected while it was reading
        names, rows = result
        # Another tool's columns (a BimmerLink export: "Engine speed (rpm)")
        # aren't catalog channels; they replay and graph all the same.
        for name in names:
            if name not in self.catalog:
                label, unit = playback.label_and_unit(name)
                self.catalog[name] = {"ARG": name, "ID": "0x0000", "INFO": label,
                                      "INFO_EN": label, "EINHEIT": unit, "LOGONLY": "1"}
        self._stop_feed()
        self._set_watch(names)
        feed = playback.ReplayFeed(self.q, rows)
        feed.name_shown = os.path.basename(path)
        self._start_feed(feed)
        self.replay_btn.config(text="Stop replay")
        self._show_replay_bar(feed)

    # -- replay controls ---------------------------------------------------

    def _build_replay_bar(self):
        """Pause, speed, a slider through the log, and the whole log at once.
        Shown only while a log replays."""
        bar = self.replay_bar = ttk.Frame(self, padding=(8, 0, 8, 6))
        self.replay_pause_btn = ttk.Button(bar, text="Pause", width=7,
                                           command=self._replay_pause)
        self.replay_pause_btn.pack(side="left")
        ttk.Label(bar, text="Speed").pack(side="left", padx=(10, 2))
        self.replay_speed_var = tk.StringVar(value="1x")
        speed = ttk.Combobox(bar, textvariable=self.replay_speed_var, width=4,
                             state="readonly",
                             values=["%dx" % s for s in playback.ReplayFeed.SPEEDS])
        speed.pack(side="left")
        speed.bind("<<ComboboxSelected>>", lambda e: self._replay_speed())
        self.scrub_var = tk.DoubleVar(value=0.0)
        self.scrub = ttk.Scale(bar, orient="horizontal", variable=self.scrub_var,
                               from_=0.0, to=1.0, command=self._scrub)
        self.scrub.pack(side="left", fill="x", expand=True, padx=10)
        self.scrub_label = ttk.Label(bar, text="0:00 / 0:00", width=13)
        self.scrub_label.pack(side="left")
        ttk.Button(bar, text="Whole log into graphs",
                   command=self._whole_log).pack(side="left", padx=(8, 0))
        self._scrub_setting = False

    def _show_replay_bar(self, feed):
        self.scrub.configure(to=max(feed.duration, 0.001))
        self.replay_speed_var.set("1x")
        self.replay_pause_btn.config(text="Pause")
        self._scrub_shown(0.0)
        self.replay_bar.pack(after=self._top_bar, fill="x")

    def _scrub_shown(self, seconds):
        """Move the slider with the replay, without that counting as a seek."""
        feed = self.feed
        self._scrub_setting = True
        try:
            self.scrub_var.set(seconds)
        finally:
            self._scrub_setting = False
        self.scrub_label.config(text="%s / %s" % (
            _clock(seconds), _clock(feed.duration if feed else 0)))

    def _scrub(self, value):
        feed = self.feed
        if self._scrub_setting or not feed or feed.kind != "replay":
            return
        feed.seek(float(value))

    def _replay_speed(self):
        if self.feed and self.feed.kind == "replay":
            self.feed.set_speed(int(self.replay_speed_var.get().rstrip("x")))

    def _replay_pause(self):
        feed = self.feed
        if not feed or feed.kind != "replay":
            return
        feed.pause(not feed.paused)
        self.replay_pause_btn.config(text="Resume" if feed.paused else "Pause")

    def _whole_log(self):
        """Every row of the log into the graphs at once, rather than replayed
        at its own pace. The table shows the last row."""
        feed = self.feed
        if not feed or feed.kind != "replay":
            return
        feed.whole = True               # its thread's leftover samples are ignored
        feed.stop()
        names = [n for n, _, _ in self.watch]
        last = {}
        for h in (self.history.get(n) for n in names):
            if h is not None:
                h.clear()
        for t, values in feed.rows:
            for name in names:
                v = values.get(name)
                if v is not None:
                    last[name] = v
                    h = self.history.get(name)
                    if h is not None and isinstance(v, (int, float)):
                        h.append((float(t), float(v)))
        self._show_sample({n: (last.get(n), "", "" if n in last else "not in log")
                           for n in names}, plot=False)
        for name in names:
            h = self.history.get(name)
            self.series[name] = [v for _, v in list(h)[-PLOT_POINTS:]] if h else []
        self.replay_bar.pack_forget()
        self._plot()
        self.status.config(text="● %s: whole log (%s, %d rows) - select rows and press "
                           "Graph" % (feed.name_shown, _clock(feed.duration), len(feed.rows)),
                           foreground="#2d7dd2")

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

        waiting = self._no_enet_address and bind == "auto"
        if waiting:
            self.status.config(text="\u25cf waiting for the ENET adapter's address...",
                               foreground="#b8860b")

        def work():
            # Just after the PC wakes or the app restarts, the adapter may
            # still be getting its 169.254 address: give it a few seconds.
            if waiting and core.wait_for_link_local():
                self.q.put(("linkup",))
            # The answer goes to the window at once; the slow network
            # snapshot for the diagnostics journal is taken after.
            try:
                diag.discover(3.0, None if bind == "auto" else bind,
                              on_result=lambda hits, near:
                              self.q.put(("discovered", hits, near)))
            except Exception as e:                                  # noqa: BLE001
                self.q.put(("joberror", "discover failed: %s" % e))

        threading.Thread(target=work, daemon=True).start()

    def _on_linkup(self, msg):
        """The ENET address appeared while Discover waited for it."""
        self._no_enet_address = not core.link_local(self._refresh_interfaces())

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
                with share.open_tester(ip, 0xF4, wait) as h:
                    for t, note in core.scan(h, lo, hi, wait):
                        if note.startswith("error"):
                            # Not "nothing here": say what went wrong; the
                            # scan carries on with the next address.
                            emit("0x%02X  %s\n" % (t, note))
                            continue
                        emit("0x%02X  responds%s\n" % (t, " (negative: %s)" % note
                                                         if note else ""))
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
        except Exception as e:                                      # noqa: BLE001
            diag.record("problem", where="listing this PC's addresses", error=str(e))
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
            self._log_ended()
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
        # What the module stored with the selected fault: odometer, time,
        # fault class, power state, how often it occurred.
        detail = tk.Text(win, height=9, wrap="none", font=("Consolas", 9))
        detail.pack(fill="x", padx=6, pady=(6, 0))
        detail.insert("1.0", "Select a fault and press Details (or double-click it) to read "
                      "what the module stored with it.")
        detail.config(state="disabled")
        row = ttk.Frame(win, padding=6)
        row.pack(fill="x")
        save_btn = ttk.Button(row, text="Save CSV...", state="disabled")
        save_btn.pack(side="right")
        ttk.Button(row, text="Details", command=lambda: self._fault_details(win)).pack(
            side="left")
        tree.bind("<Double-Button-1>", lambda e: self._fault_details(win))
        win.fault_rows = []
        win.ip = ip
        save_btn.config(command=lambda: self._save_faults(win, ask=True))
        win.parts = {"msg": msg, "tree": tree, "save": save_btn, "detail": detail}

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
                with share.open_tester(ip, 0xF4, 2.0) as h:
                    for _ in job(h):
                        pass
            except Exception as e:                                  # noqa: BLE001
                self.q.put(("faultmsg", win, "Error: %s" % e))

        threading.Thread(target=work, daemon=True).start()

    def _show_faults(self, win, rows):
        parts = win.parts
        win.fault_rows = rows
        tree = parts["tree"]
        for i, r in enumerate(rows):
            tree.insert("", "end", iid=str(i), values=(r["module"], r["module_name"], r["code"],
                                           r["state"], (r["text_de"] or r["text_en"])
                                           if self.lang == "de" else
                                           (r["text_en"] or r["text_de"])))
        n_faults = sum(1 for r in rows if r["code"])
        n_mods = len({r["module"] for r in rows})
        parts["msg"].config(text="%d fault(s) across %d module(s)." % (n_faults, n_mods))
        parts["save"].config(state="normal")
        self._save_faults(win, ask=not self.logging_to)

    def _fault_details(self, win):
        """Read the selected fault's snapshot and counters (0x19 04 / 06)."""
        sel = win.parts["tree"].selection()
        if not sel or not sel[0].isdigit() or int(sel[0]) >= len(win.fault_rows):
            return
        r = win.fault_rows[int(sel[0])]
        if not r["code"]:
            return
        module, code = int(r["module"], 16), r["code"]
        lang = self._enum_lang()
        if r.get("details"):                     # read with the faults already
            self.q.put(("faultdetail", win, r["module"], code, json.loads(r["details"])))
            return
        self._set_detail(win, "Reading what %s stored with %s..." % (r["module"], code))

        def job(h):
            self.q.put(("faultdetail", win, r["module"], code,
                        core.fault_details(h, module, code, lang=lang)))

        if self.poller:
            self.poller.submit(job)             # between samples, on the live connection
            return

        def work():
            try:
                with share.open_tester(win.ip, 0xF4, 2.0) as h:
                    job(h)
            except Exception as e:                                  # noqa: BLE001
                self.q.put(("faultmsg", win, "Error: %s" % e))

        threading.Thread(target=work, daemon=True).start()

    def _set_detail(self, win, text):
        detail = win.parts["detail"]
        detail.config(state="normal")
        detail.delete("1.0", "end")
        detail.insert("1.0", text)
        detail.config(state="disabled")

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
        self.cloud.trigger()

    def _found_car(self, hsfz):
        """Put the car Discover found in the IP box. One car on the ENET
        link (169.254.x.x) goes straight in; anything else - an answer from
        another network, or several - is shown with its VIN to confirm."""
        ip, payload = hsfz[0]
        vin = core.reply_vin(payload) or "no VIN"
        on_link = ip.startswith("169.254.")
        if len(hsfz) > 1 or not on_link:
            others = "".join("\n  %s  %s" % (h[0], core.reply_vin(h[1]) or "no VIN")
                             for h in hsfz[1:])
            why = ("More than one car answered." if on_link else
                   "It isn't on the ENET link (169.254.x.x): check this is your car.")
            if not messagebox.askyesno(
                    "Discover", "Use the car at %s (%s)?%s\n\n%s" % (
                        ip, vin, "\n\nAlso answered:" + others if others else "", why)):
                self.status.config(text="● not connected", foreground="#999")
                return
        self.ip_var.set(ip)
        self.status.config(text="● found %s at %s" % (vin, ip), foreground="#2a9d3f")

    # -- logging ---------------------------------------------------------

    def _log_ended(self):
        """The log is closed (the poller closes it on its way out); say so."""
        self.logging_to = None
        self.log_btn.config(text="Start logging")
        self.cloud.trigger()                    # up it goes, if set up and online

    def _toggle_log(self):
        if not self.poller:
            messagebox.showinfo("Logging", "Connect first.")
            return
        if self.logging_to:
            self.poller.stop_log()
            self._log_ended()
            return
        if not self.watch:
            # The header is written with the first sample, and with nothing
            # watched there is none: the file would stay empty.
            messagebox.showinfo("Logging", "Add channels to watch first.")
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
        # Reschedule whatever happens: if this loop stops, the window stops
        # showing samples, errors and status, and nothing says so. Except
        # once the window is closing: its widgets are going (a sample drawn
        # then raised TclError as the app closed for an update).
        if self._closing:
            return
        try:
            while True:
                try:
                    msg = self.q.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle(msg)
                except Exception as e:                      # noqa: BLE001
                    diag.record("gui_error", message=msg[0],
                                error="%s: %s" % (type(e).__name__, e))
                    self.status.config(text="● display error: %s" % e,
                                       foreground="#c0392b")
            # One redraw for however many samples were waiting.
            if self._chart_dirty:
                self._chart_dirty = False
                self._plot()
        finally:
            self.after(60, self._drain)

    def _handle(self, msg):
        """One message from a session, a worker thread or a feed: each kind
        has its _on_<kind> method."""
        kind = msg[0]
        if kind in ("status", "error") and self.poller is not None \
                and msg[2] is not self.poller:
            return      # from a poller already replaced by a new connection
        handler = getattr(self, "_on_" + kind, None)
        if handler is not None:
            handler(msg)

    def _on_sample(self, msg):
        self._learn(msg[2])
        self._show_sample(msg[2], msg[1], plot=False)

    def _on_checkres(self, msg):
        _, key, status, i, total = msg
        self._check_rows.append((key[0], key[1], self._check_jobs.get(key, ""), status))
        if status in core.ANSWERS or status == "refused" \
                or key not in self.avail:
            self.avail[key] = status
        if i % 50 == 0 or i == total:
            self.status.config(text="● checking %d / %d" % (i, total),
                               foreground="#b8860b")

    def _on_checkdone(self, msg):
        self._checking = None
        by_module = {}
        for (module, _did), status in self.avail.items():
            counts = by_module.setdefault("0x%02X" % module, {})
            counts[status] = counts.get(status, 0) + 1
        diag.record("check", stopped=msg[1], modules=by_module)
        answered = sum(1 for v in self.avail.values() if v in core.ANSWERS)
        note = "check %s: %d identifiers answer" % ("stopped" if msg[1] else "done", answered)
        # The results as a check CSV in the logs folder, which the cloud sync
        # uploads: the logger's channel file is made from them (logger-channels).
        rows, self._check_rows = self._check_rows, []
        if rows:
            path = os.path.join(core.logs_dir(),
                                "check_%s.csv" % time.strftime("%Y-%m-%d_%H-%M-%S"))
            try:
                core.write_check_csv(rows, path)
                note += ", saved to %s" % os.path.basename(path)
                self.cloud.trigger()
            except OSError as e:
                note += " (results not saved: %s)" % e
        self._answers_changed(note)

    def _on_feedsample(self, msg):
        _, feed, elapsed, sample = msg
        if feed is not self.feed or getattr(feed, "whole", False):
            return                      # left over from a stopped feed
        self._show_sample(sample, elapsed, plot=False)
        if feed.kind == "replay":
            self._scrub_shown(elapsed - feed.rows[0][0])
            self.status.config(
                text="● replaying %s  %s / %s" % (
                    feed.name_shown, _clock(elapsed - feed.rows[0][0]),
                    _clock(feed.duration)),
                foreground="#2d7dd2")

    def _on_replayload(self, msg):
        self._replay_loaded(msg[1], msg[2])

    def _on_feeddone(self, msg):
        if msg[1] is self.feed:
            self.feed = None
            self.replay_bar.pack_forget()
            self.replay_btn.config(text="Open log...")
            self.status.config(text="● replay finished", foreground="#999")

    def _on_status(self, msg):
        if msg[1] == "reconnecting":
            # Lost mid-log, or the process we borrowed from has stopped: the
            # session keeps the log open and connects again.
            self.status.config(text="● connection lost - reconnecting, log kept open",
                               foreground="#b8860b")
            return
        ok = msg[1] == "connected"
        self.status.config(text="● " + msg[1], foreground="#2a9d3f" if ok else "#999")
        if not ok:
            self.connect_btn.config(text="Connect")
            self.poller = None
            self._log_ended()

    def _on_discovered(self, msg):
        hits = msg[1]
        near = msg[2] if len(msg) > 2 else []
        what, ip = core.discovery_outcome(hits, near)
        if what == "doip":
            self.ip_var.set(ip)
            self.status.config(text="● car speaks DoIP", foreground="#c0392b")
            if messagebox.askyesno(
                    "Discover", core.doip_help(ip) + "\n\nSave a diagnostics report now?"):
                self._save_diag_report()
        elif what == "near":
            self.ip_var.set(ip)
            self.status.config(text="● no reply; %s is on the link" % ip,
                               foreground="#b8860b")
            messagebox.showinfo(
                "Discover",
                "The car didn't answer Discover, but %s is on the car's "
                "link, so it's probably the car. It's in the IP box: "
                "press Connect.%s" % (ip, "\n\nAlso seen: " + ", ".join(near[1:])
                                      if near[1:] else ""))
        elif what == "none":
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
            self.status.config(text="● not connected", foreground="#999")
        else:
            self._found_car([h for h in hits if core.reply_protocol(h[1]) == "HSFZ"])

    def _on_module(self, msg):
        if msg[1] not in self.modules:
            self.modules.append(msg[1])

    def _on_faultmsg(self, msg):
        _, win, text = msg
        if win.winfo_exists():
            win.parts["msg"].config(text=text)

    def _on_faultdetail(self, msg):
        _, win, module, code, details = msg
        if win.winfo_exists():
            self._set_detail(win, "%s  %s\n%s" % (module, code,
                                                  "\n".join(core.format_details(details))))

    def _on_faults_done(self, msg):
        _, win, rows = msg
        if win.winfo_exists():
            self._show_faults(win, rows)

    def _on_logfile(self, msg):
        self.logging_to = msg[1]
        self.status.config(text="● channels changed - logging to %s"
                           % os.path.basename(msg[1]), foreground="#2a9d3f")

    def _on_scanline(self, msg):
        _, box, text = msg
        if box.winfo_exists():
            box.insert("end", text)
            box.see("end")

    def _on_update(self, msg):
        self._show_update(*msg[1:])

    def _on_claude(self, msg):
        self._show_claude(msg[1])

    def _on_note(self, msg):
        # Information for the status line only; not a state change.
        self.status.config(text="● " + msg[1], foreground="#2d7dd2")

    def _on_joberror(self, msg):
        # A failed fault read, check or discovery: say so, but it is not a
        # lost connection, so leave the poller be.
        if not self.poller:
            self.status.config(text="● error", foreground="#c0392b")
        messagebox.showerror("Error", msg[1])

    def _on_error(self, msg):
        self.status.config(text="● error", foreground="#c0392b")
        messagebox.showerror("Connection", msg[1])
        self.connect_btn.config(text="Connect")
        self.poller = None
        self._log_ended()

    def _show_sample(self, sample, elapsed=None, plot=True):
        for name, (value, unit, note) in sample.items():
            if not self.tree.exists(name):
                continue
            meta = self.catalog.get(name, {})
            # Shown in the chosen units; charted and logged as the car sent it.
            display = value
            if isinstance(value, float) or (isinstance(value, int) and
                                            self._shown_unit(meta) != meta.get("EINHEIT")):
                display = float(self._shown_value(value, meta))
            shown = "" if display is None else (
                "%.4g" % display if isinstance(display, float) else display)
            if isinstance(display, float) and meta.get("EINHEIT") == "deg":
                shown = "%.6f" % display        # %.4g would round a coordinate to ~10 km
            if not note:
                # Enumerated values stay numbers (they log and plot); the
                # words go in Status.
                note = core.enum_text(value, meta, self.enums, self._enum_lang()) or ""
            self._set_cell(name, "v", shown)
            self._set_cell(name, "note", note)
            self._numbers[name] = display if isinstance(display, (int, float)) else None
            if isinstance(value, (int, float)):
                s = self.series.setdefault(name, [])
                s.append(float(value))
                del s[:-PLOT_POINTS]
                h = self.history.get(name)
                if h is not None and elapsed is not None:
                    if h and elapsed < h[-1][0]:
                        h.clear()               # a new session: its clock restarted
                    h.append((float(elapsed), float(value)))
        if self._pairs and self.compare_var.get():
            self._show_banks(sample)
        if self.alerts:
            self._check_alerts(sample)
        if plot:
            self._plot()
        else:
            self._chart_dirty = True

    def _set_cell(self, name, col, text):
        """Set a table cell, skipping the Tcl call when it already shows that:
        most values repeat, and module values change only every 1-10 s."""
        if self._cells.get((name, col)) != text:
            self._cells[(name, col)] = text
            self.tree.set(name, col, text)

    def _forget_cells(self, name):
        self._cells.pop((name, "v"), None)
        self._cells.pop((name, "note"), None)

    # -- chart -----------------------------------------------------------

    def _plot(self):
        c = self.canvas
        c.delete("all")
        w = c.winfo_width() or 700
        h = c.winfo_height() or 200
        pad = 28

        p = self.palette
        c.configure(bg=p["canvas"], highlightbackground=p["frame"])
        if self.feed:
            c.create_text(w - pad, h - 8, anchor="e", fill=p["faint"],
                          font=("TkDefaultFont", 9, "bold"),
                          text="DEMO DATA" if self.feed.kind == "demo" else "REPLAY")

        # Chart the rows selected in the table, else the first few numeric
        # ones. Each line has its own scale, so engine speed (thousands),
        # lambda (about 1) and latitude can share the chart; the legend gives
        # each line's range.
        chosen = [n for n in self.tree.selection() if len(self.series.get(n, [])) > 1]
        if not chosen:
            chosen = [n for n, v in self.series.items() if len(v) > 1 and n not in self._hidden]
        active = [(n, self.series[n]) for n in chosen[:CHART_LINES]]
        if not active:
            c.create_text(w // 2, h // 2, text="no numeric data yet", fill=p["muted"])
            return

        for frac in (0.0, 0.5, 1.0):
            y = pad + (h - 2 * pad) * frac
            c.create_line(pad, y, w - pad, y, fill=p["grid"])
        step = (w - 2 * pad) / max(1, PLOT_POINTS - 1)
        hover = None
        if self.chart_hover is not None and pad <= self.chart_hover <= w - pad:
            hover = int(round((self.chart_hover - pad) / step))
            c.create_line(pad + hover * step, pad, pad + hover * step, h - pad,
                          fill=p["muted"], dash=(2, 2))
        readings = []

        col_w = max(150, (w - 2 * pad) // 3)
        # Comparing banks: bank 2 rides along with bank 1, dashed, on the
        # same scale, so the two lines can be read against each other.
        twins = dict(self._pairs) if self.compare_var.get() else {}
        for i, (name, vals) in enumerate(active):
            color = COLORS[i % len(COLORS)]
            tvals = self.series.get(twins.get(name)) or []
            lo, hi = min(vals + tvals), max(vals + tvals)
            if hi - lo < 1e-9:
                lo, hi = lo - 1, hi + 1
            span = hi - lo
            step = (w - 2 * pad) / max(1, PLOT_POINTS - 1)

            def line(values, **style):
                pts = []
                first = PLOT_POINTS - len(values)
                for k, v in enumerate(values):
                    pts += [pad + (first + k) * step, pad + (h - 2 * pad) * (1 - (v - lo) / span)]
                if len(pts) >= 4:
                    c.create_line(*pts, fill=color, width=2, smooth=False, **style)
            line(vals)
            if tvals:
                line(tvals, dash=(5, 3))
            meta = self.catalog.get(name, {})
            unit = self._shown_unit(meta)
            label = "%s%s  %.5g\u2013%.5g %s" % (self._chart_name(name, meta),
                                                 "  (bank 2 dashed)" if tvals else "",
                                                 self._shown_value(lo, meta),
                                                 self._shown_value(hi, meta), unit)
            c.create_text(pad + (i % 3) * col_w, 8 + (i // 3) * 11, anchor="w",
                          fill=color, font=("TkDefaultFont", 7), text=label)
            for values, which in ((vals, ""), (tvals, " bank 2")):
                start = PLOT_POINTS - len(values)
                k = None if hover is None else hover - start
                if k is not None and 0 <= k < len(values):
                    y = pad + (h - 2 * pad) * (1 - (values[k] - lo) / span)
                    c.create_oval(pad + hover * step - 3, y - 3, pad + hover * step + 3,
                                  y + 3, outline=color, width=2)
                    readings.append((color, "%s%s %.5g %s" % (
                        self._chart_name(name, meta), which,
                        self._shown_value(values[k], meta), unit)))
        if readings:
            x = pad + hover * step
            right = x > w / 2
            for j, (color, text) in enumerate(readings):
                c.create_text(x - 6 if right else x + 6, h - pad - 4 - 12 * (len(readings) - 1 - j),
                              anchor="se" if right else "sw", fill=color,
                              font=("TkDefaultFont", 8, "bold"), text=text)

    def _chart_name(self, name, meta):
        """A line's name on the chart: its description in the display
        language, short; the code name if it has none."""
        text = self._describe(meta) or name.split(".")[-1]
        if len(text) > CHART_NAME_MAX:
            text = text[:CHART_NAME_MAX].rsplit(" ", 1)[0].rstrip(",;:") + "\u2026"
        return text

    def _chart_hover(self, x):
        # Mouse moves come far faster than a redraw: draw once they settle.
        self.chart_hover = x
        if not self._hover_job:
            self._hover_job = self.after_idle(self._hover_plot)

    def _hover_plot(self):
        self._hover_job = None
        self._plot()

    def _close(self):
        self._closing = True
        self.cloud.stop()
        self._stop_check()
        if self._avail_dirty:
            try:
                core.save_availability(self.avail)
            except OSError as e:
                diag.record("problem", where="saving answers", error=str(e))
        if self.poller:
            self.poller.stop()
            # The poller is a daemon thread: without the wait the process can
            # exit before it closes the log, losing the last second of it.
            self.poller.join(timeout=3)
        self._stop_feed()
        self._install_on_exit()
        self.destroy()

    def _install_on_exit(self):
        """A downloaded update installs on the way out, unless another n63read
        from this folder is still running: then it waits for next time.
        Returns True if the installer was started."""
        if not self._update_setup or update.others_running():
            return False
        try:
            update.install(self._update_setup)
        except update.UpdateError:
            return False
        return True


def _clock(seconds):
    seconds = max(0, int(seconds))
    return "%d:%02d" % (seconds // 60, seconds % 60)


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
