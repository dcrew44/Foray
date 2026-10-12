"""`resolve_or_create_match`: the one place a match id or a job id becomes a match (design §5.3;
`resolveMatch` + `ensureMatch`). The tailor start and the chat tools both go through it."""

from __future__ import annotations

import uuid

import pytest

from apps.matching.models import JobMatch
from apps.matching.queries import resolve_or_create_match
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
