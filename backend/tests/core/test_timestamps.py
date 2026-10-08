"""§2.1: timestamps are explicit. `bulk_create` keeps given values (no `auto_now` re-dating
imported rows), and `save()` on an existing row bumps `updated_at`, with or without
`update_fields`. `Timestamped` is tested through `User`, the first model that inherits it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import User

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
    before = timezone.now()
    user = User.objects.create_user("fresh@example.test")
    assert before <= user.created_at <= timezone.now()
    assert before <= user.updated_at <= timezone.now()


def test_save_bumps_updated_at() -> None:
    user = User.objects.create(
        email="saved@example.test", created_at=IMPORTED_CREATED, updated_at=IMPORTED_UPDATED
    )
    user.name = "Renamed"
    before = timezone.now()
    user.save()
    user.refresh_from_db()
    assert user.updated_at >= before
    assert user.created_at == IMPORTED_CREATED


def test_save_with_update_fields_bumps_updated_at() -> None:
    user = User.objects.create(
        email="partial@example.test", created_at=IMPORTED_CREATED, updated_at=IMPORTED_UPDATED
    )
    user.name = "Only the name"
    before = timezone.now()
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
