import os
import re
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "tools"))

import channel_docs  # noqa: E402


def test_channel_lookup_is_up_to_date():
    """docs/channels/ is generated; regenerate with python tools/channel_docs.py."""
    assert channel_docs.main(["--check"]) == 0


def test_every_channel_is_in_the_lookup():
    files = channel_docs.build()
    text = "".join(files.values())
    from n63read import core
    names = set(core.load_catalog()) | set(core.load_module_catalogs())
    missing = [n for n in names if "`%s`" % n not in text]
    assert not missing, missing[:10]


def test_no_lookup_page_is_too_big_to_render():
    for name, text in channel_docs.build().items():
        assert len(text.encode("utf-8")) <= channel_docs.PAGE_LIMIT, name


def _anchors(path):
    with open(path, encoding="utf-8") as f:
        return {channel_docs.anchor(m.group(1).replace("`", ""))
                for m in re.finditer(r"^#+ (.+)$", f.read(), re.M)}


def _md_files():
    out = [os.path.join(ROOT, "README.md"), os.path.join(ROOT, "PROJECT.md")]
    for base, _, files in os.walk(os.path.join(ROOT, "docs")):
        out += [os.path.join(base, f) for f in files if f.endswith(".md")]
    return out


@pytest.mark.parametrize("path", _md_files(), ids=lambda p: os.path.relpath(p, ROOT))
def test_relative_links_resolve(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    bad = []
    for target in re.findall(r"\]\(([^)\s]+)\)", text):
        if re.match(r"[a-z]+:", target):
            continue                                    # http:, mailto:
        file_part, _, frag = target.partition("#")
        dest = os.path.normpath(os.path.join(os.path.dirname(path), file_part)) \
            if file_part else path
        if not os.path.exists(dest):
            bad.append(target)
        elif frag and dest.endswith(".md") and frag not in _anchors(dest):
            bad.append(target)
    assert not bad, bad
