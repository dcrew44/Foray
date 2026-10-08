"""Fixtures for the sources and companies tests (step 3C).

- `FIXTURES` and `GOLDENS`: the captured API payloads and the outputs the TypeScript functions
  produced from them (step 0). Adapter goldens pin `descriptionRaw` as `{sha256, utf8Bytes}`.
- `mock_client(handler)`: an `HttpClient` over `httpx2.MockTransport`, with a resolver that
  answers every name with a public address (so the SSRF guard passes) unless told otherwise.
  Nothing reaches DNS or the network.
- `no_backoff`: retries without sleeping; records the delays it was asked for.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx2
import pytest

from apps.core import http
from apps.core.http import HttpClient
from apps.jobs.sources.types import NormalizedJob, js_iso

TESTS = Path(__file__).resolve().parents[1]
FIXTURES = TESTS / "fixtures"
GOLDENS = TESTS / "goldens"

PUBLIC_ADDRESS = "93.184.215.14"

type Handler = Callable[[httpx2.Request], httpx2.Response]


def fixture_text(path: str) -> str:
    return (FIXTURES / path).read_text()


def fixture_json(path: str) -> Any:
    return json.loads(fixture_text(path))


def golden(path: str) -> Any:
    return json.loads((GOLDENS / path).read_text())


def resolver(addresses: Mapping[str, Sequence[str]] | None = None):
    """Every name resolves to a public address, except those given."""
    table = dict(addresses or {})
    calls: list[str] = []

    async def resolve(host: str, port: int) -> list[str]:
        calls.append(host)
        return list(table.get(host, [PUBLIC_ADDRESS]))

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


def mock_client(
    handler: Handler, *, addresses: Mapping[str, Sequence[str]] | None = None
) -> HttpClient:
    return HttpClient(transport=httpx2.MockTransport(handler), resolve=resolver(addresses))


def routes(table: Mapping[str, httpx2.Response | Callable[[], httpx2.Response]]) -> Handler:
    """A handler answering exact URLs from a table, and 404 for anything else."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        answer = table.get(str(request.url))
        if answer is None:
            return httpx2.Response(404, text="Not Found")
        return answer() if callable(answer) else answer

    return handler


def html(body: str, status: int = 200) -> httpx2.Response:
    return httpx2.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8"})


@pytest.fixture
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[float]]:
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(http, "_sleep", sleep)
    yield delays


def golden_view(job: NormalizedJob) -> dict[str, Any]:
    """A `NormalizedJob` the way the adapter goldens store it (no `raw`, `descriptionRaw` by
    hash)."""
    raw = job.description_raw
    return {
        "id": job.id,
        "source": job.source,
        "externalId": job.external_id,
        "companySlug": job.company_slug,
        "companyName": job.company_name,
        "title": job.title,
        "locations": job.locations,
        "locationNorm": job.location_norm.dump() if job.location_norm else None,
        "remote": job.remote,
        "employmentType": job.employment_type,
        "salary": job.salary.dump(),
        "descriptionText": job.description_text,
        "url": job.url,
        "applyUrl": job.apply_url,
        "postedAt": js_iso(job.posted_at),
        "contentHash": job.content_hash,
        "dedupeKey": job.dedupe_key,
        "descriptionRaw": None
        if raw is None
        else {
            "sha256": hashlib.sha256(raw.encode()).hexdigest(),
            "utf8Bytes": len(raw.encode()),
        },
    }


def assert_matches_golden(job: NormalizedJob, expected: Mapping[str, Any]) -> None:
    view = golden_view(job)
    assert set(view) == set(expected), job.id
    for key, value in expected.items():
        assert view[key] == value, f"{job.id}: {key}"
