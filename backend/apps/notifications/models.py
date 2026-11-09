"""One row for each match a user is told about away from the app (#59).

The row is the "exactly once": `(user, match, channel)` is unique, so a match a watch announced
can be queued for a channel only once, whatever runs again. It is also the outbox the
`notify.send` task works from: `pending` rows whose `send_after` has come are sent, and a row
only ever leaves `pending` for one of the three other states.

| Status | Means |
|---|---|
| `pending` | waiting to be sent: new, held by quiet hours, or between two attempts |
| `sent` | the channel accepted it (`sent_at`); as one of several, it may have gone in a summary |
| `failed` | every attempt failed; `error` keeps the last reason |
| `skipped` | never sent; `error` says why (no topic any more, or the match was moved on) |

`send_after` is also the lease: the sender moves it into the future as it claims a row, so two
jobs never send the same one.
"""

from __future__ import annotations

import uuid

from django.db import models
from django.db.models import Q
from django.db.models.functions import Now

from apps.accounts.models import User
from apps.core.choices import choices_checks
from apps.core.ids import new_id
from apps.core.models import Timestamped
from apps.core.scoping import UserOwned
from apps.matching.models import JobMatch


class Channel(models.TextChoices):
    NTFY = "ntfy"


class NotificationStatus(models.TextChoices):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


class Notification(UserOwned, Timestamped):
    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    # Indexed by the (user, match, channel) constraint.
    user = models.ForeignKey(User, models.CASCADE, related_name="notifications", db_index=False)
    match = models.ForeignKey(JobMatch, models.CASCADE, related_name="notifications")
    channel = models.TextField(choices=Channel.choices)
    status = models.TextField(
        choices=NotificationStatus.choices, default=NotificationStatus.PENDING
    )
    # Not before this (the database's clock): the end of quiet hours, the wait before another
    # attempt, or the lease of the job that is sending it now.
    send_after = models.DateTimeField(db_default=Now())
    sent_at = models.DateTimeField(null=True)
    attempts = models.SmallIntegerField(db_default=0)  # sends tried so far
    error = models.TextField(null=True)  # noqa: DJ001 (null: nothing went wrong)

    user_id: str
    match_id: uuid.UUID

    class Meta(UserOwned.Meta):
        db_table = "notifications"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "match", "channel"], name="notifications_once_per_match"
            ),
            models.CheckConstraint(
                condition=Q(attempts__gte=0), name="notifications_attempts_range"
            ),
            *choices_checks("notifications", channel=Channel, status=NotificationStatus),
        ]
        indexes = [
            # What the sender asks every minute: is anything due?
            models.Index(
                fields=["send_after"],
                condition=Q(status="pending"),
                name="notifications_due",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} / {self.match_id} via {self.channel}: {self.status}"
