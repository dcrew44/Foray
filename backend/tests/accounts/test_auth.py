"""Auth (design §4; step 3A): the Better Auth hash carry-over, sign-up/in/out, `/api/me`, the
Origin check, the sign-in and sign-up throttles, the sliding session and `dev_session`."""

from __future__ import annotations

import io
import json
import re
import time
from datetime import timedelta
from typing import Any

import pytest
from django.conf import settings
from django.contrib.auth.hashers import check_password, identify_hasher
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.test import Client
from django.test.client import AsyncClient
from django.utils import timezone

from apps.accounts import api as accounts_api
from apps.accounts.hashers import BetterAuthScryptHasher, encode_imported, js_length
from apps.accounts.models import User
from apps.core import throttle
from apps.core.models import RateLimit
from foray import middleware
from tests.accounts.helpers import ORIGIN, Api, load_golden, session_cookie
from tests.factories import make_user

GOLDEN_HASHES = load_golden("auth", "better_auth_hashes.json")
NOT_SIGNED_IN = {"ok": False, "message": "Not signed in."}
REFUSED = {"ok": False, "message": "Cross-site request refused."}
TOO_MANY = {"ok": False, "message": "Too many attempts. Try again in a few minutes."}
INVALID_CREDENTIALS = {"ok": False, "message": "Invalid email or password"}
PASSWORD = "a-long-enough-password"


def imported_user(golden: dict[str, Any], **fields: Any) -> User:
    """A user as the legacy import writes one: the Better Auth hash, re-encoded."""
    user = make_user(**fields)
    User.objects.filter(pk=user.pk).update(password=encode_imported(golden["hash"]))
    user.refresh_from_db()
    return user


# --- the Better Auth hasher (§4.2) ---------------------------------------------------------------


@pytest.mark.parametrize("golden", GOLDEN_HASHES, ids=lambda g: g["label"])
def test_golden_hashes_verify(golden: dict[str, Any]) -> None:
    encoded = encode_imported(golden["hash"])
    assert encoded.startswith("ba_scrypt$") and len(encoded) == 171
    assert isinstance(identify_hasher(encoded), BetterAuthScryptHasher)

    hasher = BetterAuthScryptHasher()
    assert hasher.verify(golden["password"], encoded)
    # NFKC: the normalised form is the same password.
    assert hasher.verify(golden["nfkc"], encoded)
    assert not hasher.verify(golden["password"] + " ", encoded)
    assert not hasher.verify(golden["password"].upper(), encoded)
    # Through Django's own entry point, as ModelBackend calls it.
    assert check_password(golden["password"], encoded)
    assert not check_password("wrong password", encoded)
    assert hasher.must_update(encoded)
    assert hasher.safe_summary(encoded) == {"algorithm": "ba_scrypt"}


def test_hasher_is_import_only_and_rejects_malformed_hashes() -> None:
    hasher = BetterAuthScryptHasher()
    with pytest.raises(NotImplementedError):
        hasher.encode("password", "salt")
    good = encode_imported(GOLDEN_HASHES[0]["hash"])
    salt, key = GOLDEN_HASHES[0]["hash"].split(":")
    assert hasher.decode(good) == {"algorithm": "ba_scrypt", "salt": salt, "hash": key}
    for bad in (f"ba_scrypt${salt}", f"ba_scrypt${salt}${key[:-1]}", f"ba_scrypt${key}${salt}"):
        assert not hasher.verify(GOLDEN_HASHES[0]["password"], bad)
    for bad in ("", "nocolon", f"{salt.upper()}:{key}", f"{salt}:{key}:extra"):
        with pytest.raises(ValueError):
            encode_imported(bad)


@pytest.mark.django_db
@pytest.mark.parametrize("golden", GOLDEN_HASHES, ids=lambda g: g["label"])
def test_first_sign_in_upgrades_to_pbkdf2(api: Api, golden: dict[str, Any]) -> None:
    user = imported_user(golden, email="carried@example.test")
    before = user.password

    wrong = api.sign_in("carried@example.test", golden["password"] + "!")
    assert wrong.status_code == 400
    user.refresh_from_db()
    assert user.password == before, "a failed sign-in must not touch the hash"

    response = api.sign_in("Carried@Example.test", golden["password"])
    assert response.status_code == 200, response.content
    assert response.json() == {
        "ok": True,
        "user": {"id": user.pk, "name": user.name, "email": "carried@example.test"},
    }
    user.refresh_from_db()
    assert user.password.startswith("pbkdf2_sha256$")
    assert user.check_password(golden["password"])

    # The upgraded hash keeps working, and the session is valid after the re-hash.
    assert api.get("/api/me").status_code == 200
    again = Api(Client()).sign_in("carried@example.test", golden["password"])
    assert again.status_code == 200


# --- sign-up, sign-in, sign-out, me (§4.4) -------------------------------------------------------


@pytest.mark.django_db
def test_sign_up_me_sign_out_sign_in(api: Api) -> None:
    signed_up = api.post(
        "/api/auth/sign-up", {"name": "Ada", "email": "Ada@Example.test", "password": PASSWORD}
    )
    assert signed_up.status_code == 200, signed_up.content
    body = signed_up.json()
    user = User.objects.get(email="ada@example.test")
    assert body == {"ok": True, "user": {"id": user.pk, "name": "Ada", "email": "ada@example.test"}}
    assert re.fullmatch(r"[A-Za-z0-9]{32}", user.pk)
    assert user.password.startswith("pbkdf2_sha256$")
    cookie = session_cookie(signed_up)
    assert cookie is not None and cookie["httponly"] and cookie["samesite"] == "Lax"
    assert cookie["max-age"] == 604800

    me = api.get("/api/me")
    assert me.status_code == 200
    assert me.json() == {
        "id": user.pk,
        "name": "Ada",
        "email": "ada@example.test",
        "signupOpen": True,
    }
    assert me["Cache-Control"] == "no-store"

    out = api.post("/api/auth/sign-out")
    assert out.status_code == 200
    assert out.json() == {"ok": True}
    assert session_cookie(out).value == ""  # the cookie is expired
    assert api.get("/api/me").json() == NOT_SIGNED_IN

    signed_in = api.sign_in("ada@example.test", PASSWORD)
    assert signed_in.status_code == 200
    assert signed_in.json()["user"]["id"] == user.pk
    assert api.get("/api/me").status_code == 200


@pytest.mark.django_db
def test_sign_in_rotates_the_session_key(api: Api) -> None:
    """A session that existed before sign-in (say, one planted by another site) is never the one
    that ends up signed in."""
    make_user(email="rotate@example.test", password=PASSWORD)
    planted = SessionStore()
    planted["note"] = "anonymous"
    planted.create()
    api.client.cookies[settings.SESSION_COOKIE_NAME] = planted.session_key

    signed = session_cookie(api.sign_in("rotate@example.test", PASSWORD)).value
    assert signed and signed != planted.session_key
    assert not Session.objects.filter(session_key=planted.session_key).exists()
    assert Session.objects.get(session_key=signed).get_decoded()["note"] == "anonymous"


@pytest.mark.django_db
def test_sign_up_validation(api: Api) -> None:
    def sign_up(**body: Any) -> Any:
        return api.post("/api/auth/sign-up", {"password": PASSWORD, **body})

    for email in (
        "not-an-email",
        "a@b",
        "a@b.c",
        ".a@example.test",
        "a..b@example.test",
        "a@-b.test",
    ):
        response = sign_up(email=email)
        assert response.status_code == 400, email
        assert response.json() == {"ok": False, "message": "Invalid email"}
    assert sign_up(email="a@example.test\n").json()["message"] == "Invalid email"
    assert api.post("/api/auth/sign-up", {"password": PASSWORD}).json() == {
        "ok": False,
        "message": "Invalid email",
    }

    short = sign_up(email="short@example.test", password="seven77")
    assert (short.status_code, short.json()["message"]) == (400, "Password too short")
    long = sign_up(email="long@example.test", password="x" * 129)
    assert (long.status_code, long.json()["message"]) == (400, "Password too long")
    # Lengths are JavaScript's: an emoji is two UTF-16 code units.
    assert js_length("🔐" * 64) == 128
    assert sign_up(email="emoji@example.test", password="🔐" * 64).status_code == 200
    assert sign_up(email="emoji2@example.test", password="🔐" * 65).json()["message"] == (
        "Password too long"
    )
    assert sign_up(email="emoji3@example.test", password="🔐" * 4).status_code == 200
    assert sign_up(email="emoji4@example.test", password="🔐" * 3).json()["message"] == (
        "Password too short"
    )

    assert sign_up(email="dup@example.test").status_code == 200
    duplicate = Api(Client()).post(
        "/api/auth/sign-up", {"email": "DUP@example.test", "password": PASSWORD}
    )
    assert duplicate.status_code == 409
    assert duplicate.json() == {"ok": False, "message": "User already exists. Use another email."}
    assert User.objects.filter(email="dup@example.test").count() == 1


@pytest.mark.django_db
def test_sign_up_name_defaults_to_the_local_part(api: Api) -> None:
    response = api.post(
        "/api/auth/sign-up", {"email": "Grace.H@example.test", "password": PASSWORD}
    )
    assert response.json()["user"]["name"] == "Grace.H"
    other = Api(Client()).post(
        "/api/auth/sign-up", {"name": "  ", "email": "lin@example.test", "password": PASSWORD}
    )
    assert other.json()["user"]["name"] == "lin"


@pytest.mark.django_db
def test_sign_ups_closed(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.ENV, "signup_open", False)
    response = api.post("/api/auth/sign-up", {"email": "closed@example.test", "password": PASSWORD})
    assert response.status_code == 403
    assert response.json() == {"ok": False, "message": "Sign-ups are closed."}
    assert not User.objects.filter(email="closed@example.test").exists()
    assert api.get("/api/auth/config").json() == {"signupOpen": False}

    monkeypatch.setattr(settings.ENV, "signup_open", True)
    assert api.get("/api/auth/config").json() == {"signupOpen": True}


@pytest.mark.django_db
def test_wrong_credentials_are_400_not_401(api: Api) -> None:
    make_user(email="known@example.test", password=PASSWORD)
    for email, password in (
        ("known@example.test", "wrong password"),
        ("unknown@example.test", PASSWORD),
    ):
        response = api.sign_in(email, password)
        assert response.status_code == 400
        assert response.json() == INVALID_CREDENTIALS
        assert session_cookie(response) is None

    bad_email = api.sign_in("known", PASSWORD)
    assert (bad_email.status_code, bad_email.json()["message"]) == (400, "Invalid email")
    unreadable = api.post("/api/auth/sign-in", {"email": "known@example.test"})
    assert unreadable.status_code == 400
    assert unreadable.json() == {"ok": False, "message": "That request could not be read."}


@pytest.mark.django_db
def test_unusable_and_inactive_users_cannot_sign_in(api: Api) -> None:
    make_user(email="nopassword@example.test")  # create_test_user's unusable hash
    assert api.sign_in("nopassword@example.test", "!").json() == INVALID_CREDENTIALS
    make_user(email="inactive@example.test", password=PASSWORD, is_active=False)
    assert api.sign_in("inactive@example.test", PASSWORD).json() == INVALID_CREDENTIALS


@pytest.mark.django_db
def test_not_signed_in_is_401_json(api: Api) -> None:
    for response in (
        api.get("/api/me"),
        api.post("/api/auth/sign-out"),
        api.put("/api/preferences", {}),
        api.patch("/api/settings", {}),
    ):
        assert response.status_code == 401
        assert response.json() == NOT_SIGNED_IN
    # The unauthenticated operations answer without a session.
    assert api.get("/api/auth/config").status_code == 200


@pytest.mark.django_db
def test_unknown_email_still_hashes_once(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    """Better Auth's timing equalisation, kept by ModelBackend (§4.2)."""
    calls: list[str] = []
    original = User.set_password

    def counting(self: User, raw: str | None) -> None:
        calls.append("hash")
        original(self, raw)

    monkeypatch.setattr(User, "set_password", counting)
    assert api.sign_in("nobody@example.test", PASSWORD).status_code == 400
    assert calls == ["hash"]


# --- the Origin check (§4.3) ---------------------------------------------------------------------


@pytest.mark.django_db
def test_same_origin_post_without_token_succeeds(signed_in: Any) -> None:
    client, _ = signed_in()
    # No CSRF token, no csrftoken cookie: only the browser's Origin.
    assert "csrftoken" not in client.client.cookies
    saved = client.put("/api/preferences", {"target_roles": ["Engineer"]})
    assert saved.status_code == 200, saved.content
    assert client.patch("/api/settings", {"scoreThreshold": 60}).status_code == 200
    referer_only = client.client.put(
        "/api/preferences",
        data="{}",
        content_type="application/json",
        headers={"Referer": f"{ORIGIN}/profile?tab=1", "Sec-Fetch-Site": "same-origin"},
    )
    assert referer_only.status_code == 200
    assert "csrftoken" not in saved.cookies
    out = client.post("/api/auth/sign-out", headers={"Sec-Fetch-Site": "same-origin"})
    assert out.status_code == 200


@pytest.mark.django_db
def test_cross_origin_post_with_cookie_is_refused(signed_in: Any) -> None:
    client, user = signed_in()
    raw = client.client

    def send(**headers: str) -> Any:
        return raw.post(
            "/api/auth/sign-out", data="{}", content_type="application/json", headers=headers
        )

    for headers in (
        {"Origin": "https://evil.example"},
        {"Origin": "http://localhost:3001"},
        {"Origin": "null"},
        {"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"},
        {"Referer": "https://evil.example/page"},
        {},  # a cookie and neither header: not a browser, and not trusted
    ):
        response = send(**headers)
        assert response.status_code == 403, headers
        assert response.json() == REFUSED
    # Nothing was signed out, and safe methods are never refused.
    me = raw.get(
        "/api/me", headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}
    )
    assert me.status_code == 200
    assert me.json()["id"] == user.pk


@pytest.mark.django_db
def test_origin_check_without_a_cookie(api: Api) -> None:
    make_user(email="curl@example.test", password=PASSWORD)
    raw = api.client
    body = json.dumps({"email": "curl@example.test", "password": PASSWORD})
    # curl and tests: neither header and no cookie pass.
    plain = raw.post("/api/auth/sign-in", data=body, content_type="application/json")
    assert plain.status_code == 200
    # A login CSRF from another site is refused even without a cookie.
    other = Client().post(
        "/api/auth/sign-in",
        data=body,
        content_type="application/json",
        headers={"Origin": "https://evil.example"},
    )
    assert other.status_code == 403
    assert other.json() == REFUSED


def test_origin_parsing() -> None:
    assert middleware._origin_of("http://localhost:3000/a?b#c") == "http://localhost:3000"
    assert middleware._origin_of("HTTP://User:pw@LocalHost:3000/") == "http://localhost:3000"
    assert middleware._origin_of("/relative") is None
    assert middleware._origin_of("http://localhost:99999/") is None


# --- throttles (§4.5) ----------------------------------------------------------------------------


@pytest.mark.django_db
def test_sign_in_throttle(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    make_user(email="locked@example.test", password=PASSWORD)
    make_user(email="other@example.test", password=PASSWORD)

    # Successes never count.
    for _ in range(12):
        assert api.sign_in("locked@example.test", PASSWORD).status_code == 200
    assert not RateLimit.objects.filter(key__startswith="signin-fail:").exists()

    for attempt in range(10):
        response = api.sign_in("Locked@example.test", f"wrong {attempt}")
        assert response.status_code == 400, attempt
        assert response.json() == INVALID_CREDENTIALS
    counter = RateLimit.objects.get()
    assert (counter.key, counter.count) == ("signin-fail:locked@example.test:local", 10)

    # The 11th wrong password in the window gets 429, and so does the right one: the counter is
    # read before the password is hashed.
    hashed: list[str] = []
    real = accounts_api.authenticate
    monkeypatch.setattr(
        accounts_api, "authenticate", lambda *a, **k: hashed.append("x") or real(*a, **k)
    )
    eleventh = api.sign_in("locked@example.test", "wrong again")
    assert (eleventh.status_code, eleventh.json()) == (429, TOO_MANY)
    right = api.sign_in("locked@example.test", PASSWORD)
    assert (right.status_code, right.json()) == (429, TOO_MANY)
    assert hashed == [], "a locked key must answer without checking the password"
    assert RateLimit.objects.get().count == 10

    # Another email is not locked (there is no global sign-in bucket).
    assert api.sign_in("other@example.test", PASSWORD).status_code == 200


@pytest.mark.django_db
def test_sign_up_throttle(api: Api) -> None:
    for n in range(5):
        response = Api(Client()).post(
            "/api/auth/sign-up", {"email": f"burst{n}@example.test", "password": PASSWORD}
        )
        assert response.status_code == 200
    sixth = api.post("/api/auth/sign-up", {"email": "burst5@example.test", "password": PASSWORD})
    assert (sixth.status_code, sixth.json()) == (429, TOO_MANY)
    assert not User.objects.filter(email="burst5@example.test").exists()


@pytest.mark.django_db
def test_throttle_counts_per_window() -> None:
    assert throttle.throttle("t:a", 2, 60) == 1
    assert throttle.throttle("t:a", 2, 60) == 2
    with pytest.raises(throttle.ApiError) as refused:
        throttle.throttle("t:a", 2, 60)
    assert (refused.value.status, refused.value.message) == (429, throttle.TOO_MANY)
    assert throttle.current("t:a", 60) == 3
    assert throttle.throttle("t:b", 2, 60) == 1  # keys are independent
    with pytest.raises(throttle.ApiError):
        throttle.check("t:a", 3, 60)
    assert throttle.check("t:b", 3, 60) == 1

    old = RateLimit.objects.create(
        key="t:old", window_start=timezone.now() - timedelta(days=2), count=4
    )
    assert throttle.prune() == 1
    assert not RateLimit.objects.filter(pk=old.pk).exists()
    assert RateLimit.objects.count() == 2


def test_client_key(monkeypatch: pytest.MonkeyPatch, rf: Any) -> None:
    request = rf.get("/api/me", headers={"X-Forwarded-For": "203.0.113.9, 198.51.100.2, 10.0.0.1"})
    monkeypatch.setattr(settings.ENV, "trusted_proxy_hops", 0)
    assert throttle.client_key(request) == "local"
    monkeypatch.setattr(settings.ENV, "trusted_proxy_hops", 1)
    assert throttle.client_key(request) == "10.0.0.1"
    monkeypatch.setattr(settings.ENV, "trusted_proxy_hops", 2)
    assert throttle.client_key(request) == "198.51.100.2"
    assert throttle.client_key(rf.get("/", headers={"X-Forwarded-For": "1.2.3.4"})) == "local"
    assert throttle.client_key(rf.get("/")) == "local"


# --- the sliding session (§4.1) ------------------------------------------------------------------


def _set_session(key: str, *, seen: float | None, expire_in: timedelta) -> Session:
    """Give a stored session a `_seen` stamp and an expiry."""
    store = Session.objects.get(session_key=key).get_decoded()
    if seen is None:
        store.pop(middleware.SEEN_KEY, None)
    else:
        store[middleware.SEEN_KEY] = int(seen)
    row = Session.objects.get(session_key=key)
    row.session_data = Session.objects.encode(store)
    row.expire_date = timezone.now() + expire_in
    row.save()
    return row


def _stamp(key: str) -> Any:
    return Session.objects.get(session_key=key).get_decoded().get(middleware.SEEN_KEY)


@pytest.mark.django_db
def test_sliding_session_ignores_ssr(signed_in: Any) -> None:
    client, _ = signed_in()
    key = client.client.cookies[settings.SESSION_COOKIE_NAME].value
    stale = time.time() - 2 * 86400
    before = _set_session(key, seen=stale, expire_in=timedelta(days=5))

    # A server component's fetch: the stamp, the expiry and the cookie stay as they were.
    ssr = client.get("/api/me", headers={"X-Foray-SSR": "1"})
    assert ssr.status_code == 200
    assert session_cookie(ssr) is None
    after = Session.objects.get(session_key=key)
    assert after.expire_date == before.expire_date
    assert _stamp(key) == int(stale)

    # The next browser request slides it: a fresh stamp, a 7-day expiry and a new cookie.
    browser = client.get("/api/me")
    cookie = session_cookie(browser)
    assert cookie is not None and cookie.value == key
    assert cookie["max-age"] == 604800
    slid = Session.objects.get(session_key=key)
    assert slid.expire_date > timezone.now() + timedelta(days=6, hours=23)
    assert time.time() - _stamp(key) < 60

    # Once slid, it stays put for a day: no write, no cookie.
    assert session_cookie(client.get("/api/me")) is None


@pytest.mark.django_db
def test_sliding_session_rules(signed_in: Any, api: Api) -> None:
    client, _ = signed_in()
    key = client.client.cookies[settings.SESSION_COOKIE_NAME].value

    _set_session(key, seen=time.time() - 3600, expire_in=timedelta(days=6))
    assert session_cookie(client.get("/api/me")) is None, "an hour-old stamp does not slide"

    # /api/chat never slides: its request thread must not touch the database.
    _set_session(key, seen=None, expire_in=timedelta(days=6))
    chat = client.post("/api/chat", {})
    assert chat.status_code == 503
    assert session_cookie(chat) is None
    assert _stamp(key) is None

    # Any other request with the stale (missing) stamp slides, a GET with no auth included.
    assert session_cookie(client.get("/api/health")) is not None

    # Signing in stamps the new session, so the next request does not slide again.
    make_user(email="fresh@example.test", password=PASSWORD)
    signed = api.sign_in("fresh@example.test", PASSWORD)
    fresh_key = session_cookie(signed).value
    assert time.time() - _stamp(fresh_key) < 60
    assert session_cookie(api.get("/api/me")) is None

    # Signing out leaves no session behind, stamped or not.
    _set_session(fresh_key, seen=None, expire_in=timedelta(days=6))
    out = api.post("/api/auth/sign-out")
    assert session_cookie(out).value == ""
    assert not Session.objects.filter(session_key=fresh_key).exists()


@pytest.mark.django_db(transaction=True)
async def test_sliding_session_async_path(async_client: AsyncClient) -> None:
    """Under ASGI the whole middleware chain is async: the same rules hold there."""
    user = await User.objects.acreate(email="async@example.test", name="Async")
    await async_client.aforce_login(user)  # pyright: ignore[reportAttributeAccessIssue]
    key = async_client.cookies[settings.SESSION_COOKIE_NAME].value

    ssr = await async_client.get("/api/me", headers={"X-Foray-SSR": "1"})
    assert ssr.status_code == 200
    assert session_cookie(ssr) is None

    browser = await async_client.get("/api/me")
    cookie = session_cookie(browser)
    assert cookie is not None and cookie.value == key and cookie["max-age"] == 604800

    refused = await async_client.post(
        "/api/auth/sign-out", data={}, content_type="application/json"
    )
    assert refused.status_code == 403

    out = await async_client.post(
        "/api/auth/sign-out", data={}, content_type="application/json", headers={"Origin": ORIGIN}
    )
    assert out.status_code == 200
    assert session_cookie(out).value == ""
    assert not await Session.objects.filter(session_key=key).aexists()


# --- dev_session (§4.6) --------------------------------------------------------------------------


@pytest.mark.django_db
def test_dev_session_output_shape(debug: None) -> None:
    oldest = make_user(email="oldest@example.test", created_at=timezone.now() - timedelta(days=3))
    make_user(email="newer@example.test")

    out = io.StringIO()
    call_command("dev_session", stdout=out)
    lines = out.getvalue().splitlines()
    assert lines[0] == "user: oldest@example.test"
    match = re.fullmatch(r"cookie: sessionid=([a-z0-9]{32})", lines[1])
    assert match, lines
    assert len(lines) == 2

    # The CLAUDE.md recipe: the part after "cookie: " is a working cookie.
    client = Client()
    name, _, value = lines[1].removeprefix("cookie: ").partition("=")
    client.cookies[name] = value
    # Its session has no stamp yet: an SSR fetch leaves it alone, the first browser request
    # slides it and so hands the browser a real cookie.
    ssr = client.get("/api/me", headers={"X-Foray-SSR": "1"})
    assert ssr.status_code == 200
    assert ssr.json()["id"] == oldest.pk
    assert session_cookie(ssr) is None
    browser = client.get("/api/me")
    assert browser.json()["id"] == oldest.pk
    assert session_cookie(browser).value == value

    named = io.StringIO()
    call_command("dev_session", "NEWER@example.test", stdout=named)
    assert named.getvalue().splitlines()[0] == "user: newer@example.test"


@pytest.mark.django_db
def test_dev_session_refusals(settings: Any) -> None:
    err = io.StringIO()
    settings.DEBUG = True
    with pytest.raises(SystemExit) as no_users:
        call_command("dev_session", stderr=err)
    assert no_users.value.code == 1
    assert err.getvalue().strip() == "No users yet."

    make_user(email="someone@example.test")
    err = io.StringIO()
    with pytest.raises(SystemExit):
        call_command("dev_session", "missing@example.test", stderr=err)
    assert err.getvalue().strip() == "No user with email missing@example.test."

    settings.DEBUG = False
    err = io.StringIO()
    with pytest.raises(SystemExit):
        call_command("dev_session", stderr=err)
    assert err.getvalue().strip() == "dev_session is for local development only."
    assert not Session.objects.exists()
