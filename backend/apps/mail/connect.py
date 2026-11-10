"""Connecting a mailbox and disconnecting it: the database's side (#26). Sync.

**Connecting** is the OAuth authorization-code flow with PKCE, in three steps:

1. `begin(session)` (the user pressed Connect) makes a `state` and a PKCE verifier, keeps both
   in the user's own session, and answers Google's consent URL. The verifier never leaves the
   server; only its SHA-256 challenge goes to Google with the browser.
2. Google sends the browser back to the callback (`api.google_callback`) with that `state` and
   a code. `take_pending(session_key, state)` accepts it once: the session must be signed in,
   must hold a request no older than ten minutes, and its `state` must be the one that came
   back. So a callback that some other page set up, or that arrives in another browser, has
   nothing to redeem.
3. The callback exchanges the code (with the verifier) and reads which mailbox it is, both
   outside any transaction, and `save_account` stores the result: the tokens sealed
   (`crypto.seal`), and the first check queued in the same transaction.

Connecting the same mailbox again (after "needs reconnecting") keeps what was read from it;
connecting a different one replaces the account and deletes the first one's messages, whose ids
mean nothing in another mailbox.

**Disconnecting** (`remove`) deletes the account and, through it, every message read from it,
and hands back the refresh token so the caller can ask Google to forget the grant. What stays
is what has become the user's own record: the date and subject an application's confirmation
was recorded with, and any stage the user chose to move.

`NOTICES` are the sentences Settings shows for how a connection went. The callback sends only
the word (`/settings?mail=denied`): nothing Google said is ever put in a URL or shown.
"""

from __future__ import annotations

import contextlib
import hmac
import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace
from typing import Any, Final

from django.conf import settings
from django.contrib import auth
from django.contrib.sessions.backends.base import SessionBase
from django.db import transaction
from django.db.models.functions import Now
from procrastinate.exceptions import AlreadyEnqueued

from apps.core.clock import database_now
from apps.core.queue import defer_in_tx
from apps.mail import crypto, google
from apps.mail.models import AccountStatus, MailAccount, Provider
from apps.runs.step import task_for

__all__ = [
    "CHECK_TASK",
    "NOTICES",
    "Notice",
    "Pending",
    "Refused",
    "Removed",
    "begin",
    "defer_check",
    "notice",
    "outcome_of",
    "remove",
    "save_account",
    "take_pending",
]

CHECK_TASK = "mail.check"

PENDING_KEY: Final = "mail_oauth"  # in the session: {"state", "verifier", "at"}
PENDING_TTL_S: Final = 600
# An access token is treated as spent this long before Google says it is.
EXPIRY_MARGIN: Final = timedelta(seconds=120)

# The words the callback sends back to Settings, and what each means there.
CONNECTED: Final = "connected"
DENIED: Final = "denied"
EXPIRED: Final = "expired"
FAILED: Final = "failed"
UNREACHABLE: Final = "unreachable"
NO_REFRESH_TOKEN: Final = "no-refresh-token"
NO_SCOPE: Final = "scope"
BAD_CLIENT: Final = "client"
BAD_REDIRECT: Final = "redirect"
NOT_CONFIGURED: Final = "not-configured"


@dataclass(frozen=True, slots=True)
class Notice:
    ok: bool
    message: str


NOTICES: dict[str, Notice] = {
    CONNECTED: Notice(
        True,
        "Connected. Foray has started a first check, and looks again every 15 minutes.",
    ),
    DENIED: Notice(False, "Nothing was connected: the request was cancelled at Google."),
    EXPIRED: Notice(
        False, "That connection attempt expired, or did not start here. Start it again."
    ),
    FAILED: Notice(False, "Google did not complete the connection. Try again."),
    UNREACHABLE: Notice(False, "Could not reach Google. Try again in a moment."),
    NO_REFRESH_TOKEN: Notice(
        False,
        "Google gave Foray no way to keep reading after the first hour, so nothing was "
        "connected. Remove Foray under your Google Account's third-party access, then connect "
        "again.",
    ),
    NO_SCOPE: Notice(
        False,
        "Foray was not given permission to read the mailbox, so nothing was connected. Try "
        "again and allow the Gmail access it asks for.",
    ),
    BAD_CLIENT: Notice(False, google.BAD_CLIENT),
    BAD_REDIRECT: Notice(False, google.BAD_REDIRECT),
    NOT_CONFIGURED: Notice(False, "Gmail is not set up on this install."),
}

# A failed exchange's `GoogleError.code`, as the word Settings is sent.
_OUTCOME_OF_CODE = {
    "invalid_client": BAD_CLIENT,
    "unauthorized_client": BAD_CLIENT,
    "not-configured": NOT_CONFIGURED,
    "redirect_uri_mismatch": BAD_REDIRECT,
    "unreachable": UNREACHABLE,
    "blocked": UNREACHABLE,
}


def notice(word: str | None) -> Notice | None:
    """The sentence for a word the callback sent, or None for anything else."""
    return NOTICES.get(word) if word else None


def outcome_of(error: google.GoogleError) -> str:
    if error.code.startswith("http-5") or error.code == "http-429":
        return UNREACHABLE
    return _OUTCOME_OF_CODE.get(error.code, FAILED)


class Refused(Exception):
    """A callback that cannot be honoured. `outcome` is the word Settings is sent."""

    def __init__(self, outcome: str) -> None:
        super().__init__(outcome)
        self.outcome = outcome


@dataclass(frozen=True, slots=True)
class Pending:
    user_id: str
    verifier: str = field(repr=False)


# --- starting ------------------------------------------------------------------------------------


def begin(session: SessionBase) -> str:
    """Remember a new request in the user's session and answer the URL to send the browser to.
    A second Connect replaces the first: only the newest request can be completed."""
    state, verifier = google.new_state(), google.new_verifier()
    session[PENDING_KEY] = {"state": state, "verifier": verifier, "at": int(time.time())}
    return google.authorization_url(state, verifier)


def _session(session_key: str) -> SessionBase:
    engine: Any = import_module(settings.SESSION_ENGINE)
    return engine.SessionStore(session_key=session_key)


def take_pending(session_key: str | None, state: str | None) -> Pending:
    """The request this callback completes, taken out of the session so it completes once.
    Raises `Refused(EXPIRED)` for anything else; a wrong `state` leaves the session's own
    request where it was, so a forged callback cannot cancel a real one."""
    if not session_key or not state:
        raise Refused(EXPIRED)
    session = _session(session_key)
    # The session's user, read on this thread (as the chat view reads it).
    request: Any = SimpleNamespace(session=session)
    user = auth.get_user(request)
    if not user.is_authenticated:
        raise Refused(EXPIRED)
    pending = session.get(PENDING_KEY)
    if not isinstance(pending, dict):
        raise Refused(EXPIRED)
    expected, verifier, at = pending.get("state"), pending.get("verifier"), pending.get("at")
    if not isinstance(expected, str) or not isinstance(verifier, str) or not isinstance(at, int):
        raise Refused(EXPIRED)
    if not hmac.compare_digest(expected.encode(), state.encode()):
        raise Refused(EXPIRED)
    del session[PENDING_KEY]
    session.save()
    if time.time() - at > PENDING_TTL_S:
        raise Refused(EXPIRED)
    return Pending(user_id=str(user.pk), verifier=verifier)


# --- storing -------------------------------------------------------------------------------------


def defer_check(user_id: str) -> None:
    """Queue a check of this user's mailbox, inside the caller's transaction. One waiting copy
    per user is enough: it will find whatever a second one would."""
    with contextlib.suppress(AlreadyEnqueued):
        defer_in_tx(task_for(CHECK_TASK), queueing_lock=f"mail:{user_id}", user_id=user_id)


def save_account(user_id: str, address: str, tokens: google.Tokens) -> uuid.UUID:
    """Store a completed connection and queue its first check. The caller has made sure the
    tokens carry a refresh token and the read-only scope."""
    assert tokens.refresh_token is not None
    fields: dict[str, Any] = {
        "provider": Provider.GOOGLE,
        "address": address,
        "scopes": list(tokens.scopes),
        "status": AccountStatus.CONNECTED,
        "refresh_token": crypto.seal(tokens.refresh_token),
        "access_token": crypto.seal(tokens.access_token),
        "last_error": "",
    }
    with transaction.atomic():
        fields["access_expires_at"] = (
            database_now() + timedelta(seconds=tokens.expires_in) - EXPIRY_MARGIN
        )
        existing = MailAccount.objects.for_user(user_id).select_for_update().first()
        if existing is not None and existing.address != address:
            existing.delete()  # another mailbox: its messages go with it
            existing = None
        if existing is None:
            account = MailAccount.objects.create_for(user_id, **fields)
        else:
            MailAccount.objects.for_user(user_id).filter(id=existing.id).update(
                **fields, updated_at=Now()
            )
            account = existing
        defer_check(user_id)
    return account.id


# --- disconnecting -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Removed:
    address: str
    # The refresh token to revoke at Google, or None when it could not be opened.
    token: str | None = field(repr=False)


def remove(user_id: str) -> Removed | None:
    """Delete the user's mail account and every message read from it. None when there was no
    account."""
    with transaction.atomic():
        account = MailAccount.objects.for_user(user_id).select_for_update().first()
        if account is None:
            return None
        token: str | None = None
        with contextlib.suppress(crypto.Unreadable):
            token = crypto.unseal(account.refresh_token)
        address = account.address
        account.delete()
    return Removed(address=address, token=token)
