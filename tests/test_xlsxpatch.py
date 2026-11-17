"""Het schrijven via xlsxpatch laat alles behalve de gewijzigde cellen ongemoeid."""

import datetime as dt
import re
import warnings
import zipfile

import pytest
from openpyxl import load_workbook

from register.xlsxpatch import PatchFout, patch_werkmap, werkblad_paden

from .conftest import FIXTURE


def laad(pad):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return load_workbook(pad)


def tel_xml(pad, patroon):
    """Aantal treffers per werkblad in de ruwe XML (openpyxl ziet de x14-keuzelijsten niet)."""
    paden = werkblad_paden(pad)
    with zipfile.ZipFile(pad) as z:
        return {blad: len(re.findall(patroon, z.read(p).decode())) for blad, p in paden.items()}


def formules(pad):
    wb = laad(pad)
    return {(ws.title, c.coordinate): c.value for ws in wb.worksheets for row in ws.iter_rows() for c in row
            if isinstance(c.value, str) and c.value.startswith("=")}


def test_alleen_gewijzigde_onderdelen_veranderen(tmp_path):
    doel = patch_werkmap(FIXTURE, tmp_path / "uit.xlsx", {"Testruns": {"A2": "RUN-1", "I2": 2}})
    with zipfile.ZipFile(FIXTURE) as a, zipfile.ZipFile(doel) as b:
        assert a.namelist() == b.namelist()
        anders = [n for n in a.namelist() if a.read(n) != b.read(n)]
    assert sorted(anders) == ["xl/workbook.xml", "xl/worksheets/sheet4.xml"]


def test_keuzelijsten_opmaak_opmerkingen_en_formules_blijven(tmp_path):
    doel = patch_werkmap(FIXTURE, tmp_path / "uit.xlsx", {"Testruns": {"A2": "RUN-1", "B2": "IE-KERN-001"}})
    dv = r"<(?:x14:)?dataValidation "
    assert tel_xml(doel, dv) == tel_xml(FIXTURE, dv)
    assert sum(tel_xml(FIXTURE, dv).values()) == 15
    assert tel_xml(doel, "<conditionalFormatting") == tel_xml(FIXTURE, "<conditionalFormatting")
    origineel, nieuw = laad(FIXTURE), laad(doel)
    for ws in origineel.worksheets:
        n = nieuw[ws.title]
        assert sum(1 for r in ws.iter_rows() for c in r if c.comment) == \
            sum(1 for r in n.iter_rows() for c in r if c.comment)
    assert formules(doel) == formules(FIXTURE)
    assert nieuw.calculation.fullCalcOnLoad is True


def test_waarden_en_typen(tmp_path):
    tekst = 'Regel 1\nRegel 2 <tag> & "quote" _x0041_ \x07einde'
    doel = patch_werkmap(FIXTURE, tmp_path / "uit.xlsx", {"Testruns": {
        "A2": "RUN-1", "F2": dt.date(2026, 11, 16), "I2": 3, "J2": tekst, "K2": "n.v.t."}})
    ws = laad(doel)["Testruns"]
    assert ws["A2"].value == "RUN-1"
    assert ws["F2"].value == dt.datetime(2026, 11, 16)
    assert ws["F2"].number_format == "dd\\-mm\\-yyyy"
    assert ws["I2"].value == 3 and isinstance(ws["I2"].value, int)
    # openpyxl decodeert Excel-escapes niet; Excel leest '_x005F_x0041_' als de letterlijke tekst '_x0041_'.
    assert ws["J2"].value == tekst.replace("\x07", "").replace("_x0041_", "_x005F_x0041_")
    assert ws["K2"].value == "n.v.t."
    assert ws["A2"].style_id == laad(FIXTURE)["Testruns"]["A2"].style_id


def test_weigert_formulecel_en_bestaand_doel(tmp_path):
    with pytest.raises(PatchFout, match="formule"):
        patch_werkmap(FIXTURE, tmp_path / "a.xlsx", {"Testruns": {"C2": "x"}})
    assert not (tmp_path / "a.xlsx").exists()
    (tmp_path / "b.xlsx").write_text("bestaat")
    with pytest.raises(PatchFout, match="bestaat al"):
        patch_werkmap(FIXTURE, tmp_path / "b.xlsx", {"Testruns": {"A2": "x"}})
    with pytest.raises(PatchFout, match="nooit overschreven"):
        patch_werkmap(FIXTURE, FIXTURE, {"Testruns": {"A2": "x"}})


def test_te_lange_tekst_geweigerd(tmp_path):
    with pytest.raises(PatchFout, match="past niet"):
        patch_werkmap(FIXTURE, tmp_path / "a.xlsx", {"Testruns": {"J2": "x" * 40000}})
