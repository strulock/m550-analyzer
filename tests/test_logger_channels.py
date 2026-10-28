import csv

from n63read import cli, core, loggerchannels

CAT = cli._catalog(core.DEFAULT_CSV)
GPS = (0x63, 0xD039)


def _rows(avail, fast=()):
    return {r["name"]: r for r in loggerchannels.channels(CAT, avail, fast, ())}


def test_only_identifiers_that_answer_ok_are_swept():
    rows = _rows({GPS: "ok", (0x63, 0xD030): "size", (0x40, 0xDD7B): "refused"})
    assert "HU_STATUS_GPS_POSITION.LATITUDE" in rows
    assert not any(r["did"] == "0xD030" for r in rows.values())     # wrong size: would misdecode
    assert {r["did"] for r in rows.values()} == {"0xD039"}
    assert all(r["tier"] == "slow" for r in rows.values())


def test_fast_channels_are_kept_whatever_was_recorded():
    rows = _rows({}, fast=["INMOT", "INMOT@0x13", "EGS_ISTGANG.ISTGANG"])
    assert set(rows) == {"INMOT", "INMOT@0x13", "EGS_ISTGANG.ISTGANG"}
    assert rows["INMOT"]["module"] == "0x12"
    assert rows["INMOT@0x13"]["module"] == "0x13"
    assert rows["INMOT"]["tier"] == "fast"


def test_the_slave_counts_as_answering_where_the_master_does():
    did = int(CAT["ITOEL"]["ID"], 16)
    rows = _rows({(0x12, did): "ok"})
    assert {"ITOEL", "ITOEL@0x13"} <= set(rows)
    rows = _rows({(0x12, did): "ok", (0x13, did): "no answer"})       # checked itself
    assert "ITOEL" in rows and "ITOEL@0x13" not in rows


def test_dme_scaling_and_types():
    r = _rows({}, fast=["ITOEL", "INMOT"])
    assert {k: r["ITOEL"][k] for k in ("did", "did_size", "offset", "type", "order", "mul",
                                       "div", "add", "unit")} == {
        "did": "0x4402", "did_size": 2, "offset": 0, "type": "u16", "order": "big",
        "mul": "0.75", "div": "1", "add": "-48", "unit": "°C"}
    assert r["INMOT"]["type"] == "s16"


def test_gps_offsets_match_the_parser():
    rows = _rows({GPS: "ok"})
    payload = bytes([0x00, 0x00, 0x20, 0x2E, 0x24, 0x32, 0x01, 0x9F, 0x2F, 0x30, 0x05,
                     0x00, 0xC8])
    want = core.parse_gps_position(payload)
    lat, lon = rows["HU_STATUS_GPS_POSITION.LATITUDE"], rows["HU_STATUS_GPS_POSITION.LONGITUDE"]
    assert (lat["type"], lon["type"], lat["did_size"]) == ("gps", "gps", 13)
    assert round(core._dms(payload[lat["offset"]:lat["offset"] + 5]), 7) == want["lat"]
    assert round(core._dms(payload[lon["offset"]:lon["offset"] + 5]), 7) == want["lon"]
    height = rows["HU_STATUS_GPS_POSITION.HEIGHT"]
    o = height["offset"]
    assert int.from_bytes(payload[o:o + 2], "big") == want["height"]


def test_strings_private_and_calculated_channels_are_left_out():
    every_ok = {k: "ok" for k in core.check_layouts(CAT)}
    every_ok.update({(0x13, k[1]): "ok" for k in list(every_ok) if k[0] == 0x12})
    rows = loggerchannels.channels(CAT, every_ok, [])
    names = {r["name"] for r in rows}
    assert not any(core.is_sensitive(n) for n in names)
    assert not any(n in core.DERIVED for n in names)
    assert all(r["type"] in loggerchannels.TYPES.values() for r in rows)
    # A field after one of unknown size has no fixed place, so it isn't listed.
    layouts = core.record_layouts(CAT)
    for key, fields in layouts.items():
        sizes = [core.field_size((f.get("DATENTYP") or "").strip().lower(), b"", 0)
                 for f in fields]
        if None in sizes and not any((f.get("POS") or "").strip() for f in fields):
            after = [f["ARG"] for f in fields[sizes.index(None) + 1:]]
            assert not names & set(after), key


def test_default_tiers():
    fast, medium = loggerchannels.tier_lists()
    assert {"INMOT", "I_EWG_POS", "I_EWG_POS@0x13", "TV_EWG@0x13", "ILAG1@0x13",
            "MITTELW_ZYLIND_ZWSPAET_KR_HALBQUANT@0x13", "IWDKL"} <= set(fast)
    assert {"MASSENSTROM_KRAFTSTOFF@0x13", "HU_STATUS_GPS_POSITION.LATITUDE", "IMUL1@0x13",
            "SPG_POS_SENS_EWG", "ZAEHLER_AUSSETZERK_ZYL5"} <= set(medium)
    # The engine's coolant temperature, not the radiator outlet's (ITKUA, 52 °C
    # with the oil at 97 °C on 27 Oct), is the one the check-ins report.
    assert "ITKUM" in medium and CAT["ITKUM"]["ID"] == "0x4300"
    # The battery: the sensor's first, the DME's as the check-ins' fallback.
    assert {"IUIBS", "IUADW"} <= set(medium)
    assert not set(fast) & set(medium)                    # fast wins
    assert not any(n in core.DERIVED for n in fast + medium)


def test_app_templates_replace_the_tiers():
    fast, medium = loggerchannels.tier_lists({"Logger fast": ["INMOT", "CALC_BOOST_MINUS_TARGET"],
                                              "Logger medium": ["INMOT", "ITOEL"]})
    assert fast == ["INMOT", "IPLAD", "S_LADEDRUCK"]      # calculated: its inputs
    assert medium == ["ITOEL"]
    rows = {r["name"]: r["tier"] for r in loggerchannels.channels(CAT, {}, fast, medium)}
    assert rows == {"INMOT": "fast", "IPLAD": "fast", "S_LADEDRUCK": "fast", "ITOEL": "medium"}


def test_cli_writes_the_file(tmp_path, monkeypatch, capsys):
    check = tmp_path / "check.csv"
    with open(check, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["module", "did", "job", "status", "detail", "reply_bytes", "raw"])
        w.writerow(["0x63", "0xD039", "STATUS_GPS_POSITION", "ok", "13 bytes", 13, ""])
    out = tmp_path / "channels.csv"
    cli.main(["logger-channels", "--check", str(check), "--out", str(out)])
    with open(out, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[0].keys() == set(loggerchannels.HEADER)
    assert {r["tier"] for r in rows} == {"fast", "medium", "slow"}
    assert "channels ->" in capsys.readouterr().out


def test_merging_checks_keeps_what_answered():
    old = {GPS: "ok", (0x40, 1): "ok", (0x40, 2): "no answer", (0x40, 3): "refused",
           (0x40, 5): "refused"}
    new = {GPS: "no answer", (0x40, 1): "refused", (0x40, 2): "ok", (0x40, 3): "no answer",
           (0x76, 4): "no answer"}
    assert loggerchannels.merge_answers([old, new]) == {
        GPS: "ok", (0x40, 1): "ok",      # asleep or not ready this time: still answers
        (0x40, 2): "ok",
        (0x40, 3): "no answer",          # never answered: the newest says
        (0x40, 5): "refused", (0x76, 4): "no answer"}


def _check_csv(path, rows):
    core.write_check_csv(rows, str(path))
    return str(path)


def test_cli_merges_several_checks(tmp_path, capsys):
    a = _check_csv(tmp_path / "a.csv", [(0x63, 0xD039, "STATUS_GPS_POSITION", "ok")])
    b = _check_csv(tmp_path / "b.csv", [(0x63, 0xD039, "STATUS_GPS_POSITION", "no answer"),
                                       (0x63, 0xD030, "STATUS_SPEED", "ok")])
    out = tmp_path / "channels.csv"
    cli.main(["logger-channels", "--check", a, "--check", b, "--out", str(out)])
    with open(out, newline="", encoding="utf-8") as f:
        sweep = {r["did"] for r in csv.DictReader(f) if r["tier"] == "slow"}
    assert sweep == {"0xD039", "0xD030"}


def test_cli_takes_the_checks_from_the_cloud(tmp_path, monkeypatch, capsys):
    from n63read import cloud
    monkeypatch.setattr(cloud, "check_files", lambda settings: [
        ({GPS: "ok"}, "logs/check.csv"), ({(0x63, 0xD030): "ok"}, "check_2026-10-26.csv")])
    out = tmp_path / "channels.csv"
    cli.main(["logger-channels", "--from-cloud", "--out", str(out)])
    with open(out, newline="", encoding="utf-8") as f:
        sweep = {r["did"] for r in csv.DictReader(f) if r["tier"] == "slow"}
    assert sweep == {"0xD039", "0xD030"}
    assert "cloud: check_2026-10-26.csv" in capsys.readouterr().out


def test_cloud_check_files_reads_only_checks(monkeypatch):
    from n63read import cloud
    listing = {"files": [
        {"key": "check_b.csv", "uploaded": "2026-10-26T10:00:00Z"},
        {"key": "logs/check_a.csv", "uploaded": "2026-10-20T10:00:00Z"},
        {"key": "n63_drive.csv", "uploaded": "2026-10-21T10:00:00Z"},
        {"key": "check_notes.csv", "uploaded": "2026-10-22T10:00:00Z"}]}
    bodies = {
        "/api/files/logs/check_a.csv": b"module,did,job,status\n0x63,0xD039,X,ok\n",
        "/api/files/check_b.csv": b"\xef\xbb\xbfmodule,did,job,status\n0x63,0xD030,Y,refused\n",
        "/api/files/check_notes.csv": b"note\nnot a check\n"}
    monkeypatch.setattr(cloud, "request", lambda s, m, p, *a, **k: listing)
    monkeypatch.setattr(cloud, "fetch", lambda s, p: bodies[p])
    got = cloud.check_files({})
    assert [k for _, k in got] == ["logs/check_a.csv", "check_b.csv"]     # oldest first
    assert got[1][0] == {(0x63, 0xD030): "refused"}


def test_the_catalog_for_the_cloud_has_every_decodable_channel_and_the_answers():
    tire = "DSC_RDC_MESSDATENBLOCK_1.LETZTER_REIFENDRUCKWERT"
    did = int(CAT["ITOEL"]["ID"], 16)
    p = loggerchannels.catalog_payload(CAT, {(0x29, 0xDC98): "ok", (0x12, did): "ok",
                                             (0x63, 0xD030): "size"})
    cols = p["columns"]
    rows = {r[cols.index("name")]: dict(zip(cols, r)) for r in p["rows"]}
    # Every channel the board can decode, answering or not, and nothing it can't.
    assert len(rows) > 10000 and "INMOT" in rows and tire in rows
    assert "VIN" not in rows and not any(n.endswith("_TEXT") for n in rows if n in CAT and
                                         CAT[n].get("DATENTYP", "").startswith("string"))
    t = rows[tire]
    assert (t["module"], t["did"], t["offset"], t["type"], t["div"], t["unit"]) == (
        "0x29", "0xDC98", 5, "u16", "1000", "bar")
    assert t["info"].startswith("Last valid tire pressure") and t["group"] == "RDC_MESSDATENBLOCK_1"
    # Answers per identifier; the slave DME takes the master's where it wasn't checked.
    assert p["answers"]["0x29:0xDC98"] == "ok" and p["answers"]["0x63:0xD030"] == "size"
    assert p["answers"]["0x13:0x%04X" % did] == "ok"
    # The built-in lists are the first preset's tiers.
    assert p["defaults"]["INMOT"] == "fast" and p["defaults"]["IMUL1"] == "medium"
    assert p["modules"]["0x29"] == "DSC" and p["modules"]["0x13"] == "DME (bank 2)"


def test_the_catalog_rows_decode_like_the_channel_file():
    p = loggerchannels.catalog_payload(CAT, {GPS: "ok"})
    cols = p["columns"]
    from_catalog = {r[0]: dict(zip(cols, r)) for r in p["rows"]}
    for r in loggerchannels.channels(CAT, {GPS: "ok"}):
        want = {k: r[k] for k in loggerchannels.HEADER if k != "tier"}
        assert {k: from_catalog[r["name"]][k] for k in want} == want
