"""`run` op de database: selectie (alle / model / tc), ID's en herhalingen, auditspoor, fouten, hervatten."""

import json

import pytest
from sqlalchemy import select

from db.models import Model, Testcase, Testrun, nu
from db.opslag import ALLEEN_GOEDGEKEURD, laad_koppelingen, zet_instelling
from testrunner.uitvoeren import RunOpties, UitvoerFout, voer_uit

from .conftest import IE_ID, VANDAAG, FakeClient


def opties(**kw):
    basis = dict(modellen=["IE"], ja=True, alleen_goedgekeurd=False)
    basis.update(kw)
    return RunOpties(**basis)


def runs(s):
    return list(s.scalars(select(Testrun).order_by(Testrun.run_id)))


def test_run_met_twee_herhalingen(gevuld, omg):
    client = FakeClient()
    res = voer_uit(gevuld, opties(tc=["IE-KERN-001", "IE-GRENS-001"], modellen=[], herhalingen=2), client, omg)

    assert len(client.calls) == 4
    assert all(c["model"] == IE_ID and c["temperature"] is None for c in client.calls)
    assert all(len(c["messages"]) == 1 and c["messages"][0]["role"] == "user" for c in client.calls)
    r = runs(gevuld)
    assert [x.run_id for x in r] == [f"RUN-20261116-00{i}" for i in range(1, 5)]
    assert [x.tc_id for x in r] == ["IE-KERN-001", "IE-KERN-001", "IE-GRENS-001", "IE-GRENS-001"]
    assert [x.herhaling for x in r] == [1, 2, 1, 2]
    assert all(x.uitvoerwijze == "API" and x.modelversie == "v1.0" and x.model_id == "IE" for x in r)
    assert all(x.datum == VANDAAG and x.uitgevoerd_door == "API-script" for x in r)
    assert [x.run_id for x in res.geslaagd] == [x.run_id for x in r]
    # Auditspoor in de database
    assert r[0].payload["messages"][0]["content"].startswith("Wanneer is iets auteursrechtelijk")
    assert r[0].response["sources"] and r[0].api_model == IE_ID and r[0].duur_s is not None
    assert "Authorization" not in json.dumps(r[0].payload)
    # Koppeling is vastgelegd
    assert laad_koppelingen(gevuld)["IE"]["id"] == IE_ID


def test_tweede_run_gaat_verder_met_ids_en_herhalingen(gevuld, omg):
    voer_uit(gevuld, opties(tc=["IE-KERN-001"], herhalingen=2), FakeClient(), omg)
    voer_uit(gevuld, opties(tc=["IE-KERN-001"]), FakeClient(), omg)
    r = runs(gevuld)
    assert [x.run_id for x in r] == ["RUN-20261116-001", "RUN-20261116-002", "RUN-20261116-003"]
    assert [x.herhaling for x in r] == [1, 2, 3]


def test_verwijderde_run_id_wordt_niet_hergebruikt(gevuld, omg):
    voer_uit(gevuld, opties(tc=["IE-KERN-001"]), FakeClient(), omg)
    runs(gevuld)[0].verwijderd_op = nu()
    gevuld.commit()
    voer_uit(gevuld, opties(tc=["IE-KERN-001"]), FakeClient(), omg)
    r = runs(gevuld)
    assert r[1].run_id == "RUN-20261116-002" and r[1].herhaling == 1  # verwijderde run telt niet als herhaling


def test_lang_antwoord_volledig_bewaard(gevuld, omg):
    voer_uit(gevuld, opties(tc=["IE-KERN-001"]), FakeClient(lambda *a: "x" * 40000), omg)
    assert len(runs(gevuld)[0].antwoord) == 40000


# --- selectie ---------------------------------------------------------------------

def test_alle_slaat_modellen_zonder_versie_over(gevuld, omg):
    gevuld.add(Testcase(tc_id="GPT-KERN-001", model_id="GPT", testtype="KERN", prompt="Vraag", kernpunten="x"))
    gevuld.commit()
    res = voer_uit(gevuld, opties(modellen=[], alle=True, dry_run=True), FakeClient(), omg)
    assert len(res.gepland) == 24 and set(res.doelen) == {"IE"}
    assert ("model GPT", "geen Modelversie ingevuld in Modellen") in res.overgeslagen
    # Expliciet genoemd: stoppen met een fout
    with pytest.raises(UitvoerFout, match="GPT: geen Modelversie"):
        voer_uit(gevuld, opties(modellen=["GPT"], dry_run=True), FakeClient(), omg)


def test_versie_kiezen_en_controleren(gevuld, omg):
    gevuld.add(Model(model_id="IE", naam="IE Recht Guide", versie="v1.1", status="in test"))
    gevuld.add(Model(model_id="IE", naam="IE Recht Guide", versie="v2.0", status="in ontwikkeling"))
    gevuld.commit()
    res = voer_uit(gevuld, opties(dry_run=True), FakeClient(), omg)
    assert res.doelen["IE"].regel.versie == "v1.1"  # nieuwste 'in test' gaat voor
    res = voer_uit(gevuld, opties(versie="v2.0", dry_run=True), FakeClient(), omg)
    assert res.doelen["IE"].regel.versie == "v2.0"
    with pytest.raises(UitvoerFout, match="versie 'v9' bestaat niet"):
        voer_uit(gevuld, opties(versie="v9", dry_run=True), FakeClient(), omg)


def test_selectie_regressie_type_instelling_en_vervallen(gevuld, omg):
    def gepland(**kw):
        return {r.tc_id for r in voer_uit(gevuld, opties(dry_run=True, **kw), FakeClient(), omg).gepland}

    assert len(gepland(alleen_regressie=True)) == 10
    assert gepland(type=["grens"]) == {"IE-GRENS-001", "IE-GRENS-002"}
    # Instelling J (standaard) en alleen concept-cases: niets
    assert gepland(alleen_goedgekeurd=None) == set()
    zet_instelling(gevuld, ALLEEN_GOEDGEKEURD, "N")
    gevuld.commit()
    assert len(gepland(alleen_goedgekeurd=None)) == 24
    tc = gevuld.get(Testcase, "IE-KERN-001")
    tc.status = "vervallen"
    gevuld.get(Testcase, "IE-KERN-002").verwijderd_op = nu()
    gevuld.commit()
    res = voer_uit(gevuld, opties(dry_run=True), FakeClient(), omg)
    assert "IE-KERN-001" not in {r.tc_id for r in res.gepland} and ("IE-KERN-001", "vervallen") in res.overgeslagen
    assert "IE-KERN-002" not in {r.tc_id for r in res.gepland}
    with pytest.raises(UitvoerFout, match="Onbekende of verwijderde"):
        voer_uit(gevuld, opties(tc=["IE-KERN-002"], dry_run=True), FakeClient(), omg)


def test_bijlage_overgeslagen_en_context_meegestuurd(gevuld, omg):
    gevuld.get(Testcase, "IE-KERN-001").context = "contract.pdf"
    gevuld.get(Testcase, "IE-KERN-002").context = "Partijen zijn A en B."
    gevuld.commit()
    client = FakeClient()
    res = voer_uit(gevuld, opties(tc=["IE-KERN-001", "IE-KERN-002"]), client, omg)
    assert ("IE-KERN-001", "bijlage niet ondersteund") in res.overgeslagen
    assert client.calls[0]["messages"][0]["content"].endswith("\n\nContext:\nPartijen zijn A en B.")


def test_geen_selectie_is_fout(gevuld, omg):
    with pytest.raises(UitvoerFout, match="--alle, --model"):
        voer_uit(gevuld, RunOpties(), FakeClient(), omg)


# --- fouten, afbreken, hervatten, parallel -------------------------------------------

def test_mislukte_en_lege_antwoorden_niet_opgeslagen(gevuld, omg):
    def antwoord(messages, model, temperature):
        prompt = messages[0]["content"]
        if prompt.startswith("Wanneer is iets"):
            return RuntimeError("time-out")
        if prompt.startswith("We hebben zelf even gegoogeld"):
            return "   "
        return "ok"
    res = voer_uit(gevuld, opties(tc=["IE-KERN-001", "IE-GRENS-001", "IE-KERN-002"]), FakeClient(antwoord), omg)
    assert [r.tc_id for r in runs(gevuld)] == ["IE-KERN-002"]
    assert {tc for tc, _ in res.fouten} == {"IE-KERN-001", "IE-GRENS-001"}


def test_ctrl_c_bewaart_geslaagde_runs(gevuld, omg):
    def antwoord(messages, model, temperature):
        if len(client.calls) == 2:
            raise KeyboardInterrupt
        return "ok"
    client = FakeClient(antwoord)
    res = voer_uit(gevuld, opties(tc=["IE-KERN-001", "IE-KERN-002", "IE-KERN-003"]), client, omg)
    assert res.afgebroken and len(runs(gevuld)) == 1
    # Hervatten slaat de case over die vandaag al gedraaid is
    res = voer_uit(gevuld, opties(tc=["IE-KERN-001", "IE-KERN-002", "IE-KERN-003"], hervat=True), FakeClient(), omg)
    assert [r.tc_id for r in res.gepland] == ["IE-KERN-002", "IE-KERN-003"]


def test_parallel(gevuld, omg):
    res = voer_uit(gevuld, opties(type=["KERN"], parallel=3), FakeClient(), omg)
    r = runs(gevuld)
    assert len(r) == 6 and len(res.geslaagd) == 6
    assert len({x.run_id for x in r}) == 6
    with pytest.raises(UitvoerFout, match="--parallel"):
        voer_uit(gevuld, opties(parallel=9), FakeClient(), omg)


def test_bevestiging_nee_doet_niets(gevuld, omg):
    omg.invoer = lambda p: "n"
    client = FakeClient()
    voer_uit(gevuld, opties(tc=["IE-KERN-001"], ja=False), client, omg)
    assert not client.calls and not runs(gevuld)
