"""Token accounting (the six `token-accounting.test.ts` cases, reshaped onto `RequestUsage`), plus
each vendor's real usage shape as pydantic-ai normalises it (`research/llm-library.md` §4)."""

from __future__ import annotations

import pytest
from pydantic_ai import Agent
from pydantic_ai.usage import RequestUsage

from apps.llm.pricing import cost_of
from apps.llm.usage import TokenCounts, tokens_from_usage
from tests.llm.vendors import Answer, Vendor

# --- the ported cases ----------------------------------------------------------------------------


def test_splits_cached_from_uncached_input() -> None:
    tokens = tokens_from_usage(
        RequestUsage(input_tokens=1000, cache_read_tokens=600, output_tokens=120)
    )
    assert tokens.input_tokens == 400
    assert tokens.cached_input_tokens == 600
    assert tokens.output_tokens == 120


def test_derives_uncached_input_from_the_total() -> None:
    tokens = tokens_from_usage(RequestUsage(input_tokens=1000, cache_read_tokens=250))
    assert tokens.input_tokens == 750
    assert tokens.cached_input_tokens == 250


def test_never_reports_negative_input_if_the_totals_disagree() -> None:
    tokens = tokens_from_usage(RequestUsage(input_tokens=100, cache_read_tokens=400))
    assert tokens.input_tokens == 0


def test_carries_anthropic_cache_writes_through_to_cost() -> None:
    # pydantic-ai's input_tokens include cache reads and writes.
    usage = RequestUsage(
        input_tokens=1_001_000, cache_read_tokens=1000, cache_write_tokens=1_000_000
    )
    tokens = tokens_from_usage(usage)
    assert tokens.input_tokens == 0
    assert tokens.cache_write_tokens == 1_000_000
    assert tokens.ledger_input_tokens == 1_000_000  # the column: uncached + cache-write
    # Cache writes bill at $1.25/M on Haiku 4.5, cache reads at $0.10/M.
    assert cost_of("anthropic:claude-haiku-4-5", tokens) == pytest.approx(1.25 + 0.0001, abs=1e-6)


def test_keeps_reasoning_visible_without_charging_it_twice() -> None:
    tokens = tokens_from_usage(RequestUsage(output_tokens=900, details={"reasoning_tokens": 500}))
    assert tokens.reasoning_tokens == 500
    assert tokens.output_tokens == 900
    # 900 output tokens on Terra at $12/M: the 500 reasoning tokens are already inside them.
    assert cost_of("openai:gpt-5.6-terra", tokens) == pytest.approx(900 / 1_000_000 * 12, abs=1e-10)


def test_a_missing_usage_object_is_a_zero_cost_row() -> None:
    tokens = tokens_from_usage(None)
    assert tokens == TokenCounts()
    assert cost_of("openai:gpt-5.6-luna", tokens) == 0


# --- fixed: no double count when only a total and cache writes are reported -----------------------


def test_cache_writes_are_not_counted_twice() -> None:
    """TS added cache writes on top of a `noCache` derived from the total, which already held them
    (`map/llm.md` §8c). Here the uncached share excludes them."""
    tokens = tokens_from_usage(RequestUsage(input_tokens=1000, cache_write_tokens=800))
    assert tokens.input_tokens == 200
    assert tokens.ledger_input_tokens == 1000


def test_counts_add_up() -> None:
    one = TokenCounts(input_tokens=1, output_tokens=2, cached_input_tokens=3, cache_write_tokens=4)
    assert one + one == TokenCounts(2, 4, 6, 8, 0)


# --- each vendor's usage, through the real model classes ----------------------------------------

# (ref, ledger input_tokens, cached, output, reasoning)
VENDORS = [
    ("openai:gpt-5.6-luna", 400, 600, 300, 200),  # reasoning in details["reasoning_tokens"]
    ("deepseek:deepseek-flash", 400, 600, 300, 200),  # prompt_cache_hit_tokens
    ("anthropic:claude-haiku-4-5", 600, 400, 300, 200),  # 100 uncached + 500 written; thinking
    ("google:gemini-3.8-flash", 400, 600, 300, 200),  # 100 candidates + 200 thoughts = output
]


@pytest.mark.parametrize(("ref", "ledger_input", "cached", "output", "reasoning"), VENDORS)
async def test_vendor_usage(
    ref: str, ledger_input: int, cached: int, output: int, reasoning: int
) -> None:
    vendor = Vendor(ref)
    result = await Agent(vendor.base_model(), output_type=Answer).run("Which city?")
    tokens = tokens_from_usage(result.usage)
    assert tokens.ledger_input_tokens == ledger_input
    assert tokens.cached_input_tokens == cached
    assert tokens.output_tokens == output
    assert tokens.reasoning_tokens == reasoning


async def test_anthropic_cost_prices_each_input_kind() -> None:
    vendor = Vendor("anthropic:claude-haiku-4-5")
    result = await Agent(vendor.base_model(), output_type=Answer).run("Which city?")
    tokens = tokens_from_usage(result.usage)
    assert (tokens.input_tokens, tokens.cache_write_tokens, tokens.cached_input_tokens) == (
        100,
        500,
        400,
    )
    expected = (100 * 1 + 500 * 1.25 + 400 * 0.1 + 300 * 5) / 1_000_000
    assert cost_of(vendor.ref, tokens) == pytest.approx(expected, abs=1e-12)
