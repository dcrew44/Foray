"""Shared model bases and the core app's one table (design §2.1, §2.2).

No field uses `auto_now`/`auto_now_add`: Django's insert compiler calls `pre_save(add=True)`,
which would overwrite an explicit value with `now()` even in `bulk_create`, and so re-date every
imported row. Timestamps are plain defaults instead, and `Timestamped.save()` bumps
`updated_at` on updates. `QuerySet.update()` and raw SQL set `updated_at = now()` themselves.
"""

from __future__ import annotations

from collections.abc import Iterable

from django.db import models
from django.utils import timezone


class Timestamped(models.Model):
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        abstract = True

    def save(
        self,
        force_insert: bool = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Iterable[str] | None = None,
    ) -> None:
        if not self._state.adding:
            self.updated_at = timezone.now()
            if update_fields is not None:
                update_fields = list(dict.fromkeys([*update_fields, "updated_at"]))
        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )


class RateLimit(models.Model):
    """Fixed-window counters shared by every process (`core.throttle`, §4.5)."""

    key = models.TextField()
    window_start = models.DateTimeField()
    count = models.IntegerField(default=0)

    class Meta:
        db_table = "rate_limits"
        constraints = [
            models.UniqueConstraint(fields=["key", "window_start"], name="rate_limits_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.key}@{self.window_start:%Y-%m-%dT%H:%M}Z: {self.count}"
