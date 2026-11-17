"""Retries en time-out van de Client (zonder netwerk)."""

import pytest
import requests

from ictrecht_api import ApiError, Client


class Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self.ok = status < 400
        self._body = body or {}
        self.headers = headers or {}
        self.text = str(self._body)
        self.url = "https://x/api/chat/completions"
        self.request = type("R", (), {"method": "POST"})()
        self.elapsed = type("E", (), {"total_seconds": lambda self: 0.1})()

    def json(self):
        return self._body


def client_met(antwoorden, **kw):
    c = Client(token="t", log_dir="", models_file="", sleep=lambda s: wachttijden.append(s), **kw)
    reeks = iter(antwoorden)

    def request(method, url, timeout, **kwargs):
        a = next(reeks)
        if isinstance(a, Exception):
            raise a
        return a
    c.session.request = request
    return c


wachttijden = []


def setup_function():
    wachttijden.clear()


def test_retry_bij_5xx_429_en_timeout():
    c = client_met([Resp(503), Resp(429, headers={"Retry-After": "7"}), requests.Timeout(), Resp(200, {"ok": 1})],
                   max_retries=3)
    assert c.post("chat/completions", {}) == {"ok": 1}
    assert wachttijden == [2.0, 7.0, 8.0]


def test_geen_retry_bij_400():
    c = client_met([Resp(400, {"detail": "fout"}), Resp(200)], max_retries=3)
    with pytest.raises(ApiError):
        c.post("chat/completions", {})
    assert wachttijden == []


def test_na_drie_retries_fout():
    c = client_met([Resp(500)] * 4, max_retries=3)
    with pytest.raises(ApiError):
        c.post("chat/completions", {})
    assert len(wachttijden) == 3


def test_timeout_uit_env(monkeypatch):
    monkeypatch.setenv("ICTRECHT_API_TIMEOUT", "42")
    assert Client(token="t", log_dir="", models_file="").timeout == 42
    monkeypatch.delenv("ICTRECHT_API_TIMEOUT")
    assert Client(token="t", log_dir="", models_file="").timeout == 300
