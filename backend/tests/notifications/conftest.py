"""Fixtures for the notification tests (#59).

- `ntfy_server`: a fake ntfy behind the guarded client (`httpx2.MockTransport`, through
  `tests.jobs.conftest.mock_client`), installed where the sender and the test endpoint get
  their client (`apps.notifications.ntfy.http_client`). It records every request, answers a
  publish the way ntfy does, and can be told to answer otherwise. Nothing reaches DNS or the
  network.
- `llm`: the matching tests' fake `score` and `evaluate` models, for the tests that run a watch.
- `phone_user()`: a user who can be watched for, with a topic and no quiet hours (so a test
  passes at any hour of the day).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx2
import pytest
from pydantic_ai.models.function import FunctionModel

from apps.accounts.models import User
from apps.llm import budget, limiter
from apps.notifications import ntfy
from tests.factories import make_user
from tests.fakes import fake_models
from tests.jobs.conftest import mock_client
from tests.matching.conftest import FakeLLM

TOPIC = "foray-avery-7f3k9q"
# The same time twice: no quiet hours.
AWAKE = {"quietHoursStart": "00:00", "quietHoursEnd": "00:00"}


@dataclass
class FakeNtfy:
    requests: list[httpx2.Request] = field(default_factory=list)
    published: list[dict[str, Any]] = field(default_factory=list)  # the bodies it accepted
    # Set to answer every request some other way (a refusal, a page that is not ntfy's).
    answer: Callable[[httpx2.Request], httpx2.Response] | None = None

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.answer is not None:
            return self.answer(request)
        body = json.loads(request.content)
        self.published.append(body)
        return httpx2.Response(
            200,
            json={
                "id": f"msg{len(self.published)}",
                "time": 1_794_000_000,
                "event": "message",
                "topic": body["topic"],
                "title": body.get("title"),
                "message": body.get("message"),
            },
        )

    def refuse(self, status: int, error: str | None = None) -> None:
        """Answer everything with ntfy's error body from now on."""
        payload = {"code": status * 100 + 1, "http": status, "error": error or "refused"}
        self.answer = lambda request: httpx2.Response(status, json=payload)

    def accept(self) -> None:
        self.answer = None


@pytest.fixture
def ntfy_server(monkeypatch: pytest.MonkeyPatch) -> FakeNtfy:
    server = FakeNtfy()
    monkeypatch.setattr(ntfy, "http_client", lambda: mock_client(server.handler))
    return server


@pytest.fixture(autouse=True)
def fresh_llm_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    budget.reset_cache()
    monkeypatch.setattr(limiter, "_BUCKETS", {})
    yield
    budget.reset_cache()


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    models = FakeLLM()
    fake_models(
        monkeypatch,
        {"score": FunctionModel(models.score), "evaluate": FunctionModel(models.evaluate)},
    )
    return models


def phone_user(*, time_zone: str = "UTC", **settings: Any) -> User:
    return make_user(
        with_resume=True,
        timezone=time_zone,
        settings={"deepEvalTopN": 15, "ntfyTopic": TOPIC, **AWAKE, **settings},
    )
