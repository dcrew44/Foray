"""The one outbound HTTP client (design §10.1). This is the only module that imports httpx2.

Every request to a job board, a careers page, JSearch or Brave goes through `HttpClient`, so
politeness and safety are not per-caller decisions:

- **A real User-Agent,** a per-attempt timeout, and backoff that tells "slow down" from
  "broken": 408, 429 and 5xx are retried after `2**n * 0.5 s + U(0, 0.25)`, or after the
  response's `Retry-After` seconds (capped at 60 s); a 404 raises `HttpError(404)` at once;
  network errors and timeouts are retried like a 5xx.
- **Response caps,** streamed and aborted when exceeded: ATS JSON 64 MB (the design said 32;
  Anduril's Greenhouse board alone is 43 MB, deviation step-3C #1), JSearch 5 MB, HTML 3 MB.
  HTML fetches also check the content type.
- **Redirects are followed by hand,** at most 5, so every hop is re-validated. A caller's own
  headers (an API key such as JSearch's `x-rapidapi-key` or Brave's token) go only to the
  origin they were meant for: a hop to another scheme, host or port gets the User-Agent and
  Accept alone, as httpx2 drops `Authorization` across origins when it follows redirects itself.
- **`on_attempt`** is awaited before every attempt, the first and each retry, so a metered API
  (JSearch's monthly quota) counts every request actually sent.
- **An SSRF guard on every request and every hop.** Users type URLs (add company) and the model
  emits them (careers-page `more_listing_urls`). Only http/https on ports 80/443 are fetched,
  and a host is refused when any address it resolves to is loopback, private, link-local
  (including 169.254.169.254), CGNAT, multicast, reserved or unspecified, or the IPv4-mapped
  form of one of these. A refusal raises `HttpError("blocked")`, whose message is the sentence
  discovery records in its trail. DNS pinning (closing the resolve-then-connect window) is
  deferred to Phase 4.

One `HttpClient` serves one job: `async with HttpClient() as client: ...`. Tests pass a
`transport` (`httpx2.MockTransport`) and a `resolve` function, so nothing reaches DNS or the
network.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import math
import random
import socket
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Self

import httpx2
from ada_url import URL, join_url
from django.conf import settings

log = logging.getLogger("foray.http")

USER_AGENT = f"Foray/0.2 (job search agent; +{settings.PUBLIC_ORIGIN})"

JSON_ACCEPT = "application/json"
HTML_ACCEPT = "text/html,application/xhtml+xml"
HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")

MB = 1024 * 1024
ATS_MAX_BYTES = 64 * MB  # a 2,466-posting Greenhouse board with content is 43 MB
JSEARCH_MAX_BYTES = 5 * MB
HTML_MAX_BYTES = 3 * MB

DEFAULT_TIMEOUT_S = 20.0
DEFAULT_RETRIES = 3
RETRY_AFTER_CAP_S = 60.0
MAX_REDIRECTS = 5
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
ERROR_BODY_CHARS = 2000
_ERROR_BODY_BYTES = 16 * 1024

BLOCKED_MESSAGE = "That address is not reachable from Foray."

_CGNAT = ipaddress.ip_network("100.64.0.0/10")

type Resolver = Callable[[str, int], Awaitable[Sequence[str]]]


class FetchError(Exception):
    """A URL could not be fetched. Callers that do not care why catch this."""


class HttpError(FetchError):
    """An answer we will not use.

    `status` is the HTTP status, or one of `"blocked"` (the SSRF guard), `"too-large"` (a
    response cap), `"content-type"` (an HTML fetch that is not HTML), `"redirects"` (more than
    five hops) and `"invalid"` (not a URL).
    """

    def __init__(
        self, status: int | str, url: str, body: str = "", *, retry_after: str | None = None
    ) -> None:
        self.status = status
        self.url = url
        self.body = body
        self.retry_after = retry_after
        super().__init__(_describe(status, url))

    @property
    def blocked(self) -> bool:
        return self.status == "blocked"


class NetworkError(FetchError):
    """A timeout, a refused connection or a failed DNS lookup, after the retries."""


def _describe(status: int | str, url: str) -> str:
    match status:
        case "blocked":
            return BLOCKED_MESSAGE
        case "too-large":
            return f"The response from {url} is too large."
        case "content-type":
            return f"{url} is not a web page."
        case "redirects":
            return f"Too many redirects for {url}"
        case "invalid":
            return f"Not a URL: {url}"
        case _:
            return f"HTTP {status} for {url}"


def is_retryable(status: int | str) -> bool:
    """429 and 5xx are worth retrying; a 404 means the thing is simply not there."""
    return isinstance(status, int) and (status in (408, 429) or status >= 500)


def backoff_seconds(attempt: int, retry_after: str | None) -> float:
    """`Retry-After` seconds when given (capped at 60), else 0.5 s, 1 s, 2 s ... plus jitter,
    so parallel fetchers do not resynchronise."""
    if retry_after:
        try:
            seconds = float(retry_after.strip())
        except ValueError:
            seconds = math.nan  # an HTTP-date falls through to the exponential backoff
        if math.isfinite(seconds):
            return max(0.0, min(seconds, RETRY_AFTER_CAP_S))
    return 2**attempt * 0.5 + random.uniform(0, 0.25)


async def _sleep(seconds: float) -> None:
    # Looked up at call time, so tests can skip the waiting.
    await asyncio.sleep(seconds)


# --- the SSRF guard ------------------------------------------------------------------------------


def is_blocked_address(address: str) -> bool:
    """Loopback, private, link-local, CGNAT, multicast, reserved or unspecified (or the
    IPv4-mapped form of one of these)."""
    try:
        ip: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(
            address.split("%", 1)[0]
        )
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or (isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT)
        or (isinstance(ip, ipaddress.IPv6Address) and ip.is_site_local)
    )


async def system_resolve(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


# --- the client ----------------------------------------------------------------------------------


class HttpClient:
    """One `httpx2.AsyncClient` with Foray's rules. Use one per job and close it."""

    def __init__(
        self,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        resolve: Resolver | None = None,
    ) -> None:
        # Redirects are followed by hand (every hop is checked); proxies from the environment
        # are ignored, as Node's fetch ignores them.
        self._client = httpx2.AsyncClient(
            transport=transport, follow_redirects=False, trust_env=False
        )
        self._resolve = resolve or system_resolve

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch_json(
        self,
        url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,  # noqa: ASYNC109 (§10.1: per attempt, asyncio.timeout)
        retries: int = DEFAULT_RETRIES,
        headers: Mapping[str, str] | None = None,
        max_bytes: int = ATS_MAX_BYTES,
    ) -> Any:
        text = await self.fetch_text(
            url, timeout=timeout, retries=retries, headers=headers, max_bytes=max_bytes
        )
        return json.loads(text)

    async def fetch_text(
        self,
        url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,  # noqa: ASYNC109 (§10.1: per attempt, asyncio.timeout)
        retries: int = DEFAULT_RETRIES,
        accept: str = JSON_ACCEPT,
        headers: Mapping[str, str] | None = None,
        max_bytes: int | None = None,
        on_attempt: Callable[[int], Awaitable[None]] | None = None,
    ) -> str:
        """The body of a 2xx answer, decoded as UTF-8. Raises `HttpError` or `NetworkError`.
        `on_attempt(n)` is awaited before attempt n (0 first); what it raises propagates."""
        html = accept.startswith("text/html")
        cap = max_bytes if max_bytes is not None else (HTML_MAX_BYTES if html else ATS_MAX_BYTES)
        base_headers = {"user-agent": USER_AGENT, "accept": accept}

        for attempt in range(retries + 1):
            last = attempt == retries
            if on_attempt is not None:
                await on_attempt(attempt)
            try:
                async with asyncio.timeout(timeout):
                    return await self._attempt(
                        url,
                        base_headers=base_headers,
                        headers=dict(headers or {}),
                        timeout=timeout,
                        html=html,
                        max_bytes=cap,
                    )
            except HttpError as error:
                if not is_retryable(error.status) or last:
                    raise
                await _sleep(backoff_seconds(attempt, error.retry_after))
            except (httpx2.RequestError, TimeoutError, OSError) as error:
                if last:
                    raise NetworkError(f"Could not fetch {url}: {_reason(error)}") from error
                await _sleep(backoff_seconds(attempt, None))
        raise AssertionError("unreachable")  # the last attempt always returns or raises

    async def _attempt(
        self,
        url: str,
        *,
        base_headers: dict[str, str],
        headers: dict[str, str],
        timeout: float,  # noqa: ASYNC109 (the caller wraps the attempt in asyncio.timeout)
        html: bool,
        max_bytes: int,
    ) -> str:
        current = await self.check_url(url)
        origin = URL(current).origin
        for hop in range(MAX_REDIRECTS + 1):
            # The caller's headers only while the hop stays on the origin they were meant for.
            sent = {**base_headers, **headers} if URL(current).origin == origin else base_headers
            async with self._client.stream(
                "GET", current, headers=sent, timeout=timeout
            ) as response:
                location = response.headers.get("location")
                if response.status_code in REDIRECT_STATUSES and location:
                    if hop == MAX_REDIRECTS:
                        raise HttpError("redirects", url)
                    try:
                        target = join_url(current, location)
                    except ValueError:
                        raise HttpError("invalid", location) from None
                    current = await self.check_url(target)
                    continue

                if 200 <= response.status_code < 300:
                    if html:
                        _check_html(response, current)
                    return _decode(await _read_capped(response, max_bytes, current))

                prefix = await _read_prefix(response, _ERROR_BODY_BYTES)
                raise HttpError(
                    response.status_code,
                    url,
                    _decode(prefix)[:ERROR_BODY_CHARS],
                    retry_after=response.headers.get("retry-after"),
                )
        raise HttpError("redirects", url)

    async def check_url(self, url: str) -> str:
        """The WHATWG-serialised URL when it may be fetched, else `HttpError("blocked")`."""
        try:
            parsed = URL(url)
        except ValueError:
            raise HttpError("invalid", url) from None
        if parsed.protocol not in ("http:", "https:"):
            raise HttpError("blocked", url)
        if parsed.port not in ("", "80", "443"):
            raise HttpError("blocked", url)

        host = parsed.hostname
        bare = host[1:-1] if host.startswith("[") and host.endswith("]") else host
        if not bare or bare == "localhost" or bare.endswith(".localhost"):
            raise HttpError("blocked", url)

        try:
            ipaddress.ip_address(bare)
        except ValueError:
            port = int(parsed.port) if parsed.port else (443 if parsed.protocol == "https:" else 80)
            addresses = await self._resolve(bare, port)
            if not addresses or any(is_blocked_address(address) for address in addresses):
                raise HttpError("blocked", url) from None
        else:
            if is_blocked_address(bare):
                raise HttpError("blocked", url)
        return parsed.href


def _check_html(response: httpx2.Response, url: str) -> None:
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type and content_type not in HTML_CONTENT_TYPES:
        raise HttpError("content-type", url)


async def _read_capped(response: httpx2.Response, max_bytes: int, url: str) -> bytes:
    declared = response.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise HttpError("too-large", url)
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise HttpError("too-large", url)
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_prefix(response: httpx2.Response, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    try:
        async for chunk in response.aiter_bytes():
            chunks.append(chunk)
            total += len(chunk)
            if total >= limit:
                break
    except httpx2.RequestError:
        pass  # an error body that fails to arrive is not worth more than the status
    return b"".join(chunks)[:limit]


def _decode(body: bytes) -> str:
    # What `response.text()` does in Node: UTF-8 whatever the charset says, a leading BOM
    # dropped, invalid bytes replaced.
    return body.decode("utf-8-sig", errors="replace")


def _reason(error: BaseException) -> str:
    if isinstance(error, TimeoutError | httpx2.TimeoutException):
        return "timed out"
    return str(error) or type(error).__name__
