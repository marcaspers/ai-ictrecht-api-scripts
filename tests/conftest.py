import datetime as dt
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from db.sessie import init_db, open_sessie  # noqa: E402
from register.importeren import importeer  # noqa: E402
from register.xlsxpatch import patch_werkmap  # noqa: E402
from testrunner.uitvoeren import Omgeving  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "Testregister_IE.xlsx"
VANDAAG = dt.date(2026, 11, 16)
IE_ID = "ie-recht-guide-door-ictrecht"


class FakeClient:
    """Nep-Client zonder netwerk. ``antwoord``: functie (messages, model, temperature) -> tekst, dict of Exception."""

    def __init__(self, antwoord=None, modellen=None):
        self.calls = []
        self.antwoord = antwoord or (lambda messages, model, temperature: f"Antwoord {len(self.calls)}")
        self.modellen = modellen if modellen is not None else [
            {"id": IE_ID, "name": "IE Recht Guide"}, {"id": "gpt-5", "name": "ChatGPT"},
            {"id": "judge-model", "name": "Judge"}]

    def get(self, path):
        assert path == "models"
        return {"data": self.modellen}

    def models(self):
        return self.get("models")

    def chat(self, messages, model=None, temperature=None):
        self.calls.append({"messages": messages, "model": model, "temperature": temperature})
        uit = self.antwoord(messages, model, temperature)
        if isinstance(uit, BaseException):
            raise uit
        if isinstance(uit, dict):
            return uit
        return {"id": "x", "choices": [{"message": {"role": "assistant", "content": uit}}],
                "sources": [{"source": {"name": "kennisbank"}}]}


@pytest.fixture
def db_url(tmp_path, monkeypatch) -> str:
    """Lege SQLite-database, aangemaakt via de Alembic-migraties (zoals `db init`)."""
    url = f"sqlite:///{tmp_path / 'test.db'}"
    init_db(url)
    monkeypatch.setenv("DATABASE_URL", url)
    return url


@pytest.fixture
def sessie(db_url):
    with open_sessie(db_url) as s:
        yield s


@pytest.fixture
def gevuld(sessie):
    """Database met het IE-voorbeeldregister geïmporteerd (42 modellen, 24 testcases, IE v1.0)."""
    importeer(sessie, FIXTURE, ja=True, uitvoer=lambda s: None)
    return sessie


@pytest.fixture
def variant(tmp_path):
    """Maak een variant van het register met aangepaste cellen."""
    teller = [0]

    def maak(wijzigingen: dict) -> Path:
        teller[0] += 1
        doel = tmp_path / f"variant{teller[0]}" / "register.xlsx"
        return patch_werkmap(FIXTURE, doel, wijzigingen)
    return maak


@pytest.fixture
def omg(tmp_path, monkeypatch):
    monkeypatch.delenv("ICTRECHT_TESTER", raising=False)  # .env van de ontwikkelaar niet laten meetellen
    models = tmp_path / "models.json"
    models.write_text('{"updated": "x", "models": [{"nr": 16, "id": "%s", "name": "IE Recht Guide", '
                      '"available": true}]}' % IE_ID, encoding="utf-8")
    uitvoer = []
    o = Omgeving(models_pad=models, output_dir=tmp_path / "output",
                 invoer=lambda prompt: pytest.fail(f"onverwachte vraag: {prompt}"), uitvoer=uitvoer.append,
                 vandaag=VANDAAG)
    o.log = uitvoer
    return o
