"""What an application needs beyond a match (Phase 2; DESIGN.md, Applying).

`ApplicationAnswer` is an answer the user gave once to a question forms keep asking (#21):
whether they may work in the country, whether they need sponsorship, what pay they expect, the
voluntary disclosures. An answer about sponsorship, pay or years of experience comes only from
here, never from a model, and a voluntary disclosure with no saved answer is declined.
"""

from __future__ import annotations

from django.db import models

from apps.accounts.models import User
from apps.core.choices import choices_checks
from apps.core.ids import new_id
from apps.core.models import Timestamped
from apps.core.scoping import UserOwned


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
