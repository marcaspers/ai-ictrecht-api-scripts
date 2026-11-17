"""Kleine wrapper rond de ICTRecht AI proxy API."""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Iterator

import requests
from dotenv import load_dotenv

load_dotenv()

DEFAULT_BASE_URL = "https://proxy.ai.ictrecht.nl/api"
DEFAULT_LOG_DIR = "logs"
DEFAULT_MODELS_FILE = "models.json"
DEFAULT_TIMEOUT = 300  # seconden; overschrijfbaar met ICTRECHT_API_TIMEOUT
REDACTED_FIELDS = {"password"}


class ApiError(RuntimeError):
    def __init__(self, response: requests.Response):
        self.status_code = response.status_code
        try:
            self.body = response.json()
        except ValueError:
            self.body = response.text[:500]
        super().__init__(f"{response.request.method} {response.url} -> {self.status_code}: {self.body}")


class Client:
    def __init__(self, token: str | None = None, base_url: str | None = None, timeout: float | None = None,
                 log_dir: str | None = None, models_file: str | None = None, max_retries: int = 0,
                 backoff: float = 2.0, sleep=time.sleep):
        self.base_url = (base_url or os.getenv("ICTRECHT_API_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout if timeout is not None else float(os.getenv("ICTRECHT_API_TIMEOUT") or DEFAULT_TIMEOUT)
        # Herhaalpogingen bij time-outs, verbindingsfouten, 429 en 5xx (niet bij andere 4xx).
        self.max_retries = max_retries
        self.backoff = backoff
        self._sleep = sleep
        # Lege string (ICTRECHT_API_LOG_DIR=) schakelt logging uit.
        log_dir = log_dir if log_dir is not None else os.getenv("ICTRECHT_API_LOG_DIR", DEFAULT_LOG_DIR)
        self.log_dir = Path(log_dir) if log_dir else None
        # Lokaal overzicht van modellen met vaste nummers; lege string schakelt het uit.
        models_file = models_file if models_file is not None else os.getenv("ICTRECHT_API_MODELS_FILE", DEFAULT_MODELS_FILE)
        self.models_file = Path(models_file) if models_file else None
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        token = token or os.getenv("ICTRECHT_API_TOKEN")
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    # --- logging ---------------------------------------------------------

    def _log(self, response: requests.Response, body=None, request_payload=None) -> None:
        """Schrijf één JSON-regel per API-call naar logs/api-YYYY-MM-DD.jsonl."""
        if not self.log_dir:
            return
        if body is None:
            try:
                body = response.json()
            except ValueError:
                body = response.text
        if isinstance(request_payload, dict):
            request_payload = {k: ("***" if k in REDACTED_FIELDS else v) for k, v in request_payload.items()}
        now = datetime.now().astimezone()
        entry = {
            "timestamp": now.isoformat(timespec="milliseconds"),
            "method": response.request.method,
            "url": response.url,
            "status": response.status_code,
            "elapsed_ms": round(response.elapsed.total_seconds() * 1000),
            "request": request_payload,
            "response": body,
        }
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with open(self.log_dir / f"api-{now:%Y-%m-%d}.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            print(f"Waarschuwing: kon API-log niet schrijven: {e}", file=sys.stderr)

    # --- low level -------------------------------------------------------

    def _wachttijd(self, poging: int, response: requests.Response | None = None) -> float:
        if response is not None:
            try:
                return max(0.0, float(response.headers.get("Retry-After", "")))
            except ValueError:
                pass
        return self.backoff * (2 ** poging)

    def request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}/{path.lstrip('/')}"
        poging = 0
        while True:
            try:
                response = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except (requests.Timeout, requests.ConnectionError) as e:
                if poging >= self.max_retries:
                    raise
                wacht = self._wachttijd(poging)
                print(f"{type(e).__name__} bij {method} {path}; nieuwe poging over {wacht:.0f} s", file=sys.stderr)
                self._sleep(wacht)
                poging += 1
                continue
            # Streams worden gelogd door de aanroeper zodra de stream klaar is.
            if not kwargs.get("stream") or not response.ok:
                self._log(response, request_payload=kwargs.get("json"))
            if response.ok:
                return response
            if (response.status_code == 429 or response.status_code >= 500) and poging < self.max_retries:
                wacht = self._wachttijd(poging, response)
                print(f"HTTP {response.status_code} bij {method} {path}; nieuwe poging over {wacht:.0f} s",
                      file=sys.stderr)
                self._sleep(wacht)
                poging += 1
                continue
            raise ApiError(response)

    def get(self, path: str, **kwargs):
        return self.request("GET", path, **kwargs).json()

    def post(self, path: str, payload: dict, **kwargs):
        return self.request("POST", path, json=payload, **kwargs).json()

    # --- endpoints -------------------------------------------------------

    def login(self, email: str, password: str) -> dict:
        return self.post("auth/login", {"email": email, "password": password})

    def models(self):
        data = self.get("models")
        self._update_models_file(data)
        return data

    # --- lokaal modellen-overzicht ----------------------------------------

    def _load_models_file(self) -> list[dict]:
        if not self.models_file or not self.models_file.exists():
            return []
        try:
            return json.loads(self.models_file.read_text(encoding="utf-8")).get("models", [])
        except (OSError, ValueError, AttributeError) as e:
            print(f"Waarschuwing: kon {self.models_file} niet lezen: {e}", file=sys.stderr)
            return []

    def _update_models_file(self, data) -> None:
        """Werk models.json bij. Nummers blijven stabiel: bestaande modellen houden hun nr,
        nieuwe modellen krijgen het volgende vrije nr, verdwenen modellen krijgen available=false."""
        if not self.models_file:
            return
        items = data.get("data", []) if isinstance(data, dict) else data
        if not isinstance(items, list):
            return
        current = {m["id"]: m for m in items if isinstance(m, dict) and m.get("id")}
        entries = self._load_models_file()
        known = {e["id"] for e in entries}
        for e in entries:
            e["available"] = e["id"] in current
            if e["available"]:
                e["name"] = (current[e["id"]].get("name") or "").strip()
        next_nr = max((e["nr"] for e in entries), default=-1) + 1
        for model_id, m in current.items():
            if model_id not in known:
                entries.append({"nr": next_nr, "id": model_id, "name": (m.get("name") or "").strip(),
                                "available": True})
                next_nr += 1
        entries.sort(key=lambda e: e["nr"])
        out = {"updated": datetime.now().astimezone().isoformat(timespec="seconds"), "models": entries}
        try:
            self.models_file.parent.mkdir(parents=True, exist_ok=True)
            self.models_file.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        except OSError as e:
            print(f"Waarschuwing: kon {self.models_file} niet schrijven: {e}", file=sys.stderr)

    def resolve_model(self, model: str | int | None) -> str | None:
        """Vertaal een nummer uit models.json (bijv. 3 of "3") naar het model-id; anders ongewijzigd."""
        if model is None or not str(model).isdigit():
            return model
        nr = int(model)
        entries = self._load_models_file()
        if not entries:
            # Nog geen overzicht: eerst ophalen.
            self.models()
            entries = self._load_models_file()
        for e in entries:
            if e["nr"] == nr:
                if not e.get("available", True):
                    print(f"Waarschuwing: model {nr} ({e['id']}) stond bij de laatste update niet meer in de lijst",
                          file=sys.stderr)
                return e["id"]
        raise ValueError(f"Modelnummer {nr} niet gevonden in {self.models_file} (draai: python play.py models)")

    def prompts(self):
        return self.get("v1/prompts")

    def functions(self):
        return self.get("v1/functions")

    def chat(self, messages: list[dict], model: str | int | None = None, temperature: float | None = None) -> dict:
        payload = {"model": self.resolve_model(model), "messages": messages, "stream": False}
        if temperature is not None:
            payload["temperature"] = temperature
        return self.post("chat/completions", payload)

    def chat_stream(self, messages: list[dict], model: str | int | None = None, temperature: float | None = None) -> Iterator[str]:
        """Yield tekst-stukjes uit een (OpenAI-achtige) SSE stream."""
        payload = {"model": self.resolve_model(model), "messages": messages, "stream": True}
        if temperature is not None:
            payload["temperature"] = temperature
        response = self.request("POST", "chat/completions", json=payload, stream=True,
                                headers={"Accept": "text/event-stream"})
        events = []  # alle ontvangen SSE-events, voor de log
        try:
            for line in response.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    events.append(data)
                    yield data
                    continue
                events.append(chunk)
                if chunk.get("error"):
                    raise RuntimeError(f"Fout in stream: {chunk['error']}")
                for choice in chunk.get("choices", []):
                    text = (choice.get("delta") or {}).get("content")
                    if text:
                        yield text
        finally:
            self._log(response, body={"stream_events": events}, request_payload=payload)


def pretty(data) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)
