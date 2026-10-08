"""R10: every model request writes a ledger row, whatever happens (design §8.5).

Request, stream, vendor error, timeout and a cancel mid-stream each leave exactly one row; a
cancelled call with no usage is priced from an estimate of its input; attribution follows the
context through tools and nested agents; labels come from the context, or `fake` under LLM_FAKE.
"""

from __future__ import annotations

import asyncio
import logging
import math
from decimal import Decimal
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from apps.core.db import db
from apps.llm import ledger, pricing, registry
from apps.llm.context import current_llm_context, llm_context
from apps.llm.models import LlmCall
from apps.llm.structured import structured
from tests.factories import make_conversation, make_run, make_user
from tests.fakes import fake_models
from tests.llm.vendors import Answer, Vendor

pytestmark = pytest.mark.django_db(transaction=True)

LUNA = "openai:gpt-5.6-luna"
USAGE = RequestUsage(
    input_tokens=1000, cache_read_tokens=600, output_tokens=300, details={"reasoning_tokens": 200}
)
LUNA_COST = Decimal("0.000452")  # (400 x 0.20 + 600 x 0.02 + 300 x 1.20) / 1M


def rows() -> list[dict[str, Any]]:
    return list(
        LlmCall.objects.unscoped("tests: read the ledger")
        .order_by("created_at", "id")
        .values(
            "user_id",
            "run_id",
            "conversation_id",
            "purpose",
            "provider",
            "model",
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "reasoning_tokens",
            "cost_usd",
            "latency_ms",
            "error",
            "label",
        )
    )


async def ledger_rows(expected: int | None = None) -> list[dict[str, Any]]:
    """The rows, waiting (up to 5 s) for shielded writes still in flight after a cancel."""
    async with asyncio.timeout(5):
        while True:
            found = await db(rows)
            if expected is None or len(found) >= expected:
                return found
            await asyncio.sleep(0.02)


def over(
    monkeypatch: pytest.MonkeyPatch, base: Model, ref: str = LUNA, purpose: Any = "score"
) -> Model:
    """The real stack for `ref`, over a stand-in base model."""
    monkeypatch.setattr(registry, "base_model", lambda _ref: base)
    return registry.build_stack(purpose, ref)


def answer_text(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse(parts=[TextPart("San Francisco")], usage=USAGE)


async def test_ledger_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    """One row per request for success, stream, vendor error, timeout and cancel mid-stream."""
    # 1. A plain request: the provider's usage, priced at call time.
    stack = over(monkeypatch, FunctionModel(answer_text))
    await Agent(stack).run("Which city?")
    [row] = await ledger_rows(1)
    assert row | {"latency_ms": None} == {
        "user_id": None,
        "run_id": None,
        "conversation_id": None,
        "purpose": "score",
        "provider": "openai",
        "model": "gpt-5.6-luna",
        "input_tokens": 400,
        "output_tokens": 300,
        "cached_input_tokens": 600,
        "reasoning_tokens": 200,
        "cost_usd": LUNA_COST,
        "latency_ms": None,
        "error": None,
        "label": None,
    }
    assert row["latency_ms"] is not None and row["latency_ms"] >= 0

    # 2. A stream, recorded from the drained stream's usage.
    async def stream_text(messages: list[ModelMessage], info: AgentInfo):
        for word in ("San ", "Francisco"):
            yield word

    stack = over(monkeypatch, FunctionModel(stream_function=stream_text))
    async with Agent(stack).run_stream("Which city?") as result:
        assert await result.get_output() == "San Francisco"
    found = await ledger_rows(2)
    assert found[1]["error"] is None and found[1]["output_tokens"] > 0

    # 3. A vendor error (a 400 is not retried by the SDK): a row with the error, zero tokens.
    vendor = Vendor(LUNA, status=400, error_body={"error": {"message": "Bad request", "type": "x"}})
    stack = over(monkeypatch, vendor.base_model())
    with pytest.raises(Exception, match="Bad request"):
        await Agent(stack, output_type=Answer).run("Which city?")
    found = await ledger_rows(3)
    assert "ModelHTTPError" in found[2]["error"] and "Bad request" in found[2]["error"]
    assert (found[2]["input_tokens"], found[2]["output_tokens"], found[2]["cost_usd"]) == (
        0,
        0,
        Decimal("0"),
    )

    # 4. A timeout: the caller's asyncio.timeout cancels the request; the row is still written.
    async def slow(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        await asyncio.sleep(10)
        return answer_text(messages, info)

    stack = over(monkeypatch, FunctionModel(slow))
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.1):
            await Agent(stack).run("Which city?")
    found = await ledger_rows(4)
    assert found[3]["error"] == "cancelled (input estimated)"

    # 5. A client disconnect mid-stream: partial output is counted.
    first_chunk = asyncio.Event()

    async def stalls(messages: list[ModelMessage], info: AgentInfo):
        yield "San "
        first_chunk.set()
        await asyncio.sleep(10)
        yield "Francisco"

    stack = over(monkeypatch, FunctionModel(stream_function=stalls))

    async def consume() -> None:
        async with Agent(stack).run_stream("Which city?") as result:
            async for _ in result.stream_text(delta=True):
                pass

    task = asyncio.create_task(consume())
    await first_chunk.wait()
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    found = await ledger_rows(5)
    assert found[4]["error"] == "cancelled"
    assert found[4]["output_tokens"] > 0  # "San " was received before the cancel
    assert len(await ledger_rows()) == 5


async def test_cancelled_call_without_usage_is_estimated(monkeypatch: pytest.MonkeyPatch) -> None:
    started = asyncio.Event()

    async def hangs(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        started.set()
        await asyncio.sleep(10)
        raise AssertionError("unreachable")

    stack = over(monkeypatch, FunctionModel(hangs))
    instructions = "Answer factual questions. " * 40
    prompt = "Which city is the Golden Gate Bridge in? " * 20
    task = asyncio.create_task(Agent(stack, instructions=instructions).run(prompt))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    [row] = await ledger_rows(1)
    assert row["error"] == "cancelled (input estimated)"
    # At least the prompt and instructions, at 4 characters a token.
    assert row["input_tokens"] >= math.ceil((len(instructions.strip()) + len(prompt)) / 4)
    assert row["output_tokens"] == 0
    expected = pricing.to_column(pricing.cost_usd(LUNA, row["input_tokens"]))
    assert row["cost_usd"] == expected > 0


def test_estimate_counts_messages_and_instructions() -> None:
    from pydantic_ai.messages import InstructionPart, ModelRequest
    from pydantic_ai.models import ModelRequestParameters

    messages: list[ModelMessage] = [ModelRequest.user_text_prompt("x" * 400)]
    params = ModelRequestParameters(instruction_parts=[InstructionPart(content="y" * 800)])
    with_instructions = ledger.estimate_input_tokens(messages, params)
    without = ledger.estimate_input_tokens(messages, None)
    assert without >= 100
    assert with_instructions == without + 200


async def test_retry_writes_one_row_per_request(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def answers(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls.append(1)
        args = {"city": 3} if len(calls) == 1 else {"city": "San Francisco", "confident": True}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, args)])

    fake_models(monkeypatch, {"extract": FunctionModel(answers)})
    await structured("extract", Answer, instructions="Answer.", prompt="Which city?")
    found = await ledger_rows(2)
    assert [r["purpose"] for r in found] == ["extract", "extract"]
    assert {r["provider"] for r in found} == {"fake"}
    assert {r["cost_usd"] for r in found} == {Decimal("0")}


async def test_attribution_through_tools_and_nested_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    """A chat agent whose tool runs a nested structured call: every row carries the context."""

    def chat(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        already_called = any(
            isinstance(part, ToolCallPart)
            for message in messages
            if isinstance(message, ModelResponse)
            for part in message.parts
        )
        if not already_called:
            return ModelResponse(parts=[ToolCallPart("look_up", {})])
        return ModelResponse(parts=[TextPart("Done.")])

    def extract(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        args = {"city": "San Francisco", "confident": True}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, args)])

    fake_models(monkeypatch, {"chat": FunctionModel(chat), "extract": FunctionModel(extract)})
    agent = Agent(registry.model("chat"))

    @agent.tool_plain
    async def look_up() -> str:
        assert current_llm_context().label == "smoke"
        answer = await structured("extract", Answer, instructions="Answer.", prompt="Which city?")
        return answer.city

    async def turn(index: int) -> tuple[str, str, str]:
        user = await db(make_user)
        run = await db(make_run, user, status="running")
        conversation = await db(make_conversation, user)
        with llm_context(
            user_id=user.pk, run_id=run.pk, conversation_id=conversation.pk, label="smoke"
        ):
            await agent.run(f"Turn {index}")
        return user.pk, str(run.pk), str(conversation.pk)

    turns = await asyncio.gather(turn(1), turn(2), turn(3))
    found = await ledger_rows(9)
    assert len(found) == 9
    for user_id, run_id, conversation_id in turns:
        mine = [r for r in found if r["user_id"] == user_id]
        assert sorted(r["purpose"] for r in mine) == ["chat", "chat", "extract"]
        assert {str(r["run_id"]) for r in mine} == {run_id}
        assert {str(r["conversation_id"]) for r in mine} == {conversation_id}
        assert {r["label"] for r in mine} == {"smoke"}


async def test_rows_without_context_are_system_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    stack = over(monkeypatch, FunctionModel(answer_text))
    await Agent(stack).run("Which city?")
    [row] = await ledger_rows(1)
    assert (row["user_id"], row["run_id"], row["conversation_id"], row["label"]) == (
        None,
        None,
        None,
        None,
    )


async def test_label_comes_from_the_context(monkeypatch: pytest.MonkeyPatch) -> None:
    stack = over(monkeypatch, FunctionModel(answer_text))
    with llm_context(label="bakeoff"):
        await Agent(stack).run("Which city?")
    [row] = await ledger_rows(1)
    assert row["label"] == "bakeoff"


def test_labels_are_the_column_choices() -> None:
    from apps.llm.context import LABELS
    from apps.llm.models import Label

    assert set(LABELS) == set(Label.values)
    with pytest.raises(ValueError, match="Not a ledger label"), llm_context(label="other"):
        pass


async def test_llm_fake_rows_are_labelled_fake(monkeypatch: pytest.MonkeyPatch, env: Any) -> None:
    env(llm_fake=True)
    monkeypatch.setattr(registry, "_MODELS", {})
    with llm_context(label="smoke"):
        answer = await structured("verify", Answer, instructions="Answer.", prompt="Which city?")
    assert answer == Answer(city="", confident=False)
    [row] = await ledger_rows(1)
    assert (row["provider"], row["model"], row["label"], row["cost_usd"]) == (
        "fake",
        "verify",
        "fake",
        Decimal("0"),
    )


async def test_write_failures_are_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(row: ledger.LedgerRow) -> None:
        raise RuntimeError("database is gone")

    monkeypatch.setattr(ledger, "insert_row", broken)
    stack = over(monkeypatch, FunctionModel(answer_text))
    with caplog.at_level(logging.ERROR, logger="foray.llm"):
        result = await Agent(stack).run("Which city?")
    assert result.output == "San Francisco"
    assert any("failed to write ledger row" in r.getMessage() for r in caplog.records)
