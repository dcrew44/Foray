"""A user's connected mailbox, and the employers' replies found in it (#26).

`MailAccount` is the one mailbox a user connected, read-only: which address it is, the scopes
Google granted, and the tokens that read it. Both tokens are stored sealed (`crypto.seal`), never
in the clear. `status` is `reconnect` once Google stops honouring the refresh token (revoked, or
expired after seven days for an OAuth app still in "Testing"): nothing is asked of Google again
until the user connects again, and `last_error` is the sentence Settings shows.

`ApplicationEmail` is one message the check read, kept as little as the work needs:

- **`confirmation`, `rejection`, `interview`**: Gmail's id, when it arrived, the sender's
  display name and domain, the subject, and the first 300 characters of Gmail's snippet.
- **`other`**: Gmail's id and when it arrived, so it is not read again. Nothing it said: the
  database refuses anything else (`application_emails_other_is_blank`).

Never a body, an attachment, a recipient or the sender's full address. `match` is the match the
message is about, when one was found (`matching.match`). A rejection or an interview invitation
that was matched is a suggestion to move that match; `decision` records what the user said to
it, and only an accepted one ever moved a stage.
"""

from __future__ import annotations

import uuid

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import Q
from django.db.models.functions import Length
from django.db.models.lookups import LessThanOrEqual

from apps.accounts.models import User
from apps.core.choices import choices_checks
from apps.core.ids import new_id
from apps.core.models import Timestamped
from apps.core.scoping import UserOwned
from apps.matching.models import JobMatch

SNIPPET_MAX = 300


class Provider(models.TextChoices):
    GOOGLE = "google"


class AccountStatus(models.TextChoices):
    CONNECTED = "connected"
    RECONNECT = "reconnect"  # the stored token no longer works; only the user can fix that


class MailAccount(UserOwned, Timestamped):
    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    # One mailbox per user.
    user = models.OneToOneField(User, models.CASCADE, related_name="mail_account")
    provider = models.TextField(choices=Provider.choices, default=Provider.GOOGLE)
    address = models.TextField()  # the mailbox, as the provider names it
    scopes = ArrayField(models.TextField(), default=list)  # what the user granted
    status = models.TextField(choices=AccountStatus.choices, default=AccountStatus.CONNECTED)
    # Sealed with `crypto.seal`. The access token is "" when there is none to reuse.
    refresh_token = models.TextField()
    access_token = models.TextField(default="")
    access_expires_at = models.DateTimeField(null=True)
    last_checked_at = models.DateTimeField(null=True)  # the last check that reached the mailbox
    last_error = models.TextField(default="")  # a sentence for Settings; "" when all is well

    user_id: str

    class Meta(UserOwned.Meta):
        db_table = "mail_accounts"
        constraints = [
            *choices_checks("mail_accounts", provider=Provider, status=AccountStatus),
        ]

    def __str__(self) -> str:
        # Never the tokens, sealed or not.
        return f"{self.provider}: {self.address} ({self.status})"


class EmailKind(models.TextChoices):
    CONFIRMATION = "confirmation"  # "we received your application"
    REJECTION = "rejection"
    INTERVIEW = "interview"  # an invitation to talk
    OTHER = "other"  # none of these: only remembered as read


class Decision(models.TextChoices):
    ACCEPTED = "accepted"  # the user moved the match as suggested
    DISMISSED = "dismissed"


# The kinds that suggest a stage, and so the only ones a decision can be about.
SUGGESTING = (EmailKind.REJECTION, EmailKind.INTERVIEW)


class ApplicationEmail(UserOwned, Timestamped):
    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    # Indexed by (user, received_at).
    user = models.ForeignKey(
        User, models.CASCADE, related_name="application_emails", db_index=False
    )
    # Disconnecting deletes the account and, with it, every message read from it. Indexed by
    # the (account, message_id) constraint.
    account = models.ForeignKey(MailAccount, models.CASCADE, related_name="emails", db_index=False)
    message_id = models.TextField()  # the provider's id for the message
    received_at = models.DateTimeField()
    sender_name = models.TextField(default="")  # the display name, not the address
    sender_domain = models.TextField(default="")
    subject = models.TextField(default="")
    snippet = models.TextField(default="")
    kind = models.TextField(choices=EmailKind.choices)
    # The match this message is about. A match that is deleted leaves the message unmatched.
    match = models.ForeignKey(JobMatch, models.SET_NULL, null=True, related_name="emails")
    # What the user said to the suggestion; NULL while it is open (or was never one).
    decision = models.TextField(choices=Decision.choices, null=True)  # noqa: DJ001
    decided_at = models.DateTimeField(null=True)

    user_id: str
    account_id: uuid.UUID
    match_id: uuid.UUID | None

    class Meta(UserOwned.Meta):
        db_table = "application_emails"
        constraints = [
            models.UniqueConstraint(
                fields=["account", "message_id"], name="application_emails_once_per_message"
            ),
            # A message that is not about an application keeps nothing it said.
            models.CheckConstraint(
                condition=~Q(kind=EmailKind.OTHER)
                | Q(sender_name="", sender_domain="", subject="", snippet="", match__isnull=True),
                name="application_emails_other_is_blank",
            ),
            models.CheckConstraint(
                condition=Q(LessThanOrEqual(Length("snippet"), SNIPPET_MAX)),
                name="application_emails_snippet_short",
            ),
            # Only a suggestion is decided, and a decision has its time.
            models.CheckConstraint(
                condition=Q(decision__isnull=True, decided_at__isnull=True)
                | Q(decision__isnull=False, decided_at__isnull=False, kind__in=SUGGESTING),
                name="application_emails_decision_paired",
            ),
            *choices_checks("application_emails", kind=EmailKind, decision=Decision),
        ]
        indexes = [
            models.Index(fields=["user", "received_at"], name="application_emails_user_at"),
        ]

    def __str__(self) -> str:
        return f"{self.kind} from {self.sender_domain or 'unknown'} at {self.received_at}"
