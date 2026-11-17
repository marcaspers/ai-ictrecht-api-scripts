"""Gedeelde databasefuncties: ID's, instellingen, modelversies en de modelkoppeling."""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Koppeling, Model, Testcase, Testrun

ALLEEN_GOEDGEKEURD = "alleen_goedgekeurd"   # J/N, was Overzicht!B7
STANDAARD_INSTELLINGEN = {ALLEEN_GOEDGEKEURD: "J"}

_RUN_ID_RE = re.compile(r"^RUN-(\d{8})-(\d+)$")
TC_ID_RE = re.compile(r"^(?P<model>.+)-(?P<type>[A-Z]+)-(?P<nr>\d+)$")


# --- instellingen ------------------------------------------------------------

def instelling(sessie: Session, sleutel: str) -> str:
    from .models import Instelling
    rij = sessie.get(Instelling, sleutel)
    return rij.waarde if rij else STANDAARD_INSTELLINGEN.get(sleutel, "")


def zet_instelling(sessie: Session, sleutel: str, waarde: str) -> None:
    from .models import Instelling
    rij = sessie.get(Instelling, sleutel)
    if rij:
        rij.waarde = waarde
    else:
        sessie.add(Instelling(sleutel=sleutel, waarde=waarde))


# --- ID's (nooit hergebruiken: ook soft-deleted rijen tellen mee) ------------

def volgende_run_id(sessie: Session, datum: dt.date, gereserveerd: set[str] | None = None) -> str:
    prefix = f"RUN-{datum:%Y%m%d}-"
    bestaande = list(sessie.scalars(select(Testrun.run_id).where(Testrun.run_id.like(f"{prefix}%"))))
    bestaande += [r for r in (gereserveerd or ()) if r.startswith(prefix)]
    hoogste = max((int(m.group(2)) for r in bestaande if (m := _RUN_ID_RE.match(r))), default=0)
    return f"{prefix}{hoogste + 1:03d}"


def volgende_tc_id(sessie: Session, model_id: str, testtype: str, gereserveerd: set[str] | None = None) -> str:
    """<Model-ID>-<TYPE>-<nnn>, één hoger dan het hoogste bestaande nummer voor dat model en type."""
    prefix = f"{model_id}-{testtype.upper()}-"
    bestaande = list(sessie.scalars(select(Testcase.tc_id).where(Testcase.tc_id.like(f"{prefix}%"))))
    bestaande += [t for t in (gereserveerd or ()) if t.startswith(prefix)]
    hoogste = 0
    for t in bestaande:
        rest = t[len(prefix):]
        if rest.isdigit():
            hoogste = max(hoogste, int(rest))
    return f"{prefix}{hoogste + 1:03d}"


def hoogste_herhaling(sessie: Session, tc_id: str, versie: str) -> int:
    return sessie.scalar(select(func.max(Testrun.herhaling)).where(
        Testrun.tc_id == tc_id, Testrun.modelversie == versie, Testrun.verwijderd_op.is_(None))) or 0


# --- modellen en versies -----------------------------------------------------

def modelregels(sessie: Session, model_id: str) -> list[Model]:
    return list(sessie.scalars(select(Model).where(Model.model_id == model_id, Model.verwijderd_op.is_(None))
                               .order_by(Model.id)))


def modelregel(sessie: Session, model_id: str, versie: str) -> Model | None:
    if not versie:
        return None
    return sessie.scalar(select(Model).where(Model.model_id == model_id, Model.versie == versie,
                                             Model.verwijderd_op.is_(None)))


def kies_versie(sessie: Session, model_id: str) -> tuple[Model | None, str]:
    """Standaardversie als --versie ontbreekt: de nieuwste met status 'in test', anders de nieuwste
    (niet uitgefaseerde) versie. 'Nieuwste' = laatste Datum versie, dan laatst toegevoegd."""
    kandidaten = [m for m in modelregels(sessie, model_id) if m.versie and m.status != "uitgefaseerd"]
    if not kandidaten:
        return None, "geen Modelversie ingevuld in Modellen"
    kandidaten.sort(key=lambda m: (m.datum_versie or dt.date.min, m.id))
    in_test = [m for m in kandidaten if m.status == "in test"]
    if in_test:
        return in_test[-1], "nieuwste versie met status 'in test'"
    return kandidaten[-1], "nieuwste versie (geen versie met status 'in test')"


# --- modelkoppeling ------------------------------------------------------------

def laad_koppelingen(sessie: Session) -> dict:
    """Zelfde vorm als het oude modelkoppeling.json: {Model-ID: {id, name, bevestigd_op, bron}}."""
    return {k.model_id: {"id": k.openwebui_id, "name": k.naam,
                         "bevestigd_op": k.bevestigd_op.isoformat() if k.bevestigd_op else "", "bron": k.bron}
            for k in sessie.scalars(select(Koppeling))}


def bewaar_koppelingen(sessie: Session, cache: dict) -> None:
    bestaand = {k.model_id: k for k in sessie.scalars(select(Koppeling))}
    for model_id, k in bestaand.items():
        if model_id not in cache:
            sessie.delete(k)
    for model_id, c in cache.items():
        rij = bestaand.get(model_id) or Koppeling(model_id=model_id)
        rij.openwebui_id = c["id"]
        rij.naam = c.get("name", "")
        rij.bron = c.get("bron", "")
        rij.bevestigd_op = dt.date.fromisoformat(c["bevestigd_op"]) if c.get("bevestigd_op") else None
        sessie.add(rij)


def importeer_koppelingbestand(sessie: Session, pad: Path) -> int:
    """Eenmalig: koppelingen uit modelkoppeling.json overnemen als de tabel nog leeg is."""
    if not pad.exists() or sessie.scalar(select(func.count()).select_from(Koppeling)):
        return 0
    cache = json.loads(pad.read_text(encoding="utf-8"))
    bewaar_koppelingen(sessie, cache)
    return len(cache)
