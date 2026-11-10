"""What an application needs beyond a match (Phase 2; DESIGN.md, Applying).

`ApplicationAnswer` is an answer the user gave once to a question forms keep asking (#21):
whether they may work in the country, whether they need sponsorship, what pay they expect, the
voluntary disclosures. An answer about sponsorship, pay or years of experience comes only from
here, never from a model, and a voluntary disclosure with no saved answer is declined.

`Application` is one match's application, prepared ahead of the user (#60): the form's
questions as its board states them, the answer planned for each and where it came from, and the
resume it would send. It is `ready` when every required field has an answer and
`needs_answers` when one does not. Nothing here submits anything: the user's own browser fills
the form (#22) and the user presses submit.
"""

from __future__ import annotations

import uuid

from django.db import models
from django.db.models.functions import Now

from apps.accounts.models import User
from apps.core.choices import choices_checks
from apps.core.ids import new_id
from apps.core.models import Timestamped
from apps.core.scoping import UserOwned
from apps.matching.models import JobMatch
from apps.resumes.models import Document, ResumeVariant


class AnswerKind(models.TextChoices):
    """The questions application forms share. `custom` is any other question, kept under the
    question's own words."""

    WORK_AUTHORIZATION = "work_authorization"
    SPONSORSHIP = "sponsorship"
    CITIZENSHIP = "citizenship"
    CLEARANCE = "clearance"
    SALARY = "salary"
    START_DATE = "start_date"
    YEARS_EXPERIENCE = "years_experience"
    RELOCATION = "relocation"
    LOCATION = "location"
    PHONE = "phone"
    LINKEDIN = "linkedin"
    GITHUB = "github"
    WEBSITE = "website"
    HOW_HEARD = "how_heard"
    PREVIOUSLY_EMPLOYED = "previously_employed"
    OVER_18 = "over_18"
    PRONOUNS = "pronouns"
    GENDER = "gender"
    RACE = "race"
    HISPANIC = "hispanic"
    VETERAN = "veteran"
    DISABILITY = "disability"
    CUSTOM = "custom"


class ApplicationAnswer(UserOwned, Timestamped):
    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    # Indexed by the (user, kind, question_key) constraint.
    user = models.ForeignKey(
        User, models.CASCADE, related_name="application_answers", db_index=False
    )
    kind = models.TextField(choices=AnswerKind.choices)
    # Empty for a shared question; for a custom one, a digest of its normalised words
    # (`answers.question_key`), so the same question asked by another employer finds it.
    question_key = models.TextField(default="")
    question = models.TextField()  # the question as the user last saw it
    answer = models.TextField()

    user_id: str

    class Meta(UserOwned.Meta):
        db_table = "application_answers"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "kind", "question_key"], name="application_answers_question"
            ),
            *choices_checks("application_answers", kind=AnswerKind),
        ]

    def __str__(self) -> str:
        return f"{self.kind}: {self.question[:40]}"


class ApplicationStatus(models.TextChoices):
    PREPARING = "preparing"  # queued, or its form is being read
    NEEDS_ANSWERS = "needs_answers"  # a required question has no saved answer
    READY = "ready"  # every required field has its answer
    SUBMITTED = "submitted"  # the user sent it
    DISMISSED = "dismissed"  # the user decided not to apply
    FAILED = "failed"  # the form could not be read; `error` says why


class Application(UserOwned, Timestamped):
    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    # Indexed by the (user, status) index.
    user = models.ForeignKey(User, models.CASCADE, related_name="applications", db_index=False)
    # One application per match.
    match = models.OneToOneField(JobMatch, models.CASCADE, related_name="application")
    status = models.TextField(
        choices=ApplicationStatus.choices, default=ApplicationStatus.PREPARING
    )
    # Whose form it is (a `jobs.sources` name, or "" when Foray does not know the form), and
    # where the user applies.
    form_source = models.TextField(default="")
    apply_url = models.TextField(default="")
    # `forms.FormField` and `prepare.Planned` as JSON lists, in the form's own order. `questions`
    # is empty for a form Foray cannot read ahead; the browser extension reads it when it fills.
    questions = models.JSONField(default=list)
    answers = models.JSONField(default=list)
    # Which resume it sends (#61, `applications/resume.py`): the user's own choice ("" lets the
    # posting's title decide, "base" is the full resume, else a variant's id), the variant that
    # follows from it (null: the full resume) and the sentence saying why.
    resume_choice = models.TextField(default="")
    variant = models.ForeignKey(ResumeVariant, models.SET_NULL, null=True, related_name="+")
    resume_reason = models.TextField(default="")
    # The PDF that was attached, once the form was filled.
    document = models.ForeignKey(Document, models.SET_NULL, null=True, related_name="+")
    error = models.TextField(default="")
    # When each thing happened, for posted-to-applied time (#65). `prepared_at` is when the form
    # was read; `ready_at` when the last missing answer arrived.
    prepared_at = models.DateTimeField(null=True)
    ready_at = models.DateTimeField(null=True)
    opened_at = models.DateTimeField(null=True)
    submitted_at = models.DateTimeField(null=True)
    # Proof of arrival (#23), kept for good once the user's browser reports the form went in:
    # what was put in each field, the page that confirmed it and what it said, and a picture
    # of it (a key under STORAGE_DIR). `filled_at` is when the extension last filled the form.
    filled_at = models.DateTimeField(null=True)
    # The extension pressed submit itself, as the user's setting allowed (#40).
    auto_submitted = models.BooleanField(db_default=False)
    sent_answers = models.JSONField(null=True)
    confirmation_url = models.TextField(default="")
    confirmation_text = models.TextField(default="")
    confirmation_image = models.TextField(default="")
    # Proof of arrival (#23, #26): when the employer's own confirmation email arrived, and its
    # subject line. Written by the mail check (`mail/store.py`) and by nothing else; kept when
    # the mailbox is disconnected, because it is the application's record by then.
    confirmed_at = models.DateTimeField(null=True)
    confirmation_subject = models.TextField(default="")

    user_id: str
    match_id: uuid.UUID
    document_id: uuid.UUID | None
    variant_id: uuid.UUID | None

    class Meta(UserOwned.Meta):
        db_table = "applications"
        constraints = [*choices_checks("applications", status=ApplicationStatus)]
        indexes = [models.Index(fields=["user", "status"], name="applications_user_status")]

    def __str__(self) -> str:
        return f"{self.status}: {self.match_id}"


class ExtensionToken(UserOwned):
    """What the browser extension signs in with (#22): a secret the user makes in Settings and
    pastes into the extension once. Only its SHA-256 is kept, so a token can be shown exactly
    once and a copy of the database signs in nobody."""

    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    user = models.ForeignKey(User, models.CASCADE, related_name="extension_tokens")
    token_hash = models.CharField(max_length=64, unique=True)
    label = models.TextField(default="")
    created_at = models.DateTimeField(db_default=Now())
    last_used_at = models.DateTimeField(null=True)

    user_id: str

    class Meta(UserOwned.Meta):
        db_table = "extension_tokens"

    def __str__(self) -> str:
        return f"extension token {self.label or self.id}"
