"""Structured output (design §8.4). Stub: step 3B implements it.

One throwaway `Agent(model(purpose), output_type=output, instructions=..., retries={"output": 1})`
per call, `UsageLimits(request_limit=2)`: one retry on unusable output, none on truncation.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic_ai.messages import UserContent

from apps.llm.registry import Purpose


class OutputTruncated(Exception):
    """The model ran out of output tokens; never retried (a retry would truncate again)."""


async def structured[T](
    purpose: Purpose,
    output: type[T],
    *,
    instructions: str,
    prompt: str | Sequence[UserContent],
    timeout: float | None = None,  # noqa: ASYNC109 (§8.4: wrapped in asyncio.timeout inside)
) -> T:
    raise NotImplementedError("apps.llm.structured.structured() is built in step 3B")
