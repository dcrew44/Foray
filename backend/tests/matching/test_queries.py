"""`resolve_or_create_match`: the one place a match id or a job id becomes a match (design §5.3;
`resolveMatch` + `ensureMatch`). The tailor start and the chat tools both go through it. Also
the queries a watch adds (#5): the matches one call created, and Home's "New this week"."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

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
    stale = make_match(user, fast_score=90, deep_score=90)
    JobMatch.objects.for_user(user.pk).filter(id=stale.id).update(
        created_at=timezone.now() - timedelta(days=8)
    )
    rows = recent_recommended(user.pk)
    assert [row.match_id for row in rows] == [strong.id, apply.id]
    assert [row.recommendation for row in rows] == ["strong", "apply"]
    assert recent_recommended(make_user().pk) == []
