"""`beoordeel`: testruns laten scoren door een LLM-beoordelaar volgens beoordeling/rubric.md.

Het oordeel wordt als beoordeling van het type LLM bij de run opgeslagen (met berichten en ruwe antwoorden).
De beoordeling van de jurist (type mens) staat daar los van, zodat beide naast elkaar bestaan voor de kalibratie.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from db.models import DIMENSIES, LLM, Beoordeling, Testcase, Testrun, nu
from db.opslag import laad_koppelingen
from register.eindoordeel import eindoordeel
from .uitvoeren import Omgeving, UitvoerFout, antwoord_uit

RUBRIC_PAD = Path(__file__).resolve().parent.parent / "beoordeling" / "rubric.md"
SCORES = {0, 1, 2, 3, "n.v.t."}
KO_CODES = {f"K.O.{i}" for i in range(1, 6)}
KERNPUNT_OORDELEN = {"aanwezig", "deels", "afwezig"}

REGELS = """\
## Werkwijze voor de beoordelaar

Je beoordeelt het antwoord van een juridisch AI-model op een testcase, volgens de rubric hierboven.

- Scoor per dimensie strikt volgens de rubric en tegen de kernpunten van de testcase. Beoordeel elk kernpunt
  afzonderlijk als aanwezig, deels of afwezig.
- Gebruik `n.v.t.` alleen als een dimensie echt niet van toepassing is (bijv. Bronnen bij een BUITEN-vraag
  die terecht wordt doorverwezen).
- Scoor nooit iets hoger dan 2 als je het niet kunt controleren (bijvoorbeeld het bestaan van een ECLI,
  uitspraak of artikelnummer). Zet zulke punten onder `te_verifieren`.
- Geef een K.O. alleen met een letterlijk citaat uit het antwoord als onderbouwing.
- Licht elke score onder 2 en elke K.O. kort toe in `toelichting`.
- Antwoord uitsluitend met één JSON-object volgens dit schema, zonder tekst eromheen:

```json
{
  "kernpunten": [{"nr": 1, "oordeel": "aanwezig|deels|afwezig", "toelichting": "..."}],
  "scores": {"Juistheid": 2, "Volledigheid": 1, "Bronnen": 2, "Bruikbaarheid": 3, "Gedrag": "n.v.t."},
  "ko": {"ko": "N", "code": null, "citaat": null},
  "toelichting": "Volledigheid 1: kernpunt 3 ontbreekt (...)",
  "te_verifieren": ["ECLI:NL:... genoemd in alinea 2"]
}
```

Scores zijn de getallen 0, 1, 2 of 3, of de tekst "n.v.t.". Bij `"ko": "J"` zijn `code` (K.O.1 t/m K.O.5)
en `citaat` verplicht; bij `"ko": "N"` zijn ze null.
"""


class OngeldigOordeel(ValueError):
    pass


@dataclass
class BeoordeelOpties:
    alle: bool = False
    modellen: list[str] = field(default_factory=list)
    versie: str | None = None
    run: list[str] = field(default_factory=list)
    opnieuw: bool = False
    dry_run: bool = False
    beoordelaar_model: str | None = None


@dataclass
class BeoordeelResultaat:
    beoordeeld: list[dict] = field(default_factory=list)
    fouten: list[tuple[str, str]] = field(default_factory=list)
    overgeslagen: list[tuple[str, str]] = field(default_factory=list)
    geselecteerd: list[str] = field(default_factory=list)


# --- prompt --------------------------------------------------------------------

def lees_rubric(pad: Path = RUBRIC_PAD) -> str:
    return pad.read_text(encoding="utf-8")


def verwachtingen(rubric: str) -> dict[str, str]:
    """Testtype -> 'Invoer: … Verwachting: …' uit de tabel in de rubric."""
    uit = {}
    for m in re.finditer(r"^\|\s*([A-Z]{3,})\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$", rubric, re.M):
        uit[m.group(1)] = f"Invoer: {m.group(2)}. Verwachting: {m.group(3)}"
    return uit


def systeemprompt(rubric: str) -> str:
    return f"{rubric.strip()}\n\n{REGELS}"


def gebruikersbericht(tc: Testcase, antwoord: str, verwacht: dict[str, str]) -> str:
    testtype = (tc.testtype or "").upper()
    peildatum = f"{tc.peildatum:%d-%m-%Y}" if tc.peildatum else ""

    def veld(naam, waarde):
        return f"## {naam}\n{(waarde or '').strip() or '(leeg)'}\n"

    return "\n".join([
        f"## Testtype\n{testtype} — {verwacht.get(testtype, '(onbekend testtype)')}\n",
        veld("Prompt", tc.prompt),
        veld("Context / bijlage", tc.context),
        veld("Kernpunten verwacht antwoord", tc.kernpunten),
        veld("Verwacht gedrag", tc.verwacht_gedrag),
        veld("Verwachte bronnen", tc.verwachte_bronnen),
        veld("Peildatum", peildatum),
        f"## Antwoord van het model\n<<<\n{antwoord}\n>>>\n",
        "Beoordeel dit antwoord. Antwoord uitsluitend met de JSON.",
    ])


# --- uitvoer van de beoordelaar ------------------------------------------------

def parse_oordeel(ruw: str) -> dict:
    tekst_ = (ruw or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", tekst_, re.S)
    if m:
        tekst_ = m.group(1).strip()
    if not tekst_.startswith("{") and "{" in tekst_:
        tekst_ = tekst_[tekst_.index("{"):tekst_.rindex("}") + 1]
    try:
        data = json.loads(tekst_)
    except ValueError as e:
        raise OngeldigOordeel(f"geen geldige JSON ({e})") from None
    valideer_oordeel(data)
    return data


def _is_score(v) -> bool:
    return (isinstance(v, int) and not isinstance(v, bool) and v in SCORES) or v == "n.v.t."


def valideer_oordeel(d) -> None:
    if not isinstance(d, dict):
        raise OngeldigOordeel("verwacht een JSON-object")
    ontbrekend = [k for k in ("kernpunten", "scores", "ko", "toelichting", "te_verifieren") if k not in d]
    if ontbrekend:
        raise OngeldigOordeel(f"velden ontbreken: {', '.join(ontbrekend)}")
    scores = d["scores"]
    if not isinstance(scores, dict) or set(scores) != set(DIMENSIES):
        raise OngeldigOordeel(f"scores moet precies deze dimensies bevatten: {', '.join(DIMENSIES)}")
    fout = [f"{k}={v!r}" for k, v in scores.items() if not _is_score(v)]
    if fout:
        raise OngeldigOordeel(f"ongeldige score(s) {', '.join(fout)}; toegestaan: 0, 1, 2, 3 of \"n.v.t.\"")
    ko = d["ko"]
    if not isinstance(ko, dict) or ko.get("ko") not in ("J", "N"):
        raise OngeldigOordeel('ko.ko moet "J" of "N" zijn')
    if ko["ko"] == "J":
        if ko.get("code") not in KO_CODES:
            raise OngeldigOordeel("bij een K.O. is code verplicht (K.O.1 t/m K.O.5)")
        if not isinstance(ko.get("citaat"), str) or not ko["citaat"].strip():
            raise OngeldigOordeel("bij een K.O. is een letterlijk citaat uit het antwoord verplicht")
    elif ko.get("code") or ko.get("citaat"):
        raise OngeldigOordeel('bij "ko": "N" moeten code en citaat null zijn')
    if not isinstance(d["kernpunten"], list) or not all(
            isinstance(k, dict) and k.get("oordeel") in KERNPUNT_OORDELEN for k in d["kernpunten"]):
        raise OngeldigOordeel("kernpunten moet een lijst zijn met oordeel aanwezig, deels of afwezig")
    if not isinstance(d["toelichting"], str):
        raise OngeldigOordeel("toelichting moet tekst zijn")
    if not isinstance(d["te_verifieren"], list) or not all(isinstance(x, str) for x in d["te_verifieren"]):
        raise OngeldigOordeel("te_verifieren moet een lijst met teksten zijn")


def _normaliseer(s: str) -> str:
    s = s.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    return " ".join(s.split()).casefold()


def citaat_gevonden(citaat: str, antwoord: str) -> bool:
    return _normaliseer(citaat.strip(" \"'….")) in _normaliseer(antwoord)


def vraag_oordeel(client, beoordelaar: str, messages: list[dict]) -> tuple[dict, list[str]]:
    """Eén call, bij ongeldige uitvoer één herkansing. Geeft (oordeel, ruwe antwoorden)."""
    ruw = []
    berichten = list(messages)
    for poging in range(2):
        antwoord = antwoord_uit(client.chat(berichten, model=beoordelaar, temperature=0))
        ruw.append(antwoord)
        try:
            return parse_oordeel(antwoord), ruw
        except OngeldigOordeel as e:
            if poging == 1:
                raise OngeldigOordeel(f"{e} (ook na herkansing)") from None
            berichten = berichten + [
                {"role": "assistant", "content": antwoord},
                {"role": "user", "content": f"Je antwoord is ongeldig: {e}. Antwoord opnieuw, uitsluitend met "
                                            "één JSON-object volgens het schema."}]
    raise AssertionError("onbereikbaar")


# --- hoofdroutine ----------------------------------------------------------------

def selecteer_runs(sessie: Session, opties: BeoordeelOpties) -> tuple[list[Testrun], list[tuple[str, str]]]:
    q = (select(Testrun).where(Testrun.verwijderd_op.is_(None))
         .options(selectinload(Testrun.beoordelingen), selectinload(Testrun.testcase)).order_by(Testrun.run_id)
         .execution_options(populate_existing=True))
    if opties.run:
        runs = list(sessie.scalars(q.where(Testrun.run_id.in_(opties.run))))
        onbekend = sorted(set(opties.run) - {r.run_id for r in runs})
        if onbekend:
            raise UitvoerFout(f"Run-ID('s) niet gevonden of verwijderd: {', '.join(onbekend)}")
    elif opties.modellen or opties.alle:
        if opties.modellen:
            q = q.where(Testrun.model_id.in_(opties.modellen))
        runs = list(sessie.scalars(q))
    else:
        raise UitvoerFout("Kies wat er beoordeeld wordt: --alle, --model <Model-ID> of --run <Run-ID>")
    if opties.versie:
        runs = [r for r in runs if r.modelversie == opties.versie]
    te_doen, overgeslagen = [], []
    for run in runs:
        if not (run.antwoord or "").strip():
            overgeslagen.append((run.run_id, "geen antwoord"))
        elif not (run.testcase.kernpunten or "").strip():
            overgeslagen.append((run.run_id, "testcase zonder kernpunten"))
        elif run.beoordeling(LLM) and not opties.opnieuw:
            overgeslagen.append((run.run_id, "al door de LLM beoordeeld (gebruik --run … --opnieuw)"))
        else:
            te_doen.append(run)
    return te_doen, overgeslagen


def beoordeel(sessie: Session, opties: BeoordeelOpties, client, omg: Omgeving | None = None) -> BeoordeelResultaat:
    omg = omg or Omgeving()
    say = omg.uitvoer
    res = BeoordeelResultaat()
    if opties.opnieuw and not opties.run:
        raise UitvoerFout("--opnieuw kan alleen samen met --run")

    beoordelaar = opties.beoordelaar_model or os.getenv("ICTRECHT_JUDGE_MODEL")
    if not beoordelaar:
        raise UitvoerFout("Geen beoordelaarsmodel: gebruik --beoordelaar-model of zet ICTRECHT_JUDGE_MODEL in .env")
    beoordelaar = getattr(client, "resolve_model", lambda m: m)(beoordelaar)

    te_doen, res.overgeslagen = selecteer_runs(sessie, opties)
    res.geselecteerd = [r.run_id for r in te_doen]
    koppelingen = laad_koppelingen(sessie)
    zelfde = sorted({r.model_id for r in te_doen if koppelingen.get(r.model_id, {}).get("id") == beoordelaar})
    if zelfde:
        say(f"Waarschuwing: het beoordelaarsmodel ({beoordelaar}) is hetzelfde als het geteste model voor "
            f"{', '.join(zelfde)}.")

    say(f"\nBeoordeling door {beoordelaar}: {len(te_doen)} run(s)")
    for run_id, reden in res.overgeslagen:
        say(f"  overgeslagen: {run_id} ({reden})")
    if opties.dry_run:
        for run in te_doen:
            say(f"  {run.run_id}  {run.tc_id:<16} {run.testcase.testtype:<7} {run.modelversie}")
        say("\nDry-run: geen calls, niets opgeslagen.")
        return res
    if not te_doen:
        return res

    rubric = lees_rubric()
    verwacht = verwachtingen(rubric)
    systeem = systeemprompt(rubric)
    for run in te_doen:
        messages = [{"role": "system", "content": systeem},
                    {"role": "user", "content": gebruikersbericht(run.testcase, run.antwoord, verwacht)}]
        try:
            oordeel, ruw = vraag_oordeel(client, beoordelaar, messages)
        except Exception as e:  # noqa: BLE001 - per run melden en doorgaan
            res.fouten.append((run.run_id, f"{type(e).__name__}: {e}"))
            say(f"  ✗ {run.run_id}: {e}")
            continue
        waarschuwingen = []
        if oordeel["ko"]["ko"] == "J" and not citaat_gevonden(oordeel["ko"]["citaat"], run.antwoord):
            waarschuwingen.append("K.O.-citaat niet letterlijk in het antwoord gevonden")
        oordeel_eind = eindoordeel(oordeel["scores"], oordeel["ko"]["ko"])
        vorige = run.beoordeling(LLM)
        if vorige:  # --opnieuw: het vorige oordeel blijft bewaard, maar telt niet meer mee
            vorige.verwijderd_op = nu()
        b = Beoordeling(run_id=run.run_id, type=LLM, beoordelaar=beoordelaar, ko=oordeel["ko"]["ko"],
                        ko_code=oordeel["ko"].get("code") or "", ko_citaat=oordeel["ko"].get("citaat") or "",
                        toelichting=oordeel["toelichting"], te_verifieren=oordeel["te_verifieren"],
                        kernpunten=oordeel["kernpunten"], eindoordeel=oordeel_eind, waarschuwingen=waarschuwingen,
                        details={"berichten": messages, "ruwe_antwoorden": ruw,
                                 "tijdstip": dt.datetime.now().astimezone().isoformat(timespec="seconds")})
        b.zet_scores(oordeel["scores"])
        sessie.add(b)
        run.beoordelingen.append(b)
        sessie.commit()
        res.beoordeeld.append({"run": run, "oordeel": oordeel, "eindoordeel": oordeel_eind})
        s = oordeel["scores"]
        say(f"  ✓ {run.run_id} {run.tc_id:<16} {oordeel_eind:<11} "
            + " ".join(f"{d[:4]} {s[d]}" for d in DIMENSIES) + f"  K.O. {oordeel['ko']['ko']}"
            + "".join(f"  ! {w}" for w in waarschuwingen))

    say(f"\n{len(res.beoordeeld)} beoordeeld" + (f", {len(res.fouten)} mislukt" if res.fouten else ""))
    for run_id, melding in res.fouten:
        say(f"  - {run_id}: {melding}")
    return res
