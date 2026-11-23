"""Pagina's van het beheerscherm. Elke pagina is een functie; gui/paginas/*.py roept ze aan."""

from __future__ import annotations

import datetime as dt
import functools
import hmac
import io
import os
from collections import Counter

import pandas as pd
import streamlit as st
from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from db.models import (BLIND, DEFINITIEF, DIMENSIE_VELDEN, DIMENSIES, KLAD, KO_CODES, LLM, MENS, MODEL_STATUSSEN,
                       OVERGENOMEN, SCORE_KEUZES, TC_STATUSSEN, TESTTYPES, Beoordeling, Model, Testcase, Testrun, nu,
                       score_waarde, tel_tokens)
from db.opslag import (ALLEEN_GOEDGEKEURD, instelling, laad_koppelingen, volgende_run_id, volgende_tc_id,
                       zet_instelling)
from db.sessie import open_sessie
from register.eindoordeel import ONVOLDOENDE, TWIJFEL, VOLDOENDE, eindoordeel
from register.xlsxpatch import MAX_CELTEKST
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


# --- beoordelingsstatus van een run (Beoordelen en Testruns) -----------------------

ST_NIET, ST_LLM, ST_KLAD, ST_DEF, ST_AFWIJKING = ("⚪ niet beoordeeld", "🤖 alleen LLM", "✏️ klad jurist",
                                                 "✅ definitief", "⚠️ jurist ≠ LLM")
RUN_STATUSSEN = [ST_NIET, ST_LLM, ST_KLAD, ST_DEF, ST_AFWIJKING]


def status_run(r: Testrun) -> str:
    mens, llm = r.beoordeling(MENS), r.beoordeling(LLM)
    if mens and mens.definitief:
        if llm and llm.eindoordeel and mens.eindoordeel and llm.eindoordeel != mens.eindoordeel:
            return ST_AFWIJKING
        return ST_DEF
    if mens:
        return ST_KLAD
    return ST_LLM if llm else ST_NIET


def kort(b: Beoordeling | None) -> str:
    """Scores compact, bijv. 'J2 V1 B3 U2 G–' (U = bruikbaarheid; – = n.v.t., · = leeg)."""
    if not b:
        return ""
    tekens = {None: "·", "n.v.t.": "–"}
    waarden = (getattr(b, v) for v in DIMENSIE_VELDEN.values())
    return " ".join(f"{letter}{tekens.get(w, w)}" for letter, w in zip("JVBUG", waarden))


# --- Beoordelen ------------------------------------------------------------------

TOON_OPEN, TOON_ALLE = "Nog niet definitief door een jurist", "Alle runs"
KOLOMMEN_SCORE = [2, 1, 5, 1]  # dimensie | LLM | jurist | verschil


def tokens_tekst(t: dict | None) -> str:
    """'1.445 in · 122 uit (0 redenering)' of '' als het tokengebruik onbekend is."""
    if not t:
        return ""
    tekst = f"{t['in']:,} in · {t['uit']:,} uit".replace(",", ".")
    return tekst + (f" (waarvan {t['redenering']:,} redenering)".replace(",", ".") if t["redenering"] else "")


def blind() -> bool:
    return bool(st.session_state.get("blind"))


def bepaal_werkwijze(vorige: Beoordeling | None, llm_gezien: bool, velden: dict, ko: str, ko_code: str) -> str:
    """Werkwijze van een nieuwe jurist-beoordeling. Ongewijzigde scores en K.O. houden de werkwijze van de vorige
    versie (een typfout in de toelichting herstellen maakt een blinde beoordeling niet ineens 'overgenomen')."""
    if vorige and all(getattr(vorige, v) == velden[v] for v in velden) and (vorige.ko, vorige.ko_code) == (ko, ko_code):
        return vorige.werkwijze
    if (vorige and vorige.werkwijze == OVERGENOMEN) or llm_gezien:
        return OVERGENOMEN
    return BLIND


def _verschil(jurist: str | None, llm: str | None) -> str:
    if jurist is None or llm is None or jurist == llm:
        return ""
    if "n.v.t." in (jurist, llm):
        return ":orange[≠]"
    return f":orange[{int(jurist) - int(llm):+d}]"


def _toon_llm_oordeel(llm: Beoordeling, sleutel: str, vorige: Beoordeling | None) -> list[str]:
    """Kernpunten, te verifiëren (als checklist) en toelichting van de LLM. Geeft de afgevinkte punten terug."""
    st.markdown(f"**Oordeel LLM** ({llm.beoordelaar or 'LLM'})")
    for kp in llm.kernpunten or []:
        icoon = {"aanwezig": "✅", "deels": "🟡", "afwezig": "❌"}.get(kp.get("oordeel"), "•")
        st.markdown(f"{icoon} kernpunt {kp.get('nr')}: **{kp.get('oordeel')}** — {kp.get('toelichting', '')}")
    afgevinkt = []
    if llm.te_verifieren:
        st.markdown("**Te verifiëren** (vink af wat je hebt nagekeken)")
        eerder = set((vorige.details or {}).get("geverifieerd", [])) if vorige else set()
        for i, punt in enumerate(llm.te_verifieren):
            if st.checkbox(punt, value=punt in eerder, key=f"{sleutel}-verif-{i}"):
                afgevinkt.append(punt)
    if llm.toelichting.strip():
        st.markdown("**Toelichting LLM**")
        st.text(llm.toelichting)
    for w in llm.waarschuwingen or []:
        st.caption(f"⚠ {w}")
    return afgevinkt


def pagina_beoordelen():
    st.title("Beoordelen")
    if not gebruiker():
        st.info("Vul links je naam in; die komt als Beoordelaar bij je scores.")
    rubric_tekst, verwacht = rubric()
    with open_sessie() as s:
        alle = list(s.scalars(select(Testrun).where(Testrun.verwijderd_op.is_(None))
                              .options(selectinload(Testrun.beoordelingen), selectinload(Testrun.testcase))
                              .order_by(Testrun.run_id)))
        if not alle:
            st.info("Nog geen testruns. Draai eerst `python testregister.py run …`.")
            return
        if "open_run" in st.session_state:  # vanuit Testruns: filters zo zetten dat deze run zichtbaar is
            doel = next((r for r in alle if r.run_id == st.session_state.pop("open_run")), None)
            if doel:
                st.session_state.update(beo_model=doel.model_id, beo_versie="(alle)", beo_toon=TOON_ALLE,
                                        run_id=doel.run_id)
        modellen = sorted({r.model_id for r in alle})
        if st.session_state.get("beo_model") not in modellen:
            st.session_state.beo_model = modellen[0]
        k1, k2, k3 = st.columns([1, 1, 2])
        model = k1.selectbox("Model", modellen, key="beo_model")
        runs = [r for r in alle if r.model_id == model]
        versies = ["(alle)"] + sorted({r.modelversie for r in runs})
        if st.session_state.get("beo_versie") not in versies:
            st.session_state.beo_versie = "(alle)"
        versie = k2.selectbox("Versie", versies, key="beo_versie")
        if versie != "(alle)":
            runs = [r for r in runs if r.modelversie == versie]
        filter_ = k3.radio("Toon", [TOON_OPEN, TOON_ALLE], horizontal=True, key="beo_toon")
        if filter_ == TOON_OPEN:
            runs = [r for r in runs if not r.beoordeling(MENS, alleen_definitief=True)]
        if not runs:
            st.success("Alle runs in deze selectie zijn definitief door een jurist beoordeeld.")
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
        labels = {r.run_id: f"{status_run(r)} · {r.run_id} · {r.tc_id} · {r.testcase.testtype} · {r.modelversie} "
                            f"#{r.herhaling}" for r in runs}
        n2.selectbox("Run", ids, key="run_id", format_func=labels.get, label_visibility="collapsed")
        run = next(r for r in runs if r.run_id == st.session_state.run_id)
        tc = run.testcase
        st.caption(f"{i + 1} van {len(ids)} · {tc.onderwerp} · gedraaid {run.datum:%d-%m-%Y} door "
                   f"{run.uitgevoerd_door} ({run.uitvoerwijze}) · status testcase: {tc.status}")

        links, midden, rechts = st.columns(3)
        with links:
            st.subheader("Vraag")
            with st.container(height=480):
                st.markdown(f"**{tc.testtype}** — {verwacht.get(tc.testtype, '')}")
                st.markdown("**Prompt**")
                st.text(tc.prompt)
                if tc.context.strip():
                    st.markdown("**Context / bijlage**")
                    st.text(tc.context)
        with midden:
            st.subheader("Verwacht antwoord")
            with st.container(height=480):
                st.markdown("**Kernpunten**")
                st.text(tc.kernpunten or "(geen kernpunten)")
                st.markdown("**Verwacht gedrag**")
                st.text(tc.verwacht_gedrag or "–")
                st.markdown("**Verwachte bronnen**")
                st.text(tc.verwachte_bronnen or "–")
                if tc.peildatum:
                    st.caption(f"Peildatum {tc.peildatum:%d-%m-%Y}")
        with rechts:
            modelregel = s.scalar(select(Model).where(Model.model_id == run.model_id, Model.versie == run.modelversie,
                                                      Model.verwijderd_op.is_(None)))
            modelnaam = (modelregel.naam if modelregel else "").strip() or run.model_id
            st.subheader(f"Antwoord {modelnaam} {run.modelversie}".strip())
            with st.container(height=480):
                st.markdown(run.antwoord or "_(leeg)_")
            if run.tokens:
                st.caption(f"Tokens: {tokens_tekst(run.tokens)}")
        with st.expander("Rubric"):
            st.markdown(rubric_tekst)

        # --- beoordeling: LLM en jurist naast elkaar ---
        llm, vorige = run.beoordeling(LLM), run.beoordeling(MENS)
        # Blind: LLM-oordeel pas tonen als de jurist definitief heeft gescoord.
        llm_zichtbaar = llm is not None and not (blind() and not (vorige and vorige.definitief))
        gezien = st.session_state.setdefault("llm_gezien", set())
        if llm_zichtbaar:
            gezien.add(run.run_id)
        bron = vorige or (llm if llm_zichtbaar else None)  # waarmee het formulier start
        sleutel = f"{run.run_id}-{'blind' if blind() else 'review'}"

        titel = "Beoordeling"
        if vorige:
            titel += f" — {'definitief' if vorige.definitief else 'klad'} van {vorige.beoordelaar}"
            if vorige.werkwijze:
                titel += f" ({vorige.werkwijze})"
        elif bron is llm and llm:
            titel += " — klad overgenomen van de LLM"
        st.subheader(titel)
        if blind() and run.run_id in gezien and not (vorige and vorige.definitief):
            st.warning("Je hebt het LLM-oordeel van deze run in deze sessie al gezien; je beoordeling telt daarom "
                       "niet als blind. Zet Blind beoordelen aan vóórdat je een run opent.")
        elif llm and not llm_zichtbaar:
            st.info("Blind beoordelen: het LLM-oordeel verschijnt nadat je definitief hebt gemaakt. "
                    "Zo telt je beoordeling mee voor de kalibratie.")
        elif not llm:
            st.caption("Nog geen LLM-oordeel voor deze run (`python testregister.py beoordeel …`).")

        kop = st.columns(KOLOMMEN_SCORE)
        kop[0].markdown("**Dimensie**")
        kop[1].markdown("**LLM**")
        kop[2].markdown("**Jurist**")
        scores = {}
        for d, veld in DIMENSIE_VELDEN.items():
            rij = st.columns(KOLOMMEN_SCORE, vertical_alignment="center")
            rij[0].markdown(d)
            llm_w = getattr(llm, veld) if llm_zichtbaar else None
            rij[1].markdown(llm_w or "–" if llm_zichtbaar else "🙈")
            huidig = getattr(bron, veld) if bron else None
            scores[veld] = rij[2].radio(d, SCORE_KEUZES, index=SCORE_KEUZES.index(huidig) if huidig else None,
                                        horizontal=True, label_visibility="collapsed", key=f"{sleutel}-{veld}")
            if llm_zichtbaar:
                rij[3].markdown(_verschil(scores[veld], llm_w))
        rij = st.columns(KOLOMMEN_SCORE, vertical_alignment="center")
        rij[0].markdown("K.O.")
        if llm_zichtbaar:
            rij[1].markdown(f":red[J {llm.ko_code}]" if llm.ko == "J" else "N")
        else:
            rij[1].markdown("🙈")
        ko_kol, code_kol = rij[2].columns([1, 3], vertical_alignment="center")
        ko = ko_kol.toggle("K.O.", value=bool(bron and bron.ko == "J"), key=f"{sleutel}-ko")
        codes = [""] + KO_CODES
        code = code_kol.selectbox("K.O.-code", codes, label_visibility="collapsed",
                                  index=codes.index(bron.ko_code) if bron and bron.ko_code in codes else 0,
                                  key=f"{sleutel}-ko-code", disabled=not ko)
        ko_jn, code = ("J", code) if ko else ("N", "")
        if llm_zichtbaar and (ko_jn, code) != (llm.ko, llm.ko_code if llm.ko == "J" else ""):
            rij[3].markdown(":orange[≠]")
        eind = eindoordeel({d: score_waarde(scores[v]) for d, v in DIMENSIE_VELDEN.items()}, ko_jn)
        rij = st.columns(KOLOMMEN_SCORE, vertical_alignment="center")
        rij[0].markdown("**Eindoordeel**")
        rij[1].markdown(llm.eindoordeel or "–" if llm_zichtbaar else "🙈")
        rij[2].markdown(f"**{eind}**" if eind else "_nog niet alle scores ingevuld_")
        if llm_zichtbaar and eind and llm.eindoordeel and eind != llm.eindoordeel:
            rij[3].markdown(":orange[≠]")

        links, rechts = st.columns(2)
        with links:
            afgevinkt = _toon_llm_oordeel(llm, sleutel, vorige) if llm_zichtbaar else []
        with rechts:
            toelichting = st.text_area("Jouw toelichting", value=vorige.toelichting if vorige else "", height=160,
                                       key=f"{sleutel}-toelichting")

        b1, b2, _ = st.columns([1, 1, 3])
        klad = b1.button("Opslaan als klad", width="stretch")
        definitief = b2.button("Definitief maken", type="primary", width="stretch")
        if klad or definitief:
            fout = None
            if not gebruiker():
                fout = "Vul eerst links je naam in."
            elif definitief and any(v is None for v in scores.values()):
                fout = "Vul voor definitief alle vijf scores in (of sla op als klad)."
            elif definitief and ko and not code:
                fout = "Kies bij een K.O. de K.O.-code."
            if fout:
                st.error(fout)
            else:
                werkwijze = bepaal_werkwijze(vorige, run.run_id in gezien, scores, ko_jn, code)
                if vorige:
                    vorige.verwijderd_op = nu()
                b = Beoordeling(run_id=run.run_id, type=MENS, beoordelaar=gebruiker(), ko=ko_jn, ko_code=code,
                                toelichting=toelichting, status=DEFINITIEF if definitief else KLAD,
                                werkwijze=werkwijze, details={"geverifieerd": afgevinkt} if afgevinkt else None)
                for veld, w in scores.items():
                    setattr(b, veld, w)
                b.eindoordeel = eindoordeel(b.scores(), b.ko)
                s.add(b)
                s.commit()
                melding = f"{run.run_id} {'definitief' if definitief else 'als klad'} opgeslagen"
                if b.eindoordeel:
                    melding += f": {b.eindoordeel}"
                if definitief and llm and not llm_zichtbaar:
                    melding += f" · LLM gaf: {llm.eindoordeel or '–'}"
                st.session_state.melding = melding
                if definitief and filter_ == TOON_OPEN and i + 1 < len(ids):
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

RUN_KOLOMMEN = ["Verwijderd", "Run-ID", "TC-ID", "Testtype", "Model-ID", "Modelversie", "Datum", "Herhaling #",
                "Status", "Jurist", "Eindoordeel jurist", "LLM", "Eindoordeel LLM", "K.O.", "Tokens", "Tokens LLM",
                "Uitgevoerd door", "Uitvoerwijze", "Antwoord model"]


def _cel(v):
    """Waarde geschikt voor een Excel-cel: geen ongeldige tekens, niet langer dan de cellimiet."""
    if isinstance(v, str):
        v = ILLEGAL_CHARACTERS_RE.sub("", v)
        return v[:MAX_CELTEKST - 40] + " […afgekapt]" if len(v) > MAX_CELTEKST else v
    return v


def testruns_excel(runs: list[Testrun], filters: dict[str, str], door: str = "") -> bytes:
    """Excel-bestand met de (gefilterde) runs: per run de scores van jurist en LLM in aparte kolommen en het
    volledige antwoord. Tabblad 'Filters' legt vast met welke selectie het bestand is gemaakt."""
    koppen = ["Run-ID", "TC-ID", "Testtype", "Model-ID", "Modelversie", "API-model", "Datum", "Herhaling #",
              "Status", "Verwijderd",
              *(f"{d} jurist" for d in DIMENSIES), "K.O. jurist", "Eindoordeel jurist", "Beoordelaar",
              "Status jurist", "Toelichting jurist",
              *(f"{d} LLM" for d in DIMENSIES), "K.O. LLM", "Eindoordeel LLM", "Toelichting LLM",
              "Tokens", "Tokens LLM", "Duur (s)", "Uitgevoerd door", "Uitvoerwijze", "Antwoord model"]
    wb = Workbook()
    ws = wb.active
    ws.title = "Testruns"
    ws.append(koppen)
    for r in runs:
        mens, llm = r.beoordeling(MENS), r.beoordeling(LLM)

        def oordeel(b: Beoordeling | None, met_beoordelaar: bool) -> list:
            if not b:
                return [None] * (len(DIMENSIES) + (5 if met_beoordelaar else 3))
            ko = (b.ko_code or "J") if b.ko == "J" else b.ko
            extra = [b.beoordelaar, b.status] if met_beoordelaar else []
            return [*b.scores().values(), ko, b.eindoordeel, *extra, b.toelichting]

        ws.append([_cel(v) for v in (
            r.run_id, r.tc_id, r.testcase.testtype, r.model_id, r.modelversie, r.api_model, r.datum, r.herhaling,
            status_run(r), "ja" if r.verwijderd else "", *oordeel(mens, True), *oordeel(llm, False),
            (r.tokens or {}).get("totaal"), ((llm.tokens if llm else None) or {}).get("totaal"),
            round(r.duur_s, 1) if r.duur_s else None, r.uitgevoerd_door, r.uitvoerwijze, r.antwoord)])
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = ws.dimensions
    for i, kop in enumerate(koppen, 1):
        ws.cell(1, i).font = Font(bold=True)
        breed = 80 if kop == "Antwoord model" else 50 if kop.startswith("Toelichting") else max(10, len(kop) + 2)
        ws.column_dimensions[get_column_letter(i)].width = breed
    for (cel,) in ws.iter_rows(min_row=2, min_col=koppen.index("Datum") + 1, max_col=koppen.index("Datum") + 1):
        cel.number_format = "DD-MM-YYYY"

    wf = wb.create_sheet("Filters")
    wf.append(["Filter", "Waarde"])
    for k, v in {**filters, "Aantal runs": len(runs),
                 "Geëxporteerd op": dt.datetime.now().strftime("%d-%m-%Y %H:%M"),
                 "Geëxporteerd door": door}.items():
        wf.append([k, v])
    wf["A1"].font = wf["B1"].font = Font(bold=True)
    wf.column_dimensions["A"].width, wf.column_dimensions["B"].width = 20, 50

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def testruns_excel_uit_db(run_ids: list[str], filters: dict[str, str], door: str, url: str | None = None) -> bytes:
    """Laadt de runs opnieuw in een eigen sessie: de download-callback draait pas bij de klik, in een aparte thread,
    als de sessie van de pagina al gesloten is."""
    with open_sessie(url) as s:
        per_id = {r.run_id: r for r in s.scalars(
            select(Testrun).where(Testrun.run_id.in_(run_ids))
            .options(selectinload(Testrun.beoordelingen), selectinload(Testrun.testcase)))}
        return testruns_excel([per_id[i] for i in run_ids if i in per_id], filters, door)


def pagina_testruns():
    st.title("Testruns")
    with open_sessie() as s:
        alle = list(s.scalars(select(Testrun).options(selectinload(Testrun.beoordelingen),
                                                      selectinload(Testrun.testcase)).order_by(Testrun.run_id)))
        k1, k2, k3, k4, k5 = st.columns([1, 1, 2, 1, 1])
        model = k1.selectbox("Model", ["(alle)"] + sorted({r.model_id for r in alle}))
        runs = [r for r in alle if model == "(alle)" or r.model_id == model]
        versie = k2.selectbox("Versie", ["(alle)"] + sorted({r.modelversie for r in runs}))
        statussen = k3.multiselect("Status", RUN_STATUSSEN, placeholder="(alle)")
        oordeel = k4.selectbox("Eindoordeel", ["(alle)", VOLDOENDE, TWIJFEL, ONVOLDOENDE],
                               help="Van de jurist (definitief), anders van de LLM")
        verwijderd = k5.toggle("Toon verwijderde")

        def telt(r):
            b = r.beoordeling(MENS, alleen_definitief=True) or r.beoordeling(LLM)
            return b.eindoordeel if b else ""

        runs = [r for r in runs if (versie == "(alle)" or r.modelversie == versie) and (verwijderd or not r.verwijderd)
                and (not statussen or status_run(r) in statussen) and (oordeel == "(alle)" or telt(r) == oordeel)]

        def ko(r):
            paren = (("jurist", r.beoordeling(MENS)), ("LLM", r.beoordeling(LLM)))
            delen = [f"{wie} {b.ko_code or 'J'}" for wie, b in paren if b and b.ko == "J"]
            return "⛔ " + "; ".join(delen) if delen else ""

        rijen = []
        for r in runs:
            mens, llm = r.beoordeling(MENS), r.beoordeling(LLM)
            rijen.append({
                "Verwijderd": r.verwijderd, "Run-ID": r.run_id, "TC-ID": r.tc_id, "Testtype": r.testcase.testtype,
                "Model-ID": r.model_id, "Modelversie": r.modelversie, "Datum": r.datum, "Herhaling #": r.herhaling,
                "Status": status_run(r), "Jurist": kort(mens), "Eindoordeel jurist": mens.eindoordeel if mens else "",
                "LLM": kort(llm), "Eindoordeel LLM": llm.eindoordeel if llm else "", "K.O.": ko(r),
                "Tokens": (r.tokens or {}).get("totaal"), "Tokens LLM": ((llm.tokens if llm else None) or {}).get("totaal"),
                "Uitgevoerd door": r.uitgevoerd_door, "Uitvoerwijze": r.uitvoerwijze,
                "Antwoord model": (r.antwoord or "")[:300]})
        df = pd.DataFrame(rijen, columns=RUN_KOLOMMEN)
        bewerkt = st.data_editor(
            df, hide_index=True, width="stretch", height=480,
            key=f"runs-{model}-{versie}-{statussen}-{oordeel}-{verwijderd}",
            disabled=[c for c in df.columns if c not in ("Verwijderd", "Modelversie", "Uitgevoerd door")],
            column_config={"Verwijderd": st.column_config.CheckboxColumn(width="small"),
                           "Datum": st.column_config.DateColumn(format="DD-MM-YYYY"),
                           "Herhaling #": st.column_config.NumberColumn(width="small"),
                           "Jurist": st.column_config.TextColumn(help="J(uistheid) V(olledigheid) B(ronnen) "
                                                                 "U (bruikbaarheid) G(edrag); – = n.v.t."),
                           "LLM": st.column_config.TextColumn(help="Scores van de LLM-beoordelaar, zelfde volgorde"),
                           "Tokens": st.column_config.NumberColumn(help="Totaal tokens (in + uit) van de API-call; "
                                                                   "leeg bij handmatige of geïmporteerde runs"),
                           "Tokens LLM": st.column_config.NumberColumn(help="Totaal tokens van de LLM-beoordelaar "
                                                                       "(incl. herkansing)"),
                           "Antwoord model": st.column_config.TextColumn(width="large")})
        st.caption(f"{len(runs)} run(s). Antwoord ingekort; het volledige antwoord staat bij Details en Beoordelen. "
                   "Een klad van de jurist telt nog niet mee in Overzicht, kalibratie en export.")
        k1, k2, _ = st.columns([1, 1, 3])
        if k1.button("Wijzigingen opslaan", type="primary", width="stretch"):
            n = bewaar_wijzigingen(s, {r.run_id: r for r in runs}, df, bewerkt, "Run-ID",
                                   {"Modelversie": "modelversie", "Uitgevoerd door": "uitgevoerd_door"})
            s.commit()
            st.success(f"{n} run(s) bijgewerkt")
        filters = {"Model": model, "Versie": versie, "Status": ", ".join(statussen) or "(alle)",
                   "Eindoordeel": oordeel, "Verwijderde runs": "meegenomen" if verwijderd else "niet meegenomen"}
        k2.download_button(
            "Exporteren naar Excel", on_click="ignore",
            data=functools.partial(testruns_excel_uit_db, [r.run_id for r in runs], filters, gebruiker()),
            file_name=f"testruns_{dt.datetime.now():%Y%m%d_%H%M}.xlsx", disabled=not runs, width="stretch",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            help="Exporteert de runs die nu in de tabel staan (met de filters hierboven), met volledige antwoorden "
                 "en de scores van jurist en LLM per dimensie. Niet-opgeslagen wijzigingen in de tabel gaan niet mee.")

        if runs:
            with st.expander("Details van een run"):
                r = next(x for x in runs if x.run_id == st.selectbox("Run", [x.run_id for x in runs]))
                k1, k2 = st.columns([4, 1])
                k1.markdown(f"**{r.run_id}** · {r.tc_id} · {r.model_id} {r.modelversie} · "
                            f"API-model `{r.api_model or '–'}`"
                            + (f" · {r.duur_s:.1f} s" if r.duur_s else "")
                            + (f" · tokens {tokens_tekst(r.tokens)}" if r.tokens else ""))
                if k2.button("Openen in Beoordelen", disabled=r.verwijderd, width="stretch"):
                    st.session_state.open_run = r.run_id
                    st.switch_page("paginas/beoordelen.py")
                st.markdown(r.antwoord)
                bs = [b for b in r.beoordelingen]
                if bs:
                    st.markdown("**Beoordelingen** (verwijderde zijn eerdere versies)")
                    st.dataframe(pd.DataFrame([{"Type": b.type, "Beoordelaar": b.beoordelaar, "Status": b.status,
                                                "Werkwijze": b.werkwijze, **b.scores(), "K.O.": b.ko,
                                                "K.O.-code": b.ko_code, "Eindoordeel": b.eindoordeel,
                                                "Tokens": (b.tokens or {}).get("totaal"),
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
    st.caption("Per modelversie: eindoordeel van de jurist (definitief), anders van de LLM-beoordelaar. "
               "Kalibratie: alleen runs die de jurist blind (zonder de LLM-scores) definitief beoordeelde.")
    with open_sessie() as s:
        runs = list(s.scalars(select(Testrun).where(Testrun.verwijderd_op.is_(None))
                              .options(selectinload(Testrun.beoordelingen), selectinload(Testrun.testcase))))
        rijen = {}
        for r in runs:
            sleutel = (r.model_id, r.modelversie)
            rij = rijen.setdefault(sleutel, Counter())
            rij["Runs"] += 1
            mens, llm = r.beoordeling(MENS, alleen_definitief=True), r.beoordeling(LLM)
            rij["Door jurist"] += bool(mens)
            rij["Klad jurist"] += bool(not mens and r.beoordeling(MENS))
            rij["Door LLM"] += bool(llm)
            b = mens or llm
            if b and b.eindoordeel:
                rij[b.eindoordeel] += 1
                if b.ko == "J":
                    rij["K.O."] += 1
            if mens and llm and mens.eindoordeel and llm.eindoordeel and mens.werkwijze != OVERGENOMEN:
                rij["Kalibratie n"] += 1
                rij["Kalibratie gelijk"] += mens.eindoordeel == llm.eindoordeel
        if not rijen:
            st.info("Nog geen testruns.")
            return
        tabel = []
        for (m, v), c in sorted(rijen.items()):
            beoordeeld = c["Voldoende"] + c["Twijfel"] + c["Onvoldoende"]
            tabel.append({"Model-ID": m, "Versie": v, "Runs": c["Runs"], "Door jurist": c["Door jurist"],
                          "Klad jurist": c["Klad jurist"],
                          "Door LLM": c["Door LLM"], "Voldoende": c["Voldoende"], "Twijfel": c["Twijfel"],
                          "Onvoldoende": c["Onvoldoende"], "K.O.": c["K.O."],
                          "% voldoende": round(100 * c["Voldoende"] / beoordeeld) if beoordeeld else None,
                          "Kalibratie n": c["Kalibratie n"],
                          "Kalibratie (% gelijk)": round(100 * c["Kalibratie gelijk"] / c["Kalibratie n"])
                          if c["Kalibratie n"] else None})
        st.dataframe(pd.DataFrame(tabel), hide_index=True, width="stretch")
        tokens_per_dag(s)


def lokale_datum(t: dt.datetime | None) -> dt.date | None:
    if t is None:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)).astimezone().date()


def tokens_per_dag(s) -> None:
    """Tokengebruik per dag: testruns (op rundatum) en LLM-beoordelingen (op tijdstip van beoordelen).
    Verwijderde runs en vervangen beoordelingen tellen mee: die calls zijn wel gedaan."""
    st.subheader("Tokens per dag")
    dagen: dict[dt.date, dict] = {}

    def tel(dag, soort, t):
        if dag is None or not t:
            return
        rij = dagen.setdefault(dag, {"runs": [], "llm": [], "n_runs": 0, "n_llm": 0})
        rij[soort].append(t)
        rij[f"n_{soort}"] += 1

    for r in s.scalars(select(Testrun).where(Testrun.response.is_not(None))):
        tel(lokale_datum(r.start) or r.datum, "runs", r.tokens)
    for b in s.scalars(select(Beoordeling).where(Beoordeling.type == LLM)):
        tel(lokale_datum(b.aangemaakt_op), "llm", b.tokens)
    if not dagen:
        st.caption("Nog geen tokengebruik bekend (alleen API-runs en LLM-beoordelingen hebben tokens).")
        return
    tabel = []
    for dag, rij in sorted(dagen.items(), reverse=True):
        runs, llm = tel_tokens(*rij["runs"]) or {}, tel_tokens(*rij["llm"]) or {}
        tabel.append({"Datum": dag, "API-runs": rij["n_runs"], "Tokens in (runs)": runs.get("in", 0),
                      "Tokens uit (runs)": runs.get("uit", 0), "Beoordelingen LLM": rij["n_llm"],
                      "Tokens in (LLM)": llm.get("in", 0), "Tokens uit (LLM)": llm.get("uit", 0),
                      "Totaal": runs.get("totaal", 0) + llm.get("totaal", 0)})
    df = pd.DataFrame(tabel)
    st.dataframe(df, hide_index=True, width="stretch",
                 column_config={"Datum": st.column_config.DateColumn(format="DD-MM-YYYY")})
    st.caption(f"Totaal {int(df['Totaal'].sum()):,} tokens. ".replace(",", ".")
               + "Uit = antwoord incl. redeneertokens. Verwijderde runs en vervangen LLM-oordelen tellen mee, "
               "want die calls zijn wel gedaan. LLM-beoordelingen van vóór deze versie hebben geen tokens.")
