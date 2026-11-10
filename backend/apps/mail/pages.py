"""What the match page and the Settings page show of a user's mail (#26). Sync.

Kept here so `apps/pages` only places them: `match_mail()` is the match page's `mail`, and
`mail_settings()` the Settings page's. Neither ever answers a token, sealed or not: the account
is read into `MailAccountView`, which has no field for one.

**Queries.** `match_mail` reads the application's own columns for the confirmation (the match
page has joined them already) and makes one query for a match in `applied`, `interviewing` or
`offer`, none for any other. `mail_settings` makes one: the account, its counts, and the newest
suggestions still waiting (an array subquery, so they come with the row).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, cast

from django.conf import settings
from django.contrib.postgres.expressions import ArraySubquery
from django.db.models import Count, OuterRef, Q
from django.db.models.functions import JSONObject

from apps.applications.models import Application
from apps.core.router import ApiModel
from apps.mail import connect, google, suggestions
from apps.mail.models import ApplicationEmail, EmailKind, MailAccount
from apps.matching.models import JobMatch
from apps.matching.queries import StageName

__all__ = [
    "MailAccountView",
    "MailFound",
    "MailNotice",
    "MailSettings",
    "MailSuggestion",
    "MailWaiting",
    "MatchMail",
    "mail_settings",
    "match_mail",
]

SuggestionKind = Literal["rejection", "interview"]
WAITING_SHOWN = 5


# An employer's email that looks like news about this match, and the stage it suggests.
class MailSuggestion(ApiModel):
    id: uuid.UUID
    kind: SuggestionKind
    stage: StageName  # where accepting it moves the match
    sender: str  # the display name, else the domain
    received_at: datetime
    subject: str


class MatchMail(ApiModel):
    # Proof of arrival (#23): when the employer's confirmation came, and its subject line.
    confirmed_at: datetime | None
    confirmation_subject: str | None
    suggestion: MailSuggestion | None  # the newest one that still applies


class MailFound(ApiModel):
    confirmations: int
    rejections: int
    interviews: int
    matched: int  # of those, the ones matched to one of the user's applications
    waiting: int  # suggestions the user has not answered


# A suggestion that is waiting, and the match page to answer it on.
class MailWaiting(ApiModel):
    match_id: uuid.UUID
    kind: SuggestionKind
    company: str
    title: str
    received_at: datetime


class MailAccountView(ApiModel):
    address: str
    status: Literal["connected", "reconnect"]
    connected_at: datetime
    last_checked_at: datetime | None
    last_error: str  # a sentence; "" when the last check went well
    found: MailFound
    waiting: list[MailWaiting]  # the newest few


class MailNotice(ApiModel):
    ok: bool
    message: str


class MailSettings(ApiModel):
    configured: bool  # this install has a Google OAuth client
    redirect_uri: str  # what to register with it
    account: MailAccountView | None
    notice: MailNotice | None  # how the connection attempt that led here went


# --- the match page ------------------------------------------------------------------------------


def _suggestion(found: suggestions.Suggestion | None) -> MailSuggestion | None:
    if found is None:
        return None
    return MailSuggestion(
        id=found.id,
        kind=cast(SuggestionKind, found.kind),
        stage=cast(StageName, found.stage),
        sender=found.sender,
        received_at=found.received_at,
        subject=found.subject,
    )


def match_mail(user_id: str, match: JobMatch) -> MatchMail:
    """`match` is the page's own row, read with its application joined."""
    try:
        application: Application | None = match.application  # pyright: ignore[reportAttributeAccessIssue]
    except Application.DoesNotExist:
        application = None
    view = suggestions.for_match(user_id, match)
    if application is not None and application.confirmed_at is not None:
        confirmed_at, subject = application.confirmed_at, application.confirmation_subject
    else:
        confirmed_at, subject = view.confirmed_at, view.confirmation_subject
    return MatchMail(
        confirmed_at=confirmed_at,
        confirmation_subject=(subject or None) if confirmed_at else None,
        suggestion=_suggestion(view.suggestion),
    )


# --- the Settings page ---------------------------------------------------------------------------


def _waiting_filter(prefix: str = "") -> Q:
    """Undecided suggestions whose match is still in a stage they apply in."""
    applies = Q()
    for kind, stages in suggestions.APPLIES_IN.items():
        applies |= Q(**{f"{prefix}kind": kind, f"{prefix}match__stage__in": stages})
    return Q(**{f"{prefix}decision__isnull": True}) & applies


def _account(user_id: str) -> MailAccountView | None:
    def count(**where: object) -> Count:
        return Count(
            "emails", filter=Q(**{f"emails__{key}": value for key, value in where.items()})
        )

    # The newest suggestions still waiting, each with where to answer it: the user's own rows,
    # read as a subquery of the account's.
    waiting = (
        ApplicationEmail.objects.for_user(user_id)
        .filter(_waiting_filter(), account_id=OuterRef("pk"))
        .order_by("-received_at", "-id")
        .values(
            row=JSONObject(
                match_id="match_id",
                kind="kind",
                company="match__job__company_name",
                title="match__job__title",
                received_at="received_at",
            )
        )[:WAITING_SHOWN]
    )
    account = (
        MailAccount.objects.for_user(user_id)
        .annotate(
            confirmations=count(kind=EmailKind.CONFIRMATION),
            rejections=count(kind=EmailKind.REJECTION),
            interviews=count(kind=EmailKind.INTERVIEW),
            matched=count(match__isnull=False),
            waiting=Count("emails", filter=_waiting_filter("emails__")),
            waiting_rows=ArraySubquery(waiting),
        )
        .first()
    )
    if account is None:
        return None
    read: Any = account  # the annotations
    return MailAccountView(
        address=account.address,
        status=cast(Literal["connected", "reconnect"], account.status),
        connected_at=account.created_at,
        last_checked_at=account.last_checked_at,
        last_error=account.last_error,
        found=MailFound(
            confirmations=read.confirmations,
            rejections=read.rejections,
            interviews=read.interviews,
            matched=read.matched,
            waiting=read.waiting,
        ),
        waiting=[MailWaiting.model_validate(row) for row in read.waiting_rows or []],
    )


def mail_settings(user_id: str, word: str | None = None) -> MailSettings:
    """`word` is what the OAuth callback put in the address (`?mail=connected`)."""
    told = connect.notice(word)
    return MailSettings(
        configured=settings.ENV.mail_configured,
        redirect_uri=google.redirect_uri(),
        account=_account(user_id),
        notice=MailNotice(ok=told.ok, message=told.message) if told else None,
    )
