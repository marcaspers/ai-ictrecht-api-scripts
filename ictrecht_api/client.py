"""Kleine wrapper rond de ICTRecht AI proxy API."""

import json
import os
from typing import Iterator

import requests
from dotenv import load_dotenv

load_dotenv()

DEFAULT_BASE_URL = "https://proxy.ai.ictrecht.nl/api"


class ApiError(RuntimeError):
    def __init__(self, response: requests.Response):
        self.status_code = response.status_code
        try:
            self.body = response.json()
        except ValueError:
            self.body = response.text[:500]
        super().__init__(f"{response.request.method} {response.url} -> {self.status_code}: {self.body}")


class Client:
    def __init__(self, token: str | None = None, base_url: str | None = None, timeout: float = 120):
        self.base_url = (base_url or os.getenv("ICTRECHT_API_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        token = token or os.getenv("ICTRECHT_API_TOKEN")
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    # --- low level -------------------------------------------------------

    def request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}/{path.lstrip('/')}"
        response = self.session.request(method, url, timeout=self.timeout, **kwargs)
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
        for line in response.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                yield data
                continue
            if chunk.get("error"):
                raise RuntimeError(f"Fout in stream: {chunk['error']}")
            for choice in chunk.get("choices", []):
                text = (choice.get("delta") or {}).get("content")
                if text:
                    yield text


def pretty(data) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)
