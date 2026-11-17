"""Database exporteren naar een Excel-register (zelfde opbouw als nu, met formules, Overzicht en Regressie).

Het lege sjabloon (sjabloon/Testregister_sjabloon.xlsx) wordt gekopieerd en alleen invoercellen worden
gevuld via xlsxpatch; formules, keuzelijsten en opmaak blijven intact. Het sjabloon heeft een vast aantal
regels (standaard 50 modellen, 300 testcases, 1.000 testruns): past de export niet, filter dan op --model.

Per testrun is er in Excel plaats voor één set scores: de laatste beoordeling van de jurist (mens),
anders die van de LLM.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from openpyxl.utils import get_column_letter
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from db.models import DIMENSIES, LLM, MENS, TESTTYPES, Model, Testcase, Testrun, score_waarde
from db.opslag import ALLEEN_GOEDGEKEURD, instelling
from .importeren import MODEL_VELDEN, TC_VELDEN
from .werkmap import INSTELLING_ALLEEN_GOEDGEKEURD, Register, RegisterFout, volgende_uitvoerpad
from .xlsxpatch import MAX_CELTEKST, patch_werkmap

ROOT = Path(__file__).resolve().parent.parent
STANDAARD_SJABLOON = ROOT / "sjabloon" / "Testregister_sjabloon.xlsx"
STANDAARD_EXPORTMAP = ROOT / "data"
MAX_ANTWOORD = 32000


class ExportFout(RuntimeError):
    pass


@dataclass
class ExportResultaat:
    pad: Path
    modellen: int
    testcases: int
    testruns: int


def afkappen(antwoord: str, run_id: str) -> str:
    if len(antwoord) <= MAX_ANTWOORD:
        return antwoord
    return antwoord[:MAX_ANTWOORD] + f"\n\n[… afgekapt; volledig antwoord in de database bij {run_id}]"


def _getal_of_tekst(s: str):
    try:
        f = float(s.replace(",", "."))
        return int(f) if f.is_integer() else f
    except ValueError:
        return s or None


def exporteer(sessie: Session, uit: str | Path | None = None, modellen: list[str] | None = None,
              sjabloon: str | Path = STANDAARD_SJABLOON, vandaag: dt.date | None = None,
              uitvoer: Callable[[str], None] = print) -> ExportResultaat:
    sjabloon = Path(sjabloon)
    if not sjabloon.exists():
        raise ExportFout(f"Sjabloon niet gevonden: {sjabloon} (maak het met scripts/maak_sjabloon.py)")
    reg = Register.laad(sjabloon)
    if reg.structuurfouten:
        raise RegisterFout("Sjabloon is geen geldig testregister: " + "; ".join(reg.structuurfouten))
    if reg.modellen or reg.testcases or reg.testruns:
        raise ExportFout(f"Sjabloon {sjabloon.name} bevat al gegevens; gebruik een leeg sjabloon")

    filter_ = set(modellen or [])

    def gekozen(model_id: str) -> bool:
        return not filter_ or model_id in filter_

    mdl = [m for m in sessie.scalars(select(Model).where(Model.verwijderd_op.is_(None))
                                     .order_by(Model.model_id, Model.id)) if gekozen(m.model_id)]
    tcs = [t for t in sessie.scalars(select(Testcase).where(Testcase.verwijderd_op.is_(None))) if gekozen(t.model_id)]
    volgorde = {t: i for i, t in enumerate(TESTTYPES)}  # zoals in het register: per model KERN, DETAIL, …, GRENS
    tcs.sort(key=lambda t: (t.model_id, volgorde.get(t.testtype, len(volgorde)), t.tc_id))
    tc_ids = {t.tc_id for t in tcs}
    runs = [r for r in sessie.scalars(select(Testrun).where(Testrun.verwijderd_op.is_(None))
                                      .options(selectinload(Testrun.beoordelingen)).order_by(Testrun.run_id)
                                      .execution_options(populate_existing=True))
            if r.tc_id in tc_ids]

    capaciteit = {"Modellen": (len(mdl), reg.modellen_grens - 1), "Testcases": (len(tcs), reg.tc_grens - 1),
                  "Testruns": (len(runs), reg.runs_grens - 1)}
    te_veel = [f"{blad}: {n} regels, ruimte voor {max_}" for blad, (n, max_) in capaciteit.items() if n > max_]
    if te_veel:
        raise ExportFout("Past niet in het sjabloon (" + "; ".join(te_veel) + "). Exporteer per model met --model, "
                         "of hoog de limieten op in het generatorscript.")

    def cellen(blad: str, rijen: list[dict]) -> dict[str, object]:
        kol = reg.kolommen[blad]
        uit_ = {}
        for i, waarden in enumerate(rijen, start=2):
            for kop, w in waarden.items():
                if kop in kol and w not in (None, ""):
                    uit_[f"{get_column_letter(kol[kop])}{i}"] = w
        return uit_

    modelrijen = []
    for m in mdl:
        rij = {kop: getattr(m, veld) for kop, veld in MODEL_VELDEN.items()}
        rij["Temperatuur"] = _getal_of_tekst(m.temperatuur)
        modelrijen.append(rij)
    tcrijen = [{kop: getattr(t, veld) for kop, veld in TC_VELDEN.items()} for t in tcs]
    runrijen = []
    for r in runs:
        rij = {"Run-ID": r.run_id, "TC-ID": r.tc_id, "Modelversie": r.modelversie, "Datum": r.datum,
               "Uitgevoerd door": r.uitgevoerd_door, "Uitvoerwijze": r.uitvoerwijze, "Herhaling #": r.herhaling,
               "Antwoord model": afkappen(r.antwoord, r.run_id)}
        b = r.beoordeling(MENS) or r.beoordeling(LLM)
        if b:
            rij.update({d: score_waarde(getattr(b, d.lower())) for d in DIMENSIES})
            toelichting = b.toelichting.strip()
            if b.te_verifieren:
                toelichting += ("\n" if toelichting else "") + "te verifiëren: " + "; ".join(b.te_verifieren)
            rij.update({"K.O.": b.ko, "K.O.-code": b.ko_code, "Toelichting": toelichting[:MAX_CELTEKST],
                        "Beoordelaar": b.beoordelaar, "Type beoordelaar": b.type})
        runrijen.append(rij)

    blad, cel = INSTELLING_ALLEEN_GOEDGEKEURD
    wijzigingen = {"Modellen": cellen("Modellen", modelrijen), "Testcases": cellen("Testcases", tcrijen),
                   "Testruns": cellen("Testruns", runrijen),
                   blad: {cel: instelling(sessie, ALLEEN_GOEDGEKEURD) or "J"}}
    if len({m.model_id for m in mdl}) == 1:
        wijzigingen["Regressie"] = {"B4": mdl[0].model_id}  # Regressie!B4 = Model-ID voor de vergelijking
    doel = Path(uit) if uit else volgende_uitvoerpad(STANDAARD_EXPORTMAP, vandaag or dt.date.today())
    pad = patch_werkmap(sjabloon, doel, wijzigingen)
    uitvoer(f"Geëxporteerd naar {pad}: {len(mdl)} modelregel(s), {len(tcs)} testcase(s), {len(runs)} testrun(s)")
    return ExportResultaat(pad=pad, modellen=len(mdl), testcases=len(tcs), testruns=len(runs))
