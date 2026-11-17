"""Import (niets overschrijven, doornummeren) en export (sjabloon vullen, formules intact)."""

import datetime as dt

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import func, select

from db.models import Base, Beoordeling, Model, Testcase, Testrun
from db.sessie import maak_engine, open_sessie
from register.exporteren import STANDAARD_SJABLOON, ExportFout, exporteer
from register.importeren import importeer
from register.werkmap import Register

from .conftest import FIXTURE, FakeClient
from .test_beoordelen import mens
from .test_xlsxpatch import formules, tel_xml


def stil(*a):
    return None


def aantal(s, klasse):
    return s.scalar(select(func.count()).select_from(klasse))


def test_migraties_komen_overeen_met_het_datamodel(db_url):
    with maak_engine(db_url).connect() as conn:
        verschillen = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert verschillen == []


# --- import ------------------------------------------------------------------------

def test_import_en_nogmaals_importeren(sessie):
    plan = importeer(sessie, FIXTURE, ja=True, uitvoer=stil)
    assert (aantal(sessie, Model), aantal(sessie, Testcase), aantal(sessie, Testrun)) == (42, 24, 0)
    tc = sessie.get(Testcase, "IE-KERN-001")
    assert tc.regressieset == "J" and tc.status == "concept" and tc.peildatum == dt.date(2026, 11, 16)
    assert tc.import_bron == FIXTURE.name and tc.oorspronkelijk_id == "IE-KERN-001"
    assert not plan.hernummerd
    # Tweede keer: alles overgeslagen, niets dubbel
    plan = importeer(sessie, FIXTURE, ja=True, uitvoer=stil)
    assert plan.leeg and len(plan.overgeslagen) == 42 + 24
    assert aantal(sessie, Testcase) == 24


def test_bestaand_tc_id_wordt_doorgenummerd_niet_overschreven(sessie, variant):
    importeer(sessie, FIXTURE, ja=True, uitvoer=stil)
    origineel = sessie.get(Testcase, "IE-KERN-001").prompt
    pad = variant({"Testcases": {"F2": "Een andere vraag over auteursrecht",          # IE-KERN-001 gewijzigd
                                 "A30": None, "B30": "IE", "E30": "KERN", "F30": "Nieuwe vraag zonder ID",
                                 "A31": "IE-DETAIL-001", "B31": "IE", "E31": "DETAIL", "F31": "Nog een"}})
    plan = importeer(sessie, pad, ja=True, uitvoer=stil)
    assert sessie.get(Testcase, "IE-KERN-001").prompt == origineel
    assert [(o, n) for _, o, n in plan.hernummerd] == [
        ("IE-KERN-001", "IE-KERN-007"), ("(regel 30, geen TC-ID)", "IE-KERN-008"), ("IE-DETAIL-001", "IE-DETAIL-005")]
    assert sessie.get(Testcase, "IE-KERN-007").prompt == "Een andere vraag over auteursrecht"
    assert sessie.get(Testcase, "IE-KERN-007").oorspronkelijk_id == "IE-KERN-001"
    assert aantal(sessie, Testcase) == 27


def test_import_vraagt_bevestiging(sessie):
    importeer(sessie, FIXTURE, invoer=lambda p: "n", uitvoer=stil)
    assert aantal(sessie, Testcase) == 0
    importeer(sessie, FIXTURE, dry_run=True, uitvoer=stil)
    assert aantal(sessie, Testcase) == 0


def test_import_van_runs_en_scores(sessie, variant):
    importeer(sessie, FIXTURE, ja=True, uitvoer=stil)
    pad = variant({"Testruns": {
        "A2": "RUN-20261101-001", "B2": "IE-KERN-001", "E2": "v1.0", "F2": dt.date(2026, 11, 1), "G2": "Jurist",
        "H2": "handmatig", "I2": 1, "J2": "Antwoord uit de interface", "K2": 3, "L2": 2, "M2": 2, "N2": 3,
        "O2": "n.v.t.", "P2": "N", "T2": "Mr. X", "U2": "mens",
        "A3": "RUN-20261101-002", "B3": "BESTAAT-NIET", "J3": "x"}})
    plan = importeer(sessie, pad, ja=True, uitvoer=stil)
    run = sessie.get(Testrun, "RUN-20261101-001")
    assert run.uitvoerwijze == "handmatig" and run.model_id == "IE" and run.datum == dt.date(2026, 11, 1)
    b = run.beoordelingen[0]
    assert b.type == "mens" and b.beoordelaar == "Mr. X" and b.eindoordeel == "Voldoende" and b.gedrag == "n.v.t."
    assert ("testrun", "RUN-20261101-002", "TC-ID 'BESTAAT-NIET' onbekend") in plan.overgeslagen
    # Nogmaals: zelfde Run-ID en TC-ID -> overgeslagen
    assert importeer(sessie, pad, ja=True, uitvoer=stil).leeg


# --- export -------------------------------------------------------------------------

@pytest.fixture
def met_beoordelingen(gevuld, omg):
    from testrunner.uitvoeren import RunOpties, voer_uit
    lang = iter(["x" * 40000, "kort antwoord"])
    voer_uit(gevuld, RunOpties(tc=["IE-KERN-001", "IE-GRENS-001"], ja=True, alleen_goedgekeurd=False),
             FakeClient(lambda *a: next(lang)), omg)
    llm = Beoordeling(run_id="RUN-20261116-001", type="LLM", beoordelaar="judge", ko="N", toelichting="LLM",
                      te_verifieren=["ECLI Y"])
    llm.zet_scores({"Juistheid": 1, "Volledigheid": 1, "Bronnen": 1, "Bruikbaarheid": 1, "Gedrag": 1})
    gevuld.add(llm)
    gevuld.commit()
    mens(gevuld, "RUN-20261116-001", (3, 3, 2, 3, "n.v.t."))
    llm2 = Beoordeling(run_id="RUN-20261116-002", type="LLM", beoordelaar="judge", ko="N", toelichting="Alleen LLM")
    llm2.zet_scores({"Juistheid": 2, "Volledigheid": 2, "Bronnen": 2, "Bruikbaarheid": 2, "Gedrag": 2})
    gevuld.add(llm2)
    gevuld.commit()
    return gevuld


def test_export_vult_sjabloon_en_laat_structuur_intact(met_beoordelingen, tmp_path):
    uit = tmp_path / "export.xlsx"
    res = exporteer(met_beoordelingen, uit=uit, uitvoer=stil)
    assert (res.modellen, res.testcases, res.testruns) == (42, 24, 2)
    assert formules(uit) == formules(STANDAARD_SJABLOON)
    for patroon in (r"<dataValidation\b", r"<x14:dataValidation\b", r"<conditionalFormatting\b"):
        assert tel_xml(uit, patroon) == tel_xml(STANDAARD_SJABLOON, patroon)

    reg = Register.laad(uit)
    assert len(reg.testcases) == 24 and reg.testcases[0]["TC-ID"] == "IE-KERN-001"
    r1, r2 = reg.testruns
    assert r1["Run-ID"] == "RUN-20261116-001" and r1["Uitvoerwijze"] == "API" and r1["Herhaling #"] == 1
    assert r1["Antwoord model"].endswith("[… afgekapt; volledig antwoord in de database bij RUN-20261116-001]")
    # Mens gaat voor LLM; zonder mens de LLM
    assert (r1["Juistheid"], r1["Gedrag"], r1["Type beoordelaar"]) == (3, "n.v.t.", "mens")
    assert (r2["Juistheid"], r2["Type beoordelaar"], r2["Toelichting"]) == (2, "LLM", "Alleen LLM")
    assert reg.alleen_goedgekeurd == "J"


def test_export_filter_en_capaciteit(met_beoordelingen, tmp_path):
    res = exporteer(met_beoordelingen, uit=tmp_path / "ie.xlsx", modellen=["IE"], uitvoer=stil)
    assert (res.modellen, res.testcases, res.testruns) == (1, 24, 2)
    met_beoordelingen.add_all(Testcase(tc_id=f"IE-KERN-{i:03d}", model_id="IE", testtype="KERN", prompt=f"v{i}")
                              for i in range(100, 400))
    met_beoordelingen.commit()
    with pytest.raises(ExportFout, match="Testcases: 324 regels, ruimte voor 300"):
        exporteer(met_beoordelingen, uit=tmp_path / "te-veel.xlsx", uitvoer=stil)


def test_export_weigert_gevuld_sjabloon(gevuld, tmp_path):
    with pytest.raises(ExportFout, match="bevat al gegevens"):
        exporteer(gevuld, uit=tmp_path / "x.xlsx", sjabloon=FIXTURE, uitvoer=stil)


def test_export_en_weer_importeren_in_lege_database(met_beoordelingen, tmp_path, monkeypatch):
    uit = tmp_path / "rond.xlsx"
    exporteer(met_beoordelingen, uit=uit, uitvoer=stil)
    from db.sessie import init_db
    url = f"sqlite:///{tmp_path / 'leeg.db'}"
    init_db(url)
    with open_sessie(url) as s:
        plan = importeer(s, uit, ja=True, uitvoer=stil)
        assert not plan.hernummerd
        assert (aantal(s, Model), aantal(s, Testcase), aantal(s, Testrun), aantal(s, Beoordeling)) == (42, 24, 2, 2)
        assert s.get(Testrun, "RUN-20261116-002").antwoord == "kort antwoord"
        assert s.get(Testrun, "RUN-20261116-001").beoordelingen[0].scores()["Gedrag"] == "n.v.t."
