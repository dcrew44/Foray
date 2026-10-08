"""Foray's own middleware. Each one is sync- and async-capable, so the async chat view keeps an
async chain under ASGI.

- `stream_headers` sets `Cache-Control: no-cache, no-transform` and `X-Accel-Buffering: no` on
  every `text/event-stream` response. Without `no-transform`, Next's compression buffers a
  proxied stream until it ends (research/django-core.md §4.6).
- `origin_check` (§4.3) refuses a cross-site unsafe request under `/api/`. There is no CSRF
  token: the browser always reaches Django same-origin through the Next rewrite, the session
  cookie is `SameSite=Lax`, and this applies Better Auth's Origin and Fetch-Metadata check.
- `sliding_session` (§4.1) keeps the 7-day session rolling: a signed-in request whose `_seen`
  stamp is more than a day old rewrites it, so Django saves the session with a fresh
  `expire_date` and re-sends the cookie. One session write per user per day.

Under ASGI the whole chain runs in async mode (every Django middleware here is async-capable), so
each middleware has an async path that never touches the ORM synchronously.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

from asgiref.sync import iscoroutinefunction
from django.conf import settings
from django.contrib.auth import SESSION_KEY
from django.contrib.sessions.backends.base import SessionBase
from django.http import HttpRequest, HttpResponse
from django.http.response import HttpResponseBase
from django.utils.decorators import sync_and_async_middleware

type GetResponse = Callable[[HttpRequest], HttpResponseBase | Awaitable[HttpResponseBase]]


def _set_stream_headers(response: HttpResponseBase) -> HttpResponseBase:
    if response.get("Content-Type", "").startswith("text/event-stream"):
        response["Cache-Control"] = "no-cache, no-transform"
        response["X-Accel-Buffering"] = "no"
    return response


@sync_and_async_middleware
def stream_headers(get_response: GetResponse):
    if iscoroutinefunction(get_response):

        async def async_middleware(request: HttpRequest) -> HttpResponseBase:
            return _set_stream_headers(await get_response(request))  # type: ignore[misc]

        return async_middleware

    def middleware(request: HttpRequest) -> HttpResponseBase:
        return _set_stream_headers(get_response(request))  # type: ignore[arg-type]

    return middleware


# --- origin_check (§4.3) -------------------------------------------------------------------------

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
REFUSED_BODY = b'{"ok":false,"message":"Cross-site request refused."}'


def _origin_of(url: str) -> str | None:
    """`scheme://host[:port]` of a URL, lowercased, or None when it has no origin."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if not parts.scheme or not parts.hostname:
        return None
    origin = f"{parts.scheme.lower()}://{parts.hostname}"
    return f"{origin}:{port}" if port is not None else origin


def _allowed(origin: str | None) -> bool:
    return origin is not None and origin in {o.lower() for o in settings.ALLOWED_ORIGINS}


def cross_site(request: HttpRequest) -> bool:
    """Whether an unsafe `/api/` request must be refused:

    - `Sec-Fetch-Site: cross-site` is refused;
    - otherwise an `Origin` must be allowed (`Origin: null` never is);
    - otherwise, with no `Origin`, the `Referer`'s origin must be allowed;
    - otherwise a request with neither header passes only if it carries no session cookie (curl,
      tests), because a browser always sends one of them on these methods.
    """
    if request.method not in UNSAFE_METHODS or not request.path_info.startswith("/api/"):
        return False
    if request.headers.get("Sec-Fetch-Site", "").strip().lower() == "cross-site":
        return True
    origin = request.headers.get("Origin")
    if origin is not None:
        return not _allowed(origin.strip().lower())
    referer = request.headers.get("Referer")
    if referer is not None:
        return not _allowed(_origin_of(referer.strip()))
    return settings.SESSION_COOKIE_NAME in request.COOKIES


def _refused() -> HttpResponse:
    return HttpResponse(REFUSED_BODY, status=403, content_type="application/json")


@sync_and_async_middleware
def origin_check(get_response: GetResponse):
    if iscoroutinefunction(get_response):

        async def async_middleware(request: HttpRequest) -> HttpResponseBase:
            if cross_site(request):
                return _refused()
            return await get_response(request)  # type: ignore[misc]

        return async_middleware

    def middleware(request: HttpRequest) -> HttpResponseBase:
        if cross_site(request):
            return _refused()
        return get_response(request)  # type: ignore[return-value]

    return middleware


# --- sliding_session (§4.1) ----------------------------------------------------------------------

SEEN_KEY = "_seen"  # epoch seconds of the session's last slide (or login)
SLIDE_AFTER_S = 86400
SSR_HEADER = "X-Foray-SSR"


def stamp_session(session: SessionBase) -> None:
    """Mark a session as just refreshed. Sign-in and sign-up call it, so a fresh login does not
    slide again for a day. A session with no stamp (`manage.py dev_session`) slides on its first
    browser request, which gives the browser a real cookie."""
    session[SEEN_KEY] = int(time.time())


def is_stale(seen: Any) -> bool:
    if isinstance(seen, bool) or not isinstance(seen, int | float):
        return True
    return time.time() - seen > SLIDE_AFTER_S


def slides(request: HttpRequest) -> bool:
    """Whether this request may slide its session at all:

    - not `/api/chat`, so the streaming view's request thread never touches the database;
    - not a server component's fetch (`X-Foray-SSR: 1`, sent by Next's `django()` helper): the
      `Set-Cookie` would land on the Next server and be dropped, moving the database expiry while
      the browser cookie kept its login-time `Max-Age`;
    - only with a session cookie (a request without one has nothing to slide; sign-in stamps its
      own new session).
    """
    path = request.path_info
    if path == "/api/chat" or path.startswith("/api/chat/"):
        return False
    if request.headers.get(SSR_HEADER) == "1":
        return False
    return settings.SESSION_COOKIE_NAME in request.COOKIES


@sync_and_async_middleware
def sliding_session(get_response: GetResponse):
    if iscoroutinefunction(get_response):

        async def async_middleware(request: HttpRequest) -> HttpResponseBase:
            response = await get_response(request)  # type: ignore[misc]
            # Django's async session API (aget, ahas_key, aset); django-types does not know it.
            session: Any = request.session
            if (
                slides(request)
                and is_stale(await session.aget(SEEN_KEY))
                and await session.ahas_key(SESSION_KEY)
            ):
                # auser() re-reads the session, so a sign-out in this request (which flushed it)
                # reads as anonymous. It costs one user query, once a day per session.
                user = await request.auser()  # type: ignore[attr-defined]
                if user.is_authenticated:
                    await session.aset(SEEN_KEY, int(time.time()))
            return response

        return async_middleware

    def middleware(request: HttpRequest) -> HttpResponseBase:
        response = get_response(request)
        session = request.session
        if (
            slides(request)
            and is_stale(session.get(SEEN_KEY))
            and SESSION_KEY in session
            and request.user.is_authenticated
        ):
            stamp_session(session)
        return response  # type: ignore[return-value]

    return middleware
