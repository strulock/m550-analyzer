"""The logger firmware's fault reads match n63read's: the modules it asks
(firmware/lib/logger/faults.h) and the fault file it writes."""

import os
import re

from n63read import core, history

HERE = os.path.dirname(__file__)
FAULTS_H = os.path.join(HERE, "..", "firmware", "lib", "logger", "faults.h")


def firmware_modules():
    with open(FAULTS_H, encoding="utf-8") as f:
        text = f.read()
    body = text[text.index("FAULT_MODULES[] = {"):text.index("};", text.index("FAULT_MODULES[]"))]
    return {int(a, 16): name for a, name in re.findall(r'\{(0x[0-9A-F]{2}), "([^"]*)"\}', body)}


def test_the_board_asks_the_modules_n63read_knows():
    assert firmware_modules() == core.KNOWN_MODULES


def test_the_board_writes_n63reads_fault_columns():
    with open(FAULTS_H, encoding="utf-8") as f:
        text = f.read()
    header = re.search(r'FAULT_HEADER =\s*((?:"[^"]*"\s*)+);', text).group(1)
    columns = "".join(re.findall(r'"([^"]*)"', header)).replace("\\n", "").split(",")
    assert columns == list(core.FAULT_CSV_HEADER) + ["time_s"]


def test_the_app_reads_a_board_fault_file(tmp_path):
    # A file as faults.h writes it: one failed code, and a module with none.
    path = tmp_path / "2026-10-25_12-42-05.faults.csv"
    path.write_text(
        ",".join(core.FAULT_CSV_HEADER) + ",time_s\n"
        '2026-10-25 12:43:10,0x12,"Engine, master DME",480A14,0x2F,"present, stored",,,,,,,,65.0\n'
        '2026-10-25 12:43:12,0x29,"Stability control (DSC)",,,"no faults",,,,,,,,67.0\n',
        encoding="utf-8")
    rows = history.read_faults(str(path))
    assert [(r["module"], r["code"], r["state"]) for r in rows] == [
        ("0x12", "480A14", "present, stored"), ("0x29", "", "no faults")]
    faults = history.fault_history(rows)
    assert [(f["code"], f["now"]) for f in faults] == [("480A14", "present")]
