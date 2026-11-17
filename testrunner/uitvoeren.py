"""`run`: testcases uit de database via de API uitvoeren en als testruns opslaan.

Selectie: alle testcases (--alle), die van één of meer modellen (--model) of specifieke cases (--tc).
Zonder --versie wordt per model de standaardversie gekozen (zie db.opslag.kies_versie).
Elke geslaagde run wordt direct in de database vastgelegd, met het volledige antwoord en het auditspoor
(payload, respons, start, duur). Run-ID en Herhaling # worden pas bij het opslaan toegekend, zodat
gelijktijdige gebruikers nooit hetzelfde ID krijgen.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db.models import Model, Testcase, Testrun
from db.opslag import (ALLEEN_GOEDGEKEURD, bewaar_koppelingen, hoogste_herhaling, instelling, kies_versie,
                       laad_koppelingen, modelregel, modelregels, volgende_run_id)
from . import koppeling as kp

MAX_PARALLEL = 4
BIJLAGE_RE = re.compile(r"^[^\n]{1,200}\.(pdf|docx?|xlsx?|pptx?|odt|ods|rtf|txt|csv|eml|msg|zip|png|jpe?g)$", re.I)


class UitvoerFout(RuntimeError):
    pass


@dataclass
class RunOpties:
    alle: bool = False
    modellen: list[str] = field(default_factory=list)
    tc: list[str] = field(default_factory=list)
    versie: str | None = None
    type: list[str] = field(default_factory=list)
    alleen_regressie: bool = False
    alleen_goedgekeurd: bool | None = None  # None: volg de instelling in de database
    herhalingen: int = 1
    uitvoerder: str | None = None
    dry_run: bool = False
    ja: bool = False
    parallel: int = 1
    hervat: bool = False
    alleen_nieuw: bool = False  # cases overslaan die voor deze versie al ooit een run hebben
    niet_interactief: bool = False


@dataclass
class Omgeving:
    models_pad: Path = Path(kp.STANDAARD_MODELLENBESTAND)
    output_dir: Path = Path("output")
    invoer: Callable[[str], str] = input
    uitvoer: Callable[[str], None] = print
    vandaag: dt.date | None = None


@dataclass
class Doel:
    """Eén model dat gedraaid wordt: de modelregel (versie) en het OpenWebUI-id."""
    regel: Model
    api_id: str
    versie_reden: str
    koppelbron: str


@dataclass
class GeplandeRun:
    tc: Testcase
    doel: Doel
    herhaling: int          # voorlopig; definitief bij opslaan
    bericht: str

    @property
    def tc_id(self) -> str:
        return self.tc.tc_id


@dataclass
class Resultaat:
    geslaagd: list[Testrun] = field(default_factory=list)
    fouten: list[tuple[str, str]] = field(default_factory=list)            # (tc_id, melding)
    overgeslagen: list[tuple[str, str]] = field(default_factory=list)      # (tc_id of model, reden)
    gepland: list[GeplandeRun] = field(default_factory=list)
    doelen: dict[str, Doel] = field(default_factory=dict)
    afgebroken: bool = False


# --- bericht -------------------------------------------------------------------

def is_bijlage(context: str) -> bool:
    delen = [d.strip() for d in re.split(r"[;,\n]", context) if d.strip()]
    return bool(delen) and all(BIJLAGE_RE.match(d) for d in delen)


def maak_bericht(tc: Testcase) -> str:
    if (tc.context or "").strip():
        return f"{tc.prompt}\n\nContext:\n{tc.context}"
    return tc.prompt


def antwoord_uit(response) -> str:
    try:
        inhoud = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
    return inhoud if isinstance(inhoud, str) else ""


# --- selectie ------------------------------------------------------------------

def actieve_testcases(sessie: Session) -> list[Testcase]:
    return list(sessie.scalars(select(Testcase).where(Testcase.verwijderd_op.is_(None)).order_by(Testcase.tc_id)))


def kies_modellen(sessie: Session, opties: RunOpties) -> tuple[list[str], list[Testcase], bool]:
    """Geeft (model-ID's, kandidaat-testcases, expliciet). Expliciet = door de gebruiker genoemd (fouten stoppen)."""
    tcs = actieve_testcases(sessie)
    if opties.tc:
        per_id = {t.tc_id: t for t in tcs}
        onbekend = [t for t in opties.tc if t not in per_id]
        if onbekend:
            raise UitvoerFout(f"Onbekende of verwijderde TC-ID('s): {', '.join(onbekend)}")
        tcs = [per_id[t] for t in dict.fromkeys(opties.tc)]
        if opties.modellen:
            anders = [t.tc_id for t in tcs if t.model_id not in opties.modellen]
            if anders:
                raise UitvoerFout(f"TC-ID('s) horen niet bij {', '.join(opties.modellen)}: {', '.join(anders)}")
        return list(dict.fromkeys(t.model_id for t in tcs)), tcs, True
    if opties.modellen:
        onbekend = [m for m in opties.modellen if not modelregels(sessie, m)]
        if onbekend:
            raise UitvoerFout(f"Model-ID('s) niet in Modellen: {', '.join(onbekend)}")
        return list(dict.fromkeys(opties.modellen)), [t for t in tcs if t.model_id in opties.modellen], True
    if opties.alle:
        return sorted({t.model_id for t in tcs}), tcs, False
    raise UitvoerFout("Kies wat er gedraaid wordt: --alle, --model <Model-ID> of --tc <TC-ID>")


def bepaal_versie(sessie: Session, model_id: str, versie: str | None) -> tuple[Model | None, str]:
    if versie:
        regel = modelregel(sessie, model_id, versie)
        if regel:
            return regel, "--versie"
        bekend = [m.versie for m in modelregels(sessie, model_id) if m.versie]
        hint = f"bekende versies: {', '.join(bekend)}" if bekend else "Modelversie is nog leeg in Modellen"
        return None, f"versie '{versie}' bestaat niet ({hint})"
    return kies_versie(sessie, model_id)


def alleen_goedgekeurd(sessie: Session, opties: RunOpties) -> bool:
    if opties.alleen_goedgekeurd is not None:
        return opties.alleen_goedgekeurd
    return instelling(sessie, ALLEEN_GOEDGEKEURD) != "N"


def al_gedraaid(sessie: Session, versie: str, datum: dt.date | None = None) -> dict[str, str]:
    """TC-ID -> laatste Run-ID voor (niet-verwijderde) runs van deze versie; met ``datum`` alleen van die dag."""
    query = select(Testrun.tc_id, Testrun.run_id).where(
        Testrun.modelversie == versie, Testrun.verwijderd_op.is_(None)).order_by(Testrun.run_id)
    if datum is not None:
        query = query.where(Testrun.datum == datum)
    return {tc_id: run_id for tc_id, run_id in sessie.execute(query)}


def vandaag_gedraaid(sessie: Session, versie: str, vandaag: dt.date) -> dict[str, str]:
    """TC-ID -> Run-ID voor runs die vandaag al voor deze versie zijn gedraaid."""
    return al_gedraaid(sessie, versie, vandaag)


def selecteer(tcs: list[Testcase], opties: RunOpties, goedgekeurd: bool, gedaan: dict[str, str] | None = None
              ) -> tuple[list[Testcase], list[tuple[str, str]]]:
    gekozen, overgeslagen = [], []
    types = {t.upper() for t in opties.type}
    for tc in tcs:
        status = (tc.status or "").lower()
        if types and tc.testtype.upper() not in types:
            continue
        if opties.alleen_regressie and tc.regressieset.upper() != "J":
            continue
        if status == "vervallen":
            overgeslagen.append((tc.tc_id, "vervallen"))
        elif goedgekeurd and status != "goedgekeurd":
            overgeslagen.append((tc.tc_id, f"status '{status or '(leeg)'}', alleen goedgekeurde cases"))
        elif not (tc.prompt or "").strip():
            overgeslagen.append((tc.tc_id, "geen prompt"))
        elif (tc.context or "").strip() and is_bijlage(tc.context):
            overgeslagen.append((tc.tc_id, "bijlage niet ondersteund"))
        elif gedaan and tc.tc_id in gedaan:
            reden = "al gedraaid voor deze versie" if opties.alleen_nieuw else "vandaag al gedraaid"
            optie = "--alleen-nieuw" if opties.alleen_nieuw else "--hervat"
            overgeslagen.append((tc.tc_id, f"{reden} ({gedaan[tc.tc_id]}), {optie}"))
        else:
            gekozen.append(tc)
    return gekozen, overgeslagen


# --- uitvoeren -----------------------------------------------------------------

def _roep_aan(client, run: GeplandeRun) -> dict:
    messages = [{"role": "user", "content": run.bericht}]
    start = dt.datetime.now().astimezone()
    t0 = time.monotonic()
    response = client.chat(messages, model=run.doel.api_id)
    duur = time.monotonic() - t0
    antwoord = antwoord_uit(response)
    if not antwoord.strip():
        raise UitvoerFout("leeg antwoord of onverwachte respons")
    return {"start": start, "duur_s": round(duur, 2),
            "payload": {"model": run.doel.api_id, "messages": messages, "stream": False},
            "response": response, "antwoord": antwoord}


def sla_op(sessie: Session, run: GeplandeRun, data: dict, uitvoerder: str, vandaag: dt.date) -> Testrun:
    """Run vastleggen; bij een botsing op Run-ID (gelijktijdige gebruiker) opnieuw proberen."""
    for _ in range(5):
        tr = Testrun(run_id=volgende_run_id(sessie, vandaag), tc_id=run.tc_id, model_id=run.doel.regel.model_id,
                     modelversie=run.doel.regel.versie, datum=vandaag, uitgevoerd_door=uitvoerder,
                     uitvoerwijze="API", herhaling=hoogste_herhaling(sessie, run.tc_id, run.doel.regel.versie) + 1,
                     antwoord=data["antwoord"], api_model=run.doel.api_id, payload=data["payload"],
                     response=data["response"], start=data["start"], duur_s=data["duur_s"])
        sessie.add(tr)
        try:
            sessie.commit()
            return tr
        except IntegrityError:
            sessie.rollback()
    raise UitvoerFout("kon geen vrij Run-ID vastleggen")


def voer_uit(sessie: Session, opties: RunOpties, client, omg: Omgeving | None = None) -> Resultaat:
    omg = omg or Omgeving()
    say = omg.uitvoer
    vandaag = omg.vandaag or dt.date.today()
    res = Resultaat()
    if not 1 <= opties.parallel <= MAX_PARALLEL:
        raise UitvoerFout(f"--parallel moet tussen 1 en {MAX_PARALLEL} liggen")
    if opties.herhalingen < 1:
        raise UitvoerFout("--herhalingen moet minstens 1 zijn")

    # 1. Modellen, versies en koppelingen
    model_ids, tcs, expliciet = kies_modellen(sessie, opties)
    if not model_ids:
        raise UitvoerFout("Geen testcases gevonden voor deze selectie")
    if opties.dry_run:
        modellen, bron = kp.laad_modellenlijst(omg.models_pad), f"{omg.models_pad} (niet live gecontroleerd)"
    else:
        modellen, bron = kp.normaliseer_modellenlijst(client.get("models")), "live /api/models"
    cache = laad_koppelingen(sessie)
    voor = dict(cache)
    problemen = []
    try:
        for model_id in model_ids:
            regel, reden = bepaal_versie(sessie, model_id, opties.versie)
            if not regel:
                problemen.append((model_id, reden))
                continue
            k = kp.bepaal(regel, modellen, cache, interactief=not opties.niet_interactief, invoer=omg.invoer,
                          uitvoer=say, vandaag=vandaag)
            if not k:
                problemen.append((model_id, "geen OpenWebUI-model gekoppeld"))
                continue
            res.doelen[model_id] = Doel(regel=regel, api_id=k["id"], versie_reden=reden, koppelbron=k.get("bron", "?"))
    finally:
        if cache != voor:
            bewaar_koppelingen(sessie, cache)
            sessie.commit()
    if problemen and expliciet:
        raise UitvoerFout("Niets uitgevoerd:\n  - " + "\n  - ".join(f"{m}: {r}" for m, r in problemen))
    res.overgeslagen.extend((f"model {m}", r) for m, r in problemen)

    # 2. Selectie en plan
    goedgekeurd = alleen_goedgekeurd(sessie, opties)
    for model_id, doel in res.doelen.items():
        if opties.alleen_nieuw:
            gedaan = al_gedraaid(sessie, doel.regel.versie)
        elif opties.hervat:
            gedaan = vandaag_gedraaid(sessie, doel.regel.versie, vandaag)
        else:
            gedaan = None
        cases, overgeslagen = selecteer([t for t in tcs if t.model_id == model_id], opties, goedgekeurd, gedaan)
        res.overgeslagen.extend(overgeslagen)
        for tc in cases:
            basis = hoogste_herhaling(sessie, tc.tc_id, doel.regel.versie)
            for k in range(1, opties.herhalingen + 1):
                res.gepland.append(GeplandeRun(tc=tc, doel=doel, herhaling=basis + k, bericht=maak_bericht(tc)))
    uitvoerder = opties.uitvoerder or os.getenv("ICTRECHT_TESTER") or "API-script"

    herkomst = "optie" if opties.alleen_goedgekeurd is not None else "instelling in de database"
    say("\nPlan")
    for model_id, doel in res.doelen.items():
        n = sum(1 for r in res.gepland if r.doel is doel)
        say(f"  {model_id:<10} versie {doel.regel.versie} ({doel.versie_reden}) → {doel.api_id} "
            f"[{doel.koppelbron}]: {n} call(s)")
    say(f"  Gecontroleerd tegen: {bron}")
    say(f"  Selectie:     {'alleen goedgekeurd' if goedgekeurd else 'goedgekeurd + concept'} ({herkomst})")
    say(f"  Calls:        {len(res.gepland)} ({opties.herhalingen}× per case, parallel {opties.parallel})")
    say(f"  Uitgevoerd door: {uitvoerder}")
    per_reden: dict[str, list[str]] = {}
    for wat, reden in res.overgeslagen:
        per_reden.setdefault(reden, []).append(wat)
    for reden, wat in per_reden.items():
        lijst = ", ".join(wat) if len(wat) <= 5 else f"{', '.join(wat[:3])} en {len(wat) - 3} andere"
        say(f"  Overgeslagen: {len(wat)}× {reden}: {lijst}")
    if not res.gepland and goedgekeurd and any("alleen goedgekeurde" in r for _, r in res.overgeslagen):
        say("  Tip: gebruik --ook-concept om concept-testcases mee te nemen.")
    if not res.gepland:
        say("\nNiets uit te voeren.")
        return res
    if opties.dry_run:
        for r in res.gepland:
            say(f"  {r.tc_id:<18} {r.tc.testtype:<7} {r.doel.regel.versie:<8} herhaling {r.herhaling}")
        say("\nDry-run: geen calls, niets opgeslagen.")
        return res
    if not opties.ja and omg.invoer("\nDoorgaan? [j/N] ").strip().lower() not in ("j", "ja", "y", "yes"):
        say("Afgebroken; niets uitgevoerd.")
        return res

    # 3. Uitvoeren; elke geslaagde run wordt direct opgeslagen
    def verwerk(run: GeplandeRun, data: dict) -> None:
        tr = sla_op(sessie, run, data, uitvoerder, vandaag)
        res.geslaagd.append(tr)
        say(f"  ✓ {tr.run_id} {run.tc_id} #{tr.herhaling} ({data['duur_s']:.1f} s, {len(data['antwoord'])} tekens)")

    def fout(run: GeplandeRun, e: BaseException) -> None:
        res.fouten.append((run.tc_id, f"{type(e).__name__}: {e}"))
        say(f"  ✗ {run.tc_id}: {e}")

    try:
        if opties.parallel == 1:
            for run in res.gepland:
                try:
                    data = _roep_aan(client, run)
                except Exception as e:  # noqa: BLE001 - elke fout per run noteren en doorgaan
                    fout(run, e)
                    continue
                verwerk(run, data)
        else:
            pool = ThreadPoolExecutor(max_workers=opties.parallel)
            try:
                futures = {pool.submit(_roep_aan, client, run): run for run in res.gepland}
                for fut in as_completed(futures):
                    run = futures[fut]
                    try:
                        data = fut.result()
                    except Exception as e:  # noqa: BLE001
                        fout(run, e)
                        continue
                    verwerk(run, data)
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
    except KeyboardInterrupt:
        res.afgebroken = True
        say("\nAfgebroken (Ctrl-C); de geslaagde runs staan al in de database.")

    say(f"\n{len(res.geslaagd)} testrun(s) opgeslagen")
    if res.fouten:
        say(f"{len(res.fouten)} mislukt (niet opgeslagen):")
        for tc_id, melding in res.fouten:
            say(f"  - {tc_id}: {melding}")
    niet_gedaan = len(res.gepland) - len(res.geslaagd) - len(res.fouten)
    if niet_gedaan > 0:
        say(f"{niet_gedaan} run(s) niet uitgevoerd door afbreken; ga verder met --hervat.")
    return res
