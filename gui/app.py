"""Beheerscherm voor het testregister (Streamlit).

    streamlit run gui/app.py          # lokaal, http://localhost:8501
    docker compose up -d gui          # in Docker

Pagina's: Beoordelen (vraag, verwacht antwoord en modelantwoord naast elkaar), Testcases, Testruns, Modellen,
Overzicht. Verwijderen is altijd soft delete (terug te halen via 'Toon verwijderde'). Runs starten en de
LLM-beoordelaar draaien gebeurt via de command line.
"""

import os
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gui.paginas import toegang  # noqa: E402

st.set_page_config(page_title="Testregister", page_icon="⚖️", layout="wide")

if toegang():
    if "gebruiker" not in st.session_state:
        st.session_state.gebruiker = os.getenv("ICTRECHT_TESTER", "")
    with st.sidebar:
        # Widgetwaarde apart bewaren: Streamlit ruimt widget-state op bij het wisselen van pagina.
        naam = st.text_input("Jouw naam (Beoordelaar / Opgesteld door)", value=st.session_state.gebruiker)
        st.session_state.gebruiker = naam
    st.navigation([
        st.Page("paginas/beoordelen.py", title="Beoordelen", icon="⚖️", default=True),
        st.Page("paginas/testcases.py", title="Testcases", icon="📋"),
        st.Page("paginas/testruns.py", title="Testruns", icon="▶️"),
        st.Page("paginas/modellen.py", title="Modellen", icon="🤖"),
        st.Page("paginas/overzicht.py", title="Overzicht", icon="📊"),
    ]).run()
