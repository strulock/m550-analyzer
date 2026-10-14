import queue
import random

import pytest

from n63read import core, playback


def test_demo_channels_are_real_catalog_channels():
    catalog = core.load_catalog(core.resource_path(core.DEFAULT_CSV))
    catalog.update(core.load_module_catalogs())
    for name in playback.DEMO_CHANNELS:
        assert name in catalog


def test_demo_sample_covers_every_channel_with_plausible_values():
    rng = random.Random(1)
    for t in range(0, 600, 3):
        s = playback.demo_sample(t, rng)
        assert set(s) == set(playback.DEMO_CHANNELS)
        assert 600 <= s["MITTL_MOTORDREHZAHL_PRO_ZYLSEGM"] <= 7000
        assert 0 <= s["BUS_FAHRPEDALWERT"] <= 100
        assert 0.7 < s["LAMBDA_ISTWERT_SENSOR1_BANK2_2"] < 1.4
        assert 30 <= s["ITOEL"] <= 101


def _write_log(tmp_path, text):
    path = tmp_path / "log.csv"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_read_log_parses_numbers_text_and_blanks(tmp_path):
    path = _write_log(tmp_path, "Time,ITOEL,STATE\n0.0,80.5,ON\n0.25,,OFF\n")
    names, rows = playback.read_log(path)
    assert names == ["ITOEL", "STATE"]
    assert rows == [(0.0, {"ITOEL": 80.5, "STATE": "ON"}),
                    (0.25, {"ITOEL": None, "STATE": "OFF"})]


@pytest.mark.parametrize("text", ["", "a,b\n1,2\n", "Time,ITOEL\n"])
def test_read_log_rejects_foreign_or_empty_files(tmp_path, text):
    with pytest.raises(ValueError):
        playback.read_log(_write_log(tmp_path, text))


def test_replay_feed_plays_every_row_then_finishes():
    rows = [(0.0, {"A": 1.0}), (0.01, {"A": 2.0}), (0.02, {"A": None})]
    q = queue.Queue()
    feed = playback.ReplayFeed(q, rows)
    feed.set_channels([("A", 0, {}), ("B", 0, {})])
    feed.start()
    feed.join(5)
    msgs = [q.get_nowait() for _ in range(q.qsize())]
    samples = [m[3] for m in msgs if m[0] == "feedsample"]
    assert [s["A"][0] for s in samples] == [1.0, 2.0, None]
    assert samples[0]["B"] == (None, "", "not in log")
    assert msgs[-1] == ("feeddone", feed)
