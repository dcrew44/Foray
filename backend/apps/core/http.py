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
- **Conditional requests** (`fetch_conditional`): a board polled every few minutes is asked
  "has this changed?" with the `ETag` and `Last-Modified` its last answer carried, and a `304`
  costs neither side a body (#55). Greenhouse and Lever answer an `ETag`, Ashby a
  `Last-Modified`; a server that sends neither is simply fetched whole.
- **`post_json`** sends a JSON body with the same guard, caps and retries. It exists for
  Workday, whose career sites list their postings in answer to a POST (#57); a retry repeats a
  query, never a write. The one write, a notification published to ntfy (#59), asks for
  `retries=0`.
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
from dataclasses import dataclass
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
# A server that asks for a longer wait than this has blocked us for longer than any run should
# wait (Workable answers a rate-limited address with a day), so retrying early is pointless.
RETRY_AFTER_GIVE_UP_S = 600.0
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


@dataclass(frozen=True, slots=True)
class Validators:
    """What an answer gave us to ask "has this changed?" with."""

    etag: str | None = None
    last_modified: str | None = None

    def __bool__(self) -> bool:
        return bool(self.etag or self.last_modified)

    def headers(self) -> dict[str, str]:
        sent: dict[str, str] = {}
        if self.etag:
            sent["if-none-match"] = self.etag
        if self.last_modified:
            sent["if-modified-since"] = self.last_modified
        return sent


@dataclass(frozen=True, slots=True)
class Fetched:
    """A 2xx answer's body and validators, or (`text is None`) a 304: the copy the caller holds
    is current, and `validators` are the ones it sent."""

    text: str | None
    validators: Validators

    @property
    def not_modified(self) -> bool:
        return self.text is None


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


def _retry_after_seconds(retry_after: str | None) -> float | None:
    """`Retry-After` as seconds, or None when absent or an HTTP-date."""
    if not retry_after:
        return None
    try:
        seconds = float(retry_after.strip())
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) else None


def gives_up(error: HttpError) -> bool:
    """A `Retry-After` beyond `RETRY_AFTER_GIVE_UP_S`: fail now rather than retry in vain."""
    seconds = _retry_after_seconds(error.retry_after)
    return seconds is not None and seconds > RETRY_AFTER_GIVE_UP_S


def backoff_seconds(attempt: int, retry_after: str | None) -> float:
    """`Retry-After` seconds when given (capped at 60), else 0.5 s, 1 s, 2 s ... plus jitter,
    so parallel fetchers do not resynchronise. An HTTP-date falls through to the backoff."""
    seconds = _retry_after_seconds(retry_after)
    if seconds is not None:
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
        self._gates: dict[str, asyncio.Semaphore] = {}
        self._budgets: dict[str, int] = {}

    def gate(self, key: str, limit: int) -> asyncio.Semaphore:
        """A semaphore shared by everything this client does under `key`: how a job keeps to a
        few requests at a time against one source, however many boards it is reading."""
        return self._gates.setdefault(key, asyncio.Semaphore(limit))

    def take(self, key: str, total: int, wanted: int) -> int:
        """Up to `wanted` out of a budget of `total` shared by everything this client does under
        `key`: how one job keeps to so many requests of a kind, however many boards want them."""
        left = self._budgets.setdefault(key, total)
        granted = max(0, min(wanted, left))
        self._budgets[key] = left - granted
        return granted

    def left(self, key: str, total: int) -> int:
        """What remains of `key`'s budget of `total`, without taking any."""
        return self._budgets.get(key, total)

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
        fetched = await self._fetch(
            url,
            validators=Validators(),
            timeout=timeout,
            retries=retries,
            accept=accept,
            headers=headers,
            max_bytes=max_bytes,
            on_attempt=on_attempt,
        )
        assert fetched.text is not None  # a 304 is only an answer to a conditional request
        return fetched.text

    async def fetch_conditional(
        self,
        url: str,
        validators: Validators,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,  # noqa: ASYNC109 (§10.1: per attempt, asyncio.timeout)
        retries: int = DEFAULT_RETRIES,
        accept: str = JSON_ACCEPT,
        headers: Mapping[str, str] | None = None,
        max_bytes: int | None = None,
    ) -> Fetched:
        """`fetch_text` that sends `validators` and takes a 304 for an answer. The result
        carries the validators to send next time; with none to send it is a plain fetch."""
        return await self._fetch(
            url,
            validators=validators,
            timeout=timeout,
            retries=retries,
            accept=accept,
            headers=headers,
            max_bytes=max_bytes,
            on_attempt=None,
        )

    async def post_json(
        self,
        url: str,
        body: Any,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,  # noqa: ASYNC109 (§10.1: per attempt, asyncio.timeout)
        retries: int = DEFAULT_RETRIES,
        headers: Mapping[str, str] | None = None,
        max_bytes: int | None = None,
    ) -> str:
        """The body of a 2xx answer to a JSON POST. For read-only queries that happen to be
        POSTs: it is retried like a GET."""
        fetched = await self._fetch(
            url,
            validators=Validators(),
            timeout=timeout,
            retries=retries,
            accept=JSON_ACCEPT,
            headers={**(headers or {}), "content-type": "application/json"},
            max_bytes=max_bytes,
            on_attempt=None,
            content=json.dumps(body, separators=(",", ":")).encode(),
        )
        assert fetched.text is not None
        return fetched.text

    async def fetch_range(
        self,
        url: str,
        start: int,
        length: int,
        *,
        timeout: float = 60.0,  # noqa: ASYNC109 (§10.1: per attempt, asyncio.timeout)
        retries: int = DEFAULT_RETRIES,
    ) -> tuple[bytes, int | None]:
        """`length` bytes of a file from `start`, undecoded, and the file's size when the
        server gives it: how a large static index is read a piece at a time. Only a `206` is
        an answer (a server that ignores the range would send the whole file), and no redirect
        is followed."""
        target = await self.check_url(url)
        headers = {"user-agent": USER_AGENT, "range": f"bytes={start}-{start + length - 1}"}
        for attempt in range(retries + 1):
            last = attempt == retries
            try:
                async with (
                    asyncio.timeout(timeout),
                    self._client.stream("GET", target, headers=headers, timeout=timeout) as answer,
                ):
                    if answer.status_code != 206:
                        raise HttpError(
                            answer.status_code, url, retry_after=answer.headers.get("retry-after")
                        )
                    body = await _read_capped(answer, length, url)
                    size = answer.headers.get("content-range", "").rpartition("/")[2]
                    return body, int(size) if size.isdigit() else None
            except HttpError as error:
                if not is_retryable(error.status) or last or gives_up(error):
                    raise
                await _sleep(backoff_seconds(attempt, error.retry_after))
            except (httpx2.RequestError, TimeoutError, OSError) as error:
                if last:
                    raise NetworkError(f"Could not fetch {url}: {_reason(error)}") from error
                await _sleep(backoff_seconds(attempt, None))
        raise AssertionError("unreachable")  # the last attempt always returns or raises

    async def status_of(
        self,
        url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,  # noqa: ASYNC109 (one attempt, asyncio.timeout)
        accept: str = HTML_ACCEPT,
    ) -> tuple[int, str | None]:
        """`(status, Location)` of one guarded GET that follows no redirect and reads no body.
        For telling "exists" from "redirects away" where the body says nothing (SmartRecruiters
        answers an unknown company with an empty list, #57). Raises `HttpError("blocked")` or
        `NetworkError`."""
        current = await self.check_url(url)
        try:
            async with (
                asyncio.timeout(timeout),
                self._client.stream(
                    "GET",
                    current,
                    headers={"user-agent": USER_AGENT, "accept": accept},
                    timeout=timeout,
                ) as response,
            ):
                return response.status_code, response.headers.get("location")
        except (httpx2.RequestError, TimeoutError, OSError) as error:
            raise NetworkError(f"Could not fetch {url}: {_reason(error)}") from error

    async def _fetch(
        self,
        url: str,
        *,
        validators: Validators,
        timeout: float,  # noqa: ASYNC109 (§10.1: per attempt, asyncio.timeout)
        retries: int,
        accept: str,
        headers: Mapping[str, str] | None,
        max_bytes: int | None,
        on_attempt: Callable[[int], Awaitable[None]] | None,
        content: bytes | None = None,
    ) -> Fetched:
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
                        headers={**(headers or {}), **validators.headers()},
                        validators=validators,
                        timeout=timeout,
                        html=html,
                        max_bytes=cap,
                        content=content,
                    )
            except HttpError as error:
                if not is_retryable(error.status) or last or gives_up(error):
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
        validators: Validators,
        timeout: float,  # noqa: ASYNC109 (the caller wraps the attempt in asyncio.timeout)
        html: bool,
        max_bytes: int,
        content: bytes | None = None,
    ) -> Fetched:
        current = await self.check_url(url)
        origin = URL(current).origin
        for hop in range(MAX_REDIRECTS + 1):
            # The caller's headers only while the hop stays on the origin they were meant for,
            # and its body only there too: a redirect elsewhere is answered with a plain GET.
            same_origin = URL(current).origin == origin
            sent = {**base_headers, **headers} if same_origin else base_headers
            posting = content is not None and same_origin
            async with self._client.stream(
                "POST" if posting else "GET",
                current,
                headers=sent,
                content=content if posting else None,
                timeout=timeout,
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

                # A 304 answers only a hop that carried the validators (a hop to another
                # origin did not); anywhere else it is an answer we cannot use.
                if response.status_code == 304 and (
                    "if-none-match" in sent or "if-modified-since" in sent
                ):
                    return Fetched(None, validators)

                if 200 <= response.status_code < 300:
                    if html:
                        _check_html(response, current)
                    body = _decode(await _read_capped(response, max_bytes, current))
                    return Fetched(
                        body,
                        Validators(
                            etag=response.headers.get("etag") or None,
                            last_modified=response.headers.get("last-modified") or None,
                        ),
                    )

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
