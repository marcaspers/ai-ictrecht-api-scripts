"""Het Excel-testregister lezen (openpyxl), voor import en als sjabloon voor de export.

Kolommen worden altijd op kopnaam in rij 1 gezocht. Berekende waarden worden nooit
uit het bestand gelezen; wat nodig is rekent het script zelf uit. Schrijven gebeurt
uitsluitend via xlsxpatch (zie register/exporteren.py).
"""

from __future__ import annotations

import datetime as dt
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import load_workbook

TABBLADEN = ["Instructie", "Modellen", "Testcases", "Testruns", "Overzicht", "Regressie", "Lijsten"]

KOPPEN = {
    "Modellen": ["Model-ID", "Modelnaam (OpenWebUI)", "Modelversie", "Status", "Opmerkingen"],
    "Testcases": ["TC-ID", "Model-ID", "Rechtsgebied", "Onderwerp", "Testtype", "Prompt", "Context / bijlage",
                  "Kernpunten verwacht antwoord", "Verwacht gedrag", "Verwachte bronnen", "Regressieset",
                  "Herkomst", "Opgesteld door", "Goedgekeurd door", "Status", "Peildatum", "Opmerkingen",
                  "Regressie-nr"],
    "Testruns": ["Run-ID", "TC-ID", "Testtype", "Model-ID", "Modelversie", "Datum", "Uitgevoerd door",
                 "Uitvoerwijze", "Herhaling #", "Antwoord model", "Juistheid", "Volledigheid", "Bronnen",
                 "Bruikbaarheid", "Gedrag", "K.O.", "K.O.-code", "Eindoordeel", "Toelichting", "Beoordelaar",
                 "Type beoordelaar", "Status testcase"],
}
DIMENSIES = ["Juistheid", "Volledigheid", "Bronnen", "Bruikbaarheid", "Gedrag"]
INSTELLING_ALLEEN_GOEDGEKEURD = ("Overzicht", "B7")
STANDAARD_UITVOERNAAM = "Testregister_AI-modellen_ICTRecht_{datum:%Y%m%d}.xlsx"


class RegisterFout(RuntimeError):
    pass


def tekst(waarde) -> str:
    """Celwaarde als vergelijkbare tekst ('' voor leeg); 1.0 -> '1'."""
    if waarde is None:
        return ""
    if isinstance(waarde, float) and waarde.is_integer():
        return str(int(waarde))
    return str(waarde).strip()


def is_formule(waarde) -> bool:
    return isinstance(waarde, str) and waarde.startswith("=")


@dataclass
class ModelRegel:
    rij: int
    model_id: str
    naam: str
    versie: str
    status: str
    opmerkingen: str
    waarden: dict = field(default_factory=dict)  # alle kolommen op kopnaam


@dataclass
class Register:
    pad: Path
    kolommen: dict[str, dict[str, int]] = field(default_factory=dict)
    modellen: list[ModelRegel] = field(default_factory=list)
    testcases: list[dict] = field(default_factory=list)
    testruns: list[dict] = field(default_factory=list)
    runs_grens: int = 0       # laatste regel van Testruns met een Eindoordeel-formule
    tc_grens: int = 0         # laatste regel van Testcases met een Regressie-nr-formule
    modellen_grens: int = 0   # laatste regel van Modellen (met opmaak)
    vrije_rijen: list[int] = field(default_factory=list)
    alleen_goedgekeurd: str = ""   # inhoud van Overzicht!B7
    structuurfouten: list[str] = field(default_factory=list)

    # --- laden ------------------------------------------------------------

    @classmethod
    def laad(cls, pad: str | Path) -> "Register":
        pad = Path(pad)
        if not pad.exists():
            raise RegisterFout(f"Bestand niet gevonden: {pad}")
        with warnings.catch_warnings():
            # openpyxl kent de x14-keuzelijsten niet; voor lezen maakt dat niet uit.
            warnings.filterwarnings("ignore", message="Data Validation extension")
            wb = load_workbook(pad)  # niet data_only: we willen formules zien, geen oude berekende waarden
        reg = cls(pad=pad)
        for naam in TABBLADEN:
            if naam not in wb.sheetnames:
                reg.structuurfouten.append(f"Tabblad '{naam}' ontbreekt")
        for blad, koppen in KOPPEN.items():
            if blad not in wb.sheetnames:
                continue
            ws = wb[blad]
            gevonden = {tekst(c.value): c.column for c in ws[1] if tekst(c.value)}
            reg.kolommen[blad] = gevonden
            for kop in koppen:
                if kop not in gevonden:
                    reg.structuurfouten.append(f"Kolom '{kop}' ontbreekt op tabblad {blad}")
        if reg.structuurfouten:
            return reg

        reg._lees_modellen(wb["Modellen"])
        reg._lees_testcases(wb["Testcases"])
        reg._lees_testruns(wb["Testruns"])
        blad, cel = INSTELLING_ALLEEN_GOEDGEKEURD
        reg.alleen_goedgekeurd = tekst(wb[blad][cel].value).upper() if blad in wb.sheetnames else ""
        return reg

    def _rijen(self, ws, blad: str, max_rij: int | None = None):
        kol = self.kolommen[blad]
        for rij in range(2, (max_rij or ws.max_row) + 1):
            yield rij, {kop: ws.cell(rij, c).value for kop, c in kol.items()}

    def _lees_modellen(self, ws) -> None:
        self.modellen_grens = ws.max_row
        for rij, w in self._rijen(ws, "Modellen"):
            if not tekst(w["Model-ID"]):
                continue
            self.modellen.append(ModelRegel(rij=rij, model_id=tekst(w["Model-ID"]),
                                            naam=tekst(w["Modelnaam (OpenWebUI)"]), versie=tekst(w["Modelversie"]),
                                            status=tekst(w["Status"]), opmerkingen=tekst(w["Opmerkingen"]),
                                            waarden=w))

    def _lees_testcases(self, ws) -> None:
        kol = self.kolommen["Testcases"]["Regressie-nr"]
        self.tc_grens = max((r for r in range(2, ws.max_row + 1) if is_formule(ws.cell(r, kol).value)), default=1)
        for rij, w in self._rijen(ws, "Testcases"):
            if tekst(w["TC-ID"]) or tekst(w["Prompt"]):  # ook nieuwe cases zonder TC-ID (import nummert)
                w["rij"] = rij
                self.testcases.append(w)

    def _lees_testruns(self, ws) -> None:
        kol = self.kolommen["Testruns"]["Eindoordeel"]
        self.runs_grens = max((r for r in range(2, ws.max_row + 1) if is_formule(ws.cell(r, kol).value)), default=1)
        for rij, w in self._rijen(ws, "Testruns"):
            if tekst(w["Run-ID"]) or tekst(w["TC-ID"]):
                w["rij"] = rij
                self.testruns.append(w)
            elif rij <= self.runs_grens:
                self.vrije_rijen.append(rij)

    # --- opzoeken ---------------------------------------------------------

    def modelregels(self, model_id: str) -> list[ModelRegel]:
        return [m for m in self.modellen if m.model_id == model_id]

    def modelregel(self, model_id: str, versie: str) -> ModelRegel | None:
        for m in self.modellen:
            if m.model_id == model_id and m.versie == versie and versie:
                return m
        return None

    def testcase(self, tc_id: str) -> dict | None:
        for tc in self.testcases:
            if tekst(tc["TC-ID"]) == tc_id:
                return tc
        return None

    def model_van_run(self, run: dict) -> str:
        tc = self.testcase(tekst(run["TC-ID"]))
        return tekst(tc["Model-ID"]) if tc else ""


def volgende_uitvoerpad(map_: Path, datum: dt.date) -> Path:
    basis = map_ / STANDAARD_UITVOERNAAM.format(datum=datum)
    if not basis.exists():
        return basis
    n = 2
    while (kandidaat := basis.with_name(f"{basis.stem}_{n}{basis.suffix}")).exists():
        n += 1
    return kandidaat
