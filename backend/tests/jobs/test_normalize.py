"""Locations, remote status and salaries (ported from `normalize.test.ts`, 30 cases), plus golden
equality with the TypeScript `normalize.ts` (design §12.2)."""

from __future__ import annotations

import pytest

from apps.jobs.sources.normalize import (
    detect_remote,
    is_plausible_salary,
    parse_location,
    parse_salary,
    primary_location,
)
from apps.jobs.sources.types import NormalizedSalary
from tests.jobs.conftest import golden

# --- goldens -------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", golden("normalize/locations.json"))
def test_parse_location_matches_golden(case: dict) -> None:
    assert parse_location(case["input"]).dump() == case["output"]


@pytest.mark.parametrize("case", golden("normalize/primary_location.json"))
def test_primary_location_matches_golden(case: dict) -> None:
    location = primary_location(case["input"])
    assert (location.dump() if location else None) == case["output"]


@pytest.mark.parametrize("case", golden("normalize/remote.json"))
def test_detect_remote_matches_golden(case: dict) -> None:
    given = case["input"]
    remote = detect_remote(
        location=given.get("location"),
        title=given.get("title"),
        is_remote_flag=given.get("isRemoteFlag"),
        description=given.get("description"),
    )
    assert remote == case["output"]


@pytest.mark.parametrize("case", golden("normalize/salaries.json"))
def test_parse_salary_matches_golden(case: dict) -> None:
    assert parse_salary(case["input"]).dump() == case["output"]


@pytest.mark.parametrize("case", golden("normalize/plausible_salary.json"))
def test_is_plausible_salary_matches_golden(case: dict) -> None:
    assert is_plausible_salary(NormalizedSalary(**case["input"])) is case["output"]


# --- parseLocation -------------------------------------------------------------------------------


def test_splits_a_us_city_and_state() -> None:
    location = parse_location("San Francisco, CA")
    assert (location.city, location.region, location.country, location.metro) == (
        "San Francisco",
        "CA",
        "US",
        "bay-area",
    )


def test_expands_a_spelled_out_state() -> None:
    location = parse_location("Austin, Texas")
    assert (location.city, location.region, location.country, location.metro) == (
        "Austin",
        "TX",
        "US",
        None,
    )


def test_recognises_a_metro_named_directly() -> None:
    location = parse_location("SF Bay Area")
    assert location.city is None
    assert location.metro == "bay-area"


def test_maps_suburbs_onto_the_same_metro() -> None:
    for place in ("Mountain View, CA", "Oakland, CA", "Palo Alto, CA"):
        assert parse_location(place).metro == "bay-area"


def test_strips_a_trailing_remote_qualifier() -> None:
    location = parse_location("New York, NY (Remote)")
    assert (location.city, location.region) == ("New York", "NY")
    assert parse_location("Seattle, WA - Hybrid").city == "Seattle"


def test_keeps_the_source_words_in_raw() -> None:
    raw = "  San Francisco, CA (Remote)  "
    assert parse_location(raw).raw == raw


def test_leaves_an_unrecognised_place_unguessed() -> None:
    location = parse_location("Remote")
    assert location.metro is None
    assert location.country is None


def test_handles_a_non_us_location() -> None:
    location = parse_location("London, United Kingdom")
    assert (location.city, location.country, location.metro) == ("London", "GB", None)


# --- primaryLocation -----------------------------------------------------------------------------


def test_primary_prefers_a_recognised_location() -> None:
    location = primary_location(["Dublin, Ireland", "San Jose, CA"])
    assert location is not None and location.metro == "bay-area"


def test_primary_falls_back_to_the_first() -> None:
    location = primary_location(["Dublin, Ireland", "Berlin"])
    assert location is not None and location.city == "Dublin"


def test_primary_is_none_without_locations() -> None:
    assert primary_location([]) is None


# --- detectRemote --------------------------------------------------------------------------------


def test_trusts_an_explicit_flag() -> None:
    assert detect_remote(is_remote_flag=True, location="San Francisco") == "remote"


def test_prefers_hybrid_over_remote() -> None:
    assert detect_remote(location="Hybrid Remote - Austin, TX") == "hybrid"


def test_reads_remote_from_the_location_or_title() -> None:
    assert detect_remote(location="Remote - US") == "remote"
    assert detect_remote(title="Software Engineer (Remote)", location="") == "remote"


def test_treats_a_bare_city_as_onsite() -> None:
    assert detect_remote(location="San Francisco, CA") == "onsite"


def test_a_passing_mention_does_not_outvote_the_location() -> None:
    remote = detect_remote(
        location="San Francisco, CA",
        description="You will work with remote teams across the company.",
    )
    assert remote == "onsite"


def test_falls_back_to_the_description() -> None:
    assert detect_remote(location="", description="This is a fully remote position.") == "remote"


def test_says_unknown_rather_than_guessing() -> None:
    assert detect_remote() == "unknown"


def test_word_boundaries_are_ascii() -> None:
    # JS `\b` is ASCII: "é" is not a word character, so "éremote" still has a boundary.
    assert detect_remote(title="éremote") == "remote"


# --- parseSalary ---------------------------------------------------------------------------------


def test_reads_a_plain_annual_range() -> None:
    salary = parse_salary("The salary range is $150,000 - $200,000 per year")
    assert salary.dump() == {"min": 150000, "max": 200000, "currency": "USD", "period": "year"}


def test_carries_a_k_suffix_across_the_range() -> None:
    salary = parse_salary("$180-220k annually")
    assert (salary.min, salary.max) == (180_000, 220_000)


def test_reads_an_hourly_rate() -> None:
    salary = parse_salary("$55 - $70 per hour")
    assert (salary.min, salary.max, salary.period) == (55, 70, "hour")


def test_picks_up_a_non_dollar_currency() -> None:
    assert parse_salary("£70,000 \u2013 £95,000 per year").currency == "GBP"
    assert parse_salary("Salary: 90,000 - 120,000 EUR annually").currency == "EUR"


def test_ignores_a_year_range() -> None:
    assert parse_salary("Founded 2015 - 2020, we have grown fast").min is None


def test_ignores_a_headcount_range() -> None:
    assert parse_salary("a team of 5 - 10 engineers").min is None


def test_ignores_a_backwards_range() -> None:
    assert parse_salary("$200,000 - $150,000").min is None


def test_empty_for_text_without_a_range() -> None:
    assert parse_salary("Competitive salary and equity") == NormalizedSalary()
    assert parse_salary(None).min is None


# --- isPlausibleSalary ---------------------------------------------------------------------------


def test_rejects_an_annual_salary_in_thousands() -> None:
    assert not is_plausible_salary(NormalizedSalary(90, 90, "USD", "year"))


def test_accepts_a_real_annual_range() -> None:
    assert is_plausible_salary(NormalizedSalary(180_000, 220_000, "USD", "year"))


def test_accepts_an_hourly_range_at_hourly_magnitudes() -> None:
    assert is_plausible_salary(NormalizedSalary(35, 38, "CAD", "hour"))
    assert not is_plausible_salary(NormalizedSalary(35, 38, "CAD", "year"))


def test_rejects_a_backwards_or_empty_range() -> None:
    assert not is_plausible_salary(NormalizedSalary(200_000, 150_000, "USD", "year"))
    assert not is_plausible_salary(NormalizedSalary())
