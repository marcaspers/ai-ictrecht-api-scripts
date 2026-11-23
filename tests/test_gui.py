"""Rooktest van het beheerscherm (Streamlit AppTest, zonder browser)."""

import pytest
from sqlalchemy import select
from streamlit.testing.v1 import AppTest

from db.models import LLM, MENS, Beoordeling, Testrun
from db.sessie import open_sessie
from testrunner.uitvoeren import RunOpties, voer_uit

from .conftest import ROOT, FakeClient

APP = str(ROOT / "gui" / "app.py")


@pytest.fixture
def app_db(gevuld, omg, db_url, monkeypatch):
    monkeypatch.delenv("GUI_WACHTWOORD", raising=False)
    monkeypatch.delenv("ICTRECHT_TESTER", raising=False)
    voer_uit(gevuld, RunOpties(tc=["IE-KERN-001", "IE-FOUT-001"], ja=True, alleen_goedgekeurd=False), FakeClient(), omg)
    return db_url


def start(pagina: str | None = None, **state) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=30)
    for k, v in state.items():
        at.session_state[k] = v
    at.run()
    if pagina:
        at.switch_page(f"paginas/{pagina}.py").run()
    assert not at.exception, at.exception
    return at


DIMENSIES = ("Juistheid", "Volledigheid", "Bronnen", "Bruikbaarheid", "Gedrag")


def scores(at: AppTest):
    return [r for r in at.radio if r.label in DIMENSIES]


def llm_oordeel(db_url, run_id="RUN-20261116-001", waarden=("2", "1", "3", "2", "3"), eind="Twijfel"):
    with open_sessie(db_url) as s:
        b = Beoordeling(run_id=run_id, type=LLM, beoordelaar="judge-model", ko="N", eindoordeel=eind,
                        toelichting="Volledigheid 1: kernpunt 2 ontbreekt", te_verifieren=["ECLI:NL:HR:2008:BC2153"])
        b.zet_scores(dict(zip(DIMENSIES, waarden)))
        s.add(b)
        s.commit()


def opslaan(at: AppTest, label="Definitief maken"):
    knop = next(b for b in at.button if b.label == label)
    knop.click().run()
    assert not at.exception, at.exception


def test_beoordelen_toont_vraag_verwachting_en_antwoord(app_db):
    at = start()
    koppen = [h.value for h in at.subheader][:3]
    assert koppen[:2] == ["Vraag", "Verwacht antwoord"]
    assert koppen[2].startswith("Antwoord ") and koppen[2].endswith(" v1.0")
    assert any("Antwoord 1" in m.value for m in at.markdown)
    assert any(t.value.startswith("Wanneer is iets auteursrechtelijk") for t in at.text)


def test_beoordelen_slaat_scores_op_en_gaat_door(app_db):
    at = start()
    at.sidebar.text_input[0].set_value("Mr. Jurist").run()
    for radio, waarde in zip(scores(at), ["3", "2", "2", "3", "n.v.t."]):
        radio.set_value(waarde)
    at.text_area[0].set_value("Goed antwoord")
    opslaan(at)
    with open_sessie(app_db) as s:
        b = s.scalar(select(Beoordeling).where(Beoordeling.type == MENS))
        assert (b.run_id, b.beoordelaar, b.eindoordeel, b.gedrag, b.toelichting, b.status, b.werkwijze) == (
            "RUN-20261116-001", "Mr. Jurist", "Voldoende", "n.v.t.", "Goed antwoord", "definitief", "blind")
    assert at.session_state.run_id == "RUN-20261116-002"  # door naar de volgende onbeoordeelde run


def test_beoordelen_zonder_naam_geeft_fout(app_db):
    at = start()
    opslaan(at)
    assert any("naam" in e.value for e in at.error)
    with open_sessie(app_db) as s:
        assert s.scalar(select(Beoordeling)) is None


def test_review_start_met_llm_scores_als_klad(app_db):
    llm_oordeel(app_db)
    at = start()
    assert [r.value for r in scores(at)] == ["2", "1", "3", "2", "3"]
    assert any("Twijfel" in m.value for m in at.markdown)  # eindoordeel rekent mee
    at.sidebar.text_input[0].set_value("Mr. Jurist").run()
    scores(at)[1].set_value("2")
    opslaan(at, "Opslaan als klad")
    with open_sessie(app_db) as s:
        b = s.scalar(select(Beoordeling).where(Beoordeling.type == MENS))
        assert (b.status, b.werkwijze, b.volledigheid, b.eindoordeel) == ("klad", "overgenomen", "2", "Voldoende")
    assert at.session_state.run_id == "RUN-20261116-001"  # klad: blijft in de werklijst staan
    opslaan(at)  # definitief maken
    with open_sessie(app_db) as s:
        actief = [b for b in s.scalars(select(Beoordeling).where(Beoordeling.type == MENS)) if not b.verwijderd]
        assert [(b.status, b.werkwijze) for b in actief] == [("definitief", "overgenomen")]


def test_blind_verbergt_llm_tot_definitief(app_db):
    llm_oordeel(app_db)
    at = start(gebruiker="Mr. Jurist", blind=True)
    assert at.sidebar.toggle[0].value
    assert [r.value for r in scores(at)] == [None] * 5
    assert not any("kernpunt 2 ontbreekt" in t.value for t in at.text)
    opslaan(at)
    assert any("alle vijf scores" in e.value for e in at.error)
    for radio, waarde in zip(scores(at), ["3", "2", "2", "3", "3"]):
        radio.set_value(waarde)
    opslaan(at)
    with open_sessie(app_db) as s:
        b = s.scalar(select(Beoordeling).where(Beoordeling.type == MENS))
        assert (b.status, b.werkwijze) == ("definitief", "blind")
    assert any("LLM gaf: Twijfel" in t.value for t in at.toast)


def test_blind_na_zien_telt_niet_als_blind(app_db):
    llm_oordeel(app_db)
    at = start(gebruiker="Mr. Jurist")          # review: LLM-scores gezien
    at.sidebar.toggle[0].set_value(True).run()  # pas daarna blind
    assert any("al gezien" in w.value for w in at.warning)
    for radio, waarde in zip(scores(at), ["3", "2", "2", "3", "3"]):
        radio.set_value(waarde)
    opslaan(at)
    with open_sessie(app_db) as s:
        assert s.scalar(select(Beoordeling).where(Beoordeling.type == MENS)).werkwijze == "overgenomen"


def test_werkwijze():
    from gui.paginas import bepaal_werkwijze
    velden = {"juistheid": "2", "volledigheid": "1", "bronnen": "3", "bruikbaarheid": "2", "gedrag": "3"}
    vorige = Beoordeling(type=MENS, werkwijze="blind", ko="N", ko_code="", **velden)
    assert bepaal_werkwijze(None, False, velden, "N", "") == "blind"
    assert bepaal_werkwijze(None, True, velden, "N", "") == "overgenomen"
    assert bepaal_werkwijze(vorige, True, velden, "N", "") == "blind"  # ongewijzigd: blijft blind
    assert bepaal_werkwijze(vorige, True, {**velden, "gedrag": "2"}, "N", "") == "overgenomen"
    vorige.werkwijze = "overgenomen"
    assert bepaal_werkwijze(vorige, False, {**velden, "gedrag": "2"}, "N", "") == "overgenomen"


def test_testruns_status(app_db):
    llm_oordeel(app_db)
    from gui.paginas import ST_LLM, ST_NIET, kort, status_run
    with open_sessie(app_db) as s:
        r1, r2 = s.scalars(select(Testrun).order_by(Testrun.run_id))
        assert (status_run(r1), status_run(r2)) == (ST_LLM, ST_NIET)
        assert kort(r1.beoordeling(LLM)) == "J2 V1 B3 U2 G3"


@pytest.mark.parametrize("pagina", ["testcases", "testruns", "modellen", "overzicht"])
def test_paginas_laden(app_db, pagina):
    start(pagina)


def test_tabel_wijzigingen_en_soft_delete(app_db):
    """De opslaglogica achter de tabellen (AppTest kan de tabel-editor zelf niet bedienen)."""
    import pandas as pd

    from gui.paginas import bewaar_wijzigingen
    with open_sessie(app_db) as s:
        runs = {r.run_id: r for r in s.scalars(select(Testrun))}
        voor = pd.DataFrame([{"Verwijderd": False, "Run-ID": k, "Uitgevoerd door": r.uitgevoerd_door}
                             for k, r in runs.items()])
        na = voor.copy()
        na.loc[0, "Verwijderd"] = True
        na.loc[1, "Uitgevoerd door"] = "Iemand anders"
        assert bewaar_wijzigingen(s, runs, voor, na, "Run-ID", {"Uitgevoerd door": "uitgevoerd_door"}) == 2
        s.commit()
        assert runs["RUN-20261116-001"].verwijderd_op is not None
        assert runs["RUN-20261116-002"].uitgevoerd_door == "Iemand anders"
        # Terughalen
        assert bewaar_wijzigingen(s, runs, na, voor, "Run-ID", {"Uitgevoerd door": "uitgevoerd_door"}) == 2
        assert runs["RUN-20261116-001"].verwijderd_op is None


def test_wachtwoord(app_db, monkeypatch):
    monkeypatch.setenv("GUI_WACHTWOORD", "geheim")
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.subheader  # nog niets te zien
    at.text_input[0].set_value("fout").run()
    assert at.error
    at.text_input[0].set_value("geheim").run()
    assert not at.exception and at.session_state.ingelogd


def test_overzicht_tokens_per_dag(app_db):
    llm_oordeel(app_db)
    at = start("overzicht")
    assert "Tokens per dag" in [h.value for h in at.subheader]
    df = at.dataframe[1].value
    assert df["API-runs"].sum() == 2 and df["Totaal"].sum() == 240  # 2 runs × 120; oud LLM-oordeel zonder tokens


def test_testruns_excel_export(app_db):
    import io

    from openpyxl import load_workbook

    from gui.paginas import testruns_excel_uit_db
    llm_oordeel(app_db)
    with open_sessie(app_db) as s:
        runs = list(s.scalars(select(Testrun).order_by(Testrun.run_id)))
    data = testruns_excel_uit_db([runs[0].run_id], {"Model": "IE", "Status": "(alle)"}, "Tester", app_db)
    wb = load_workbook(io.BytesIO(data))
    ws = wb["Testruns"]
    koppen = [c.value for c in ws[1]]
    rijen = [dict(zip(koppen, (c.value for c in rij))) for rij in ws.iter_rows(min_row=2)]
    assert [r["Run-ID"] for r in rijen] == ["RUN-20261116-001"]  # alleen de meegegeven (gefilterde) runs
    assert rijen[0]["Juistheid LLM"] == 2 and rijen[0]["Eindoordeel LLM"] == "Twijfel"
    assert rijen[0]["Juistheid jurist"] is None
    assert rijen[0]["Antwoord model"] == runs[0].antwoord  # volledig, niet ingekort
    filters = {r[0].value: r[1].value for r in wb["Filters"].iter_rows(min_row=2)}
    assert filters["Model"] == "IE" and filters["Aantal runs"] == 1 and filters["Geëxporteerd door"] == "Tester"
