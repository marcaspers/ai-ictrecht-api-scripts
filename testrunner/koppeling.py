"""Koppeling tussen de korte Model-ID's uit het register (IE) en OpenWebUI-model-id's.

Kandidaat A: Modelnaam (OpenWebUI) == name in models.json (hoofdletterongevoelig, getrimd).
Kandidaat B: het id uit Opmerkingen ('OpenWebUI-id: <id>').
Zeker: A en B geven hetzelfde id, of er is er maar één, en dat id is beschikbaar. Anders: vragen.
Bevestigde koppelingen komen in modelkoppeling.json en worden steeds tegen de actuele lijst gecontroleerd.
"""

from __future__ import annotations

import datetime as dt
import difflib
import json
import re
from collections.abc import Callable
from pathlib import Path

from register.werkmap import ModelRegel

OPMERKING_RE = re.compile(r"OpenWebUI-id:\s*(\S+)")
STANDAARD_KOPPELINGBESTAND = "modelkoppeling.json"
STANDAARD_MODELLENBESTAND = "models.json"


class KoppelingFout(RuntimeError):
    pass


# --- modellenlijst -----------------------------------------------------------

def normaliseer_modellenlijst(data) -> list[dict]:
    """Accepteert models.json ({"models": [...]}), de ruwe API-respons ({"data": [...]}) of een lijst."""
    if isinstance(data, dict):
        data = data.get("models", data.get("data", []))
    if not isinstance(data, list):
        raise KoppelingFout("Onbekend formaat voor de modellenlijst")
    uit = []
    for m in data:
        if isinstance(m, dict) and m.get("id"):
            uit.append({"id": m["id"], "name": (m.get("name") or "").strip(), "available": m.get("available", True)})
    return uit


def laad_modellenlijst(pad: str | Path) -> list[dict]:
    pad = Path(pad)
    if not pad.exists():
        raise KoppelingFout(f"{pad} niet gevonden; draai 'python testregister.py koppel <register> --ververs'")
    return normaliseer_modellenlijst(json.loads(pad.read_text(encoding="utf-8")))


# --- cache -------------------------------------------------------------------

def laad_cache(pad: str | Path) -> dict:
    pad = Path(pad)
    if not pad.exists():
        return {}
    return json.loads(pad.read_text(encoding="utf-8"))


def bewaar_cache(pad: str | Path, cache: dict) -> None:
    Path(pad).write_text(json.dumps(dict(sorted(cache.items())), indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")


# --- bepalen -----------------------------------------------------------------

def _norm(s: str) -> str:
    return " ".join(s.split()).casefold()


def kandidaten(regel: ModelRegel, modellen: list[dict]) -> tuple[list[str], str | None]:
    a = [m["id"] for m in modellen if regel.naam and _norm(m["name"]) == _norm(regel.naam)]
    mb = OPMERKING_RE.search(regel.opmerkingen or "")
    return a, (mb.group(1) if mb else None)


def _beschikbaar(model_id: str, modellen: list[dict]) -> dict | None:
    for m in modellen:
        if m["id"] == model_id and m.get("available", True):
            return m
    return None


def beoordeel_zekerheid(regel: ModelRegel, modellen: list[dict]) -> tuple[str | None, str, str]:
    """Geeft (id of None, bron, reden-van-twijfel)."""
    a, b = kandidaten(regel, modellen)
    if len(a) > 1:
        return None, "", f"naam '{regel.naam}' komt {len(a)}× voor in de modellenlijst"
    a_id = a[0] if a else None
    if a_id and b and a_id != b:
        return None, "", f"naam wijst naar '{a_id}', opmerking naar '{b}'"
    gekozen = a_id or b
    if not gekozen:
        return None, "", "geen match op naam en geen OpenWebUI-id in Opmerkingen"
    if not _beschikbaar(gekozen, modellen):
        return None, "", f"id '{gekozen}' staat niet (meer) als beschikbaar in de modellenlijst"
    bron = "naam+opmerking" if (a_id and b) else ("naam" if a_id else "opmerking")
    return gekozen, bron, ""


def top_kandidaten(regel: ModelRegel, modellen: list[dict], n: int = 5) -> list[dict]:
    zoek = _norm(regel.naam or regel.model_id)
    beschikbaar = [m for m in modellen if m.get("available", True)]
    return sorted(beschikbaar, key=lambda m: -max(difflib.SequenceMatcher(None, zoek, _norm(m["name"])).ratio(),
                                                  difflib.SequenceMatcher(None, zoek, _norm(m["id"])).ratio()))[:n]


def vraag(regel: ModelRegel, modellen: list[dict], reden: str, invoer: Callable[[str], str],
          uitvoer: Callable[[str], None]) -> dict | None:
    opties = top_kandidaten(regel, modellen)
    uitvoer(f"\nTwijfel bij Model-ID {regel.model_id} ('{regel.naam}'): {reden}")
    for i, m in enumerate(opties, 1):
        uitvoer(f"  {i}. {m['id']}  ({m['name']})")
    while True:
        keuze = invoer("Kies een nummer, typ een OpenWebUI-id, of Enter om over te slaan: ").strip()
        if not keuze:
            return None
        if keuze.isdigit() and 1 <= int(keuze) <= len(opties):
            return opties[int(keuze) - 1]
        m = next((m for m in modellen if m["id"] == keuze), None)
        if m and m.get("available", True):
            return m
        uitvoer(f"'{keuze}' is geen geldige keuze of geen beschikbaar model.")


def bepaal(regel: ModelRegel, modellen: list[dict], cache: dict, *, interactief: bool = True,
           invoer: Callable[[str], str] = input, uitvoer: Callable[[str], None] = print,
           vandaag: dt.date | None = None) -> dict | None:
    """Bepaal de koppeling voor één Model-ID en werk ``cache`` bij. Geeft de cache-entry of None (overgeslagen)."""
    vandaag = vandaag or dt.date.today()
    bestaand = cache.get(regel.model_id)
    reden = ""
    if bestaand:
        m = _beschikbaar(bestaand["id"], modellen)
        if m:
            return bestaand
        reden = f"eerder gekoppeld id '{bestaand['id']}' staat niet (meer) in de actuele modellenlijst"
    else:
        gekozen, bron, reden = beoordeel_zekerheid(regel, modellen)
        if gekozen:
            m = _beschikbaar(gekozen, modellen)
            cache[regel.model_id] = {"id": gekozen, "name": m["name"], "bevestigd_op": vandaag.isoformat(),
                                     "bron": bron}
            return cache[regel.model_id]

    if not interactief:
        raise KoppelingFout(f"Koppeling voor Model-ID {regel.model_id} is niet zeker: {reden}. "
                            "Draai 'python testregister.py koppel' interactief.")
    m = vraag(regel, modellen, reden, invoer, uitvoer)
    if not m:
        cache.pop(regel.model_id, None)  # een verouderde koppeling niet laten staan
        return None
    cache[regel.model_id] = {"id": m["id"], "name": m["name"], "bevestigd_op": vandaag.isoformat(),
                             "bron": "handmatig"}
    return cache[regel.model_id]
