"""Verbinding met de database. Adres uit DATABASE_URL (zie .env.example).

Zonder DATABASE_URL: lokaal SQLite-bestand data/testregister.db. In Docker wijst DATABASE_URL naar PostgreSQL.
Het schema wordt beheerd met Alembic (db/migraties); `testregister.py db init` maakt of werkt het bij.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
STANDAARD_URL = f"sqlite:///{ROOT / 'data' / 'testregister.db'}"
MIGRATIES = Path(__file__).resolve().parent / "migraties"


class DatabaseFout(RuntimeError):
    pass


def database_url(url: str | None = None) -> str:
    return url or os.getenv("DATABASE_URL") or STANDAARD_URL


def veilige_url(url: str) -> str:
    """URL zonder wachtwoord, voor meldingen."""
    from sqlalchemy.engine import make_url
    return make_url(url).render_as_string(hide_password=True)


def maak_engine(url: str | None = None) -> Engine:
    url = database_url(url)
    if url.startswith("sqlite:///") and ":memory:" not in url:
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, future=True)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _fk_aan(dbapi_conn, _):  # SQLite controleert foreign keys alleen als je erom vraagt
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
    return engine


def alembic_config(url: str):
    from alembic.config import Config
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIES))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def init_db(url: str | None = None) -> None:
    """Schema aanmaken of bijwerken tot de nieuwste migratie."""
    from alembic import command
    url = database_url(url)
    maak_engine(url).dispose()  # maakt zo nodig de map voor SQLite
    command.upgrade(alembic_config(url), "head")


def schema_actueel(engine: Engine) -> tuple[bool, str]:
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory
    if "alembic_version" not in inspect(engine).get_table_names():
        return False, "de database heeft nog geen schema"
    with engine.connect() as conn:
        huidig = MigrationContext.configure(conn).get_current_revision()
    nieuwste = ScriptDirectory.from_config(alembic_config(str(engine.url))).get_current_head()
    if huidig != nieuwste:
        return False, f"schema is versie {huidig}, nieuwste is {nieuwste}"
    return True, ""


_fabrieken: dict[str, sessionmaker] = {}


def sessiefabriek(url: str | None = None, controleer: bool = True) -> sessionmaker:
    url = database_url(url)
    if url not in _fabrieken:
        engine = maak_engine(url)
        if controleer:
            ok, reden = schema_actueel(engine)
            if not ok:
                raise DatabaseFout(f"Database {veilige_url(url)}: {reden}. Draai eerst 'python testregister.py db init'.")
        _fabrieken[url] = sessionmaker(engine, expire_on_commit=False)
    return _fabrieken[url]


@contextmanager
def open_sessie(url: str | None = None):
    sessie: Session = sessiefabriek(url)()
    try:
        yield sessie
    finally:
        sessie.close()
