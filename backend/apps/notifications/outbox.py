"""The notification outbox (#59): what is queued, what is due, and what became of it. Sync.

A watch that announces matches calls `queue_matches` inside the transaction that completes it:
one `notifications` row per match for a user with an ntfy topic (nothing at all for a user
without one), and a `notify.send` job. The job, and a sweep of the same task every minute, send
what is due through `claim` → publish → `record_sent` / `record_failure`, so no transaction
spans a request to ntfy.

**Once per match.** `(user, match, channel)` is unique and rows are inserted with
`ON CONFLICT DO NOTHING`, so a watch that runs again, or a job that is delivered twice, adds
nothing. `claim` locks the due rows, and before it returns them it moves their `send_after`
ten minutes ahead (the lease) and counts the attempt, in one transaction: a second job running
at the same moment finds nothing due. A row is sent again only after an attempt that was
recorded as failed, or whose job died before it could record anything, and never more than
`MAX_ATTEMPTS` times. An attempt whose answer was lost (a timeout after ntfy accepted the
message) is the one case that can reach a phone twice.

**Decided when it is due, not when it is queued.** `claim` reads the user's settings at send
time: no topic any more → `skipped`; inside quiet hours → `send_after` moves to their end and
the sweep picks the row up then; a match the user has already moved out of `found` (they saw
it in the app while the notification waited) → `skipped`. How many people the user knows at
the posting's company (#64) is counted then too, so the message is true of their contacts as
they are when it goes out.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.db.models import Case, F, TextField, Value, When
from django.db.models.functions import Now
from procrastinate.exceptions import AlreadyEnqueued

from apps.accounts.models import User
from apps.accounts.schemas import Settings
from apps.contacts.known import known_at_jobs
from apps.core import throttle
from apps.core.clock import database_now
from apps.core.errors import ApiError
from apps.core.queue import defer_in_tx
from apps.matching.models import JobMatch, Stage
from apps.matching.queries import valid_uuids
from apps.notifications import compose
from apps.notifications.compose import MAX_SINGLE, Message
from apps.notifications.models import Channel, Notification, NotificationStatus
from apps.notifications.quiet import clock, quiet_until, zone
from apps.runs.step import task_for

__all__ = [
    "LEASE",
    "MAX_ATTEMPTS",
    "NO_TOPIC",
    "RETRY_AFTER",
    "SEND_TASK",
    "TEST_TASK",
    "Batch",
    "Delivery",
    "claim",
    "due_user_ids",
    "queue_matches",
    "record_failure",
    "record_sent",
    "topic_for_test",
    "topic_of",
]

log = logging.getLogger("foray.notifications")

SEND_TASK = "notify.send"
TEST_TASK = "notify.test"

MAX_ATTEMPTS = 3
# Longer than `notify.send` may run (`step.TASKS`), so a row a job has claimed cannot come due
# again while that job is still sending it.
LEASE = timedelta(minutes=10)
# The wait after the first failed attempt, and after the second. The third failure is final.
RETRY_AFTER = (timedelta(minutes=1), timedelta(minutes=10))

NO_TOPIC = "Save a topic first, then send a test."
SKIPPED_NO_TOPIC = "The topic was removed before this could be sent."
SKIPPED_HANDLED = "The match had already been moved on in Foray."
GAVE_UP = "A worker stopped while sending this, more than once."

PENDING = NotificationStatus.PENDING


@dataclass(frozen=True, slots=True)
class Delivery:
    """One message, and the rows it settles: one match, or every match a summary stands for."""

    ids: tuple[uuid.UUID, ...]
    message: Message


@dataclass(frozen=True, slots=True)
class Batch:
    user_id: str
    topic: str
    deliveries: tuple[Delivery, ...]


def topic_of(user_id: str) -> str:
    """The user's ntfy topic, or "" when they have none (or no longer exist)."""
    stored = User.objects.filter(pk=user_id).values_list("settings", flat=True).first()
    return Settings.read(stored).ntfy_topic


def topic_for_test(user_id: str) -> str:
    """The topic Settings' test will go to, checked before the job is queued: 400 without one,
    and counted against the test's throttle (in autocommit, so the count stays)."""
    topic = topic_of(user_id)
    if not topic:
        raise ApiError(400, NO_TOPIC)
    limit = throttle.NOTIFY_TEST
    throttle.throttle(f"notify-test:{user_id}", limit.limit, limit.window_s)
    return topic


# --- queueing ------------------------------------------------------------------------------------


def _defer_send(user_id: str) -> None:
    # Already enqueued: a job for this user is waiting, and will find these rows too.
    with contextlib.suppress(AlreadyEnqueued):
        defer_in_tx(task_for(SEND_TASK), queueing_lock=f"notify:{user_id}", user_id=user_id)


def _queue(user_id: str, match_ids: Sequence[str]) -> int:
    ids = valid_uuids(match_ids)
    if not ids or not topic_of(user_id):
        return 0
    # The user's own matches only; a foreign key alone would take anyone's id.
    own = JobMatch.objects.for_user(user_id).filter(id__in=ids).values_list("id", flat=True)
    told = (
        Notification.objects.for_user(user_id)
        .filter(channel=Channel.NTFY, match_id__in=ids)
        .values_list("match_id", flat=True)
    )
    wanted = {str(match_id) for match_id in own} - {str(match_id) for match_id in told}
    # In the order given (a watch lists its best match first), which the row ids then keep.
    new = [
        Notification(match_id=uuid.UUID(match_id), channel=Channel.NTFY)
        for match_id in ids
        if match_id in wanted
    ]
    if not new:
        return 0
    # The unique constraint has the last word if another transaction got there first.
    Notification.objects.bulk_create_for(user_id, new, ignore_conflicts=True)
    _defer_send(user_id)
    return len(new)


def queue_matches(user_id: str, match_ids: Sequence[str]) -> int:
    """Queue a phone notification for each of these matches the user has not been sent yet,
    and the job that sends them; returns how many were new. Nothing for a user with no topic.

    Call it inside the transaction that announces the matches, before that transaction's
    `emit()` (it reads and inserts rows that reference the user and the matches). It never
    raises: its work is in a savepoint, and a failure is logged and leaves the caller's
    transaction as it was, because a notification must never fail the watch that found the
    match."""
    try:
        with transaction.atomic():
            return _queue(user_id, match_ids)
    except Exception:
        log.exception("could not queue phone notifications for user %s", user_id)
        return 0


# --- sending -------------------------------------------------------------------------------------


def due_user_ids() -> list[str]:
    """Who has something due, for the sweep. The database's clock decides."""
    return list(
        Notification.objects.unscoped("notify.send: who has a notification due")
        .filter(status=PENDING, send_after__lte=Now())
        .order_by("user_id")
        .values_list("user_id", flat=True)
        .distinct()
    )


def _contacts_at(user_id: str, rows: Sequence[Notification]) -> dict[str, int]:
    """By posting, how many people the user knows at its company (#64): what a notification
    counts. One read, in a savepoint: failing to count must never cost the notification."""
    try:
        with transaction.atomic():
            found = known_at_jobs(user_id, [row.match.job for row in rows])
    except Exception:
        log.warning("could not count contacts for user %s", user_id, exc_info=True)
        return {}
    return {job_id: len(people) for job_id, people in found.items()}


def claim(user_id: str) -> Batch | None:
    """Take the user's due notifications for sending: at most `MAX_SINGLE` messages of their
    own, best match first, and one line for the rest. None when nothing is to be sent now
    (nothing due, quiet hours, no topic, or another job holds the rows). See the module
    docstring for what happens to the rows on each path."""
    with transaction.atomic():
        now = database_now()
        mine = Notification.objects.for_user(user_id)
        rows = list(
            mine.filter(channel=Channel.NTFY, status=PENDING, send_after__lte=Now())
            .select_related("match__job")
            .select_for_update(skip_locked=True, of=("self",))
            .order_by(F("match__deep_score").desc(nulls_last=True), "created_at", "id")
        )
        if not rows:
            return None

        def settle(found: list[Notification], status: str, reason: str) -> None:
            mine.filter(id__in=[row.id for row in found]).update(
                status=status, error=reason, updated_at=Now()
            )

        user = User.objects.only("settings", "timezone").get(pk=user_id)
        settings = Settings.read(user.settings)
        if not settings.ntfy_topic:
            settle(rows, NotificationStatus.SKIPPED, SKIPPED_NO_TOPIC)
            return None

        # A job that died mid-send leaves its rows leased and counted; the last attempt is final.
        spent = [row for row in rows if row.attempts >= MAX_ATTEMPTS]
        if spent:
            settle(spent, NotificationStatus.FAILED, GAVE_UP)
        live = [row for row in rows if row.attempts < MAX_ATTEMPTS]
        handled = [row for row in live if row.match.stage != Stage.FOUND]
        if handled:
            settle(handled, NotificationStatus.SKIPPED, SKIPPED_HANDLED)
        rows = [row for row in live if row.match.stage == Stage.FOUND]
        if not rows:
            return None

        until = quiet_until(
            now,
            clock(settings.quiet_hours_start),
            clock(settings.quiet_hours_end),
            zone(user.timezone),
        )
        ids = [row.id for row in rows]
        if until is not None:
            mine.filter(id__in=ids).update(send_after=until, updated_at=Now())
            return None

        mine.filter(id__in=ids).update(
            attempts=F("attempts") + 1, send_after=Now() + LEASE, updated_at=Now()
        )
        singles, rest = rows[:MAX_SINGLE], rows[MAX_SINGLE:]
        knows = _contacts_at(user_id, singles)
        deliveries = [
            Delivery(
                (row.id,), compose.match_message(row.match, now, knows.get(row.match.job_id, 0))
            )
            for row in singles
        ]
        if rest:
            deliveries.append(
                Delivery(tuple(row.id for row in rest), compose.summary_message(len(rest)))
            )
        return Batch(user_id, settings.ntfy_topic, tuple(deliveries))


def record_sent(user_id: str, ids: Sequence[uuid.UUID]) -> int:
    """The channel accepted the message: its rows are done."""
    return (
        Notification.objects.for_user(user_id)
        .filter(id__in=list(ids), status=PENDING)
        .update(status=NotificationStatus.SENT, sent_at=Now(), error=None, updated_at=Now())
    )


def record_failure(user_id: str, ids: Sequence[uuid.UUID], reason: str) -> int:
    """An attempt failed. The rows wait for another (`RETRY_AFTER`), or after `MAX_ATTEMPTS`
    are `failed` for good; either way `error` keeps the reason."""
    first, later = RETRY_AFTER
    return (
        Notification.objects.for_user(user_id)
        .filter(id__in=list(ids), status=PENDING)
        .update(
            status=Case(
                When(attempts__gte=MAX_ATTEMPTS, then=Value(NotificationStatus.FAILED.value)),
                default=Value(PENDING.value),
                output_field=TextField(),
            ),
            send_after=Case(
                When(attempts__lte=1, then=Now() + first),
                default=Now() + later,
            ),
            error=reason,
            updated_at=Now(),
        )
    )
