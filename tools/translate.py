#!/usr/bin/env python3
"""
translate.py - regenerate the English columns of the SGBD data CSVs from German.

German is the source of truth; this rewrites only the English column of each
file. Translation is term substitution against glossary.py, done per word:

  1. Multi-word phrases from the glossary, matched on whole words.
  2. Single words, matched whole and case-insensitively.
  3. Inflected forms: strip a German ending (-e, -en, -er, -es, -em, -n, -s)
     and retry, so "unbekannte" finds "unbekannt".
  4. Compounds: split a word into glossary terms, allowing a linking -s-,
     -n- or -es- between them, so
     "Kuehlmitteltemperatur" becomes "coolant temperature". A word is only
     split if every piece is known; otherwise it is left in German.

Words are never matched inside other words. The previous approach did that
and produced "Lonunruhe" (Laufunruhe), "withte" (Mitte) and "Enginerad"
(Motorrad). Identifiers (anything touching a digit or underscore) and
all-caps words are left alone.

Usage:
  python tools/translate.py            rewrite the English columns
  python tools/translate.py --check    exit 1 if any file is out of date
  python tools/translate.py --report   most frequent words left in German
"""

import argparse
import collections
import csv
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from glossary import GLOSSARY  # noqa: E402

DATA = os.path.join(HERE, "..", "src", "n63read", "data")

# file -> (German column, English column)
COLUMNS = {
    "N63TU2_DME_readable_measurements.csv": ("INFO", "INFO_EN"),
    "BMW_fault_code_texts.csv": ("text_de", "text_en"),
    "BMW_dtc_status_byte.csv": ("text_de", "text_en"),
    "BMW_enum_tables.csv": ("text_de", "text_en"),
    "BMW_freeze_frame_fields.csv": ("name_de", "name_en"),
}
# Module catalogs from tools/module_catalog.py.
for _name in ("NBTEVO", "GSZFB1", "ACSM5", "DSC_G30N", "BDC_G11",
              "EPS_G11", "VDP_G11", "LMV_G11", "IHKA_G11", "EKP301"):
    COLUMNS[_name + "_measurements.csv"] = ("INFO", "INFO_EN")
    COLUMNS[_name + "_enum_tables.csv"] = ("text_de", "text_en")

# Glossary words that must never be a piece of a compound: articles,
# prepositions and conjunctions. Without this, "Austria" would split into
# "aus" + ... and the old bugs come back.
NOT_A_PIECE = {
    "aber", "als", "auf", "aufgrund", "aus", "bei", "beim", "bzw", "dann",
    "das", "dem", "den", "der", "des", "die", "durch", "ein", "eine", "fuer",
    "für", "gegen", "hat", "im", "in", "ist", "kein", "keine", "mit", "nach",
    "nicht", "noch", "oder", "ohne", "sondern", "sowie", "ueber", "über",
    "und", "unter", "von", "vor", "waehrend", "während", "wegen", "wenn",
    "werden", "wird", "wurde", "wuerde", "würde", "zu", "zum", "zur",
    "zwischen",
}

# A value that is only one of these words is a switch state, where "aus"
# means off rather than the "from" it means inside a sentence.
STATES = {"aus": "off", "ein": "on", "an": "on", "aktiv": "active",
          "inaktiv": "inactive", "nicht aktiv": "not active"}

ENDINGS = ("en", "em", "er", "es", "e", "n", "s")
LINKS = ("s", "n", "es")
MIN_PIECE = 3

WORD = re.compile(r"(?<!\w)[^\W\d_]+(?!\w)")


_FOLD = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def fold(text):
    """Lower-case and spell umlauts out, so 'Kühl' and 'Kuehl' are one key."""
    return text.lower().translate(_FOLD)


def _build():
    phrases, words = {}, {}
    for de, en in GLOSSARY:
        key = fold(de)
        if " " in de.strip():
            phrases.setdefault(key, en)
        # First entry wins, except a lower-case English rendering beats a
        # capitalised one; sentence-initial capitals are applied later.
        elif key not in words or (en[:1].islower() and words[key][:1].isupper()):
            words[key] = en
    # Phrases are matched against the original text, so accept either
    # spelling of each umlaut in the pattern.
    def pattern(p):
        out = re.escape(p)
        for spelled, umlaut in (("ae", "ä"), ("oe", "ö"), ("ue", "ü"), ("ss", "ß")):
            out = out.replace(spelled, "(?:%s|%s)" % (spelled, umlaut))
        return out
    alternation = "|".join(pattern(p) for p in sorted(phrases, key=len, reverse=True))
    phrase_re = re.compile(r"(?<!\w)(?:%s)(?!\w)" % alternation, re.IGNORECASE) if phrases else None
    return phrases, words, phrase_re


PHRASES, WORDS, PHRASE_RE = _build()
PIECES = {k: v for k, v in WORDS.items() if k not in NOT_A_PIECE and len(k) >= MIN_PIECE}


def _lookup(word):
    """Whole word, then with a German ending stripped."""
    w = fold(word)
    if w in WORDS:
        return WORDS[w]
    for end in ENDINGS:
        if w.endswith(end) and len(w) - len(end) >= MIN_PIECE + 1:
            stem = w[:-len(end)]
            if stem in WORDS:
                return WORDS[stem]
    return None


def _split(word):
    """Fewest-pieces split of a compound into glossary terms, or None."""
    w = fold(word)
    n = len(w)
    best = [None] * (n + 1)       # best[i] = list of English pieces for w[:i]
    best[0] = []
    for i in range(n):
        if best[i] is None:
            continue
        starts = [i]
        # A linking element (Fugenelement) may sit between two pieces:
        # Zeit-s-punkt, Klappe-n-steller, Tag-es-zeit.
        if i > 0:
            for link in LINKS:
                if w.startswith(link, i):
                    starts.append(i + len(link))
        for s in starts:
            for j in range(s + MIN_PIECE, n + 1):
                piece = w[s:j]
                en = PIECES.get(piece)
                if en is None and j == n:
                    for end in ENDINGS:
                        if piece.endswith(end) and piece[:-len(end)] in PIECES:
                            en = PIECES[piece[:-len(end)]]
                            break
                if en is None:
                    continue
                cand = best[i] + [en]
                if best[j] is None or len(cand) < len(best[j]):
                    best[j] = cand
    pieces = best[n]
    return " ".join(pieces) if pieces and len(pieces) > 1 else None


def translate_word(word):
    if len(word) > 1 and word.isupper():
        return word                     # acronym or symbolic constant
    en = _lookup(word)
    if en is None:
        en = _split(word)
    return word if en is None else en


def translate(text):
    """German -> English by phrase, word, inflection and compound lookup."""
    if not text or text.strip() in ("", "-"):
        return text
    state = STATES.get(fold(text.strip()))
    if state:
        source = text.strip()
        if source.isupper():
            return state.upper()
        return state.capitalize() if source[:1].isupper() else state

    def sub_phrase(m):
        return "\x00%s\x01" % PHRASES[fold(m.group(0))]

    marked = PHRASE_RE.sub(sub_phrase, text) if PHRASE_RE else text
    # Translate words outside the phrase markers only.
    parts = re.split(r"(\x00[^\x01]*\x01)", marked)
    out = []
    for part in parts:
        if part.startswith("\x00"):
            out.append(part[1:-1])
        else:
            out.append(WORD.sub(lambda m: translate_word(m.group(0)), part))
    result = "".join(out)

    # Keep a capital at the start if the German had one.
    if text[:1].isupper() and result[:1].islower():
        result = result[0].upper() + result[1:]
    return result


def _looks_german(word):
    """Heuristic for --report: a capitalised noun or a word with an umlaut."""
    return (len(word) > 3 and word[:1].isupper() and not word.isupper()) or \
        any(c in word for c in "äöüßÄÖÜ")


# ------------------------------------------------------------------- files

def _read(path):
    with open(path, "rb") as f:
        return list(csv.reader(io.StringIO(f.read().decode("utf-8-sig"), newline="")))


def _render(rows):
    buf = io.StringIO(newline="")
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")


def regenerate(name):
    """Return (old_bytes, new_bytes) for one data file."""
    path = os.path.join(DATA, name)
    with open(path, "rb") as f:
        old = f.read()
    rows = _read(path)
    de_col, en_col = COLUMNS[name]
    header = rows[0]
    di, ei = header.index(de_col), header.index(en_col)
    for row in rows[1:]:
        if len(row) > max(di, ei):
            row[ei] = translate(row[di])
    return old, _render(rows)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--check", action="store_true",
                   help="exit 1 if any English column is out of date")
    p.add_argument("--report", type=int, nargs="?", const=60, metavar="N",
                   help="list the N most frequent words left untranslated")
    args = p.parse_args(argv)

    if args.report:
        counts = collections.Counter()
        for name, (de_col, _) in COLUMNS.items():
            rows = _read(os.path.join(DATA, name))
            di = rows[0].index(de_col)
            for row in rows[1:]:
                if len(row) > di:
                    counts.update(w for w in WORD.findall(row[di])
                                  if translate_word(w) == w and _looks_german(w))
        for word, n in counts.most_common(args.report):
            print("%5d  %s" % (n, word))
        return 0

    stale = []
    for name in COLUMNS:
        old, new = regenerate(name)
        if old != new:
            stale.append(name)
            if not args.check:
                with open(os.path.join(DATA, name), "wb") as f:
                    f.write(new)
    if args.check:
        for name in stale:
            print("out of date: %s" % name)
        return 1 if stale else 0
    print("rewrote %d file(s)" % len(stale) if stale else "already up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
