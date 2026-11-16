import datetime as dt
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from register.xlsxpatch import patch_werkmap  # noqa: E402
from testrunner.uitvoeren import Omgeving  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "Testregister_IE.xlsx"
VANDAAG = dt.date(2026, 11, 16)
IE_ID = "ie-recht-guide-door-ictrecht"


class FakeClient:
    """Nep-Client zonder netwerk. ``antwoorden``: functie (messages, model) -> antwoordtekst of Exception."""

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
def register(tmp_path) -> Path:
    """Kopie van het IE-voorbeeldregister in een tijdelijke map."""
    pad = tmp_path / "invoer" / "register.xlsx"
    pad.parent.mkdir()
    shutil.copy(FIXTURE, pad)
    return pad


@pytest.fixture
def variant(tmp_path):
    """Maak een variant van het register met aangepaste cellen (alleen voor tests: ook andere tabbladen)."""
    teller = [0]

    def maak(wijzigingen: dict) -> Path:
        teller[0] += 1
        doel = tmp_path / f"variant{teller[0]}" / "register.xlsx"
        return patch_werkmap(FIXTURE, doel, wijzigingen)
    return maak


@pytest.fixture
def omg(tmp_path):
    koppeling = tmp_path / "modelkoppeling.json"
    models = tmp_path / "models.json"
    models.write_text('{"updated": "x", "models": [{"nr": 16, "id": "%s", "name": "IE Recht Guide", '
                      '"available": true}]}' % IE_ID, encoding="utf-8")
    uitvoer = []
    o = Omgeving(output_dir=tmp_path / "output", koppeling_pad=koppeling, models_pad=models,
                 invoer=lambda prompt: pytest.fail(f"onverwachte vraag: {prompt}"), uitvoer=uitvoer.append,
                 vandaag=VANDAAG)
    o.log = uitvoer
    return o
