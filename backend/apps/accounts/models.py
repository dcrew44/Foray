"""The user (design §2.2, §4.1). Written in step 1 because `django.setup()` needs
`AUTH_USER_MODEL` installed from the first commit.

Preferences, settings and interview state live on the user row: without Better Auth owning a
`user` table there is no reason for a separate profile row that might be missing.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import ClassVar

from django.conf import settings as django_settings
from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.db import models

from apps.core.ids import new_user_id
from apps.core.models import Timestamped


def default_timezone() -> str:
    """`APP_TIMEZONE`; chat overwrites it with the browser's zone (§7.1)."""
    return django_settings.ENV.app_timezone


def normalize_email_address(email: str) -> str:
    return email.strip().lower()


class UserManager(BaseUserManager["User"]):
    """Lowercases the email on every write and lookup."""

    def get_by_natural_key(self, username: str | None) -> User:
        return self.get(email=normalize_email_address(username or ""))

    def create_user(self, email: str, password: str | None = None, **fields: object) -> User:
        user = self.model(email=normalize_email_address(email), **fields)
        if password is None:
            user.set_unusable_password()
        else:
            user.set_password(password)
        user.save(using=self._db)
        return user


class User(AbstractBaseUser, Timestamped):
    # 32 random [A-Za-z0-9] (Better Auth's shape); legacy ids are kept verbatim, so storage keys
    # stay valid.
    id = models.CharField(primary_key=True, max_length=64, default=new_user_id, editable=False)
    email = models.CharField(max_length=320, unique=True)
    name = models.CharField(max_length=200, blank=True)
    # "ba_scrypt$<32>$<128>" is 171 characters, above Django's 128.
    password = models.CharField(max_length=256)
    is_active = models.BooleanField(default=True)
    preferences = models.JSONField(default=dict)  # accounts.schemas.Preferences
    interview_state = models.JSONField(default=dict)  # accounts.schemas.InterviewState
    settings = models.JSONField(default=dict)  # accounts.schemas.Settings, partial
    timezone = models.CharField(max_length=64, default=default_timezone)

    objects: ClassVar[UserManager] = UserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = ["name"]

    class Meta:
        db_table = "users"

    def __str__(self) -> str:
        return self.email

    def save(
        self,
        force_insert: bool = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Iterable[str] | None = None,
    ) -> None:
        self.email = normalize_email_address(self.email)
        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )
