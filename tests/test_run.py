"""Acceptatiecriteria 1–4 en 8 voor `run`, plus afbreken, hervatten, parallel en fouten."""

import datetime as dt
import hashlib
import json
import warnings

import pytest
from openpyxl import load_workbook

from register.werkmap import Register, tekst
from testrunner.uitvoeren import RunOpties, UitvoerFout, voer_uit

from .conftest import IE_ID, VANDAAG, FakeClient
from .test_xlsxpatch import formules


def laad(pad):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return load_workbook(pad)


def sha(pad):
    return hashlib.sha256(pad.read_bytes()).hexdigest()


def opties(**kw):
    basis = dict(model="IE", versie="v1.0", ja=True, alleen_goedgekeurd=False)
    basis.update(kw)
    return RunOpties(**basis)


def runs(pad):
    return Register.laad(pad).testruns


# --- criterium 1 en 2 ------------------------------------------------------

def test_run_met_twee_herhalingen(register, omg):
    hash_voor = sha(register)
    client = FakeClient()
    res = voer_uit(register, opties(tc=["IE-KERN-001", "IE-GRENS-001"], herhalingen=2), client, omg)

    assert sha(register) == hash_voor, "invoerbestand is gewijzigd"
    assert res.pad == register.parent / "Testregister_AI-modellen_ICTRecht_20261116.xlsx"
    assert len(client.calls) == 4
    assert all(c["model"] == IE_ID and c["temperature"] is None for c in client.calls)
    assert all(len(c["messages"]) == 1 and c["messages"][0]["role"] == "user" for c in client.calls)

    nieuw = runs(res.pad)
    assert [r["rij"] for r in nieuw] == [2, 3, 4, 5]
    assert [r["Run-ID"] for r in nieuw] == [f"RUN-20261116-00{i}" for i in range(1, 5)]
    assert [r["TC-ID"] for r in nieuw] == ["IE-KERN-001", "IE-KERN-001", "IE-GRENS-001", "IE-GRENS-001"]
    assert [r["Herhaling #"] for r in nieuw] == [1, 2, 1, 2]
    assert all(r["Uitvoerwijze"] == "API" and r["Modelversie"] == "v1.0" for r in nieuw)
    assert all(r["Datum"] == dt.datetime(2026, 11, 16) for r in nieuw)
    assert all(r["Uitgevoerd door"] == "API-script" for r in nieuw)
    assert all(r[k] is None for r in nieuw for k in ("Juistheid", "K.O.", "Toelichting", "Type beoordelaar"))
    assert formules(res.pad) == formules(register)
    assert laad(res.pad).calculation.fullCalcOnLoad is True

    # Auditspoor
    audit = json.loads((omg.output_dir / "runs" / "RUN-20261116-001.json").read_text())
    assert audit["tc_id"] == "IE-KERN-001" and audit["api_model"] == IE_ID and audit["modelversie"] == "v1.0"
    assert audit["payload"]["messages"][0]["content"].startswith("Wanneer is iets auteursrechtelijk")
    assert audit["response"]["sources"]
    assert "Authorization" not in json.dumps(audit)


def test_tweede_run_gaat_verder_met_ids_en_herhalingen(register, omg):
    eerste = voer_uit(register, opties(tc=["IE-KERN-001"], herhalingen=2), FakeClient(), omg).pad
    tweede = voer_uit(eerste, opties(tc=["IE-KERN-001"]), FakeClient(), omg).pad
    assert tweede.name == "Testregister_AI-modellen_ICTRecht_20261116_2.xlsx"
    r = runs(tweede)
    assert [x["Run-ID"] for x in r] == ["RUN-20261116-001", "RUN-20261116-002", "RUN-20261116-003"]
    assert [x["Herhaling #"] for x in r] == [1, 2, 3]


def test_run_ids_houden_rekening_met_auditbestanden(register, omg):
    """Twee runs vanaf hetzelfde invoerbestand mogen geen Run-ID hergebruiken."""
    voer_uit(register, opties(tc=["IE-KERN-001"]), FakeClient(), omg)
    res = voer_uit(register, opties(tc=["IE-KERN-001"]), FakeClient(), omg)
    assert runs(res.pad)[0]["Run-ID"] == "RUN-20261116-002"


# --- criterium 3: selectie ----------------------------------------------------

def tc_ids(res):
    return sorted({r.tc_id for r in res.gepland})


def test_selectie_regressie_en_type(register, omg):
    reg = Register.laad(register)
    regressie = sorted(tekst(t["TC-ID"]) for t in reg.testcases if t["Regressieset"] == "J")
    res = voer_uit(register, opties(alleen_regressie=True, dry_run=True), FakeClient(), omg)
    assert tc_ids(res) == regressie and len(regressie) == 10
    res = voer_uit(register, opties(type=["kern", "GRENS"], dry_run=True), FakeClient(), omg)
    assert tc_ids(res) == sorted([f"IE-KERN-00{i}" for i in range(1, 7)] + ["IE-GRENS-001", "IE-GRENS-002"])


def test_selectie_volgt_overzicht_b7_en_slaat_vervallen_over(variant, omg):
    # Rij 2 = IE-KERN-001 goedgekeurd, rij 3 = IE-KERN-002 vervallen, rest concept.
    status = {"O2": "goedgekeurd", "O3": "vervallen"}
    met_j = variant({"Testcases": status})
    met_n = variant({"Testcases": status, "Overzicht": {"B7": "N"}})

    res = voer_uit(met_j, opties(alleen_goedgekeurd=None, dry_run=True), FakeClient(), omg)
    assert tc_ids(res) == ["IE-KERN-001"]
    res = voer_uit(met_n, opties(alleen_goedgekeurd=None, dry_run=True), FakeClient(), omg)
    assert len(tc_ids(res)) == 23 and "IE-KERN-002" not in tc_ids(res)
    assert ("IE-KERN-002", "vervallen") in res.overgeslagen
    # Opties overschrijven B7
    assert tc_ids(voer_uit(met_n, opties(alleen_goedgekeurd=True, dry_run=True), FakeClient(), omg)) == ["IE-KERN-001"]
    assert len(tc_ids(voer_uit(met_j, opties(alleen_goedgekeurd=False, dry_run=True), FakeClient(), omg))) == 23


def test_bijlage_wordt_overgeslagen_en_tekstcontext_toegevoegd(variant, omg):
    pad = variant({"Testcases": {"G2": "contract.pdf", "G3": "Artikel 5: de opdrachtnemer behoudt alle rechten."}})
    client = FakeClient()
    res = voer_uit(pad, opties(tc=["IE-KERN-001", "IE-KERN-002"]), client, omg)
    assert ("IE-KERN-001", "bijlage niet ondersteund") in res.overgeslagen
    assert len(client.calls) == 1
    bericht = client.calls[0]["messages"][0]["content"]
    assert bericht.endswith("\n\nContext:\nArtikel 5: de opdrachtnemer behoudt alle rechten.")


def test_dry_run_doet_niets(register, omg):
    client = FakeClient()
    res = voer_uit(register, opties(dry_run=True, herhalingen=2), client, omg)
    assert len(res.gepland) == 48 and not client.calls and res.pad is None
    assert list(register.parent.iterdir()) == [register]


def test_onbekende_versie_stopt(register, omg):
    with pytest.raises(UitvoerFout, match="v9"):
        voer_uit(register, opties(versie="v9"), FakeClient(), omg)


def test_model_niet_live_beschikbaar_stopt(register, omg):
    omg.invoer = lambda prompt: ""  # bij de vraag overslaan
    with pytest.raises(UitvoerFout, match="Geen OpenWebUI-model"):
        voer_uit(register, opties(tc=["IE-KERN-001"]), FakeClient(modellen=[{"id": "gpt-5", "name": "ChatGPT"}]), omg)


# --- criterium 4: afkappen --------------------------------------------------

def test_lang_antwoord_wordt_afgekapt(register, omg):
    lang = "a" * 40000
    res = voer_uit(register, opties(tc=["IE-KERN-001"]), FakeClient(lambda *a: lang), omg)
    antwoord = runs(res.pad)[0]["Antwoord model"]
    assert antwoord.startswith("a" * 32000) and not antwoord.startswith("a" * 32001)
    assert antwoord.endswith("[… afgekapt; volledig antwoord in output/runs/RUN-20261116-001.json]")
    audit = json.loads((omg.output_dir / "runs" / "RUN-20261116-001.json").read_text())
    assert audit["response"]["choices"][0]["message"]["content"] == lang


# --- criterium 8: capaciteit -------------------------------------------------

def test_capaciteitscheck_stopt_voor_eerste_call(variant, omg):
    vol = variant({"Testruns": {f"A{r}": f"OUD-{r}" for r in range(2, 999)}})  # 3 regels vrij
    client = FakeClient()
    with pytest.raises(UitvoerFout, match="Niet genoeg vrije regels"):
        voer_uit(vol, opties(tc=["IE-KERN-001", "IE-KERN-002"], herhalingen=2), client, omg)
    assert not client.calls
    res = voer_uit(vol, opties(tc=["IE-KERN-001"], herhalingen=3), client, omg)
    assert [r["rij"] for r in runs(res.pad) if r["TC-ID"]] == [999, 1000, 1001]


# --- fouten, afbreken, hervatten, parallel ----------------------------------

def test_mislukte_en_lege_antwoorden_worden_niet_geschreven(register, omg):
    antwoorden = iter([RuntimeError("HTTP 500"), "", "goed antwoord"])
    res = voer_uit(register, opties(tc=["IE-KERN-001"], herhalingen=3), FakeClient(lambda *a: next(antwoorden)), omg)
    assert len(res.fouten) == 2
    r = runs(res.pad)
    assert len(r) == 1 and r[0]["Antwoord model"] == "goed antwoord" and r[0]["Herhaling #"] == 3


def test_afbreken_schrijft_geslaagde_runs_weg(register, omg):
    antwoorden = iter(["een", "twee", KeyboardInterrupt()])
    res = voer_uit(register, opties(tc=["IE-KERN-001", "IE-KERN-002"], herhalingen=2),
                   FakeClient(lambda *a: next(antwoorden)), omg)
    assert res.afgebroken
    assert [r["Antwoord model"] for r in runs(res.pad)] == ["een", "twee"]


def test_hervat_slaat_vandaag_gedraaide_cases_over(register, omg):
    eerste = voer_uit(register, opties(tc=["IE-KERN-001"]), FakeClient(), omg).pad
    client = FakeClient()
    res = voer_uit(eerste, opties(tc=["IE-KERN-001", "IE-KERN-002"], hervat=True), client, omg)
    assert len(client.calls) == 1
    assert [r["TC-ID"] for r in runs(res.pad)] == ["IE-KERN-001", "IE-KERN-002"]


def test_parallel(register, omg):
    client = FakeClient(lambda messages, *a: messages[0]["content"][:20])
    res = voer_uit(register, opties(type=["KERN"], parallel=3), client, omg)
    r = runs(res.pad)
    assert len(r) == 6 and [x["rij"] for x in r] == list(range(2, 8))
    assert [x["Run-ID"] for x in r] == [f"RUN-20261116-00{i}" for i in range(1, 7)]
    with pytest.raises(UitvoerFout, match="parallel"):
        voer_uit(register, opties(parallel=5), client, omg)


def test_bevestiging_nee_doet_niets(register, omg):
    omg.invoer = lambda prompt: "n"
    client = FakeClient()
    res = voer_uit(register, opties(tc=["IE-KERN-001"], ja=False), client, omg)
    assert not client.calls and res.pad is None


def test_uitvoerder_uit_env(register, omg, monkeypatch):
    monkeypatch.setenv("ICTRECHT_TESTER", "Marco")
    res = voer_uit(register, opties(tc=["IE-KERN-001"]), FakeClient(), omg)
    assert runs(res.pad)[0]["Uitgevoerd door"] == "Marco"
    res = voer_uit(register, opties(tc=["IE-KERN-001"], uitvoerder="Jurist X", uit=str(register.parent / "x.xlsx")),
                   FakeClient(), omg)
    assert res.pad.name == "x.xlsx" and runs(res.pad)[0]["Uitgevoerd door"] == "Jurist X"
