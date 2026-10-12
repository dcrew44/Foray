"""Provenance stamping and base versions (design §11.3, R14; port of `resume-edits.test.ts`,
8 cases, plus the version tests)."""

from __future__ import annotations

import threading
from typing import Any

import pytest
from django.db import connections

from apps.resumes import edits
from apps.resumes.edits import create_base_version, save_user_edit, stamp_user_edits
from apps.resumes.models import Resume as ResumeRow
from apps.resumes.queries import latest_base_resume
from apps.resumes.schema import Resume
from tests.factories import make_user, sample_resume_data


def resume(work: list[dict[str, Any]]) -> Resume:
    return Resume.model_validate(
        {
            "basics": {"name": "Test Person", "links": []},
            "work": [
                {
                    "id": "w1",
                    "source": "resume",
                    "company": "Acme",
                    "position": "Engineer",
                    "highlights": [],
                    **item,
                }
                for item in work
            ],
        }
    )


def copy(value: Resume) -> Resume:
    return value.model_copy(deep=True)


# --- stamp_user_edits ----------------------------------------------------------------------------


def test_untouched_items_keep_their_provenance():
    before = resume([{"id": "w1", "source": "resume"}])
    assert stamp_user_edits(copy(before), before).work[0].source == "resume"


def test_an_edited_item_becomes_the_users():
    before = resume([{"id": "w1", "source": "resume"}])
    after = copy(before)
    after.work[0].position = "Senior Engineer"
    assert stamp_user_edits(after, before).work[0].source == "user"


def test_a_new_item_becomes_the_users():
    before = resume([{"id": "w1", "source": "resume"}])
    after = resume([{"id": "w1", "source": "resume"}, {"id": "w2", "company": "New Co"}])
    stamped = {item.id: item.source for item in stamp_user_edits(after, before).work}
    assert stamped == {"w1": "resume", "w2": "user"}


def test_a_changed_bullet_counts_not_just_a_changed_field():
    before = resume([{"id": "w1", "source": "resume", "highlights": ["Did a thing"]}])
    after = copy(before)
    after.work[0].highlights = ["Did a thing", "Did another thing"]
    assert stamp_user_edits(after, before).work[0].source == "user"


def test_reordering_does_not_launder_provenance():
    before = resume(
        [
            {"id": "w1", "source": "resume"},
            {"id": "w2", "source": "linkedin_pdf", "company": "Other"},
        ]
    )
    after = copy(before)
    after.work.reverse()
    stamped = {item.id: item.source for item in stamp_user_edits(after, before).work}
    assert stamped == {"w1": "resume", "w2": "linkedin_pdf"}


def test_key_order_does_not_matter():
    """jsonb does not keep key order: the stored side reads back shuffled."""
    before = resume([{"id": "w1", "source": "resume", "highlights": ["a"]}])
    stored = before.work[0].dump()
    shuffled = dict(reversed(list(stored.items())))
    assert list(shuffled) != list(stored)
    assert edits.canonical_json(shuffled) == edits.canonical_json(stored)

    previous = Resume.model_validate({**before.dump(), "work": [shuffled]})
    assert stamp_user_edits(copy(before), previous).work[0].source == "resume"


def test_none_and_absent_are_the_same():
    before = resume([{"id": "w1", "source": "resume"}])
    after = Resume.model_validate(
        {**before.dump(), "work": [{**before.work[0].dump(), "location": None}]}
    )
    stored = {**before.work[0].dump()}
    stored.pop("location")
    assert edits.canonical_json(stored) == edits.canonical_json(after.work[0].dump())
    assert stamp_user_edits(after, before).work[0].source == "resume"


def test_a_first_save_keeps_provenance_as_extracted():
    after = resume([{"id": "w1", "source": "resume"}])
    assert stamp_user_edits(after, None).work[0].source == "resume"


# --- base versions -------------------------------------------------------------------------------


@pytest.mark.django_db
def test_save_user_edit_is_a_no_op_when_nothing_changed():
    user = make_user(with_resume=True)
    current = latest_base_resume(user.pk)
    assert current is not None

    unchanged = save_user_edit(user.pk, current.resume.model_copy(deep=True))
    assert unchanged.changed is False
    assert unchanged.row is not None and unchanged.row.version == 1
    assert ResumeRow.objects.for_user(user.pk).count() == 1

    edited = current.resume.model_copy(deep=True)
    edited.work[0].highlights.append("Wrote the on-call runbook.")
    saved = save_user_edit(user.pk, edited)
    assert saved.changed is True
    assert saved.row is not None and saved.row.version == 2
    newest = latest_base_resume(user.pk)
    assert newest is not None and newest.version == 2
    assert newest.resume.work[0].source == "user"
    assert all(item.source == "resume" for item in newest.resume.work[1:])


@pytest.mark.django_db
def test_create_base_version_numbers_from_the_newest():
    user = make_user(with_resume=True)
    row = create_base_version(user.pk, Resume.model_validate(sample_resume_data()))
    assert row.version == 2
    assert row.kind == "base" and row.match_id is None


@pytest.mark.django_db(transaction=True)
def test_base_versions_unique_under_race(monkeypatch: pytest.MonkeyPatch):
    """Two writers read the same newest version before either inserts: the constraint refuses
    the second insert, which retries once and takes the next number (R14)."""
    user = make_user()
    data = Resume.model_validate(sample_resume_data())
    barrier = threading.Barrier(2, timeout=10)
    waited = threading.local()
    manager = ResumeRow.objects
    real_create_for = type(manager).create_for

    def create_for(self: Any, user_id: str, **fields: Any) -> ResumeRow:
        if not getattr(waited, "done", False):
            waited.done = True
            barrier.wait()  # both have read max(version) = 0 by now
        return real_create_for(self, user_id, **fields)

    monkeypatch.setattr(type(manager), "create_for", create_for)
    collisions: list[bool] = []
    real_is_collision = edits._is_version_collision

    def is_collision(error: Any) -> bool:
        collisions.append(real_is_collision(error))
        return collisions[-1]

    monkeypatch.setattr(edits, "_is_version_collision", is_collision)
    results: list[int] = []
    errors: list[BaseException] = []

    def write() -> None:
        try:
            results.append(create_base_version(user.pk, data).version)
        except BaseException as error:  # noqa: BLE001 (reported below)
            errors.append(error)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=write) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert collisions == [True]  # the second writer hit the constraint and retried
    assert sorted(results) == [1, 2]
    versions = sorted(
        ResumeRow.objects.for_user(user.pk).filter(kind="base").values_list("version", flat=True)
    )
    assert versions == [1, 2]
