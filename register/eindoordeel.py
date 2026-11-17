"""Eindoordeel van een testrun, exact zoals de formule in Testruns (kolom Eindoordeel):

=IF(OR(B="",COUNTA(K:O)<5),"",
  IF(P="J","Onvoldoende",
  IF(OR(AND(ISNUMBER(K),K<=1),COUNTIF(K:O,0)>0),"Onvoldoende",
  IF(COUNTIF(K:O,1)>0,"Twijfel","Voldoende"))))
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .werkmap import DIMENSIES

VOLDOENDE, TWIJFEL, ONVOLDOENDE = "Voldoende", "Twijfel", "Onvoldoende"


def _is_getal(v) -> bool:
    # ISNUMBER: alleen echte getallen, geen tekst als "1".
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _gelijk_aan(v, n: int) -> bool:
    # COUNTIF(bereik, n) telt getallen gelijk aan n én tekst die als getal n te lezen is.
    if _is_getal(v):
        return v == n
    if isinstance(v, str):
        try:
            return float(v) == n
        except ValueError:
            return False
    return False


def _leeg(v) -> bool:
    return v is None or v == ""


def eindoordeel(scores: Sequence | Mapping, ko, heeft_tc_id: bool = True) -> str:
    """``scores``: vijf waarden (Juistheid, Volledigheid, Bronnen, Bruikbaarheid, Gedrag) of een dict
    met die namen. Waarden: 0–3, 'n.v.t.' of leeg (None/''). ``ko``: 'J', 'N' of leeg.
    Geeft '' (nog niet te bepalen), 'Onvoldoende', 'Twijfel' of 'Voldoende'."""
    waarden = [scores.get(d) for d in DIMENSIES] if isinstance(scores, Mapping) else list(scores)
    if len(waarden) != 5:
        raise ValueError("Precies vijf scores verwacht")
    if not heeft_tc_id or any(_leeg(v) for v in waarden):
        return ""
    if isinstance(ko, str) and ko.upper() == "J":  # Excel vergelijkt hoofdletterongevoelig
        return ONVOLDOENDE
    juistheid = waarden[0]
    if (_is_getal(juistheid) and juistheid <= 1) or any(_gelijk_aan(v, 0) for v in waarden):
        return ONVOLDOENDE
    if any(_gelijk_aan(v, 1) for v in waarden):
        return TWIJFEL
    return VOLDOENDE
