"""Pricing (the ten `pricing.test.ts` cases, plus the fakes and the warn-once rule)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apps.llm import pricing
from apps.llm.pricing import Band, Rate, TimeBandedRate, cost_usd, is_priced, rate_for, to_column

PEAK = datetime(2026, 9, 18, 8, 0, tzinfo=UTC)  # a Friday, inside DeepSeek's 06:00-10:00 window
OFF_PEAK = datetime(2026, 9, 18, 20, 0, tzinfo=UTC)
BETWEEN_WINDOWS = datetime(2026, 9, 18, 5, 0, tzinfo=UTC)  # after 01:00-04:00, before 06:00
WEEKEND = datetime(2026, 9, 19, 8, 0, tzinfo=UTC)  # Saturday at the weekday peak hour


# --- rate_for ------------------------------------------------------------------------------------


def test_unpriced_model_has_no_rate() -> None:
    assert rate_for("openai:some-unreleased-model") is None
    assert is_priced("openai:some-unreleased-model") is False


def test_picks_deepseek_band_by_utc_hour() -> None:
    peak, off_peak = (
        rate_for("deepseek:deepseek-flash", PEAK),
        rate_for("deepseek:deepseek-flash", OFF_PEAK),
    )
    assert peak is not None and (peak.input, peak.output, peak.cached_input) == (0.3, 1.2, 0.006)
    assert off_peak is not None
    assert (off_peak.input, off_peak.output, off_peak.cached_input) == (0.15, 0.6, 0.003)


@pytest.mark.parametrize("at", [BETWEEN_WINDOWS, WEEKEND, datetime(2026, 9, 18, 10, tzinfo=UTC)])
def test_deepseek_is_off_peak_outside_its_weekday_windows(at: datetime) -> None:
    rate = rate_for("deepseek:deepseek-flash", at)
    assert rate is not None and rate.input == 0.15


def test_deepseek_pro_has_the_same_hours() -> None:
    peak, off_peak = (
        rate_for("deepseek:deepseek-v4-pro", PEAK),
        rate_for("deepseek:deepseek-v4-pro", OFF_PEAK),
    )
    assert peak is not None and (peak.input, peak.output) == (1.32, 3.96)
    assert off_peak is not None and (off_peak.input, off_peak.output) == (0.66, 1.98)


def test_a_band_that_wraps_past_midnight_is_one_window(monkeypatch: pytest.MonkeyPatch) -> None:
    night = Rate(input=1, output=1)
    banded = TimeBandedRate(bands=(Band(17, 0, night),), fallback=Rate(input=2, output=2))
    monkeypatch.setitem(pricing.PRICING, "compat:banded", banded)
    assert rate_for("compat:banded", datetime(2026, 9, 18, 23, tzinfo=UTC)) == night
    # 17:00-00:00 does not contain 02:00, so this must fall back.
    assert rate_for("compat:banded", datetime(2026, 9, 18, 2, tzinfo=UTC)) == banded.fallback


def test_a_flat_rate_ignores_the_hour() -> None:
    assert rate_for("openai:gpt-5.6-luna", PEAK) == rate_for("openai:gpt-5.6-luna", OFF_PEAK)


def test_band_reads_the_utc_hour_of_an_aware_time() -> None:
    from zoneinfo import ZoneInfo

    # 13:00 in Los Angeles is 20:00 UTC: off-peak.
    local = datetime(2026, 9, 18, 13, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    rate = rate_for("deepseek:deepseek-flash", local)
    assert rate is not None and rate.input == 0.15
    # Thursday 23:30 in Los Angeles is Friday 06:30 UTC: the weekday is read in UTC too.
    thursday_night = datetime(2026, 9, 17, 23, 30, tzinfo=ZoneInfo("America/Los_Angeles"))
    rate = rate_for("deepseek:deepseek-flash", thursday_night)
    assert rate is not None and rate.input == 0.3


# --- cost_usd ------------------------------------------------------------------------------------


def test_bills_input_and_output_at_their_own_rates() -> None:
    # 1M in at $0.20 + 1M out at $1.20.
    assert cost_usd("openai:gpt-5.6-luna", 1_000_000, output_tokens=1_000_000) == pytest.approx(
        1.4, abs=1e-10
    )


def test_bills_cached_input_at_the_cached_rate() -> None:
    assert cost_usd("openai:gpt-5.6-luna", 1_000_000) == pytest.approx(0.2, abs=1e-10)
    assert cost_usd("openai:gpt-5.6-luna", cached_input_tokens=1_000_000) == pytest.approx(
        0.02, abs=1e-10
    )


def test_charges_anthropic_cache_writes_at_the_write_rate() -> None:
    assert cost_usd("anthropic:claude-haiku-4-5", cache_write_tokens=1_000_000) == pytest.approx(
        1.25, abs=1e-10
    )


def test_cache_writes_bill_at_the_input_rate_where_not_priced_separately() -> None:
    assert cost_usd("deepseek:deepseek-flash", cache_write_tokens=1_000_000, at=PEAK) == (
        pytest.approx(0.3, abs=1e-10)
    )


@pytest.mark.parametrize(
    ("ref", "rate"),
    [
        ("openai:gpt-5.6-luna", 0.25),
        ("openai:gpt-5.6-terra", 2.5),
        ("openai:gpt-6-luna", 0.125),
        ("openai:gpt-6.1-terra", 1.25),
    ],
)
def test_gpt_5_6_cache_writes_bill_at_one_and_a_quarter_times_input(ref: str, rate: float) -> None:
    """OpenAI bills GPT-5.6 and GPT-6 cache writes at 1.25x the uncached input rate (30-minute
    retention); pydantic-ai reports
    them as `cache_write_tokens`, so they must not fall back to the input rate."""
    assert cost_usd(ref, cache_write_tokens=1_000_000) == pytest.approx(rate, abs=1e-10)
    # The review's example request: 1,000 uncached, 3,000 written, 1,000 read, 300 out.
    luna = cost_usd(
        "openai:gpt-5.6-luna",
        input_tokens=1_000,
        cache_write_tokens=3_000,
        cached_input_tokens=1_000,
        output_tokens=300,
    )
    assert luna == pytest.approx(0.001330, abs=1e-12)


def test_does_not_double_count_reasoning() -> None:
    # Providers count reasoning inside output already.
    cost = cost_usd("openai:gpt-5.6-terra", output_tokens=1_000_000, reasoning_tokens=500_000)
    assert cost == pytest.approx(12, abs=1e-10)


def test_unpriced_model_costs_zero_and_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="foray.llm"):
        assert cost_usd("compat:whatever", 5_000, output_tokens=5_000) == 0
        assert cost_usd("compat:whatever", 5_000, output_tokens=5_000) == 0
        assert cost_usd("compat:another", 1) == 0
    warnings = [r.getMessage() for r in caplog.records if r.name == "foray.llm"]
    assert len(warnings) == 2
    assert "compat:whatever" in warnings[0] and "compat:another" in warnings[1]


def test_fakes_are_free_and_silent(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="foray.llm"):
        assert cost_usd("fake:score", 1_000_000, output_tokens=1_000_000) == 0
    assert caplog.records == []
    assert is_priced("fake:chat")


def test_off_peak_is_materially_cheaper() -> None:
    day = cost_usd("deepseek:deepseek-flash", 2_000_000, output_tokens=200_000, at=PEAK)
    night = cost_usd("deepseek:deepseek-flash", 2_000_000, output_tokens=200_000, at=OFF_PEAK)
    assert night == pytest.approx(day / 2, abs=1e-10)


def test_column_rounds_like_to_fixed() -> None:
    assert to_column(0.000452) == Decimal("0.000452")
    assert to_column(1.4) == Decimal("1.400000")
    assert to_column(0.0000004) == Decimal("0.000000")
    assert to_column(0.0000006) == Decimal("0.000001")
    assert to_column(0) == Decimal("0.000000")
