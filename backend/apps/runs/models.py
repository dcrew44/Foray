"""Runs, their approvals and their events (design §2.2, §9).

- A `Run` is the only unit of background work a user sees. Its liveness is "a `todo`/`doing`
  procrastinate job holds lock `run:<id>`", so there is no job-id column (§9.8). At most one
  active run exists per `(user, dedupe_key)` (R2).
- An `Approval` is a pause for a decision; at most one is pending per run. `next_step` names the
  continuation task a decision defers (§9.6).
- An `Event` is the progress feed. Its bigserial id is the live cursor, made commit-ordered per
  user by `runs.events.emit()`, the only writer (R5). `kind` has no CHECK, so imported history
  keeps every legacy kind; the catalogue is `runs.catalogue.EventKind`.

The kinds, statuses and JSON shapes live in `runs/catalogue.py`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Now

from apps.accounts.models import User
from apps.core.choices import choices_checks
from apps.core.ids import new_id
from apps.core.models import Timestamped
from apps.core.scoping import UserOwned
from apps.runs.catalogue import (
    ACTIVE,
    ApprovalKind,
    ApprovalStatus,
    DecidedVia,
    Level,
    Mode,
    NextStep,
    RunKind,
    RunStatus,
)

if TYPE_CHECKING:
    from apps.chat.models import Conversation


class Run(UserOwned, Timestamped):
    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    # Indexed by the (user, ...) indexes below.
    user = models.ForeignKey(User, models.CASCADE, related_name="runs", db_index=False)
    kind = models.TextField(choices=RunKind.choices)
    mode = models.TextField(choices=Mode.choices, default=Mode.APPROVE_EACH)
    status = models.TextField(choices=RunStatus.choices, default=RunStatus.QUEUED)
    input = models.JSONField(default=dict)  # one model per kind (catalogue.INPUT_MODELS)
    stats = models.JSONField(default=dict)  # open dict; merged with stats || patch::jsonb
    output = models.JSONField(null=True)  # one model per kind (catalogue.OUTPUT_MODELS)
    error = models.TextField(null=True)  # noqa: DJ001
    # The chat this run reports back to (was an unchecked uuid).
    conversation: models.ForeignKey[Conversation | None] = models.ForeignKey(
        "chat.Conversation", models.SET_NULL, null=True, related_name="runs"
    )
    dedupe_key = models.TextField(null=True)  # noqa: DJ001 (§9.8)
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)

    user_id: str
    conversation_id: uuid.UUID | None

    class Meta(UserOwned.Meta):
        db_table = "runs"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "dedupe_key"],
                condition=Q(status__in=ACTIVE) & Q(dedupe_key__isnull=False),
                name="runs_one_active_per_key",
            ),
            *choices_checks("runs", kind=RunKind, mode=Mode, status=RunStatus),
        ]
        indexes = [
            models.Index(F("user"), F("created_at").desc(), name="runs_user_created"),
            models.Index(fields=["user", "status"], name="runs_user_status"),
            models.Index(fields=["status"], condition=Q(status__in=ACTIVE), name="runs_active"),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.id}: {self.status}"

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE


class Approval(UserOwned):
    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    # Indexed by the (user, status) index.
    user = models.ForeignKey(User, models.CASCADE, related_name="approvals", db_index=False)
    run = models.ForeignKey(Run, models.CASCADE, related_name="approvals")
    kind = models.TextField(choices=ApprovalKind.choices)
    title = models.TextField()
    payload = models.JSONField(default=dict)  # DeepEvalBatchPayload | ShortlistPayload
    status = models.TextField(choices=ApprovalStatus.choices, default=ApprovalStatus.PENDING)
    decision = models.JSONField(null=True)  # StoredDecision {approved, itemIds?}
    decided_via = models.TextField(choices=DecidedVia.choices, null=True)  # noqa: DJ001
    # Declared, so assigning `Now()` (approvals.decide) does not widen the attribute's type.
    decided_at: models.DateTimeField[datetime | None] = models.DateTimeField(null=True)
    # The continuation a decision defers (was `continuation` jsonb, always {userId, runId,
    # approvalId}).
    next_step = models.TextField(choices=NextStep.choices)
    expires_at = models.DateTimeField(null=True)  # pending: now + 7 days; autopilot: null
    created_at = models.DateTimeField(db_default=Now())

    user_id: str
    run_id: uuid.UUID

    class Meta(UserOwned.Meta):
        db_table = "approvals"
        constraints = [
            models.UniqueConstraint(
                fields=["run"],
                condition=Q(status=ApprovalStatus.PENDING),
                name="approvals_one_pending_per_run",
            ),
            *choices_checks(
                "approvals",
                kind=ApprovalKind,
                status=ApprovalStatus,
                decided_via=DecidedVia,
                next_step=NextStep,
            ),
        ]
        indexes = [
            # `run` is indexed by its foreign key.
            models.Index(fields=["user", "status"], name="approvals_user_status"),
            models.Index(
                fields=["expires_at"],
                condition=Q(status=ApprovalStatus.PENDING),
                name="approvals_pending_expiry",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.id}: {self.status}"


class Event(UserOwned):
    # The live cursor; commit-ordered per user by emit() (§9.5).
    id = models.BigAutoField(primary_key=True)
    # Both indexed by the (user, id) and (run, id) indexes.
    user = models.ForeignKey(User, models.CASCADE, related_name="events", db_index=False)
    run = models.ForeignKey(Run, models.CASCADE, null=True, related_name="events", db_index=False)
    approval = models.ForeignKey(Approval, models.SET_NULL, null=True, related_name="events")
    level = models.TextField(choices=Level.choices, default=Level.INFO)
    kind = models.CharField(max_length=64)  # runs.catalogue.EventKind; legacy kinds allowed
    message = models.TextField()
    data = models.JSONField(null=True)  # an open dict per kind
    created_at = models.DateTimeField(db_default=Now())

    user_id: str
    run_id: uuid.UUID | None
    approval_id: uuid.UUID | None

    class Meta(UserOwned.Meta):
        db_table = "events"
        constraints = [*choices_checks("events", level=Level)]
        indexes = [
            models.Index(fields=["user", "id"], name="events_user_id"),
            models.Index(fields=["run", "id"], name="events_run_id"),
        ]

    def __str__(self) -> str:
        return f"#{self.id} {self.kind}"
