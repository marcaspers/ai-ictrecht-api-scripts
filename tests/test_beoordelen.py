"""Acceptatiecriterium 6 (beoordelaar) en de kalibratie, op de database."""

import csv
import json

import pytest
from sqlalchemy import select

from db.models import LLM, MENS, Beoordeling, Testrun
from testrunner.beoordelen import (BeoordeelOpties, OngeldigOordeel, beoordeel, lees_rubric, parse_oordeel,
                                   verwachtingen)
from testrunner.kalibratie import kalibreer
from testrunner.uitvoeren import RunOpties, UitvoerFout, voer_uit

from .conftest import IE_ID, FakeClient


def oordeel(scores=(3, 2, 2, 3, 3), ko="N", code=None, citaat=None, **extra):
    d = {"kernpunten": [{"nr": 1, "oordeel": "aanwezig", "toelichting": "ok"}],
         "scores": dict(zip(["Juistheid", "Volledigheid", "Bronnen", "Bruikbaarheid", "Gedrag"], scores)),
         "ko": {"ko": ko, "code": code, "citaat": citaat}, "toelichting": "Prima.", "te_verifieren": []}
    d.update(extra)
    return json.dumps(d)


@pytest.fixture
def met_runs(gevuld, omg):
    """3 runs: IE-KERN-001 2×, IE-FOUT-001 1×; antwoorden 'Antwoord n'."""
    voer_uit(gevuld, RunOpties(tc=["IE-KERN-001", "IE-FOUT-001"], ja=True, alleen_goedgekeurd=False), FakeClient(), omg)
    voer_uit(gevuld, RunOpties(tc=["IE-KERN-001"], ja=True, alleen_goedgekeurd=False), FakeClient(), omg)
    return gevuld


def opties(**kw):
    basis = dict(modellen=["IE"], beoordelaar_model="judge-model")
    basis.update(kw)
    return BeoordeelOpties(**basis)


def oordelen(s, type_=LLM, ook_verwijderd=False):
    q = select(Beoordeling).where(Beoordeling.type == type_).order_by(Beoordeling.id)
    return [b for b in s.scalars(q) if ook_verwijderd or not b.verwijderd]


# --- schema --------------------------------------------------------------------

def test_parse_geldig_ook_met_codeblok():
    assert parse_oordeel("```json\n" + oordeel() + "\n```")["scores"]["Juistheid"] == 3
    assert parse_oordeel("Hier is het oordeel: " + oordeel(scores=(3, 2, 2, 3, "n.v.t.")))["scores"]["Gedrag"] == "n.v.t."


@pytest.mark.parametrize("ruw, melding", [
    ("geen json", "geen geldige JSON"),
    (oordeel(scores=(4, 2, 2, 3, 3)), "ongeldige score"),
    (oordeel(scores=("2", 2, 2, 3, 3)), "ongeldige score"),
    (oordeel(scores=(True, 2, 2, 3, 3)), "ongeldige score"),
    (oordeel(ko="J", code=None, citaat="tekst"), "code verplicht"),
    (oordeel(ko="J", code="K.O.6", citaat="tekst"), "code verplicht"),
    (oordeel(ko="J", code="K.O.1", citaat=""), "citaat"),
    (oordeel(ko="N", code="K.O.1"), "null"),
    (oordeel(ko="ja"), "J\" of \"N"),
    (json.dumps({"scores": {}}), "velden ontbreken"),
])
def test_parse_ongeldig(ruw, melding):
    with pytest.raises(OngeldigOordeel, match=melding):
        parse_oordeel(ruw)


def test_verwachtingen_uit_rubric():
    v = verwachtingen(lees_rubric())
    assert set(v) == {"KERN", "DETAIL", "CASUS", "TAAK", "RAND", "FOUT", "BUITEN", "GRENS"}
    assert "niet meegaan in de premisse" in v["FOUT"]


# --- beoordeel ---------------------------------------------------------------------

def test_geldige_json_wordt_opgeslagen(met_runs, omg):
    client = FakeClient(lambda *a: oordeel(scores=(3, 1, 2, 3, "n.v.t."), te_verifieren=["ECLI X"]))
    res = beoordeel(met_runs, opties(), client, omg)
    assert len(res.beoordeeld) == 3 and not res.fouten
    assert all(c["model"] == "judge-model" and c["temperature"] == 0 for c in client.calls)
    systeem, gebruiker = client.calls[0]["messages"]
    assert "K.O.1" in systeem["content"] and "Antwoord uitsluitend met één JSON-object" in systeem["content"]
    assert "## Kernpunten verwacht antwoord" in gebruiker["content"] and "Antwoord 1" in gebruiker["content"]
    bs = oordelen(met_runs)
    assert len(bs) == 3
    b = bs[0]
    assert b.scores() == {"Juistheid": 3, "Volledigheid": 1, "Bronnen": 2, "Bruikbaarheid": 3, "Gedrag": "n.v.t."}
    assert b.eindoordeel == "Twijfel" and b.te_verifieren == ["ECLI X"] and b.beoordelaar == "judge-model"
    assert b.details["ruwe_antwoorden"] and b.details["berichten"][0]["role"] == "system"
    # Tweede keer: al beoordeeld
    res = beoordeel(met_runs, opties(), FakeClient(lambda *a: oordeel()), omg)
    assert not res.beoordeeld and all("al door de LLM" in r for _, r in res.overgeslagen)


def test_ongeldige_json_een_herkansing_dan_fout(met_runs, omg):
    client = FakeClient(lambda *a: "geen json")
    res = beoordeel(met_runs, opties(run=["RUN-20261116-001"], modellen=[]), client, omg)
    assert len(client.calls) == 2 and len(client.calls[1]["messages"]) == 4
    assert client.calls[1]["messages"][-1]["content"].startswith("Je antwoord is ongeldig")
    assert res.fouten and "ook na herkansing" in res.fouten[0][1]
    assert not oordelen(met_runs)
    # Herkansing lukt
    antwoorden = iter(["geen json", oordeel()])
    res = beoordeel(met_runs, opties(run=["RUN-20261116-001"], modellen=[]), FakeClient(lambda *a: next(antwoorden)), omg)
    assert len(res.beoordeeld) == 1


def test_ko_wordt_opgeslagen_met_citaatcontrole(met_runs, omg):
    client = FakeClient(lambda *a: oordeel(ko="J", code="K.O.1", citaat="niet in het antwoord"))
    beoordeel(met_runs, opties(run=["RUN-20261116-001"], modellen=[]), client, omg)
    b = oordelen(met_runs)[0]
    assert b.ko == "J" and b.ko_code == "K.O.1" and b.eindoordeel == "Onvoldoende"
    assert b.waarschuwingen == ["K.O.-citaat niet letterlijk in het antwoord gevonden"]


def test_opnieuw_alleen_met_run_en_bewaart_vorige(met_runs, omg):
    with pytest.raises(UitvoerFout, match="alleen samen met --run"):
        beoordeel(met_runs, opties(opnieuw=True), FakeClient(), omg)
    beoordeel(met_runs, opties(run=["RUN-20261116-001"], modellen=[]), FakeClient(lambda *a: oordeel()), omg)
    beoordeel(met_runs, opties(run=["RUN-20261116-001"], modellen=[], opnieuw=True),
              FakeClient(lambda *a: oordeel(scores=(0, 0, 0, 0, 0))), omg)
    alle = oordelen(met_runs, ook_verwijderd=True)
    assert len(alle) == 2 and alle[0].verwijderd and not alle[1].verwijderd
    run = met_runs.get(Testrun, "RUN-20261116-001")
    assert run.beoordeling(LLM).eindoordeel == "Onvoldoende"


def test_beoordelaarsmodel_verplicht_en_waarschuwing(met_runs, omg, monkeypatch):
    monkeypatch.delenv("ICTRECHT_JUDGE_MODEL", raising=False)
    with pytest.raises(UitvoerFout, match="Geen beoordelaarsmodel"):
        beoordeel(met_runs, opties(beoordelaar_model=None), FakeClient(), omg)
    beoordeel(met_runs, opties(beoordelaar_model=IE_ID, dry_run=True), FakeClient(), omg)
    assert any("hetzelfde als het geteste model" in r for r in omg.log)


def test_selectie_versie_en_verwijderd(met_runs, omg):
    met_runs.get(Testrun, "RUN-20261116-002").verwijderd_op = met_runs.get(Testrun, "RUN-20261116-001").aangemaakt_op
    met_runs.commit()
    res = beoordeel(met_runs, opties(dry_run=True), FakeClient(), omg)
    assert res.geselecteerd == ["RUN-20261116-001", "RUN-20261116-003"]
    assert beoordeel(met_runs, opties(versie="v9", dry_run=True), FakeClient(), omg).geselecteerd == []
    with pytest.raises(UitvoerFout, match="niet gevonden of verwijderd"):
        beoordeel(met_runs, opties(run=["RUN-20261116-002"], modellen=[]), FakeClient(), omg)


# --- kalibratie ----------------------------------------------------------------------

def mens(s, run_id, scores, ko="N", code="", **kw):
    b = Beoordeling(run_id=run_id, type=MENS, beoordelaar="Jurist", ko=ko, ko_code=code, **kw)
    b.zet_scores(dict(zip(["Juistheid", "Volledigheid", "Bronnen", "Bruikbaarheid", "Gedrag"], scores)))
    s.add(b)
    s.commit()


def test_kalibratie(met_runs, omg):
    beoordeel(met_runs, opties(), FakeClient(lambda *a: oordeel(scores=(3, 2, 2, 3, 3))), omg)
    mens(met_runs, "RUN-20261116-001", (3, 2, 2, 3, 3))                 # gelijk
    mens(met_runs, "RUN-20261116-002", (1, 2, 2, 3, 3))                 # Onvoldoende vs Voldoende
    mens(met_runs, "RUN-20261116-003", (3, 2, 2, 3, "n.v.t."), "J", "K.O.2")  # gemiste K.O.
    res = kalibreer(met_runs, ["IE"], "v1.0", omg)
    assert len(res.vergelijkingen) == 3
    assert res.pct_gelijk == pytest.approx(1 / 3)
    assert [v.run_id for v in res.gemiste_kos] == ["RUN-20261116-003"]
    d = res.per_dimensie()
    assert d["Juistheid"]["aantal_2_of_meer"] == 1 and d["Gedrag"]["nvt_verschil"] == 1
    with open(res.csv_pad, encoding="utf-8-sig") as f:
        rijen = list(csv.reader(f, delimiter=";"))
    assert len(rijen) == 4 and rijen[0][0] == "Run-ID"


def test_kalibratie_zonder_llm(met_runs, omg):
    mens(met_runs, "RUN-20261116-001", (3, 2, 2, 3, 3))
    res = kalibreer(met_runs, None, None, omg)
    assert res.zonder_llm == ["RUN-20261116-001"] and not res.vergelijkingen


def test_kalibratie_alleen_definitief_en_niet_overgenomen(met_runs, omg):
    beoordeel(met_runs, opties(), FakeClient(lambda *a: oordeel(scores=(3, 2, 2, 3, 3))), omg)
    mens(met_runs, "RUN-20261116-001", (3, 2, 2, 3, 3), werkwijze="blind")
    mens(met_runs, "RUN-20261116-002", (3, 2, 2, 3, 3), werkwijze="overgenomen")
    mens(met_runs, "RUN-20261116-003", (1, 2, 2, 3, 3), status="klad")
    res = kalibreer(met_runs, ["IE"], "v1.0", omg)
    assert [v.run_id for v in res.vergelijkingen] == ["RUN-20261116-001"]
    assert res.overgenomen == ["RUN-20261116-002"]
