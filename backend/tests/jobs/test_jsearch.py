"""JSearch (ported from `jsearch.test.ts`, 13 cases), plus golden equality and the budget rules of
design §10.6: counted before the response is read, one upsert, never over budget."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from django.conf import settings
from pydantic import SecretStr

from apps.jobs.models import ApiUsage
from apps.jobs.sources import jsearch
from apps.jobs.sources.jsearch import (
    DiscoveredBoard,
    JSearchDisabledError,
    JSearchQuotaError,
    ats_from_apply_options,
    count_request,
    current_period,
    employer_slug,
    normalize_job,
    search_jsearch,
)
from tests.jobs.conftest import assert_matches_golden, fixture_json, golden, mock_client

response = fixture_json("jsearch/entry-level-sf.json")
raw = response["data"]["jobs"]
jobs = [normalize_job(job) for job in raw]


# --- goldens -------------------------------------------------------------------------------------


def test_normalize_matches_typescript_golden() -> None:
    expected = golden("adapters/jsearch/entry-level-sf.json")["jobs"]
    assert len(expected) == len(raw)
    for posting, want in zip(raw, expected, strict=True):
        assert_matches_golden(normalize_job(posting), want["job"])
        board = ats_from_apply_options(posting)
        assert (None if board is None else {"source": board.source, "slug": board.slug}) == want[
            "board"
        ]
        assert employer_slug(js_name(posting)) == want["employerSlug"]


def js_name(posting: dict) -> str:
    return (posting.get("employer_name") or "").strip() or "Unknown"


# --- the envelope and normalisation --------------------------------------------------------------


def test_the_envelope_carries_a_cursor_not_a_page_number() -> None:
    assert isinstance(response["data"]["cursor"], str)
    assert response["data"]["cursor"]


def test_ids_come_from_the_employer() -> None:
    for job in jobs:
        assert job.id.startswith("jsearch:")
        assert len(job.id.split(":")) == 3
        assert job.source == "jsearch"


def test_keeps_the_description_exactly_as_sent() -> None:
    assert jobs[0].description_raw == raw[0]["job_description"]


def test_still_strips_trailing_boilerplate_from_plain_text() -> None:
    for job in jobs:
        if job.description_text:
            assert len(job.description_text) <= len(job.description_raw or "")


def test_trusts_the_explicit_remote_flag() -> None:
    for posting, job in zip(raw, jobs, strict=True):
        if posting.get("job_is_remote") is True:
            assert job.remote == "remote"


def test_discards_an_implausible_reported_salary() -> None:
    job = normalize_job(
        {
            "job_id": "x",
            "job_title": "Engineer",
            "employer_name": "Acme",
            "job_min_salary": 90,
            "job_max_salary": 120,
            "job_salary_period": "YEAR",
        }
    )
    assert job.salary.min is None


def test_keeps_a_plausible_reported_salary() -> None:
    job = normalize_job(
        {
            "job_id": "x",
            "job_title": "Engineer",
            "employer_name": "Acme",
            "job_min_salary": 150_000,
            "job_max_salary": 200_000,
            "job_salary_period": "YEAR",
        }
    )
    assert (job.salary.min, job.salary.max, job.salary.period) == (150_000, 200_000, "year")


def test_dedupes_against_the_company_board() -> None:
    aggregator = normalize_job(
        {
            "job_id": "abc",
            "job_title": "Software Engineer, Payments (Remote)",
            "employer_name": "Stripe, Inc.",
            "job_city": "San Francisco",
            "job_state": "CA",
            "job_country": "US",
        }
    )
    other = normalize_job(
        {
            "job_id": "different-id-entirely",
            "job_title": "Software Engineer, Payments",
            "employer_name": "Stripe",
            "job_location": "San Francisco, CA",
        }
    )
    assert aggregator.dedupe_key == other.dedupe_key


def test_an_empty_job_location_does_not_fall_back() -> None:
    job = normalize_job({"job_id": "x", "job_title": "E", "job_location": "", "job_city": "Austin"})
    assert job.locations == []


def test_spots_a_greenhouse_board_in_an_apply_link() -> None:
    board = ats_from_apply_options(
        {
            "job_id": "1",
            "job_title": "Engineer",
            "apply_options": [
                {"apply_link": "https://www.linkedin.com/jobs/view/123"},
                {"apply_link": "https://job-boards.greenhouse.io/ramp/jobs/456"},
            ],
        }
    )
    assert board == DiscoveredBoard("greenhouse", "ramp")


def test_spots_lever_and_ashby_too() -> None:
    lever = ats_from_apply_options(
        {"job_id": "1", "job_title": "E", "job_apply_link": "https://jobs.lever.co/plaid/abc"}
    )
    ashby = ats_from_apply_options(
        {
            "job_id": "1",
            "job_title": "E",
            "job_apply_link": "https://jobs.ashbyhq.com/vanta/abc/application",
        }
    )
    assert lever == DiscoveredBoard("lever", "plaid")
    assert ashby == DiscoveredBoard("ashby", "vanta")


def test_finds_nothing_among_aggregator_links() -> None:
    board = ats_from_apply_options(
        {
            "job_id": "1",
            "job_title": "E",
            "apply_options": [
                {"apply_link": "https://www.indeed.com/viewjob?jk=1"},
                {"apply_link": "https://www.linkedin.com/jobs/view/2"},
            ],
        }
    )
    assert board is None


def test_employer_slug_is_stable() -> None:
    assert employer_slug("Stripe, Inc.") == "stripe-inc"
    assert employer_slug("Ben & Jerry's") == "ben-jerry-s"


def test_employer_slug_is_never_empty() -> None:
    assert employer_slug("   ") == "unknown"
    assert employer_slug("!!!") == "unknown"


def test_current_period_is_the_utc_month() -> None:
    assert current_period(datetime(2026, 1, 31, 23, 30, tzinfo=UTC)) == "2026-01"


# --- searching and the budget --------------------------------------------------------------------


@pytest.fixture
def jsearch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.ENV, "jsearch_enabled", True)
    monkeypatch.setattr(settings.ENV, "rapidapi_key", SecretStr("test-key"))
    monkeypatch.setattr(settings.ENV, "jsearch_monthly_budget", 5)


def _page(jobs_: list[dict], cursor: str | None) -> httpx2.Response:
    return httpx2.Response(200, json={"status": "OK", "data": {"jobs": jobs_, "cursor": cursor}})


@pytest.mark.django_db(transaction=True)
async def test_disabled_raises_before_spending(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("no request may be sent")

    async with mock_client(handler) as client:
        with pytest.raises(JSearchDisabledError, match="JSEARCH_ENABLED is false"):
            await search_jsearch(client, "engineer")
        monkeypatch.setattr(settings.ENV, "jsearch_enabled", True)
        monkeypatch.setattr(settings.ENV, "rapidapi_key", None)
        with pytest.raises(JSearchDisabledError, match="RAPIDAPI_KEY is not set"):
            await search_jsearch(client, "engineer")


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("jsearch_on")
async def test_pages_by_cursor_and_counts_each_request() -> None:
    seen: list[dict[str, list[str]]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        query = parse_qs(urlsplit(str(request.url)).query)
        seen.append(query)
        assert request.headers["x-rapidapi-key"] == "test-key"
        assert request.headers["x-rapidapi-host"] == "jsearch.p.rapidapi.com"
        return _page(raw[:2], "next") if "cursor" not in query else _page(raw[2:], None)

    async with mock_client(handler) as client:
        result = await search_jsearch(client, "software engineer", max_requests=3)

    assert [job.id for job in result.jobs] == [job.id for job in jobs]
    assert result.requests_used == 2
    assert result.requests_this_month == 2
    assert seen[0]["query"] == ["software engineer"]
    assert seen[0]["date_posted"] == ["month"]
    assert seen[1]["cursor"] == ["next"]
    usage = await ApiUsage.objects.aget(provider="jsearch", period=current_period())
    assert usage.requests == 2


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("jsearch_on", "no_backoff")
async def test_a_failed_request_still_counts() -> None:
    sent = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal sent
        sent += 1
        return httpx2.Response(500, text="down")

    async with mock_client(handler) as client:
        with pytest.raises(jsearch.HttpError):
            await search_jsearch(client, "engineer")
    usage = await ApiUsage.objects.aget(provider="jsearch", period=current_period())
    # The page and its one retry both reached RapidAPI, so both are counted.
    assert sent == 2
    assert usage.requests == 2


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("jsearch_on", "no_backoff")
async def test_every_attempt_counts_against_the_monthly_budget() -> None:
    """A retried page reaches RapidAPI twice and is counted twice, so the budget guard sees what
    was really sent. A retry that would go past the budget is not sent."""
    sent: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(str(request.url))
        if len(sent) % 2 == 1:
            return httpx2.Response(503, text="busy")  # each page's first attempt
        return _page(raw[:1], f"c{len(sent)}")

    async with mock_client(handler) as client:
        result = await search_jsearch(client, "engineer", max_requests=3)
    usage = await ApiUsage.objects.aget(provider="jsearch", period=current_period())
    # Budget 5: page 1 (2 attempts) and page 2 (2 attempts) fit; page 3's first attempt is the
    # fifth request, and its retry would be the sixth, so it is not sent.
    assert len(sent) == 5
    assert (usage.requests, result.requests_used, result.requests_this_month) == (5, 5, 5)
    assert len(result.jobs) == 2


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("jsearch_on", "no_backoff")
async def test_a_later_page_is_not_sent_once_a_retry_spent_its_share() -> None:
    """Budget 5 with 2 already used leaves room for 3 pages, but page 1's retry spends one of
    them. Page 2 is the fifth request; page 3's first attempt would be the sixth, so it is not
    sent, and the pages already read come back."""
    await ApiUsage.objects.acreate(provider="jsearch", period=current_period(), requests=2)
    sent: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(str(request.url))
        if len(sent) == 1:
            return httpx2.Response(503, text="busy")  # page 1's first attempt
        return _page(raw[:1], f"c{len(sent)}")

    async with mock_client(handler) as client:
        result = await search_jsearch(client, "engineer", max_requests=3)
    usage = await ApiUsage.objects.aget(provider="jsearch", period=current_period())
    assert len(sent) == 3  # page 1 twice, then page 2
    assert usage.requests <= 5
    assert (usage.requests, result.requests_used, result.requests_this_month) == (5, 3, 5)
    assert len(result.jobs) == 2


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("jsearch_on", "no_backoff")
async def test_a_failure_after_a_page_keeps_what_it_has() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if "cursor" in str(request.url):
            return httpx2.Response(503, text="busy")
        return _page(raw[:1], "next")

    async with mock_client(handler) as client:
        result = await search_jsearch(client, "engineer", max_requests=3)
    assert len(result.jobs) == 1
    assert result.requests_used == 3  # page 1, then page 2 and its one retry


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("jsearch_on")
async def test_never_exceeds_the_monthly_budget() -> None:
    await ApiUsage.objects.acreate(provider="jsearch", period=current_period(), requests=4)
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return _page(raw[:1], f"c{calls}")

    async with mock_client(handler) as client:
        result = await search_jsearch(client, "engineer", max_requests=3)
        assert result.requests_used == 1  # 5 - 4 left
        with pytest.raises(JSearchQuotaError, match=r"\(5/5 requests\)"):
            await search_jsearch(client, "engineer", max_requests=3)
    assert calls == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("jsearch_on")
async def test_collects_boards_from_apply_links_once() -> None:
    posting = dict(
        raw[0],
        job_id="a",
        employer_name="Ramp",
        apply_options=[{"apply_link": "https://job-boards.greenhouse.io/ramp/jobs/1"}],
    )
    again = dict(posting, job_id="b")

    def handler(request: httpx2.Request) -> httpx2.Response:
        return _page([posting, again], None)

    async with mock_client(handler) as client:
        result = await search_jsearch(client, "engineer")
    assert result.discovered_boards == [DiscoveredBoard("greenhouse", "ramp", "Ramp")]


@pytest.mark.django_db
def test_count_request_is_one_upsert() -> None:
    at = datetime(2026, 3, 9, tzinfo=UTC)
    assert [count_request("jsearch", at) for _ in range(3)] == [1, 2, 3]
    assert ApiUsage.objects.get(provider="jsearch", period="2026-03").requests == 3
