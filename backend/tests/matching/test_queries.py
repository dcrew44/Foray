"""`resolve_or_create_match`: the one place a match id or a job id becomes a match (design §5.3;
`resolveMatch` + `ensureMatch`). The tailor start and the chat tools both go through it. Also
the queries a watch adds (#5): the matches one call created, and Home's "New this week"."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from django.db import connection
from django.utils import timezone

from apps.matching import queries
from apps.matching.models import JobMatch
from apps.matching.queries import (
    insert_new_matches,
    job_ids_for_matches,
    recent_recommended,
    resolve_or_create_match,
    upsert_matches,
)
from tests.factories import make_job, make_match, make_user


@pytest.mark.django_db
def test_a_match_id_or_a_job_id_finds_the_users_match() -> None:
    user = make_user()
    match = make_match(user, stage="shortlisted")
    assert resolve_or_create_match(user.pk, str(match.id)) == match
    assert resolve_or_create_match(user.pk, match.job_id) == match
    # Found again by its job, the match keeps its stage: nothing is created or moved.
    assert JobMatch.objects.for_user(user.pk).get().stage == "shortlisted"


@pytest.mark.django_db
def test_a_posting_without_a_match_gets_a_found_one_once() -> None:
    user = make_user()
    job = make_job()
    created = resolve_or_create_match(user.pk, job.id)
    assert created is not None and (created.job_id, created.stage) == (job.id, "found")
    assert resolve_or_create_match(user.pk, job.id) == created
    assert JobMatch.objects.for_user(user.pk).count() == 1


@pytest.mark.django_db
def test_anything_else_resolves_to_nothing() -> None:
    user, other = make_user(), make_user()
    others_match = make_match(other)
    for wanted in (str(others_match.id), str(uuid.uuid4()), "greenhouse:nobody:1", "not an id"):
        assert resolve_or_create_match(user.pk, wanted) is None, wanted
    assert not JobMatch.objects.for_user(user.pk).exists()


@pytest.mark.django_db
def test_insert_new_matches_returns_only_what_it_created_in_order() -> None:
    user = make_user()
    old, first, second = make_job(), make_job(), make_job()
    make_match(user, job=old, stage="shortlisted")
    created = insert_new_matches(user.pk, [first.id, old.id, second.id, first.id])
    assert created == [first.id, second.id]
    assert JobMatch.objects.for_user(user.pk).get(job_id=old.id).stage == "shortlisted"
    assert insert_new_matches(user.pk, [first.id, second.id]) == []
    assert upsert_matches(user.pk, [first.id, make_job().id]) == 1


@pytest.mark.django_db
def test_job_ids_for_matches_reads_only_the_users_own() -> None:
    user, other = make_user(), make_user()
    mine = [make_match(user), make_match(user)]
    theirs = make_match(other)
    wanted = [str(mine[1].id), str(theirs.id), "not an id", str(mine[0].id)]
    assert job_ids_for_matches(user.pk, wanted) == [mine[1].job_id, mine[0].job_id]
    assert job_ids_for_matches(user.pk, None) == []


@pytest.mark.django_db
def test_new_this_week_is_recent_reviewed_and_still_waiting() -> None:
    user = make_user()
    strong = make_match(user, fast_score=88, deep_score=92)
    apply = make_match(user, stage="shortlisted", fast_score=80, deep_score=76)
    make_match(user, fast_score=60, deep_score=60)  # maybe: not announced
    make_match(user, fast_score=95)  # never reviewed
    make_match(user, stage="applied", fast_score=90, deep_score=90)  # already acted on
    # Matched today, but the posting has been in the mirror for a month: not new this week.
    old_posting = make_job(first_seen_at=timezone.now() - timedelta(days=30))
    make_match(user, job=old_posting, fast_score=90, deep_score=90)
    rows = recent_recommended(user.pk)
    assert [row.match_id for row in rows] == [strong.id, apply.id]
    assert [row.recommendation for row in rows] == ["strong", "apply"]
    assert recent_recommended(make_user().pk) == []


# --- when a match first reached Applied and Interviewing (#94) -----------------------------------


def _times(match: Any) -> tuple[Any, Any]:
    match.refresh_from_db()
    return match.applied_at, match.interviewing_at


@pytest.mark.django_db
def test_a_match_keeps_when_it_first_reached_applied_and_interviewing() -> None:
    user = make_user()
    match = make_match(user, stage="found")
    assert _times(match) == (None, None)

    assert queries.set_match_stage(user.pk, match.id, "shortlisted")
    assert _times(match) == (None, None)
    assert queries.set_match_stage(user.pk, match.id, "applied")
    applied_at, interviewing_at = _times(match)
    assert applied_at == match.stage_changed_at and interviewing_at is None

    # Moving on keeps the first time; the stage it is already in changes nothing.
    assert queries.set_match_stage(user.pk, match.id, "applied")
    assert queries.set_match_stage(user.pk, match.id, "interviewing")
    kept, interviewing_at = _times(match)
    assert kept == applied_at and interviewing_at == match.stage_changed_at
    for closed in ("offer", "rejected", "archived"):
        assert queries.set_match_stage(user.pk, match.id, closed)
        assert _times(match) == (applied_at, interviewing_at), closed


@pytest.mark.django_db
def test_moving_a_match_back_clears_the_steps_it_is_now_before() -> None:
    user = make_user()
    match = make_match(user, stage="found")
    queries.set_match_stage(user.pk, match.id, "interviewing")  # straight past Applied
    applied_at, interviewing_at = _times(match)
    assert applied_at is not None and applied_at == interviewing_at

    queries.set_match_stage(user.pk, match.id, "applied")
    assert _times(match) == (applied_at, None)
    queries.set_match_stage(user.pk, match.id, "shortlisted")
    assert _times(match) == (None, None)


@pytest.mark.django_db
def test_an_undo_restores_the_time_the_match_had_reached_the_stage() -> None:
    """`entered_at` is when the match had entered the stage before the move being undone."""
    user = make_user()
    match = make_match(user, stage="found")
    before = timezone.now() - timedelta(days=2)
    queries.set_match_stage(user.pk, match.id, "applied", entered_at=before)
    assert _times(match) == (before, None)
    # Never later than now, as for `stage_changed_at`.
    late = make_match(user, stage="found")
    queries.set_match_stage(
        user.pk, late.id, "interviewing", entered_at=timezone.now() + timedelta(days=1)
    )
    applied_at, interviewing_at = _times(late)
    assert applied_at == interviewing_at and applied_at <= timezone.now()


@pytest.mark.django_db
def test_a_move_of_several_matches_keeps_the_times_too() -> None:
    user = make_user()
    first, second = make_match(user, stage="shortlisted"), make_match(user, stage="found")
    moved = queries.move_matches_from_stage(
        user.pk, [first.id, second.id], "shortlisted", "applied"
    )
    assert moved == [str(first.id)]
    assert _times(first)[0] is not None and _times(second) == (None, None)


@pytest.mark.django_db
def test_the_migration_fills_in_what_the_stage_a_match_is_in_proves() -> None:
    from importlib import import_module

    backfill = import_module("apps.matching.migrations.0005_stage_times").BACKFILL
    user = make_user()
    by_stage = {
        stage: make_match(user, stage=stage)
        for stage in ("found", "applied", "interviewing", "offer", "rejected", "archived")
    }
    with connection.cursor() as cursor:
        cursor.execute(backfill)
    found = {stage: _times(match) for stage, match in by_stage.items()}
    assert found["applied"] == (by_stage["applied"].stage_changed_at, None)
    for stage in ("interviewing", "offer"):
        assert found[stage] == (None, by_stage[stage].stage_changed_at), stage
    for stage in ("found", "rejected", "archived"):
        assert found[stage] == (None, None), stage
