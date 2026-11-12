"""Kleine wrapper rond de ICTRecht AI proxy API."""

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterator

import requests
from dotenv import load_dotenv

load_dotenv()

DEFAULT_BASE_URL = "https://proxy.ai.ictrecht.nl/api"
DEFAULT_LOG_DIR = "logs"
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
    def __init__(self, token: str | None = None, base_url: str | None = None, timeout: float = 120,
                 log_dir: str | None = None):
        self.base_url = (base_url or os.getenv("ICTRECHT_API_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        # Lege string (ICTRECHT_API_LOG_DIR=) schakelt logging uit.
        log_dir = log_dir if log_dir is not None else os.getenv("ICTRECHT_API_LOG_DIR", DEFAULT_LOG_DIR)
        self.log_dir = Path(log_dir) if log_dir else None
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

    def request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}/{path.lstrip('/')}"
        response = self.session.request(method, url, timeout=self.timeout, **kwargs)
        # Streams worden gelogd door de aanroeper zodra de stream klaar is.
        if not kwargs.get("stream") or not response.ok:
            self._log(response, request_payload=kwargs.get("json"))
        if not response.ok:
            raise ApiError(response)
        return response

    def get(self, path: str, **kwargs):
        return self.request("GET", path, **kwargs).json()

    def post(self, path: str, payload: dict, **kwargs):
        return self.request("POST", path, json=payload, **kwargs).json()

    # --- endpoints -------------------------------------------------------

    def login(self, email: str, password: str) -> dict:
        return self.post("auth/login", {"email": email, "password": password})

    def models(self):
        return self.get("models")

    def prompts(self):
        return self.get("v1/prompts")

    def functions(self):
        return self.get("v1/functions")

    def chat(self, messages: list[dict], model: str | None = None, temperature: float | None = None) -> dict:
        payload = {"model": model, "messages": messages, "stream": False}
        if temperature is not None:
            payload["temperature"] = temperature
        return self.post("chat/completions", payload)

    def chat_stream(self, messages: list[dict], model: str | None = None, temperature: float | None = None) -> Iterator[str]:
        """Yield tekst-stukjes uit een (OpenAI-achtige) SSE stream."""
        payload = {"model": model, "messages": messages, "stream": True}
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
