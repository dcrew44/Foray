"""Foray's own middleware. Each one is sync- and async-capable, so the async chat view keeps an
async chain under ASGI.

- `stream_headers` sets `Cache-Control: no-cache, no-transform` and `X-Accel-Buffering: no` on
  every `text/event-stream` response. Without `no-transform`, Next's compression buffers a
  proxied stream until it ends (research/django-core.md §4.6).
- `origin_check` (§4.3) and `sliding_session` (§4.1) are pass-through stubs here; step 3A fills
  them in.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from asgiref.sync import iscoroutinefunction
from django.http import HttpRequest
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


def _pass_through(get_response: GetResponse):
    if iscoroutinefunction(get_response):

        async def async_middleware(request: HttpRequest) -> HttpResponseBase:
            return await get_response(request)  # type: ignore[misc]

        return async_middleware

    def middleware(request: HttpRequest) -> HttpResponseBase:
        return get_response(request)  # type: ignore[return-value]

    return middleware


@sync_and_async_middleware
def origin_check(get_response: GetResponse):
    """Stub: refuses nothing yet. Step 3A implements the Origin/Fetch-Metadata check (§4.3)."""
    return _pass_through(get_response)


@sync_and_async_middleware
def sliding_session(get_response: GetResponse):
    """Stub: slides nothing yet. Step 3A implements the daily session refresh (§4.1)."""
    return _pass_through(get_response)
