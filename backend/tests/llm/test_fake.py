"""The fakes of `LLM_FAKE=1` (design §8.9): schema-driven answers, lane overrides, switches."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

import pytest
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo

from apps.core.db import db
from apps.jobs.schemas import ExtractedPosting
from apps.llm import fake, registry
from apps.llm.fake import FakeFailure, minimal_instance
from apps.llm.models import LlmCall
from apps.llm.structured import structured
from apps.matching.schemas import DeepEval, FastScore
from apps.resumes.schema import ExtractedResume, Verification
from foray.env import PURPOSES
from tests.llm.vendors import Answer

# --- the smallest valid instance -----------------------------------------------------------------


class Inner(BaseModel):
    kind: Literal["a", "b"]
    weight: float = Field(gt=0, le=10)


class Shapes(BaseModel):
    name: str
    code: Annotated[str, Field(min_length=3)]
    when: datetime
    count: int = Field(ge=5, le=9)
    above: int = Field(gt=0)
    below: int = Field(lt=-2)
    maybe: str | None
    inner: Inner
    many: list[Inner] = Field(min_length=2)
    tags: list[str]
    fixed: Literal["only"]
    optional: str = "has a default"


def test_minimal_instance_shapes() -> None:
    value = minimal_instance(Shapes.model_json_schema())
    assert value == {
        "name": "",
        "code": "xxx",
        "when": "1970-01-01T00:00:00Z",
        "count": 5,
        "above": 1,
        "below": -3,
        "maybe": None,
        "inner": {"kind": "a", "weight": 1},
        "many": [{"kind": "a", "weight": 1}, {"kind": "a", "weight": 1}],
        "tags": [],
        "fixed": "only",
    }
    Shapes.model_validate(value)


def test_the_smoke_answer() -> None:
    assert minimal_instance(Answer.model_json_schema()) == {"city": "", "confident": False}


@pytest.mark.parametrize(
    "output", [Answer, FastScore, DeepEval, ExtractedResume, ExtractedPosting, Verification]
)
def test_output_types_other_lanes_write_validate(output: type[BaseModel]) -> None:
    output.model_validate(minimal_instance(output.model_json_schema()))


# --- through the stack, as LLM_FAKE runs it ------------------------------------------------------


@pytest.fixture
def llm_fake(env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(registry, "_MODELS", {})
    return lambda **values: env(llm_fake=True, **values)


def ledger() -> list[tuple[str, str, str, str | None, Decimal, str | None]]:
    return list(
        LlmCall.objects.unscoped("tests: read the ledger")
        .order_by("created_at")
        .values_list("purpose", "provider", "model", "label", "cost_usd", "error")
    )


@pytest.mark.django_db(transaction=True)
async def test_every_purpose_answers_at_no_cost(llm_fake: Any) -> None:
    llm_fake()
    for purpose in PURPOSES:
        assert registry.model_ref(purpose) == f"fake:{purpose}"
        answer = await structured(purpose, Answer, instructions="Answer.", prompt="Which city?")
        assert answer == Answer(city="", confident=False)
    rows = await db(ledger)
    assert [r[0] for r in rows] == list(PURPOSES)
    assert {(r[1], r[3], r[4], r[5]) for r in rows} == {("fake", "fake", Decimal("0"), None)}


@pytest.mark.django_db(transaction=True)
async def test_chat_without_an_override_replies_in_text(llm_fake: Any) -> None:
    llm_fake()
    result = await Agent(registry.model("chat")).run("Hello")
    assert result.output == fake.DEFAULT_TEXT
    async with Agent(registry.model("chat")).run_stream("Hello") as streamed:
        assert await streamed.get_output() == fake.DEFAULT_TEXT


@pytest.mark.django_db(transaction=True)
async def test_overrides_are_tried_in_order(llm_fake: Any) -> None:
    llm_fake()
    seen: list[str] = []

    def resumes(messages: list[ModelMessage], info: AgentInfo) -> Any:
        seen.append("resumes")
        return None if fake.output_title(info) != "ExtractedResume" else {"never": "used"}

    async def careers(messages: list[ModelMessage], info: AgentInfo) -> Any:
        seen.append("careers")
        if fake.output_title(info) != "Answer":
            return None
        return Answer(city=fake.latest_user_text(messages).upper(), confident=True)

    fake.register("extract", resumes)
    fake.register("extract", careers)
    fake.register("extract", careers)  # registering twice is harmless
    answer = await structured("extract", Answer, instructions="Answer.", prompt="paris")
    assert answer == Answer(city="PARIS", confident=True)
    assert seen == ["resumes", "careers"]

    fake.unregister("extract", careers)
    answer = await structured("extract", Answer, instructions="Answer.", prompt="paris")
    assert answer == Answer(city="", confident=False)  # back to the schema-driven default


@pytest.mark.django_db(transaction=True)
async def test_an_override_can_call_tools_and_reply_in_text(llm_fake: Any) -> None:
    llm_fake()

    @fake.register("chat")
    def chat(messages: list[ModelMessage], info: AgentInfo) -> Any:
        if "get_resume" in fake.offered_tools(info) and len(messages) == 1:
            return fake.tool_call("get_resume", {})
        return "Here is your resume."

    agent = Agent(registry.model("chat"))
    called: list[bool] = []

    @agent.tool_plain
    def get_resume() -> str:
        called.append(True)
        return "resume"

    result = await agent.run("Show my resume")
    assert result.output == "Here is your resume."
    assert called == [True]


def test_register_refuses_unknown_purposes() -> None:
    with pytest.raises(ValueError, match="Not an LLM purpose"):
        fake.register("summarise", lambda messages, info: None)


def test_discover_imports_each_apps_fakes_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr("django.utils.module_loading.autodiscover_modules", calls.append)
    monkeypatch.setattr(fake, "_discovered", False)
    fake.discover()
    fake.discover()
    assert calls == ["fakes"]


# --- switches ------------------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
async def test_fake_fail(llm_fake: Any) -> None:
    llm_fake(llm_fake_fail=("score", "chat"))
    with pytest.raises(RuntimeError, match="^Fake failure$"):
        await structured("score", Answer, instructions="Answer.", prompt="Which city?")
    with pytest.raises(FakeFailure):
        async with Agent(registry.model("chat")).run_stream("Hello") as streamed:
            await streamed.get_output()
    await structured("verify", Answer, instructions="Answer.", prompt="Which city?")
    rows = await db(ledger)
    assert [(r[0], r[5]) for r in rows] == [
        ("score", "FakeFailure('Fake failure')"),
        ("chat", "FakeFailure('Fake failure')"),
        ("verify", None),
    ]


@pytest.mark.django_db(transaction=True)
async def test_fake_delay_streams_text_in_twenty_chunks(llm_fake: Any) -> None:
    llm_fake(llm_fake_delay_ms=200)
    started = time.perf_counter()
    chunks: list[str] = []
    async with Agent(registry.model("chat")).run_stream("Hello") as streamed:
        async for delta in streamed.stream_text(delta=True, debounce_by=None):
            chunks.append(delta)
    assert time.perf_counter() - started >= 0.19
    assert "".join(chunks) == fake.DEFAULT_TEXT
    assert len(chunks) == 20


@pytest.mark.django_db(transaction=True)
async def test_fake_delay_slows_structured_answers(llm_fake: Any) -> None:
    llm_fake(llm_fake_delay_ms=150)
    started = time.perf_counter()
    await structured("score", Answer, instructions="Answer.", prompt="Which city?")
    assert time.perf_counter() - started >= 0.14


@pytest.mark.django_db(transaction=True)
async def test_stop_lands_mid_reply(llm_fake: Any) -> None:
    """The point of the delay: a cancel arrives while the reply streams, and is ledgered."""
    llm_fake(llm_fake_delay_ms=2000)
    first = asyncio.Event()

    async def consume() -> None:
        async with Agent(registry.model("chat")).run_stream("Hello") as streamed:
            async for _ in streamed.stream_text(delta=True, debounce_by=None):
                first.set()

    task = asyncio.create_task(consume())
    await first.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    rows = await db(ledger)
    for _ in range(100):  # the shielded ledger write may still be finishing
        if rows:
            break
        await asyncio.sleep(0.02)
        rows = await db(ledger)
    assert [(r[0], r[5]) for r in rows] == [("chat", "cancelled")]


def test_chunks_split_evenly() -> None:
    assert fake._chunks("", 20) == [""]
    assert fake._chunks("abc", 20) == ["a", "b", "c"]
    parts = fake._chunks("x" * 45, 20)
    assert len(parts) == 20 and "".join(parts) == "x" * 45


def test_a_structured_reply_needs_an_output_tool() -> None:
    info = AgentInfo(
        function_tools=[],
        allow_text_output=True,
        output_tools=[],
        model_settings=None,
        model_request_parameters=None,  # type: ignore[arg-type]
        instructions=None,
    )
    with pytest.raises(TypeError, match="output tool"):
        fake.as_response({"city": "x"}, info)
    assert isinstance(fake.as_response("hi", info), ModelResponse)
