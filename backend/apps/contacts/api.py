"""`/api/contacts` (#63) and `/api/outreach` (#25): the people a user knows, from files they
exported themself, and the notes they draft to them.

- `POST /api/contacts/import` (multipart `file`): reads LinkedIn's Connections.csv (or the
  archive it comes in) or any CSV with a name column, and answers `200 {ok, message, added,
  updated, unchanged, skipped, total, matched}`. Importing the same file again answers with
  `added: 0, updated: 0` and writes nothing. 413 over the upload limit or 50,000 rows, 415 for
  a file that is not a CSV, 400 for one that lists nobody, 429 past `contacts-import:<user>`.
  The import is all the request does: no job is queued and nothing is fetched.
- `DELETE /api/contacts`: removes every contact of the user's.
- `DELETE /api/contacts/{id}`: removes one; another user's id is the same 404 as a missing one.
- `POST /api/contacts/lookup`: `202 {ok, runId, message, names}`, a `company_discovery` run
  that looks for the job boards of the next unmatched companies the contacts name
  (`page.start_lookup`). 400 when there is nothing left to look up.

- `POST /api/outreach {matchId, contactId}`: `202 {ok, runId, message}`, an `outreach` run
  that drafts a short note to that contact about that match (`outreach.start_note`); 200
  with the same body when that note is already being drafted. 404 for a match or contact
  that is not the user's, 400 when the contact does not work at the posting's company, 422
  without a resume, 429 past `outreach:<user>`.
- `PATCH /api/outreach/{id} {body}`: saves the user's own wording over a note.
- `DELETE /api/outreach/{id}`: discards a note.

Nothing here sends a note anywhere: the user copies it and sends it themself. The contacts
are read with the Contacts page (`GET /api/pages/contacts`) and a match's notes with its
page (`GET /api/pages/matches/{id}`). No endpoint here or anywhere returns another user's.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from django.core.exceptions import RequestDataTooBig, SuspiciousOperation
from django.core.files.uploadedfile import UploadedFile
from django.http import HttpRequest
from django.http.multipartparser import MultiPartParserError
from ninja import Status
from pydantic import Field

from apps.accounts.models import User
from apps.contacts import importer, outreach, page
from apps.contacts import tasks as _tasks  # noqa: F401 (registers the task this API defers)
from apps.contacts.parse import Unreadable
from apps.core import throttle
from apps.core.errors import DEFAULT_INVALID, ApiError, messages
from apps.core.router import ApiModel, router
from apps.core.schemas import OkMessageOut
from apps.resumes.parse.sniff import UploadRejected

contacts = router(tags=["contacts"])

notes = router(tags=["outreach"])

ROUTERS = {"/contacts": contacts, "/outreach": notes}

NO_FILE = "No file uploaded."
CONTACT_GONE = "That contact was not found."
NONE_TO_REMOVE = "There were no contacts to remove."


def _user_id(request: HttpRequest) -> str:
    return cast(User, request.user).pk


class ContactsImported(ApiModel):
    ok: bool
    message: str
    added: int
    updated: int
    unchanged: int
    skipped: int  # rows that named nobody
    total: int  # the user's contacts now
    matched: int  # of those, at a company in the mirror


class ContactsLookupStarted(ApiModel):
    ok: bool
    run_id: uuid.UUID
    message: str
    names: list[str]  # the company names this lookup asks about


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


@contacts.post(
    "/import", response=ContactsImported, summary="Import contacts", openapi_extra=_UPLOAD_BODY
)
def import_file(request: HttpRequest) -> dict[str, Any]:
    """Reads the uploaded list into the user's contacts. Like the resume upload, it reads its
    own body, so it counts against its throttle before Django parses the multipart stream."""
    user_id = _user_id(request)
    limit = throttle.CONTACTS_IMPORT
    throttle.throttle(f"contacts-import:{user_id}", limit.limit, limit.window_s)
    try:
        files = request.FILES  # parsed here: MaxSizeUploadHandler refuses > 10 MB with 413
    except UploadRejected as error:
        raise ApiError(error.status, error.message) from None
    except MultiPartParserError, RequestDataTooBig, SuspiciousOperation:
        raise ApiError(400, DEFAULT_INVALID) from None
    file = files.get("file")
    if not isinstance(file, UploadedFile):
        raise ApiError(400, NO_FILE)
    try:
        result = importer.import_contacts(user_id, file.read())
    except Unreadable as error:
        raise ApiError(error.status, error.message) from None
    return {
        "ok": True,
        "message": importer.import_message(result),
        "added": result.added,
        "updated": result.updated,
        "unchanged": result.unchanged,
        "skipped": result.skipped,
        "total": result.total,
        "matched": result.matched,
    }


@contacts.post("/lookup", response={202: ContactsLookupStarted}, summary="Look for boards")
def lookup(request: HttpRequest) -> Status[dict[str, Any]]:
    started, names = page.start_lookup(_user_id(request))
    count = "1 company" if len(names) == 1 else f"{len(names)} companies"
    return Status(
        202,
        {
            "ok": True,
            "run_id": started.run.id,
            "message": f"Looking for the job boards of {count} your contacts work at.",
            "names": names,
        },
    )


@contacts.delete("", response=OkMessageOut, summary="Remove every contact")
def delete_all(request: HttpRequest) -> dict[str, Any]:
    removed = importer.delete_all(_user_id(request))
    if removed == 0:
        return {"ok": True, "message": NONE_TO_REMOVE}
    people = "1 contact" if removed == 1 else f"{removed:,} contacts"
    return {"ok": True, "message": f"Removed {people}."}


@contacts.delete("/{id}", response=OkMessageOut, summary="Remove a contact")
@messages(not_found=CONTACT_GONE)
def delete_one(request: HttpRequest, id: uuid.UUID) -> dict[str, Any]:
    if not importer.delete_one(_user_id(request), id):
        raise ApiError(404, CONTACT_GONE)
    return {"ok": True, "message": "Contact removed."}


# --- /outreach -----------------------------------------------------------------------------------

NOTE_INVALID = (
    "That note could not be saved. Write something first, and keep it under 5,000 characters."
)


class OutreachIn(ApiModel):
    match_id: uuid.UUID
    contact_id: uuid.UUID


class OutreachStarted(ApiModel):
    ok: bool
    run_id: uuid.UUID
    message: str


class OutreachEdit(ApiModel):
    body: str = Field(min_length=1, max_length=outreach.BODY_MAX)


@notes.post(
    "", response={200: OutreachStarted, 202: OutreachStarted}, summary="Draft a note to a contact"
)
def post_note(request: HttpRequest, body: OutreachIn) -> Status[dict[str, Any]]:
    """Queues the draft and answers at once: a request never waits on a model."""
    started = outreach.start_note(_user_id(request), body.match_id, body.contact_id)
    return Status(
        200 if started.reused else 202,
        {
            "ok": True,
            "run_id": started.run.id,
            "message": outreach.ALREADY if started.reused else outreach.STARTED,
        },
    )


@notes.patch("/{id}", response=OkMessageOut, summary="Edit a note")
@messages(invalid=NOTE_INVALID, not_found=outreach.NOTE_GONE)
def patch_note(request: HttpRequest, id: uuid.UUID, body: OutreachEdit) -> dict[str, Any]:
    text = body.body.strip()
    if not text:
        raise ApiError(400, NOTE_INVALID)
    if not outreach.save_edit(_user_id(request), id, text):
        raise ApiError(404, outreach.NOTE_GONE)
    return {"ok": True, "message": "Saved"}


@notes.delete("/{id}", response=OkMessageOut, summary="Discard a note")
@messages(not_found=outreach.NOTE_GONE)
def delete_note(request: HttpRequest, id: uuid.UUID) -> dict[str, Any]:
    if not outreach.discard(_user_id(request), id):
        raise ApiError(404, outreach.NOTE_GONE)
    return {"ok": True, "message": "Note discarded."}
