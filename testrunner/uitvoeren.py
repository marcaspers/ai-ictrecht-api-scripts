"""`run`: testcases uit het register via de API uitvoeren en als testruns terugschrijven."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from register.ids import hoogste_herhaling, volgende_run_ids
from register.validatie import valideer
from register.werkmap import Register, RegisterFout, tekst, volgende_uitvoerpad
from . import koppeling as kp

MAX_ANTWOORD = 32000
MAX_PARALLEL = 4
BIJLAGE_RE = re.compile(r"^[^\n]{1,200}\.(pdf|docx?|xlsx?|pptx?|odt|ods|rtf|txt|csv|eml|msg|zip|png|jpe?g)$", re.I)


class UitvoerFout(RuntimeError):
    pass


@dataclass
class RunOpties:
    model: str
    versie: str
    tc: list[str] = field(default_factory=list)
    type: list[str] = field(default_factory=list)
    alleen_regressie: bool = False
    alleen_goedgekeurd: bool | None = None  # None: volg Overzicht!B7
    herhalingen: int = 1
    uitvoerder: str | None = None
    dry_run: bool = False
    ja: bool = False
    parallel: int = 1
    hervat: bool = False
    uit: str | None = None
    niet_interactief: bool = False


@dataclass
class Omgeving:
    output_dir: Path = Path("output")
    koppeling_pad: Path = Path(kp.STANDAARD_KOPPELINGBESTAND)
    models_pad: Path = Path(kp.STANDAARD_MODELLENBESTAND)
    invoer: Callable[[str], str] = input
    uitvoer: Callable[[str], None] = print
    vandaag: dt.date | None = None


@dataclass
class GeplandeRun:
    run_id: str
    tc: dict
    herhaling: int
    rij: int
    bericht: str

    @property
    def tc_id(self) -> str:
        return tekst(self.tc["TC-ID"])


@dataclass
class Resultaat:
    pad: Path | None = None
    geslaagd: list[dict] = field(default_factory=list)
    fouten: list[tuple[str, str, str]] = field(default_factory=list)  # (run_id, tc_id, melding)
    overgeslagen: list[tuple[str, str]] = field(default_factory=list)  # (tc_id, reden)
    gepland: list[GeplandeRun] = field(default_factory=list)
    afgebroken: bool = False


# --- selectie ----------------------------------------------------------------

def is_bijlage(context: str) -> bool:
    delen = [d.strip() for d in re.split(r"[;,\n]", context) if d.strip()]
    return bool(delen) and all(BIJLAGE_RE.match(d) for d in delen)


def maak_bericht(tc: dict) -> str:
    prompt = tc["Prompt"] if isinstance(tc["Prompt"], str) else tekst(tc["Prompt"])
    context = tc["Context / bijlage"]
    context = context if isinstance(context, str) else tekst(context)
    if context.strip():
        return f"{prompt}\n\nContext:\n{context}"
    return prompt


def alleen_goedgekeurd(reg: Register, opties: RunOpties) -> bool:
    if opties.alleen_goedgekeurd is not None:
        return opties.alleen_goedgekeurd
    return reg.alleen_goedgekeurd != "N"  # J (of onbekend) = formele testronde


def vandaag_gedraaid(runs_dir: Path, model: str, versie: str, vandaag: dt.date) -> dict[str, str]:
    """TC-ID -> Run-ID voor runs die vandaag al voor deze versie in output/runs staan."""
    gedaan = {}
    for p in sorted(runs_dir.glob("RUN-*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (d.get("model_id") == model and d.get("modelversie") == versie
                and str(d.get("start", ""))[:10] == vandaag.isoformat()):
            gedaan[d.get("tc_id")] = d.get("run_id", p.stem)
    return gedaan


def selecteer(reg: Register, opties: RunOpties, gedaan: dict[str, str] | None = None
              ) -> tuple[list[dict], list[tuple[str, str]]]:
    gekozen, overgeslagen = [], []
    tcs = [tc for tc in reg.testcases if tekst(tc["Model-ID"]) == opties.model]
    if opties.tc:
        bekend = {tekst(tc["TC-ID"]) for tc in tcs}
        onbekend = [t for t in opties.tc if t not in bekend]
        if onbekend:
            raise UitvoerFout(f"Onbekende TC-ID('s) voor model {opties.model}: {', '.join(onbekend)}")
    types = {t.upper() for t in opties.type}
    goedgekeurd = alleen_goedgekeurd(reg, opties)
    for tc in tcs:
        tc_id, status = tekst(tc["TC-ID"]), tekst(tc["Status"]).lower()
        if opties.tc and tc_id not in opties.tc:
            continue
        if types and tekst(tc["Testtype"]).upper() not in types:
            continue
        if opties.alleen_regressie and tekst(tc["Regressieset"]).upper() != "J":
            continue
        if status == "vervallen":
            overgeslagen.append((tc_id, "vervallen"))
            continue
        if goedgekeurd and status != "goedgekeurd":
            overgeslagen.append((tc_id, f"status '{status or '(leeg)'}', alleen goedgekeurde cases"))
            continue
        if not str(tc["Prompt"] or "").strip():
            overgeslagen.append((tc_id, "geen prompt"))
            continue
        context = str(tc["Context / bijlage"] or "")
        if context.strip() and is_bijlage(context):
            overgeslagen.append((tc_id, "bijlage niet ondersteund"))
            continue
        if gedaan and tc_id in gedaan:
            overgeslagen.append((tc_id, f"vandaag al gedraaid ({gedaan[tc_id]}), --hervat"))
            continue
        gekozen.append(tc)
    return gekozen, overgeslagen


def plan(reg: Register, cases: list[dict], opties: RunOpties, vandaag: dt.date, extra_run_ids: list[str]
         ) -> list[GeplandeRun]:
    aantal = len(cases) * opties.herhalingen
    if aantal > len(reg.vrije_rijen):
        raise UitvoerFout(f"Niet genoeg vrije regels in Testruns: {aantal} runs gepland, "
                          f"{len(reg.vrije_rijen)} regels vrij (2–{reg.runs_grens}). Er is niets uitgevoerd.")
    ids = volgende_run_ids([tekst(r["Run-ID"]) for r in reg.testruns] + extra_run_ids, vandaag, aantal)
    geplande, i = [], 0
    for tc in cases:
        basis = hoogste_herhaling(reg.testruns, tekst(tc["TC-ID"]), opties.versie)
        for k in range(1, opties.herhalingen + 1):
            geplande.append(GeplandeRun(run_id=ids[i], tc=tc, herhaling=basis + k, rij=reg.vrije_rijen[i],
                                        bericht=maak_bericht(tc)))
            i += 1
    return geplande


# --- uitvoeren ---------------------------------------------------------------

def antwoord_uit(response) -> str:
    try:
        inhoud = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
    return inhoud if isinstance(inhoud, str) else ""


def afkappen(antwoord: str, run_id: str) -> str:
    if len(antwoord) <= MAX_ANTWOORD:
        return antwoord
    return antwoord[:MAX_ANTWOORD] + f"\n\n[… afgekapt; volledig antwoord in output/runs/{run_id}.json]"


def _roep_aan(client, api_id: str, run: GeplandeRun) -> dict:
    messages = [{"role": "user", "content": run.bericht}]
    start = dt.datetime.now().astimezone()
    t0 = time.monotonic()
    response = client.chat(messages, model=api_id)
    duur = time.monotonic() - t0
    antwoord = antwoord_uit(response)
    if not antwoord.strip():
        raise UitvoerFout("leeg antwoord of onverwachte respons")
    return {"start": start.isoformat(timespec="seconds"), "duur_s": round(duur, 2),
            "payload": {"model": api_id, "messages": messages, "stream": False},
            "response": response, "antwoord": antwoord}


def _bewaar_auditspoor(runs_dir: Path, run: GeplandeRun, opties: RunOpties, api_id: str, uitvoerder: str,
                       register: Path, data: dict) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    pad = runs_dir / f"{run.run_id}.json"
    inhoud = {"run_id": run.run_id, "tc_id": run.tc_id, "model_id": opties.model, "modelversie": opties.versie,
              "api_model": api_id, "herhaling": run.herhaling, "uitgevoerd_door": uitvoerder,
              "register": register.name, "start": data["start"], "duur_s": data["duur_s"],
              "payload": data["payload"], "response": data["response"]}
    with open(pad, "x", encoding="utf-8") as f:  # 'x': een bestaand auditbestand nooit overschrijven
        json.dump(inhoud, f, indent=2, ensure_ascii=False)
    return pad


def voer_uit(register_pad: str | Path, opties: RunOpties, client, omg: Omgeving | None = None) -> Resultaat:
    omg = omg or Omgeving()
    say = omg.uitvoer
    vandaag = omg.vandaag or dt.date.today()
    runs_dir = Path(omg.output_dir) / "runs"
    res = Resultaat()
    if not 1 <= opties.parallel <= MAX_PARALLEL:
        raise UitvoerFout(f"--parallel moet tussen 1 en {MAX_PARALLEL} liggen")
    if opties.herhalingen < 1:
        raise UitvoerFout("--herhalingen moet minstens 1 zijn")

    # 1. Laden en valideren
    reg = Register.laad(register_pad)
    uitslag = valideer(reg, opties.model, opties.versie)
    for w in uitslag.waarschuwingen:
        say(f"Waarschuwing: {w}")
    if not uitslag.ok:
        raise UitvoerFout("Register bevat fouten:\n  - " + "\n  - ".join(uitslag.fouten))
    regel = reg.modelregel(opties.model, opties.versie)
    uit = Path(opties.uit) if opties.uit else volgende_uitvoerpad(reg.pad.parent, vandaag)
    if uit.exists() or uit.resolve() == reg.pad.resolve():
        raise UitvoerFout(f"Uitvoerbestand {uit} bestaat al; kies een ander pad met --uit")

    # Koppeling: bij dry-run tegen models.json, anders tegen de live /api/models.
    cache = kp.laad_cache(omg.koppeling_pad)
    voor = json.dumps(cache, sort_keys=True)
    if opties.dry_run:
        modellen, bron = kp.laad_modellenlijst(omg.models_pad), f"{omg.models_pad} (niet live gecontroleerd)"
    else:
        modellen, bron = kp.normaliseer_modellenlijst(client.get("models")), "live /api/models"
    koppel = kp.bepaal(regel, modellen, cache, interactief=not opties.niet_interactief, invoer=omg.invoer,
                       uitvoer=say, vandaag=vandaag)
    if json.dumps(cache, sort_keys=True) != voor:
        kp.bewaar_cache(omg.koppeling_pad, cache)
    if not koppel:
        raise UitvoerFout(f"Geen OpenWebUI-model gekoppeld aan {opties.model}; niets uitgevoerd")
    api_id = koppel["id"]

    # 2. Selectie, capaciteit, plan
    gedaan = vandaag_gedraaid(runs_dir, opties.model, opties.versie, vandaag) if opties.hervat else None
    cases, res.overgeslagen = selecteer(reg, opties, gedaan)
    if opties.hervat and gedaan:
        in_register = {tekst(r["Run-ID"]) for r in reg.testruns}
        ontbrekend = sorted(r for r in gedaan.values() if r not in in_register)
        if ontbrekend:
            say(f"Let op: deze runs staan in {runs_dir} maar niet in dit register: {', '.join(ontbrekend)}. "
                "Gebruik als invoer het register dat bij de afgebroken run is opgeslagen.")
    bestaande_audit = [p.stem for p in runs_dir.glob("RUN-*.json")] if runs_dir.exists() else []
    res.gepland = plan(reg, cases, opties, vandaag, bestaande_audit)
    uitvoerder = opties.uitvoerder or os.getenv("ICTRECHT_TESTER") or "API-script"

    goedgekeurd = alleen_goedgekeurd(reg, opties)
    herkomst = "optie" if opties.alleen_goedgekeurd is not None else f"Overzicht!B7 = {reg.alleen_goedgekeurd or '?'}"
    say(f"\nPlan voor {reg.pad.name}")
    say(f"  Model:        {opties.model} {opties.versie} → API-model {api_id} "
        f"(koppeling: {koppel.get('bron', '?')}; gecontroleerd tegen {bron})")
    say(f"  Selectie:     {len(cases)} testcase(s); {'alleen goedgekeurd' if goedgekeurd else 'goedgekeurd + concept'}"
        f" ({herkomst})")
    say(f"  Calls:        {len(res.gepland)} ({opties.herhalingen}× per case, parallel {opties.parallel})")
    if res.gepland:
        say(f"  Testruns:     regels {res.gepland[0].rij}–{res.gepland[-1].rij}; "
            f"{len(reg.vrije_rijen)} van {reg.runs_grens - 1} regels vrij")
        say(f"  Run-ID's:     {res.gepland[0].run_id} t/m {res.gepland[-1].run_id}")
    say(f"  Uitgevoerd door: {uitvoerder}")
    say(f"  Uitvoer:      {uit}")
    per_reden: dict[str, list[str]] = {}
    for tc_id, reden in res.overgeslagen:
        per_reden.setdefault(reden, []).append(tc_id)
    for reden, tc_ids in per_reden.items():
        lijst = ", ".join(tc_ids) if len(tc_ids) <= 5 else f"{', '.join(tc_ids[:3])} en {len(tc_ids) - 3} andere"
        say(f"  Overgeslagen: {len(tc_ids)}× {reden}: {lijst}")
    if not cases and goedgekeurd and any("alleen goedgekeurde" in r for _, r in res.overgeslagen):
        say("  Tip: gebruik --ook-concept (of zet Overzicht!B7 op N) om concept-testcases mee te nemen.")
    if not res.gepland:
        say("\nNiets uit te voeren.")
        return res
    if opties.dry_run:
        for r in res.gepland:
            say(f"  {r.run_id}  {r.tc_id:<16} {tekst(r.tc['Testtype']):<7} herhaling {r.herhaling}  → regel {r.rij}")
        say("\nDry-run: geen calls, niets geschreven.")
        return res
    if not opties.ja and omg.invoer("\nDoorgaan? [j/N] ").strip().lower() not in ("j", "ja", "y", "yes"):
        say("Afgebroken; niets uitgevoerd.")
        return res

    # 3. Uitvoeren; geslaagde runs altijd wegschrijven (try/finally)
    def verwerk(run: GeplandeRun, data: dict) -> None:
        _bewaar_auditspoor(runs_dir, run, opties, api_id, uitvoerder, reg.pad, data)
        res.geslaagd.append({"run": run, "antwoord": data["antwoord"]})
        say(f"  ✓ {run.run_id} {run.tc_id} #{run.herhaling} ({data['duur_s']:.1f} s, {len(data['antwoord'])} tekens)")

    def fout(run: GeplandeRun, e: BaseException) -> None:
        res.fouten.append((run.run_id, run.tc_id, f"{type(e).__name__}: {e}"))
        say(f"  ✗ {run.run_id} {run.tc_id} #{run.herhaling}: {e}")

    try:
        if opties.parallel == 1:
            for run in res.gepland:
                try:
                    data = _roep_aan(client, api_id, run)
                except Exception as e:  # noqa: BLE001 - elke fout per run noteren en doorgaan
                    fout(run, e)
                    continue
                verwerk(run, data)
        else:
            pool = ThreadPoolExecutor(max_workers=opties.parallel)
            try:
                futures = {pool.submit(_roep_aan, client, api_id, run): run for run in res.gepland}
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
        say("\nAfgebroken (Ctrl-C); geslaagde runs worden weggeschreven.")
    finally:
        if res.geslaagd:
            for item in sorted(res.geslaagd, key=lambda g: g["run"].rij):
                run = item["run"]
                for kop, waarde in {"Run-ID": run.run_id, "TC-ID": run.tc_id, "Modelversie": regel.versie_waarde,
                                    "Datum": vandaag, "Uitgevoerd door": uitvoerder, "Uitvoerwijze": "API",
                                    "Herhaling #": run.herhaling,
                                    "Antwoord model": afkappen(item["antwoord"], run.run_id)}.items():
                    reg.zet(run.rij, kop, waarde)
            res.pad = reg.opslaan(uit)

    say(f"\n{len(res.geslaagd)} testrun(s) toegevoegd" + (f" in {res.pad}" if res.pad else "; geen nieuw bestand"))
    if res.fouten:
        say(f"{len(res.fouten)} mislukt (niet in het register):")
        for run_id, tc_id, melding in res.fouten:
            say(f"  - {run_id} {tc_id}: {melding}")
    niet_gedaan = len(res.gepland) - len(res.geslaagd) - len(res.fouten)
    if niet_gedaan > 0:
        say(f"{niet_gedaan} run(s) niet uitgevoerd door afbreken; hervat met --hervat op het nieuwe register.")
    return res


__all__ = ["RunOpties", "Omgeving", "Resultaat", "UitvoerFout", "RegisterFout", "voer_uit", "selecteer",
           "maak_bericht", "afkappen", "is_bijlage"]
