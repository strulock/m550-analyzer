import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import translate  # noqa: E402


@pytest.mark.parametrize("german, english", [
    # Words the old substring approach mangled.
    ("Laufunruhe Zylinder 5", "Uneven running cylinder 5"),
    ("Innenbeleuchtung Mitte", "Interior lighting center"),
    ("Motorrad Tachometer", "Motorcycle Tachometer"),
    ("unbekannte Fehlerart", "unknown fault type"),
    ("austriamicrosystems AG", "austriamicrosystems AG"),
    # Phrases, compounds, linking elements and inflection.
    ("Kurzschluss nach Masse", "Short circuit to ground"),
    ("Zaehler Aussetzerkennung Zylinder 5", "Counter misfire detection cylinder 5"),
    ("Kühlmitteltemperatur zu hoch", "Coolant temperature too high"),
    ("Abgasklappensteller", "Exhaust flap actuator"),
    ("Fehler momentan vorhanden und bereits gespeichert",
     "Fault currently present and already stored"),
    # "aus" is "from" in a sentence and "off" as a switch state.
    ("Drosselklappenwinkel aus Poti 1", "Throttle valve angle from potentiometer 1"),
    ("Aus", "Off"),
    ("AUS", "OFF"),
])
def test_translate(german, english):
    assert translate.translate(german) == english


def test_umlaut_and_ascii_spellings_are_the_same():
    assert translate.translate("Kühlmittel") == translate.translate("Kuehlmittel")


def test_identifiers_and_acronyms_are_left_alone():
    for text in ("STAT_LAMBDA_WERT", "CA_response_check_nok", "DME", "KOMBI"):
        assert translate.translate(text) == text


def test_compound_only_splits_into_known_pieces():
    # "Austria" must not become "from" + "tria".
    assert translate.translate_word("Austria") == "Austria"


def test_data_files_match_the_glossary():
    # Fails if someone edits glossary.py without regenerating, or edits an
    # English column by hand. Fix: python tools/translate.py
    assert translate.main(["--check"]) == 0
