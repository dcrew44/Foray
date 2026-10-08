"""The ledger on the model (design §8.5, R10; DESIGN.md invariant 10).

`LedgerModel.request` and `request_stream` write **one `llm_calls` row per model request**, in
`finally`, under `asyncio.shield`, through `db()`: an autocommit write on its own pooled
connection that no caller transaction can roll back. That covers success, vendor errors,
timeouts, a client disconnect mid-stream and cancellation. Because the ledger sits on the model,
agent-internal calls, nested agents and streams are all covered, not just our own helpers.

- Streams record from `stream.usage`, final once drained and partial on cancel.
- A cancelled request usually has no usage yet (OpenAI's Responses stream sends usage only in
  its final event), but the provider may still bill it. Its input is then estimated as
  `ceil(len(serialised messages and instructions) / 4)`, priced with output 0, and the row's
  error reads "cancelled (input estimated)", so Stops and shutdowns do not under-count the
  budget.
- A failed write is logged, never raised: a lost row must not fail paid work.
- Vendor-SDK retries happen below this wrapper, so a call retried after a 429 is one row.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.utils import timezone
from pydantic_ai import RunContext
from pydantic_ai.messages import (
    InstructionPart,
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelResponse,
)
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RequestUsage

from apps.core.db import db
from apps.llm import budget, pricing
from apps.llm.context import LlmContext, current_llm_context
from apps.llm.models import LlmCall
from apps.llm.usage import TokenCounts, tokens_from_usage

log = logging.getLogger("foray.llm")

CHARS_PER_TOKEN = 4
ERROR_MAX = 2000


@dataclass(frozen=True, slots=True)
class LedgerRow:
    purpose: str
    ref: str
    tokens: TokenCounts
    cost_usd: Decimal
    latency_ms: int
    error: str | None
    context: LlmContext
    label: str | None

    def fields(self) -> dict[str, Any]:
        provider, _, model_name = self.ref.partition(":")
        return {
            "run_id": _as_uuid(self.context.run_id),
            "conversation_id": _as_uuid(self.context.conversation_id),
            "purpose": self.purpose,
            "provider": provider,
            "model": model_name,
            "input_tokens": self.tokens.ledger_input_tokens,
            "output_tokens": self.tokens.output_tokens,
            "cached_input_tokens": self.tokens.cached_input_tokens,
            "reasoning_tokens": self.tokens.reasoning_tokens,
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
            "error": self.error,
            "label": self.label,
        }


def _as_uuid(value: str | uuid.UUID | None) -> uuid.UUID | None:
    if value is None or isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def insert_row(row: LedgerRow) -> LlmCall:
    """Sync: run through db()."""
    user_id = row.context.user_id
    if user_id:
        return LlmCall.objects.create_for(user_id, **row.fields())
    return LlmCall.objects.unscoped("ledger: a system call has no user").create(**row.fields())


def _is_cancellation(exc: BaseException) -> bool:
    return isinstance(exc, asyncio.CancelledError | GeneratorExit)


def describe_error(exc: BaseException) -> str:
    if _is_cancellation(exc):
        return "cancelled"
    return repr(exc)[:ERROR_MAX]


def estimate_input_tokens(
    messages: Sequence[ModelMessage], params: ModelRequestParameters | None
) -> int:
    """`ceil(len(serialised request messages and instructions) / 4)`."""
    try:
        chars = len(ModelMessagesTypeAdapter.dump_json(list(messages)).decode())
    except Exception:  # an estimate must never fail the row
        chars = 0
    parts = (params.instruction_parts if params is not None else None) or []
    if parts:
        chars += len(InstructionPart.join(parts) or "")
    return math.ceil(chars / CHARS_PER_TOKEN)


class LedgerModel(WrapperModel):
    """Writes one `llm_calls` row per model request, whatever happens."""

    def __init__(self, wrapped: Model, *, purpose: str, ref: str, label: str | None = None):
        super().__init__(wrapped)
        self.purpose = purpose
        self.ref = ref
        self.fixed_label = label  # "fake" for the LLM_FAKE stack: it wins over the context's

    def _row(
        self,
        usage: RequestUsage | None,
        started: float,
        context: LlmContext,
        *,
        error: BaseException | None,
        messages: Sequence[ModelMessage],
        params: ModelRequestParameters,
    ) -> LedgerRow:
        tokens = tokens_from_usage(usage)
        error_text = describe_error(error) if error is not None else None
        if (
            error is not None
            and _is_cancellation(error)
            and tokens.input_tokens + tokens.cached_input_tokens + tokens.cache_write_tokens == 0
        ):
            tokens = TokenCounts(input_tokens=estimate_input_tokens(messages, params))
            error_text = "cancelled (input estimated)"
        cost = pricing.to_column(pricing.cost_of(self.ref, tokens, at=timezone.now()))
        return LedgerRow(
            purpose=self.purpose,
            ref=self.ref,
            tokens=tokens,
            cost_usd=cost,
            latency_ms=max(round((time.perf_counter() - started) * 1000), 0),
            error=error_text,
            context=context,
            label=self.fixed_label or context.label,
        )

    async def _record(self, row: LedgerRow) -> None:
        try:
            await db(insert_row, row)
            budget.note_spend(row.cost_usd)
        except Exception:
            log.exception("failed to write ledger row (%s %s)", row.purpose, row.ref)

    async def _write(self, row: LedgerRow) -> None:
        """Shielded: a cancelled caller still gets its row written."""
        await asyncio.shield(self._record(row))

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        context = current_llm_context()
        started = time.perf_counter()
        try:
            response = await self.wrapped.request(
                messages, model_settings, model_request_parameters
            )
        except BaseException as exc:
            row = self._row(
                None,
                started,
                context,
                error=exc,
                messages=messages,
                params=model_request_parameters,
            )
            await self._write(row)
            raise
        row = self._row(
            response.usage,
            started,
            context,
            error=None,
            messages=messages,
            params=model_request_parameters,
        )
        await self._write(row)
        return response

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        context = current_llm_context()
        started = time.perf_counter()
        stream: StreamedResponse | None = None
        error: BaseException | None = None
        try:
            async with self.wrapped.request_stream(
                messages, model_settings, model_request_parameters, run_context
            ) as stream:
                yield stream
        except BaseException as exc:
            error = exc
            raise
        finally:
            row = self._row(
                stream.usage if stream is not None else None,
                started,
                context,
                error=error,
                messages=messages,
                params=model_request_parameters,
            )
            await self._write(row)
