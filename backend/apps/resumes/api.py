"""Resume and document endpoints (design §5.1, §5.3, §11.1, §11.8).

- `POST /api/uploads` (multipart `file`): 202 `{ok, runId, documentId, message: "Reading your
  resume…"}`, and a `profile_import` run reads it. 413 over 10 MB, 400 "No file uploaded.", 415
  for anything that is not a PDF or a Word file by its bytes, 429 past `upload:<user>`.
- `GET /api/documents/{id}[?download=1]`: the stored bytes, `private, no-store`, `nosniff`, and
  an RFC 6266 `Content-Disposition` (inline, or attachment with `download=1`).
- `PUT /api/resume`: 200 `{ok, message: "Profile saved"}`, a new base version unless nothing
  changed. A 400 names the first bad field ("Experience item 2: the start date is too long (40
  characters at most).").
- `POST /api/resume/render`: 202 `{ok, runId, message}`, a `resume_render` run for the newest
  base version.
- `POST /api/resumes/tailor {matchId}`: 202 `{ok, runId, matchId, message}`, a `tailor` run (a
  match id or a job id, as today). 400 "No job was given to tailor for.", 404 "I couldn't find
  that job.", 422 without a resume, 429 past `tailor:<user>`.
- `GET /api/resumes/{id}/preview`: the resume as HTML with today's CSP and headers; 404, or 422
  "This resume could not be read.".

The upload is the one operation that reads its own body: it counts against `upload:<user>`
before Django parses the multipart stream, so an oversized file still counts (the order of
§11.1). Its body is documented through `openapi_extra`. Ids are UUIDs; a malformed id or another
user's row answers 404, indistinguishable from a missing one. The sentences are today's
(`map/http-surface.md` §3.5, §3.8, §3.11, §4.10, §4.12, §4.13).
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Sequence
from typing import Any, Literal, cast
from urllib.parse import quote

from django.core.exceptions import RequestDataTooBig, SuspiciousOperation
from django.core.files.uploadedfile import UploadedFile
from django.http import HttpRequest, HttpResponse
from django.http.multipartparser import MultiPartParserError
from ninja import Status
from pydantic import StrictStr

from apps.accounts.models import User
from apps.core import throttle
from apps.core.errors import DEFAULT_INVALID, DEFAULT_NOT_FOUND, ApiError, messages
from apps.core.router import ApiModel, router
from apps.core.storage import StorageError, storage
from apps.resumes import pipeline
from apps.resumes.edits import save_user_edit
from apps.resumes.models import Document
from apps.resumes.models import Resume as ResumeRow
from apps.resumes.parse.sniff import UploadRejected, sniff
from apps.resumes.queries import latest_base_resume_row
from apps.resumes.schema import Resume
from apps.resumes.template import render_resume_html
from apps.resumes.text import js_trim

log = logging.getLogger("foray.resumes")

uploads = router(tags=["uploads"])
documents = router(tags=["documents"])
resume = router(tags=["resume"])
resumes = router(tags=["resumes"])

ROUTERS = {"/uploads": uploads, "/documents": documents, "/resume": resume, "/resumes": resumes}

NO_FILE = "No file uploaded."
FILE_MISSING = "The file is missing from storage."
UNREADABLE_RESUME = "This resume could not be read."
UPLOAD_FIRST = "Upload a resume before editing your profile."
PROFILE_SAVED = "Profile saved"
RESUME_INVALID = "That resume could not be saved. Check the fields and try again."
NO_JOB_GIVEN = "No job was given to tailor for."

# The preview is user- and model-written text served from the app's origin, so the CSP allows
# nothing but inline styles: no scripts, no network, no forms, no framing by other sites. The
# template escapes everything; this is the second lock.
PREVIEW_CSP = "; ".join(
    [
        "default-src 'none'",
        "style-src 'unsafe-inline'",
        "img-src data:",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'self'",
    ]
)


def _user_id(request: HttpRequest) -> str:
    return cast(User, request.user).pk


# --- schemas -------------------------------------------------------------------------------------


class UploadAcceptedOut(ApiModel):
    ok: bool
    run_id: uuid.UUID
    document_id: uuid.UUID
    message: str


class RunStartedOut(ApiModel):
    ok: bool
    run_id: uuid.UUID
    message: str


class TailorStartedOut(ApiModel):
    ok: bool
    run_id: uuid.UUID
    match_id: uuid.UUID
    message: str


class ResumeSavedOut(ApiModel):
    ok: bool
    message: str


class TailorIn(ApiModel):
    match_id: StrictStr  # a match id or a job id


_UPLOAD_BODY: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "properties": {"file": {"type": "string", "format": "binary"}},
                    "required": ["file"],
                }
            }
        },
    }
}


def _file_response(content_type: str, description: str) -> dict[str, Any]:
    return {
        "responses": {
            "200": {
                "description": description,
                "content": {content_type: {"schema": {"type": "string", "format": "binary"}}},
            }
        }
    }


def _started(started: pipeline.Started) -> dict[str, Any]:
    if not started.ok:
        raise ApiError(422 if started.refusal == "no_resume" else 404, started.message)
    out: dict[str, Any] = {"ok": True, "run_id": started.run_id, "message": started.message}
    if started.match_id is not None:
        out["match_id"] = started.match_id
    return out


# --- /uploads ------------------------------------------------------------------------------------


@uploads.post(
    "",
    response={202: UploadAcceptedOut},
    summary="Upload a resume",
    openapi_extra=_UPLOAD_BODY,
)
def upload(request: HttpRequest) -> Status[dict[str, Any]]:
    """Stores the file (if it is new) and starts reading it; the run does the reading (§11.1)."""
    user_id = _user_id(request)
    throttle.throttle(f"upload:{user_id}", throttle.UPLOAD.limit, throttle.UPLOAD.window_s)
    try:
        files = request.FILES  # parsed here: MaxSizeUploadHandler refuses > 10 MB with 413
    except UploadRejected as error:
        raise ApiError(error.status, error.message) from None
    except MultiPartParserError, RequestDataTooBig, SuspiciousOperation:
        raise ApiError(400, DEFAULT_INVALID) from None
    file = files.get("file")
    if not isinstance(file, UploadedFile):
        raise ApiError(400, NO_FILE)
    data = file.read()
    try:
        kind = sniff(data)
    except UploadRejected as error:
        raise ApiError(error.status, error.message) from None
    filename = file.name or f"resume.{kind}"
    accepted = pipeline.accept_upload(user_id, data, filename, kind)
    return Status(
        202,
        {
            "ok": True,
            "run_id": accepted.run_id,
            "document_id": accepted.document_id,
            "message": pipeline.READING,
        },
    )


# --- /documents ----------------------------------------------------------------------------------


def _js_units(char: str) -> int:
    """How many UTF-16 code units JavaScript counts for a character."""
    return 2 if ord(char) > 0xFFFF else 1


_NOT_PRINTABLE_ASCII = re.compile(r"[^\x20-\x7e]")
_QUOTE_OR_BACKSLASH = re.compile(r'["\\]')


def content_disposition(kind: Literal["inline", "attachment"], filename: str) -> str:
    """RFC 6266, as today: a plain-ASCII `filename` for old clients plus a UTF-8 `filename*`, so
    "Zoë Müller - Resume.pdf" survives the trip. JavaScript replaced each UTF-16 code unit, so an
    astral character becomes two underscores; `encodeURIComponent` plus `'()*` leaves only
    letters, digits and `-_.!~` unescaped."""
    ascii_name = _NOT_PRINTABLE_ASCII.sub(lambda m: "_" * _js_units(m.group()), filename)
    ascii_name = _QUOTE_OR_BACKSLASH.sub("_", ascii_name)
    encoded = quote(filename, safe="!~", encoding="utf-8", errors="replace")
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"


def _read(key: str) -> bytes:
    with storage().open(key) as handle:
        return handle.read()


@documents.get(
    "/{id}",
    summary="A stored file",
    openapi_extra=_file_response("application/octet-stream", "The file's bytes"),
)
def get_document(request: HttpRequest, id: uuid.UUID, download: str | None = None) -> HttpResponse:
    """An uploaded resume or a rendered PDF, for its owner only. Inline by default, so a PDF opens
    in the browser's viewer; `?download=1` makes it an attachment. Never cached."""
    document = Document.objects.for_user(_user_id(request)).filter(id=id).first()
    if document is None:
        raise ApiError(404, DEFAULT_NOT_FOUND)
    try:
        data = _read(document.storage_path)
    except OSError, StorageError:
        log.exception("document %s: the stored file is unreadable", document.id)
        raise ApiError(404, FILE_MISSING) from None
    response = HttpResponse(data, content_type=document.mime)
    response["Content-Length"] = str(len(data))
    response["Content-Disposition"] = content_disposition(
        "attachment" if download == "1" else "inline", document.filename
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


# --- /resume -------------------------------------------------------------------------------------

_SECTIONS = {"work": "Experience item", "education": "Education item", "projects": "Project"}
_FIELDS = {
    "position": "title",
    "company": "company",
    "location": "location",
    "startDate": "start date",
    "start_date": "start date",
    "endDate": "end date",
    "end_date": "end date",
    "summary": "summary",
    "highlights": "highlights",
    "institution": "school",
    "area": "field of study",
    "studyType": "degree",
    "study_type": "degree",
    "score": "grade",
    "name": "name",
    "description": "description",
    "url": "link",
    "label": "label",
    "category": "category",
    "headline": "headline",
    "email": "email",
    "phone": "phone",
    "id": "id",
    "source": "source",
    "links": "links",
}


def _problem(error: dict[str, Any], field: str) -> str:
    kind = str(error.get("type", ""))
    context = error.get("ctx") or {}
    match kind:
        case "string_too_long":
            return f"the {field} is too long ({context.get('max_length')} characters at most)"
        case "string_too_short":
            return f"the {field} is empty"
        case "missing":
            return f"the {field} is missing"
        case "string_type":
            return f"the {field} should be text"
        case "list_type":
            return f"the {field} should be a list"
        case "literal_error" | "enum":
            return f"the {field} is not one of the options"
        case _:
            return f"the {field} is not valid"


def resume_invalid(errors: Sequence[dict[str, Any]]) -> str:
    """The first failing field, in words: `"<Where>: <problem>."` (§5.2), never pydantic's."""
    if not errors:
        return RESUME_INVALID
    error = errors[0]
    loc = tuple(error.get("loc", ()))
    if loc[:1] == ("body",):
        loc = loc[2:]  # ninja prefixes ("body", <param>)
    if not loc:
        return RESUME_INVALID
    section, rest = loc[0], loc[1:]
    if section in _SECTIONS or section == "skills":
        if not rest or not isinstance(rest[0], int):
            return f"{section.capitalize()}: the list is not valid."
        label = _SECTIONS.get(section, "Skill")
        where, rest = f"{label} {rest[0] + 1}", rest[1:]
    elif section == "basics":
        if rest[:1] == ("links",) and len(rest) > 1 and isinstance(rest[1], int):
            where, rest = f"Link {rest[1] + 1}", rest[2:]
        else:
            where = "Basics"
    elif section == "summary":
        where = "Summary"
    else:
        return RESUME_INVALID
    names = [part for part in rest if isinstance(part, str)]
    if not names:
        return f"{where}: the entry is not valid."
    field = _FIELDS.get(names[0], names[0])
    return f"{where}: {_problem(error, field)}."


@resume.put("", response=ResumeSavedOut, summary="Save the resume editor")
@messages(invalid=resume_invalid)
def put_resume(request: HttpRequest, body: Resume) -> dict[str, Any]:
    """Validated, then diffed against the newest base version so only the items the user really
    changed become `source: "user"` (§11.3); a save that changes nothing writes nothing."""
    user_id = _user_id(request)
    if latest_base_resume_row(user_id) is None:
        raise ApiError(422, UPLOAD_FIRST)
    save_user_edit(user_id, body)
    return {"ok": True, "message": PROFILE_SAVED}


@resume.post("/render", response={202: RunStartedOut}, summary="Render the resume to PDF")
def render_resume(request: HttpRequest) -> Status[dict[str, Any]]:
    return Status(202, _started(pipeline.render_base_resume(_user_id(request))))


# --- /resumes ------------------------------------------------------------------------------------


@resumes.post("/tailor", response={202: TailorStartedOut}, summary="Tailor the resume for a job")
@messages(invalid=NO_JOB_GIVEN, not_found=pipeline.JOB_NOT_FOUND)
def tailor_resume(request: HttpRequest, body: TailorIn) -> Status[dict[str, Any]]:
    """`body.matchId` is a match id or a job id, as today; a job the user has no match for yet
    gets one."""
    if not js_trim(body.match_id):
        raise ApiError(400, NO_JOB_GIVEN)
    return Status(202, _started(pipeline.start_tailor_run(_user_id(request), body.match_id)))


@resumes.get(
    "/{id}/preview",
    summary="A resume as HTML",
    openapi_extra=_file_response("text/html", "The resume, rendered with the PDF template"),
)
def preview_resume(request: HttpRequest, id: uuid.UUID) -> HttpResponse:
    """The same template the PDF is printed from, so what the user sees is what they will send
    (§11.8)."""
    row = ResumeRow.objects.for_user(_user_id(request)).filter(id=id).first()
    if row is None:
        raise ApiError(404, DEFAULT_NOT_FOUND)
    parsed = Resume.read(row.data)
    if parsed is None:
        raise ApiError(422, UNREADABLE_RESUME)
    response = HttpResponse(render_resume_html(parsed), content_type="text/html; charset=utf-8")
    response["Content-Security-Policy"] = PREVIEW_CSP
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["Referrer-Policy"] = "no-referrer"
    return response
