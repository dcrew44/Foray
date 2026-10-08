"""Token accounting: pydantic-ai's `RequestUsage` onto the ledger's columns (design §8.5).

pydantic-ai normalises usage across vendors (`research/llm-library.md` §4):

- `input_tokens` **includes** cache reads and cache writes for every vendor (Anthropic's raw
  `input_tokens` are re-summed);
- `output_tokens` **includes** reasoning tokens for every vendor;
- reasoning is reported in `details` under a vendor-specific key.

The ledger keeps today's column meanings: `input_tokens` is uncached plus cache-write tokens,
`cached_input_tokens` are cache reads, and cost prices the three input kinds separately.
Computing the uncached share as `input - cache_read - cache_write` (never below 0) also removes
TS's possible double count when a provider reported only a total plus cache writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

# openai and deepseek / anthropic / google
REASONING_KEYS = ("reasoning_tokens", "thinking_tokens", "thoughts_tokens")


class Usage(Protocol):
    """`RequestUsage` and `RunUsage` both fit."""

    @property
    def input_tokens(self) -> int: ...
    @property
    def cache_read_tokens(self) -> int: ...
    @property
    def cache_write_tokens(self) -> int: ...
    @property
    def output_tokens(self) -> int: ...
    @property
    def details(self) -> dict[str, int]: ...


@dataclass(frozen=True, slots=True)
class TokenCounts:
    """Counts as pricing reads them (`TokenCounts` in `pricing.ts`)."""

    input_tokens: int = 0  # billed at the full input rate (cached and cache-write excluded)
    output_tokens: int = 0  # reasoning included
    cached_input_tokens: int = 0  # cache reads
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0  # informational: already inside output_tokens

    @property
    def ledger_input_tokens(self) -> int:
        """`llm_calls.input_tokens`: tokens written into a provider cache were still read in
        full, and some providers report most of a prompt that way."""
        return self.input_tokens + self.cache_write_tokens

    def __add__(self, other: TokenCounts) -> TokenCounts:
        return TokenCounts(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
        )


def reasoning_tokens(details: dict[str, int] | None) -> int:
    for key in REASONING_KEYS:
        if value := (details or {}).get(key):
            return value
    return 0


def tokens_from_usage(usage: Usage | None) -> TokenCounts:
    """A missing usage object is a zero-cost row, not an error."""
    if usage is None:
        return TokenCounts()
    cache_read = max(usage.cache_read_tokens or 0, 0)
    cache_write = max(usage.cache_write_tokens or 0, 0)
    uncached = max((usage.input_tokens or 0) - cache_read - cache_write, 0)
    return TokenCounts(
        input_tokens=uncached,
        output_tokens=max(usage.output_tokens or 0, 0),
        cached_input_tokens=cache_read,
        cache_write_tokens=cache_write,
        reasoning_tokens=reasoning_tokens(usage.details),
    )
