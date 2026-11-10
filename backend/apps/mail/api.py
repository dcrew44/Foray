"""`/api/mail` (#26): connect a mailbox, disconnect it, and answer what its mail suggests.

- `POST /api/mail/google/start`: `{ok, message, url}`, the Google consent page to send the
  browser to. 409 when this install has no Google OAuth client; 429 past ten in ten minutes.
- `GET /api/mail/google/callback?state=&code=` is where Google sends the browser back: a
  top-level navigation, not a call of the app's. It always answers a redirect to
  `/settings?mail=<word>#email`, and Settings shows the sentence for the word
  (`connect.NOTICES`). Nothing Google said, and no error text, is put in the URL.
- `DELETE /api/mail/google`: `{ok, message}`. Deletes the account and everything read from it,
  then asks Google to forget the grant.
- `POST /api/mail/suggestions/{id} {action: "accept" | "dismiss"}`: `{ok, message}`. Accepting
  moves the match to the stage the email suggests; nothing else moves a stage.

**The two requests that wait on Google.** The callback (the code exchange and one profile read)
and disconnecting (the revocation) are `async` operations: each makes at most two requests to
Google, ten seconds each and never retried, holds no transaction while it waits, and reaches
the database only through `db()`. An authorization code lasts minutes and a deleted account's
token should not sit in a job queue, so neither is a job. Everything else that reads mail is
the worker's (`tasks.py`).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Literal, cast

from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect

from apps.accounts.models import User
from apps.core import throttle
from apps.core.db import db
from apps.core.errors import ApiError, messages
from apps.core.router import ApiModel, router
from apps.core.schemas import OkMessageOut
from apps.mail import connect, google, suggestions
from apps.mail import tasks as _tasks  # noqa: F401 (registers the task connecting defers)
from apps.matching.models import Stage

log = logging.getLogger("foray.mail")

mail = router(tags=["mail"])

ROUTERS = {"/mail": mail}

SETTINGS_PATH = "/settings"
NOT_CONFIGURED = (
    "Gmail is not set up on this install: it needs a Google OAuth client. See docs/gmail.md."
)
GOING = "Taking you to Google."
NOT_CONNECTED = "No mailbox is connected."
DISCONNECTED = (
    "Disconnected. Everything Foray read from the mailbox is deleted, and Google has been "
    "told to forget the access."
)
DISCONNECTED_NOT_REVOKED = (
    "Disconnected, and everything Foray read from the mailbox is deleted. Google could not be "
    "told to forget the access: remove Foray under your Google Account's third-party access "
    "to finish."
)
DECISION_INVALID = "That answer could not be read."
SUGGESTION_GONE = "That suggestion no longer exists."
SUGGESTION_STALE = "That suggestion no longer applies. Reload the page."
DISMISSED = "Dismissed."


class ConnectStarted(ApiModel):
    ok: bool
    message: str
    url: str


class SuggestionDecision(ApiModel):
    action: Literal["accept", "dismiss"]


# --- connecting ----------------------------------------------------------------------------------


@mail.post("/google/start", response=ConnectStarted, summary="Start connecting Gmail")
def google_start(request: HttpRequest) -> dict[str, Any]:
    user_id = cast(User, request.user).pk
    if not settings.ENV.mail_configured:
        raise ApiError(409, NOT_CONFIGURED)
    limit = throttle.MAIL_CONNECT
    throttle.throttle(f"mail-connect:{user_id}", limit.limit, limit.window_s)
    return {"ok": True, "message": GOING, "url": connect.begin(request.session)}


def _back_to_settings(outcome: str) -> HttpResponse:
    response = HttpResponseRedirect(f"{SETTINGS_PATH}?mail={outcome}#email")
    response["Cache-Control"] = "no-store"
    return response


async def _complete(session_key: str | None, state: str | None, code: str) -> str:
    """Exchange the code and store the account. Returns the word for Settings. Whenever the
    exchange worked and the account was not stored, Google is asked to forget the grant:
    nothing is kept, so nothing should stay granted."""
    pending = await db(connect.take_pending, session_key, state)
    async with google.http_client() as client:
        tokens = await google.exchange_code(client, code, pending.verifier)
        try:
            if not tokens.reads_mail:
                outcome = connect.NO_SCOPE
            elif tokens.refresh_token is None:
                outcome = connect.NO_REFRESH_TOKEN
            else:
                address = await google.mailbox_address(client, tokens.access_token)
                await db(connect.save_account, pending.user_id, address, tokens)
                outcome = connect.CONNECTED
        except Exception:
            await google.revoke(client, tokens.refresh_token or tokens.access_token)
            raise
        if outcome != connect.CONNECTED:
            await google.revoke(client, tokens.refresh_token or tokens.access_token)
    return outcome


@mail.get("/google/callback", auth=None, include_in_schema=False)
async def google_callback(
    request: HttpRequest,
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
) -> HttpResponse:
    """Where Google sends the browser back. Unauthenticated as far as ninja is concerned: the
    session is checked by `connect.take_pending`, which is what binds the callback to the
    browser that started."""
    if not settings.ENV.mail_configured:
        return _back_to_settings(connect.NOT_CONFIGURED)
    session_key = request.COOKIES.get(settings.SESSION_COOKIE_NAME)
    if error or not code:
        # The user pressed Cancel (`access_denied`), or Google refused the request. The pending
        # request is spent either way, when it is this session's.
        try:
            await db(connect.take_pending, session_key, state)
        except connect.Refused as refused:
            return _back_to_settings(refused.outcome)
        return _back_to_settings(connect.DENIED if error == "access_denied" else connect.FAILED)
    try:
        outcome = await _complete(session_key, state, code)
    except connect.Refused as refused:
        outcome = refused.outcome
    except google.GoogleError as failed:
        log.warning("mail: connecting failed (%s)", failed.code)
        outcome = connect.outcome_of(failed)
    except Exception:
        log.exception("mail: connecting failed unexpectedly")
        outcome = connect.FAILED
    return _back_to_settings(outcome)


# --- disconnecting -------------------------------------------------------------------------------


@mail.delete("/google", response=OkMessageOut, summary="Disconnect Gmail")
async def google_disconnect(request: HttpRequest) -> dict[str, Any]:
    # ninja authenticated the session on a thread; `request.auth` is the loaded user.
    user_id = cast(User, request.auth).pk  # type: ignore[attr-defined]
    removed = await db(connect.remove, user_id)
    if removed is None:
        raise ApiError(404, NOT_CONNECTED)
    revoked = False
    if removed.token is not None:
        try:
            async with google.http_client() as client:
                revoked = await google.revoke(client, removed.token)
        except Exception:
            log.exception("mail: revoking user %s's access failed", user_id)
    return {"ok": True, "message": DISCONNECTED if revoked else DISCONNECTED_NOT_REVOKED}


# --- suggestions ---------------------------------------------------------------------------------


@mail.post("/suggestions/{id}", response=OkMessageOut, summary="Answer a suggestion")
@messages(invalid=DECISION_INVALID, not_found=SUGGESTION_GONE)
def post_suggestion(
    request: HttpRequest, id: uuid.UUID, body: SuggestionDecision
) -> dict[str, Any]:
    user_id = cast(User, request.user).pk
    if body.action == "dismiss":
        outcome, stage = suggestions.dismiss(user_id, id), None
    else:
        outcome, stage = suggestions.accept(user_id, id)
    if outcome == "gone":
        raise ApiError(404, SUGGESTION_GONE)
    if outcome == "stale":
        raise ApiError(409, SUGGESTION_STALE)
    message = f"Moved to {Stage(stage).label}." if stage else DISMISSED
    return {"ok": True, "message": message}
