"""What an employer's email suggests about a match, and the user's answer (#26). Sync.

A rejection or an interview invitation that was matched to an application **suggests** a stage
and moves nothing. The match page shows the newest one that still applies ("An email from
Stripe Recruiting on 4 November looks like a rejection: … Move this match to Rejected?"), and
only `accept()`, which is the user pressing Move, changes the stage. It does so through
`matching.queries.set_match_stage`, the same write every other stage change makes.

| The email | Suggests | While the match is in |
|---|---|---|
| a rejection | `rejected` | `applied`, `interviewing` or `offer` |
| an interview invitation | `interviewing` | `applied` |

A suggestion whose match has already moved past it (the user got there first) is not shown and
cannot be accepted; nothing needs to clear it. Dismissing one hides it for good.

`for_match` is the match page's read: one query, and none at all for a match no employer could
be answering (anything not yet applied to, or already closed), which is most of them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from django.db import transaction
from django.db.models.functions import Now

from apps.mail.models import ApplicationEmail as Email
from apps.mail.models import Decision, EmailKind
from apps.matching import queries as match_queries
from apps.matching.models import JobMatch, Stage

__all__ = [
    "APPLIES_IN",
    "MOVES_TO",
    "MatchMailView",
    "Outcome",
    "Suggestion",
    "accept",
    "dismiss",
    "for_match",
]

# Where accepting a suggestion moves the match, and the stages it still makes sense in.
MOVES_TO: dict[str, str] = {
    EmailKind.REJECTION: Stage.REJECTED,
    EmailKind.INTERVIEW: Stage.INTERVIEWING,
}
APPLIES_IN: dict[str, tuple[str, ...]] = {
    EmailKind.REJECTION: (Stage.APPLIED, Stage.INTERVIEWING, Stage.OFFER),
    EmailKind.INTERVIEW: (Stage.APPLIED,),
}
# The stages in which a match can have mail worth reading at all.
_WITH_MAIL = frozenset(stage for stages in APPLIES_IN.values() for stage in stages)
RECENT = 20

Outcome = Literal["done", "gone", "stale"]


@dataclass(frozen=True, slots=True)
class Suggestion:
    id: uuid.UUID
    kind: str  # "rejection" or "interview"
    stage: str  # where accepting it moves the match
    sender: str
    received_at: datetime
    subject: str


@dataclass(frozen=True, slots=True)
class MatchMailView:
    """What a match's mail says, for its page."""

    confirmed_at: datetime | None = None
    confirmation_subject: str | None = None
    suggestion: Suggestion | None = None


def _applies(kind: str, stage: str) -> bool:
    return stage in APPLIES_IN.get(kind, ())


def _sender(email: Email) -> str:
    return email.sender_name or email.sender_domain or "the employer"


def for_match(user_id: str, match: JobMatch) -> MatchMailView:
    """The match's confirmation (for one applied to by hand: an application row carries its
    own) and its newest suggestion that still applies. Reads only for a match in a stage an
    employer could still be answering."""
    if match.stage not in _WITH_MAIL:
        return MatchMailView()
    emails = list(
        Email.objects.for_user(user_id)
        .filter(match_id=match.id, decision__isnull=True)
        .exclude(kind=EmailKind.OTHER)
        .order_by("-received_at", "-id")[:RECENT]
    )
    confirmations = [email for email in emails if email.kind == EmailKind.CONFIRMATION]
    first = min(confirmations, key=lambda email: email.received_at, default=None)
    suggested = next((email for email in emails if _applies(email.kind, match.stage)), None)
    return MatchMailView(
        confirmed_at=first.received_at if first else None,
        confirmation_subject=first.subject if first else None,
        suggestion=Suggestion(
            id=suggested.id,
            kind=suggested.kind,
            stage=MOVES_TO[suggested.kind],
            sender=_sender(suggested),
            received_at=suggested.received_at,
            subject=suggested.subject,
        )
        if suggested
        else None,
    )


def _open(user_id: str, email_id: uuid.UUID) -> Email | None:
    """The user's undecided suggestion with this id, locked; None for anything else."""
    return (
        Email.objects.for_user(user_id)
        .select_for_update(of=("self",))
        .filter(id=email_id, kind__in=list(MOVES_TO), match__isnull=False)
        .first()
    )


def accept(user_id: str, email_id: uuid.UUID) -> tuple[Outcome, str | None]:
    """The user pressed Move: the match goes to the suggested stage, and the suggestion is
    settled. Answers `("done", stage)`; `("gone", None)` for an id that is not the user's
    suggestion; `("stale", None)` when it was already answered or no longer applies."""
    with transaction.atomic():
        email = _open(user_id, email_id)
        if email is None or email.match_id is None:
            return "gone", None
        if email.decision is not None:
            return "stale", None
        stage = (
            JobMatch.objects.for_user(user_id)
            .filter(id=email.match_id)
            .values_list("stage", flat=True)
            .first()
        )
        if stage is None or not _applies(email.kind, stage):
            return "stale", None
        target = MOVES_TO[email.kind]
        if not match_queries.set_match_stage(user_id, email.match_id, target):
            return "gone", None
        Email.objects.for_user(user_id).filter(id=email.id).update(
            decision=Decision.ACCEPTED, decided_at=Now(), updated_at=Now()
        )
    return "done", target


def dismiss(user_id: str, email_id: uuid.UUID) -> Outcome:
    """The user said no: the suggestion is not shown again, and nothing else changes."""
    with transaction.atomic():
        email = _open(user_id, email_id)
        if email is None:
            return "gone"
        if email.decision is not None:
            return "stale"
        Email.objects.for_user(user_id).filter(id=email.id).update(
            decision=Decision.DISMISSED, decided_at=Now(), updated_at=Now()
        )
    return "done"
