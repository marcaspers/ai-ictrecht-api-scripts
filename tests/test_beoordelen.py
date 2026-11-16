"""Acceptatiecriterium 6 (beoordelaar) en de kalibratie."""

import csv
import json

import pytest

from register.werkmap import Register
from register.xlsxpatch import patch_werkmap
from testrunner import koppeling as kp
from testrunner.beoordelen import (BeoordeelOpties, OngeldigOordeel, beoordeel, lees_rubric, parse_oordeel,
                                   verwachtingen)
from testrunner.kalibratie import kalibreer
from testrunner.uitvoeren import RunOpties, UitvoerFout, voer_uit

from .conftest import IE_ID, FakeClient
from .test_run import sha
from .test_xlsxpatch import formules


def oordeel(scores=(3, 2, 2, 3, 3), ko="N", code=None, citaat=None, **extra):
    d = {"kernpunten": [{"nr": 1, "oordeel": "aanwezig", "toelichting": "ok"}],
         "scores": dict(zip(["Juistheid", "Volledigheid", "Bronnen", "Bruikbaarheid", "Gedrag"], scores)),
         "ko": {"ko": ko, "code": code, "citaat": citaat}, "toelichting": "Prima.", "te_verifieren": []}
    d.update(extra)
    return json.dumps(d)


@pytest.fixture
def met_runs(register, omg):
    """Register met 3 runs (IE-KERN-001 2×, IE-FOUT-001 1×), antwoorden 'Antwoord n'."""
    res = voer_uit(register, RunOpties(model="IE", versie="v1.0", tc=["IE-KERN-001", "IE-FOUT-001"],
                                       herhalingen=1, ja=True, alleen_goedgekeurd=False), FakeClient(), omg)
    return voer_uit(res.pad, RunOpties(model="IE", versie="v1.0", tc=["IE-KERN-001"], ja=True,
                                       alleen_goedgekeurd=False), FakeClient(), omg).pad


def opties(**kw):
    basis = dict(model="IE", versie="v1.0", beoordelaar_model="judge-model")
    basis.update(kw)
    return BeoordeelOpties(**basis)


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

def test_geldige_json_wordt_verwerkt_register_blijft_ongewijzigd(met_runs, omg):
    hash_voor = sha(met_runs)
    bestanden_voor = sorted(met_runs.parent.iterdir())
    client = FakeClient(lambda *a: oordeel(scores=(3, 1, 2, 3, "n.v.t."), te_verifieren=["ECLI X"]))
    res = beoordeel(met_runs, opties(), client, omg)

    assert len(res.beoordeeld) == 3 and not res.fouten and res.pad is None
    assert sha(met_runs) == hash_voor and sorted(met_runs.parent.iterdir()) == bestanden_voor
    call = client.calls[0]
    assert call["model"] == "judge-model" and call["temperature"] == 0
    systeem, gebruiker = call["messages"][0]["content"], call["messages"][1]["content"]
    assert "K.O.5" in systeem and "te_verifieren" in systeem
    assert "## Kernpunten verwacht antwoord\n1. Bescherming ontstaat vormvrij" in gebruiker
    assert "KERN — Invoer: Gangbare vraag" in gebruiker
    assert "<<<\nAntwoord 1\n>>>" in gebruiker
    assert "Peildatum\n16-11-2026" in gebruiker
    data = json.loads((omg.output_dir / "beoordelingen" / "RUN-20261116-001.json").read_text())
    assert data["eindoordeel"] == "Twijfel" and data["beoordelaar_model"] == "judge-model"
    assert data["ruwe_antwoorden"] and data["antwoord_bron"].endswith("RUN-20261116-001.json")

    # Tweede keer: alles al beoordeeld
    res = beoordeel(met_runs, opties(), FakeClient(), omg)
    assert not res.geselecteerd and len(res.overgeslagen) == 3


def test_ongeldige_json_een_herkansing_dan_fout(met_runs, omg):
    client = FakeClient(lambda *a: "Ik vind het een prima antwoord.")
    res = beoordeel(met_runs, opties(run=["RUN-20261116-001"]), client, omg)
    assert len(client.calls) == 2 and len(res.fouten) == 1 and "herkansing" in res.fouten[0][1]
    assert client.calls[1]["messages"][-1]["role"] == "user" and "ongeldig" in client.calls[1]["messages"][-1]["content"]
    assert not (omg.output_dir / "beoordelingen" / "RUN-20261116-001.json").exists()

    antwoorden = iter([oordeel(ko="J", citaat="x"), oordeel()])  # K.O. zonder code -> afgekeurd, herkansing goed
    client = FakeClient(lambda *a: next(antwoorden))
    res = beoordeel(met_runs, opties(run=["RUN-20261116-001"]), client, omg)
    assert len(client.calls) == 2 and len(res.beoordeeld) == 1


def test_schrijf_in_register(met_runs, omg):
    antwoorden = iter([oordeel(scores=(3, 3, 2, 3, "n.v.t.")),
                       oordeel(scores=(2, 2, 2, 2, 2), ko="J", code="K.O.2", citaat="Antwoord 2",
                               te_verifieren=["art. 1 Aw"]),
                       oordeel(scores=(1, 3, 3, 3, 3))])
    res = beoordeel(met_runs, opties(schrijf_in_register=True), FakeClient(lambda *a: next(antwoorden)), omg)
    assert res.pad and res.pad != met_runs
    runs = {r["Run-ID"]: r for r in Register.laad(res.pad).testruns}
    r1, r2 = runs["RUN-20261116-001"], runs["RUN-20261116-002"]
    assert [r1[k] for k in ("Juistheid", "Volledigheid", "Bronnen", "Bruikbaarheid", "Gedrag", "K.O.")] == \
        [3, 3, 2, 3, "n.v.t.", "N"]
    assert r1["K.O.-code"] is None and r1["Type beoordelaar"] == "LLM" and r1["Beoordelaar"] == "judge-model"
    assert r2["K.O."] == "J" and r2["K.O.-code"] == "K.O.2"
    assert r2["Toelichting"] == "Prima.\nte verifiëren: art. 1 Aw"
    assert formules(res.pad) == formules(met_runs)
    eind = {b["run"]["Run-ID"]: b["eindoordeel"] for b in res.beoordeeld}
    assert eind == {"RUN-20261116-001": "Voldoende", "RUN-20261116-002": "Onvoldoende",
                    "RUN-20261116-003": "Onvoldoende"}


def test_schrijft_niet_over_bestaande_scores(met_runs, omg, tmp_path):
    gescoord = patch_werkmap(met_runs, tmp_path / "gescoord.xlsx", {"Testruns": {
        "K2": 1, "L2": 1, "M2": 1, "N2": 1, "O2": 1, "P2": "N", "U2": "mens"}})
    # Zonder --run: de gescoorde run wordt overgeslagen
    res = beoordeel(gescoord, opties(dry_run=True), FakeClient(), omg)
    assert ("RUN-20261116-001", "al gescoord") in res.overgeslagen
    # Met --run wel beoordeeld (voor kalibratie), maar niet in het register gezet
    res = beoordeel(gescoord, opties(run=["RUN-20261116-001"], schrijf_in_register=True),
                    FakeClient(lambda *a: oordeel()), omg)
    assert len(res.beoordeeld) == 1 and res.pad is None


def test_opnieuw_alleen_met_run_en_bewaart_vorige(met_runs, omg):
    with pytest.raises(UitvoerFout, match="--opnieuw"):
        beoordeel(met_runs, opties(opnieuw=True), FakeClient(), omg)
    beoordeel(met_runs, opties(run=["RUN-20261116-001"]), FakeClient(lambda *a: oordeel()), omg)
    beoordeel(met_runs, opties(run=["RUN-20261116-001"], opnieuw=True), FakeClient(lambda *a: oordeel()), omg)
    namen = sorted(p.name for p in (omg.output_dir / "beoordelingen").iterdir())
    assert len(namen) == 2 and "RUN-20261116-001.json" in namen


def test_beoordelaarsmodel_verplicht_en_waarschuwing(met_runs, omg, monkeypatch):
    monkeypatch.delenv("ICTRECHT_JUDGE_MODEL", raising=False)
    with pytest.raises(UitvoerFout, match="beoordelaarsmodel"):
        beoordeel(met_runs, opties(beoordelaar_model=None), FakeClient(), omg)
    monkeypatch.setenv("ICTRECHT_JUDGE_MODEL", IE_ID)
    kp.bewaar_cache(omg.koppeling_pad, {"IE": {"id": IE_ID}})
    beoordeel(met_runs, opties(beoordelaar_model=None, dry_run=True), FakeClient(), omg)
    assert any("hetzelfde als het geteste model" in r for r in omg.log)


def test_volledig_antwoord_uit_auditbestand(register, omg):
    lang = "b" * 40000
    pad = voer_uit(register, RunOpties(model="IE", versie="v1.0", tc=["IE-KERN-001"], ja=True,
                                       alleen_goedgekeurd=False), FakeClient(lambda *a: lang), omg).pad
    client = FakeClient(lambda *a: oordeel())
    beoordeel(pad, opties(), client, omg)
    assert lang in client.calls[0]["messages"][1]["content"]


# --- kalibratie ------------------------------------------------------------------

def test_kalibratie(met_runs, omg, tmp_path):
    # Jurist scoort de drie runs in het register (regels 2-4)
    mens = {
        2: [3, 3, 3, 3, 3, "N"],          # Voldoende
        3: [2, 2, 1, 2, 2, "J"],          # Onvoldoende (K.O.)
        4: [3, 2, 2, 2, "n.v.t.", "N"],   # Voldoende
    }
    cellen = {}
    for rij, waarden in mens.items():
        for kol, w in zip("KLMNOP", waarden):
            cellen[f"{kol}{rij}"] = w
        cellen[f"U{rij}"] = "mens"
    gescoord = patch_werkmap(met_runs, tmp_path / "gescoord.xlsx", {"Testruns": cellen})

    llm = iter([oordeel(scores=(3, 3, 3, 3, 3)),             # gelijk
                oordeel(scores=(3, 3, 3, 3, 3)),             # gemiste K.O., Voldoende i.p.v. Onvoldoende
                oordeel(scores=(3, 2, 0, 2, 2))])            # Bronnen 2 punten lager, n.v.t. ongelijk
    beoordeel(gescoord, opties(run=["RUN-20261116-001", "RUN-20261116-002", "RUN-20261116-003"]),
              FakeClient(lambda *a: next(llm)), omg)

    res = kalibreer(gescoord, "IE", "v1.0", omg)
    assert len(res.vergelijkingen) == 3
    assert res.pct_gelijk == pytest.approx(1 / 3)
    assert [v.run_id for v in res.gemiste_kos] == ["RUN-20261116-002"]
    dim = res.per_dimensie()
    assert dim["Bronnen"]["aantal_2_of_meer"] == 2 and dim["Bronnen"]["gem_verschil"] == pytest.approx(0)
    assert dim["Gedrag"]["nvt_verschil"] == 1 and dim["Gedrag"]["n"] == 2
    with open(res.csv_pad, encoding="utf-8-sig") as f:
        rijen = list(csv.reader(f, delimiter=";"))
    assert rijen[0][:5] == ["Run-ID", "TC-ID", "Testtype", "Eindoordeel mens", "Eindoordeel LLM"]
    assert len(rijen) == 4
    assert any("Gelijk eindoordeel: 33%" in r for r in omg.log)
