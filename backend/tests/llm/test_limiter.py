"""The per-provider reservation bucket (design §8.7)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from apps.llm import limiter, registry
from apps.llm.limiter import DEFAULT_RPM, TokenBucket
from tests.fakes import fake_models


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_starts_full_then_spaces_requests() -> None:
    clock = Clock()
    bucket = TokenBucket(300, clock=clock)  # 5 a second
    assert [bucket.reserve() for _ in range(300)] == [0.0] * 300
    assert bucket.reserve() == pytest.approx(0.2)
    assert bucket.reserve() == pytest.approx(0.4)  # FIFO: each waiter queues behind the last


def test_one_token_left_three_calls_take_two_seconds() -> None:
    """`research/llm-library.md` §8, run B: a 60 rpm bucket holding 1 token."""
    clock = Clock()
    bucket = TokenBucket(60, clock=clock)
    for _ in range(59):
        bucket.reserve()
    assert [bucket.reserve() for _ in range(3)] == pytest.approx([0.0, 1.0, 2.0])


def test_refills_with_time_up_to_the_rate() -> None:
    clock = Clock()
    bucket = TokenBucket(60, clock=clock)
    for _ in range(60):
        bucket.reserve()
    clock.now += 2  # two tokens back
    assert bucket.reserve() == 0.0
    assert bucket.reserve() == 0.0
    assert bucket.reserve() == pytest.approx(1.0)
    clock.now += 3600  # never more than a full bucket
    assert [bucket.reserve() for _ in range(60)] == [0.0] * 60
    assert bucket.reserve() > 0


async def test_acquire_sleeps_for_its_slot() -> None:
    bucket = TokenBucket(600)  # 10 a second, real clock
    for _ in range(599):
        bucket.reserve()
    started = time.perf_counter()
    await asyncio.gather(*(bucket.acquire() for _ in range(3)))
    assert time.perf_counter() - started >= 0.19


def test_rate_per_provider_comes_from_the_env(env: Any) -> None:
    env(llm_rpm_openai=500)
    assert limiter.rpm_for("openai") == 500
    assert limiter.rpm_for("deepseek") == DEFAULT_RPM
    assert limiter.rpm_for("fake") == DEFAULT_RPM
    assert limiter.bucket_for("openai") is limiter.bucket_for("openai")
    assert limiter.bucket_for("openai") is not limiter.bucket_for("google")


@pytest.mark.django_db(transaction=True)
async def test_agent_loop_calls_are_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unlike TS, the chat agent's own loop goes through the limiter: one token per request."""

    def chat(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("ping", {})])
        return ModelResponse(parts=[TextPart("Done.")])

    class Counting(TokenBucket):
        taken = 0

        def reserve(self) -> float:
            Counting.taken += 1
            return super().reserve()

    limiter._BUCKETS["fake"] = Counting(60)
    fake_models(monkeypatch, {"chat": FunctionModel(chat)})
    agent = Agent(registry.model("chat"))

    @agent.tool_plain
    def ping() -> str:
        return "pong"

    await agent.run("Hello")
    assert Counting.taken == 2
