"""Run-ID's en herhalingsnummers. ID's worden nooit hergebruikt."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable

from .werkmap import tekst

_RUN_ID_RE = re.compile(r"^RUN-(\d{8})-(\d+)$")


def run_id_volgnummer(run_id: str, datum: dt.date) -> int | None:
    m = _RUN_ID_RE.match(run_id.strip())
    if m and m.group(1) == f"{datum:%Y%m%d}":
        return int(m.group(2))
    return None


def volgende_run_ids(bestaande: Iterable[str], datum: dt.date, aantal: int) -> list[str]:
    """Run-ID = hoogste bestaande RUN-<datum>-nnn + 1, oplopend."""
    hoogste = max((n for r in bestaande if (n := run_id_volgnummer(r, datum)) is not None), default=0)
    return [f"RUN-{datum:%Y%m%d}-{hoogste + i:03d}" for i in range(1, aantal + 1)]


def hoogste_herhaling(testruns: Iterable[dict], tc_id: str, versie: str) -> int:
    hoogste = 0
    for run in testruns:
        if tekst(run["TC-ID"]) == tc_id and tekst(run["Modelversie"]) == versie:
            try:
                hoogste = max(hoogste, int(float(tekst(run["Herhaling #"]) or 0)))
            except ValueError:
                pass
    return hoogste
