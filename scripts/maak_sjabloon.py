#!/usr/bin/env python3
"""Maak een leeg exportsjabloon van een bestaand testregister.

    python scripts/maak_sjabloon.py "Testregister_….xlsx" [sjabloon/Testregister_sjabloon.xlsx]

Alle ingevulde invoercellen (geen formules) vanaf regel 2 op Modellen, Testcases en Testruns worden leeggemaakt
via xlsxpatch; opmaak, keuzelijsten, formules en de andere tabbladen blijven ongewijzigd. Gebruik dit opnieuw
als het generatorscript een nieuwe versie van het register (bijv. met meer regels) oplevert.
"""

import sys
import warnings
from pathlib import Path

from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from register.xlsxpatch import patch_werkmap  # noqa: E402

BLADEN = ["Modellen", "Testcases", "Testruns"]


def main(bron: str, doel: str = "sjabloon/Testregister_sjabloon.xlsx") -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        wb = load_workbook(bron)
    leeg = {}
    for blad in BLADEN:
        ws = wb[blad]
        leeg[blad] = {c.coordinate: None for rij in ws.iter_rows(min_row=2) for c in rij
                      if c.value is not None and not (isinstance(c.value, str) and c.value.startswith("="))}
    pad = patch_werkmap(bron, doel, leeg)
    print(f"Sjabloon: {pad} ({', '.join(f'{b}: {len(c)} cellen leeg' for b, c in leeg.items())})")


if __name__ == "__main__":
    main(*sys.argv[1:])
