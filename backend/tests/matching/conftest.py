"""Fixtures for the matching tests (design §12.1).

- `FakeLLM`: deterministic `score` and `evaluate` models (pydantic-ai `FunctionModel`s) that
  answer by posting title and record every request, installed with `tests.fakes.fake_models`,
  so the budget guard, limiter and ledger stay in the path.
- Per-process LLM state (the limiter's buckets, the budget's cached spend) is reset around every
  test, so a long run never waits on the 300-a-minute bucket.
- `golden(path)` reads `tests/goldens/<path>`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from django.test import Client
from django.utils import timezone
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from apps.accounts.models import User
from apps.jobs.models import Company, Job
from apps.llm import budget, fake, limiter
from apps.matching.fakes import recommendation_for
from tests.factories import make_company, make_job, make_user
from tests.fakes import fake_models

GOLDENS = Path(__file__).resolve().parents[1] / "goldens"
ORIGIN = "http://localhost:3000"


def golden(path: str) -> Any:
    return json.loads((GOLDENS / path).read_text())


@pytest.fixture(autouse=True)
def fresh_llm_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    budget.reset_cache()
    monkeypatch.setattr(limiter, "_BUCKETS", {})
    yield
    budget.reset_cache()


# --- the fake models -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Call:
    purpose: str
    output: str  # "derive", "fast" or "deep"
    instructions: str
    prompt: str
    title: str


def _line(text: str, label: str) -> str:
    found = re.search(rf"^{label}: (.*)$", text, re.MULTILINE)
    return found.group(1) if found else ""


def _answer(info: AgentInfo, args: dict[str, Any]) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, args)])


@dataclass
class FakeLLM:
    """`score` and `evaluate` by posting title. A title not in `fast` scores `default_fast`; one
    not in `deep` gets its fast score (or `default_deep`). Titles in `fail_fast`/`fail_deep`
    raise "model timed out"."""

    title_include: list[str] = field(default_factory=lambda: ["software engineer"])
    title_exclude: list[str] = field(default_factory=list)
    brief: dict[str, Any] | None = None
    fast: dict[str, int] = field(default_factory=dict)
    deep: dict[str, int] = field(default_factory=dict)
    default_fast: int = 70
    default_deep: int = 75
    fail_fast: set[str] = field(default_factory=set)
    fail_deep: set[str] = field(default_factory=set)
    calls: list[Call] = field(default_factory=list)
    # Called (on the model's thread) after each fast score is answered: a test's way to act
    # mid-scoring, such as pressing Stop.
    after_fast: Callable[[], None] | None = None

    def _record(self, purpose: str, output: str, messages: list[ModelMessage], info: AgentInfo):
        prompt = fake.latest_user_text(messages)
        call = Call(purpose, output, info.instructions or "", prompt, _line(prompt, "Title"))
        self.calls.append(call)
        return call

    def score(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        properties = set(info.output_tools[0].parameters_json_schema.get("properties", {}))
        if "title_include" in properties:
            self._record("score", "derive", messages, info)
            empty = dict.fromkeys(
                ("roles", "seniority", "locations", "remote", "companies", "focus")
            )
            return _answer(
                info,
                {
                    "brief": {**empty, **(self.brief or {})},
                    "title_include": self.title_include,
                    "title_exclude": self.title_exclude,
                    "keywords": ["python"],
                },
            )
        call = self._record("score", "fast", messages, info)
        if call.title in self.fail_fast:
            raise RuntimeError("model timed out")
        answer = _answer(
            info,
            {
                "score": self.fast.get(call.title, self.default_fast),
                "fit_reasons": [f"Fits {call.title}"],
                "red_flags": [],
                "seniority": "mid",
                "hard_requirement_misses": [],
            },
        )
        if self.after_fast is not None:
            self.after_fast()
        return answer

    def evaluate(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        call = self._record("evaluate", "deep", messages, info)
        if call.title in self.fail_deep:
            raise RuntimeError("model timed out")
        score = self.deep.get(call.title, self.fast.get(call.title, self.default_deep))
        return _answer(
            info,
            {
                "matched_requirements": [],
                "gaps": [],
                "seniority_fit": "match",
                "location_fit": "match",
                "salary_fit": "unstated",
                "risk_flags": [],
                "talking_points": [],
                "summary": f"Why {call.title} fits.",
                "score": score,
                "recommendation": recommendation_for(score),
            },
        )

    def of(self, output: str) -> list[Call]:
        return [call for call in self.calls if call.output == output]


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    models = FakeLLM()
    fake_models(
        monkeypatch,
        {"score": FunctionModel(models.score), "evaluate": FunctionModel(models.evaluate)},
    )
    return models


# --- data ----------------------------------------------------------------------------------------


@pytest.fixture
def fresh_company() -> Callable[..., Company]:
    """A company fetched just now, so a search does not try to refresh it over the network."""

    def make(**fields: Any) -> Company:
        fields.setdefault("last_fetched_at", timezone.now())
        return make_company(**fields)

    return make


def bay_area_job(company: Company, title: str, **fields: Any) -> Job:
    return make_job(company=company, title=title, **fields)


class Api:
    """`Client` with JSON bodies and the browser's Origin header on unsafe methods (§4.3)."""

    def __init__(self, client: Client) -> None:
        self.client = client

    def get(self, path: str) -> Any:
        return self.client.get(path)

    def post(self, path: str, body: Any = None) -> Any:
        payload = body if isinstance(body, str) else json.dumps({} if body is None else body)
        return self.client.post(
            path, data=payload, content_type="application/json", headers={"Origin": ORIGIN}
        )

    def patch(self, path: str, body: Any) -> Any:
        payload = body if isinstance(body, str) else json.dumps(body)
        return self.client.patch(
            path, data=payload, content_type="application/json", headers={"Origin": ORIGIN}
        )


@pytest.fixture
def signed_in() -> Callable[..., tuple[Api, User]]:
    """`signed_in(**user_fields)`: a client signed in as a new user."""

    def make(**fields: Any) -> tuple[Api, User]:
        user = make_user(**fields)
        client = Client()
        client.force_login(user)
        return Api(client), user

    return make
