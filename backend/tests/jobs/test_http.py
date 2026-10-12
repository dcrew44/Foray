"""The one outbound HTTP client (design §10.1): SSRF refusals, manual redirects, response caps,
the content-type check, retries and backoff."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx2
import pytest

from apps.core import http
from apps.core.http import (
    BLOCKED_MESSAGE,
    HTML_ACCEPT,
    HttpError,
    NetworkError,
    backoff_seconds,
    is_blocked_address,
)
from tests.jobs.conftest import html, mock_client


def _never(request: httpx2.Request) -> httpx2.Response:
    raise AssertionError(f"no request may be sent ({request.url})")


# --- the SSRF guard ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/admin",
        "http://foo.localhost/",
        "http://127.1/",
        "http://127.0.0.1:80/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://100.64.0.1/",
        "http://[::ffff:127.0.0.1]/",
        "http://0.0.0.0/",
        "http://224.0.0.1/",
        "http://0x7f.1/",
        "ftp://example.com/",
        "http://example.com:8080/",
        "file:///etc/passwd",
    ],
)
async def test_refuses_internal_and_odd_addresses(url: str) -> None:
    async with mock_client(_never) as client:
        with pytest.raises(HttpError) as raised:
            await client.fetch_text(url)
    assert raised.value.status == "blocked"
    assert raised.value.blocked
    assert str(raised.value) == BLOCKED_MESSAGE


async def test_refuses_a_name_that_resolves_inside_the_network() -> None:
    async with mock_client(_never, addresses={"intranet.example.com": ["10.1.2.3"]}) as client:
        with pytest.raises(HttpError) as raised:
            await client.fetch_text("https://intranet.example.com/jobs")
    assert raised.value.status == "blocked"


async def test_refuses_a_name_with_any_internal_address() -> None:
    addresses = {"mixed.example.com": ["93.184.215.14", "127.0.0.1"]}
    async with mock_client(_never, addresses=addresses) as client:
        with pytest.raises(HttpError, match=BLOCKED_MESSAGE):
            await client.fetch_text("https://mixed.example.com/")


async def test_refuses_a_redirect_to_an_internal_address() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == "example.com", "the internal hop is never requested"
        return httpx2.Response(302, headers={"location": "http://169.254.169.254/latest"})

    async with mock_client(handler) as client:
        with pytest.raises(HttpError) as raised:
            await client.fetch_text("https://example.com/")
    assert raised.value.status == "blocked"


async def test_allows_a_public_address_on_the_default_ports() -> None:
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(str(request.url))
        return httpx2.Response(200, text="ok")

    async with mock_client(handler) as client:
        assert await client.fetch_text("https://Example.COM:443/a b") == "ok"
        assert await client.fetch_text("http://example.com:80/") == "ok"
    # The WHATWG-serialised URL is what gets requested.
    assert seen == ["https://example.com/a%20b", "http://example.com/"]


@pytest.mark.parametrize(
    ("address", "blocked"),
    [
        ("8.8.8.8", False),
        ("2606:4700:4700::1111", False),
        ("127.0.0.1", True),
        ("::1", True),
        ("10.0.0.1", True),
        ("172.16.0.1", True),
        ("192.168.0.1", True),
        ("169.254.169.254", True),
        ("100.64.0.1", True),
        ("100.127.255.254", True),
        ("::ffff:10.0.0.1", True),
        ("fe80::1", True),
        ("fc00::1", True),
        ("ff02::1", True),
        ("0.0.0.0", True),
        ("240.0.0.1", True),
        ("not an address", True),
    ],
)
def test_blocked_address_table(address: str, blocked: bool) -> None:
    assert is_blocked_address(address) is blocked


# --- redirects -----------------------------------------------------------------------------------


async def test_follows_redirects_by_hand_and_checks_every_hop() -> None:
    hops = {
        "https://a.example.com/": "https://b.example.com/start",
        "https://b.example.com/start": "/next",
        "https://b.example.com/next": "https://c.example.com/end",
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        target = hops.get(str(request.url))
        if target:
            return httpx2.Response(301, headers={"location": target})
        return httpx2.Response(200, text="arrived")

    client = mock_client(handler)
    async with client:
        assert await client.fetch_text("https://a.example.com/") == "arrived"
    resolved = client._resolve.calls  # type: ignore[attr-defined]
    assert resolved == ["a.example.com", "b.example.com", "b.example.com", "c.example.com"]


async def test_a_callers_headers_never_follow_a_redirect_to_another_origin() -> None:
    """An API key goes only where it was meant to: a 302 from the API to another host (a
    misbehaving or compromised upstream) gets the User-Agent and Accept alone. A redirect that
    stays on the origin keeps them."""
    seen: list[httpx2.Request] = []
    hops = {
        "https://jsearch.p.rapidapi.com/search": "/search-v2",
        "https://jsearch.p.rapidapi.com/search-v2": "https://collector.example.net/x",
        "https://collector.example.net/x": "http://jsearch.p.rapidapi.com/back",
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        target = hops.get(str(request.url))
        if target:
            return httpx2.Response(302, headers={"location": target})
        return httpx2.Response(200, text="done")

    secret = {"x-rapidapi-key": "SECRET", "authorization": "Bearer SECRET"}
    async with mock_client(handler) as client:
        assert await client.fetch_text("https://jsearch.p.rapidapi.com/search", headers=secret)
    sent = {str(request.url): request.headers for request in seen}
    for same_origin in (
        "https://jsearch.p.rapidapi.com/search",
        "https://jsearch.p.rapidapi.com/search-v2",
    ):
        assert sent[same_origin]["x-rapidapi-key"] == "SECRET"
        assert sent[same_origin]["authorization"] == "Bearer SECRET"
    # Another host, and the same host over another scheme: no caller header at all.
    for other in ("https://collector.example.net/x", "http://jsearch.p.rapidapi.com/back"):
        headers = sent[other]
        assert "x-rapidapi-key" not in headers and "authorization" not in headers
        assert (
            headers["user-agent"].startswith("Foray/") and headers["accept"] == "application/json"
        )


@pytest.mark.usefixtures("no_backoff")
async def test_on_attempt_runs_before_every_attempt() -> None:
    answers = [httpx2.Response(503, text="busy"), httpx2.Response(200, text="fine")]
    sent: list[int] = []
    attempts: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(len(attempts))
        return answers.pop(0)

    async def counted(attempt: int) -> None:
        attempts.append(attempt)

    async with mock_client(handler) as client:
        assert await client.fetch_text("https://flaky.example.com/", on_attempt=counted) == "fine"
    assert attempts == [0, 1]
    assert sent == [1, 2]  # each request went out after its attempt was counted


async def test_stops_after_five_redirects() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        n = int(request.url.path.strip("/") or 0)
        return httpx2.Response(302, headers={"location": f"/{n + 1}"})

    async with mock_client(handler) as client:
        with pytest.raises(HttpError) as raised:
            await client.fetch_text("https://loop.example.com/0")
    assert raised.value.status == "redirects"


async def test_five_redirects_are_fine() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        n = int(request.url.path.strip("/"))
        if n == 5:
            return httpx2.Response(200, text="five")
        return httpx2.Response(307, headers={"location": f"/{n + 1}"})

    async with mock_client(handler) as client:
        assert await client.fetch_text("https://hops.example.com/0") == "five"


# --- response caps and content types -------------------------------------------------------------


async def test_a_declared_length_over_the_cap_is_refused() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b"x" * 2048)

    async with mock_client(handler) as client:
        with pytest.raises(HttpError) as raised:
            await client.fetch_text("https://big.example.com/", max_bytes=1024)
        assert await client.fetch_text("https://big.example.com/", max_bytes=4096) == "x" * 2048
    assert raised.value.status == "too-large"


async def test_a_streamed_body_is_aborted_at_the_cap() -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(100):
            yield b"y" * 1000

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=chunks())

    async with mock_client(handler) as client:
        with pytest.raises(HttpError) as raised:
            await client.fetch_text("https://stream.example.com/", max_bytes=10_000)
    assert raised.value.status == "too-large"


def test_the_caps_are_the_designed_ones() -> None:
    assert http.ATS_MAX_BYTES == 64 * 1024 * 1024  # deviation step-3C #1
    assert http.JSEARCH_MAX_BYTES == 5 * 1024 * 1024
    assert http.HTML_MAX_BYTES == 3 * 1024 * 1024


async def test_html_fetches_check_the_content_type() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/pdf":
            return httpx2.Response(
                200, content=b"%PDF", headers={"content-type": "application/pdf"}
            )
        if request.url.path == "/bare":
            return httpx2.Response(200, content=b"<p>no header</p>")
        return html("<p>page</p>")

    async with mock_client(handler) as client:
        assert await client.fetch_text("https://site.example.com/", accept=HTML_ACCEPT) == (
            "<p>page</p>"
        )
        assert await client.fetch_text("https://site.example.com/bare", accept=HTML_ACCEPT) == (
            "<p>no header</p>"
        )
        with pytest.raises(HttpError) as raised:
            await client.fetch_text("https://site.example.com/pdf", accept=HTML_ACCEPT)
        # JSON fetches do not care.
        assert await client.fetch_text("https://site.example.com/pdf") == "%PDF"
    assert raised.value.status == "content-type"


async def test_decodes_utf8_like_node() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            content="\ufeffcafé ".encode() + b"\xff",
            headers={"content-type": "text/plain; charset=latin-1"},
        )

    async with mock_client(handler) as client:
        assert await client.fetch_text("https://text.example.com/") == "café \ufffd"


async def test_sends_the_user_agent_and_accept_headers() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"ok": True})

    async with mock_client(handler) as client:
        assert await client.fetch_json("https://api.example.com/", headers={"x-key": "k"}) == {
            "ok": True
        }
    headers = seen[0].headers
    assert headers["user-agent"] == "Foray/0.2 (job search agent; +http://localhost:3000)"
    assert headers["accept"] == "application/json"
    assert headers["x-key"] == "k"


# --- retries -------------------------------------------------------------------------------------


@pytest.mark.usefixtures("no_backoff")
async def test_retries_a_5xx_then_succeeds(no_backoff: list[float]) -> None:
    answers = [httpx2.Response(503, text="busy"), httpx2.Response(200, text="fine")]

    def handler(request: httpx2.Request) -> httpx2.Response:
        return answers.pop(0)

    async with mock_client(handler) as client:
        assert await client.fetch_text("https://flaky.example.com/") == "fine"
    assert len(no_backoff) == 1
    assert 0.5 <= no_backoff[0] <= 0.75


async def test_a_404_is_not_retried(no_backoff: list[float]) -> None:
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(404, text="Not Found")

    async with mock_client(handler) as client:
        with pytest.raises(HttpError) as raised:
            await client.fetch_text("https://gone.example.com/")
    assert raised.value.status == 404
    assert str(raised.value) == "HTTP 404 for https://gone.example.com/"
    assert raised.value.body == "Not Found"
    assert calls == 1
    assert no_backoff == []


async def test_honours_retry_after_capped_at_a_minute(no_backoff: list[float]) -> None:
    answers = [
        httpx2.Response(429, headers={"retry-after": "7"}),
        httpx2.Response(429, headers={"retry-after": "120"}),
        httpx2.Response(200, text="ok"),
    ]

    def handler(request: httpx2.Request) -> httpx2.Response:
        return answers.pop(0)

    async with mock_client(handler) as client:
        assert await client.fetch_text("https://busy.example.com/") == "ok"
    assert no_backoff == [7.0, 60.0]


async def test_gives_up_after_the_retries(no_backoff: list[float]) -> None:
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(500, text="broken")

    async with mock_client(handler) as client:
        with pytest.raises(HttpError):
            await client.fetch_text("https://down.example.com/", retries=2)
    assert calls == 3
    assert len(no_backoff) == 2


async def test_network_errors_are_retried_then_raised(no_backoff: list[float]) -> None:
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        raise httpx2.ConnectError("refused", request=request)

    async with mock_client(handler) as client:
        with pytest.raises(NetworkError):
            await client.fetch_text("https://unreachable.example.com/", retries=1)
    assert calls == 2
    assert len(no_backoff) == 1


async def test_each_attempt_has_a_timeout(no_backoff: list[float]) -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(5)
        return httpx2.Response(200, text="late")

    async with mock_client(handler) as client:  # type: ignore[arg-type]
        with pytest.raises(NetworkError, match="timed out"):
            await client.fetch_text("https://slow.example.com/", timeout=0.05, retries=1)
    assert len(no_backoff) == 1


def test_backoff_doubles_with_jitter() -> None:
    for attempt in range(4):
        delay = backoff_seconds(attempt, None)
        assert 2**attempt * 0.5 <= delay <= 2**attempt * 0.5 + 0.25
    assert backoff_seconds(0, "Wed, 21 Oct 2015 07:28:00 GMT") < 1
