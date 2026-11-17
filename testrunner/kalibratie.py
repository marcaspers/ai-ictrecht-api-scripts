"""`kalibratie`: LLM-beoordelingen vergelijken met die van de jurist (type mens), beide uit de database."""

from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from db.models import DIMENSIES, LLM, MENS, Testrun
from register.eindoordeel import eindoordeel
from register.werkmap import tekst
from .uitvoeren import Omgeving

DOEL_GELIJK = 0.80


def _getal(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@dataclass
class Vergelijking:
    run_id: str
    tc_id: str
    testtype: str
    mens: dict
    llm: dict
    ko_mens: str
    ko_code_mens: str
    ko_llm: str
    ko_code_llm: str
    eind_mens: str
    eind_llm: str

    @property
    def gelijk(self) -> bool:
        return self.eind_mens == self.eind_llm


@dataclass
class KalibratieResultaat:
    vergelijkingen: list[Vergelijking] = field(default_factory=list)
    zonder_llm: list[str] = field(default_factory=list)
    csv_pad: Path | None = None

    @property
    def pct_gelijk(self) -> float | None:
        if not self.vergelijkingen:
            return None
        return sum(v.gelijk for v in self.vergelijkingen) / len(self.vergelijkingen)

    @property
    def gemiste_kos(self) -> list[Vergelijking]:
        return [v for v in self.vergelijkingen if v.ko_mens == "J" and v.ko_llm != "J"]

    @property
    def extra_kos(self) -> list[Vergelijking]:
        return [v for v in self.vergelijkingen if v.ko_llm == "J" and v.ko_mens != "J"]

    def per_dimensie(self) -> dict[str, dict]:
        uit = {}
        for d in DIMENSIES:
            paren = [(_getal(v.mens[d]), _getal(v.llm[d])) for v in self.vergelijkingen]
            verschillen = [l - m for m, l in paren if m is not None and l is not None]
            uit[d] = {
                "n": len(verschillen),
                "gem_verschil": sum(verschillen) / len(verschillen) if verschillen else None,
                "gem_absoluut": sum(abs(x) for x in verschillen) / len(verschillen) if verschillen else None,
                "aantal_2_of_meer": sum(1 for x in verschillen if abs(x) >= 2),
                "nvt_verschil": sum(1 for m, l in paren if (m is None) != (l is None)),
            }
        return uit


def kalibreer(sessie: Session, modellen: list[str] | None = None, versie: str | None = None,
              omg: Omgeving | None = None) -> KalibratieResultaat:
    omg = omg or Omgeving()
    say = omg.uitvoer
    res = KalibratieResultaat()
    q = (select(Testrun).where(Testrun.verwijderd_op.is_(None))
         .options(selectinload(Testrun.beoordelingen), selectinload(Testrun.testcase)).order_by(Testrun.run_id)
         .execution_options(populate_existing=True))
    if modellen:
        q = q.where(Testrun.model_id.in_(modellen))
    if versie:
        q = q.where(Testrun.modelversie == versie)
    for run in sessie.scalars(q):
        mens_b = run.beoordeling(MENS)
        if not mens_b or any(v is None for v in mens_b.scores().values()):
            continue
        llm_b = run.beoordeling(LLM)
        if not llm_b:
            res.zonder_llm.append(run.run_id)
            continue
        mens, llm = mens_b.scores(), llm_b.scores()
        res.vergelijkingen.append(Vergelijking(
            run_id=run.run_id, tc_id=run.tc_id, testtype=run.testcase.testtype, mens=mens, llm=llm,
            ko_mens=mens_b.ko.upper(), ko_code_mens=mens_b.ko_code, ko_llm=llm_b.ko.upper(), ko_code_llm=llm_b.ko_code,
            eind_mens=eindoordeel(mens, mens_b.ko), eind_llm=eindoordeel(llm, llm_b.ko)))
    model = "+".join(modellen) if modellen else "alle"
    versie = versie or "alle-versies"

    # Rapport
    n = len(res.vergelijkingen)
    say(f"\nKalibratie {model} {versie}: {n} run(s) door mens én LLM beoordeeld")
    if res.zonder_llm:
        say(f"  {len(res.zonder_llm)} door mens gescoorde run(s) zonder LLM-oordeel: {', '.join(res.zonder_llm)}")
    if not n:
        return res
    pct = res.pct_gelijk
    say(f"  Gelijk eindoordeel: {pct:.0%} ({sum(v.gelijk for v in res.vergelijkingen)}/{n}); doel ≥ {DOEL_GELIJK:.0%} "
        f"{'✓' if pct >= DOEL_GELIJK else '✗'}")
    say(f"  Gemiste K.O.'s (jurist J, LLM niet): {len(res.gemiste_kos)}; doel 0 {'✓' if not res.gemiste_kos else '✗'}"
        + (f" — {', '.join(v.run_id for v in res.gemiste_kos)}" if res.gemiste_kos else ""))
    if res.extra_kos:
        say(f"  Extra K.O.'s (LLM J, jurist niet): {', '.join(v.run_id for v in res.extra_kos)}")
    say("\n  Dimensie        n  gem. verschil (LLM−mens)  gem. |verschil|  ≥2 punten  n.v.t. ongelijk")
    for d, s in res.per_dimensie().items():
        gem = f"{s['gem_verschil']:+.2f}" if s["gem_verschil"] is not None else "–"
        absg = f"{s['gem_absoluut']:.2f}" if s["gem_absoluut"] is not None else "–"
        say(f"  {d:<14} {s['n']:>2}  {gem:>24}  {absg:>15}  {s['aantal_2_of_meer']:>9}  {s['nvt_verschil']:>15}")
    afwijkend = [v for v in res.vergelijkingen if not v.gelijk]
    if afwijkend:
        say("\n  Afwijkend eindoordeel")
        for v in afwijkend:
            say(f"  {v.run_id}  {v.tc_id:<16} mens {v.eind_mens:<11} LLM {v.eind_llm}")

    # CSV (puntkomma en BOM, zodat Excel NL het direct goed opent)
    Path(omg.output_dir).mkdir(parents=True, exist_ok=True)
    res.csv_pad = Path(omg.output_dir) / f"kalibratie_{model}_{versie}_{dt.datetime.now():%Y%m%d-%H%M%S}.csv"
    with open(res.csv_pad, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Run-ID", "TC-ID", "Testtype", "Eindoordeel mens", "Eindoordeel LLM", "Gelijk", "K.O. mens",
                    "K.O.-code mens", "K.O. LLM", "K.O.-code LLM"]
                   + [f"{d} {wie}" for d in DIMENSIES for wie in ("mens", "LLM", "verschil")])
        for v in res.vergelijkingen:
            rij = [v.run_id, v.tc_id, v.testtype, v.eind_mens, v.eind_llm, "J" if v.gelijk else "N", v.ko_mens,
                   v.ko_code_mens, v.ko_llm, v.ko_code_llm]
            for d in DIMENSIES:
                m, l = _getal(v.mens[d]), _getal(v.llm[d])
                rij += [tekst(v.mens[d]), tekst(v.llm[d]), "" if m is None or l is None else tekst(l - m)]
            w.writerow(rij)
    say(f"\n  CSV: {res.csv_pad}")
    return res
