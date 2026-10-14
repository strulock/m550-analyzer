import pytest

from n63read import core

from test_head_unit import FakeH


# ------------------------------------------------------------ function groups

@pytest.mark.parametrize("texts, group", [
    (("HU_STATUS_GPS_POSITION", "GPS Breitengrad"), "GPS & navigation"),
    (("EGS_GETRIEBEOELTMEPERATUR", "Getriebeöltemperatur"), "Gearbox & driveline"),
    (("EGS_ADAPTIONSWERTE_GLS_KF1_1_2", "Schaltungsapplikation"), "Gearbox & driveline"),
    (("DSC_RDC_MESSDATENBLOCK_1", "Reifendruck"), "Brakes & tires"),
    (("ILAG2", "Lambda-Istwert Bank 2"), "Lambda & exhaust"),
    (("ILUZ5", "Laufunruhe Zylinder 5"), "Ignition & misfire"),
    (("ITOEL", "Oeltemperatur nach Filter"), "Oil"),
    (("EKP_STATUS_ADC_BEWERTET", "Kraftstoffpumpe Strom"), "Fuel"),
    (("XYZ", "nichts Bekanntes"), core.OTHER_GROUP),
])
def test_function_group(texts, group):
    assert core.function_group(*texts) == group


# --------------------------------------------------------------- answers

def test_availability_round_trip(tmp_path):
    path = str(tmp_path / "a" / "availability.json")
    core.save_availability({(0x12, 0x588A): "ok", (0x63, 0xD039): "refused"}, path)
    assert core.load_availability(path) == {(0x12, 0x588A): "ok", (0x63, 0xD039): "refused"}
    assert core.load_availability(str(tmp_path / "missing.json")) == {}


def test_import_check_csv(tmp_path):
    path = tmp_path / "check.csv"
    path.write_text("module,did,job,status,detail,reply_bytes,raw\n"
                    "0x63,0xD039,STATUS_GPS_POSITION,ok,13 bytes,13,00\n"
                    "0x18,0xDA2E,ISTGANG,refused,requestOutOfRange,0,\n", encoding="utf-8")
    assert core.import_check_csv(str(path)) == {(0x63, 0xD039): "ok", (0x18, 0xDA2E): "refused"}
    bad = tmp_path / "bad.csv"
    bad.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError):
        core.import_check_csv(str(bad))


def test_check_steps_skips_a_silent_module():
    layouts = {(0x18, d): [{"DATENTYP": "unsigned char"}] for d in range(10)}
    layouts[(0x63, 1)] = [{"DATENTYP": "unsigned char"}]
    answers = {(0x18, d): core.HsfzTimeout("t") for d in range(10)}
    answers[(0x63, 1)] = b"\x01"
    h = FakeH(answers)
    got = list(core.check_steps(h, layouts))
    assert sum(1 for k in h.calls if k[0] == 0x18) == 3
    assert got[-1] == ((0x63, 1), "ok")


# ------------------------------------------------------------------- GUI

gui = pytest.importorskip("n63read.gui")


@pytest.fixture(scope="module")
def shared_app(tmp_path_factory):
    # One window for all of these: creating Tk roots back to back is flaky
    # on some Windows Pythons ("Can't find a usable init.tcl").
    avail = str(tmp_path_factory.mktemp("answers") / "avail.json")
    saved = core.availability_path
    core.availability_path = lambda: avail
    try:
        a = gui.App()
    except Exception as e:                                  # noqa: BLE001
        core.availability_path = saved
        pytest.skip("no display: %s" % e)
    a._stop_feed()
    a.avail_file = avail
    yield a
    a.destroy()
    core.availability_path = saved


@pytest.fixture
def app(shared_app):
    a = shared_app
    a._clear()
    a.avail.clear()
    a.filter_var.set("")
    a.group_var.set("Module")
    a.only_var.set(False)
    a._refilter()
    return a


def tops(app):
    return [app.picker.item(i, "text").rsplit("  (", 1)[0] for i in app.picker.get_children()]


def test_grouped_by_module_with_the_dme_first(app):
    names = tops(app)
    assert names[0] == gui.DME_LABEL
    assert {"Head unit", "Transmission", "DSC", "Fuel pump"} <= set(names)
    assert sum(len(app._members["g|" + t]) for t in names) == len(app.catalog)


def test_grouped_by_function(app):
    app.group_var.set("Function")
    app._refilter()
    names = tops(app)
    assert names[0] == core.FUNCTION_GROUPS[0][0] and names[-1] == core.OTHER_GROUP


def test_groups_fill_lazily_and_keep_records_together(app):
    sub = "g|Head unit|GPS & navigation"
    assert app.picker.get_children(sub) == (sub + "|...",)
    app._fill(sub)
    record = sub + "|HU_STATUS_GPS_POSITION"
    assert app.picker.exists(record)
    assert set(app._members[record]) == {"HU_STATUS_GPS_POSITION." + f for f in
                                          ("VALIDITY", "LATITUDE", "LONGITUDE", "HEIGHT")}


def test_filter_expands_small_results(app):
    app.filter_var.set("latitude")
    app._refilter()
    assert app.picker.exists("c|HU_STATUS_GPS_POSITION.LATITUDE")
    assert "of" in app.count_label.cget("text")


def test_selecting_a_group_adds_it_without_private_values(app, monkeypatch):
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: True)
    infos = []
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda *a, **k: infos.append(a))
    app.picker.selection_set("g|Head unit")
    app._add()
    watched = {n for n, _, _ in app.watch}
    assert "HU_STATUS_GPS_POSITION.LATITUDE" in watched
    assert not any(core.is_sensitive(n) for n in watched)
    assert infos                                              # told what was skipped


def test_only_answering_channels(app):
    app.only_var.set(True)
    app._refilter()
    assert app.picker.get_children() == ("none",)            # nothing known yet
    app._learn({"HU_STATUS_GPS_POSITION.LATITUDE": (30.1, "deg", ""),
                "HU_STATUS_GPS": (None, "", "requestOutOfRange")})
    assert app.avail[(0x63, 0xD039)] == "ok" and app.avail[(0x63, 0xD038)] == "refused"
    app._refilter()
    assert tops(app) == ["Head unit"]
    assert set(app._members["g|Head unit"]) == {n for n, m in app.catalog.items()
                                                 if m.get("ID") == "0xD039"
                                                 and m.get("MODULE") == "0x63"}
    app._fill("g|Head unit|GPS & navigation")
    leaf = "c|HU_STATUS_GPS_POSITION.LATITUDE"
    assert app.picker.item(leaf, "text").startswith("✓")


def test_answers_are_saved_and_reloaded(app):
    app._learn({"HU_STATUS_GPS_POSITION.LATITUDE": (30.1, "deg", "")})
    app._answers_changed("test")
    assert core.load_availability(app.avail_file) == {(0x63, 0xD039): "ok"}
