"""Settings: units, theme, language in the graphs, and the chart's hover."""

import pytest

from n63read import core, graph, theme

DEG_C, DEG_F = "°C", "°F"


def test_imperial_units():
    assert graph.display_unit(DEG_C, "imperial") == DEG_F
    assert graph.display_unit("hPa", "imperial") == "psi"
    assert graph.display_unit("km/h", "imperial") == "mph"
    assert graph.display_unit("V", "imperial") == "V"           # nothing to convert
    assert graph.display_unit(DEG_C, "metric") == DEG_C
    for unit, shown in graph.IMPERIAL.items():
        graph.converter(unit, shown)                            # every one converts


def test_german_numbers():
    assert graph.fmt(14.04, "de") == "14,04" and graph.fmt(14.04) == "14.04"


def test_themes():
    assert theme.resolve("dark") == "dark" and theme.resolve("nonsense") == "light"
    assert theme.resolve("system") in ("light", "dark")
    assert set(theme.PALETTES["light"]) == set(theme.PALETTES["dark"])


# ------------------------------------------------------------------ the app

gui = pytest.importorskip("n63read.gui")


def test_imperial_shows_converted_values_but_the_car_s_units_are_logged(app):
    app._set_watch(["ITKUA", "OELDRUCK", "ILAG2"])
    app._set_units("imperial")
    assert app.tree.set("ITKUA", "u") == DEG_F and app.tree.set("OELDRUCK", "u") == "psi"
    assert app.tree.set("ILAG2", "u") == "-"
    app._show_sample({"ITKUA": (100.0, DEG_C, ""), "OELDRUCK": (2934.0, "hPa", "")},
                     elapsed=1.0)
    assert app.tree.set("ITKUA", "v") == "212"
    assert app.tree.set("OELDRUCK", "v") == "42.55"
    assert app.series["ITKUA"] == [100.0]                 # kept as the car sent it
    assert core.load_settings()["units"] == "imperial"
    app._set_units("metric")
    assert app.tree.set("ITKUA", "u") == DEG_C


def test_graphs_follow_language_units_and_theme(app):
    app._set_watch(["ITKUA"])
    for i in range(5):
        app._show_sample({"ITKUA": (100.0 + i, DEG_C, "")}, elapsed=float(i))
    win, = app._pop_out(["ITKUA"])
    win.update()
    win.draw()
    assert win.time_label.cget("text") == "Time"
    assert win.stats_label.cget("text").startswith("shown:")

    app.lang_var.set(core.LANGUAGES["de"])
    app._set_language()
    assert win.time_label.cget("text") == "Zeit" and win.pause_btn.cget("text") == "Anhalten"
    assert "Alle" in win.window_box.cget("values")
    assert win.stats_label.cget("text").startswith("angezeigt:")
    assert win.heading.cget("text") == app._describe(app.catalog["ITKUA"])  # German
    assert "Kühlmitteltemperatur" in win.title() or "ITKUA" in win.title()
    win._motion(type("E", (), {"x": 300})())
    texts = [win.canvas.itemcget(i, "text") for i in win.canvas.find_all()
             if win.canvas.type(i) == "text"]
    assert any(" bei " in t for t in texts)                   # the hover read-off

    app._set_units("imperial")
    assert win.unit_var.get() == DEG_F
    win.draw()
    assert DEG_F in win.stats_label.cget("text")

    app._set_theme("dark")
    assert win.canvas.cget("bg") == theme.PALETTES["dark"]["canvas"]
    assert app.canvas.cget("bg") == theme.PALETTES["dark"]["canvas"]
    assert core.load_settings()["theme"] == "dark"


def test_the_settings_window(app):
    app._open_settings()
    win = app.settings_window
    assert win.winfo_exists() and win.title() == "Settings"
    app._open_settings()                                     # one window, raised
    assert app.settings_window is win
    app.theme_var.set("dark")
    app._set_theme(app.theme_var.get())
    assert app.theme_name == "dark"
    app.units_var.set("imperial")
    app._set_units(app.units_var.get())
    assert app.units == "imperial"


def test_hovering_the_chart_reads_off_every_line(app):
    app._set_watch(["ITKUA", "ILAG2"])
    for i in range(30):
        app._show_sample({"ITKUA": (90.0 + i, DEG_C, ""), "ILAG2": (1.0 + i / 100, "-", "")},
                         elapsed=float(i))
    app.update()
    coolant = app._chart_name("ITKUA", app.catalog["ITKUA"])
    lambda2 = app._chart_name("ILAG2", app.catalog["ILAG2"])
    assert coolant != "ITKUA" and lambda2 != "ILAG2"      # descriptions, not code names

    def chart_texts():
        return [app.canvas.itemcget(i, "text") for i in app.canvas.find_all()
                if app.canvas.type(i) == "text"]
    # Legend: "name  lo-hi unit" (two spaces); reading: "name value unit".
    assert any(t.startswith(coolant + "  ") for t in chart_texts())
    app._chart_hover(app.canvas.winfo_width() - 30)          # the newest samples
    readings = [t for t in chart_texts() if t.startswith((coolant + " ", lambda2 + " "))
                and not t.startswith((coolant + "  ", lambda2 + "  "))]
    assert any(t.startswith(coolant + " 11") and t.endswith(DEG_C) for t in readings)
    assert any(t.startswith(lambda2 + " ") for t in readings)
    app._chart_hover(None)
    assert not [t for t in chart_texts() if t.startswith(coolant + " ")
                and not t.startswith(coolant + "  ")]


def test_chart_names_follow_the_language_and_stay_short(app):
    meta = {"INFO": "Mittlere Motordrehzahl über ein Zylindersegment",
            "INFO_EN": "Mean engine speed over one cylinder segment"}
    name = app._chart_name("MITTL_MOTORDREHZAHL_PRO_ZYLSEGM", meta)
    assert name.startswith("Mean engine speed") and name.endswith("\u2026")
    assert len(name) <= 31
    app.lang = "de"
    try:
        assert app._chart_name("X", meta).startswith("Mittlere Motordrehzahl")
    finally:
        app.lang = "en"
    assert app._chart_name("EGS_Y.FIELD", {}) == "FIELD"
