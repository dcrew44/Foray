"""A user's relationship to one job (design §2.2).

`fast_eval` and `deep_eval` hold `matching.schemas.FastScore` / `DeepEval` exactly; each score
is written together with its evaluation (`jm_fast_paired`, `jm_deep_paired`). `lane_override` is
the user's own choice of apply lane; the lane a match is in is `matching.lanes.effective_lane`.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Now

from apps.accounts.models import User
from apps.core.choices import choices_checks, pairing_checks
from apps.core.ids import new_id
from apps.core.models import Timestamped
from apps.core.scoping import UserOwned
from apps.jobs.models import Job

if TYPE_CHECKING:
    from apps.resumes.models import Resume


class Stage(models.TextChoices):
    FOUND = "found"
    SHORTLISTED = "shortlisted"
    APPLIED = "applied"
    INTERVIEWING = "interviewing"
    OFFER = "offer"
    REJECTED = "rejected"
    ARCHIVED = "archived"


class Lane(models.TextChoices):
    """How an application to a match is made (design "Apply lanes", #39)."""

    COACHED = "coached"  # Foray coaches; the user writes and sends
    AUTOMATED = "automated"  # Foray fills the form; the user presses submit
    NONE = "none"  # neither


class JobMatch(UserOwned, Timestamped):
    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    # Indexed by the (user, ...) constraints and indexes below.
    user = models.ForeignKey(User, models.CASCADE, related_name="job_matches", db_index=False)
    job = models.ForeignKey(Job, models.CASCADE, related_name="matches")
    stage = models.TextField(choices=Stage.choices, default=Stage.FOUND)
    stage_changed_at = models.DateTimeField(db_default=Now())
    # When the match first reached Applied, and Interviewing (#94): kept when it moves on to
    # Rejected or Archived, so an interview that ended in a rejection is still an interview,
    # and cleared when the user moves it back before that step (it was not applied to after
    # all). Written with the stage, by `queries.STAGE_TIMES`, and by nothing else.
    applied_at = models.DateTimeField(null=True)
    interviewing_at = models.DateTimeField(null=True)
    fast_score = models.SmallIntegerField(null=True)
    fast_eval = models.JSONField(null=True)  # FastScore
    deep_score = models.SmallIntegerField(null=True)
    deep_eval = models.JSONField(null=True)  # DeepEval
    user_rating = models.SmallIntegerField(null=True)
    # The lane the user put this match in; NULL follows the deep review (`matching/lanes.py`).
    # Nothing but the user's own change writes it, so a re-review never moves it.
    lane_override = models.TextField(choices=Lane.choices, null=True)  # noqa: DJ001
    notes = models.TextField(null=True)  # noqa: DJ001 (appended with blank-line separators)
    # The newest tailored resume for this match (was a soft uuid).
    tailored_resume: models.ForeignKey[Resume | None] = models.ForeignKey(
        "resumes.Resume", models.SET_NULL, null=True, related_name="+"
    )

    user_id: str
    job_id: str
    tailored_resume_id: uuid.UUID | None

    class Meta(UserOwned.Meta):
        db_table = "job_matches"
        constraints = [
            models.UniqueConstraint(fields=["user", "job"], name="job_matches_user_job"),
            models.CheckConstraint(
                condition=Q(fast_score__isnull=True) | Q(fast_score__range=(0, 100)),
                name="jm_fast_range",
            ),
            models.CheckConstraint(
                condition=Q(deep_score__isnull=True) | Q(deep_score__range=(0, 100)),
                name="jm_deep_range",
            ),
            *pairing_checks("fast"),
            *pairing_checks("deep"),
            models.CheckConstraint(
                condition=Q(user_rating__isnull=True) | Q(user_rating__range=(-10, 10)),
                name="jm_rating_range",
            ),
            *choices_checks("job_matches", stage=Stage, lane_override=Lane),
        ]
        indexes = [
            models.Index(fields=["user", "stage"], name="job_matches_user_stage"),
            models.Index(
                F("user"), F("fast_score").desc(nulls_last=True), name="job_matches_user_fast"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} / {self.job_id}: {self.stage}"
