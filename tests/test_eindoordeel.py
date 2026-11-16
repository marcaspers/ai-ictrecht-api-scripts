"""Acceptatiecriterium 7: eindoordeel() volgt de Excel-formule."""

import pytest

from register.eindoordeel import eindoordeel

N = "n.v.t."


@pytest.mark.parametrize("scores, ko, verwacht", [
    ([3, 3, 3, 3, 3], "N", "Voldoende"),
    ([2, 2, 2, 2, 2], None, "Voldoende"),
    ([3, 3, 3, 3, 3], "J", "Onvoldoende"),
    ([3, 3, 3, 3, 3], "j", "Onvoldoende"),          # Excel vergelijkt hoofdletterongevoelig
    ([1, 3, 3, 3, 3], "N", "Onvoldoende"),          # Juistheid <= 1
    ([0, 3, 3, 3, 3], "N", "Onvoldoende"),
    ([3, 0, 3, 3, 3], "N", "Onvoldoende"),          # een dimensie 0
    ([3, 3, 3, 3, 0], "N", "Onvoldoende"),
    ([3, 1, 3, 3, 3], "N", "Twijfel"),              # een dimensie 1 (niet Juistheid)
    ([2, 3, 1, 1, 3], "N", "Twijfel"),
    ([3, 3, 3, 3, N], "N", "Voldoende"),            # n.v.t. telt als gevuld
    ([N, 3, 3, 3, 3], "N", "Voldoende"),            # Juistheid n.v.t. is geen getal
    ([N, N, N, N, N], "N", "Voldoende"),
    ([N, 1, N, N, N], "N", "Twijfel"),
    ([3, 3, 3, 3, None], "J", ""),                  # niet alle vijf gevuld: leeg, ook bij K.O.
    ([3, 3, "", 3, 3], "N", ""),
    ([None] * 5, None, ""),
    (["1", 3, 3, 3, 3], "N", "Twijfel"),            # tekst "1": geen ISNUMBER, wel COUNTIF(…,1)
    ([3, "0", 3, 3, 3], "N", "Onvoldoende"),        # tekst "0" telt mee in COUNTIF(…,0)
    ([2.0, 3.0, 3, 3, 3], "N", "Voldoende"),
    ([1.0, 3, 3, 3, 3], "N", "Onvoldoende"),
])
def test_eindoordeel(scores, ko, verwacht):
    assert eindoordeel(scores, ko) == verwacht


def test_zonder_tc_id_leeg():
    assert eindoordeel([3, 3, 3, 3, 3], "N", heeft_tc_id=False) == ""


def test_dict_invoer():
    s = {"Juistheid": 3, "Volledigheid": 1, "Bronnen": 3, "Bruikbaarheid": 3, "Gedrag": N}
    assert eindoordeel(s, "N") == "Twijfel"
