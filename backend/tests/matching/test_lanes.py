"""The apply lane of a match (#39; `apps/matching/lanes.py`): the deep review decides, the user's
override wins, and a re-review moves the first and never the second. The rule is one function;
its SQL form is held to it here over every shape a stored evaluation can take."""

from __future__ import annotations

import itertools
from typing import Any

import pytest
from django.db import IntegrityError, transaction

from apps.matching.evaluate import start_match_evaluation
from apps.matching.lanes import (
    LANES,
    REVIEW_LANES,
    Lane,
    as_lane,
    effective_lane,
    lane_expression,
    lane_of,
    review_lane,
)
from apps.matching.models import JobMatch
from apps.matching.queries import get_match_rows, list_matches, save_deep_eval, update_match
from tests.factories import make_job, make_match, make_user, sample_deep_eval
from tests.matching.conftest import FakeLLM
from tests.queue import run_deferred


def review(recommendation: Any) -> dict[str, Any]:
    return {**sample_deep_eval(60), "recommendation": recommendation}


# What a `deep_eval` column can hold: a review of each verdict, then what an import or an older
# version may have left there. None is a match with no review at all.
STORED: list[Any] = [
    None,
    review("strong"),
    review("apply"),
    review("maybe"),
    review("skip"),
    review("Strong"),
    review("hire"),
    review(""),
    review(None),
    review(1),
    review(True),
    review(["strong"]),
    review({"is": "strong"}),
    {"summary": "No verdict kept."},
    {},
    ["strong"],
    "strong",
    7,
]
OVERRIDES: list[str | None] = [None, *LANES]


# --- the rule ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("recommendation", "lane"),
    [("strong", "coached"), ("apply", "automated"), ("maybe", "none"), ("skip", "none")],
)
def test_the_review_decides(recommendation: str, lane: str) -> None:
    assert review_lane(review(recommendation)) == lane
    assert effective_lane(None, review(recommendation)) == lane


def test_a_match_without_a_review_is_in_no_lane() -> None:
    """A fast score alone puts a match nowhere, whatever it is."""
    assert effective_lane(None, None) == Lane.NONE
    assert review_lane(None) == Lane.NONE


@pytest.mark.parametrize("stored", STORED[5:])
def test_an_unreadable_review_is_read_as_no_lane(stored: Any) -> None:
    assert review_lane(stored) == Lane.NONE
    assert effective_lane(None, stored) == Lane.NONE


@pytest.mark.parametrize("override", LANES)
@pytest.mark.parametrize("stored", STORED)
def test_an_override_wins_whatever_the_review_says(override: str, stored: Any) -> None:
    assert effective_lane(override, stored) == override


def test_only_a_lane_is_an_override() -> None:
    assert [as_lane(value) for value in LANES] == list(Lane)
    for value in (None, "", "vip", "Coached", 1, ["coached"]):
        assert as_lane(value) is None
        assert effective_lane(value, review("apply")) == Lane.AUTOMATED


def test_the_lanes_are_the_columns_choices() -> None:
    assert LANES == ("coached", "automated", "none")
    assert set(REVIEW_LANES.values()) == {Lane.COACHED, Lane.AUTOMATED}


# --- rows and the filter -------------------------------------------------------------------------


def _matrix(user: Any) -> dict[Any, tuple[str | None, Any]]:
    """One match for every override with every stored evaluation, by id."""
    made = {}
    for override, stored in itertools.product(OVERRIDES, STORED):
        match = make_match(
            user,
            fast_score=60,
            deep_score=None if stored is None else 60,
            deep_eval=stored,
            lane_override=override,
        )
        made[match.id] = (override, stored)
    return made


@pytest.mark.django_db
def test_every_row_says_the_lane_the_rule_gives() -> None:
    user = make_user()
    made = _matrix(user)
    rows = list_matches(user.pk, limit=len(made))
    assert len(rows) == len(made)
    for row in rows:
        override, stored = made[row.match_id]
        assert row.lane == effective_lane(override, stored), (override, stored)
    assert {row.lane for row in rows} == set(LANES)
    by_id = get_match_rows(user.pk, list(made))
    assert [row.lane for row in by_id] == [row.lane for row in rows]
    # And so does a loaded match, for code that acts on a lane.
    loaded = {match.id: lane_of(match) for match in JobMatch.objects.for_user(user.pk)}
    assert loaded == {row.match_id: row.lane for row in rows}


@pytest.mark.django_db
def test_the_sql_filter_agrees_with_the_rule_on_every_stored_shape() -> None:
    """`list_matches(lane=…)` narrows in SQL (`lane_expression`), before its limit; a row says
    its lane in Python (`effective_lane`). Each lane's filter finds exactly the rows that say
    that lane, and every match is in exactly one."""
    user = make_user()
    made = _matrix(user)
    expected = {match_id: effective_lane(*stored).value for match_id, stored in made.items()}
    annotated = dict(
        JobMatch.objects.for_user(user.pk)
        .annotate(lane=lane_expression())
        .values_list("id", "lane")
    )
    assert annotated == expected
    found: dict[Any, str] = {}
    for lane in LANES:
        rows = list_matches(user.pk, lane=lane, limit=len(made))
        assert rows and all(row.lane == lane for row in rows), lane
        found.update({row.match_id: lane for row in rows})
    assert found == expected
    assert list_matches(user.pk, lane="vip", limit=len(made)) == []


@pytest.mark.django_db
def test_the_lane_filter_costs_no_query_and_is_only_the_users(
    django_assert_num_queries: Any,
) -> None:
    user, other = make_user(), make_user()
    coached = make_match(user, fast_score=80, deep_score=90)
    make_match(user, fast_score=80, deep_score=72)
    make_match(other, fast_score=80, deep_score=95)
    with django_assert_num_queries(1):
        rows = list_matches(user.pk, lane="coached")
    assert [row.match_id for row in rows] == [coached.id]


@pytest.mark.django_db
def test_the_lane_filter_narrows_with_the_other_filters() -> None:
    user = make_user()
    found = make_match(user, fast_score=80, deep_score=75)
    make_match(user, fast_score=80, deep_score=74, stage="applied")
    make_match(user, fast_score=80, deep_score=90)
    rows = list_matches(user.pk, lane="automated", stages=["found"])
    assert [row.match_id for row in rows] == [found.id]


# --- the override --------------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_user_moves_a_match_and_hands_it_back() -> None:
    user = make_user()
    match = make_match(user, fast_score=80, deep_score=90)  # strong: coached

    def lane() -> str:
        return get_match_rows(user.pk, [match.id])[0].lane

    assert lane() == "coached"
    for chosen in ("automated", "none", "coached"):
        assert update_match(user.pk, match.id, lane_override=chosen) is True
        assert lane() == chosen
    assert update_match(user.pk, match.id, lane_override=None) is True
    assert lane() == "coached"
    # A change to anything else leaves the lane where it is.
    update_match(user.pk, match.id, lane_override="automated")
    update_match(user.pk, match.id, notes="Call Dana.")
    assert lane() == "automated"


@pytest.mark.django_db
def test_a_match_that_is_not_the_users_is_not_moved() -> None:
    user, other = make_user(), make_user()
    theirs = make_match(other, fast_score=80, deep_score=90)
    assert update_match(user.pk, theirs.id, lane_override="none") is False
    assert JobMatch.objects.for_user(other.pk).get(id=theirs.id).lane_override is None


@pytest.mark.django_db
def test_the_column_holds_only_a_lane() -> None:
    user = make_user()
    match = make_match(user)
    with (
        pytest.raises(IntegrityError, match="job_matches_lane_override_valid"),
        transaction.atomic(),
    ):
        update_match(user.pk, match.id, lane_override="vip")
    assert JobMatch.objects.for_user(user.pk).get(id=match.id).lane_override is None


@pytest.mark.django_db
def test_storing_a_review_never_writes_the_override() -> None:
    """`save_deep_eval` is where every review lands: the lane a review gives follows the newest
    one, and the lane the user chose stays put, through a skip that archives the match too."""
    user = make_user()
    followed = make_match(user, fast_score=80, deep_score=90)
    pinned = make_match(user, fast_score=80, deep_score=90, lane_override="coached")
    out = make_match(user, fast_score=80, deep_score=90, lane_override="none")

    def lanes() -> list[tuple[str, str | None]]:
        rows = {
            row.match_id: row.lane
            for row in get_match_rows(user.pk, [followed.id, pinned.id, out.id])
        }
        stored = dict(JobMatch.objects.for_user(user.pk).values_list("id", "lane_override"))
        return [(rows[match.id], stored[match.id]) for match in (followed, pinned, out)]

    assert lanes() == [("coached", None), ("coached", "coached"), ("none", "none")]
    for score, derived in ((72, "automated"), (55, "none"), (20, "none"), (91, "coached")):
        for match in (followed, pinned, out):
            assert save_deep_eval(user.pk, match.id, sample_deep_eval(score)) is True
        assert lanes() == [(derived, None), ("coached", "coached"), ("none", "none")], score
    # The skip archived all three (they were still in found); their lanes did not move with it.
    assert set(JobMatch.objects.for_user(user.pk).values_list("stage", flat=True)) == {"archived"}


@pytest.mark.django_db(transaction=True)
def test_an_override_survives_review_again(llm: FakeLLM) -> None:
    """The whole path behind "Review again": the run, the worker, the model's answer stored. A
    match that follows the review changes lane with it; one the user moved does not, until they
    hand it back."""
    user = make_user(with_resume=True)
    followed = make_match(user, job=make_job(title="Backend Engineer"), fast_score=80)
    moved = make_match(user, job=make_job(title="Platform Engineer"), fast_score=80)
    ids = [followed.id, moved.id]

    def reviewed(score: int) -> dict[Any, tuple[str | None, str]]:
        llm.deep = {"Backend Engineer": score, "Platform Engineer": score}
        start_match_evaluation(user.pk, ids)
        run_deferred()
        return {
            row.match_id: (row.recommendation, row.lane) for row in get_match_rows(user.pk, ids)
        }

    assert set(reviewed(90).values()) == {("strong", "coached")}
    assert update_match(user.pk, moved.id, lane_override="automated") is True
    assert reviewed(60) == {followed.id: ("maybe", "none"), moved.id: ("maybe", "automated")}
    assert reviewed(88) == {followed.id: ("strong", "coached"), moved.id: ("strong", "automated")}
    assert len(llm.of("deep")) == 6  # three reviews of each: every one reached the model
    assert JobMatch.objects.for_user(user.pk).get(id=moved.id).lane_override == "automated"
    # Handed back, it follows the newest review like the other.
    assert update_match(user.pk, moved.id, lane_override=None) is True
    assert {row.lane for row in get_match_rows(user.pk, ids)} == {"coached"}
