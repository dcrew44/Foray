"""A mail check's database steps (#26). Sync; `check.py` runs them through `db()` between its
requests to Google, so no transaction ever spans one.

`open_account` → (requests) → `unseen` → (requests) → `record` → `finish`.

**What `record` does with a message**, in one transaction:

- It is classified (`classify.classify`) from its subject and snippet. `other` is stored as
  its id and date alone: enough not to read it again, and nothing it said.
- Anything else is matched to one of the user's sent applications (`matching.find`): the
  matches in `applied`, `interviewing` or `offer`, sent when their application was marked sent,
  or when the match entered its stage for one applied to by hand.
- **A confirmation is proof of arrival** (#23) and is recorded without anyone looking, so it
  is held to more: it has to be the employer's own word (`matching.is_employers_word`), and to
  arrive within a week of the application being sent. One that is neither stays unmatched. A
  matched one writes `confirmed_at` and `confirmation_subject` on the application, keeping the
  earliest. (A match applied to by hand has no application row; its confirmation is the
  message itself.)
- **A rejection or an invitation changes nothing.** Matched, it is a suggestion the match page
  puts to the user (`suggestions.py`).

A message that found no application is tried again at each check until it is two weeks old,
because the user often tells Foray an application was sent after the employer has already
answered it.

**Bounded.** One check stores at most `LIMIT` new messages; the rest wait for the next one,
which lists the mailbox again and skips what is stored.

**Forgotten.** A message matched to no application is deleted once it is `FORGET_AFTER` old,
whatever its kind: the search only looks two weeks back, so it cannot be listed again, and it
can no longer be matched. What stays is mail about the user's own applications.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import Q
from django.db.models.functions import Now

from apps.applications.models import Application
from apps.core.clock import database_now
from apps.mail import crypto, google
from apps.mail.classify import classify
from apps.mail.connect import EXPIRY_MARGIN
from apps.mail.matching import EARLY, Found, Mail, Sent, find, is_employers_word
from apps.mail.models import (
    SNIPPET_MAX,
    AccountStatus,
    ApplicationEmail,
    EmailKind,
    MailAccount,
)
from apps.matching.models import JobMatch, Stage

__all__ = [
    "AWAITING_AN_ANSWER",
    "FORGET_AFTER",
    "LIMIT",
    "PROOF_WINDOW",
    "RETRY_MATCHING_FOR",
    "UNREADABLE_TOKEN",
    "Access",
    "Incoming",
    "Recorded",
    "connected_accounts",
    "finish",
    "need_reconnect",
    "open_account",
    "record",
    "save_access_token",
    "sent_applications",
    "unseen",
]

LIMIT = 50  # new messages stored by one check
PROOF_WINDOW = timedelta(days=7)  # how long after sending a confirmation still counts
RETRY_MATCHING_FOR = timedelta(days=14)
FORGET_AFTER = timedelta(days=30)
SENDER_MAX = 200
SUBJECT_MAX = 300

# The stages in which an employer's answer is still expected.
AWAITING_AN_ANSWER = (Stage.APPLIED, Stage.INTERVIEWING, Stage.OFFER)

UNREADABLE_TOKEN = (
    "Foray can no longer open the access it stored for this mailbox (SECRET_KEY changed since "
    "it was connected). Connect it again."
)


@dataclass(frozen=True, slots=True)
class Access:
    """What a check needs of an account. The tokens stay out of `repr`."""

    account_id: uuid.UUID
    user_id: str
    refresh_token: str = field(repr=False)
    access_token: str | None = field(repr=False)  # None: get a new one first


@dataclass(frozen=True, slots=True)
class Incoming:
    """One message as `check.read_message` made it from Gmail's metadata."""

    message_id: str
    received_at: datetime
    sender_name: str
    sender_domain: str
    subject: str
    snippet: str


@dataclass(frozen=True, slots=True)
class Recorded:
    stored: int = 0
    matched: int = 0
    confirmed: int = 0  # applications that now have their proof of arrival


def _accounts(user_id: str, account_id: uuid.UUID):
    return MailAccount.objects.for_user(user_id).filter(id=account_id)


def connected_accounts() -> list[tuple[str, uuid.UUID]]:
    """`(user_id, account_id)` of every mailbox a check should read."""
    return list(
        MailAccount.objects.unscoped("the mail check reads every connected mailbox")
        .filter(status=AccountStatus.CONNECTED)
        .order_by("last_checked_at", "id")
        .values_list("user_id", "id")
    )


def account_of(user_id: str) -> uuid.UUID | None:
    return MailAccount.objects.for_user(user_id).values_list("id", flat=True).first()


def open_account(user_id: str, account_id: uuid.UUID) -> Access | None:
    """The account's tokens, opened, for one check. None when there is nothing to check: the
    account is gone or waiting to be reconnected, or (now marked so) its tokens cannot be
    opened with this install's key."""
    account = _accounts(user_id, account_id).filter(status=AccountStatus.CONNECTED).first()
    if account is None:
        return None
    try:
        refresh = crypto.unseal(account.refresh_token)
        usable = (
            account.access_token != ""
            and account.access_expires_at is not None
            and account.access_expires_at > database_now()
        )
        access = crypto.unseal(account.access_token) if usable else None
    except crypto.Unreadable:
        need_reconnect(user_id, account_id, UNREADABLE_TOKEN)
        return None
    return Access(
        account_id=account.id, user_id=user_id, refresh_token=refresh, access_token=access
    )


def save_access_token(user_id: str, account_id: uuid.UUID, tokens: google.Tokens) -> None:
    """Keep a refreshed access token (sealed) until shortly before it expires. Google may send
    a new refresh token with it; that one replaces the old."""
    fields: dict[str, object] = {
        "access_token": crypto.seal(tokens.access_token),
        "access_expires_at": database_now() + timedelta(seconds=tokens.expires_in) - EXPIRY_MARGIN,
        "updated_at": Now(),
    }
    if tokens.refresh_token:
        fields["refresh_token"] = crypto.seal(tokens.refresh_token)
    _accounts(user_id, account_id).update(**fields)


def need_reconnect(user_id: str, account_id: uuid.UUID, sentence: str) -> None:
    """Stop checking this mailbox until the user connects it again, and say why. The access
    token is dropped; the refresh token stays (sealed) so that disconnecting can still ask
    Google to forget it."""
    _accounts(user_id, account_id).update(
        status=AccountStatus.RECONNECT,
        last_error=sentence,
        access_token="",
        access_expires_at=None,
        updated_at=Now(),
    )


def finish(user_id: str, account_id: uuid.UUID, error: str = "") -> None:
    """The end of a check. One that reached the mailbox moves `last_checked_at` and clears the
    last error; one that failed only says why."""
    if error:
        _accounts(user_id, account_id).update(last_error=error, updated_at=Now())
        return
    _accounts(user_id, account_id).update(last_checked_at=Now(), last_error="", updated_at=Now())
    ApplicationEmail.objects.for_user(user_id).filter(
        account_id=account_id, match__isnull=True, received_at__lt=Now() - FORGET_AFTER
    ).delete()


def unseen(user_id: str, account_id: uuid.UUID, message_ids: Sequence[str]) -> list[str]:
    """The ids not stored yet, in the order given."""
    stored = set(
        ApplicationEmail.objects.for_user(user_id)
        .filter(account_id=account_id, message_id__in=list(message_ids))
        .values_list("message_id", flat=True)
    )
    return [message_id for message_id in dict.fromkeys(message_ids) if message_id not in stored]


# --- what a message is about ---------------------------------------------------------------------


def sent_applications(user_id: str) -> list[Sent]:
    """The applications an employer could still be answering: one query."""
    matches = (
        JobMatch.objects.for_user(user_id)
        .filter(stage__in=AWAITING_AN_ANSWER)
        .select_related("job__company", "application")
    )
    sent: list[Sent] = []
    for match in matches:
        try:
            # The reverse of `Application.match`, joined by the query above.
            application: Application | None = match.application  # pyright: ignore[reportAttributeAccessIssue]
        except Application.DoesNotExist:
            application = None
        sent_at = application.submitted_at if application is not None else None
        company = match.job.company
        sent.append(
            Sent(
                match_id=match.id,
                company=match.job.company_name,
                title=match.job.title,
                sent_at=sent_at or match.stage_changed_at,
                domain=company.domain if company is not None else None,
            )
        )
    return sent


def _about(kind: str, mail: Mail, sent: Sequence[Sent]) -> Found | None:
    """The application a classified message is about, by the rule for its kind."""
    found = find(mail, sent)
    if found is None or kind != EmailKind.CONFIRMATION:
        return found
    arrived_in_time = (
        found.sent.sent_at - EARLY <= mail.received_at <= found.sent.sent_at + PROOF_WINDOW
    )
    return found if arrived_in_time and is_employers_word(mail, found.evidence) else None


def _mail(row: ApplicationEmail | Incoming) -> Mail:
    return Mail(
        sender_name=row.sender_name,
        sender_domain=row.sender_domain,
        subject=row.subject,
        received_at=row.received_at,
    )


def _confirm(user_id: str, match_id: uuid.UUID, received_at: datetime, subject: str) -> bool:
    """Write an application's proof of arrival, keeping the earliest confirmation."""
    return bool(
        Application.objects.for_user(user_id)
        .filter(match_id=match_id)
        .filter(Q(confirmed_at__isnull=True) | Q(confirmed_at__gt=received_at))
        .update(confirmed_at=received_at, confirmation_subject=subject, updated_at=Now())
    )


def record(user_id: str, account_id: uuid.UUID, incoming: Sequence[Incoming]) -> Recorded:
    """Classify, match and store what a check read, and try the unmatched once more."""
    stored = matched = confirmed = 0
    with transaction.atomic():
        account = (
            _accounts(user_id, account_id)
            .select_for_update()
            .filter(status=AccountStatus.CONNECTED)
            .first()
        )
        if account is None:
            return Recorded()  # disconnected while the check was reading
        sent = sent_applications(user_id)

        waiting = ApplicationEmail.objects.for_user(user_id).filter(
            account_id=account_id,
            match__isnull=True,
            decision__isnull=True,
            received_at__gte=Now() - RETRY_MATCHING_FOR,
        )
        for row in waiting.exclude(kind=EmailKind.OTHER) if sent else ():
            found = _about(row.kind, _mail(row), sent)
            if found is None:
                continue
            ApplicationEmail.objects.for_user(user_id).filter(id=row.id).update(
                match_id=found.sent.match_id, updated_at=Now()
            )
            matched += 1
            if row.kind == EmailKind.CONFIRMATION:
                confirmed += _confirm(user_id, found.sent.match_id, row.received_at, row.subject)

        rows: list[ApplicationEmail] = []
        proofs: list[tuple[uuid.UUID, datetime, str]] = []
        for message in {item.message_id: item for item in incoming}.values():
            kind = classify(message.subject, message.snippet)
            if kind == EmailKind.OTHER:
                rows.append(
                    ApplicationEmail(
                        account_id=account_id,
                        message_id=message.message_id,
                        received_at=message.received_at,
                        kind=EmailKind.OTHER,
                    )
                )
                continue
            found = _about(kind, _mail(message), sent)
            subject = message.subject[:SUBJECT_MAX]
            rows.append(
                ApplicationEmail(
                    account_id=account_id,
                    message_id=message.message_id,
                    received_at=message.received_at,
                    sender_name=message.sender_name[:SENDER_MAX],
                    sender_domain=message.sender_domain[:SENDER_MAX],
                    subject=subject,
                    snippet=message.snippet[:SNIPPET_MAX],
                    kind=kind,
                    match_id=found.sent.match_id if found is not None else None,
                )
            )
            if found is not None:
                matched += 1
                if kind == EmailKind.CONFIRMATION:
                    proofs.append((found.sent.match_id, message.received_at, subject))
        # A message another check stored meanwhile is simply kept as it is.
        ApplicationEmail.objects.bulk_create_for(user_id, rows, ignore_conflicts=True)
        stored = len(rows)
        for match_id, received_at, subject in proofs:
            confirmed += _confirm(user_id, match_id, received_at, subject)
    return Recorded(stored=stored, matched=matched, confirmed=confirmed)
