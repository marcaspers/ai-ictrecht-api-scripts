"""`kalibratie`: LLM-oordelen (output/beoordelingen/) vergelijken met de scores van de jurist in het register."""

from __future__ import annotations

import csv
import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path

from register.eindoordeel import eindoordeel
from register.werkmap import DIMENSIES, Register, tekst
from .uitvoeren import Omgeving, UitvoerFout

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


def kalibreer(register_pad: str | Path, model: str, versie: str, omg: Omgeving | None = None
              ) -> KalibratieResultaat:
    omg = omg or Omgeving()
    say = omg.uitvoer
    oordelen_dir = Path(omg.output_dir) / "beoordelingen"
    reg = Register.laad(register_pad)
    if reg.structuurfouten:
        raise UitvoerFout("; ".join(reg.structuurfouten))
    if not reg.modelregel(model, versie):
        raise UitvoerFout(f"Model-ID '{model}' met Modelversie '{versie}' staat niet in Modellen")
    res = KalibratieResultaat()

    for run in reg.testruns:
        if reg.model_van_run(run) != model or tekst(run["Modelversie"]) != versie:
            continue
        if tekst(run["Type beoordelaar"]).lower() != "mens" or any(tekst(run[d]) == "" for d in DIMENSIES):
            continue
        run_id = tekst(run["Run-ID"])
        pad = oordelen_dir / f"{run_id}.json"
        if not pad.exists():
            res.zonder_llm.append(run_id)
            continue
        llm = json.loads(pad.read_text(encoding="utf-8"))["oordeel"]
        mens = {d: run[d] for d in DIMENSIES}
        tc = reg.testcase(tekst(run["TC-ID"])) or {}
        res.vergelijkingen.append(Vergelijking(
            run_id=run_id, tc_id=tekst(run["TC-ID"]), testtype=tekst(tc.get("Testtype")),
            mens=mens, llm=llm["scores"],
            ko_mens=tekst(run["K.O."]).upper(), ko_code_mens=tekst(run["K.O.-code"]),
            ko_llm=llm["ko"]["ko"], ko_code_llm=llm["ko"].get("code") or "",
            eind_mens=eindoordeel(mens, run["K.O."]), eind_llm=eindoordeel(llm["scores"], llm["ko"]["ko"])))

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
