"""
n63read.theme - light and dark colours for the app.

apply() restyles every ttk widget and recolours the plain Tk ones (menus,
canvases, windows) in place, so the theme can change while the app runs.
"system" follows Windows' "app mode" (light or dark).
"""

import sys
import tkinter as tk
from tkinter import ttk

THEMES = ("light", "dark", "system")

PALETTES = {
    "light": {"bg": "#f0f0f0", "fg": "#1e1e1e", "field": "#ffffff", "border": "#c8c8c8",
              "select": "#cce4f7", "select_fg": "#1e1e1e", "muted": "#888888",
              "canvas": "#fbfbfb", "grid": "#e6e6e6", "grid2": "#efefef",
              "axis": "#666666", "frame": "#cfcfcf", "faint": "#c8c8c8"},
    "dark": {"bg": "#202124", "fg": "#e8eaed", "field": "#2b2c30", "border": "#3c4043",
             "select": "#264f78", "select_fg": "#ffffff", "muted": "#9aa0a6",
             "canvas": "#18191b", "grid": "#2f3134", "grid2": "#26282b",
             "axis": "#9aa0a6", "frame": "#3c4043", "faint": "#4a4d51"},
}


def system_theme():
    """'dark' if Windows apps are set to dark mode, else 'light'."""
    if sys.platform != "win32":
        return "light"
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return "light" if winreg.QueryValueEx(k, "AppsUseLightTheme")[0] else "dark"
    except OSError:
        return "light"


def resolve(name):
    return system_theme() if name == "system" else (name if name in PALETTES else "light")


def palette(name):
    return PALETTES[resolve(name)]


def apply(root, name):
    """Restyle the app; returns the palette in use."""
    p = palette(name)
    style = ttk.Style(root)
    if not hasattr(root, "_native_theme"):
        root._native_theme = style.theme_use()
    if resolve(name) == "dark":
        # The native Windows theme draws its own (light) controls; clam
        # takes colours.
        style.theme_use("clam")
    else:
        style.theme_use(root._native_theme)
    if resolve(name) == "dark":
        style.configure(".", background=p["bg"], foreground=p["fg"],
                        fieldbackground=p["field"], bordercolor=p["border"],
                        lightcolor=p["border"], darkcolor=p["border"],
                        troughcolor=p["field"], selectbackground=p["select"],
                        selectforeground=p["select_fg"], insertcolor=p["fg"])
        style.map(".", background=[("active", p["border"]), ("disabled", p["bg"])],
                  foreground=[("disabled", p["muted"])])
        style.configure("Treeview", background=p["field"], fieldbackground=p["field"],
                        foreground=p["fg"])
        style.map("Treeview", background=[("selected", p["select"])],
                  foreground=[("selected", p["select_fg"])])
        style.configure("Treeview.Heading", background=p["bg"], foreground=p["fg"])
        style.configure("TEntry", fieldbackground=p["field"], foreground=p["fg"])
        style.configure("TCombobox", fieldbackground=p["field"], foreground=p["fg"],
                        arrowcolor=p["fg"])
        style.map("TCombobox", fieldbackground=[("readonly", p["field"])],
                  foreground=[("readonly", p["fg"])])
        style.configure("TButton", background=p["field"])
        style.configure("TMenubutton", background=p["field"])
    # Footer text keeps its own size; only its colour follows the theme.
    style.configure("Footer.TMenubutton", foreground=p["muted"])
    root.option_add("*TCombobox*Listbox.background", p["field"])
    root.option_add("*TCombobox*Listbox.foreground", p["fg"])
    root.option_add("*Menu.background", p["field"])
    root.option_add("*Menu.foreground", p["fg"])
    _recolour(root, p)
    return p


def _recolour(widget, p):
    """Plain Tk widgets don't follow ttk styles: set theirs directly."""
    try:
        if isinstance(widget, (tk.Tk, tk.Toplevel)):
            widget.configure(background=p["bg"])
        elif isinstance(widget, tk.Menu):
            widget.configure(background=p["field"], foreground=p["fg"],
                             activebackground=p["select"], activeforeground=p["select_fg"])
        elif isinstance(widget, tk.Canvas):
            widget.configure(background=p["canvas"], highlightbackground=p["frame"])
        elif isinstance(widget, tk.Listbox):
            widget.configure(background=p["field"], foreground=p["fg"])
    except tk.TclError:
        pass
    for child in widget.winfo_children():
        _recolour(child, p)
    if isinstance(widget, ttk.Menubutton):
        try:
            menu = widget.nametowidget(widget.cget("menu"))
            _recolour(menu, p)
        except (KeyError, tk.TclError):
            pass
