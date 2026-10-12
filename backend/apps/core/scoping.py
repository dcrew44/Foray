"""User scoping for per-user rows (design §2.1, R8; DESIGN.md invariant 8).

Every per-user model inherits `UserOwned`. Its public manager, `objects`, refuses to build a
query on its own: app code reaches per-user rows only through

- `Model.objects.for_user(user_id)`, the normal path;
- `Model.objects.unscoped(reason)`, for system code that is about every user (the reaper, the
  cron, ledger totals, the import), with the reason written at the call site;
- `Model.objects.create_for(user_id, ...)` and `bulk_create_for(user_id, objs)` for inserts.

`Model.objects.filter(...)`, `.all()`, `.get(...)`, `.create(...)` and every other queryset
method raise `UnscopedQueryError`.

Django itself keeps working, through public API only: the plain `_plain` manager is the default
and base manager, so related managers (`user.job_matches`, already filtered by their foreign
key), cascades, `refresh_from_db` and serializers never see the refusal. Concrete models declare
`class Meta(UserOwned.Meta)` so these options are inherited. `tests/test_register.py` keeps
`._plain` and `_default_manager` out of app code (this module is on its allowlist).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, ClassVar, NoReturn, Self

from django.db import models


class UnscopedQueryError(RuntimeError):
    """A per-user model was queried without a user (R8)."""


class ScopedManager[M: models.Model](models.Manager[M]):
    """The only public entry point for per-user rows."""

    def get_queryset(self) -> NoReturn:
        name = self.model.__name__
        raise UnscopedQueryError(f"{name}.objects needs .for_user(user_id) or .unscoped(reason)")

    def _all(self) -> models.QuerySet[M]:
        return super().get_queryset()

    def for_user(self, user_id: str) -> models.QuerySet[M]:
        """This user's rows: the normal path."""
        if not user_id:
            raise UnscopedQueryError(f"{self.model.__name__}.objects.for_user() needs a user id")
        return self._all().filter(user_id=user_id)

    def unscoped(self, reason: str) -> models.QuerySet[M]:
        """Every user's rows, for system code. The reason documents the call site."""
        if not reason or not reason.strip():
            raise ValueError("unscoped() needs a reason")
        return self._all()

    def create_for(self, user_id: str, **fields: Any) -> M:
        if not user_id:
            raise UnscopedQueryError(f"{self.model.__name__}.objects.create_for() needs a user id")
        return self._all().create(user_id=user_id, **fields)

    def bulk_create_for(self, user_id: str, objs: Iterable[M], **kwargs: Any) -> list[M]:
        if not user_id:
            raise UnscopedQueryError(
                f"{self.model.__name__}.objects.bulk_create_for() needs a user id"
            )
        rows = list(objs)
        for row in rows:
            row.user_id = user_id  # pyright: ignore[reportAttributeAccessIssue]
        return self._all().bulk_create(rows, **kwargs)


class UserOwned(models.Model):
    """The base of every per-user model. Subclasses declare their own `user` foreign key."""

    objects: ClassVar[ScopedManager[Self]] = ScopedManager()
    _plain: ClassVar[models.Manager[Self]] = models.Manager()  # Django internals only

    class Meta:
        abstract = True
        default_manager_name = "_plain"  # related managers, cascades and serializers
        base_manager_name = "_plain"
