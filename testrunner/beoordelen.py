"""`beoordeel`: testruns laten scoren door een LLM-beoordelaar volgens beoordeling/rubric.md.

Standaard gaan de oordelen alleen naar output/beoordelingen/<Run-ID>.json; het register blijft
voor de jurist. Met --schrijf-in-register worden lege scorevelden gevuld (Type beoordelaar = LLM).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from register.eindoordeel import eindoordeel
from register.validatie import valideer
from register.werkmap import DIMENSIES, Register, tekst, volgende_uitvoerpad
from . import koppeling as kp
from .uitvoeren import Omgeving, UitvoerFout, antwoord_uit

RUBRIC_PAD = Path(__file__).resolve().parent.parent / "beoordeling" / "rubric.md"
SCORES = {0, 1, 2, 3, "n.v.t."}
KO_CODES = {f"K.O.{i}" for i in range(1, 6)}
KERNPUNT_OORDELEN = {"aanwezig", "deels", "afwezig"}
SCOREVELDEN = [*DIMENSIES, "K.O.", "K.O.-code"]

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
    model: str
    versie: str
    run: list[str] = field(default_factory=list)
    opnieuw: bool = False
    dry_run: bool = False
    schrijf_in_register: bool = False
    beoordelaar_model: str | None = None
    uit: str | None = None


@dataclass
class BeoordeelResultaat:
    beoordeeld: list[dict] = field(default_factory=list)
    fouten: list[tuple[str, str]] = field(default_factory=list)
    overgeslagen: list[tuple[str, str]] = field(default_factory=list)
    geselecteerd: list[str] = field(default_factory=list)
    pad: Path | None = None


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


def gebruikersbericht(tc: dict, antwoord: str, verwacht: dict[str, str]) -> str:
    testtype = tekst(tc["Testtype"]).upper()
    peildatum = tc["Peildatum"]
    if isinstance(peildatum, (dt.date, dt.datetime)):
        peildatum = f"{peildatum:%d-%m-%Y}"

    def veld(naam, waarde):
        return f"## {naam}\n{tekst(waarde) or '(leeg)'}\n"

    return "\n".join([
        f"## Testtype\n{testtype} — {verwacht.get(testtype, '(onbekend testtype)')}\n",
        veld("Prompt", tc["Prompt"]),
        veld("Context / bijlage", tc["Context / bijlage"]),
        veld("Kernpunten verwacht antwoord", tc["Kernpunten verwacht antwoord"]),
        veld("Verwacht gedrag", tc["Verwacht gedrag"]),
        veld("Verwachte bronnen", tc["Verwachte bronnen"]),
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

def _antwoord_van_run(run: dict, runs_dir: Path) -> tuple[str, str]:
    pad = runs_dir / f"{tekst(run['Run-ID'])}.json"
    if pad.exists():
        try:
            antwoord = antwoord_uit(json.loads(pad.read_text(encoding="utf-8")).get("response"))
            if antwoord.strip():
                return antwoord, str(pad)
        except (OSError, ValueError):
            pass
    return str(run["Antwoord model"] or ""), "register"


def scores_leeg(run: dict) -> bool:
    return all(tekst(run[k]) == "" for k in SCOREVELDEN)


def beoordeel(register_pad: str | Path, opties: BeoordeelOpties, client, omg: Omgeving | None = None
              ) -> BeoordeelResultaat:
    omg = omg or Omgeving()
    say = omg.uitvoer
    output = Path(omg.output_dir)
    runs_dir, oordelen_dir = output / "runs", output / "beoordelingen"
    res = BeoordeelResultaat()
    if opties.opnieuw and not opties.run:
        raise UitvoerFout("--opnieuw kan alleen samen met --run")

    reg = Register.laad(register_pad)
    uitslag = valideer(reg, opties.model, opties.versie)
    for w in uitslag.waarschuwingen:
        say(f"Waarschuwing: {w}")
    if not uitslag.ok:
        raise UitvoerFout("Register bevat fouten:\n  - " + "\n  - ".join(uitslag.fouten))

    beoordelaar = opties.beoordelaar_model or os.getenv("ICTRECHT_JUDGE_MODEL")
    if not beoordelaar:
        raise UitvoerFout("Geen beoordelaarsmodel: gebruik --beoordelaar-model of zet ICTRECHT_JUDGE_MODEL in .env")
    beoordelaar = getattr(client, "resolve_model", lambda m: m)(beoordelaar)
    getest = kp.laad_cache(omg.koppeling_pad).get(opties.model, {}).get("id")
    if getest and getest == beoordelaar:
        say(f"Waarschuwing: het beoordelaarsmodel is hetzelfde als het geteste model ({beoordelaar}).")

    # Selectie
    kandidaten = [r for r in reg.testruns if reg.model_van_run(r) == opties.model
                  and tekst(r["Modelversie"]) == opties.versie and tekst(r["Run-ID"])]
    if opties.run:
        bekend = {tekst(r["Run-ID"]) for r in kandidaten}
        onbekend = [r for r in opties.run if r not in bekend]
        if onbekend:
            raise UitvoerFout(f"Run-ID('s) niet gevonden voor {opties.model} {opties.versie}: {', '.join(onbekend)}")
        kandidaten = [r for r in kandidaten if tekst(r["Run-ID"]) in opties.run]
    te_doen = []
    for run in kandidaten:
        run_id = tekst(run["Run-ID"])
        tc = reg.testcase(tekst(run["TC-ID"]))
        if not tekst(run["Antwoord model"]):
            res.overgeslagen.append((run_id, "geen antwoord"))
        elif not opties.run and not scores_leeg(run):
            res.overgeslagen.append((run_id, "al gescoord"))
        elif not tekst(tc["Kernpunten verwacht antwoord"]):
            res.overgeslagen.append((run_id, "testcase zonder kernpunten"))
        elif (oordelen_dir / f"{run_id}.json").exists() and not opties.opnieuw:
            res.overgeslagen.append((run_id, "al beoordeeld (gebruik --run … --opnieuw)"))
        else:
            te_doen.append((run, tc))
    res.geselecteerd = [tekst(r["Run-ID"]) for r, _ in te_doen]

    say(f"\nBeoordeling van {opties.model} {opties.versie} door {beoordelaar}: {len(te_doen)} run(s)")
    for run_id, reden in res.overgeslagen:
        say(f"  overgeslagen: {run_id} ({reden})")
    if opties.dry_run:
        for run, tc in te_doen:
            say(f"  {tekst(run['Run-ID'])}  {tekst(tc['TC-ID']):<16} {tekst(tc['Testtype'])}")
        say("\nDry-run: geen calls, niets geschreven.")
        return res
    if not te_doen:
        return res

    rubric = lees_rubric()
    verwacht = verwachtingen(rubric)
    systeem = systeemprompt(rubric)
    oordelen_dir.mkdir(parents=True, exist_ok=True)
    for run, tc in te_doen:
        run_id = tekst(run["Run-ID"])
        antwoord, bron = _antwoord_van_run(run, runs_dir)
        messages = [{"role": "system", "content": systeem},
                    {"role": "user", "content": gebruikersbericht(tc, antwoord, verwacht)}]
        try:
            oordeel, ruw = vraag_oordeel(client, beoordelaar, messages)
        except Exception as e:  # noqa: BLE001 - per run melden en doorgaan
            res.fouten.append((run_id, f"{type(e).__name__}: {e}"))
            say(f"  ✗ {run_id}: {e}")
            continue
        waarschuwingen = []
        if oordeel["ko"]["ko"] == "J" and not citaat_gevonden(oordeel["ko"]["citaat"], antwoord):
            waarschuwingen.append("K.O.-citaat niet letterlijk in het antwoord gevonden")
        oordeel_eind = eindoordeel(oordeel["scores"], oordeel["ko"]["ko"])
        pad = oordelen_dir / f"{run_id}.json"
        if pad.exists():  # --opnieuw: vorig oordeel bewaren
            pad.rename(pad.with_name(f"{run_id}.vorige-{dt.datetime.now():%Y%m%dT%H%M%S}.json"))
        pad.write_text(json.dumps({
            "run_id": run_id, "tc_id": tekst(tc["TC-ID"]), "model_id": opties.model, "modelversie": opties.versie,
            "beoordelaar_model": beoordelaar, "tijdstip": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "antwoord_bron": bron, "oordeel": oordeel, "eindoordeel": oordeel_eind,
            "waarschuwingen": waarschuwingen, "berichten": messages, "ruwe_antwoorden": ruw,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        res.beoordeeld.append({"run": run, "oordeel": oordeel, "eindoordeel": oordeel_eind})
        s = oordeel["scores"]
        say(f"  ✓ {run_id} {tekst(tc['TC-ID']):<16} {oordeel_eind:<11} "
            + " ".join(f"{d[:4]} {s[d]}" for d in DIMENSIES) + f"  K.O. {oordeel['ko']['ko']}"
            + "".join(f"  ! {w}" for w in waarschuwingen))

    if opties.schrijf_in_register and res.beoordeeld:
        geschreven = 0
        for item in res.beoordeeld:
            run, oordeel = item["run"], item["oordeel"]
            if not scores_leeg(run):
                say(f"  {tekst(run['Run-ID'])}: scorevelden in het register al gevuld; niet overschreven")
                continue
            for d in DIMENSIES:
                reg.zet(run["rij"], d, oordeel["scores"][d])
            reg.zet(run["rij"], "K.O.", oordeel["ko"]["ko"])
            if oordeel["ko"]["ko"] == "J":
                reg.zet(run["rij"], "K.O.-code", oordeel["ko"]["code"])
            toelichting = oordeel["toelichting"].strip()
            if oordeel["te_verifieren"]:
                toelichting += ("\n" if toelichting else "") + "te verifiëren: " + "; ".join(oordeel["te_verifieren"])
            reg.zet(run["rij"], "Toelichting", toelichting[:32000])
            reg.zet(run["rij"], "Beoordelaar", beoordelaar)
            reg.zet(run["rij"], "Type beoordelaar", "LLM")
            geschreven += 1
        if geschreven:
            uit = Path(opties.uit) if opties.uit else volgende_uitvoerpad(reg.pad.parent, omg.vandaag or dt.date.today())
            res.pad = reg.opslaan(uit)
            say(f"\n{geschreven} beoordeling(en) in het register gezet: {res.pad}")

    say(f"\n{len(res.beoordeeld)} beoordeeld (oordelen in {oordelen_dir})"
        + (f", {len(res.fouten)} mislukt" if res.fouten else ""))
    for run_id, melding in res.fouten:
        say(f"  - {run_id}: {melding}")
    return res
