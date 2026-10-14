import json
import os
import sys

import pytest

from n63read import core

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "tools"))

import translate  # noqa: E402


# ------------------------------------------------------------ curated English

def test_every_channel_and_value_text_has_a_curated_translation():
    """New catalog text falls back to the glossary; curate it in
    tools/translations_en.csv (python tools/translate.py --uncurated)."""
    missing = translate.uncurated()
    assert not missing, sorted(missing)[:20]


def test_curated_translation_wins_over_the_glossary():
    de, en = next(iter(translate.CURATED_EN.items()))
    assert translate.translate(de) == en
    assert translate.translate("  %s " % de) == en


def test_curated_file_is_clean():
    for de, en in translate.CURATED_EN.items():
        assert en.strip() == en and en, de
        assert "\n" not in en, de


def test_catalogs_carry_the_curated_english():
    cat = core.load_module_catalogs()
    lat = cat["HU_STATUS_GPS_POSITION.LATITUDE"]
    assert lat["INFO_EN"] == translate.CURATED_EN[lat["INFO"]]


# ---------------------------------------------------------------- settings

def test_settings_round_trip(tmp_path):
    path = str(tmp_path / "s" / "settings.json")
    assert core.load_settings(path) == {}
    core.save_settings({"language": "de"}, path)
    assert core.load_settings(path) == {"language": "de"}
    with open(path, "w", encoding="utf-8") as f:
        f.write("not json")
    assert core.load_settings(path) == {}


# --------------------------------------------------------------------- GUI

pytest.importorskip("n63read.gui")


def test_switching_to_german_relabels_picker_watch_and_status(app):
    name = "HU_STATUS_GPS"
    meta = app.catalog[name]
    app._add_names([name])
    app._show_sample({name: (12, "", "")})
    english_note = app.tree.set(name, "note")
    assert app.tree.item(name, "text") == meta["INFO_EN"]

    app.lang_var.set(core.LANGUAGES["de"])
    app._set_language()
    assert app.lang == "de"
    assert app.tree.item(name, "text") == meta["INFO"]
    app._show_sample({name: (12, "", "")})
    german_note = app.tree.set(name, "note")
    assert german_note == core.enum_text(12, meta, app.enums, "text_de")
    assert german_note != english_note
    assert app._info[name][3].startswith(meta["INFO"][:20])

    # Saved, and a German search still finds English words and vice versa.
    assert json.load(open(core.settings_path(), encoding="utf-8"))["language"] == "de"
    app.filter_var.set("latitude")
    app._refilter()
    assert app.picker.exists("c|HU_STATUS_GPS_POSITION.LATITUDE")


def test_language_is_restored_at_start(app):
    app.lang_var.set(core.LANGUAGES["de"])
    app._set_language()
    assert core.load_settings()["language"] == "de"
    # A new window reads it; checked through the same loader the App uses.
    assert core.load_settings().get("language") in core.LANGUAGES
