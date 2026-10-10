"""§2.1: timestamps are explicit. `bulk_create` keeps given values (no `auto_now` re-dating
imported rows), and `save()` on an existing row bumps `updated_at`, with or without
`update_fields`. `Timestamped` is tested through `User`, the first model that inherits it.

Step H2: a timestamp not given is the database's (`db_default=Now()`, `Now()` on save), never
this process's clock, so the comparisons below use `database_now()`; the skew tests move
Python's clock far away and check that nothing stored follows it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from django.apps import apps as django_apps
from django.db.models import NOT_PROVIDED
from django.db.models.functions import Now

from apps.accounts.models import User
from apps.core.clock import database_now
from tests.clock import python_clock, python_clock_defaults, skewed

pytestmark = pytest.mark.django_db

IMPORTED_CREATED = datetime(2026, 9, 1, 12, 30, tzinfo=UTC)
IMPORTED_UPDATED = datetime(2026, 9, 2, 8, 15, tzinfo=UTC)


def test_bulk_create_keeps_explicit_timestamps() -> None:
    User.objects.bulk_create(
        [
            User(
                email="imported@example.test",
                created_at=IMPORTED_CREATED,
                updated_at=IMPORTED_UPDATED,
            )
        ]
    )
    user = User.objects.get(email="imported@example.test")
    assert user.created_at == IMPORTED_CREATED
    assert user.updated_at == IMPORTED_UPDATED


def test_create_keeps_explicit_timestamps() -> None:
    user = User.objects.create(
        email="kept@example.test", created_at=IMPORTED_CREATED, updated_at=IMPORTED_UPDATED
    )
    user.refresh_from_db()
    assert (user.created_at, user.updated_at) == (IMPORTED_CREATED, IMPORTED_UPDATED)


def test_new_rows_default_to_now() -> None:
    before = database_now()
    user = User.objects.create_user("fresh@example.test")
    assert before <= user.created_at <= database_now()
    assert before <= user.updated_at <= database_now()
    user.refresh_from_db()  # the instance holds what was stored (RETURNING)
    assert before <= user.created_at <= database_now()


def test_save_bumps_updated_at() -> None:
    user = User.objects.create(
        email="saved@example.test", created_at=IMPORTED_CREATED, updated_at=IMPORTED_UPDATED
    )
    user.name = "Renamed"
    before = database_now()
    user.save()
    stored = user.updated_at
    user.refresh_from_db()
    assert user.updated_at == stored  # read back with the save, not left as an expression
    assert user.updated_at >= before
    assert user.created_at == IMPORTED_CREATED


def test_save_with_update_fields_bumps_updated_at() -> None:
    user = User.objects.create(
        email="partial@example.test", created_at=IMPORTED_CREATED, updated_at=IMPORTED_UPDATED
    )
    user.name = "Only the name"
    before = database_now()
    user.save(update_fields=["name"])
    user.refresh_from_db()
    assert user.name == "Only the name"
    assert user.updated_at >= before
    assert user.updated_at - IMPORTED_UPDATED > timedelta(days=1)
    assert user.created_at == IMPORTED_CREATED


def test_user_emails_are_lowercased() -> None:
    user = User.objects.create_user("Mixed.Case@Example.TEST")
    assert user.email == "mixed.case@example.test"
    assert User.objects.get_by_natural_key("MIXED.case@example.test") == user
    assert not user.has_usable_password()
    assert len(user.pk) == 32 and user.pk.isalnum()


# Every column the application stamps itself; each is the database's present by default.
ROW_STAMPS = {"created_at", "updated_at", "stage_changed_at"}
STAMPED_COLUMNS = {
    "users.created_at",
    "users.updated_at",
    "companies.created_at",
    "companies.updated_at",
    "api_usage.updated_at",
    "job_matches.created_at",
    "job_matches.updated_at",
    "job_matches.stage_changed_at",
    "resumes.created_at",
    "resumes.updated_at",
    "documents.created_at",
    "runs.created_at",
    "runs.updated_at",
    "approvals.created_at",
    "events.created_at",
    "conversations.created_at",
    "conversations.updated_at",
    "messages.created_at",
    "messages.updated_at",
    "llm_calls.created_at",
}


def test_every_row_timestamp_defaults_to_the_databases_clock() -> None:
    """Structural (step H2): a row's own timestamps are `db_default=Now()` with no Python
    default, so no `default=timezone.now` can come back unnoticed. The one Python-clock default
    left is the import record's `started_at`, which the importer always sets explicitly and
    which is only displayed (the `legacy` app goes after the cutover)."""
    found: dict[str, tuple[object, object]] = {
        f"{model._meta.db_table}.{field.column}": (field.default, field.db_default)
        for model in django_apps.get_models()
        if model.__module__.startswith("apps.")
        for field in model._meta.concrete_fields
        if field.name in ROW_STAMPS
    }
    assert set(found) == STAMPED_COLUMNS
    wrong = {
        column: values
        for column, values in found.items()
        if values[0] is not NOT_PROVIDED or not isinstance(values[1], Now)
    }
    assert wrong == {}
    assert python_clock_defaults() == ["legacy_imports.started_at"]


@pytest.mark.parametrize("skew", [timedelta(minutes=-90), timedelta(days=40)])
def test_stored_timestamps_ignore_this_processs_clock(skew: timedelta) -> None:
    """Python's clock and the database's drift apart (the Docker VM's falls behind after a
    sleep). With Python's clock far off, every timestamp written without an explicit value
    still reads the database's present. `python_clock` moves `default=timezone.now` field
    defaults too, so a Python-clock default fails here by the whole skew."""
    from apps.chat.models import Conversation
    from apps.llm.models import LlmCall
    from apps.runs.models import Event
    from tests.factories import make_conversation, make_llm_call, make_run

    before = database_now()
    with python_clock(skewed(skew)):
        user = User.objects.create_user("skewed@example.test")
        run = make_run(user)
        conversation = make_conversation(user)
        event = Event.objects.create_for(user.pk, kind="run.started", message="Started.", run=run)
        call = make_llm_call(user)
        user.name = "Saved"
        user.save()
    after = database_now()
    stamped = [
        user.created_at,
        user.updated_at,
        run.created_at,
        run.updated_at,
        conversation.created_at,
        event.created_at,
        call.created_at,
        Conversation.objects.for_user(user.pk).get().updated_at,
        LlmCall.objects.for_user(user.pk).get().created_at,
    ]
    assert all(before <= stamp <= after for stamp in stamped), stamped
