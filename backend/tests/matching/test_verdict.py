"""What a match leads with (#38): one line of why and a blocker, read from the stored
evaluations (`apps/matching/verdict.py`), and the same two on every `MatchRow`."""

from __future__ import annotations

from typing import Any

import pytest

from apps.matching.queries import get_match_rows, list_matches
from apps.matching.verdict import blocker_for, first_sentence, reason_for
from tests.factories import make_match, make_user, sample_deep_eval, sample_fast_eval


def deep(score: int = 60, **changes: Any) -> dict[str, Any]:
    return {**sample_deep_eval(score), **changes}


def fast(**changes: Any) -> dict[str, Any]:
    return {**sample_fast_eval(60), **changes}


CLEARANCE = {"gap": "Active TS/SCI clearance", "severity": "disqualifying", "note": "Required."}
DEGREE = {"gap": "A PhD in statistics", "severity": "disqualifying", "note": "Stated as a must."}
RUST = {"gap": "Rust", "severity": "learnable", "note": "Listed as a plus."}


@pytest.mark.parametrize(
    ("text", "first"),
    [
        ("A close fit. The gaps are small.", "A close fit."),
        ("One sentence with no full stop", "One sentence with no full stop"),
        (
            "Backend work close to yours; Go is the one gap. Apply.",
            "Backend work close to yours; Go is the one gap.",
        ),
        ("Is it a fit? Mostly.", "Is it a fit?"),
        ('They call it "a stretch." You have done harder.', 'They call it "a stretch."'),
        ("Four years of Python.  \n\n 3 of them in payments.", "Four years of Python."),
        # A full stop inside a name, a number or an abbreviation ends nothing.
        ("Node.js and 3.5 years of it. Strong.", "Node.js and 3.5 years of it."),
        (
            "Covers the stack, e.g. Postgres and Kafka. Level fits.",
            "Covers the stack, e.g. Postgres and Kafka.",
        ),
        ("Open to U.S. Citizens only. You are one.", "Open to U.S. Citizens only."),
        ("A Sr. Engineer role vs. Staff. Close.", "A Sr. Engineer role vs. Staff."),
        ("  Split\nover lines.\nSecond.", "Split over lines."),
        ("", ""),
    ],
)
def test_first_sentence(text: str, first: str) -> None:
    assert first_sentence(text) == first


def test_the_reason_is_the_reviews_first_sentence_else_the_first_fast_reason() -> None:
    summary = "Direct payments experience. The level is a stretch. Worth a look."
    assert reason_for(deep(summary=summary), fast()) == "Direct payments experience."
    assert reason_for(None, fast()) == "Backend work in Python"
    # A review that says nothing leaves the line to triage, as the headline always has.
    assert reason_for(deep(summary="  "), fast()) == "Backend work in Python"
    assert reason_for(None, fast(fit_reasons=["", " Close\nto home "])) == "Close to home"
    assert reason_for(None, fast(fit_reasons=[])) is None
    assert reason_for(None, None) is None


def test_the_blocker_is_the_first_disqualifying_gap_else_the_first_hard_miss() -> None:
    assert blocker_for(deep(gaps=[RUST, CLEARANCE, DEGREE]), None) == "Active TS/SCI clearance"
    assert blocker_for(deep(gaps=[RUST]), None) is None
    misses = fast(hard_requirement_misses=["Requires 8+ years", "On-site in Austin"])
    assert blocker_for(None, misses) == "Requires 8+ years"
    assert blocker_for(None, fast()) is None
    assert blocker_for(None, None) is None
    # A review overrules triage: a miss only the cheap pass saw is not shown beside its verdict.
    assert blocker_for(deep(gaps=[RUST]), misses) is None
    assert blocker_for(deep(gaps=[CLEARANCE]), misses) == "Active TS/SCI clearance"


@pytest.mark.parametrize(
    "stored",
    [
        {"bogus": True},
        {"summary": 7, "gaps": "none", "fit_reasons": {"a": 1}, "hard_requirement_misses": 3},
        {"gaps": [None, "Rust", {"severity": "disqualifying"}, {"gap": 4}]},
        {"fit_reasons": [None, 3], "hard_requirement_misses": [None]},
        [],
        "text",
    ],
)
def test_anything_stored_reads_without_raising(stored: Any) -> None:
    for deep_eval, fast_eval in ((stored, stored), (None, stored), (stored, None)):
        assert reason_for(deep_eval, fast_eval) is None
        assert blocker_for(deep_eval, fast_eval) is None


@pytest.mark.django_db
def test_rows_lead_with_the_reason_and_any_blocker() -> None:
    user = make_user()
    summary = "Direct payments experience. The level is a stretch."
    reviewed = make_match(user, fast_score=80, deep_score=90, deep_eval=deep(90, summary=summary))
    blocked = make_match(
        user,
        fast_score=75,
        deep_score=40,
        deep_eval=deep(40, summary="Needs a clearance you lack.", gaps=[RUST, CLEARANCE]),
        # What triage missed does not stand beside the review's own blocker.
        fast_eval=fast(hard_requirement_misses=["Requires 8+ years"]),
    )
    fast_only = make_match(user, fast_score=70)
    fast_blocked = make_match(
        user, fast_score=35, fast_eval=fast(hard_requirement_misses=["Requires 8+ years"])
    )
    unscored = make_match(user)

    rows = {row.match_id: row for row in list_matches(user.pk)}
    assert len(rows) == 5

    def lead(match: Any) -> tuple[str | None, str | None, str | None]:
        row = rows[match.id]
        return row.recommendation, row.reason, row.blocker

    assert lead(reviewed) == ("strong", "Direct payments experience.", None)
    assert rows[reviewed.id].headline == summary  # the whole summary, as chat quotes it
    assert lead(blocked) == ("skip", "Needs a clearance you lack.", "Active TS/SCI clearance")
    assert lead(fast_only) == (None, "Backend work in Python", None)
    assert lead(fast_blocked) == (None, "Backend work in Python", "Requires 8+ years")
    assert lead(unscored) == (None, None, None)

    assert [(row.reason, row.blocker) for row in get_match_rows(user.pk, [blocked.id])] == [
        ("Needs a clearance you lack.", "Active TS/SCI clearance")
    ]


@pytest.mark.django_db
def test_rows_with_a_lead_cost_one_query(django_assert_num_queries: Any) -> None:
    """The reason and the blocker come from the columns the row query already reads."""
    user = make_user()
    for score in (90, 60, 30):
        make_match(
            user, fast_score=score, deep_score=score, deep_eval=deep(score, gaps=[CLEARANCE])
        )
        make_match(user, fast_score=score, fast_eval=fast(hard_requirement_misses=["A miss"]))
    with django_assert_num_queries(1):
        rows = list_matches(user.pk)
    assert len(rows) == 6 and all(row.reason and row.blocker for row in rows)


# --- the rubric's bands (#87) --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("score", "band"),
    [(100, "strong"), (85, "strong"), (84, "apply"), (70, "apply"), (69, "maybe"), (50, "maybe"),
     (49, "skip"), (0, "skip")],
)  # fmt: skip
def test_a_score_is_called_by_the_rubrics_band(score: int, band: str) -> None:
    from apps.matching.verdict import band_of

    assert band_of(score) == band
    # The factory's reviews say what a real review at that score would.
    assert sample_deep_eval(score)["recommendation"] == band


@pytest.mark.django_db
def test_nothing_calls_a_score_strong_below_the_rubrics_line() -> None:
    """A match reviewed at 84 is "Worth applying" on its row, so it is not one of Home's strong
    matches or a run summary's either: every count reads the same band."""
    from datetime import timedelta

    from django.utils import timezone

    from apps.matching import queries
    from apps.pages import build

    user = make_user()
    at_the_line = make_match(user, deep_score=85)
    just_under = make_match(user, deep_score=84)
    plausible = make_match(user, fast_score=70)
    low = make_match(user, fast_score=69)
    job_ids = [m.job_id for m in (at_the_line, just_under, plausible, low)]

    assert queries.score_bands_for_jobs(user.pk, job_ids) == (1, 2)
    since = timezone.now() - timedelta(hours=1)
    assert queries.count_new_since(user.pk, since) == {"total": 4, "strong": 1}
    assert build.STRONG_SCORE == 85
    rows = {row.match_id: row for row in get_match_rows(user.pk, [at_the_line.id, just_under.id])}
    assert rows[at_the_line.id].recommendation == "strong"
    assert rows[just_under.id].recommendation == "apply"
