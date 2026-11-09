"""`/api/answers` and `/api/applications` (#21, #60, #62).

`PUT /api/answers` saves any number of the user's answers to the questions forms share (the
Profile page's form); an empty answer forgets it. Every open application is then planned again.

`POST /api/applications` starts preparing a match's application (`202`: the form is read by the
worker). `POST /api/applications/{id}/answers` answers one question an application is missing
and saves it for next time. `PATCH /api/applications/{id}` is what the user does with one:
dismiss it, note that its form was opened, say it was sent, or bring it back. The applications
themselves are read with the pages that show them. Nothing here sends anything to an employer.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal, cast

from django.http import HttpRequest
from ninja import Status
from pydantic import Field

from apps.accounts.models import User
from apps.applications import actions, prepare
from apps.applications import answers as saved_answers
from apps.applications import tasks as _tasks  # noqa: F401 (registers the task this API defers)
from apps.applications.models import AnswerKind, Application
from apps.core.errors import ApiError, messages
from apps.core.router import ApiModel, router
from apps.core.schemas import OkMessageOut

answers = router(tags=["answers"])
applications = router(tags=["applications"])

ROUTERS = {"/answers": answers, "/applications": applications}

ANSWERS_INVALID = "Those answers could not be read."
ANSWERS_SAVED = "Answers saved"
MAX_AT_ONCE = 60


class AnswerIn(ApiModel):
    kind: AnswerKind
    answer: str = Field(max_length=saved_answers.MAX_ANSWER)
    # The question's own words: required for a custom answer, ignored for the others.
    question: str = Field(default="", max_length=saved_answers.MAX_QUESTION)


class AnswersIn(ApiModel):
    answers: list[AnswerIn] = Field(max_length=MAX_AT_ONCE)


@answers.put("", response=OkMessageOut, summary="Save answers")
@messages(invalid=ANSWERS_INVALID)
def put_answers(request: HttpRequest, body: AnswersIn) -> dict[str, Any]:
    user_id = cast(User, request.user).pk
    for item in body.answers:
        try:
            saved_answers.save(user_id, item.kind, item.answer, item.question)
        except ValueError:
            raise ApiError(400, ANSWERS_INVALID) from None
    ready = prepare.replan(user_id)
    message = ANSWERS_SAVED
    if ready:
        message += f". {ready} {'application is' if ready == 1 else 'applications are'} now ready"
    return {"ok": True, "message": message}


# --- /applications -------------------------------------------------------------------------------

APPLICATION_INVALID = "That request could not be read."
APPLICATION_GONE = "That application was not found."
MATCH_GONE = "That match was not found."
PREPARING = "Reading the application form"
NOT_NOW = "That application has moved on. Reload the page."
DONE = {
    "dismiss": "Application dismissed",
    "opened": "Opened",
    "submitted": "Marked as sent. The match moved to Applied.",
    "reopen": "Preparing it again",
}


class PrepareIn(ApiModel):
    match_id: uuid.UUID


class ApplicationStarted(ApiModel):
    ok: bool
    message: str
    application_id: uuid.UUID


class ApplicationPatch(ApiModel):
    action: Literal["dismiss", "opened", "submitted", "reopen"]


class QuestionAnswerIn(ApiModel):
    key: str = Field(min_length=1, max_length=200)
    value: str = Field(max_length=saved_answers.MAX_ANSWER)


@applications.post("", response={202: ApplicationStarted}, summary="Prepare an application")
@messages(invalid=APPLICATION_INVALID, not_found=MATCH_GONE)
def post_application(request: HttpRequest, body: PrepareIn) -> Status[dict[str, Any]]:
    application = prepare.start(cast(User, request.user).pk, body.match_id)
    if application is None:
        raise ApiError(404, MATCH_GONE)
    return Status(202, {"ok": True, "message": PREPARING, "application_id": application.id})


@applications.post("/{id}/answers", response=OkMessageOut, summary="Answer a question")
@messages(invalid=APPLICATION_INVALID, not_found=APPLICATION_GONE)
def post_answer(request: HttpRequest, id: uuid.UUID, body: QuestionAnswerIn) -> dict[str, Any]:
    try:
        ready = actions.answer(cast(User, request.user).pk, id, body.key, body.value)
    except actions.AnswerRefused as refused:
        raise ApiError(400, str(refused)) from None
    if ready is None:
        raise ApiError(404, APPLICATION_GONE)
    return {
        "ok": True,
        "message": "Saved. This application is ready." if ready else "Saved for next time too.",
    }


@applications.patch("/{id}", response=OkMessageOut, summary="Act on an application")
@messages(invalid=APPLICATION_INVALID, not_found=APPLICATION_GONE)
def patch_application(
    request: HttpRequest, id: uuid.UUID, body: ApplicationPatch
) -> dict[str, Any]:
    user_id = cast(User, request.user).pk
    if not Application.objects.for_user(user_id).filter(id=id).exists():
        raise ApiError(404, APPLICATION_GONE)
    done = {
        "dismiss": actions.dismiss,
        "opened": actions.mark_opened,
        "submitted": actions.mark_submitted,
        "reopen": actions.reopen,
    }[body.action](user_id, id)
    if not done and body.action != "opened":
        raise ApiError(409, NOT_NOW)
    return {"ok": True, "message": DONE[body.action]}
