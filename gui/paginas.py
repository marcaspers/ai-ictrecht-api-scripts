"""Pagina's van het beheerscherm. Elke pagina is een functie; gui/paginas/*.py roept ze aan."""

from __future__ import annotations

import datetime as dt
import hmac
import os
from collections import Counter

import pandas as pd
import streamlit as st
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from db.models import (KO_CODES, LLM, MENS, MODEL_STATUSSEN, SCORE_KEUZES, TC_STATUSSEN, TESTTYPES, Beoordeling,
                       Model, Testcase, Testrun, nu)
from db.opslag import (ALLEEN_GOEDGEKEURD, instelling, laad_koppelingen, volgende_run_id, volgende_tc_id,
                       zet_instelling)
from db.sessie import open_sessie
from register.eindoordeel import eindoordeel
from testrunner.beoordelen import RUBRIC_PAD, verwachtingen


# --- algemeen -------------------------------------------------------------------

def toegang() -> bool:
    """Optioneel wachtwoord (GUI_WACHTWOORD in .env), bedoeld voor een centrale installatie."""
    wachtwoord = os.getenv("GUI_WACHTWOORD")
    if not wachtwoord or st.session_state.get("ingelogd"):
        return True
    invoer = st.text_input("Wachtwoord", type="password")
    if invoer and hmac.compare_digest(invoer, wachtwoord):
        st.session_state.ingelogd = True
        st.rerun()
    elif invoer:
        st.error("Onjuist wachtwoord")
    return False


def gebruiker() -> str:
    return st.session_state.get("gebruiker", "").strip()


def leeg(v) -> bool:
    try:
        return v is None or bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def als_datum(v) -> dt.date | None:
    if leeg(v):
        return None
    if isinstance(v, pd.Timestamp):
        return v.date()
    if isinstance(v, dt.datetime):
        return v.date()
    return v


def als_tekst(v) -> str:
    return "" if leeg(v) else str(v)


def bewaar_wijzigingen(sessie, objecten: dict, origineel: pd.DataFrame, bewerkt: pd.DataFrame, sleutel: str,
                       velden: dict[str, str], datumvelden: set[str] = frozenset()) -> int:
    """Vergelijk de tabel vóór en na bewerken en schrijf verschillen naar de objecten.
    ``velden``: kolomkop -> attribuut. Kolom 'Verwijderd' zet of wist verwijderd_op."""
    aantal = 0
    oud = origineel.set_index(sleutel)
    for _, rij in bewerkt.iterrows():
        k = rij[sleutel]
        if k not in objecten:
            continue
        obj, voor = objecten[k], oud.loc[k]
        gewijzigd = False
        for kop, attr in velden.items():
            nieuw = als_datum(rij[kop]) if attr in datumvelden else als_tekst(rij[kop])
            was = als_datum(voor[kop]) if attr in datumvelden else als_tekst(voor[kop])
            if nieuw != was:
                setattr(obj, attr, nieuw)
                gewijzigd = True
        if bool(rij["Verwijderd"]) != bool(voor["Verwijderd"]):
            obj.verwijderd_op = nu() if rij["Verwijderd"] else None
            gewijzigd = True
        aantal += gewijzigd
    return aantal


@st.cache_data
def rubric() -> tuple[str, dict[str, str]]:
    tekst = RUBRIC_PAD.read_text(encoding="utf-8")
    return tekst, verwachtingen(tekst)


# --- Beoordelen ------------------------------------------------------------------

def pagina_beoordelen():
    st.title("Beoordelen")
    if not gebruiker():
        st.info("Vul links je naam in; die komt als Beoordelaar bij je scores.")
    rubric_tekst, verwacht = rubric()
    with open_sessie() as s:
        runs = list(s.scalars(select(Testrun).where(Testrun.verwijderd_op.is_(None))
                              .options(selectinload(Testrun.beoordelingen), selectinload(Testrun.testcase))
                              .order_by(Testrun.run_id)))
        if not runs:
            st.info("Nog geen testruns. Draai eerst `python testregister.py run …`.")
            return
        k1, k2, k3 = st.columns([1, 1, 2])
        model = k1.selectbox("Model", sorted({r.model_id for r in runs}))
        runs = [r for r in runs if r.model_id == model]
        versie = k2.selectbox("Versie", ["(alle)"] + sorted({r.modelversie for r in runs}))
        if versie != "(alle)":
            runs = [r for r in runs if r.modelversie == versie]
        filter_ = k3.radio("Toon", ["Nog niet door een jurist beoordeeld", "Alle runs"], horizontal=True)
        if filter_.startswith("Nog"):
            runs = [r for r in runs if not r.beoordeling(MENS)]
        if not runs:
            st.success("Alle runs in deze selectie zijn door een jurist beoordeeld.")
            return

        ids = [r.run_id for r in runs]
        if "ga_naar" in st.session_state:  # gezet door knoppen; vóór het tekenen van de keuzelijst toepassen
            st.session_state.run_id = st.session_state.pop("ga_naar")
        if st.session_state.get("run_id") not in ids:
            st.session_state.run_id = ids[0]
        n1, n2, n3 = st.columns([1, 6, 1])
        i = ids.index(st.session_state.run_id)
        if n1.button("← Vorige", disabled=i == 0, width="stretch"):
            st.session_state.ga_naar = ids[i - 1]
            st.rerun()
        if n3.button("Volgende →", disabled=i == len(ids) - 1, width="stretch"):
            st.session_state.ga_naar = ids[i + 1]
            st.rerun()
        labels = {r.run_id: f"{r.run_id} · {r.tc_id} · {r.testcase.testtype} · {r.modelversie} #{r.herhaling}"
                  for r in runs}
        n2.selectbox("Run", ids, key="run_id", format_func=labels.get, label_visibility="collapsed")
        run = next(r for r in runs if r.run_id == st.session_state.run_id)
        tc = run.testcase
        st.caption(f"{i + 1} van {len(ids)} · {tc.onderwerp} · gedraaid {run.datum:%d-%m-%Y} door "
                   f"{run.uitgevoerd_door} ({run.uitvoerwijze}) · status testcase: {tc.status}")

        links, midden, rechts = st.columns(3)
        with links:
            st.subheader("Vraag")
            with st.container(height=560):
                st.markdown(f"**{tc.testtype}** — {verwacht.get(tc.testtype, '')}")
                st.markdown("**Prompt**")
                st.text(tc.prompt)
                if tc.context.strip():
                    st.markdown("**Context / bijlage**")
                    st.text(tc.context)
        with midden:
            st.subheader("Verwacht antwoord")
            with st.container(height=560):
                st.markdown("**Kernpunten**")
                st.text(tc.kernpunten or "(geen kernpunten)")
                st.markdown("**Verwacht gedrag**")
                st.text(tc.verwacht_gedrag or "–")
                st.markdown("**Verwachte bronnen**")
                st.text(tc.verwachte_bronnen or "–")
                if tc.peildatum:
                    st.caption(f"Peildatum {tc.peildatum:%d-%m-%Y}")
        with rechts:
            st.subheader("Antwoord model")
            with st.container(height=560):
                st.markdown(run.antwoord or "_(leeg)_")

        llm = run.beoordeling(LLM)
        if llm:
            with st.expander("Oordeel van de LLM-beoordelaar (voor de kalibratie eerst zelf scoren)"):
                st.write({**llm.scores(), "K.O.": f"{llm.ko} {llm.ko_code}".strip(), "Eindoordeel": llm.eindoordeel})
                st.text(llm.toelichting)
                if llm.te_verifieren:
                    st.markdown("**Te verifiëren:** " + "; ".join(llm.te_verifieren))
                for kp in llm.kernpunten or []:
                    st.markdown(f"- kernpunt {kp.get('nr')}: **{kp.get('oordeel')}** — {kp.get('toelichting', '')}")
        with st.expander("Rubric"):
            st.markdown(rubric_tekst)

        vorige = run.beoordeling(MENS)
        st.subheader("Jouw beoordeling" + (f" (bijwerken; nu door {vorige.beoordelaar})" if vorige else ""))
        with st.form(f"beoordeling-{run.run_id}"):
            kolommen = st.columns(5)
            scores = {}
            for kol, (d, veld) in zip(kolommen, [("Juistheid", "juistheid"), ("Volledigheid", "volledigheid"),
                                                 ("Bronnen", "bronnen"), ("Bruikbaarheid", "bruikbaarheid"),
                                                 ("Gedrag", "gedrag")]):
                huidig = getattr(vorige, veld) if vorige else None
                scores[d] = kol.radio(d, SCORE_KEUZES, index=SCORE_KEUZES.index(huidig) if huidig else None)
            c1, c2 = st.columns([1, 3])
            ko = c1.toggle("K.O.", value=bool(vorige and vorige.ko == "J"))
            code = c2.selectbox("K.O.-code", [""] + KO_CODES,
                                index=([""] + KO_CODES).index(vorige.ko_code) if vorige and vorige.ko_code in KO_CODES else 0)
            toelichting = st.text_area("Toelichting", value=vorige.toelichting if vorige else "")
            if st.form_submit_button("Opslaan", type="primary"):
                if not gebruiker():
                    st.error("Vul eerst links je naam in.")
                elif ko and not code:
                    st.error("Kies bij een K.O. de K.O.-code.")
                else:
                    if vorige:
                        vorige.verwijderd_op = nu()
                    b = Beoordeling(run_id=run.run_id, type=MENS, beoordelaar=gebruiker(), ko="J" if ko else "N",
                                    ko_code=code if ko else "", toelichting=toelichting)
                    b.zet_scores(scores)
                    b.eindoordeel = eindoordeel(b.scores(), b.ko)
                    s.add(b)
                    s.commit()
                    st.session_state.melding = f"{run.run_id} opgeslagen: {b.eindoordeel or 'nog niet alle scores ingevuld'}"
                    if filter_.startswith("Nog") and i + 1 < len(ids):
                        st.session_state.ga_naar = ids[i + 1]
                    st.rerun()
    if st.session_state.get("melding"):
        st.toast(st.session_state.pop("melding"))


# --- Testcases ----------------------------------------------------------------------

TC_KOLOMMEN = {"Model-ID": "model_id", "Testtype": "testtype", "Rechtsgebied": "rechtsgebied", "Onderwerp": "onderwerp",
               "Prompt": "prompt", "Context / bijlage": "context", "Kernpunten verwacht antwoord": "kernpunten",
               "Verwacht gedrag": "verwacht_gedrag", "Verwachte bronnen": "verwachte_bronnen",
               "Regressieset": "regressieset", "Herkomst": "herkomst", "Opgesteld door": "opgesteld_door",
               "Goedgekeurd door": "goedgekeurd_door", "Status": "status", "Peildatum": "peildatum",
               "Opmerkingen": "opmerkingen"}


def pagina_testcases():
    st.title("Testcases")
    with open_sessie() as s:
        alle = list(s.scalars(select(Testcase).order_by(Testcase.tc_id)))
        modellen = sorted({m.model_id for m in s.scalars(select(Model).where(Model.verwijderd_op.is_(None)))}
                          | {t.model_id for t in alle})
        k1, k2, k3 = st.columns([1, 2, 1])
        model = k1.selectbox("Model", ["(alle)"] + modellen)
        zoek = k2.text_input("Zoeken in TC-ID, onderwerp en prompt")
        verwijderd = k3.toggle("Toon verwijderde")
        tcs = [t for t in alle if (model == "(alle)" or t.model_id == model) and (verwijderd or not t.verwijderd)
               and (not zoek or zoek.lower() in f"{t.tc_id} {t.onderwerp} {t.prompt}".lower())]

        df = pd.DataFrame([{"Verwijderd": t.verwijderd, "TC-ID": t.tc_id,
                            **{kop: getattr(t, attr) for kop, attr in TC_KOLOMMEN.items()}} for t in tcs],
                          columns=["Verwijderd", "TC-ID", *TC_KOLOMMEN])
        bewerkt = st.data_editor(
            df, hide_index=True, width="stretch", height=560, key=f"tc-{model}-{zoek}-{verwijderd}",
            disabled=["TC-ID", "Model-ID", "Testtype"],
            column_config={
                "Verwijderd": st.column_config.CheckboxColumn(width="small"),
                "Testtype": st.column_config.TextColumn(width="small"),
                "Prompt": st.column_config.TextColumn(width="large"),
                "Kernpunten verwacht antwoord": st.column_config.TextColumn(width="large"),
                "Regressieset": st.column_config.SelectboxColumn(options=["J", "N"], width="small"),
                "Status": st.column_config.SelectboxColumn(options=TC_STATUSSEN),
                "Peildatum": st.column_config.DateColumn(format="DD-MM-YYYY"),
            })
        st.caption(f"{len(tcs)} testcase(s). TC-ID, Model-ID en Testtype liggen vast (het ID is daarvan afgeleid); "
                   "verkeerd type of model? Maak een nieuwe case en zet de oude op verwijderd.")
        if st.button("Wijzigingen opslaan", type="primary"):
            n = bewaar_wijzigingen(s, {t.tc_id: t for t in tcs}, df, bewerkt, "TC-ID",
                                   {k: v for k, v in TC_KOLOMMEN.items() if k not in ("Model-ID", "Testtype")},
                                   {"peildatum"})
            s.commit()
            st.success(f"{n} testcase(s) bijgewerkt")

        with st.expander("Nieuwe testcase"):
            with st.form("nieuwe-tc", clear_on_submit=True):
                c1, c2, c3 = st.columns(3)
                m = c1.selectbox("Model-ID", modellen)
                tt = c2.selectbox("Testtype", TESTTYPES)
                status = c3.selectbox("Status", TC_STATUSSEN)
                onderwerp = st.text_input("Onderwerp")
                rechtsgebied = st.text_input("Rechtsgebied")
                prompt = st.text_area("Prompt (precies zoals in te voeren)")
                context = st.text_area("Context / bijlage")
                kernpunten = st.text_area("Kernpunten verwacht antwoord (3–6 controleerbare punten met vindplaatsen)")
                gedrag = st.text_area("Verwacht gedrag")
                bronnen = st.text_area("Verwachte bronnen")
                c4, c5, c6 = st.columns(3)
                regressie = c4.selectbox("Regressieset", ["N", "J"])
                herkomst = c5.selectbox("Herkomst", ["Jurist", "Claude", "Praktijk"])
                peildatum = c6.date_input("Peildatum", value=dt.date.today(), format="DD-MM-YYYY")
                if st.form_submit_button("Toevoegen"):
                    if not prompt.strip():
                        st.error("Prompt is verplicht.")
                    else:
                        tc_id = volgende_tc_id(s, m, tt)
                        s.add(Testcase(tc_id=tc_id, model_id=m, testtype=tt, status=status, onderwerp=onderwerp,
                                       rechtsgebied=rechtsgebied, prompt=prompt.strip(), context=context.strip(),
                                       kernpunten=kernpunten.strip(), verwacht_gedrag=gedrag.strip(),
                                       verwachte_bronnen=bronnen.strip(), regressieset=regressie, herkomst=herkomst,
                                       opgesteld_door=gebruiker(), peildatum=peildatum))
                        s.commit()
                        st.success(f"Toegevoegd als {tc_id}")


# --- Testruns -----------------------------------------------------------------------

def pagina_testruns():
    st.title("Testruns")
    with open_sessie() as s:
        alle = list(s.scalars(select(Testrun).options(selectinload(Testrun.beoordelingen),
                                                      selectinload(Testrun.testcase)).order_by(Testrun.run_id)))
        k1, k2, k3 = st.columns([1, 1, 1])
        model = k1.selectbox("Model", ["(alle)"] + sorted({r.model_id for r in alle}))
        runs = [r for r in alle if model == "(alle)" or r.model_id == model]
        versie = k2.selectbox("Versie", ["(alle)"] + sorted({r.modelversie for r in runs}))
        verwijderd = k3.toggle("Toon verwijderde")
        runs = [r for r in runs if (versie == "(alle)" or r.modelversie == versie) and (verwijderd or not r.verwijderd)]

        def eind(r, t):
            b = r.beoordeling(t)
            return b.eindoordeel if b else ""

        df = pd.DataFrame([{
            "Verwijderd": r.verwijderd, "Run-ID": r.run_id, "TC-ID": r.tc_id, "Testtype": r.testcase.testtype,
            "Model-ID": r.model_id, "Modelversie": r.modelversie, "Datum": r.datum,
            "Uitgevoerd door": r.uitgevoerd_door, "Uitvoerwijze": r.uitvoerwijze, "Herhaling #": r.herhaling,
            "Eindoordeel jurist": eind(r, MENS), "Eindoordeel LLM": eind(r, LLM),
            "Antwoord model": (r.antwoord or "")[:300]} for r in runs],
            columns=["Verwijderd", "Run-ID", "TC-ID", "Testtype", "Model-ID", "Modelversie", "Datum", "Uitgevoerd door",
                     "Uitvoerwijze", "Herhaling #", "Eindoordeel jurist", "Eindoordeel LLM", "Antwoord model"])
        bewerkt = st.data_editor(
            df, hide_index=True, width="stretch", height=480, key=f"runs-{model}-{versie}-{verwijderd}",
            disabled=[c for c in df.columns if c not in ("Verwijderd", "Modelversie", "Uitgevoerd door")],
            column_config={"Verwijderd": st.column_config.CheckboxColumn(width="small"),
                           "Datum": st.column_config.DateColumn(format="DD-MM-YYYY"),
                           "Antwoord model": st.column_config.TextColumn(width="large")})
        st.caption(f"{len(runs)} run(s). Antwoord ingekort; het volledige antwoord staat bij Details en Beoordelen.")
        if st.button("Wijzigingen opslaan", type="primary"):
            n = bewaar_wijzigingen(s, {r.run_id: r for r in runs}, df, bewerkt, "Run-ID",
                                   {"Modelversie": "modelversie", "Uitgevoerd door": "uitgevoerd_door"})
            s.commit()
            st.success(f"{n} run(s) bijgewerkt")

        if runs:
            with st.expander("Details van een run"):
                r = next(x for x in runs if x.run_id == st.selectbox("Run", [x.run_id for x in runs]))
                st.markdown(f"**{r.run_id}** · {r.tc_id} · {r.model_id} {r.modelversie} · API-model `{r.api_model or '–'}`"
                            + (f" · {r.duur_s:.1f} s" if r.duur_s else ""))
                st.markdown(r.antwoord)
                bs = [b for b in r.beoordelingen]
                if bs:
                    st.markdown("**Beoordelingen** (verwijderde zijn eerdere versies)")
                    st.dataframe(pd.DataFrame([{"Type": b.type, "Beoordelaar": b.beoordelaar, **b.scores(), "K.O.": b.ko,
                                                "K.O.-code": b.ko_code, "Eindoordeel": b.eindoordeel,
                                                "Toelichting": b.toelichting, "Verwijderd": b.verwijderd,
                                                "Tijdstip": b.aangemaakt_op} for b in bs]), hide_index=True)
                if r.response:
                    with st.popover("Ruwe respons (auditspoor)"):
                        st.json(r.response, expanded=False)

        with st.expander("Handmatige run toevoegen (antwoord uit de OpenWebUI-interface)"):
            tcs = list(s.scalars(select(Testcase).where(Testcase.verwijderd_op.is_(None)).order_by(Testcase.tc_id)))
            with st.form("handmatig", clear_on_submit=True):
                tc_id = st.selectbox("TC-ID", [t.tc_id for t in tcs])
                mv = st.text_input("Modelversie", help="Exact zoals in Modellen, bijv. v1.0")
                antwoord = st.text_area("Antwoord model (volledig, ongewijzigd)", height=240)
                if st.form_submit_button("Toevoegen"):
                    tc = next(t for t in tcs if t.tc_id == tc_id)
                    versies = {m.versie for m in s.scalars(select(Model).where(
                        Model.model_id == tc.model_id, Model.verwijderd_op.is_(None))) if m.versie}
                    if not antwoord.strip():
                        st.error("Antwoord is verplicht.")
                    elif mv.strip() not in versies:
                        st.error(f"Modelversie '{mv}' staat niet in Modellen voor {tc.model_id} "
                                 f"(bekend: {', '.join(sorted(versies)) or 'geen'}).")
                    else:
                        vandaag = dt.date.today()
                        herhaling = max((r.herhaling for r in tc.runs if r.modelversie == mv and not r.verwijderd),
                                        default=0) + 1
                        run_id = volgende_run_id(s, vandaag)
                        s.add(Testrun(run_id=run_id, tc_id=tc.tc_id, model_id=tc.model_id, modelversie=mv.strip(),
                                      datum=vandaag, uitgevoerd_door=gebruiker(), uitvoerwijze="handmatig",
                                      herhaling=herhaling, antwoord=antwoord))
                        s.commit()
                        st.success(f"Toegevoegd als {run_id}")


# --- Modellen -------------------------------------------------------------------------

MODEL_KOLOMMEN = {"Model-ID": "model_id", "Modelnaam (OpenWebUI)": "naam", "Rechtsgebied / expertise": "rechtsgebied",
                  "Modelversie": "versie", "Basismodel": "basismodel", "Systeemprompt-versie": "systeemprompt_versie",
                  "Kennisbank": "kennisbank", "Tools": "tools", "Temperatuur": "temperatuur", "Eigenaar": "eigenaar",
                  "Datum versie": "datum_versie", "Status": "status", "Opmerkingen": "opmerkingen"}


def pagina_modellen():
    st.title("Modellen")
    st.caption("Eén regel per modelversie; elke wijziging aan een model is een nieuwe regel. Voeg onderaan een regel toe.")
    with open_sessie() as s:
        verwijderd = st.toggle("Toon verwijderde")
        alle = [m for m in s.scalars(select(Model).order_by(Model.model_id, Model.id)) if verwijderd or not m.verwijderd]
        koppelingen = laad_koppelingen(s)
        df = pd.DataFrame([{"Verwijderd": m.verwijderd, "id": m.id,
                            **{kop: getattr(m, attr) for kop, attr in MODEL_KOLOMMEN.items()},
                            "OpenWebUI-id (koppeling)": koppelingen.get(m.model_id, {}).get("id", "")} for m in alle],
                          columns=["Verwijderd", "id", *MODEL_KOLOMMEN, "OpenWebUI-id (koppeling)"])
        bewerkt = st.data_editor(
            df, hide_index=True, width="stretch", height=560, num_rows="dynamic", key=f"modellen-{verwijderd}",
            disabled=["id", "OpenWebUI-id (koppeling)"],
            column_config={"id": None, "Verwijderd": st.column_config.CheckboxColumn(width="small", default=False),
                           "Status": st.column_config.SelectboxColumn(options=MODEL_STATUSSEN),
                           "Datum versie": st.column_config.DateColumn(format="DD-MM-YYYY")})
        st.caption("Koppeling aan OpenWebUI wijzigen: `python testregister.py koppel --model <Model-ID>`.")
        if st.button("Wijzigingen opslaan", type="primary"):
            velden = dict(MODEL_KOLOMMEN)
            n = bewaar_wijzigingen(s, {m.id: m for m in alle}, df, bewerkt, "id", velden, {"datum_versie"})
            nieuw = bewerkt[bewerkt["id"].isna()]
            for _, rij in nieuw.iterrows():
                if not als_tekst(rij["Model-ID"]).strip():
                    continue
                m = Model(**{attr: (als_datum(rij[kop]) if attr == "datum_versie" else als_tekst(rij[kop]).strip())
                             for kop, attr in MODEL_KOLOMMEN.items()})
                s.add(m)
                n += 1
            try:
                s.commit()
                st.success(f"{n} modelregel(s) opgeslagen")
            except Exception as e:  # noqa: BLE001 - bijv. dubbele Model-ID + versie
                s.rollback()
                st.error(f"Niet opgeslagen: {e.__class__.__name__}. Bestaat de combinatie Model-ID + Modelversie al?")

        st.subheader("Instellingen")
        huidig = instelling(s, ALLEEN_GOEDGEKEURD) != "N"
        nieuw_ag = st.toggle("Run selecteert standaard alleen goedgekeurde testcases", value=huidig)
        if nieuw_ag != huidig:
            zet_instelling(s, ALLEEN_GOEDGEKEURD, "J" if nieuw_ag else "N")
            s.commit()
            st.rerun()


# --- Overzicht -----------------------------------------------------------------------

def pagina_overzicht():
    st.title("Overzicht")
    st.caption("Per modelversie: eindoordeel van de jurist, anders van de LLM-beoordelaar.")
    with open_sessie() as s:
        runs = list(s.scalars(select(Testrun).where(Testrun.verwijderd_op.is_(None))
                              .options(selectinload(Testrun.beoordelingen), selectinload(Testrun.testcase))))
        rijen = {}
        for r in runs:
            sleutel = (r.model_id, r.modelversie)
            rij = rijen.setdefault(sleutel, Counter())
            rij["Runs"] += 1
            mens, llm = r.beoordeling(MENS), r.beoordeling(LLM)
            rij["Door jurist"] += bool(mens)
            rij["Door LLM"] += bool(llm)
            b = mens or llm
            if b and b.eindoordeel:
                rij[b.eindoordeel] += 1
                if b.ko == "J":
                    rij["K.O."] += 1
            if mens and llm and mens.eindoordeel and llm.eindoordeel:
                rij["Kalibratie n"] += 1
                rij["Kalibratie gelijk"] += mens.eindoordeel == llm.eindoordeel
        if not rijen:
            st.info("Nog geen testruns.")
            return
        tabel = []
        for (m, v), c in sorted(rijen.items()):
            beoordeeld = c["Voldoende"] + c["Twijfel"] + c["Onvoldoende"]
            tabel.append({"Model-ID": m, "Versie": v, "Runs": c["Runs"], "Door jurist": c["Door jurist"],
                          "Door LLM": c["Door LLM"], "Voldoende": c["Voldoende"], "Twijfel": c["Twijfel"],
                          "Onvoldoende": c["Onvoldoende"], "K.O.": c["K.O."],
                          "% voldoende": round(100 * c["Voldoende"] / beoordeeld) if beoordeeld else None,
                          "Kalibratie (% gelijk)": round(100 * c["Kalibratie gelijk"] / c["Kalibratie n"])
                          if c["Kalibratie n"] else None})
        st.dataframe(pd.DataFrame(tabel), hide_index=True, width="stretch")
