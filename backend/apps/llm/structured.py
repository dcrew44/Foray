"""Structured output with one retry on unusable output (design §8.4).

Each call runs a throwaway `Agent(model(purpose), output_type=output, instructions=...,
retries={"output": 1})` with `UsageLimits(request_limit=2)`. The default `ToolOutput` works on
every configured provider.

- **One retry on unusable output**: schema errors and malformed JSON alike (TS missed the
  latter). pydantic-ai continues the conversation with the validation errors, so the model sees
  why it failed. A second failure usually means the prompt or schema is wrong, and looping would
  multiply the bill.
- **No retry on truncation.** A truncated answer would truncate again and only double the bill.
  pydantic-ai 2.54 does retry a truncated output-tool call, so the model is wrapped in a
  `TruncationGuard` (outside the ledger): a response with `finish_reason == "length"` raises
  `OutputTruncated` at once, after its ledger row is written.
- **Prompt layout**: the stable half in `instructions`, the per-item half in `prompt`, for prefix
  caching. Vision prompts pass `BinaryContent(png, media_type="image/png")` items.
- **Timeout**: `timeout` seconds wrap the whole call (both attempts) in `asyncio.timeout`.
  Transport retries are the vendor SDKs' (2, Retry-After aware; Gemini 3 attempts).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, UserContent
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RunUsage, UsageLimits

from apps.llm import registry
from apps.llm.registry import Purpose

__all__ = [
    "OutputTruncated",
    "StructuredResult",
    "TruncationGuard",
    "structured",
    "structured_result",
]

OUTPUT_RETRIES = 1
REQUEST_LIMIT = 2


class OutputTruncated(Exception):
    """The model ran out of output tokens; never retried (a retry would truncate again)."""


class TruncationGuard(WrapperModel):
    def __init__(self, wrapped: Model, *, purpose: str):
        super().__init__(wrapped)
        self.purpose = purpose

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        response = await self.wrapped.request(messages, model_settings, model_request_parameters)
        if response.finish_reason == "length":
            raise OutputTruncated(f"{self.purpose}: the model ran out of output tokens.")
        return response


@dataclass(frozen=True, slots=True)
class StructuredResult[T]:
    output: T
    usage: RunUsage  # summed over the requests of this call
    retried: bool  # the first answer was unusable and the model got one more try


async def structured_result[T](
    purpose: Purpose,
    output: type[T],
    *,
    instructions: str,
    prompt: str | Sequence[UserContent],
    timeout: float | None = None,  # noqa: ASYNC109 (§8.4: wrapped in asyncio.timeout inside)
    model: Model | None = None,
) -> StructuredResult[T]:
    """`structured()` plus the usage and whether it retried (for `llm_smoke`/`llm_bakeoff`).
    `model` replaces `registry.model(purpose)`, e.g. the bake-off's stack for an explicit ref."""
    guarded = TruncationGuard(model or registry.model(purpose), purpose=purpose)
    agent = Agent(
        guarded,
        output_type=cast(Any, output),
        instructions=instructions,
        retries={"output": OUTPUT_RETRIES},
    )
    async with asyncio.timeout(timeout):
        result = await agent.run(prompt, usage_limits=UsageLimits(request_limit=REQUEST_LIMIT))
    usage = result.usage
    return StructuredResult(output=cast(T, result.output), usage=usage, retried=usage.requests > 1)


async def structured[T](
    purpose: Purpose,
    output: type[T],
    *,
    instructions: str,
    prompt: str | Sequence[UserContent],
    timeout: float | None = None,  # noqa: ASYNC109 (§8.4: wrapped in asyncio.timeout inside)
) -> T:
    """One structured answer for `purpose`, validated against `output`."""
    result = await structured_result(
        purpose, output, instructions=instructions, prompt=prompt, timeout=timeout
    )
    return result.output
