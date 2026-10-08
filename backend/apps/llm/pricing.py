"""List prices per million tokens, checked 2026-09-17 (design §8.9; a port of `pricing.ts`).

These exist so the ledger can attribute real dollars per purpose and per model, and so an
approval card can quote a cost before the user says yes. Rates change: `manage.py llm_bakeoff`
is the moment to re-check them, and ledger rows keep the cost computed at call time rather than
recomputing from whatever this file says later.

An unpriced ref costs 0 and logs one warning per ref per process: the ledger row is still
written, because a call we cannot price is exactly the one worth noticing. `fake:*` refs (the
fakes of §8.9) cost 0 silently.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from apps.llm.usage import TokenCounts

log = logging.getLogger("foray.llm")

FAKE_PROVIDER = "fake"


@dataclass(frozen=True, slots=True)
class Rate:
    """USD per million tokens."""

    input: float
    output: float
    # Input served from the provider's prompt cache. None: the provider does not discount
    # cached input, so it bills at the normal input rate.
    cached_input: float | None = None
    # Writing the cache. Anthropic charges this; the others fold it into the input rate.
    cache_write: float | None = None
    # Set only when a vendor bills reasoning tokens *on top of* output tokens. Every provider
    # here counts them inside output already, so the default is 0: adding them again would
    # double-count the expensive half of a reasoning model's bill.
    reasoning: float | None = None


@dataclass(frozen=True, slots=True)
class Band:
    from_hour_utc: int  # inclusive
    to_hour_utc: int  # exclusive; wraps past midnight when smaller than from_hour_utc
    rate: Rate


@dataclass(frozen=True, slots=True)
class TimeBandedRate:
    """DeepSeek bills different rates by time of day, in UTC. The off-peak window is a real lever
    for bulk scoring: a nightly re-score costs half what the same run costs at midday."""

    bands: tuple[Band, ...]
    fallback: Rate


type Pricing = Rate | TimeBandedRate

# Keyed by `provider:model`, the same string shape as the LLM_* settings.
PRICING: dict[str, Pricing] = {
    # --- cheap tier ---------------------------------------------------------------------------
    "deepseek:deepseek-flash": TimeBandedRate(
        # Off-peak is 16:30-00:30 UTC; we approximate on hour boundaries, which errs toward
        # over-reporting cost rather than under-reporting it.
        bands=(Band(17, 0, Rate(input=0.15, output=0.6, cached_input=0.015)),),
        fallback=Rate(input=0.3, output=1.2, cached_input=0.03),
    ),
    "openai:gpt-5.6-luna": Rate(input=0.2, output=1.2, cached_input=0.02),
    "google:gemini-3.8-flash": Rate(input=0.75, output=3.75, cached_input=0.19),
    "anthropic:claude-haiku-4-5": Rate(input=1, output=5, cached_input=0.1, cache_write=1.25),
    # --- strong tier --------------------------------------------------------------------------
    "openai:gpt-5.6-terra": Rate(input=2, output=12, cached_input=0.2),
    "google:gemini-3.1-pro-preview": Rate(input=2, output=12, cached_input=0.5),
    "anthropic:claude-sonnet-5": Rate(input=2, output=10, cached_input=0.2, cache_write=2.5),
    "anthropic:claude-opus-5": Rate(input=5, output=25, cached_input=0.5, cache_write=6.25),
}

_FREE = Rate(input=0, output=0)
_warned: set[str] = set()


def is_fake(ref: str) -> bool:
    return ref.split(":", 1)[0] == FAKE_PROVIDER


def _hour_in_band(hour: int, start: int, end: int) -> bool:
    return start <= hour < end if start <= end else hour >= start or hour < end


def rate_for(ref: str, at: datetime | None = None) -> Rate | None:
    """The rate in force for a model now (or at `at`, for reproducing a row). None: unpriced."""
    if is_fake(ref):
        return _FREE
    pricing = PRICING.get(ref)
    if pricing is None:
        return None
    if isinstance(pricing, Rate):
        return pricing
    hour = (at or datetime.now(UTC)).astimezone(UTC).hour
    for band in pricing.bands:
        if _hour_in_band(hour, band.from_hour_utc, band.to_hour_utc):
            return band.rate
    return pricing.fallback


def is_priced(ref: str) -> bool:
    """Whether we can quote a price for this model, e.g. on an approval card."""
    return is_fake(ref) or ref in PRICING


def cost_usd(
    ref: str,
    input_tokens: int = 0,
    cached_input_tokens: int = 0,
    cache_write_tokens: int = 0,
    output_tokens: int = 0,
    reasoning_tokens: int = 0,
    *,
    at: datetime | None = None,
) -> float:
    """Cost in USD. `input_tokens` are the uncached ones, billed at the full rate.

    Returns 0 for an unpriced model rather than raising: a missing price must not be able to
    fail a job that already spent the money.
    """
    rate = rate_for(ref, at)
    if rate is None:
        if ref not in _warned:
            _warned.add(ref)
            log.warning(
                "no price for %s: its ledger rows cost 0. Add it to apps/llm/pricing.py", ref
            )
        return 0.0

    def per_million(count: int, price: float) -> float:
        return (count / 1_000_000) * price

    return (
        per_million(input_tokens, rate.input)
        + per_million(
            cached_input_tokens, rate.input if rate.cached_input is None else rate.cached_input
        )
        + per_million(
            cache_write_tokens, rate.input if rate.cache_write is None else rate.cache_write
        )
        + per_million(output_tokens, rate.output)
        + per_million(reasoning_tokens, rate.reasoning or 0)
    )


def cost_of(ref: str, tokens: TokenCounts, *, at: datetime | None = None) -> float:
    return cost_usd(
        ref,
        tokens.input_tokens,
        tokens.cached_input_tokens,
        tokens.cache_write_tokens,
        tokens.output_tokens,
        tokens.reasoning_tokens,
        at=at,
    )


def to_column(cost: float) -> Decimal:
    """The `numeric(12, 6)` value the ledger stores: `cost.toFixed(6)` in TS, which rounds the
    exact binary value half up (costs are never negative)."""
    return Decimal(cost).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
