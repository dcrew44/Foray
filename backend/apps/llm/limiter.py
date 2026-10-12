"""A per-provider request-rate ceiling (design §8.7).

Scoring a few hundred postings fires calls as fast as the fan-out allows, which is well past
what a new provider account is allowed. Waiting our turn here is cheaper than a 429 storm, and
the bucket is per provider, so a DeepSeek scoring run does not consume the chat model's
headroom. Unlike TS, the limiter sits on the model (`RateLimited`), so the chat agent's loop
is gated too.

The bucket is the reservation bucket of `research/llm-library.md` §8: `reserve()` runs
synchronously between awaits and returns how long the caller must sleep for its slot, so
waiters are served FIFO without a lock bound to one event loop. It starts full
(`LLM_RPM_<PROVIDER>`, default 300). A cancelled waiter keeps its reservation, which
over-throttles slightly. Buckets are per process: prod runs one API and one worker process, so
the worst case is twice the configured rate (§8.7).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Any

from django.conf import settings
from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings

# 60 was far too low and made the limiter, not the provider, the bottleneck (measured: of 80
# scoring calls the first 60 took 9 s and the last 20 took 12.9 s). 300 is comfortably under a
# paid account's limits and ten times what any single pipeline needs.
DEFAULT_RPM = 300


class TokenBucket:
    def __init__(self, rate_per_minute: float, *, clock: Callable[[], float] = time.monotonic):
        if rate_per_minute <= 0:
            raise ValueError("rate_per_minute must be positive")
        self.rate_per_minute = float(rate_per_minute)
        self._clock = clock
        self._tokens = float(rate_per_minute)  # starts full
        self._last = clock()

    @property
    def per_second(self) -> float:
        return self.rate_per_minute / 60

    def reserve(self) -> float:
        """Take one token, possibly borrowed from the future: the seconds to wait for it."""
        now = self._clock()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self.rate_per_minute, self._tokens + elapsed * self.per_second)
            self._last = now
        self._tokens -= 1
        return 0.0 if self._tokens >= 0 else -self._tokens / self.per_second

    async def acquire(self) -> None:
        delay = self.reserve()
        if delay > 0:
            await asyncio.sleep(delay)


_BUCKETS: dict[str, TokenBucket] = {}


def rpm_for(provider: str) -> int:
    return getattr(settings.ENV, f"llm_rpm_{provider}", None) or DEFAULT_RPM


def bucket_for(provider: str) -> TokenBucket:
    bucket = _BUCKETS.get(provider)
    if bucket is None:
        bucket = _BUCKETS[provider] = TokenBucket(rpm_for(provider))
    return bucket


class RateLimited(WrapperModel):
    """Takes one token from the provider's bucket per model request."""

    def __init__(self, wrapped: Model, *, provider: str):
        super().__init__(wrapped)
        self.provider_id = provider

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        await bucket_for(self.provider_id).acquire()
        return await self.wrapped.request(messages, model_settings, model_request_parameters)

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        await bucket_for(self.provider_id).acquire()
        async with self.wrapped.request_stream(
            messages, model_settings, model_request_parameters, run_context
        ) as stream:
            yield stream
