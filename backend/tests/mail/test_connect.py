"""Connecting a mailbox and disconnecting it (#26), against the fake Google of `conftest.py`.

What these prove, with no request leaving the machine:

- the browser is sent to Google for the read-only scope alone, with PKCE and a `state` that
  only the session that started can redeem, once, within ten minutes;
- a completed connection stores the mailbox and its tokens **sealed**: the plain refresh token
  is in no column, no API answer, no URL and no log line;
- every way it can fail comes back to Settings as one of Foray's own sentences;
- disconnecting deletes the account and every message read from it, and asks Google to forget.

The callback and the disconnect run their database steps on `db()` threads, which see only
committed rows, so these tests are transactional.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import uuid
from typing import Any, cast
from urllib.parse import parse_qsl, urlsplit

import httpx2
import pytest
from django.conf import settings
from django.db import connection
from django.test import Client
from django.test.client import AsyncClient

from apps.accounts.models import User
from apps.applications.models import Application
from apps.core.db import db
from apps.mail import api as mail_api
from apps.mail import connect, crypto, google, store
from apps.mail.models import ApplicationEmail, MailAccount
from tests.accounts.helpers import Api
from tests.factories import (
    make_application,
    make_application_email,
    make_match,
    make_user,
)
from tests.mail.conftest import (
    CLIENT_ID,
    CLIENT_SECRET,
    MAILBOX,
    REDIRECT_URI,
    FakeGoogle,
    dump,
    secrets_of,
)
from tests.queue import deferred

pytestmark = pytest.mark.django_db(transaction=True)

SSR = {"X-Foray-SSR": "1"}


def signed_in(user: User) -> Api:
    client = Client()
    client.force_login(user)
    return Api(client)


def start(api: Api) -> str:
    response = api.post("/api/mail/google/start")
    assert response.status_code == 200, response.content
    body = response.json()
    assert (body["ok"], body["message"]) == (True, "Taking you to Google.")
    return body["url"]


def came_back_with(response: Any) -> str:
    """The word a callback answered: it is always a redirect to Settings' Email section."""
    assert response.status_code == 302, response.content
    assert response["Cache-Control"] == "no-store"
    location = urlsplit(response["Location"])
    assert (location.scheme, location.netloc, location.path) == ("", "", "/settings")
    assert location.fragment == "email"
    [(key, word)] = parse_qsl(location.query)
    assert key == "mail" and word in connect.NOTICES
    return word


def connect_mailbox(api: Api, server: FakeGoogle) -> Any:
    return api.get(server.consent(start(api)))


def account_of(user: User) -> MailAccount | None:
    return MailAccount.objects.for_user(user.pk).first()


def settings_mail(api: Api, word: str | None = None) -> dict[str, Any]:
    path = "/api/pages/settings" + (f"?mail={word}" if word else "")
    response = api.get(path, headers=SSR)
    assert response.status_code == 200, response.content
    return response.json()["mail"]


def stored_row(user: User) -> str:
    """The account's row exactly as the database holds it."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT row_to_json(a)::text FROM mail_accounts a WHERE user_id = %s", [user.pk]
        )
        row = cursor.fetchone()
    return row[0] if row else ""


# --- an install without a Google client ----------------------------------------------------------


def test_without_a_google_client_nothing_can_be_connected(google_server: FakeGoogle) -> None:
    user = make_user()
    api = signed_in(user)
    assert settings.ENV.mail_configured is False

    refused = api.post("/api/mail/google/start")
    assert refused.status_code == 409
    assert refused.json() == {"ok": False, "message": mail_api.NOT_CONFIGURED}
    assert "docs/gmail.md" in mail_api.NOT_CONFIGURED

    assert came_back_with(api.get("/api/mail/google/callback?state=x&code=y")) == "not-configured"
    assert google_server.requests == [] and account_of(user) is None
    assert settings_mail(api) == {
        "configured": False,
        "redirectUri": REDIRECT_URI,
        "account": None,
        "notice": None,
    }


def test_connecting_needs_a_session(configured: None) -> None:
    response = Api(Client()).post("/api/mail/google/start")
    assert response.status_code == 401
    assert response.json() == {"ok": False, "message": "Not signed in."}


# --- the request ---------------------------------------------------------------------------------


def test_the_browser_is_sent_to_google_for_the_read_only_scope_alone(configured: None) -> None:
    user = make_user()
    api = signed_in(user)
    url = start(api)

    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == (
        "https://accounts.google.com/o/oauth2/v2/auth"
    )
    query = dict(parse_qsl(parts.query))
    pending = api.client.session[connect.PENDING_KEY]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(pending["verifier"].encode()).digest()
    ).rstrip(b"=")
    assert query == {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        # One scope, and it only reads.
        "scope": "https://www.googleapis.com/auth/gmail.readonly",
        "access_type": "offline",
        "prompt": "consent",
        "state": pending["state"],
        "code_challenge": challenge.decode(),
        "code_challenge_method": "S256",
    }
    # The verifier and the client secret stay on the server.
    assert pending["verifier"] not in url and CLIENT_SECRET not in url
    assert 43 <= len(pending["verifier"]) <= 128 and len(pending["state"]) >= 32

    # A second Connect replaces the first: a new state and a new verifier.
    again = dict(parse_qsl(urlsplit(start(api)).query))
    assert again["state"] != query["state"]
    assert again["code_challenge"] != query["code_challenge"]


def test_starting_is_throttled(configured: None) -> None:
    api = signed_in(make_user())
    for _ in range(10):
        start(api)
    assert api.post("/api/mail/google/start").status_code == 429


# --- a connection --------------------------------------------------------------------------------


def test_connecting_stores_the_mailbox_and_its_tokens_sealed(
    configured: None, google_server: FakeGoogle, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    user = make_user()
    api = signed_in(user)

    callback = google_server.consent(start(api))
    assert came_back_with(api.get(callback)) == "connected"

    account = account_of(user)
    assert account is not None
    assert (account.provider, account.address, account.status) == ("google", MAILBOX, "connected")
    assert account.scopes == [google.SCOPE] and account.last_error == ""
    assert account.access_expires_at is not None and account.last_checked_at is None

    # What Google issued is stored, but never as Google issued it.
    [refresh] = google_server.refresh_tokens
    [access] = google_server.access_tokens
    assert account.refresh_token != refresh and account.access_token != access
    assert account.refresh_token.startswith("v1:") and account.access_token.startswith("v1:")
    assert crypto.unseal(account.refresh_token) == refresh
    assert crypto.unseal(account.access_token) == access
    row = stored_row(user)
    assert MAILBOX in row
    for secret in secrets_of(google_server):
        assert secret not in row

    # The exchange: one form POST with the code, the verifier that proves who started (the
    # fake checks it against the challenge), and this install's client. Then one profile read.
    [exchange] = google_server.forms
    assert exchange == {
        "grant_type": "authorization_code",
        "code": "code-1",
        "code_verifier": exchange["code_verifier"],
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
    }
    assert [(r.method, r.url.host, r.url.path) for r in google_server.requests] == [
        ("POST", "oauth2.googleapis.com", "/token"),
        ("GET", "gmail.googleapis.com", "/gmail/v1/users/me/profile"),
    ]
    assert google_server.refused == []

    # The first check is queued with the account, for this user alone.
    [job] = deferred("mail.check")
    assert (job.queue, job.args, job.lock) == ("system", {"user_id": user.pk}, None)
    assert job.queueing_lock == f"mail:{user.pk}"

    # The request is spent: the same callback again redeems nothing.
    assert connect.PENDING_KEY not in api.client.session
    assert came_back_with(api.get(callback)) == "expired"
    assert len(google_server.requests) == 2 and len(MailAccount.objects.for_user(user.pk)) == 1

    # Settings shows the mailbox and never a token, sealed or not.
    mail = settings_mail(api, "connected")
    assert mail["notice"] == {"ok": True, "message": connect.NOTICES["connected"].message}
    assert mail["account"] == {
        "address": MAILBOX,
        "status": "connected",
        "connectedAt": mail["account"]["connectedAt"],
        "lastCheckedAt": None,
        "lastError": "",
        "found": {"confirmations": 0, "rejections": 0, "interviews": 0, "matched": 0, "waiting": 0},
        "waiting": [],
    }
    page = api.get("/api/pages/settings", headers=SSR).content.decode()
    for secret in [*secrets_of(google_server), account.refresh_token, account.access_token]:
        assert secret not in page
        assert secret not in caplog.text
    assert exchange["code_verifier"] not in caplog.text


def test_tokens_stay_out_of_reprs() -> None:
    tokens = google.Tokens("access-secret", 3599, (google.SCOPE,), "refresh-secret")
    access = store.Access(
        account_id=uuid.uuid4(), user_id="u", refresh_token="r-secret", access_token="a-secret"
    )
    pending = connect.Pending(user_id="u", verifier="verifier-secret")
    removed = connect.Removed(address=MAILBOX, token="refresh-secret")
    for value in (tokens, access, pending, removed):
        assert "secret" not in repr(value), type(value).__name__
    account = MailAccount(address=MAILBOX, refresh_token="v1:sealed", access_token="v1:sealed")
    assert "sealed" not in str(account)


# --- who may redeem a callback -------------------------------------------------------------------


def test_a_callback_is_bound_to_the_session_that_started(
    configured: None, google_server: FakeGoogle
) -> None:
    user, other = make_user(), make_user()
    api = signed_in(user)
    callback = google_server.consent(start(api))

    # Another browser, signed in or not, holds no such request.
    assert came_back_with(Api(Client()).get(callback)) == "expired"
    assert came_back_with(signed_in(other).get(callback)) == "expired"
    # A callback with someone else's state (a page that set one up) redeems nothing, and
    # leaves this session's own request standing.
    forged = "/api/mail/google/callback?state=attacker-chosen&code=code-1"
    assert came_back_with(api.get(forged)) == "expired"
    assert came_back_with(api.get("/api/mail/google/callback?code=code-1")) == "expired"
    assert google_server.requests == []
    assert account_of(user) is None and account_of(other) is None

    assert came_back_with(api.get(callback)) == "connected"
    connected = account_of(user)
    assert connected is not None and connected.user_id == user.pk
    assert account_of(other) is None


async def test_connecting_and_disconnecting_through_the_asgi_handler(
    configured: None, google_server: FakeGoogle
) -> None:
    """The same path as the server runs it: Django's ASGI handler and the async middleware
    chain (the sliding session included), with the two `async` operations awaited on the event
    loop and their database steps on `db()` threads."""
    user = await db(make_user)
    client = AsyncClient()
    await cast(Any, client).aforce_login(user)  # django-types does not know the async login
    origin = {"Origin": "http://localhost:3000"}

    started = await client.post(
        "/api/mail/google/start", data="{}", content_type="application/json", headers=origin
    )
    assert started.status_code == 200, started.content
    back = await client.get(google_server.consent(started.json()["url"]))
    assert came_back_with(back) == "connected"

    account = await db(account_of, user)
    assert account is not None and account.address == MAILBOX
    [refresh] = google_server.refresh_tokens
    assert account.refresh_token != refresh and crypto.unseal(account.refresh_token) == refresh
    # Still signed in afterwards, with the request gone from the session.
    me = await client.get("/api/me")
    assert me.status_code == 200 and me.json()["email"] == user.email
    assert connect.PENDING_KEY not in await db(lambda: dict(client.session))

    gone = await client.delete("/api/mail/google", headers=origin)
    assert gone.status_code == 200, gone.content
    assert gone.json() == {"ok": True, "message": mail_api.DISCONNECTED}
    assert await db(account_of, user) is None and google_server.revoked == [refresh]
    # A cross-site page cannot disconnect (or start) anything.
    for method, path in (("delete", "/api/mail/google"), ("post", "/api/mail/google/start")):
        refused = await getattr(client, method)(path, headers={"Origin": "https://evil.example"})
        assert refused.status_code == 403, path


def test_a_request_lasts_ten_minutes(configured: None, google_server: FakeGoogle) -> None:
    user = make_user()
    api = signed_in(user)
    callback = google_server.consent(start(api))
    session = api.client.session
    session[connect.PENDING_KEY] = {
        **session[connect.PENDING_KEY],
        "at": session[connect.PENDING_KEY]["at"] - connect.PENDING_TTL_S - 1,
    }
    session.save()

    assert came_back_with(api.get(callback)) == "expired"
    assert google_server.requests == [] and account_of(user) is None
    assert connect.PENDING_KEY not in api.client.session  # spent, not left lying around


def test_cancelling_at_google_connects_nothing(configured: None, google_server: FakeGoogle) -> None:
    user = make_user()
    api = signed_in(user)
    denied = google_server.consent(start(api), error="access_denied")

    assert came_back_with(api.get(denied)) == "denied"
    assert google_server.requests == [] and account_of(user) is None
    assert connect.PENDING_KEY not in api.client.session
    assert settings_mail(api, "denied")["notice"] == {
        "ok": False,
        "message": "Nothing was connected: the request was cancelled at Google.",
    }
    # Any other refusal Google redirects with is a failure, in Foray's words.
    other = google_server.consent(start(api), error="server_error")
    assert came_back_with(api.get(other)) == "failed"


# --- failures, as sentences ----------------------------------------------------------------------

GOOGLES_WORDS = "Google's own words, which Foray never shows"


def _refuse(status: int, error: str) -> Any:
    body = {"error": error, "error_description": GOOGLES_WORDS}
    return lambda request: httpx2.Response(status, json=body)


def _unreachable(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ConnectError("connection refused", request=request)


@pytest.mark.parametrize(
    ("answer", "word"),
    [
        (_refuse(401, "invalid_client"), "client"),
        (_refuse(400, "unauthorized_client"), "client"),
        (_refuse(400, "redirect_uri_mismatch"), "redirect"),
        (_refuse(400, "invalid_grant"), "failed"),
        (_refuse(400, "something_new"), "failed"),
        (_refuse(503, "backend_error"), "unreachable"),
        (_unreachable, "unreachable"),
        (lambda request: httpx2.Response(200, text="<html>not json</html>"), "failed"),
        (lambda request: httpx2.Response(200, json={"token_type": "Bearer"}), "failed"),
    ],
)
def test_a_failed_exchange_comes_back_as_one_of_forays_sentences(
    answer: Any,
    word: str,
    configured: None,
    google_server: FakeGoogle,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    user = make_user()
    api = signed_in(user)
    google_server.answer["token"] = answer

    response = connect_mailbox(api, google_server)

    assert came_back_with(response) == word
    assert account_of(user) is None and deferred("mail.check") == []
    # Nothing of Google's text, and no stack trace, in the address or on the page.
    assert GOOGLES_WORDS not in response["Location"]
    notice = settings_mail(api, word)["notice"]
    assert notice == {"ok": False, "message": connect.NOTICES[word].message}
    assert "Traceback" not in notice["message"] and GOOGLES_WORDS not in notice["message"]
    assert GOOGLES_WORDS not in caplog.text and CLIENT_SECRET not in caplog.text


def test_an_unknown_word_in_the_address_shows_nothing(configured: None) -> None:
    api = signed_in(make_user())
    for word in ("<script>alert(1)</script>", "Google said no", "", "CONNECTED"):
        assert settings_mail(api, word)["notice"] is None


def test_a_grant_foray_cannot_use_is_not_kept_and_is_revoked(
    configured: None, google_server: FakeGoogle
) -> None:
    user = make_user()
    api = signed_in(user)

    # No refresh token: Foray could read for an hour and never again.
    google_server.sends_refresh_token = False
    assert came_back_with(connect_mailbox(api, google_server)) == "no-refresh-token"
    assert account_of(user) is None
    assert google_server.revoked == ["access-2"]

    # Not the scope: nothing to read with.
    google_server.sends_refresh_token = True
    google_server.granted_scope = "openid email"
    assert came_back_with(connect_mailbox(api, google_server)) == "scope"
    assert account_of(user) is None and len(google_server.revoked) == 2
    assert google_server.gmail_requests == []  # the mailbox was never touched

    # The mailbox could not be named: the grant is given back too.
    google_server.granted_scope = google.SCOPE
    google_server.answer["gmail"] = lambda request: httpx2.Response(503, text="busy")
    assert came_back_with(connect_mailbox(api, google_server)) == "unreachable"
    assert account_of(user) is None and len(google_server.revoked) == 3
    assert deferred("mail.check") == []


# --- connecting again ----------------------------------------------------------------------------


def test_connecting_the_same_mailbox_again_keeps_what_was_read(
    configured: None, google_server: FakeGoogle
) -> None:
    user = make_user()
    api = signed_in(user)
    assert came_back_with(connect_mailbox(api, google_server)) == "connected"
    first = account_of(user)
    assert first is not None
    kept = make_application_email(user, account=first, kind="rejection", match=make_match(user))
    store.need_reconnect(user.pk, first.id, google.RECONNECT)

    assert came_back_with(connect_mailbox(api, google_server)) == "connected"

    again = account_of(user)
    assert again is not None and again.id == first.id
    assert (again.status, again.last_error) == ("connected", "")
    assert again.refresh_token != first.refresh_token
    assert crypto.unseal(again.refresh_token) in google_server.refresh_tokens
    assert ApplicationEmail.objects.for_user(user.pk).filter(id=kept.id).exists()


def test_connecting_another_mailbox_replaces_the_first(
    configured: None, google_server: FakeGoogle
) -> None:
    user = make_user()
    api = signed_in(user)
    assert came_back_with(connect_mailbox(api, google_server)) == "connected"
    first = account_of(user)
    assert first is not None
    make_application_email(user, account=first)

    google_server.mailbox = "sam.other@mailbox.example"
    assert came_back_with(connect_mailbox(api, google_server)) == "connected"

    [account] = MailAccount.objects.for_user(user.pk)
    assert account.id != first.id and account.address == "sam.other@mailbox.example"
    # Its message ids mean nothing in another mailbox.
    assert not ApplicationEmail.objects.for_user(user.pk).exists()


# --- disconnecting -------------------------------------------------------------------------------


def test_disconnecting_removes_everything_of_the_account(
    configured: None, google_server: FakeGoogle
) -> None:
    user, other = make_user(), make_user()
    api = signed_in(user)
    account = google_server.connect(user)
    theirs = make_application_email(other)
    match = make_match(user, stage="applied")
    application = make_application(user, match, status="submitted")
    confirmation = make_application_email(user, account=account, match=match)
    Application.objects.for_user(user.pk).filter(id=application.id).update(
        confirmed_at=confirmation.received_at, confirmation_subject=confirmation.subject
    )
    make_application_email(user, account=account, kind="rejection", match=match)
    make_application_email(user, account=account, kind="other")
    refresh = crypto.unseal(account.refresh_token)

    response = api.client.delete("/api/mail/google", headers={"Origin": "http://localhost:3000"})

    assert response.status_code == 200, response.content
    assert response.json() == {"ok": True, "message": mail_api.DISCONNECTED}
    assert account_of(user) is None
    assert not ApplicationEmail.objects.for_user(user.pk).exists()
    assert stored_row(user) == ""
    # Google was asked to forget the grant: one form POST of the refresh token.
    [request] = google_server.requests
    assert (request.method, str(request.url)) == ("POST", google.REVOKE_URL)
    assert google_server.forms == [{"token": refresh}] and google_server.revoked == [refresh]
    # What stays is the user's own record, and other people's mail.
    kept = Application.objects.for_user(user.pk).get(id=application.id)
    assert kept.confirmed_at == confirmation.received_at
    assert kept.confirmation_subject == confirmation.subject
    assert ApplicationEmail.objects.for_user(other.pk).filter(id=theirs.id).exists()
    assert MailAccount.objects.for_user(other.pk).exists()
    mail = settings_mail(api)
    assert mail["account"] is None and refresh not in dump(mail)

    again = api.client.delete("/api/mail/google", headers={"Origin": "http://localhost:3000"})
    assert again.status_code == 404
    assert again.json() == {"ok": False, "message": "No mailbox is connected."}


@pytest.mark.parametrize("trouble", ["unreachable", "already revoked", "unreadable token"])
def test_disconnecting_works_when_google_cannot_be_told(
    trouble: str, google_server: FakeGoogle
) -> None:
    """Best effort: an unreachable Google, a token it no longer knows, or a stored token this
    install's key can no longer open all still delete everything here."""
    user = make_user()
    api = signed_in(user)
    account = google_server.connect(user)
    make_application_email(user, account=account)
    if trouble == "unreachable":
        google_server.answer["revoke"] = _unreachable
    elif trouble == "already revoked":
        google_server.revoke_grant()
    else:
        MailAccount.objects.for_user(user.pk).update(refresh_token="v1:not-a-token")

    response = api.client.delete("/api/mail/google", headers={"Origin": "http://localhost:3000"})

    assert response.status_code == 200, response.content
    assert response.json() == {"ok": True, "message": mail_api.DISCONNECTED_NOT_REVOKED}
    assert account_of(user) is None
    assert not ApplicationEmail.objects.for_user(user.pk).exists()
    assert google_server.revoked == []
    assert len(google_server.requests) == (0 if trouble == "unreadable token" else 1)
