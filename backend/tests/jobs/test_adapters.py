"""Board adapters against real captured payloads (ported from `adapters.test.ts`, 20 cases), plus
golden equality with the TypeScript adapters on every normalised field (design §12.2).

The fixtures are genuine API responses, trimmed to a few postings: every provider has a quirk that
only shows up in real data (escaped HTML, a description that omits the requirements, a
compensation block that is present but empty).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import httpx2
import pytest

from apps.jobs.sources import BOARD_ADAPTERS, ashby, greenhouse, lever
from apps.jobs.sources.types import BoardNotFound
from tests.jobs.conftest import (
    assert_matches_golden,
    fixture_json,
    golden,
    mock_client,
    routes,
)

greenhouse_board = fixture_json("greenhouse/anthropic.json")
lever_board = fixture_json("lever/matchgroup.json")
ashby_linear = fixture_json("ashby/linear.json")
ashby_vanta = fixture_json("ashby/vanta.json")

NORMALIZERS = {
    "greenhouse": greenhouse.normalize_job,
    "lever": lever.normalize_job,
    "ashby": ashby.normalize_job,
}
BOARD_URLS = {
    "greenhouse": greenhouse.board_url,
    "lever": lever.board_url,
    "ashby": ashby.board_url,
}
BOARD_FIXTURES = [
    ("greenhouse", "anthropic"),
    ("greenhouse", "stripe"),
    ("lever", "matchgroup"),
    ("lever", "spotify"),
    ("ashby", "linear"),
    ("ashby", "vanta"),
]


def _postings(source: str, payload: object) -> list[dict]:
    if source == "lever":
        assert isinstance(payload, list)
        return payload
    assert isinstance(payload, dict)
    jobs = payload["jobs"]
    return [job for job in jobs if job.get("isListed") is not False] if source == "ashby" else jobs


# --- goldens -------------------------------------------------------------------------------------


@pytest.mark.parametrize(("source", "slug"), BOARD_FIXTURES)
def test_adapter_matches_typescript_golden(source: str, slug: str) -> None:
    expected = golden(f"adapters/{source}/{slug}.json")
    assert BOARD_URLS[source](slug) == expected["request"]
    payload = fixture_json(expected["fixture"])
    jobs = [NORMALIZERS[source](slug, posting) for posting in _postings(source, payload)]
    assert [job.id for job in jobs] == [job["id"] for job in expected["jobs"]]
    for job, want in zip(jobs, expected["jobs"], strict=True):
        assert_matches_golden(job, want)


@pytest.mark.parametrize(("source", "slug"), BOARD_FIXTURES)
async def test_fetch_board_through_the_client(source: str, slug: str) -> None:
    expected = golden(f"adapters/{source}/{slug}.json")
    body = fixture_json(expected["fixture"])
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=body)

    async with mock_client(handler) as client:
        jobs = await BOARD_ADAPTERS[source].fetch_board(client, slug)

    assert [str(request.url) for request in seen] == [expected["request"]]
    assert seen[0].headers["user-agent"].startswith("Foray/0.2 (job search agent; +")
    for job, want in zip(jobs, expected["jobs"], strict=True):
        assert_matches_golden(job, want)


@pytest.mark.parametrize("source", ["greenhouse", "lever", "ashby"])
async def test_a_404_is_board_not_found(source: str) -> None:
    expected = golden(f"adapters/{source}/_not-found.json")
    response = expected["response"]
    slug = "foray-no-such-company-xyz123"
    handler = routes(
        {
            expected["request"]: httpx2.Response(
                response["status"],
                text=response["body"],
                headers={"content-type": response["contentType"]},
            )
        }
    )
    async with mock_client(handler) as client:
        with pytest.raises(BoardNotFound) as raised:
            await BOARD_ADAPTERS[source].fetch_board(client, slug)
    assert str(raised.value) == expected["outcome"]["error"]["message"]


async def test_an_empty_board_is_not_a_404() -> None:
    url = greenhouse.board_url("quiet")
    handler = routes({url: httpx2.Response(200, json={"jobs": [], "meta": {"total": 0}})})
    async with mock_client(handler) as client:
        assert await greenhouse.fetch_board(client, "quiet") == []


def test_unparseable_dates_are_none_not_a_failed_board() -> None:
    posting = dict(greenhouse_board["jobs"][0], first_published="not a date")
    assert greenhouse.normalize_job("anthropic", posting).posted_at is None


def test_slugs_are_percent_encoded_like_encode_uri_component() -> None:
    assert greenhouse.board_url("a b/c!") == (
        "https://boards-api.greenhouse.io/v1/boards/a%20b%2Fc!/jobs?content=true"
    )


# --- greenhouse ----------------------------------------------------------------------------------

gh_jobs = [greenhouse.normalize_job("anthropic", job) for job in greenhouse_board["jobs"]]


def test_greenhouse_ids_coerce_the_numeric_id_to_a_string() -> None:
    for job in gh_jobs:
        assert re.fullmatch(r"greenhouse:anthropic:\d+", job.id)
        assert isinstance(job.external_id, str)


def test_greenhouse_leaves_description_raw_exactly_as_sent() -> None:
    first = gh_jobs[0]
    assert first.description_raw == greenhouse_board["jobs"][0]["content"]
    assert "&lt;" in (first.description_raw or "")  # which for Greenhouse means still escaped


def test_greenhouse_unescapes_the_html_for_description_text() -> None:
    text = gh_jobs[0].description_text or ""
    assert "&lt;" not in text
    assert "<div" not in text
    assert len(text) > 200


def test_greenhouse_splits_a_pipe_separated_location() -> None:
    assert any(len(job.locations) > 1 for job in gh_jobs)
    for job in gh_jobs:
        for location in job.locations:
            assert "|" not in location


def test_greenhouse_splits_on_semicolons_too_and_dedupes() -> None:
    raw_by_id = {str(job["id"]): job for job in greenhouse_board["jobs"]}
    mixed = next(
        job
        for job in gh_jobs
        if ";" in ((raw_by_id[job.external_id].get("location") or {}).get("name") or "")
    )
    assert "San Francisco, CA" in mixed.locations
    assert "New York City, NY" in mixed.locations
    assert len(set(mixed.locations)) == len(mixed.locations)
    # And the Bay Area is visible, which a pipe-only split would have hidden.
    assert mixed.location_norm is not None and mixed.location_norm.metro == "bay-area"


def test_greenhouse_recognises_the_bay_area() -> None:
    sf = next(job for job in gh_jobs if any("San Francisco" in loc for loc in job.locations))
    assert sf.location_norm is not None and sf.location_norm.metro == "bay-area"


def test_greenhouse_parses_a_date_and_keys_for_every_posting() -> None:
    for job in gh_jobs:
        assert isinstance(job.posted_at, datetime)
        assert job.posted_at.tzinfo is not None
        assert re.fullmatch(r"[0-9a-f]{40}", job.dedupe_key)
        assert re.fullmatch(r"[0-9a-f]{40}", job.content_hash)


# --- lever ---------------------------------------------------------------------------------------

lever_jobs = [lever.normalize_job("matchgroup", posting) for posting in lever_board]


def test_lever_uses_text_as_title_and_the_uuid_as_id() -> None:
    assert lever_jobs[0].title == lever_board[0]["text"]
    assert lever_jobs[0].id == f"lever:matchgroup:{lever_board[0]['id']}"


def test_lever_includes_the_requirements_lists() -> None:
    index = next(i for i, posting in enumerate(lever_board) if posting.get("lists"))
    posting, job = lever_board[index], lever_jobs[index]
    assert len(job.description_raw or "") > len(posting["description"])
    for item in posting["lists"]:
        if item.get("text"):
            assert item["text"] in (job.description_raw or "")


def test_lever_reads_the_structured_salary() -> None:
    index = next((i for i, p in enumerate(lever_board) if (p.get("salaryRange") or {}).get("min")))
    salary_range = lever_board[index]["salaryRange"]
    salary = lever_jobs[index].salary
    assert (salary.min, salary.max, salary.currency) == (
        salary_range["min"],
        salary_range["max"],
        salary_range["currency"],
    )


def test_lever_maps_workplace_type_onto_remote() -> None:
    for posting, job in zip(lever_board, lever_jobs, strict=True):
        if posting.get("workplaceType"):
            assert job.remote == posting["workplaceType"].lower()


def test_lever_keeps_the_apply_url_separate() -> None:
    for job in lever_jobs:
        assert job.apply_url != job.url
        assert "/apply" in (job.apply_url or "")


def test_lever_reads_created_at_as_epoch_milliseconds() -> None:
    posted = lever_jobs[0].posted_at
    assert posted is not None
    assert int(posted.timestamp() * 1000) == lever_board[0]["createdAt"]
    assert posted.tzinfo == UTC


def test_lever_keeps_an_empty_all_locations_empty() -> None:
    posting = dict(lever_board[0], categories={"allLocations": [], "location": "London"})
    assert lever.normalize_job("matchgroup", posting).locations == []


def test_lever_ignores_a_zero_salary_minimum() -> None:
    posting = dict(
        lever_board[0],
        salaryRange={"min": 0, "max": 200000, "currency": "USD", "interval": "per-year-salary"},
    )
    job = lever.normalize_job("matchgroup", posting)
    assert job.salary.min != 0


# --- ashby ---------------------------------------------------------------------------------------

linear_jobs = [ashby.normalize_job("linear", job) for job in ashby_linear["jobs"]]
vanta_jobs = [ashby.normalize_job("vanta", job) for job in ashby_vanta["jobs"]]


def test_ashby_keeps_the_complete_description() -> None:
    assert linear_jobs[0].description_raw == ashby_linear["jobs"][0]["descriptionHtml"]
    assert "<p" not in (linear_jobs[0].description_text or "")


def test_ashby_reads_structured_compensation() -> None:
    index = next(
        i
        for i, job in enumerate(ashby_vanta["jobs"])
        if any(
            (c.get("compensationType") or "").lower() == "salary"
            for c in (job.get("compensation") or {}).get("summaryComponents") or []
        )
    )
    salary = vanta_jobs[index].salary
    assert salary.min is not None and salary.min > 0
    assert salary.currency == "USD"
    assert salary.period == "year"


def test_ashby_survives_a_board_with_no_compensation() -> None:
    for job in linear_jobs:
        assert job.id.startswith("ashby:linear:")


def test_ashby_collects_secondary_locations() -> None:
    index = next(
        (i for i, job in enumerate(ashby_linear["jobs"]) if job.get("secondaryLocations")), None
    )
    if index is not None:
        assert len(linear_jobs[index].locations) > 1


def test_ashby_keeps_the_apply_url_separate() -> None:
    for job in linear_jobs + vanta_jobs:
        assert job.apply_url != job.url


async def test_ashby_drops_unlisted_postings() -> None:
    hidden = dict(ashby_linear["jobs"][0], isListed=False)
    url = ashby.board_url("linear")
    handler = routes({url: httpx2.Response(200, json={"jobs": [hidden, ashby_linear["jobs"][1]]})})
    async with mock_client(handler) as client:
        jobs = await ashby.fetch_board(client, "linear")
    assert [job.external_id for job in jobs] == [ashby_linear["jobs"][1]["id"]]


# --- across adapters -----------------------------------------------------------------------------


def test_description_raw_is_never_the_cleaned_text() -> None:
    every = (
        gh_jobs + lever_jobs + [ashby.normalize_job("linear", job) for job in ashby_linear["jobs"]]
    )
    for job in every:
        if not job.description_raw or not job.description_text:
            continue
        assert len(job.description_text) <= len(job.description_raw)
        assert not re.search(r"<(p|div|li|ul|h[1-6])\b", job.description_text)


def test_every_job_has_a_well_formed_id_and_a_remote_verdict() -> None:
    for job in gh_jobs + lever_jobs + vanta_jobs:
        assert len(job.id.split(":")) == 3
        assert job.remote in ("remote", "hybrid", "onsite", "unknown")
        assert re.match(r"https?://", job.url)
