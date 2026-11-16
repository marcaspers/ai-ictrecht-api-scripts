"""Controles op het register. Fouten blokkeren `run` en `beoordeel`; waarschuwingen niet."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .werkmap import Register, tekst


@dataclass
class Uitslag:
    fouten: list[str] = field(default_factory=list)
    waarschuwingen: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.fouten


def _dubbel(waarden: list[str]) -> list[str]:
    return sorted(w for w, n in Counter(w for w in waarden if w).items() if n > 1)


def valideer(reg: Register, model: str | None = None, versie: str | None = None,
             benodigde_regels: int | None = None) -> Uitslag:
    u = Uitslag()
    if reg.structuurfouten:
        u.fouten.extend(reg.structuurfouten)
        return u

    # Model en versie
    if model is not None:
        if not reg.modelregels(model):
            u.fouten.append(f"Model-ID '{model}' staat niet in Modellen")
        elif versie is not None and not reg.modelregel(model, versie):
            bekend = [m.versie for m in reg.modelregels(model) if m.versie]
            hint = f"bekende versies: {', '.join(bekend)}" if bekend else "Modelversie is nog leeg in Modellen"
            u.fouten.append(f"Model-ID '{model}' met Modelversie '{versie}' staat niet in Modellen ({hint})")

    # Dubbele ID's
    for tc_id in _dubbel([tekst(tc["TC-ID"]) for tc in reg.testcases]):
        u.fouten.append(f"Dubbele TC-ID in Testcases: {tc_id}")
    for run_id in _dubbel([tekst(r["Run-ID"]) for r in reg.testruns]):
        u.fouten.append(f"Dubbele Run-ID in Testruns: {run_id}")

    # Capaciteit
    totaal = reg.runs_grens - 1
    if benodigde_regels is not None and benodigde_regels > len(reg.vrije_rijen):
        u.fouten.append(f"Niet genoeg vrije regels in Testruns: {benodigde_regels} nodig, {len(reg.vrije_rijen)} vrij "
                        f"(regels 2–{reg.runs_grens}). Regels daarna tellen niet mee in Overzicht en Regressie.")
    if totaal > 0 and len(reg.vrije_rijen) < 0.1 * totaal:
        u.waarschuwingen.append(f"Minder dan 10% vrije regels in Testruns ({len(reg.vrije_rijen)} van {totaal})")

    # Waarschuwingen
    tcs = [tc for tc in reg.testcases if model is None or tekst(tc["Model-ID"]) == model]
    zonder = [tekst(tc["TC-ID"]) for tc in tcs
              if not tekst(tc["Kernpunten verwacht antwoord"]) and tekst(tc["Status"]) != "vervallen"]
    if zonder:
        u.waarschuwingen.append(f"Testcases zonder kernpunten (niet te beoordelen): {', '.join(zonder)}")

    versies = {(m.model_id, m.versie) for m in reg.modellen}
    onbekend = Counter()
    for run in reg.testruns:
        sleutel = (reg.model_van_run(run), tekst(run["Modelversie"]))
        if sleutel not in versies:
            onbekend[sleutel] += 1
    for (m, v), n in sorted(onbekend.items()):
        u.waarschuwingen.append(f"{n} testrun(s) met Model-ID '{m or '?'}' en Modelversie '{v or '(leeg)'}' "
                                "die niet in Modellen staat")

    buiten = [tekst(tc["TC-ID"]) for tc in reg.testcases if tc["rij"] > reg.tc_grens]
    if buiten:
        u.waarschuwingen.append(f"Testcases na regel {reg.tc_grens} (tellen niet mee in de formules): "
                                f"{', '.join(buiten)}")
    buiten_runs = [tekst(r["Run-ID"]) or f"regel {r['rij']}" for r in reg.testruns if r["rij"] > reg.runs_grens]
    if buiten_runs:
        u.waarschuwingen.append(f"Testruns na regel {reg.runs_grens} (tellen niet mee): {', '.join(buiten_runs)}")
    return u
