"""Helpers for the accounts tests (imported by the tests and by `conftest.py`)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.conf import settings
from django.test import Client

ORIGIN = "http://localhost:3000"
GOLDENS = Path(__file__).resolve().parents[1] / "goldens"


class Api:
    """`Client` with JSON bodies and the browser's Origin header on unsafe methods."""

    def __init__(self, client: Client) -> None:
        self.client = client

    def _send(self, method: str, path: str, body: Any, headers: dict[str, str] | None) -> Any:
        send = getattr(self.client, method)
        extra = {"Origin": ORIGIN, **(headers or {})}
        payload = body if isinstance(body, str) else json.dumps(body)
        return send(path, data=payload, content_type="application/json", headers=extra)

    def get(self, path: str, headers: dict[str, str] | None = None) -> Any:
        return self.client.get(path, headers=headers or {})

    def post(self, path: str, body: Any = None, headers: dict[str, str] | None = None) -> Any:
        return self._send("post", path, {} if body is None else body, headers)

    def put(self, path: str, body: Any, headers: dict[str, str] | None = None) -> Any:
        return self._send("put", path, body, headers)

    def patch(self, path: str, body: Any, headers: dict[str, str] | None = None) -> Any:
        return self._send("patch", path, body, headers)

    def sign_in(self, email: str, password: str) -> Any:
        return self.post("/api/auth/sign-in", {"email": email, "password": password})


def session_cookie(response: Any) -> Any:
    return response.cookies.get(settings.SESSION_COOKIE_NAME)


def load_golden(*parts: str) -> Any:
    return json.loads(GOLDENS.joinpath(*parts).read_text())
