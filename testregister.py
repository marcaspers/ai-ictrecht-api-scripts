#!/usr/bin/env python3
"""Testregister-runner: testcases uit de database via de ICTRecht AI API draaien en beoordelen.

    python testregister.py db init                                   # schema aanmaken/bijwerken
    python testregister.py import  register.xlsx [--ja] [--dry-run]  # Excel → database (niets overschrijven)
    python testregister.py export  [--model IE] [--uit pad.xlsx]     # database → Excel-register
    python testregister.py info    [--model IE]
    python testregister.py koppel  [--model IE] [--ververs]
    python testregister.py run       (--alle | --model IE … [--tc IE-KERN-001 …] | --tc IE-KERN-001 …) [opties]
    python testregister.py beoordeel (--alle | --model IE … | --run RUN-… …) [--versie v1.0] [opties]
    python testregister.py kalibratie [--model IE] [--versie v1.0]
    python testregister.py instelling alleen-goedgekeurd J|N

De database staat in DATABASE_URL (zie .env.example); zonder DATABASE_URL lokaal data/testregister.db.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from sqlalchemy import func, select

from db.models import KLAD, LLM, MENS, Beoordeling, Model, Testcase, Testrun
from db.opslag import (ALLEEN_GOEDGEKEURD, bewaar_koppelingen, importeer_koppelingbestand, instelling,
                       kies_versie, laad_koppelingen, modelregels, zet_instelling)
from db.sessie import DatabaseFout, database_url, init_db, open_sessie, veilige_url
from ictrecht_api import ApiError, Client
from register.exporteren import STANDAARD_SJABLOON, ExportFout, exporteer
from register.importeren import importeer
from register.werkmap import RegisterFout
from register.xlsxpatch import PatchFout
from testrunner import koppeling as kp
from testrunner.beoordelen import BeoordeelOpties, beoordeel
from testrunner.kalibratie import kalibreer
from testrunner.uitvoeren import MAX_PARALLEL, Omgeving, RunOpties, UitvoerFout, voer_uit

KOPPELINGBESTAND = Path(__file__).resolve().parent / "modelkoppeling.json"


# --- db, import, export, instelling ---------------------------------------------

def cmd_db(args) -> int:
    url = database_url()
    init_db(url)
    print(f"Database {veilige_url(url)}: schema is actueel.")
    with open_sessie() as s:
        n = importeer_koppelingbestand(s, KOPPELINGBESTAND)
        s.commit()
    if n:
        print(f"{n} modelkoppeling(en) overgenomen uit {KOPPELINGBESTAND.name}.")
    return 0


def cmd_import(args) -> int:
    with open_sessie() as s:
        importeer(s, args.bestand, ja=args.ja, dry_run=args.dry_run)
    return 0


def cmd_export(args) -> int:
    with open_sessie() as s:
        exporteer(s, uit=args.uit, modellen=args.model, sjabloon=args.sjabloon)
    return 0


def cmd_instelling(args) -> int:
    with open_sessie() as s:
        if args.waarde:
            zet_instelling(s, ALLEEN_GOEDGEKEURD, args.waarde.upper())
            s.commit()
        print(f"alleen-goedgekeurd = {instelling(s, ALLEEN_GOEDGEKEURD)} "
              "(J: run selecteert alleen goedgekeurde cases; N: ook concept)")
    return 0


# --- info ----------------------------------------------------------------------

def cmd_info(args) -> int:
    print(f"Database: {veilige_url(database_url())}")
    with open_sessie() as s:
        filt = set(args.model or [])
        modellen = [m for m in s.scalars(select(Model).where(Model.verwijderd_op.is_(None))
                                         .order_by(Model.model_id, Model.id)) if not filt or m.model_id in filt]
        tcs = [t for t in s.scalars(select(Testcase).where(Testcase.verwijderd_op.is_(None)))
               if not filt or t.model_id in filt]
        runs = [r for r in s.scalars(select(Testrun).where(Testrun.verwijderd_op.is_(None)))
                if not filt or r.model_id in filt]
        koppelingen = laad_koppelingen(s)
        tcs_per_model = Counter(t.model_id for t in tcs)
        runs_per = Counter((r.model_id, r.modelversie) for r in runs)
        beoordeeld = Counter(s.execute(select(Beoordeling.type, func.count()).where(
            Beoordeling.verwijderd_op.is_(None), Beoordeling.status != KLAD).group_by(Beoordeling.type)).all())
        klad = s.scalar(select(func.count()).select_from(Beoordeling).where(
            Beoordeling.verwijderd_op.is_(None), Beoordeling.status == KLAD))

        print(f"\nModellen ({len(modellen)} regels)")
        print(f"  {'Model-ID':<10} {'Versie':<8} {'Status':<16} {'Cases':>5} {'Runs':>5}  OpenWebUI-id")
        for m in modellen:
            print(f"  {m.model_id:<10} {m.versie or '(leeg)':<8} {m.status:<16} {tcs_per_model[m.model_id]:>5} "
                  f"{runs_per[(m.model_id, m.versie)]:>5}  {koppelingen.get(m.model_id, {}).get('id', '–')}")
        zonder = sorted({m.model_id for m in modellen} - {m.model_id for m in modellen
                                                          if kies_versie(s, m.model_id)[0]})
        if zonder:
            print(f"  {len(zonder)} model(len) zonder bruikbare Modelversie (run kan pas na het invullen): "
                  f"{', '.join(zonder)}")

        print(f"\nTestcases: {len(tcs)}")
        if tcs:
            print("  per testtype: " + ", ".join(f"{k} {v}" for k, v in Counter(t.testtype for t in tcs).items()))
            print("  per status:   " + ", ".join(f"{k or '(leeg)'} {v}" for k, v in Counter(t.status for t in tcs).items()))
            print(f"  regressieset: {sum(1 for t in tcs if t.regressieset == 'J')}")
        ag = instelling(s, ALLEEN_GOEDGEKEURD)
        print(f"  instelling alleen-goedgekeurd: {ag} → run selecteert standaard "
              f"{'alleen goedgekeurd' if ag != 'N' else 'goedgekeurd + concept'}")
        print(f"\nTestruns: {len(runs)}; beoordelingen: mens {beoordeeld.get(MENS, 0)}, LLM {beoordeeld.get(LLM, 0)}"
              + (f", klad {klad}" if klad else ""))

        waarschuwingen = []
        zonder_kp = [t.tc_id for t in tcs if not t.kernpunten.strip() and t.status != "vervallen"]
        if zonder_kp:
            waarschuwingen.append(f"Testcases zonder kernpunten (niet te beoordelen): {', '.join(sorted(zonder_kp))}")
        versies = {(m.model_id, m.versie) for m in s.scalars(select(Model).where(Model.verwijderd_op.is_(None)))}
        for (m, v), n in sorted(runs_per.items()):
            if (m, v) not in versies:
                waarschuwingen.append(f"{n} testrun(s) van {m} met Modelversie '{v or '(leeg)'}' die niet in Modellen staat")
        if waarschuwingen:
            print("\nWaarschuwingen")
            for w in waarschuwingen:
                print(f"  ! {w}")
    return 0


# --- koppel ----------------------------------------------------------------------

def cmd_koppel(args) -> int:
    if args.ververs:
        Client(models_file=args.models).models()  # schrijft models.json in het vaste formaat bij
        print(f"{args.models} ververst via /api/models")
    modellen = kp.laad_modellenlijst(args.models)
    with open_sessie() as s:
        ids = args.model or sorted({m.model_id for m in s.scalars(select(Model).where(Model.verwijderd_op.is_(None)))})
        cache = laad_koppelingen(s)
        resultaat = []
        try:
            for model_id in ids:
                regels = modelregels(s, model_id)
                if not regels:
                    raise UitvoerFout(f"Model-ID '{model_id}' staat niet in Modellen")
                k = kp.bepaal(regels[-1], modellen, cache, interactief=not args.niet_interactief)
                resultaat.append((model_id, k))
        finally:
            bewaar_koppelingen(s, cache)
            s.commit()
    print("\nKoppelingen")
    for model_id, k in resultaat:
        print(f"  {model_id:<10} → {k['id']}  [{k['bron']}, {k['bevestigd_op']}]" if k else f"  {model_id:<10} → overgeslagen")
    return 0 if all(k for _, k in resultaat) else 1


# --- run, beoordeel, kalibratie ----------------------------------------------------

def _omgeving(args) -> Omgeving:
    return Omgeving(models_pad=Path(args.models), output_dir=Path(getattr(args, "output", "output")))


def cmd_run(args) -> int:
    if not (args.alle or args.model or args.tc):
        raise UitvoerFout("Kies wat er gedraaid wordt: --alle, --model <Model-ID> en/of --tc <TC-ID>")
    if args.alle and args.tc:
        raise UitvoerFout("--tc kan niet samen met --alle (wel met --model)")
    goedgekeurd = True if args.alleen_goedgekeurd else (False if args.ook_concept else None)
    opties = RunOpties(alle=args.alle, modellen=args.model or [], tc=args.tc or [], versie=args.versie,
                       type=args.type or [], alleen_regressie=args.alleen_regressie, alleen_goedgekeurd=goedgekeurd,
                       herhalingen=args.herhalingen, uitvoerder=args.uitvoerder, dry_run=args.dry_run, ja=args.ja,
                       parallel=args.parallel, hervat=args.hervat, alleen_nieuw=args.alleen_nieuw,
                       niet_interactief=args.niet_interactief)
    with open_sessie() as s:
        res = voer_uit(s, opties, Client(max_retries=3), _omgeving(args))
    return 1 if (res.fouten or res.afgebroken) else 0


def cmd_beoordeel(args) -> int:
    opties = BeoordeelOpties(alle=args.alle, modellen=args.model or [], versie=args.versie, run=args.run or [],
                             opnieuw=args.opnieuw, dry_run=args.dry_run, beoordelaar_model=args.beoordelaar_model)
    with open_sessie() as s:
        res = beoordeel(s, opties, Client(max_retries=3), _omgeving(args))
    return 1 if res.fouten else 0


def cmd_kalibratie(args) -> int:
    with open_sessie() as s:
        kalibreer(s, args.model, args.versie, _omgeving(args))
    return 0


# --- main ----------------------------------------------------------------------

def bouw_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Testregister-runner voor de ICTRecht AI API")
    sub = parser.add_subparsers(dest="command", required=True)

    def model_arg(p, help="Alleen dit Model-ID (herhaalbaar)"):
        p.add_argument("--model", "--domein", action="append", metavar="MODEL-ID", help=help)

    p = sub.add_parser("db", help="Database beheren")
    p.add_argument("actie", choices=["init"], help="init: schema aanmaken of bijwerken (veilig om te herhalen)")
    p.set_defaults(func=cmd_db)

    p = sub.add_parser("import", help="Excel-register importeren (bestaande gegevens worden nooit overschreven)")
    p.add_argument("bestand", help="Pad naar het Excel-register (.xlsx)")
    p.add_argument("--ja", action="store_true", help="Geen bevestigingsvraag")
    p.add_argument("--dry-run", action="store_true", help="Alleen tonen wat er geïmporteerd zou worden")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("export", help="Database exporteren naar een Excel-register")
    model_arg(p)
    p.add_argument("--uit", help="Pad voor het register (standaard data/Testregister_AI-modellen_ICTRecht_<datum>.xlsx)")
    p.add_argument("--sjabloon", default=str(STANDAARD_SJABLOON), help="Leeg register als sjabloon")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("instelling", help="Instelling tonen of zetten")
    p.add_argument("naam", choices=["alleen-goedgekeurd"])
    p.add_argument("waarde", nargs="?", choices=["J", "N", "j", "n"])
    p.set_defaults(func=cmd_instelling)

    p = sub.add_parser("info", help="Modellen, versies, aantallen en waarschuwingen")
    model_arg(p)
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("koppel", help="Model-ID ↔ OpenWebUI-model vastleggen")
    model_arg(p)
    p.add_argument("--ververs", action="store_true", help="Eerst models.json opnieuw ophalen via /api/models")
    p.add_argument("--niet-interactief", action="store_true", help="Bij twijfel stoppen in plaats van vragen")
    p.add_argument("--models", default=kp.STANDAARD_MODELLENBESTAND, help="Pad naar models.json")
    p.set_defaults(func=cmd_koppel)

    def selectie(p, wat, verplicht=True):
        g = p.add_mutually_exclusive_group(required=verplicht)
        g.add_argument("--alle", action="store_true", help=f"Alle {wat}")
        g.add_argument("--model", "--domein", action="append", metavar="MODEL-ID",
                       help=f"{wat.capitalize()} van dit model (herhaalbaar)")
        return g

    p = sub.add_parser("run", help="Testcases via de API uitvoeren en als testruns opslaan")
    selectie(p, "testcases", verplicht=False)  # --alle of --model; --tc mag los of samen met --model
    p.add_argument("--tc", action="append", metavar="TC-ID",
                   help="Alleen deze testcase (herhaalbaar; te combineren met --model)")
    p.add_argument("--versie", help="Modelversie (standaard per model de nieuwste versie 'in test')")
    p.add_argument("--type", action="append", metavar="TYPE", help="Alleen dit testtype (herhaalbaar)")
    p.add_argument("--alleen-regressie", action="store_true", help="Alleen cases met Regressieset = J")
    groep = p.add_mutually_exclusive_group()
    groep.add_argument("--ook-concept", action="store_true", help="Ook concept-cases (overschrijft de instelling)")
    groep.add_argument("--alleen-goedgekeurd", action="store_true", help="Alleen goedgekeurde cases")
    p.add_argument("--herhalingen", type=int, default=1, help="Runs per case (standaard 1)")
    p.add_argument("--uitvoerder", help="Uitgevoerd door (standaard ICTRECHT_TESTER uit .env, anders API-script)")
    p.add_argument("--dry-run", action="store_true", help="Alleen het plan tonen; geen calls, niets opslaan")
    p.add_argument("--ja", action="store_true", help="Geen bevestigingsvraag")
    p.add_argument("--parallel", type=int, default=1, help=f"Gelijktijdige calls (1–{MAX_PARALLEL})")
    p.add_argument("--hervat", action="store_true", help="Cases overslaan die vandaag al voor deze versie zijn gedraaid")
    p.add_argument("--alleen-nieuw", action="store_true",
                   help="Cases overslaan die voor deze versie al een run hebben (ongeacht de datum)")
    p.add_argument("--niet-interactief", action="store_true", help="Bij twijfel over de koppeling stoppen")
    p.add_argument("--models", default=kp.STANDAARD_MODELLENBESTAND, help="Pad naar models.json (voor --dry-run)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("beoordeel", help="Testruns laten scoren door een LLM-beoordelaar")
    g = selectie(p, "testruns")
    g.add_argument("--run", action="append", metavar="RUN-ID", help="Alleen deze run (herhaalbaar)")
    p.add_argument("--versie", help="Alleen runs van deze Modelversie")
    p.add_argument("--opnieuw", action="store_true", help="Opnieuw beoordelen (alleen met --run)")
    p.add_argument("--beoordelaar-model", help="OpenWebUI-model (standaard ICTRECHT_JUDGE_MODEL uit .env)")
    p.add_argument("--dry-run", action="store_true", help="Alleen tonen welke runs beoordeeld zouden worden")
    p.add_argument("--models", default=kp.STANDAARD_MODELLENBESTAND, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_beoordeel)

    p = sub.add_parser("kalibratie", help="LLM-oordelen vergelijken met de scores van de jurist")
    model_arg(p)
    p.add_argument("--versie", help="Alleen runs van deze Modelversie")
    p.add_argument("--output", default="output", help="Map voor de CSV (standaard output/)")
    p.add_argument("--models", default=kp.STANDAARD_MODELLENBESTAND, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_kalibratie)
    return parser


def main(argv=None) -> int:
    args = bouw_parser().parse_args(argv)
    try:
        return args.func(args)
    except (RegisterFout, UitvoerFout, kp.KoppelingFout, PatchFout, ExportFout, DatabaseFout) as e:
        print(f"Fout: {e}", file=sys.stderr)
        return 2
    except ApiError as e:
        print(f"API-fout: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
