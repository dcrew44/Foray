"""The monthly budget guard (design §8.6, R12): the outermost wrapper on every model.

Before every request it reads the month-to-date ledger spend (all users: it is the operator's
API budget) and refuses with `BudgetExceeded` once the spend reaches `LLM_MONTHLY_BUDGET_USD`,
without calling the provider. The sum is cached for 30 s per process; each ledger row this
process writes is added to the cached sum, so a burst cannot overrun the budget by more than one
window of other processes' spend.

How a refusal surfaces: the run wrapper fails the run with `user_message`, chat streams it as
its error text, tools return it as `{ok: false, message}`, and `llm_smoke`/`llm_bakeoff` exit 1.
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import connection
from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings

from apps.core.db import db
from apps.core.errors import UserFacingError

CACHE_SECONDS = 30.0

MONTH_TO_DATE_SQL = """
    SELECT coalesce(sum(cost_usd), 0) FROM llm_calls
    WHERE created_at >= date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
"""

# Read at call time so a test can move time forward.
clock: Callable[[], float] = time.monotonic

_cached: tuple[Decimal, float] | None = None  # (spend, fetched at)


def dollars(amount: Decimal) -> str:
    """`90` for 90, `2.5` for 2.50, `0.000001` for 1e-6: no exponent, no trailing zeros."""
    text = format(amount.normalize(), "f")
    return text


class BudgetExceeded(UserFacingError):
    """This month's model budget is spent. Never retried; `user_message` is shown as is."""

    def __init__(self, budget: Decimal) -> None:
        self.budget = budget
        self.user_message = (
            f"This month's model budget of ${dollars(budget)} is spent. "
            "Raise LLM_MONTHLY_BUDGET_USD to continue."
        )
        super().__init__(self.user_message)


def month_to_date_spend_sync() -> Decimal:
    """Every user's ledger spend since the first of this month, UTC. Sync: run it through db()."""
    with connection.cursor() as cursor:
        cursor.execute(MONTH_TO_DATE_SQL)
        row = cursor.fetchone()
    return Decimal(row[0]) if row and row[0] is not None else Decimal(0)


async def month_to_date_spend() -> Decimal:
    """The month-to-date spend, cached for 30 s per process."""
    global _cached
    now = clock()
    if _cached is not None and now - _cached[1] < CACHE_SECONDS:
        return _cached[0]
    spend = await db(month_to_date_spend_sync)
    _cached = (spend, clock())
    return spend


def note_spend(cost: Decimal) -> None:
    """Called by the ledger after each row: keeps the cached sum current between reads."""
    global _cached
    if _cached is not None and cost:
        _cached = (_cached[0] + cost, _cached[1])


def reset_cache() -> None:
    global _cached
    _cached = None


async def check_budget() -> None:
    budget: Decimal = settings.ENV.llm_monthly_budget_usd
    if await month_to_date_spend() >= budget:
        raise BudgetExceeded(budget)


class BudgetGuard(WrapperModel):
    """Refuses every request once the month's spend reaches the budget."""

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        await check_budget()
        return await self.wrapped.request(messages, model_settings, model_request_parameters)

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        await check_budget()
        async with self.wrapped.request_stream(
            messages, model_settings, model_request_parameters, run_context
        ) as stream:
            yield stream
