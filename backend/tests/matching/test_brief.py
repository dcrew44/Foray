"""The search brief (R15; a port of `src/test/unit/brief.test.ts`, plus the `describeBrief`
goldens). A search follows its request: the brief overrides the saved preferences for one run
and never changes them. Company names match as whole words at the start, with Postgres's `\\y`
spelled as Python's `\\b` here.
"""

from __future__ import annotations

import re

import pytest

from apps.accounts.schemas import Preferences
from apps.matching.brief import (
    EMPTY_BRIEF,
    SAVED_PREFERENCES_PROMPT,
    SearchBrief,
    apply_brief,
    clean_brief,
    describe_brief,
    is_empty_brief,
    merge_brief,
    parse_brief,
    request_text,
)
from apps.matching.prefilter import company_name_pattern
from apps.runs.live import to_live_run
from tests.factories import make_run, make_user
from tests.matching.conftest import golden

SAVED = Preferences(
    target_roles=["Senior Backend Engineer"],
    seniority="senior",
    locations=["San Francisco Bay Area"],
    remote_pref="any",
)


def brief(**fields: object) -> SearchBrief:
    return EMPTY_BRIEF.model_copy(update=fields)


def test_overrides_only_what_the_request_states() -> None:
    effective = apply_brief(
        SAVED, brief(seniority="entry", roles=["software engineer"], companies=["Anthropic"])
    )
    assert effective.seniority == "entry"
    assert effective.target_roles == ["software engineer"]
    assert effective.locations == ["San Francisco Bay Area"]
    assert effective.remote_pref == "any"
    # The saved preferences themselves are untouched.
    assert SAVED.seniority == "senior"


def test_changes_nothing_when_the_brief_is_empty() -> None:
    assert apply_brief(SAVED, EMPTY_BRIEF) == SAVED
    assert is_empty_brief(EMPTY_BRIEF)
    assert not is_empty_brief(brief(focus="fintech"))


def test_the_chat_agents_hints_win_field_by_field() -> None:
    merged = merge_brief(
        {"companies": ["anthropic"], "seniority": "mid", "focus": "fintech only"},
        {"seniority": "entry", "roles": ["developer"], "companies": []},
    )
    assert merged.seniority == "entry"
    assert merged.roles == ["developer"]
    # An empty list is "not stated", so the derived value stands.
    assert merged.companies == ["anthropic"]
    assert merged.focus == "fintech only"
    assert merge_brief({"remote": "onsite"}, None).remote == "onsite"


def test_cleans_blanks_and_duplicates_and_reads_junk_as_no_brief() -> None:
    assert clean_brief({"companies": [" Anthropic ", "anthropic", ""], "focus": "  "}) == brief(
        companies=["Anthropic"]
    )
    assert parse_brief({"seniority": "wizard"}) == EMPTY_BRIEF
    assert parse_brief({"roles": "not a list"}) == EMPTY_BRIEF
    assert parse_brief(None) == EMPTY_BRIEF
    assert parse_brief("junk") == EMPTY_BRIEF
    # Unknown keys are ignored, as zod strips them.
    assert parse_brief({"companies": ["Stripe"], "extra": 1}) == brief(companies=["Stripe"])


def test_describes_a_search_in_one_line_or_not_at_all() -> None:
    assert (
        describe_brief(
            brief(
                seniority="entry",
                roles=["software engineer"],
                companies=["Anthropic"],
                locations=["San Francisco Bay Area"],
            )
        )
        == "Entry-level software engineer roles at Anthropic, in San Francisco Bay Area"
    )
    assert (
        describe_brief(brief(companies=["Stripe", "Plaid", "Ramp"], remote="remote"))
        == "Roles at Stripe, Plaid or Ramp, remote only"
    )
    assert describe_brief(brief(focus="fintech only")) is None
    # A role that already names its level is not given a second one.
    assert (
        describe_brief(brief(seniority="senior", roles=["Senior Backend Engineer"]))
        == "Senior Backend Engineer roles"
    )
    # JavaScript's ASCII `\b`: "Sr." before a space, "Internal" is not "intern".
    assert describe_brief(brief(seniority="mid", roles=["Sr. engineer"])) == "Sr. engineer roles"
    assert (
        describe_brief(brief(seniority="mid", roles=["Internal tools engineer"]))
        == "Mid-level Internal tools engineer roles"
    )


@pytest.mark.parametrize("case", golden("strings/describe_brief.json"))
def test_describe_brief_matches_the_typescript(case: dict[str, object]) -> None:
    assert describe_brief(SearchBrief.model_validate(case["input"])) == case["output"]


def test_treats_the_home_buttons_placeholder_as_no_request() -> None:
    assert request_text(SAVED_PREFERENCES_PROMPT) is None
    assert request_text("  ") is None
    assert request_text(None) is None
    assert request_text(" roles at Anthropic ") == "roles at Anthropic"


def _matches(name: str, company: str) -> bool:
    pattern = company_name_pattern(name)
    assert pattern is not None
    return re.search(pattern.replace("\\y", "\\b"), company, re.IGNORECASE | re.ASCII) is not None


def test_matches_a_company_name_as_whole_words_at_the_start() -> None:
    assert _matches("Anthropic", "Anthropic")
    assert _matches("anthropic", "Anthropic PBC")
    assert _matches("Scale AI", "Scale AI, Inc.")
    assert not _matches("Meta", "Metabase")
    assert not _matches("Anthropic", "Not Anthropic")


def test_has_nothing_to_match_for_a_name_with_no_letters_or_digits() -> None:
    assert company_name_pattern(" - ") is None


@pytest.mark.django_db
def test_the_live_run_card_describes_a_searchs_brief() -> None:
    """Step 4's `LiveRun.summary` reads `parse_brief` and `describe_brief` from here: the stored
    brief once it is derived, the chat agent's hints before that."""
    user = make_user()
    derived = make_run(
        user, input={"prompt": "x", "brief": {"seniority": "entry", "companies": ["Anthropic"]}}
    )
    assert to_live_run(derived).summary == "Entry-level roles at Anthropic"
    hinted = make_run(user, input={"prompt": "x", "hints": {"roles": ["data analyst"]}})
    assert to_live_run(hinted).summary == "Data analyst roles"
    assert to_live_run(make_run(user, input={"prompt": "x", "hints": None})).summary is None
