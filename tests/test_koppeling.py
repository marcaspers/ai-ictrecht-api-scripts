"""Acceptatiecriterium 5: modelkoppeling."""

import datetime as dt
import json

import pytest

from db.models import Model
from db.opslag import bewaar_koppelingen, laad_koppelingen
from testrunner import koppeling as kp

VANDAAG = dt.date(2026, 11, 16)
MODELLEN = kp.normaliseer_modellenlijst({"models": [
    {"nr": 16, "id": "ie-recht-guide-door-ictrecht", "name": "IE Recht Guide", "available": True},
    {"nr": 13, "id": "dpia-assistent-door-ictrecht-", "name": "DPIA Assistent", "available": True},
    {"nr": 36, "id": "patintenrechten-assistent", "name": "Patiëntenrechten Assistent", "available": True},
    {"nr": 50, "id": "oud-model", "name": "Oud Model", "available": False},
]})


def regel(model_id="IE", naam="IE Recht Guide", opm="OpenWebUI-id: ie-recht-guide-door-ictrecht (nr 16)"):
    return Model(model_id=model_id, naam=naam, versie="v1.0", status="", opmerkingen=opm)


def geen_vraag(prompt):
    pytest.fail(f"onverwachte vraag: {prompt}")


def test_formaten_modellenlijst():
    assert kp.normaliseer_modellenlijst({"data": [{"id": "a", "name": " A "}]}) == \
        [{"id": "a", "name": "A", "available": True}]
    assert kp.normaliseer_modellenlijst([{"id": "a"}])[0]["id"] == "a"


@pytest.mark.parametrize("r, bron", [
    (regel(), "naam+opmerking"),
    (regel(naam="  ie recht   GUIDE "), "naam+opmerking"),
    (regel(opm=""), "naam"),
    (regel(naam="Iets anders"), "opmerking"),
])
def test_zekere_match(r, bron):
    cache = {}
    k = kp.bepaal(r, MODELLEN, cache, invoer=geen_vraag, vandaag=VANDAAG)
    assert k == {"id": "ie-recht-guide-door-ictrecht", "name": "IE Recht Guide", "bevestigd_op": "2026-11-16",
                 "bron": bron}
    assert cache["IE"] == k


def test_vreemde_ids_letterlijk():
    r = regel("DPIA", "DPIA Assistent", "OpenWebUI-id: dpia-assistent-door-ictrecht- (models.json nr 13)")
    assert kp.bepaal(r, MODELLEN, {}, invoer=geen_vraag)["id"] == "dpia-assistent-door-ictrecht-"
    r = regel("PATIENT", "Patiëntenrechten Assistent", "OpenWebUI-id: patintenrechten-assistent (nr 36)")
    assert kp.bepaal(r, MODELLEN, {}, invoer=geen_vraag)["id"] == "patintenrechten-assistent"


def test_conflict_naam_en_opmerking_vraagt():
    r = regel(opm="OpenWebUI-id: dpia-assistent-door-ictrecht-")
    vragen, uitvoer = [], []
    antwoorden = iter(["99", "1"])  # eerst ongeldig, dan nummer 1

    def invoer(prompt):
        vragen.append(prompt)
        return next(antwoorden)
    k = kp.bepaal(r, MODELLEN, {}, invoer=invoer, uitvoer=uitvoer.append, vandaag=VANDAAG)
    assert len(vragen) == 2
    assert any("opmerking naar 'dpia-assistent-door-ictrecht-'" in u for u in uitvoer)
    assert k["id"] == "ie-recht-guide-door-ictrecht" and k["bron"] == "handmatig"
    # De top-5 bevat geen niet-beschikbare modellen
    assert not any("oud-model" in u for u in uitvoer)


def test_conflict_id_typen_en_overslaan():
    r = regel(opm="OpenWebUI-id: dpia-assistent-door-ictrecht-")
    k = kp.bepaal(r, MODELLEN, {}, invoer=lambda p: "patintenrechten-assistent", uitvoer=lambda s: None)
    assert k["id"] == "patintenrechten-assistent"
    cache = {}
    assert kp.bepaal(r, MODELLEN, cache, invoer=lambda p: "", uitvoer=lambda s: None) is None
    assert cache == {}


def test_niet_beschikbaar_is_twijfel():
    r = regel("OUD", "Oud Model", "")
    with pytest.raises(kp.KoppelingFout, match="niet \\(meer\\) als beschikbaar"):
        kp.bepaal(r, MODELLEN, {}, interactief=False)


def test_niet_interactief_stopt_bij_twijfel():
    with pytest.raises(kp.KoppelingFout, match="niet zeker"):
        kp.bepaal(regel(naam="Onbekend", opm=""), MODELLEN, {}, interactief=False)


def test_cache_wordt_gebruikt_en_gecontroleerd(sessie):
    cache = {"IE": {"id": "patintenrechten-assistent", "name": "x", "bevestigd_op": "2026-11-01", "bron": "handmatig"}}
    bewaar_koppelingen(sessie, cache)
    sessie.commit()
    # Cache wint van naam/opmerking zolang het id nog bestaat
    assert kp.bepaal(regel(), MODELLEN, laad_koppelingen(sessie), invoer=geen_vraag)["id"] == "patintenrechten-assistent"
    # Verdwenen id -> opnieuw vragen
    cache = {"IE": {"id": "oud-model", "name": "x", "bevestigd_op": "2026-11-01", "bron": "handmatig"}}
    with pytest.raises(kp.KoppelingFout, match="niet \\(meer\\) in de actuele"):
        kp.bepaal(regel(), MODELLEN, cache, interactief=False)
    k = kp.bepaal(regel(), MODELLEN, cache, invoer=lambda p: "1", uitvoer=lambda s: None, vandaag=VANDAAG)
    assert k["id"] == "ie-recht-guide-door-ictrecht" and cache["IE"]["bevestigd_op"] == "2026-11-16"
    bewaar_koppelingen(sessie, cache)
    sessie.commit()
    assert laad_koppelingen(sessie)["IE"]["id"] == "ie-recht-guide-door-ictrecht"


def test_cli_koppel(tmp_path, gevuld):
    import testregister
    models = tmp_path / "m.json"
    models.write_text(json.dumps({"models": [m | {"nr": i} for i, m in enumerate(MODELLEN)]}))
    code = testregister.main(["koppel", "--model", "IE", "--models", str(models), "--niet-interactief"])
    gevuld.expire_all()
    assert code == 0 and laad_koppelingen(gevuld)["IE"]["bron"] == "naam+opmerking"
    # Een model waarvoor de koppeling twijfelachtig is stopt met een fout
    code = testregister.main(["koppel", "--model", "GPT", "--models", str(models), "--niet-interactief"])
    assert code == 2
