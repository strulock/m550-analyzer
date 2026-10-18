"""Channel templates, and the pop-out graph's units and axes."""

import types

import pytest

from n63read import cli, core, graph

from fake_car import FakeCar

DEG_C, DEG_F = "°C", "°F"


# ------------------------------------------------------------ units and axes

@pytest.mark.parametrize("src, dst, value, expect", [
    (DEG_C, DEG_F, 100, 212), (DEG_F, DEG_C, 32, 0), (DEG_C, "K", 0, 273.15),
    ("hPa", "psi", 1013.25, 14.6959), ("bar", "hPa", 1.5, 1500), ("hPa", "kPa", 997, 99.7),
    ("mV", "V", 14040, 14.04), ("km/h", "mph", 100, 62.1371), ("kg/h", "g/s", 36, 10),
    ("Nm", "lb-ft", 100, 73.7562), ("ms", "s", 250, 0.25), ("1/min", "rpm", 700, 700),
])
def test_conversions(src, dst, value, expect):
    assert graph.converter(src, dst)(value) == pytest.approx(expect, rel=1e-5)


def test_only_sensible_conversions():
    with pytest.raises(ValueError):
        graph.converter(DEG_C, "hPa")
    assert graph.units_for(DEG_C)[:2] == [DEG_C, DEG_F]
    assert "psi" in graph.units_for("bar") and graph.units_for("bar")[0] == "bar"
    assert graph.units_for("1/min") == ["rpm"]
    assert graph.units_for("%") == ["%"] and graph.units_for("") == [""]


def test_ticks_axes_and_window():
    ticks = graph.nice_ticks(0, 97)
    assert ticks[0] == 0 and ticks[-1] <= 97 and len(ticks) >= 4
    assert all(b > a for a, b in zip(ticks, ticks[1:]))
    assert graph.nice_ticks(13.6, 14.3)[0] >= 13.6
    pts = [(t, float(t)) for t in range(100)]
    assert graph.visible(pts, 10) == pts[-11:]
    assert graph.visible(pts, None) == pts
    assert graph.value_range([5.0, 5.0]) == (4.0, 6.0)
    lo, hi = graph.value_range([0.0, 10.0])
    assert lo < 0 and hi > 10
    assert graph.value_range([0.0, 10.0], fixed=(2.0, 3.0)) == (2.0, 3.0)
    assert graph.stats([1.0, 2.0, 6.0]) == {"min": 1.0, "max": 6.0, "mean": 3.0,
                                            "last": 6.0, "n": 3}


# --------------------------------------------------------------- templates

def test_templates_are_saved_listed_and_deleted():
    assert core.load_templates() == {}
    core.save_template("Idle check", ["ITKUA", "ITOEL"])
    core.save_template("Knock", ["IUKZ1", "IUKZ5@0x13"])
    assert core.load_templates() == {"Idle check": ["ITKUA", "ITOEL"],
                                     "Knock": ["IUKZ1", "IUKZ5@0x13"]}
    core.save_template("Knock", ["IUKZ2"])                      # replaced
    assert core.load_templates()["Knock"] == ["IUKZ2"]
    core.delete_template("Knock")
    assert list(core.load_templates()) == ["Idle check"]
    with pytest.raises(ValueError):
        core.save_template("  ", ["ITKUA"])


def test_cli_logs_a_template(monkeypatch, tmp_path, capsys):
    core.save_template("Lambda", ["ILAG1", "ILAG2"])
    c = FakeCar()
    monkeypatch.setattr(core.Hsfz.__init__, "__defaults__", (c.port, core.TESTER_ADDR, 3.0))
    out = tmp_path / "t.csv"
    try:
        cli.main(["log", "--ip", "127.0.0.1", "--timeout", "0.5", "--interval", "0",
                  "--samples", "1", "--template", "Lambda", "--out", str(out),
                  "ILAG2", "ITKUA"])
    finally:
        c.close()
    assert out.read_text(encoding="utf-8").splitlines()[0] == "Time,Clock,ILAG1,ILAG2,ITKUA,Event"
    cli.main(["templates"])
    assert "Lambda  (2)" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="no template 'Nope'; available: .*Lambda"):
        cli.main(["log", "--ip", "127.0.0.1", "--template", "Nope"])


# ------------------------------------------------------------------ the app

gui = pytest.importorskip("n63read.gui")


def test_save_and_load_a_template_in_the_app(app, monkeypatch):
    app._set_watch(["ITKUA", "ITOEL", "ILAG2"])
    monkeypatch.setattr(gui.simpledialog, "askstring", lambda *a, **k: "Warm-up")
    app._save_template()
    assert core.load_templates()["Warm-up"] == ["ITKUA", "ITOEL", "ILAG2"]
    app._clear()
    core.save_template("Warm-up", ["ITOEL", "GONE_CHANNEL", "ITKUA"])
    app._load_template("Warm-up")
    assert [n for n, _, _ in app.watch] == ["ITOEL", "ITKUA"]
    assert "1 no longer in the catalog" in app.status.cget("text")

    # Saving under a name that exists asks first; no keeps the old one.
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: False)
    app._save_template()
    assert core.load_templates()["Warm-up"] == ["ITOEL", "GONE_CHANNEL", "ITKUA"]

    app._fill_templates_menu()
    labels = [app.templates_menu.entrycget(i, "label")
              for i in range(app.templates_menu.index("end") + 1)
              if app.templates_menu.type(i) == "command"]
    assert "Warm-up  (3)" in labels


def test_pop_out_graph_follows_the_channel(app):
    app._set_watch(["ITKUA", "ILAG2"])
    for i in range(20):
        app._show_sample({"ITKUA": (100.0 + i, DEG_C, ""), "ILAG2": (1.0, "-", "")},
                         elapsed=i * 0.5)
    win, = app._pop_out(["ITKUA"])
    try:
        assert app._pop_out(["ITKUA"]) == [win]                 # one window per channel
        win.update()
        win.draw()
        assert "max 119" in win.stats_label.cget("text")
        assert "°F" in graph.units_for(win.unit)

        win.unit_var.set(DEG_F)
        win.draw()
        assert "max 246.2" in win.stats_label.cget("text")     # 119 C in F

        win.auto_var.set(False)
        win._auto_changed()
        win.min_var.set("200")
        win.max_var.set("260")
        win.draw()
        assert win.last_range == (200.0, 260.0)

        win.window_var.set("All")
        win.tunit_var.set("min")
        win.draw()

        win._toggle_pause()
        app._show_sample({"ITKUA": (150.0, DEG_C, "")}, elapsed=10.5)
        win.draw()
        assert "(20 points)" in win.stats_label.cget("text") and "PAUSED" in \
            win.stats_label.cget("text")
        win._toggle_pause()
        win.draw()
        assert "(21 points)" in win.stats_label.cget("text")

        win._motion(types.SimpleNamespace(x=300))             # hover read-off
        win.update_idletasks()
    finally:
        app.tree.selection_set("ITKUA")
        app._remove()
    assert "ITKUA" not in app.graphs and not win.winfo_exists()


def test_a_new_session_restarts_the_history(app):
    app._set_watch(["ITKUA"])
    app._show_sample({"ITKUA": (90.0, DEG_C, "")}, elapsed=30.0)
    app._show_sample({"ITKUA": (91.0, DEG_C, "")}, elapsed=0.2)  # clock went back
    assert list(app.history["ITKUA"]) == [(0.2, 91.0)]
