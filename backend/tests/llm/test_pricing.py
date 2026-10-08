"""Pricing (the ten `pricing.test.ts` cases, plus the fakes and the warn-once rule)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apps.llm.pricing import cost_usd, is_priced, rate_for, to_column

PEAK = datetime(2026, 9, 18, 16, 0, tzinfo=UTC)  # inside DeepSeek's peak window
OFF_PEAK = datetime(2026, 9, 18, 20, 0, tzinfo=UTC)
AFTER_MIDNIGHT = datetime(2026, 9, 18, 2, 0, tzinfo=UTC)  # off-peak only if the band wrapped


# --- rate_for ------------------------------------------------------------------------------------


def test_unpriced_model_has_no_rate() -> None:
    assert rate_for("openai:some-unreleased-model") is None
    assert is_priced("openai:some-unreleased-model") is False


def test_picks_deepseek_band_by_utc_hour() -> None:
    peak, off_peak = (
        rate_for("deepseek:deepseek-flash", PEAK),
        rate_for("deepseek:deepseek-flash", OFF_PEAK),
    )
    assert peak is not None and peak.input == 0.3
    assert off_peak is not None and off_peak.input == 0.15


def test_a_band_that_wraps_past_midnight_is_one_window() -> None:
    # 17:00-00:00 does not contain 02:00, so this must fall back to peak.
    rate = rate_for("deepseek:deepseek-flash", AFTER_MIDNIGHT)
    assert rate is not None and rate.input == 0.3


def test_a_flat_rate_ignores_the_hour() -> None:
    assert rate_for("openai:gpt-5.6-luna", PEAK) == rate_for("openai:gpt-5.6-luna", OFF_PEAK)


def test_band_reads_the_utc_hour_of_an_aware_time() -> None:
    from zoneinfo import ZoneInfo

    # 13:00 in Los Angeles is 20:00 UTC: off-peak.
    local = datetime(2026, 9, 18, 13, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    rate = rate_for("deepseek:deepseek-flash", local)
    assert rate is not None and rate.input == 0.15


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
    assert cost_usd("openai:gpt-5.6-luna", cache_write_tokens=1_000_000) == pytest.approx(
        0.2, abs=1e-10
    )


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
