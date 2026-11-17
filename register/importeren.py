"""Excel-register importeren in de database.

Regels:
- Er wordt nooit iets overschreven.
- Testcase met een TC-ID dat al bestaat (of zonder TC-ID): krijgt een nieuw volgnummer <Model>-<TYPE>-<nnn>.
  Staat exact dezelfde case (model, type, prompt, context) er al, dan wordt hij overgeslagen.
- Testrun met een Run-ID dat al bestaat: overgeslagen als het dezelfde run is (zelfde TC-ID), anders nieuw Run-ID.
  Scores in de regel worden een beoordeling (Type beoordelaar; standaard mens).
- Modelregel (Model-ID + Modelversie) die al bestaat: overgeslagen.
Eerst wordt een plan gemaakt en getoond; pas na bevestiging wordt het weggeschreven.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import DIMENSIES, LLM, MENS, Beoordeling, Model, Testcase, Testrun, score_tekst
from db.opslag import volgende_run_id, volgende_tc_id
from .eindoordeel import eindoordeel
from .werkmap import Register, RegisterFout, tekst

MODEL_VELDEN = {"Model-ID": "model_id", "Modelnaam (OpenWebUI)": "naam", "Rechtsgebied / expertise": "rechtsgebied",
                "Modelversie": "versie", "Basismodel": "basismodel", "Systeemprompt-versie": "systeemprompt_versie",
                "Kennisbank": "kennisbank", "Tools": "tools", "Temperatuur": "temperatuur", "Eigenaar": "eigenaar",
                "Datum versie": "datum_versie", "Status": "status", "Opmerkingen": "opmerkingen"}
TC_VELDEN = {"TC-ID": "tc_id", "Model-ID": "model_id", "Rechtsgebied": "rechtsgebied", "Onderwerp": "onderwerp",
             "Testtype": "testtype", "Prompt": "prompt", "Context / bijlage": "context",
             "Kernpunten verwacht antwoord": "kernpunten", "Verwacht gedrag": "verwacht_gedrag",
             "Verwachte bronnen": "verwachte_bronnen", "Regressieset": "regressieset", "Herkomst": "herkomst",
             "Opgesteld door": "opgesteld_door", "Goedgekeurd door": "goedgekeurd_door", "Status": "status",
             "Peildatum": "peildatum", "Opmerkingen": "opmerkingen"}
DATUMVELDEN = {"datum_versie", "peildatum"}


def _datum(v) -> dt.date | None:
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    t = tekst(v)
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(t, fmt).date()
        except ValueError:
            pass
    return None


def _velden(rij: dict, mapping: dict[str, str]) -> dict:
    uit = {}
    for kop, veld in mapping.items():
        v = rij.get(kop)
        uit[veld] = _datum(v) if veld in DATUMVELDEN else tekst(v)
    return uit


def _norm(s: str) -> str:
    return " ".join((s or "").split()).casefold()


def tc_sleutel(model_id: str, testtype: str, prompt: str, context: str) -> tuple:
    """Inhoud die bepaalt of twee testcases dezelfde zijn."""
    return (model_id.casefold(), testtype.upper(), _norm(prompt), _norm(context))


@dataclass
class ImportPlan:
    bron: Path
    modellen: list[Model] = field(default_factory=list)
    testcases: list[Testcase] = field(default_factory=list)
    testruns: list[Testrun] = field(default_factory=list)
    beoordelingen: list[Beoordeling] = field(default_factory=list)
    hernummerd: list[tuple[str, str, str]] = field(default_factory=list)   # (soort, oud, nieuw)
    overgeslagen: list[tuple[str, str, str]] = field(default_factory=list)  # (soort, id, reden)
    waarschuwingen: list[str] = field(default_factory=list)

    @property
    def leeg(self) -> bool:
        return not (self.modellen or self.testcases or self.testruns or self.beoordelingen)


def maak_plan(sessie: Session, pad: str | Path, vandaag: dt.date | None = None) -> ImportPlan:
    reg = Register.laad(pad)
    if reg.structuurfouten:
        raise RegisterFout("Geen geldig testregister:\n  - " + "\n  - ".join(reg.structuurfouten))
    plan = ImportPlan(bron=Path(pad))
    bron = Path(pad).name
    vandaag = vandaag or dt.date.today()

    # --- Modellen ----------------------------------------------------------
    bekend = {(m.model_id, m.versie) for m in sessie.scalars(select(Model))}
    for regel in reg.modellen:
        v = _velden(regel.waarden, MODEL_VELDEN)
        sleutel = (v["model_id"], v["versie"])
        label = f"{v['model_id']} {v['versie'] or '(geen versie)'}"
        if sleutel in bekend:
            plan.overgeslagen.append(("model", label, "staat al in de database"))
            continue
        bekend.add(sleutel)
        plan.modellen.append(Model(**v))
    model_ids = {m for m, _ in bekend}

    # --- Testcases ---------------------------------------------------------
    bestaand = {tc.tc_id: tc for tc in sessie.scalars(select(Testcase))}
    inhoud = {tc_sleutel(tc.model_id, tc.testtype, tc.prompt, tc.context): tc.tc_id for tc in bestaand.values()}
    gereserveerd: set[str] = set()
    tc_map: dict[str, str] = {}   # TC-ID in het bestand -> TC-ID in de database
    for rij in reg.testcases:
        v = _velden(rij, TC_VELDEN)
        oud = v.pop("tc_id")
        v["testtype"] = v["testtype"].upper()
        v["status"] = v["status"].lower() or "concept"
        v["regressieset"] = "J" if v["regressieset"].upper() == "J" else "N"
        label = oud or f"regel {rij['rij']}"
        if not v["model_id"] or not v["prompt"] or not v["testtype"]:
            plan.overgeslagen.append(("testcase", label, "Model-ID, Testtype of Prompt ontbreekt"))
            continue
        sleutel = tc_sleutel(v["model_id"], v["testtype"], v["prompt"], v["context"])
        if sleutel in inhoud:
            gelijk = inhoud[sleutel]
            if oud:
                tc_map[oud] = gelijk
            reden = "staat al in de database" if gelijk == oud else f"zelfde case staat al in de database als {gelijk}"
            plan.overgeslagen.append(("testcase", label, reden))
            continue
        if oud and oud not in bestaand and oud not in gereserveerd:
            nieuw = oud
        else:
            nieuw = volgende_tc_id(sessie, v["model_id"], v["testtype"], gereserveerd)
            plan.hernummerd.append(("testcase", oud or f"(regel {rij['rij']}, geen TC-ID)", nieuw))
        gereserveerd.add(nieuw)
        inhoud[sleutel] = nieuw
        if oud:
            tc_map[oud] = nieuw
        if v["model_id"] not in model_ids:
            plan.waarschuwingen.append(f"Testcase {nieuw}: Model-ID '{v['model_id']}' staat niet in Modellen")
        plan.testcases.append(Testcase(tc_id=nieuw, import_bron=bron, oorspronkelijk_id=oud, **v))
    tc_model = {tc.tc_id: tc.model_id for tc in bestaand.values()} | {tc.tc_id: tc.model_id for tc in plan.testcases}

    # --- Testruns en scores ------------------------------------------------
    runs = {r.run_id: r for r in sessie.scalars(select(Testrun))}
    run_inhoud = {(r.tc_id, r.modelversie, _norm(r.antwoord)) for r in runs.values()}
    run_gereserveerd: set[str] = set()
    for rij in reg.testruns:
        oud_run, oud_tc = tekst(rij["Run-ID"]), tekst(rij["TC-ID"])
        label = oud_run or f"regel {rij['rij']}"
        tc_id = tc_map.get(oud_tc) or (oud_tc if oud_tc in bestaand else "")
        if not tc_id:
            plan.overgeslagen.append(("testrun", label, f"TC-ID '{oud_tc}' onbekend"))
            continue
        if oud_run in runs and runs[oud_run].tc_id == tc_id:
            plan.overgeslagen.append(("testrun", label, "staat al in de database"))
            continue
        antwoord = rij["Antwoord model"] if isinstance(rij["Antwoord model"], str) else tekst(rij["Antwoord model"])
        versie = tekst(rij["Modelversie"])
        if (tc_id, versie, _norm(antwoord)) in run_inhoud:
            plan.overgeslagen.append(("testrun", label, "zelfde run (TC-ID, versie, antwoord) staat al in de database"))
            continue
        datum = _datum(rij["Datum"]) or vandaag
        if oud_run and oud_run not in runs and oud_run not in run_gereserveerd:
            run_id = oud_run
        else:
            run_id = volgende_run_id(sessie, datum, run_gereserveerd)
            plan.hernummerd.append(("testrun", oud_run or f"(regel {rij['rij']}, geen Run-ID)", run_id))
        run_gereserveerd.add(run_id)
        run_inhoud.add((tc_id, versie, _norm(antwoord)))
        if not versie:
            plan.waarschuwingen.append(f"Testrun {run_id}: Modelversie is leeg")
        try:
            herhaling = int(float(tekst(rij["Herhaling #"]) or 1))
        except ValueError:
            herhaling = 1
        run = Testrun(run_id=run_id, tc_id=tc_id, model_id=tc_model.get(tc_id, ""), modelversie=versie, datum=datum,
                      uitgevoerd_door=tekst(rij["Uitgevoerd door"]),
                      uitvoerwijze=tekst(rij["Uitvoerwijze"]) or "handmatig", herhaling=herhaling,
                      antwoord=antwoord, import_bron=bron, oorspronkelijk_id=oud_run)
        plan.testruns.append(run)
        b = _beoordeling_uit_rij(rij, run_id, plan)
        if b:
            plan.beoordelingen.append(b)
    return plan


def _beoordeling_uit_rij(rij: dict, run_id: str, plan: ImportPlan) -> Beoordeling | None:
    velden = [*DIMENSIES, "K.O.", "K.O.-code", "Toelichting"]
    if not any(tekst(rij[k]) for k in velden):
        return None
    scores = {}
    for d in DIMENSIES:
        try:
            scores[d] = score_tekst(rij[d])
        except (TypeError, ValueError):
            plan.waarschuwingen.append(f"Testrun {run_id}: ongeldige score {d} = {rij[d]!r}; leeg gelaten")
            scores[d] = None
    type_ = LLM if tekst(rij["Type beoordelaar"]).lower() == "llm" else MENS
    ko = "J" if tekst(rij["K.O."]).upper() == "J" else "N"
    b = Beoordeling(run_id=run_id, type=type_, beoordelaar=tekst(rij["Beoordelaar"]), ko=ko,
                    ko_code=tekst(rij["K.O.-code"]), toelichting=tekst(rij["Toelichting"]))
    b.zet_scores(scores)
    b.eindoordeel = eindoordeel(b.scores(), ko)
    return b


def toon_plan(plan: ImportPlan, say: Callable[[str], None]) -> None:
    say(f"\nImport uit {plan.bron.name}")
    say(f"  Nieuw: {len(plan.modellen)} modelregel(s), {len(plan.testcases)} testcase(s), "
        f"{len(plan.testruns)} testrun(s), {len(plan.beoordelingen)} beoordeling(en)")
    if plan.hernummerd:
        say(f"\n  Nieuw ID toegekend ({len(plan.hernummerd)}):")
        for soort, oud, nieuw in plan.hernummerd:
            say(f"    {soort:<9} {oud} → {nieuw}")
    if plan.overgeslagen:
        say(f"\n  Overgeslagen ({len(plan.overgeslagen)}):")
        for soort, label, reden in plan.overgeslagen:
            say(f"    {soort:<9} {label}: {reden}")
    if plan.waarschuwingen:
        say("\n  Waarschuwingen:")
        for w in plan.waarschuwingen:
            say(f"    ! {w}")


def voer_plan_uit(sessie: Session, plan: ImportPlan) -> None:
    sessie.add_all(plan.modellen)
    sessie.add_all(plan.testcases)
    sessie.flush()
    sessie.add_all(plan.testruns)
    sessie.flush()
    sessie.add_all(plan.beoordelingen)
    sessie.commit()


def importeer(sessie: Session, pad: str | Path, *, ja: bool = False, dry_run: bool = False,
              invoer: Callable[[str], str] = input, uitvoer: Callable[[str], None] = print) -> ImportPlan:
    plan = maak_plan(sessie, pad)
    toon_plan(plan, uitvoer)
    if plan.leeg:
        uitvoer("\nNiets te importeren.")
        return plan
    if dry_run:
        uitvoer("\nDry-run: niets geïmporteerd.")
        return plan
    if not ja and invoer("\nImporteren? [j/N] ").strip().lower() not in ("j", "ja", "y", "yes"):
        uitvoer("Afgebroken; niets geïmporteerd.")
        sessie.rollback()
        return plan
    voer_plan_uit(sessie, plan)
    uitvoer("\nGeïmporteerd.")
    return plan
