"""
n63read.historywin - the History window: every drive and fault, over time.

Three tabs over history.scan(): Drives (one row a drive, double-click to
replay it), Trends (a statistic per drive, per cylinder or bank, as lines)
and Faults (each fault's first and last sighting and whether it's still
there). The scan runs off the Tk thread; the same data is in drives.csv
and faults.csv in the History folder.
"""

import os
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk

from n63read import core, graph, history

COLORS = ["#2d7dd2", "#e4572e", "#29a35a", "#8e44ad", "#f2a541", "#17becf", "#7f5539",
          "#d62f8b"]

DRIVE_COLUMNS = [
    ("start", "Start", 130), ("minutes", "Min", 50), ("km", "km", 55),
    ("speed_max", "Max km/h", 70), ("l_per_100km", "l/100 km", 65), ("mpg", "mpg", 50),
    ("misfires", "Misfires", 60), ("where", "By cylinder", 150),
    ("knock_b1", "Knock b1", 65), ("knock_b2", "Knock b2", 65),
    ("trim_b1", "Trim b1 %", 70), ("trim_b2", "Trim b2 %", 70),
    ("coolant_max", "Coolant max", 80), ("battery_min", "Battery min", 80),
    ("file", "File", 260),
]
FAULT_COLUMNS = [
    ("now", "Now", 70), ("module", "Module", 60), ("code", "Code", 70),
    ("text_en", "Fault", 380), ("first_seen", "First seen", 130),
    ("last_seen", "Last seen", 130), ("reads_seen", "Reads", 50),
    ("reads_present", "Present", 60),
]


def _cell(value):
    if value is None or value == "":
        return ""
    if isinstance(value, float):
        return "%.4g" % value
    return str(value)


class HistoryWindow(tk.Toplevel):
    def __init__(self, master, logs=None, on_open=None, colors=None):
        super().__init__(master)
        self.logs, self.on_open, self.colors = logs, on_open, colors or {}
        self.title("History")
        self.geometry("1100x620")
        self.minsize(700, 400)
        self.summary = None
        self._result = None

        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(fill="x")
        ttk.Button(bar, text="Rescan logs", command=self.rescan).pack(side="left")
        ttk.Button(bar, text="Open History folder", command=self.open_folder).pack(
            side="left", padx=6)
        self.status = ttk.Label(bar, text="")
        self.status.pack(side="left", padx=8)

        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.drives = self._table(self.tabs, DRIVE_COLUMNS, "Drives")
        self.drives.bind("<Double-Button-1>", self._open_drive)

        trend = ttk.Frame(self.tabs)
        self.tabs.add(trend, text="Trends")
        tbar = ttk.Frame(trend, padding=(0, 6))
        tbar.pack(fill="x")
        ttk.Label(tbar, text="Show").pack(side="left")
        self.trend_var = tk.StringVar(value=history.TRENDS[0][0])
        box = ttk.Combobox(tbar, textvariable=self.trend_var, state="readonly", width=40,
                           values=[t[0] for t in history.TRENDS])
        box.pack(side="left", padx=4)
        box.bind("<<ComboboxSelected>>", lambda e: self.draw_trend())
        self.chart = tk.Canvas(trend, highlightthickness=0,
                               bg=self.colors.get("canvas", "#fbfbfb"))
        self.chart.pack(fill="both", expand=True)
        self.chart.bind("<Configure>", lambda e: self.draw_trend())

        self.faults = self._table(self.tabs, FAULT_COLUMNS, "Faults")
        self.rescan()

    def _table(self, parent, columns, title):
        frame = ttk.Frame(parent)
        parent.add(frame, text=title)
        tree = ttk.Treeview(frame, columns=[c for c, _, _ in columns], show="headings")
        for key, text, width in columns:
            tree.heading(key, text=text)
            tree.column(key, width=width, anchor="w", stretch=key in ("file", "text_en"))
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)
        return tree

    # -- scanning, off the Tk thread

    def rescan(self):
        if self._result is not None:
            return                                  # one at a time
        self.status.config(text="reading the logs folder...")
        box = self._result = {}

        def work():
            # Anything that goes wrong is shown: only OSError was, and
            # anything else left the window at "reading the logs folder..."
            # with no way to scan again.
            try:
                box["summary"] = history.scan(
                    self.logs, progress=lambda i, n: box.__setitem__("progress", (i, n)))
            except Exception as e:                                  # noqa: BLE001
                box["error"] = e
            finally:
                box["done"] = True
        threading.Thread(target=work, daemon=True).start()
        self.after(100, self._poll)

    def _poll(self):
        box = self._result
        if box is None or not self.winfo_exists():
            return
        if not box.get("done"):
            if "progress" in box:
                self.status.config(text="reading %d of %d files..." % box["progress"])
            self.after(150, self._poll)
            return
        self._result = None
        if "error" in box:
            self.status.config(text="could not read the logs folder: %s" % box["error"])
            return
        self.show(box["summary"])

    def show(self, summary):
        self.summary = summary
        for tree in (self.drives, self.faults):
            tree.delete(*tree.get_children())
        for d in reversed(summary.drives):          # newest first
            values = dict(d, where=history.misfire_text(d))
            self.drives.insert("", "end", iid=d["file"],
                               values=[_cell(values.get(k)) for k, _, _ in DRIVE_COLUMNS])
        for f in summary.faults:
            self.faults.insert("", "end", values=[_cell(f.get(k)) for k, _, _ in FAULT_COLUMNS])
        present = sum(1 for f in summary.faults if f["now"] == "present")
        text = "%d drives, %d faults seen (%d present now) - saved to %s" % (
            len(summary.drives), len(summary.faults), present, summary.folder)
        if summary.errors:
            text += "  (%d files couldn't be read)" % len(summary.errors)
        self.status.config(text=text)
        self.draw_trend()

    def open_folder(self):
        path = history.folder(self.logs)
        os.makedirs(path, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(path)                      # noqa: S606 - our own folder
        else:
            subprocess.Popen(["xdg-open" if sys.platform != "darwin" else "open", path])

    def _open_drive(self, event):
        iid = self.drives.identify_row(event.y)
        if iid and self.on_open:
            self.on_open(os.path.join(self.logs or core.logs_dir(), iid))

    # -- the trend chart

    def trend(self):
        return next(t for t in history.TRENDS if t[0] == self.trend_var.get())

    def draw_trend(self):
        c = self.chart
        c.delete("all")
        w, h = max(c.winfo_width(), 300), max(c.winfo_height(), 200)
        drives = self.summary.drives if self.summary else []
        title, unit, series = self.trend()
        lines = [(label, [(i, d.get(key)) for i, d in enumerate(drives)
                          if isinstance(d.get(key), (int, float))])
                 for key, label in series]
        lines = [(label, pts) for label, pts in lines if pts]
        if not lines:
            c.create_text(w // 2, h // 2, fill="#888",
                          text="No drives with these values yet." if drives else
                          "No drive logs found yet.")
            return
        L, R, T, B = 64, w - 150, 16, h - 56
        values = [v for _, pts in lines for _, v in pts]
        lo, hi = graph.value_range(values)
        if min(values) >= 0 and lo < 0:
            lo = 0.0                                # counts and distances start at zero
        n = max(1, len(drives) - 1)

        def px(i):
            return L + (R - L) * (i / n if len(drives) > 1 else 0.5)

        def py(v):
            return B - (B - T) * (v - lo) / (hi - lo)

        font = ("TkDefaultFont", 8)
        for v in graph.nice_ticks(lo, hi):
            if lo <= v <= hi:
                c.create_line(L, py(v), R, py(v), fill="#e6e6e6")
                c.create_text(L - 6, py(v), anchor="e", text=graph.fmt(v), font=font,
                              fill="#666")
        step = max(1, len(drives) // 10)
        for i in range(0, len(drives), step):
            c.create_text(px(i), B + 6, anchor="n", font=font, fill="#666",
                          text=(drives[i].get("start") or "")[:10])
        c.create_rectangle(L, T, R, B, outline="#cfcfcf")
        c.create_text(8, T, anchor="nw", text=unit, font=font, fill="#666")
        c.create_text((L + R) // 2, h - 6, anchor="s", font=font, fill="#666",
                      text="%s, one point per drive" % title)
        for k, (label, pts) in enumerate(lines):
            colour = COLORS[k % len(COLORS)]
            coords = [xy for i, v in pts for xy in (px(i), py(v))]
            if len(coords) >= 4:
                c.create_line(*coords, fill=colour, width=2)
            for i, v in pts:
                c.create_oval(px(i) - 3, py(v) - 3, px(i) + 3, py(v) + 3, fill=colour,
                              outline=colour)
            c.create_line(R + 12, T + 8 + 16 * k, R + 30, T + 8 + 16 * k, fill=colour, width=3)
            c.create_text(R + 36, T + 8 + 16 * k, anchor="w", text=label, font=font,
                          fill="#1e1e1e")
