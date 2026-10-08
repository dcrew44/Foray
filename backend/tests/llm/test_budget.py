"""R12: monthly model spend is bounded by LLM_MONTHLY_BUDGET_USD (design §8.6)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.utils import timezone
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from apps.core.db import db
from apps.llm import budget, registry
from apps.llm.budget import BudgetExceeded
from apps.llm.models import LlmCall
from apps.llm.structured import structured
from tests.factories import make_llm_call, make_user
from tests.fakes import fake_models
from tests.llm.vendors import Answer

pytestmark = pytest.mark.django_db(transaction=True)

MESSAGE_90 = "This month's model budget of $90 is spent. Raise LLM_MONTHLY_BUDGET_USD to continue."


class Counting:
    """A model function that counts its calls (the provider)."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.calls += 1
        if info.output_tools:
            from pydantic_ai.messages import ToolCallPart

            args = {"city": "San Francisco", "confident": True}
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, args)])
        return ModelResponse(
            parts=[TextPart("ok")], usage=RequestUsage(input_tokens=1000, output_tokens=300)
        )


def ledger_count() -> int:
    return LlmCall.objects.unscoped("tests: count the ledger").count()


async def test_budget_guard_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """At the budget: BudgetExceeded, 0 provider calls, no ledger row."""
    user = await db(make_user)
    await db(make_llm_call, user, cost_usd=Decimal("60"))
    await db(make_llm_call, None, cost_usd=Decimal("30"))  # system rows count too
    provider = Counting()
    fake_models(monkeypatch, {"score": FunctionModel(provider)})
    with pytest.raises(BudgetExceeded) as caught:
        await structured("score", Answer, instructions="Answer.", prompt="Which city?")
    assert caught.value.user_message == MESSAGE_90
    assert str(caught.value) == MESSAGE_90
    assert provider.calls == 0
    assert await db(ledger_count) == 2


async def test_streams_are_refused_too(monkeypatch: pytest.MonkeyPatch, env: Any) -> None:
    env(llm_monthly_budget_usd=Decimal("0.5"))
    await db(make_llm_call, None, cost_usd=Decimal("0.5"))

    async def stream(messages: list[ModelMessage], info: AgentInfo):
        raise AssertionError("the provider must not be called")
        yield ""

    fake_models(monkeypatch, {"chat": FunctionModel(stream_function=stream)})
    with pytest.raises(BudgetExceeded, match=r"budget of \$0\.5 is spent"):
        async with Agent(registry.model("chat")).run_stream("Hello") as result:
            await result.get_output()


async def test_under_the_budget_calls_go_through(monkeypatch: pytest.MonkeyPatch) -> None:
    await db(make_llm_call, None, cost_usd=Decimal("89.99"))
    provider = Counting()
    fake_models(monkeypatch, {"score": FunctionModel(provider)})
    await structured("score", Answer, instructions="Answer.", prompt="Which city?")
    assert provider.calls == 1


async def test_only_this_month_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    last_month = timezone.now().replace(day=1) - timedelta(days=1)
    await db(make_llm_call, None, cost_usd=Decimal("500"), created_at=last_month)
    assert await budget.month_to_date_spend() == 0
    provider = Counting()
    fake_models(monkeypatch, {"score": FunctionModel(provider)})
    await structured("score", Answer, instructions="Answer.", prompt="Which city?")
    assert provider.calls == 1


async def test_the_cache_expires_after_30_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(budget, "clock", lambda: now[0])
    provider = Counting()
    fake_models(monkeypatch, {"score": FunctionModel(provider)})

    await structured("score", Answer, instructions="Answer.", prompt="Which city?")  # reads 0
    await db(make_llm_call, None, cost_usd=Decimal("95"))  # written by another process

    now[0] += 29
    await structured("score", Answer, instructions="Answer.", prompt="Which city?")  # cached
    assert provider.calls == 2

    now[0] += 2  # 31 s after the read
    with pytest.raises(BudgetExceeded):
        await structured("score", Answer, instructions="Answer.", prompt="Which city?")
    assert provider.calls == 2


async def test_own_spend_updates_the_cached_sum(monkeypatch: pytest.MonkeyPatch, env: Any) -> None:
    """Rows this process writes count at once, without waiting for the cache to expire."""
    env(llm_monthly_budget_usd=Decimal("0.0005"))
    monkeypatch.setattr(budget, "clock", lambda: 1000.0)  # the cache never expires
    provider = Counting()
    monkeypatch.setattr(registry, "base_model", lambda ref: FunctionModel(provider))
    stack = registry.build_stack("chat", "openai:gpt-5.6-luna")  # (1000 x 0.2 + 300 x 1.2) / 1M

    await Agent(stack).run("one")  # spend 0 -> 0.00056
    with pytest.raises(BudgetExceeded):
        await Agent(stack).run("two")
    assert provider.calls == 1


def test_dollars_never_use_an_exponent() -> None:
    assert budget.dollars(Decimal(90)) == "90"
    assert budget.dollars(Decimal("2.50")) == "2.5"
    assert budget.dollars(Decimal("0.000001")) == "0.000001"
    assert BudgetExceeded(Decimal("0.000001")).user_message == (
        "This month's model budget of $0.000001 is spent. Raise LLM_MONTHLY_BUDGET_USD to continue."
    )
