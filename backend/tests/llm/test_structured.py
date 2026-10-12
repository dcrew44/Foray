"""`structured()` (design §8.4): one retry on unusable output, none on truncation."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from apps.core.db import db
from apps.llm import registry
from apps.llm.models import LlmCall
from apps.llm.structured import OutputTruncated, structured, structured_result
from tests.fakes import fake_models
from tests.llm.vendors import Answer

pytestmark = pytest.mark.django_db(transaction=True)

GOOD = {"city": "San Francisco", "confident": True}


def ledger_errors() -> list[str | None]:
    return list(
        LlmCall.objects.unscoped("tests: read the ledger")
        .order_by("created_at")
        .values_list("error", flat=True)
    )


class Script:
    """Answers each request with the next scripted reply."""

    def __init__(self, *replies: Any, finish_reason: Any = None) -> None:
        self.replies = list(replies)
        self.finish_reasons = finish_reason if isinstance(finish_reason, list) else None
        self.requests: list[list[ModelMessage]] = []
        self.infos: list[AgentInfo] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.requests.append(list(messages))
        self.infos.append(info)
        index = len(self.requests) - 1
        args = self.replies[min(index, len(self.replies) - 1)]
        finish = self.finish_reasons[index] if self.finish_reasons else None
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, args)], finish_reason=finish
        )


async def test_returns_the_validated_output(monkeypatch: pytest.MonkeyPatch) -> None:
    script = Script(GOOD)
    fake_models(monkeypatch, {"extract": FunctionModel(script)})
    answer = await structured(
        "extract", Answer, instructions="Answer factual questions.", prompt="Which city?"
    )
    assert answer == Answer(city="San Francisco", confident=True)
    # The stable half travels as instructions, the per-item half as the user prompt.
    assert script.infos[0].instructions == "Answer factual questions."
    [request] = script.requests[0]
    assert isinstance(request, ModelRequest)
    assert [p.content for p in request.parts if isinstance(p, UserPromptPart)] == ["Which city?"]
    assert await db(ledger_errors) == [None]


async def test_one_retry_on_a_schema_error(monkeypatch: pytest.MonkeyPatch) -> None:
    script = Script({"city": 3}, GOOD)
    fake_models(monkeypatch, {"score": FunctionModel(script)})
    result = await structured_result("score", Answer, instructions="Answer.", prompt="Which city?")
    assert result.output == Answer(city="San Francisco", confident=True)
    assert result.retried is True and result.usage.requests == 2
    # The model saw why it failed.
    retry = script.requests[1][-1]
    assert isinstance(retry, ModelRequest)
    assert any(isinstance(p, RetryPromptPart) for p in retry.parts)
    assert await db(ledger_errors) == [None, None]


async def test_one_retry_on_malformed_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """TS missed this case (its `instanceof SyntaxError` check never matched)."""
    script = Script('{"city": "San Fra', GOOD)
    fake_models(monkeypatch, {"score": FunctionModel(script)})
    result = await structured_result("score", Answer, instructions="Answer.", prompt="Which city?")
    assert result.output.city == "San Francisco"
    assert len(script.requests) == 2


async def test_a_second_failure_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    script = Script({"city": 3})
    fake_models(monkeypatch, {"score": FunctionModel(script)})
    with pytest.raises(UnexpectedModelBehavior):
        await structured("score", Answer, instructions="Answer.", prompt="Which city?")
    assert len(script.requests) == 2  # one retry, never a third request
    assert len(await db(ledger_errors)) == 2


async def test_no_retry_on_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    """`finish_reason="length"`: exactly one model request and one ledger row."""
    script = Script('{"city": "San Fra', GOOD, finish_reason=["length", None])
    fake_models(monkeypatch, {"evaluate": FunctionModel(script)})
    with pytest.raises(OutputTruncated, match="evaluate: the model ran out of output tokens."):
        await structured("evaluate", Answer, instructions="Answer.", prompt="Which city?")
    assert len(script.requests) == 1
    assert await db(ledger_errors) == [None]  # the provider call itself succeeded


async def test_pydantic_ai_alone_would_retry_a_truncated_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Why the guard exists: without it, 2.54 retries a truncated output-tool call."""
    script = Script('{"city": "San Fra', GOOD, finish_reason=["length", None])
    fake_models(monkeypatch, {"evaluate": FunctionModel(script)})
    agent = Agent(registry.model("evaluate"), output_type=Answer, retries={"output": 1})
    result = await agent.run("Which city?")
    assert result.output.city == "San Francisco"
    assert len(script.requests) == 2


async def test_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        await asyncio.sleep(10)
        raise AssertionError("unreachable")

    fake_models(monkeypatch, {"evaluate": FunctionModel(slow)})
    with pytest.raises(TimeoutError):
        await structured(
            "evaluate", Answer, instructions="Answer.", prompt="Which city?", timeout=0.05
        )
    for _ in range(100):  # the shielded ledger write may still be finishing
        if await db(ledger_errors):
            break
        await asyncio.sleep(0.02)
    assert await db(ledger_errors) == ["cancelled (input estimated)"]


async def test_images_reach_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    script = Script(GOOD)
    fake_models(monkeypatch, {"extract": FunctionModel(script)})
    page = BinaryContent(b"\x89PNG\r\n\x1a\n", media_type="image/png")
    await structured("extract", Answer, instructions="Read the page.", prompt=["Page 1:", page])
    [request] = script.requests[0]
    assert isinstance(request, ModelRequest)
    [user] = [p for p in request.parts if isinstance(p, UserPromptPart)]
    assert user.content == ["Page 1:", page]
