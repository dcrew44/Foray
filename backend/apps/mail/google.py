"""Google, the one mail provider (#26): the OAuth endpoints and the three Gmail reads.

Everything Foray asks of Google is here, so what it can ask is easy to audit:

| Request | Why |
|---|---|
| the browser is sent to `AUTH_URL` | the user grants `gmail.readonly`, and only that |
| `POST TOKEN_URL` (form) | an authorization code, or a refresh token, for an access token |
| `POST REVOKE_URL` (form) | disconnecting: Google forgets the grant |
| `GET …/users/me/profile` | which mailbox was connected |
| `GET …/users/me/messages?q=…` | the ids of messages that might be an employer's answer |
| `GET …/users/me/messages/{id}` | one message's metadata, and nothing else of it |

A message is always asked for with `format=metadata` and `metadataHeaders` of `From`, `Subject`
and `Date`: its ids, its date, those three headers and Gmail's snippet. Nothing else: no body
(that format never returns one), no attachment, no label, and no request that sends, deletes
or changes mail. The scope would not allow those either.

Requests go through the guarded `HttpClient` like every other. The token endpoint is asked once
per call (`post_form` does not retry: an authorization code is spent by its first use); the
Gmail reads are GETs and are retried once.

**Errors are ours, never Google's words.** A failure raises `GoogleError` with a short `code`
for the log and a sentence for the user. Google's own text (`error_description`, an API
message) is read only to tell the cases apart and is never stored, shown or logged, and no
token ever appears in an error. `Reconnect` is the failure only the user can fix: Google no
longer honours the grant (`invalid_grant`: revoked, or expired after seven days while the OAuth
app is in "Testing"), or the grant lacks the scope.

Written from Google's documentation, and not yet run against Google: `docs/gmail.md` lists
what to watch the first time a mailbox is connected.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode

from django.conf import settings

from apps.core.http import FetchError, HttpClient, HttpError, NetworkError

__all__ = [
    "AUTH_URL",
    "CALLBACK_PATH",
    "GMAIL_URL",
    "METADATA_HEADERS",
    "QUERY",
    "REVOKE_URL",
    "SCOPE",
    "TOKEN_URL",
    "Gone",
    "GoogleError",
    "Reconnect",
    "Tokens",
    "Unauthorized",
    "authorization_url",
    "challenge",
    "exchange_code",
    "http_client",
    "list_message_ids",
    "mailbox_address",
    "message_metadata",
    "new_state",
    "new_verifier",
    "redirect_uri",
    "refresh_access_token",
    "revoke",
]

# Read-only, and the only scope asked for. It is one of Google's "restricted" scopes.
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GMAIL_URL = "https://gmail.googleapis.com/gmail/v1/users/me"
CALLBACK_PATH = "/api/mail/google/callback"

# What the search asks Gmail for: mail of the last two weeks, not written by the user and not
# in the Promotions tab (job boards' marketing), that uses a word an employer's answer to an
# application uses. Deliberately wide on words and narrow on time: `classify` decides what each
# message is, and anything that is not an answer is remembered only as read. In Gmail's search
# syntax `newer_than:14d` is by the message's date, a leading `-` leaves something out, `{…}`
# is "any of these", and a quoted phrase has to appear as written. Gmail matches whole words,
# so the forms that matter are each listed.
QUERY = (
    "newer_than:14d -category:promotions -from:me "
    "{application applying applied candidacy candidate interview interviews recruiter "
    '"hiring manager" "phone screen" "next steps" "move forward" "moving forward" '
    '"other candidates"}'
)
# The only headers a message is asked for.
METADATA_HEADERS = ("From", "Subject", "Date")
PAGE_SIZE = 100

TOKEN_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 10.0
ANSWER_MAX_BYTES = 256 * 1024

UNREACHABLE = "Could not reach Google. Foray will try again at the next check."
REFUSED = "Google refused the request. Try connecting again."
RECONNECT = (
    "Google no longer accepts Foray's access to this mailbox: it was revoked, or it expired. "
    "Connect it again."
)
RECONNECT_SCOPE = (
    "Foray was not given permission to read this mailbox. Connect it again and allow the "
    "Gmail access it asks for."
)
BAD_CLIENT = (
    "Google did not recognise this install's OAuth client. Check GOOGLE_CLIENT_ID and "
    "GOOGLE_CLIENT_SECRET against docs/gmail.md."
)
BAD_REDIRECT = (
    "The redirect URI registered with Google is not this install's. It has to be PUBLIC_ORIGIN "
    "followed by /api/mail/google/callback (docs/gmail.md)."
)
API_DISABLED = (
    "The Gmail API is not enabled for this install's Google Cloud project. Enable it "
    "(docs/gmail.md) and the next check will work."
)
RATE_LIMITED = "Gmail is limiting how fast Foray may read. It will try again at the next check."
UNREADABLE = "Google gave an answer Foray could not read. It will try again at the next check."
NOT_ALLOWED = "Google refused to let Foray read this mailbox. It will try again at the next check."
NOT_ACCEPTED = "Gmail did not accept Foray's request. It will try again at the next check."


class GoogleError(Exception):
    """A request to Google that did not give what was asked. `code` is a word of ours for the
    log; the text is a sentence for the user. Neither quotes Google, and neither holds a token."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class Reconnect(GoogleError):
    """Only the user can fix this: the grant is gone or was never enough."""


class Unauthorized(GoogleError):
    """Gmail refused the access token (401). A refresh may cure it, once."""


class Gone(GoogleError):
    """The message no longer exists (deleted between the listing and the read)."""


@dataclass(frozen=True, slots=True)
class Tokens:
    """A token endpoint's answer. The tokens stay out of `repr`, so a log line or a traceback
    that shows this object shows no secret."""

    access_token: str = field(repr=False)
    expires_in: int  # seconds
    scopes: tuple[str, ...]
    refresh_token: str | None = field(default=None, repr=False)

    @property
    def reads_mail(self) -> bool:
        return SCOPE in self.scopes


def http_client() -> HttpClient:
    """One guarded client per job or request. Tests replace this to play Google."""
    return HttpClient()


def redirect_uri() -> str:
    """Where Google sends the browser back to: the address registered with the OAuth client."""
    return f"{settings.PUBLIC_ORIGIN}{CALLBACK_PATH}"


def _client_id() -> str:
    client_id = settings.ENV.google_client_id
    if not client_id:
        raise GoogleError("not-configured", BAD_CLIENT)
    return client_id


def _client_secret() -> str:
    secret = settings.ENV.google_client_secret
    if secret is None:
        raise GoogleError("not-configured", BAD_CLIENT)
    return secret.get_secret_value()


# --- the authorization request -------------------------------------------------------------------


def new_state() -> str:
    return secrets.token_urlsafe(32)


def new_verifier() -> str:
    """A PKCE code verifier: 86 characters of `[A-Za-z0-9_-]` (RFC 7636 allows 43 to 128)."""
    return secrets.token_urlsafe(64)


def challenge(verifier: str) -> str:
    """The S256 code challenge of a verifier: base64url(sha256(verifier)), unpadded."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorization_url(state: str, verifier: str) -> str:
    """Google's consent page for this install: the code flow with PKCE, for the one read-only
    scope. `access_type=offline` asks for a refresh token; `prompt=consent` makes Google give
    one again when the user has connected before (it otherwise sends none the second time)."""
    query = {
        "client_id": _client_id(),
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge(verifier),
        "code_challenge_method": "S256",
    }
    return f"{AUTH_URL}?{urlencode(query)}"


# --- the token endpoint --------------------------------------------------------------------------


def _json(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _oauth_code(error: HttpError) -> str:
    """The token endpoint's `error` word (`invalid_grant`, `invalid_client`, …), or ""."""
    code = _json(error.body).get("error")
    return code if isinstance(code, str) else ""


def _token_failure(error: FetchError, *, refreshing: bool) -> GoogleError:
    if isinstance(error, NetworkError):
        return GoogleError("unreachable", UNREACHABLE)
    if not isinstance(error, HttpError):
        return GoogleError("refused", REFUSED)
    if not isinstance(error.status, int):
        return GoogleError(str(error.status), UNREACHABLE)
    code = _oauth_code(error)
    if code == "invalid_grant":
        # A refresh token Google no longer honours, or (when exchanging) a code that was
        # already used, has expired, or came with the wrong verifier.
        return Reconnect(code, RECONNECT) if refreshing else GoogleError(code, REFUSED)
    if code in ("invalid_client", "unauthorized_client"):
        return GoogleError(code, BAD_CLIENT)
    if code == "redirect_uri_mismatch":
        return GoogleError(code, BAD_REDIRECT)
    if error.status == 429 or error.status >= 500:
        return GoogleError(f"http-{error.status}", UNREACHABLE)
    return GoogleError(code or f"http-{error.status}", REFUSED)


def _tokens(text: str) -> Tokens:
    answer = _json(text)
    access = answer.get("access_token")
    expires_in = answer.get("expires_in")
    if not isinstance(access, str) or not access:
        raise GoogleError("unreadable", UNREADABLE)
    refresh = answer.get("refresh_token")
    scope = answer.get("scope")
    return Tokens(
        access_token=access,
        # Google says 3599. An answer without it is treated as nearly spent, never as eternal.
        expires_in=expires_in if isinstance(expires_in, int) and expires_in > 0 else 60,
        scopes=tuple(scope.split()) if isinstance(scope, str) else (),
        refresh_token=refresh if isinstance(refresh, str) and refresh else None,
    )


async def exchange_code(client: HttpClient, code: str, verifier: str) -> Tokens:
    """An authorization code for tokens, with the PKCE verifier that proves this is the browser
    that started. One attempt. Raises `GoogleError`."""
    fields = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "client_id": _client_id(),
        "client_secret": _client_secret(),
        "redirect_uri": redirect_uri(),
    }
    try:
        text = await client.post_form(
            TOKEN_URL, fields, timeout=TOKEN_TIMEOUT_S, max_bytes=ANSWER_MAX_BYTES
        )
    except FetchError as error:
        raise _token_failure(error, refreshing=False) from None
    return _tokens(text)


async def refresh_access_token(client: HttpClient, refresh_token: str) -> Tokens:
    """A new access token. Raises `Reconnect` when Google no longer honours the refresh token,
    and any other `GoogleError` for a failure worth another try later."""
    fields = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": _client_id(),
        "client_secret": _client_secret(),
    }
    try:
        text = await client.post_form(
            TOKEN_URL, fields, timeout=TOKEN_TIMEOUT_S, max_bytes=ANSWER_MAX_BYTES
        )
    except FetchError as error:
        raise _token_failure(error, refreshing=True) from None
    return _tokens(text)


async def revoke(client: HttpClient, token: str) -> bool:
    """Ask Google to forget the grant this token belongs to. Best effort: False when it could
    not be done (unreachable, or already revoked), never an exception."""
    try:
        await client.post_form(
            REVOKE_URL, {"token": token}, timeout=TOKEN_TIMEOUT_S, max_bytes=ANSWER_MAX_BYTES
        )
    except FetchError:
        return False
    return True


# --- Gmail ---------------------------------------------------------------------------------------


def _api_reasons(error: HttpError) -> set[str]:
    """The machine words of a Google API error body: its `status` and each `reason`."""
    detail = _json(error.body).get("error")
    if not isinstance(detail, dict):
        return set()
    found = {detail.get("status")}
    for key in ("errors", "details"):
        items = detail.get(key)
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict):
                found.add(item.get("reason"))
    return {word for word in found if isinstance(word, str)}


def _gmail_failure(error: FetchError) -> GoogleError:
    if isinstance(error, NetworkError):
        return GoogleError("unreachable", UNREACHABLE)
    if not isinstance(error, HttpError):
        return GoogleError("refused", NOT_ALLOWED)
    status = error.status
    if not isinstance(status, int):
        return GoogleError(str(status), UNREACHABLE)
    if status == 401:
        return Unauthorized("unauthorized", RECONNECT)
    if status == 404:
        return Gone("gone", NOT_ALLOWED)
    reasons = _api_reasons(error)
    if status == 429 or reasons & {"rateLimitExceeded", "userRateLimitExceeded"}:
        return GoogleError("rate-limited", RATE_LIMITED)
    if status == 403:
        if reasons & {"accessNotConfigured", "SERVICE_DISABLED"}:
            return GoogleError("api-disabled", API_DISABLED)
        if reasons & {"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}:
            return Reconnect("scope", RECONNECT_SCOPE)
        return GoogleError("forbidden", NOT_ALLOWED)
    if status >= 500:
        return GoogleError(f"http-{status}", UNREACHABLE)
    return GoogleError(f"http-{status}", NOT_ACCEPTED)


async def _get(client: HttpClient, access_token: str, url: str) -> dict[str, Any]:
    try:
        answer = await client.fetch_json(
            url,
            timeout=READ_TIMEOUT_S,
            retries=1,
            headers={"authorization": f"Bearer {access_token}"},
            max_bytes=ANSWER_MAX_BYTES,
        )
    except FetchError as error:
        raise _gmail_failure(error) from None
    except ValueError:
        raise GoogleError("unreadable", UNREADABLE) from None
    if not isinstance(answer, dict):
        raise GoogleError("unreadable", UNREADABLE)
    return answer


async def mailbox_address(client: HttpClient, access_token: str) -> str:
    """The address of the mailbox the token reads (`users.getProfile`)."""
    address = (await _get(client, access_token, f"{GMAIL_URL}/profile")).get("emailAddress")
    if not isinstance(address, str) or "@" not in address:
        raise GoogleError("unreadable", UNREADABLE)
    return address.strip().lower()


async def list_message_ids(
    client: HttpClient, access_token: str, page_token: str | None = None
) -> tuple[list[str], str | None]:
    """One page of the ids `QUERY` finds, newest first, and the token of the next page."""
    query = {"q": QUERY, "maxResults": str(PAGE_SIZE)}
    if page_token:
        query["pageToken"] = page_token
    answer = await _get(client, access_token, f"{GMAIL_URL}/messages?{urlencode(query)}")
    listed = answer.get("messages")
    ids = [
        item["id"]
        for item in (listed if isinstance(listed, list) else [])
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]
    ]
    next_token = answer.get("nextPageToken")
    return ids, next_token if isinstance(next_token, str) and next_token else None


async def message_metadata(
    client: HttpClient, access_token: str, message_id: str
) -> dict[str, Any]:
    """One message as `format=metadata` gives it: its ids, `internalDate`, Gmail's `snippet`
    and the three headers asked for. Never a body."""
    query = [("format", "metadata"), *(("metadataHeaders", name) for name in METADATA_HEADERS)]
    url = f"{GMAIL_URL}/messages/{quote(message_id, safe='')}?{urlencode(query)}"
    return await _get(client, access_token, url)
