"""The prefilter: title terms (a port of `src/test/unit/prefilter.test.ts`) and the SQL itself
over factory jobs (design §12.2): one match per role, dismissed roles excluded, the 45-day
window, the Bay Area regex, the company filter, and the `statement_timeout` it sets first.
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.schemas import Preferences
from apps.matching.prefilter import (
    BAY_AREA_PLACES,
    CompanyFilter,
    DerivedCriteria,
    DerivedSearch,
    escape_for_regex,
    finish_derived,
    guard_seniority,
    prefilter_jobs,
    title_matches,
    without_level,
)
from tests.factories import make_company, make_job, make_match, make_user


def criteria(include: list[str], exclude: list[str] | None = None) -> DerivedCriteria:
    return DerivedCriteria(
        title_include=[escape_for_regex(term) for term in include],
        title_exclude=[escape_for_regex(term) for term in exclude or []],
        keywords=[],
    )


# --- escaping and title terms --------------------------------------------------------------------


def test_escape_neutralises_regex_syntax() -> None:
    # "C++ Engineer" and "Sr." both appear in real derived criteria; unescaped, the first is an
    # invalid quantifier and the second matches anything.
    re.compile(escape_for_regex("C++ engineer"))
    assert not re.search(escape_for_regex("sr. engineer"), "srx engineer", re.IGNORECASE)
    assert escape_for_regex("  A$B (x) [y] {z} | \\ ^ ? * + . ") == (
        "a\\$b \\(x\\) \\[y\\] \\{z\\} \\| \\\\ \\^ \\? \\* \\+ \\."
    )


def test_keeps_a_title_matching_any_include_term() -> None:
    c = criteria(["backend engineer", "platform engineer"])
    assert title_matches("Senior Backend Engineer, Payments", c)
    assert title_matches("Staff Platform Engineer", c)
    assert not title_matches("Account Executive", criteria(["backend engineer"]))


def test_drops_a_title_matching_an_exclusion() -> None:
    c = criteria(["software engineer"], ["senior", "staff", "principal"])
    assert not title_matches("Senior Software Engineer", c)
    assert title_matches("Software Engineer, New Grad", c)


def test_excludes_senior_titles_from_a_new_grad_search() -> None:
    c = criteria(
        ["software engineer", "new grad"], ["senior", "staff", "principal", "director", "manager"]
    )
    for title in ("Senior Software Engineer", "Staff Software Engineer", "Engineering Manager"):
        assert not title_matches(title, c)
    for title in ("Software Engineer, New Grad (2027)", "Software Engineer I"):
        assert title_matches(title, c)


def test_excludes_intern_titles_from_a_senior_search() -> None:
    c = criteria(["senior backend engineer", "backend engineer"], ["intern", "new grad", "junior"])
    assert not title_matches("Backend Engineering Intern", c)
    assert title_matches("Senior Backend Engineer", c)


def test_keeps_everything_when_no_criteria_were_derived() -> None:
    # Better to score a wide set than to silently return nothing.
    assert title_matches("Anything At All", criteria([]))


# What the model proposed for "the most entry-level developer positions at Anthropic": literal
# phrases that missed "Staff+" and shut out new grads.
PROPOSED = DerivedCriteria(
    title_include=["software engineer", "developer", "fellows program"],
    title_exclude=["senior software engineer", "staff software engineer", "intern", "new grad"],
    keywords=[],
)
JUNIOR = guard_seniority(PROPOSED, "entry", ["software engineer"])


def test_excludes_senior_titles_however_they_are_written() -> None:
    for title in (
        "Staff+ Software Engineer, Scaling",
        "Staff + Sr. Software Engineer, Cloud Inference",
        "Sr. Software Engineer",
        "Software Engineer, Team Lead",
        "Engineering Manager",
    ):
        assert not title_matches(title, JUNIOR), title


def test_keeps_plain_and_early_career_titles_including_ones_that_say_senior() -> None:
    for title in (
        "Software Engineer, Research Data Platform",
        "New Grad Software Engineer",
        "Software Engineer Intern",
        "Senior Software Engineer, New Grad Program",
        "Anthropic Fellows Program, ML Systems",
        # A word boundary, not a substring: SRE is not "sr", Leadership is not "lead".
        "Software Engineer, SRE",
        "Software Engineer, Leadership Tools",
    ):
        assert title_matches(title, JUNIOR), title


def test_does_not_exclude_a_senior_word_the_requested_role_contains() -> None:
    apm = guard_seniority(
        DerivedCriteria(title_include=["product manager"], title_exclude=[], keywords=[]),
        "entry",
        ["associate product manager"],
    )
    assert title_matches("Associate Product Manager", apm)
    assert title_matches("Product Manager", apm)
    assert not title_matches("Senior Product Manager", apm)


def test_includes_every_early_career_title_when_the_search_names_companies() -> None:
    base = DerivedCriteria(title_include=["software engineer"], title_exclude=[], keywords=[])
    targeted = guard_seniority(base, "entry", ["software engineer"], companies=True)
    assert title_matches("Anthropic Fellows Program, ML Systems", targeted)
    assert title_matches("Associate, Rotational Program", targeted)
    # A word, not a substring: "Internal" and "International" are not "intern".
    assert not title_matches("Accounting, Revenue Internal Controls", targeted)
    assert not title_matches("Marketing Events Manager, International", targeted)

    broad = guard_seniority(base, "entry", ["software engineer"])
    assert not title_matches("Anthropic Fellows Program, ML Systems", broad)


def test_leaves_other_searches_alone() -> None:
    assert guard_seniority(PROPOSED, "senior", None) == PROPOSED
    assert guard_seniority(PROPOSED, None, None) == PROPOSED


def test_drops_a_leading_level_word() -> None:
    assert without_level("senior backend engineer") == "backend engineer"
    assert without_level("Sr. Staff software engineer") == "software engineer"
    assert without_level("entry-level data analyst") == "data analyst"
    assert without_level("lead generation specialist") == "lead generation specialist"
    assert without_level("software engineer, backend") == "software engineer, backend"


BAY_AREA = re.compile(BAY_AREA_PLACES.replace("\\y", "\\b"), re.IGNORECASE | re.ASCII)


def test_bay_area_places_in_multi_city_strings() -> None:
    for text in (
        "Seattle, San Francisco, New York",
        "SEA, SF, NY, Toronto",
        "San Francisco HQ",
        "US-Remote, Chicago, Seattle, San Francisco",
        "Palo Alto, CA",
    ):
        assert BAY_AREA.search(text), text
    for text in ("New York City Office", "Seattle, WA", "Dublin", "Sfax, Tunisia"):
        assert not BAY_AREA.search(text), text


def test_finishing_the_models_answer() -> None:
    answer = DerivedSearch.model_validate(
        {
            "brief": {
                "roles": None,
                "seniority": "entry",
                "locations": None,
                "remote": "any",
                "companies": ["Anthropic"],
                "focus": None,
            },
            "title_include": ["Senior Backend Engineer", "backend engineer", "c++ dev", ""],
            "title_exclude": ["staff", " "],
            "keywords": ["go", ""],
        }
    )
    saved = Preferences(target_roles=["Backend engineer"], seniority="senior", remote_pref="onsite")
    brief, found = finish_derived(answer, preferences=saved, request="entry roles", hints=None)
    # "any" from the model is not a stated preference: the saved "onsite" stands.
    assert brief.remote is None
    assert brief.seniority == "entry" and brief.companies == ["Anthropic"]
    assert found.title_include[:2] == ["backend engineer", "c\\+\\+ dev"]
    assert found.keywords == ["go"]
    assert found.title_keep is not None  # an entry-level search: guarded
    # Without a request, the model's brief is discarded; the chat agent's hints still apply.
    hints = brief.model_copy(update={"seniority": None, "companies": None, "remote": "any"})
    brief, _ = finish_derived(answer, preferences=saved, request=None, hints=hints)
    assert brief.seniority is None and brief.companies is None and brief.remote == "any"


# --- the SQL -------------------------------------------------------------------------------------

BAY = Preferences(
    target_roles=["Software engineer"],
    seniority="mid",
    locations=["San Francisco Bay Area"],
    remote_pref="onsite",
)
SWE = criteria(["software engineer"])
NOT_BAY = {
    "locations": ["Austin, TX"],
    "location_norm": {"raw": "Austin, TX", "city": "Austin", "region": "TX", "country": "US"},
    "remote": "onsite",
}


@pytest.mark.django_db
def test_one_match_per_role_best_copy_newest_first() -> None:
    user = make_user()
    acme = make_company(name="Acme")
    now = timezone.now()
    # The same role in two cities, and again from the aggregator: one survives, the board's.
    board = make_job(company=acme, title="Software Engineer", posted_at=now - timedelta(days=3))
    make_job(
        company=acme,
        title="Software  Engineer",
        posted_at=now - timedelta(days=4),
        locations=["Oakland, CA"],
    )
    aggregator = make_company(name="Acme Inc", slug="acme-aggregated")
    make_job(
        company=aggregator,
        source="jsearch",
        title="Software Engineer",
        company_name="Acme",
        posted_at=now - timedelta(days=1),
    )
    newer = make_job(company=acme, title="Software Engineer, Payments", posted_at=now)
    undated = make_job(company=acme, title="Software Engineer, Infra", posted_at=None)
    make_job(company=acme, title="Account Executive")

    found = prefilter_jobs(user.pk, BAY, SWE)
    assert found.job_ids == [newer.id, board.id, undated.id]
    assert found.selected == 3 and found.capped_at is None
    assert found.active_in_mirror == 6
    assert found.funnel() == {"activeInMirror": 6, "selected": 3, "cappedAt": None}

    capped = prefilter_jobs(user.pk, BAY, SWE, limit=2)
    assert capped.job_ids == [newer.id, board.id] and capped.capped_at == 2


@pytest.mark.django_db
def test_known_and_dismissed_roles() -> None:
    user = make_user()
    acme = make_company(name="Acme")
    dismissed = make_job(company=acme, title="Software Engineer, Search")
    make_match(user, job=dismissed, stage="archived")
    matched = make_job(company=acme, title="Software Engineer, Data")
    make_match(user, job=matched, stage="shortlisted")
    # Another posting of a role the user already has a match for: skipped.
    make_job(company=acme, title="Software Engineer, Data", locations=["San Jose, CA"])
    fresh = make_job(company=acme, title="Software Engineer, Billing")

    found = prefilter_jobs(user.pk, BAY, SWE)
    # The matched posting itself may come back (harmlessly); the dismissed one never does.
    assert sorted(found.job_ids) == sorted([matched.id, fresh.id])
    # Another user's matches change nothing for this one.
    assert len(prefilter_jobs(make_user().pk, BAY, SWE).job_ids) == 3


@pytest.mark.django_db
def test_the_45_day_window_and_the_company_filter() -> None:
    user = make_user()
    acme = make_company(name="Acme")
    other = make_company(name="Other Co")
    old = make_job(
        company=acme, title="Software Engineer", posted_at=timezone.now() - timedelta(days=60)
    )
    recent = make_job(company=other, title="Software Engineer, Platform")
    unknown = make_company(name="Anthropic PBC", slug="anthropic-aggregated")
    by_name = make_job(company=unknown, source="jsearch", title="Software Engineer, Research")
    make_job(company=acme, title="Software Engineer", is_active=False)

    assert sorted(prefilter_jobs(user.pk, BAY, SWE).job_ids) == sorted([recent.id, by_name.id])
    # Naming companies drops the window: what those boards list is open now.
    named = prefilter_jobs(user.pk, BAY, SWE, CompanyFilter(ids=[acme.id], names=["Anthropic"]))
    assert sorted(named.job_ids) == sorted([old.id, by_name.id])
    # A name with no letters adds nothing, so the window applies again.
    nothing = prefilter_jobs(user.pk, BAY, SWE, CompanyFilter(names=[" - "]))
    assert sorted(nothing.job_ids) == sorted([recent.id, by_name.id])


@pytest.mark.django_db
def test_location_and_remote() -> None:
    user = make_user()
    acme = make_company(name="Acme")
    sf = make_job(company=acme, title="Software Engineer, A")
    # The normalizer leaves multi-city strings without a metro; the raw text still matches.
    multi = make_job(
        company=acme,
        title="Software Engineer, B",
        locations=["Seattle, San Francisco, New York"],
        location_norm={"raw": "Seattle, San Francisco, New York", "metro": None},
    )
    remote = make_job(company=acme, title="Software Engineer, C", **{**NOT_BAY, "remote": "remote"})
    hybrid = make_job(company=acme, title="Software Engineer, D", **{**NOT_BAY, "remote": "hybrid"})
    austin = make_job(company=acme, title="Software Engineer, E", **NOT_BAY)

    assert sorted(prefilter_jobs(user.pk, BAY, SWE).job_ids) == sorted([sf.id, multi.id])
    wants_remote = BAY.model_copy(update={"remote_pref": "any"})
    assert sorted(prefilter_jobs(user.pk, wants_remote, SWE).job_ids) == sorted(
        [sf.id, multi.id, remote.id]
    )
    wants_hybrid = BAY.model_copy(update={"remote_pref": "hybrid"})
    assert hybrid.id in prefilter_jobs(user.pk, wants_hybrid, SWE).job_ids
    # A city outside the gazetteer matches its raw text.
    texas = Preferences(locations=["Austin, TX"], remote_pref="onsite")
    assert sorted(prefilter_jobs(user.pk, texas, SWE).job_ids) == sorted(
        [remote.id, hybrid.id, austin.id]
    )
    # No location, no remote preference: no location clause at all.
    assert len(prefilter_jobs(user.pk, Preferences(), SWE).job_ids) == 5


@pytest.mark.django_db
def test_title_terms_in_sql_agree_with_title_matches() -> None:
    user = make_user()
    acme = make_company(name="Acme")
    titles = [
        "Staff+ Software Engineer, Scaling",
        "Sr. Software Engineer",
        "Software Engineer, SRE",
        "Senior Software Engineer, New Grad Program",
        "Software Engineer Intern",
        "Anthropic Fellows Program, ML Systems",
        "Account Executive",
    ]
    jobs = {make_job(company=acme, title=title).id: title for title in titles}
    found = prefilter_jobs(user.pk, BAY, JUNIOR)
    assert sorted(jobs[job_id] for job_id in found.job_ids) == sorted(
        title for title in titles if title_matches(title, JUNIOR)
    )


@pytest.mark.django_db
def test_sets_a_statement_timeout_first() -> None:
    user = make_user()
    with CaptureQueriesContext(connection) as queries:
        prefilter_jobs(user.pk, BAY, SWE)
    statements = [query["sql"] for query in queries.captured_queries]
    first = next(i for i, sql in enumerate(statements) if "statement_timeout" in sql)
    assert statements[first] == "SET LOCAL statement_timeout = '20s'"
    assert "DISTINCT ON" in statements[first + 1]
