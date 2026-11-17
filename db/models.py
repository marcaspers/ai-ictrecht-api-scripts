"""Datamodel van het testregister.

Veldnamen volgen de kolommen van het Excel-register, zodat import en export één-op-één zijn.
Niets wordt hard verwijderd: `verwijderd_op` gevuld = soft delete (niet meer zichtbaar of geselecteerd).
Scores worden als tekst bewaard: '0'–'3' of 'n.v.t.' (zie `score_waarde`).
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

DIMENSIES = ["Juistheid", "Volledigheid", "Bronnen", "Bruikbaarheid", "Gedrag"]
DIMENSIE_VELDEN = {d: d.lower() for d in DIMENSIES}
TESTTYPES = ["KERN", "DETAIL", "CASUS", "TAAK", "RAND", "FOUT", "BUITEN", "GRENS"]
TC_STATUSSEN = ["concept", "goedgekeurd", "vervallen"]
MODEL_STATUSSEN = ["in ontwikkeling", "in test", "in gebruik", "uitgefaseerd"]
SCORE_KEUZES = ["3", "2", "1", "0", "n.v.t."]
KO_CODES = [f"K.O.{i}" for i in range(1, 6)]
MENS, LLM = "mens", "LLM"


def nu() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def score_waarde(s: str | None):
    """'2' -> 2, 'n.v.t.' -> 'n.v.t.', None/'' -> None (zoals het in een Excel-cel zou staan)."""
    if s is None or s == "":
        return None
    return s if s == "n.v.t." else int(s)


def score_tekst(v) -> str | None:
    if v is None or v == "":
        return None
    if isinstance(v, str) and v.strip().lower() in ("n.v.t.", "nvt", "n.v.t"):
        return "n.v.t."
    n = float(v)
    if n not in (0, 1, 2, 3):
        raise ValueError(f"ongeldige score {v!r}")
    return str(int(n))


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSON, list: JSON}


class Tijdstempels:
    aangemaakt_op: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=nu, server_default=func.now())
    gewijzigd_op: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), onupdate=nu)
    verwijderd_op: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def verwijderd(self) -> bool:
        return self.verwijderd_op is not None


class Model(Tijdstempels, Base):
    """Eén regel per modelversie (tabblad Modellen)."""
    __tablename__ = "modellen"
    __table_args__ = (UniqueConstraint("model_id", "versie", name="uq_model_versie"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model_id: Mapped[str] = mapped_column(String(40), index=True)
    naam: Mapped[str] = mapped_column(Text, default="")              # Modelnaam (OpenWebUI)
    rechtsgebied: Mapped[str] = mapped_column(Text, default="")
    versie: Mapped[str] = mapped_column(String(40), default="")      # Modelversie; leeg = nog niet ingevuld
    basismodel: Mapped[str] = mapped_column(Text, default="")
    systeemprompt_versie: Mapped[str] = mapped_column(Text, default="")
    kennisbank: Mapped[str] = mapped_column(Text, default="")
    tools: Mapped[str] = mapped_column(Text, default="")
    temperatuur: Mapped[str] = mapped_column(String(20), default="")
    eigenaar: Mapped[str] = mapped_column(Text, default="")
    datum_versie: Mapped[dt.date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(40), default="")
    opmerkingen: Mapped[str] = mapped_column(Text, default="")


class Testcase(Tijdstempels, Base):
    __tablename__ = "testcases"
    __test__ = False  # geen pytest-testklasse

    tc_id: Mapped[str] = mapped_column(String(60), primary_key=True)
    model_id: Mapped[str] = mapped_column(String(40), index=True)
    rechtsgebied: Mapped[str] = mapped_column(Text, default="")
    onderwerp: Mapped[str] = mapped_column(Text, default="")
    testtype: Mapped[str] = mapped_column(String(20), default="")
    prompt: Mapped[str] = mapped_column(Text, default="")
    context: Mapped[str] = mapped_column(Text, default="")            # Context / bijlage
    kernpunten: Mapped[str] = mapped_column(Text, default="")         # Kernpunten verwacht antwoord
    verwacht_gedrag: Mapped[str] = mapped_column(Text, default="")
    verwachte_bronnen: Mapped[str] = mapped_column(Text, default="")
    regressieset: Mapped[str] = mapped_column(String(1), default="N")  # J/N
    herkomst: Mapped[str] = mapped_column(Text, default="")
    opgesteld_door: Mapped[str] = mapped_column(Text, default="")
    goedgekeurd_door: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="concept")
    peildatum: Mapped[dt.date | None] = mapped_column(Date)
    opmerkingen: Mapped[str] = mapped_column(Text, default="")
    # Herkomst bij import: bestand en het TC-ID zoals het daar stond (kan afwijken na hernummeren).
    import_bron: Mapped[str] = mapped_column(Text, default="")
    oorspronkelijk_id: Mapped[str] = mapped_column(String(60), default="")

    runs: Mapped[list[Testrun]] = relationship(back_populates="testcase")


class Testrun(Tijdstempels, Base):
    __tablename__ = "testruns"
    __test__ = False  # geen pytest-testklasse

    run_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tc_id: Mapped[str] = mapped_column(ForeignKey("testcases.tc_id"), index=True)
    model_id: Mapped[str] = mapped_column(String(40), index=True)
    modelversie: Mapped[str] = mapped_column(String(40), index=True)
    datum: Mapped[dt.date] = mapped_column(Date)
    uitgevoerd_door: Mapped[str] = mapped_column(Text, default="")
    uitvoerwijze: Mapped[str] = mapped_column(String(20), default="API")  # API / handmatig
    herhaling: Mapped[int] = mapped_column(Integer, default=1)
    antwoord: Mapped[str] = mapped_column(Text, default="")              # volledig, ongewijzigd
    # Auditspoor (was output/runs/<Run-ID>.json)
    api_model: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict | None] = mapped_column(JSON)
    response: Mapped[dict | None] = mapped_column(JSON)
    start: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    duur_s: Mapped[float | None] = mapped_column(Float)
    import_bron: Mapped[str] = mapped_column(Text, default="")
    oorspronkelijk_id: Mapped[str] = mapped_column(String(40), default="")

    testcase: Mapped[Testcase] = relationship(back_populates="runs")
    beoordelingen: Mapped[list[Beoordeling]] = relationship(back_populates="run", order_by="Beoordeling.id")

    def beoordeling(self, type_: str) -> Beoordeling | None:
        """Laatste niet-verwijderde beoordeling van dit type (mens of LLM)."""
        actief = [b for b in self.beoordelingen if b.type == type_ and not b.verwijderd]
        return actief[-1] if actief else None


class Beoordeling(Tijdstempels, Base):
    """Scores voor een run. Een run kan meerdere beoordelingen hebben (jurist én LLM)."""
    __tablename__ = "beoordelingen"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("testruns.run_id"), index=True)
    type: Mapped[str] = mapped_column(String(10))               # mens / LLM
    beoordelaar: Mapped[str] = mapped_column(Text, default="")
    juistheid: Mapped[str | None] = mapped_column(String(6))
    volledigheid: Mapped[str | None] = mapped_column(String(6))
    bronnen: Mapped[str | None] = mapped_column(String(6))
    bruikbaarheid: Mapped[str | None] = mapped_column(String(6))
    gedrag: Mapped[str | None] = mapped_column(String(6))
    ko: Mapped[str] = mapped_column(String(1), default="N")     # J/N
    ko_code: Mapped[str] = mapped_column(String(10), default="")
    ko_citaat: Mapped[str] = mapped_column(Text, default="")
    toelichting: Mapped[str] = mapped_column(Text, default="")
    te_verifieren: Mapped[list | None] = mapped_column(JSON)
    kernpunten: Mapped[list | None] = mapped_column(JSON)       # oordeel per kernpunt (LLM)
    eindoordeel: Mapped[str] = mapped_column(String(12), default="")
    waarschuwingen: Mapped[list | None] = mapped_column(JSON)
    details: Mapped[dict | None] = mapped_column(JSON)          # berichten en ruwe antwoorden van de LLM

    run: Mapped[Testrun] = relationship(back_populates="beoordelingen")

    def scores(self) -> dict:
        """{'Juistheid': 2, ..., 'Gedrag': 'n.v.t.'} zoals in Excel."""
        return {d: score_waarde(getattr(self, veld)) for d, veld in DIMENSIE_VELDEN.items()}

    def zet_scores(self, scores: dict) -> None:
        for d, veld in DIMENSIE_VELDEN.items():
            setattr(self, veld, score_tekst(scores.get(d)))


class Koppeling(Base):
    """Model-ID ↔ OpenWebUI-model (was modelkoppeling.json)."""
    __tablename__ = "modelkoppeling"

    model_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    openwebui_id: Mapped[str] = mapped_column(Text)
    naam: Mapped[str] = mapped_column(Text, default="")
    bevestigd_op: Mapped[dt.date | None] = mapped_column(Date)
    bron: Mapped[str] = mapped_column(String(40), default="")


class Instelling(Base):
    __tablename__ = "instellingen"

    sleutel: Mapped[str] = mapped_column(String(60), primary_key=True)
    waarde: Mapped[str] = mapped_column(Text, default="")
