"""Rooktest van het beheerscherm (Streamlit AppTest, zonder browser)."""

import pytest
from sqlalchemy import select
from streamlit.testing.v1 import AppTest

from db.models import MENS, Beoordeling, Testrun
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


def start(pagina: str | None = None) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    if pagina:
        at.switch_page(f"paginas/{pagina}.py").run()
    assert not at.exception, at.exception
    return at


def opslaan(at: AppTest):
    knop = next(b for b in at.button if b.label == "Opslaan")
    knop.click().run()
    assert not at.exception, at.exception


def test_beoordelen_toont_vraag_verwachting_en_antwoord(app_db):
    at = start()
    assert [h.value for h in at.subheader][:3] == ["Vraag", "Verwacht antwoord", "Antwoord model"]
    assert any("Antwoord 1" in m.value for m in at.markdown)
    assert any(t.value.startswith("Wanneer is iets auteursrechtelijk") for t in at.text)


def test_beoordelen_slaat_scores_op_en_gaat_door(app_db):
    at = start()
    at.sidebar.text_input[0].set_value("Mr. Jurist").run()
    scores = [r for r in at.radio if r.label in ("Juistheid", "Volledigheid", "Bronnen", "Bruikbaarheid", "Gedrag")]
    for radio, waarde in zip(scores, ["3", "2", "2", "3", "n.v.t."]):
        radio.set_value(waarde)
    at.text_area[0].set_value("Goed antwoord")
    opslaan(at)
    with open_sessie(app_db) as s:
        b = s.scalar(select(Beoordeling).where(Beoordeling.type == MENS))
        assert (b.run_id, b.beoordelaar, b.eindoordeel, b.gedrag, b.toelichting) == (
            "RUN-20261116-001", "Mr. Jurist", "Voldoende", "n.v.t.", "Goed antwoord")
    assert at.session_state.run_id == "RUN-20261116-002"  # door naar de volgende onbeoordeelde run


def test_beoordelen_zonder_naam_geeft_fout(app_db):
    at = start()
    opslaan(at)
    assert any("naam" in e.value for e in at.error)
    with open_sessie(app_db) as s:
        assert s.scalar(select(Beoordeling)) is None


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
