"""The record of a legacy import (design §2.2, §3.2). Deleted with the `legacy` app once the
user retires the old database.

The importer writes one row in its own transaction, together with everything it imported, so a
failed import leaves none. `cutover` is set by `mark_cutover` and blocks `--replace` and
`reset_app_db` afterwards.
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone

from apps.core.choices import choices_checks
from apps.core.ids import new_id


class ImportStatus(models.TextChoices):
    OK = "ok"
    FAILED = "failed"


class ImportRecord(models.Model):
    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True)
    status = models.TextField(choices=ImportStatus.choices)
    legacy_db = models.TextField()  # the source database's name
    counts = models.JSONField()  # per table: legacy rows, imported rows, converted rows
    checksums = models.JSONField()  # id-set checksums and the legacy fingerprints (§3.4)
    report = models.JSONField()
    cutover = models.BooleanField(default=False)

    class Meta:
        db_table = "legacy_imports"
        constraints = [*choices_checks("legacy_imports", status=ImportStatus)]

    def __str__(self) -> str:
        return f"import {self.started_at:%Y-%m-%dT%H:%M}Z from {self.legacy_db}: {self.status}"
