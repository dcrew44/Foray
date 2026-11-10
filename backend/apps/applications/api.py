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

from django.http import HttpRequest, HttpResponse
from ninja import Status
from ninja.security import HttpBearer
from pydantic import Field

from apps.accounts.models import User
from apps.applications import actions, extension, prepare, resume
from apps.applications import answers as saved_answers
from apps.applications import tasks as _tasks  # noqa: F401 (registers the task this API defers)
from apps.applications.models import AnswerKind, Application, ExtensionToken
from apps.core.errors import ApiError, messages
from apps.core.router import ApiModel, router
from apps.core.schemas import OkMessageOut
from apps.core.storage import StorageError, storage
from apps.resumes.api import content_disposition

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


@applications.get(
    "/{id}/proof",
    summary="The confirming page, as it looked",
    openapi_extra={"responses": {200: {"description": "The picture's bytes"}}},
)
@messages(not_found=APPLICATION_GONE)
def get_proof(request: HttpRequest, id: uuid.UUID) -> HttpResponse:
    """The picture the extension took of the employer's confirmation (#23), for its owner."""
    application = Application.objects.for_user(cast(User, request.user).pk).filter(id=id).first()
    if application is None or not application.confirmation_image:
        raise ApiError(404, APPLICATION_GONE)
    try:
        with storage().open(application.confirmation_image) as handle:
            data = handle.read()
    except OSError, StorageError:
        raise ApiError(404, APPLICATION_GONE) from None
    kind = "image/jpeg" if application.confirmation_image.endswith(".jpg") else "image/png"
    response = HttpResponse(data, content_type=kind)
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


class ResumeChoiceIn(ApiModel):
    # A variant's id, "base" for the full resume, or "" to let the posting's title decide.
    choice: str = Field(max_length=64)


@applications.put("/{id}/resume", response=OkMessageOut, summary="Choose the resume it sends")
@messages(invalid=APPLICATION_INVALID, not_found=APPLICATION_GONE)
def put_resume_choice(request: HttpRequest, id: uuid.UUID, body: ResumeChoiceIn) -> dict[str, Any]:
    if not resume.set_choice(cast(User, request.user).pk, id, body.choice):
        raise ApiError(404, APPLICATION_GONE)
    return {"ok": True, "message": "This application sends that resume now."}


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


# --- /extension-tokens (the user, in Settings) ---------------------------------------------------

tokens = router(tags=["extension"])
ROUTERS["/extension-tokens"] = tokens

TOKEN_INVALID = "That request could not be read."
TOKEN_GONE = "That token was not found."
TOO_MANY_TOKENS = (
    f"You already have {extension.MAX_TOKENS} extension tokens. Remove one you no longer use."
)


class TokenIn(ApiModel):
    label: str = Field(default="", max_length=80)


class TokenCreated(ApiModel):
    ok: bool
    message: str
    id: uuid.UUID
    token: str  # shown once; only its hash is kept


@tokens.post("", response=TokenCreated, summary="Make an extension token")
@messages(invalid=TOKEN_INVALID)
def post_token(request: HttpRequest, body: TokenIn) -> dict[str, Any]:
    made = extension.new_token(cast(User, request.user).pk, body.label)
    if made is None:
        raise ApiError(409, TOO_MANY_TOKENS)
    row, secret = made
    return {
        "ok": True,
        "message": "Copy this token into the extension now. It is not shown again.",
        "id": row.id,
        "token": secret,
    }


@tokens.delete("/{id}", response=OkMessageOut, summary="Remove an extension token")
@messages(not_found=TOKEN_GONE)
def delete_token(request: HttpRequest, id: uuid.UUID) -> dict[str, Any]:
    removed, _ = ExtensionToken.objects.for_user(cast(User, request.user).pk).filter(id=id).delete()
    if not removed:
        raise ApiError(404, TOKEN_GONE)
    return {"ok": True, "message": "Token removed. An extension using it is signed out."}


# --- /extension (the browser extension, with its token) ------------------------------------------


class ExtensionAuth(HttpBearer):
    """The extension's only way in: a token made in Settings. No session is accepted here."""

    def authenticate(self, request: HttpRequest, token: str) -> User | None:
        user = extension.authenticate(token)
        if user is not None:
            request.user = user  # pyright: ignore[reportAttributeAccessIssue]
        return user


ext = router(tags=["extension"], auth=ExtensionAuth())
ROUTERS["/extension"] = ext

EXT_INVALID = "That request could not be read."
NO_RESUME_PDF = "There is no resume PDF yet. Make one on the Profile page."


class FillField(ApiModel):
    key: str
    label: str
    type: str
    required: bool
    options: list[str]
    kind: str
    value: str | None
    source: Literal["profile", "file", "saved", "declined", "skipped", "missing"]


class FillResume(ApiModel):
    filename: str
    size_bytes: int


class FillPayload(ApiModel):
    """One application as the extension fills it (`extension.payload`)."""

    id: uuid.UUID
    status: str
    company: str
    title: str
    apply_url: str
    form_source: str
    form_read: bool
    fields: list[FillField]
    missing: list[str]
    resume: FillResume | None
    blocked: str | None  # why Foray will not fill this one; null when it will
    may_submit: bool


class ExtensionMe(ApiModel):
    ok: bool
    name: str
    waiting: int  # applications ready or waiting on answers


class FoundOut(ApiModel):
    application: FillPayload | None


class PageFieldIn(ApiModel):
    key: str = Field(min_length=1, max_length=200)
    label: str = Field(max_length=2000)
    type: str = Field(default="text", max_length=40)
    required: bool = False
    options: list[str] = Field(default_factory=list, max_length=300)


class PlanIn(ApiModel):
    fields: list[PageFieldIn] = Field(max_length=300)


class SentField(ApiModel):
    key: str = Field(max_length=200)
    label: str = Field(default="", max_length=2000)
    value: str = Field(default="", max_length=20000)


class FilledIn(ApiModel):
    filled: list[SentField] = Field(default_factory=list, max_length=300)


class SubmittedIn(ApiModel):
    url: str = Field(default="", max_length=4000)
    text: str = Field(default="", max_length=40000)
    # A PNG or JPEG data URL of the confirming page, at most a few megabytes.
    screenshot: str = Field(default="", max_length=6_000_000)
    filled: list[SentField] | None = Field(default=None, max_length=300)
    # The extension pressed submit itself. Believed only when the rule allowed it.
    auto: bool = False


def _application(request: HttpRequest, id: uuid.UUID) -> tuple[User, Application]:
    user = cast(User, request.user)
    application = (
        Application.objects.for_user(user.pk).filter(id=id).select_related("match__job").first()
    )
    if application is None:
        raise ApiError(404, APPLICATION_GONE)
    return user, application


@ext.get("/me", response=ExtensionMe, summary="Who the token belongs to")
def ext_me(request: HttpRequest) -> dict[str, Any]:
    user = cast(User, request.user)
    waiting = Application.objects.for_user(user.pk).filter(status__in=extension.FILLABLE).count()
    return {"ok": True, "name": user.name or user.email, "waiting": waiting}


@ext.get("/applications", response=FoundOut, summary="The application for a page")
def ext_find(request: HttpRequest, url: str) -> dict[str, Any]:
    user = cast(User, request.user)
    found = extension.find_for_url(user.pk, url)
    return {"application": extension.payload(user, found) if found is not None else None}


@ext.get("/applications/{id}", response=FillPayload, summary="An application to fill")
@messages(not_found=APPLICATION_GONE)
def ext_application(request: HttpRequest, id: uuid.UUID) -> dict[str, Any]:
    user, application = _application(request, id)
    return extension.payload(user, application)


@ext.get(
    "/applications/{id}/resume",
    summary="The resume an application attaches",
    openapi_extra={"responses": {200: {"description": "The PDF's bytes"}}},
)
@messages(not_found=APPLICATION_GONE)
def ext_resume(request: HttpRequest, id: uuid.UUID) -> HttpResponse:
    user, application = _application(request, id)
    document = extension.resume_document(user.pk, application)
    if document is None:
        raise ApiError(404, NO_RESUME_PDF)
    try:
        with storage().open(document.storage_path) as handle:
            data = handle.read()
    except OSError, StorageError:
        raise ApiError(404, NO_RESUME_PDF) from None
    response = HttpResponse(data, content_type=document.mime)
    response["Content-Disposition"] = content_disposition("attachment", document.filename)
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@ext.post("/applications/{id}/plan", response=FillPayload, summary="Plan fields read from a page")
@messages(invalid=EXT_INVALID, not_found=APPLICATION_GONE)
def ext_plan(request: HttpRequest, id: uuid.UUID, body: PlanIn) -> dict[str, Any]:
    user, application = _application(request, id)
    fields = [
        extension.PageField(f.key, f.label, f.type, f.required, tuple(f.options))
        for f in body.fields
    ]
    return extension.plan_fields(user, application, fields)


@ext.post("/applications/{id}/filled", response=OkMessageOut, summary="The form was filled")
@messages(invalid=EXT_INVALID, not_found=APPLICATION_GONE)
def ext_filled(request: HttpRequest, id: uuid.UUID, body: FilledIn) -> dict[str, Any]:
    user, application = _application(request, id)
    extension.record_filled(user.pk, application.id, [item.model_dump() for item in body.filled])
    return {"ok": True, "message": "Noted"}


@ext.post("/applications/{id}/submitted", response=OkMessageOut, summary="The form was sent")
@messages(invalid=EXT_INVALID, not_found=APPLICATION_GONE)
def ext_submitted(request: HttpRequest, id: uuid.UUID, body: SubmittedIn) -> dict[str, Any]:
    user, application = _application(request, id)
    recorded = extension.record_submitted(
        user.pk,
        application.id,
        url=body.url,
        text=body.text,
        screenshot=body.screenshot,
        filled=None if body.filled is None else [item.model_dump() for item in body.filled],
        auto=body.auto,
    )
    return {
        "ok": True,
        "message": "Recorded. The match moved to Applied." if recorded else "Already recorded",
    }
