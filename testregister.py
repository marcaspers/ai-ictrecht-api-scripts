#!/usr/bin/env python3
"""Testregister-runner: testcases uit het Excel-testregister via de ICTRecht AI API draaien.

    python testregister.py info       register.xlsx [--model IE]
    python testregister.py koppel     register.xlsx [--ververs] [--model IE]
    python testregister.py run        register.xlsx --model IE --versie v1.0 [opties]

Elk schrijvend commando maakt een nieuw bestand (Testregister_AI-modellen_ICTRecht_<jjjjmmdd>.xlsx,
met _2, _3 … als dat al bestaat) of schrijft naar --uit. Het invoerbestand blijft ongewijzigd.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from ictrecht_api import ApiError, Client
from register.validatie import valideer
from register.werkmap import Register, RegisterFout, tekst
from register.xlsxpatch import PatchFout
from testrunner import koppeling as kp
from testrunner.uitvoeren import MAX_PARALLEL, Omgeving, RunOpties, UitvoerFout, voer_uit


# --- info --------------------------------------------------------------------

def cmd_info(args) -> int:
    reg = Register.laad(args.register)
    uitslag = valideer(reg, args.model, args.versie)
    print(f"Register: {reg.pad}")
    if reg.structuurfouten:
        _toon_uitslag(uitslag)
        return 1
    cache = kp.laad_cache(args.koppeling)
    modellen = [m for m in reg.modellen if not args.model or m.model_id == args.model]
    tcs_per_model = Counter(tekst(tc["Model-ID"]) for tc in reg.testcases)
    runs_per = Counter((reg.model_van_run(r), tekst(r["Modelversie"])) for r in reg.testruns)

    print(f"\nModellen ({len(modellen)} regels)")
    print(f"  {'Model-ID':<10} {'Versie':<8} {'Status':<16} {'Cases':>5} {'Runs':>5}  OpenWebUI-id (modelkoppeling.json)")
    for m in modellen:
        gekoppeld = cache.get(m.model_id, {}).get("id", "–")
        print(f"  {m.model_id:<10} {m.versie or '(leeg)':<8} {m.status:<16} {tcs_per_model[m.model_id]:>5} "
              f"{runs_per[(m.model_id, m.versie)]:>5}  {gekoppeld}")
    zonder_versie = [m.model_id for m in reg.modellen if not m.versie]
    if zonder_versie and not args.model:
        print(f"  {len(zonder_versie)} model(len) zonder Modelversie: run kan pas na het invullen daarvan.")

    tcs = [tc for tc in reg.testcases if not args.model or tekst(tc["Model-ID"]) == args.model]
    print(f"\nTestcases: {len(tcs)} (regels 2–{reg.tc_grens} hebben formules)")
    if tcs:
        print("  per testtype: " + ", ".join(f"{k} {v}" for k, v in Counter(tekst(t["Testtype"]) for t in tcs).items()))
        print("  per status:   " + ", ".join(f"{k or '(leeg)'} {v}" for k, v in Counter(tekst(t["Status"]) for t in tcs).items()))
        print(f"  regressieset: {sum(1 for t in tcs if tekst(t['Regressieset']).upper() == 'J')}")
    b7 = reg.alleen_goedgekeurd or "?"
    print(f"  Overzicht!B7 (alleen goedgekeurde testcases): {b7} → run selecteert standaard "
          f"{'alleen goedgekeurd' if b7 != 'N' else 'goedgekeurd + concept'}")
    if b7 != "N" and tcs and not any(tekst(t["Status"]).lower() == "goedgekeurd" for t in tcs):
        print("  Nog geen goedgekeurde testcases: run selecteert standaard niets (gebruik --ook-concept). "
              "Runs van concept-cases tellen in Overzicht alleen mee als B7 = N.")
    print(f"\nTestruns: {len(reg.testruns)} gevuld, {len(reg.vrije_rijen)} van {reg.runs_grens - 1} regels vrij "
          f"(regels 2–{reg.runs_grens})")
    _toon_uitslag(uitslag)
    return 0 if uitslag.ok else 1


def _toon_uitslag(uitslag) -> None:
    if uitslag.fouten:
        print("\nFouten")
        for f in uitslag.fouten:
            print(f"  ✗ {f}")
    if uitslag.waarschuwingen:
        print("\nWaarschuwingen")
        for w in uitslag.waarschuwingen:
            print(f"  ! {w}")
    if uitslag.ok and not uitslag.waarschuwingen:
        print("\nGeen fouten of waarschuwingen.")


# --- koppel ------------------------------------------------------------------

def cmd_koppel(args) -> int:
    if args.ververs:
        client = Client(models_file=args.models)
        client.models()  # schrijft models.json in het vaste formaat bij
        print(f"{args.models} ververst via /api/models")
    reg = Register.laad(args.register)
    if reg.structuurfouten:
        raise RegisterFout("; ".join(reg.structuurfouten))
    modellen = kp.laad_modellenlijst(args.models)
    cache = kp.laad_cache(args.koppeling)
    ids = [args.model] if args.model else list(dict.fromkeys(m.model_id for m in reg.modellen))
    resultaat = []
    try:
        for model_id in ids:
            regels = reg.modelregels(model_id)
            if not regels:
                raise RegisterFout(f"Model-ID '{model_id}' staat niet in Modellen")
            k = kp.bepaal(regels[-1], modellen, cache, interactief=not args.niet_interactief)
            resultaat.append((model_id, k))
    finally:
        kp.bewaar_cache(args.koppeling, cache)
    print(f"\nKoppelingen ({args.koppeling})")
    for model_id, k in resultaat:
        print(f"  {model_id:<10} → {k['id']}  [{k['bron']}, {k['bevestigd_op']}]" if k else f"  {model_id:<10} → overgeslagen")
    return 0 if all(k for _, k in resultaat) else 1


# --- run ---------------------------------------------------------------------

def cmd_run(args) -> int:
    goedgekeurd = True if args.alleen_goedgekeurd else (False if args.ook_concept else None)
    opties = RunOpties(model=args.model, versie=args.versie, tc=args.tc or [], type=args.type or [],
                       alleen_regressie=args.alleen_regressie, alleen_goedgekeurd=goedgekeurd,
                       herhalingen=args.herhalingen, uitvoerder=args.uitvoerder, dry_run=args.dry_run, ja=args.ja,
                       parallel=args.parallel, hervat=args.hervat, uit=args.uit,
                       niet_interactief=args.niet_interactief)
    client = Client(max_retries=3)
    res = voer_uit(args.register, opties, client,
                   Omgeving(output_dir=Path(args.output), koppeling_pad=Path(args.koppeling),
                            models_pad=Path(args.models)))
    return 1 if (res.fouten or res.afgebroken) else 0


# --- main --------------------------------------------------------------------

def bouw_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Testregister-runner voor de ICTRecht AI API")
    sub = parser.add_subparsers(dest="command", required=True)

    def basis(naam, help):
        p = sub.add_parser(naam, help=help)
        p.add_argument("register", help="Pad naar het testregister (.xlsx)")
        p.add_argument("--koppeling", default=kp.STANDAARD_KOPPELINGBESTAND, help="Pad naar modelkoppeling.json")
        p.add_argument("--models", default=kp.STANDAARD_MODELLENBESTAND, help="Pad naar models.json")
        return p

    p = basis("info", "Modellen, versies, aantallen, vrije regels en waarschuwingen")
    p.add_argument("--model", help="Alleen dit Model-ID")
    p.add_argument("--versie", help="Ook controleren of deze Modelversie bestaat")
    p.set_defaults(func=cmd_info)

    p = basis("koppel", "Model-ID ↔ OpenWebUI-model vastleggen in modelkoppeling.json")
    p.add_argument("--ververs", action="store_true", help="Eerst models.json opnieuw ophalen via /api/models")
    p.add_argument("--model", help="Alleen dit Model-ID")
    p.add_argument("--niet-interactief", action="store_true", help="Bij twijfel stoppen in plaats van vragen")
    p.set_defaults(func=cmd_koppel)

    p = basis("run", "Testcases via de API uitvoeren en als testruns opslaan")
    p.add_argument("--model", required=True, help="Model-ID, bijv. IE")
    p.add_argument("--versie", required=True, help="Modelversie zoals in Modellen, bijv. v1.0")
    p.add_argument("--tc", action="append", metavar="TC-ID", help="Alleen deze testcase (herhaalbaar)")
    p.add_argument("--type", action="append", metavar="TYPE", help="Alleen dit testtype (herhaalbaar)")
    p.add_argument("--alleen-regressie", action="store_true", help="Alleen cases met Regressieset = J")
    groep = p.add_mutually_exclusive_group()
    groep.add_argument("--ook-concept", action="store_true", help="Ook concept-cases (overschrijft Overzicht!B7)")
    groep.add_argument("--alleen-goedgekeurd", action="store_true", help="Alleen goedgekeurde cases (overschrijft B7)")
    p.add_argument("--herhalingen", type=int, default=1,
                   help="Runs per case (standaard 1; de regressieset draait in de eerste ronde 3×)")
    p.add_argument("--uitvoerder", help="Uitgevoerd door (standaard ICTRECHT_TESTER uit .env, anders API-script)")
    p.add_argument("--dry-run", action="store_true", help="Alleen het plan tonen; geen calls, niets schrijven")
    p.add_argument("--ja", action="store_true", help="Geen bevestigingsvraag")
    p.add_argument("--parallel", type=int, default=1, help=f"Gelijktijdige calls (1–{MAX_PARALLEL})")
    p.add_argument("--hervat", action="store_true", help="Cases overslaan die vandaag al voor deze versie zijn gedraaid")
    p.add_argument("--niet-interactief", action="store_true", help="Bij twijfel over de koppeling stoppen")
    p.add_argument("--uit", help="Pad voor het nieuwe register")
    p.add_argument("--output", default="output", help="Map voor auditbestanden (standaard output/)")
    p.set_defaults(func=cmd_run)
    return parser


def main(argv=None) -> int:
    args = bouw_parser().parse_args(argv)
    try:
        return args.func(args)
    except (RegisterFout, UitvoerFout, kp.KoppelingFout, PatchFout) as e:
        print(f"Fout: {e}", file=sys.stderr)
        return 2
    except ApiError as e:
        print(f"API-fout: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
