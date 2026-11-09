"""What a user does with a prepared application (#60, #62): answer what it is missing, dismiss
it, open its form, say it was sent, or bring it back.

Every function is sync, takes the user's id, and answers None or False for an application that
is not theirs. A status changes only from the statuses that make sense, in one conditional
update, so two tabs cannot both act on one application.
"""

from __future__ import annotations

import uuid

from django.db import transaction
from django.db.models.functions import Now

from apps.applications import answers as saved_answers
from apps.applications import prepare
from apps.applications.forms import FormField
from apps.applications.models import AnswerKind, Application, ApplicationStatus
from apps.matching import queries as match_queries
from apps.matching.models import Stage

__all__ = ["AnswerRefused", "answer", "dismiss", "mark_opened", "mark_submitted", "reopen"]

S = ApplicationStatus
WAITING = (S.NEEDS_ANSWERS, S.READY)


class AnswerRefused(Exception):
    """An answer the question cannot take. Its text is a sentence for the user."""


def _mine(user_id: str, application_id: uuid.UUID | str) -> Application | None:
    return Application.objects.for_user(user_id).filter(id=application_id).first()


def answer(user_id: str, application_id: uuid.UUID | str, key: str, value: str) -> bool | None:
    """Answers one of an application's questions and saves the answer for next time (#21):
    under the question's own words always, and under the shared question it asks when the
    answer says what that question wants to know. Then every open application is planned
    again. Returns whether this application is now ready; None when it is not the user's."""
    application = _mine(user_id, application_id)
    if application is None:
        return None
    fields = [field for field in map(FormField.read, application.questions) if field]
    field = next((item for item in fields if item.key == key), None)
    if field is None:
        raise AnswerRefused("That question is no longer on this application.")
    value = " ".join(value.split()) if field.type != "textarea" else value.strip()
    if not value:
        raise AnswerRefused("Write an answer first.")
    if field.options and value not in field.options:
        raise AnswerRefused("Choose one of the form's own options.")
    if field.type == "file":
        raise AnswerRefused("Foray cannot attach that file yet. Add it on the form itself.")

    with transaction.atomic():
        saved_answers.save(user_id, AnswerKind.CUSTOM, value, field.label)
        kind = saved_answers.classify(field.label)
        shared = saved_answers.canonical(kind, value)
        if shared is not None and saved_answers.load(user_id).get(kind) is None:
            saved_answers.save(user_id, kind, shared)
        prepare.replan(user_id)
    status = (
        Application.objects.for_user(user_id)
        .filter(id=application.id)
        .values_list("status", flat=True)
        .first()
    )
    return status == S.READY


def dismiss(user_id: str, application_id: uuid.UUID | str) -> bool:
    return bool(
        Application.objects.for_user(user_id)
        .filter(id=application_id, status__in=[S.PREPARING, *WAITING, S.FAILED])
        .update(status=S.DISMISSED, updated_at=Now())
    )


def mark_opened(user_id: str, application_id: uuid.UUID | str) -> bool:
    """The user opened the form. Only the first time is kept."""
    return bool(
        Application.objects.for_user(user_id)
        .filter(id=application_id, status__in=WAITING, opened_at__isnull=True)
        .update(opened_at=Now(), updated_at=Now())
    )


def mark_submitted(user_id: str, application_id: uuid.UUID | str) -> bool:
    """The user sent it: the application is `submitted` and its match moves to Applied."""
    with transaction.atomic():
        application = _mine(user_id, application_id)
        if application is None:
            return False
        updated = (
            Application.objects.for_user(user_id)
            .filter(id=application.id, status__in=WAITING)
            .update(status=S.SUBMITTED, submitted_at=Now(), updated_at=Now())
        )
        if updated:
            match_queries.set_match_stage(user_id, application.match_id, Stage.APPLIED)
    return bool(updated)


def reopen(user_id: str, application_id: uuid.UUID | str) -> bool:
    """Prepares a dismissed or failed application again, or takes back "sent"."""
    application = _mine(user_id, application_id)
    if application is None:
        return False
    if application.status == S.SUBMITTED:
        with transaction.atomic():
            Application.objects.for_user(user_id).filter(
                id=application.id, status=S.SUBMITTED
            ).update(status=S.READY, submitted_at=None, updated_at=Now())
            prepare.replan(user_id)
        return True
    if application.status not in (S.DISMISSED, S.FAILED):
        return False
    Application.objects.for_user(user_id).filter(id=application.id).update(
        status=S.FAILED, updated_at=Now()
    )
    return prepare.start(user_id, application.match_id) is not None
