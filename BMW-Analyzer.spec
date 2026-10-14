# PyInstaller: one folder, dist\BMW Analyzer\, holding both exes, n63read.exe
# (GUI) and n63read-cli.exe (console), and the _internal\ folder they share.
# A folder build starts straight from disk; a one-file exe unpacks itself to
# %TEMP% first, which antivirus heuristics often flag.
#
#     python -m PyInstaller --noconfirm BMW-Analyzer.spec

import glob
import os

SRC = os.path.join(SPECPATH, "src")
DATA = os.path.join(SRC, "n63read", "data")
ICON = os.path.join(DATA, "n63read.ico")
datas = [(p, ".") for pattern in ("*.csv", "*.ico", "*.png")
         for p in sorted(glob.glob(os.path.join(DATA, pattern)))]


def build(script, name, console):
    a = Analysis([os.path.join(SRC, "n63read", script)], pathex=[SRC], datas=datas)
    pyz = PYZ(a.pure)
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=name,
              console=console, icon=ICON, upx=False)
    return a, exe


gui, gui_exe = build("gui.py", "n63read", console=False)
# The CLI is a console exe, or it has no stdout.
cli, cli_exe = build("__main__.py", "n63read-cli", console=True)

COLLECT(gui_exe, gui.binaries, gui.datas,
        cli_exe, cli.binaries, cli.datas,
        name="BMW Analyzer", upx=False)
