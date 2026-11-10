"""Fixtures for the mail tests (#26). Nothing here, or in any test, reaches Google.

`FakeGoogle` plays the three hosts Foray talks to, behind the guarded client
(`httpx2.MockTransport`, through `tests.jobs.conftest.mock_client`), installed where the code
gets its client (`apps.mail.google.http_client`):

- **`oauth2.googleapis.com`**: the token endpoint, which redeems an authorization code only
  with the PKCE verifier whose challenge the consent page was given, and refreshes only a
  refresh token it still honours; and the revocation endpoint.
- **`gmail.googleapis.com`**: `users/me/profile`, `users/me/messages` (the search, paged) and
  `users/me/messages/{id}`, each only with an access token it issued. A message is answered as
  `format=metadata` answers it: ids, `internalDate`, `snippet` and the headers asked for. A
  request for any other format, or one that is not a GET, is refused and remembered in
  `refused`, so a test can say nothing but metadata was ever asked for.
- **`accounts.google.com`** is never requested by Foray: the browser goes there. `consent(url)`
  stands in for the user at the consent page and returns the callback the browser would be
  sent to.

Every request is recorded (`requests`), so tests assert what was asked: the scope, the
verifier, the query, the methods.

`configured` gives the install a Google OAuth client for the test.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit

import httpx2
import pytest
from django.conf import settings
from django.utils import timezone
from pydantic import SecretStr

from apps.accounts.models import User
from apps.core import http
from apps.mail import google
from apps.mail.models import MailAccount
from tests.factories import make_mail_account
from tests.jobs.conftest import mock_client

CLIENT_ID = "foray-test-client.apps.googleusercontent.example"
CLIENT_SECRET = "test-client-secret-not-real"
MAILBOX = "sam@mailbox.example"
REDIRECT_URI = "http://localhost:3000/api/mail/google/callback"
ORIGIN = "http://localhost:3000"


def _json(status: int, body: Any) -> httpx2.Response:
    return httpx2.Response(status, json=body)


def _api_error(status: int, reason: str, state: str) -> httpx2.Response:
    return _json(
        status,
        {
            "error": {
                "code": status,
                "message": f"Google's own words about {reason}, which Foray never shows.",
                "errors": [{"reason": reason, "domain": "global"}],
                "status": state,
            }
        },
    )


@dataclass
class FakeGoogle:
    mailbox: str = MAILBOX
    # What the consent page grants, and whether the token endpoint sends a refresh token.
    granted_scope: str = google.SCOPE
    sends_refresh_token: bool = True
    page_size: int = 100

    requests: list[httpx2.Request] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)  # requests no read-only client makes
    forms: list[dict[str, str]] = field(default_factory=list)  # token-endpoint bodies
    revoked: list[str] = field(default_factory=list)
    # Newest first, as Gmail lists them.
    messages: list[dict[str, Any]] = field(default_factory=list)
    codes: dict[str, str] = field(default_factory=dict)  # code -> the challenge it came with
    refresh_tokens: set[str] = field(default_factory=set)
    access_tokens: set[str] = field(default_factory=set)
    issued: int = 0
    # Set to answer some other way: {"gmail": handler}, {"token": handler}, {"revoke": handler}.
    answer: dict[str, Callable[[httpx2.Request], httpx2.Response]] = field(default_factory=dict)

    # --- the browser's side ----------------------------------------------------------------------

    def consent(self, url: str, *, error: str | None = None) -> str:
        """The user at Google's consent page. Checks the request Foray sent them there with,
        and answers the path the browser is redirected to."""
        parts = urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == google.AUTH_URL
        query = dict(parse_qsl(parts.query))
        assert query["client_id"] == CLIENT_ID
        assert query["redirect_uri"] == REDIRECT_URI
        assert query["response_type"] == "code"
        assert query["code_challenge_method"] == "S256"
        back = {"state": query["state"]}
        if error:
            back["error"] = error
        else:
            self.issued += 1
            code = f"code-{self.issued}"
            self.codes[code] = query["code_challenge"]
            back |= {"code": code, "scope": self.granted_scope}
        return f"/api/mail/google/callback?{urlencode(back)}"

    # --- the mailbox ------------------------------------------------------------------------------

    def deliver(
        self,
        *,
        sender: str = "Acme Recruiting <no-reply@greenhouse-mail.io>",
        subject: str = "Thank you for applying to Acme",
        snippet: str = "Hi Sam, we received your application and will review it shortly.",
        at: datetime | None = None,
        message_id: str | None = None,
    ) -> str:
        """Put a message at the top of the mailbox, as if it had just arrived."""
        self.issued += 1
        message_id = message_id or f"msg{self.issued:04d}"
        at = at or timezone.now()
        self.messages.insert(
            0,
            {
                "id": message_id,
                "threadId": f"thread-{message_id}",
                "labelIds": ["INBOX", "CATEGORY_UPDATES"],
                "snippet": snippet,
                "historyId": "1",
                "internalDate": str(int(at.timestamp() * 1000)),
                "sizeEstimate": 4096,
                "payload": {
                    "mimeType": "multipart/alternative",
                    "headers": [
                        {"name": "From", "value": sender},
                        {"name": "Subject", "value": subject},
                        {"name": "Date", "value": at.strftime("%a, %d %b %Y %H:%M:%S +0000")},
                        # Never asked for, and never to be read:
                        {"name": "To", "value": self.mailbox},
                        {"name": "Delivered-To", "value": self.mailbox},
                    ],
                    # A body Foray must never see; `format=metadata` leaves it out.
                    "body": {"data": "VGhlIGZ1bGwgdGV4dCBvZiB0aGUgbWVzc2FnZS4="},
                },
            },
        )
        return message_id

    def revoke_grant(self) -> None:
        """The user removed Foray at Google, or the grant expired: nothing it issued works."""
        self.refresh_tokens.clear()
        self.access_tokens.clear()

    def expire_access_tokens(self) -> None:
        self.access_tokens.clear()

    def connect(self, user: User, **fields: Any) -> MailAccount:
        """A mailbox already connected: tokens this fake honours, sealed in a `MailAccount`."""
        self.issued += 1
        refresh, access = f"refresh-{self.issued}", f"access-{self.issued}"
        self.refresh_tokens.add(refresh)
        self.access_tokens.add(access)
        return make_mail_account(
            user, **{"address": self.mailbox, "refresh": refresh, "access": access, **fields}
        )

    # --- the requests -----------------------------------------------------------------------------

    @property
    def gmail_requests(self) -> list[httpx2.Request]:
        return [r for r in self.requests if r.url.host == "gmail.googleapis.com"]

    @property
    def token_requests(self) -> list[httpx2.Request]:
        return [r for r in self.requests if str(r.url) == google.TOKEN_URL]

    def reads(self) -> list[str]:
        """The message ids whose metadata was read, in order."""
        prefix = "/gmail/v1/users/me/messages/"
        return [
            r.url.path.removeprefix(prefix)
            for r in self.gmail_requests
            if r.url.path.startswith(prefix)
        ]

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        host = request.url.host
        if host == "oauth2.googleapis.com":
            if request.url.path == "/revoke":
                return (self.answer.get("revoke") or self._revoke)(request)
            return (self.answer.get("token") or self._token)(request)
        if host == "gmail.googleapis.com":
            return (self.answer.get("gmail") or self._gmail)(request)
        self.refused.append(f"{request.method} {request.url}")
        return httpx2.Response(404, text="Not Found")

    def _form(self, request: httpx2.Request) -> dict[str, str]:
        assert request.method == "POST"
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        self.forms.append(form)
        return form

    def _token(self, request: httpx2.Request) -> httpx2.Response:
        form = self._form(request)
        if (form.get("client_id"), form.get("client_secret")) != (CLIENT_ID, CLIENT_SECRET):
            return _json(401, {"error": "invalid_client", "error_description": "Unauthorized"})
        if form.get("grant_type") == "authorization_code":
            challenge = self.codes.pop(form.get("code", ""), None)  # a code is redeemed once
            verifier = form.get("code_verifier", "")
            digest = hashlib.sha256(verifier.encode()).digest()
            proved = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
            if challenge is None or proved != challenge or form.get("redirect_uri") != REDIRECT_URI:
                return _json(400, {"error": "invalid_grant", "error_description": "Bad Request"})
            return self._issue(with_refresh=self.sends_refresh_token)
        if form.get("grant_type") == "refresh_token":
            if form.get("refresh_token") not in self.refresh_tokens:
                return _json(
                    400,
                    {
                        "error": "invalid_grant",
                        "error_description": "Token has been expired or revoked.",
                    },
                )
            return self._issue(with_refresh=False)
        return _json(400, {"error": "unsupported_grant_type"})

    def _issue(self, *, with_refresh: bool) -> httpx2.Response:
        self.issued += 1
        access = f"access-{self.issued}"
        self.access_tokens.add(access)
        body: dict[str, Any] = {
            "access_token": access,
            "expires_in": 3599,
            "scope": self.granted_scope,
            "token_type": "Bearer",
        }
        if with_refresh:
            refresh = f"refresh-{self.issued}"
            self.refresh_tokens.add(refresh)
            # What Google adds for an OAuth app still in "Testing": seven days.
            body |= {"refresh_token": refresh, "refresh_token_expires_in": 604799}
        return _json(200, body)

    def _revoke(self, request: httpx2.Request) -> httpx2.Response:
        token = self._form(request).get("token", "")
        if token not in self.refresh_tokens and token not in self.access_tokens:
            return _json(400, {"error": "invalid_token"})
        self.revoked.append(token)
        self.revoke_grant()
        return _json(200, {})

    def _gmail(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if request.method != "GET":
            self.refused.append(f"{request.method} {path}")
            return _api_error(403, "insufficientPermissions", "PERMISSION_DENIED")
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if token not in self.access_tokens:
            return _api_error(401, "authError", "UNAUTHENTICATED")
        root = "/gmail/v1/users/me"
        if path == f"{root}/profile":
            return _json(
                200,
                {
                    "emailAddress": self.mailbox,
                    "messagesTotal": len(self.messages),
                    "threadsTotal": len(self.messages),
                    "historyId": "1",
                },
            )
        if path == f"{root}/messages":
            return self._list(request)
        if path.startswith(f"{root}/messages/"):
            return self._get(request, path.removeprefix(f"{root}/messages/"))
        self.refused.append(f"GET {path}")
        return _api_error(404, "notFound", "NOT_FOUND")

    def _list(self, request: httpx2.Request) -> httpx2.Response:
        params = request.url.params
        if params.get("q") != google.QUERY:
            self.refused.append(f"GET {request.url.path}?q={params.get('q')}")
            return _api_error(400, "invalidArgument", "INVALID_ARGUMENT")
        start = int(params.get("pageToken") or 0)
        size = min(int(params.get("maxResults") or 100), self.page_size)
        page = self.messages[start : start + size]
        body: dict[str, Any] = {"resultSizeEstimate": len(self.messages)}
        if page:  # Gmail leaves the key out of an empty answer
            body["messages"] = [{"id": m["id"], "threadId": m["threadId"]} for m in page]
        if start + size < len(self.messages):
            body["nextPageToken"] = str(start + size)
        return _json(200, body)

    def _get(self, request: httpx2.Request, message_id: str) -> httpx2.Response:
        params = request.url.params
        asked = set(params.get_list("metadataHeaders"))
        if params.get("format") != "metadata" or not asked <= {"From", "Subject", "Date"}:
            self.refused.append(f"GET {request.url.path}?{request.url.query.decode()}")
            return _api_error(403, "insufficientPermissions", "PERMISSION_DENIED")
        message = next((m for m in self.messages if m["id"] == message_id), None)
        if message is None:
            return _api_error(404, "notFound", "NOT_FOUND")
        payload = message["payload"]
        headers = [h for h in payload["headers"] if h["name"] in asked]
        metadata = {**message, "payload": {"mimeType": payload["mimeType"], "headers": headers}}
        return _json(200, metadata)


@pytest.fixture
def google_server(monkeypatch: pytest.MonkeyPatch) -> FakeGoogle:
    server = FakeGoogle()
    monkeypatch.setattr(google, "http_client", lambda: mock_client(server.handler))
    return server


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """A retried read does not sleep through its backoff."""

    async def instantly(seconds: float) -> None:
        return None

    monkeypatch.setattr(http, "_sleep", instantly)


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """This install has a Google OAuth client."""
    monkeypatch.setattr(settings.ENV, "google_client_id", CLIENT_ID)
    monkeypatch.setattr(settings.ENV, "google_client_secret", SecretStr(CLIENT_SECRET))


def days_ago(days: float) -> datetime:
    return timezone.now() - timedelta(days=days)


def secrets_of(server: FakeGoogle) -> list[str]:
    """Every credential the fake has seen or issued: what must never be stored in the clear,
    answered by the API, put in a URL or logged."""
    return [CLIENT_SECRET, *server.refresh_tokens, *server.access_tokens, *server.revoked]


def dump(value: Any) -> str:
    return json.dumps(value, default=str)
