"""Polling boards (#55): a board is asked whether it changed before it is read, a new posting is
timed against the last look, boards that matter are asked more often, and a failing one backs
off."""

from __future__ import annotations

import copy
import uuid
from datetime import timedelta
from typing import Any

import httpx2
import pytest
from asgiref.sync import sync_to_async
from django.db import connection
from django.utils import timezone

from apps.core.clock import database_now
from apps.jobs import radar
from apps.jobs.ingest import ingest_jobs
from apps.jobs.models import Company, Job
from apps.jobs.refresh import refresh_company
from apps.jobs.registry import (
    COLD_INTERVAL,
    HOT_INTERVAL,
    MAX_BACKOFF,
    due_companies,
    mark_relevant,
    poll_interval,
    record_fetch,
)
from apps.jobs.sources import BOARD_ADAPTERS, ashby, greenhouse
from apps.jobs.sources.poll import VERSION, PollState, poll_board
from apps.jobs.tasks import watches_due
from tests.factories import make_company, make_job
from tests.jobs.conftest import fixture_json, greenhouse_listing, mock_client
from tests.jobs.test_ingest import posting

BOARD = fixture_json("greenhouse/anthropic.json")
LINEAR = fixture_json("ashby/linear.json")
db = pytest.mark.django_db(transaction=True)


class Greenhouse:
    """One Greenhouse board that honours `If-None-Match`, as the real one does: the listing and
    the board each carry an ETag of their content, and an asker who already holds it gets a 304.
    """

    def __init__(self, slug: str, board: Any) -> None:
        self.slug = slug
        self.board = copy.deepcopy(board)
        self.requests: list[tuple[str, str | None]] = []  # (which, If-None-Match)

    def _answer(self, request: httpx2.Request, body: Any) -> httpx2.Response:
        etag = f'W/"{abs(hash(repr(body)))}"'
        if request.headers.get("if-none-match") == etag:
            return httpx2.Response(304)
        return httpx2.Response(200, json=body, headers={"etag": etag})

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        asked = request.headers.get("if-none-match")
        if url == greenhouse.probe_url(self.slug):
            self.requests.append(("listing", asked))
            return self._answer(request, greenhouse_listing(self.board))
        if url == greenhouse.board_url(self.slug):
            self.requests.append(("board", asked))
            return self._answer(request, self.board)
        return httpx2.Response(404, text="Not Found")

    def add(self, job_id: int, **fields: Any) -> None:
        job = {**copy.deepcopy(self.board["jobs"][0]), "id": job_id, **fields}
        job["absolute_url"] = f"https://job-boards.greenhouse.io/{self.slug}/jobs/{job_id}"
        self.board["jobs"].append(job)


async def company(**fields: Any) -> Company:
    return await sync_to_async(make_company)(**fields)


async def reload(found: Company) -> Company:
    return await Company.objects.aget(id=found.id)


def _xmins(company_id: Any) -> dict[str, int]:
    """Each posting's row version: a row that was not rewritten keeps its `xmin`."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT id, xmin::text::bigint FROM jobs WHERE company_id = %s", [company_id]
        )
        return dict(cursor.fetchall())


# --- asking before reading -----------------------------------------------------------------------


@db
async def test_the_first_poll_reads_the_board_and_the_next_asks_and_touches_nothing() -> None:
    acme = await company(slug="acme", ats_slug="acme")
    site = Greenhouse("acme", BOARD)
    async with mock_client(site.handler) as client:
        first = await refresh_company(client, acme)
        acme = await reload(acme)
        versions = await sync_to_async(_xmins)(acme.id)
        checked = acme.last_ok_at
        second = await refresh_company(client, acme)

    assert (first.status, first.unchanged, first.jobs) == ("ok", False, len(BOARD["jobs"]))
    assert first.ingest is not None and first.ingest.inserted == len(BOARD["jobs"])
    # The listing, then the board it had never read; then only the listing, with its ETag.
    assert [which for which, _ in site.requests] == ["listing", "board", "listing"]
    assert site.requests[0][1] is None and site.requests[2][1] is not None

    assert (second.status, second.unchanged, second.ingest) == ("ok", True, None)
    assert second.jobs == len(BOARD["jobs"])  # the count it had
    after = await reload(acme)
    assert after.last_ok_at is not None and checked is not None and after.last_ok_at > checked
    assert (after.last_fetch_status, after.last_fetch_job_count) == ("ok", len(BOARD["jobs"]))
    assert await sync_to_async(_xmins)(acme.id) == versions  # no posting was rewritten
    assert PollState.read(after.poll_state).url == greenhouse.probe_url("acme")


@db
async def test_a_changed_listing_is_read_and_the_new_posting_is_timed() -> None:
    acme = await company(slug="acme", ats_slug="acme")
    site = Greenhouse("acme", BOARD)
    async with mock_client(site.handler) as client:
        await refresh_company(client, acme)
        # The board was last checked ten minutes ago. Two postings appear: one the source dates
        # two minutes back, one it dates last week (taken down and put back, say).
        await Company.objects.filter(id=acme.id).aupdate(
            last_ok_at=await sync_to_async(database_now)() - timedelta(minutes=10)
        )
        now = timezone.now()
        site.add(9001, first_published=(now - timedelta(minutes=2)).isoformat())
        site.add(9002, first_published=(now - timedelta(days=7)).isoformat())
        outcome = await refresh_company(client, await reload(acme))

    assert outcome.ingest is not None and outcome.ingest.inserted == 2
    assert [which for which, _ in site.requests][2:] == ["listing", "board"]
    lags = {
        job.external_id: job.seen_lag_s
        async for job in Job.objects.filter(company_id=acme.id).only("external_id", "seen_lag_s")
    }
    # Posted two minutes ago: that long. Dated last week but absent ten minutes ago: ten minutes.
    assert lags["9001"] is not None and 110 <= lags["9001"] <= 135
    assert lags["9002"] is not None and 590 <= lags["9002"] <= 615
    # What the first fetch brought in was already up before anyone looked: not measured.
    assert {lag for key, lag in lags.items() if key not in ("9001", "9002")} == {None}


@db
async def test_fetch_now_reads_the_board_whatever_it_says() -> None:
    acme = await company(slug="acme", ats_slug="acme")
    site = Greenhouse("acme", BOARD)
    async with mock_client(site.handler) as client:
        await refresh_company(client, acme)
        outcome = await refresh_company(client, await reload(acme), force=True)
    assert (outcome.unchanged, outcome.jobs) == (False, len(BOARD["jobs"]))
    assert site.requests[2:] == [("listing", None), ("board", None)]  # no validators sent


@db
async def test_a_board_is_read_whole_once_a_day_even_if_it_says_unchanged() -> None:
    acme = await company(slug="acme", ats_slug="acme")
    site = Greenhouse("acme", BOARD)
    async with mock_client(site.handler) as client:
        await refresh_company(client, acme)
        acme = await reload(acme)
        aged = {**acme.poll_state, "readAt": (timezone.now() - timedelta(hours=31)).isoformat()}
        await Company.objects.filter(id=acme.id).aupdate(poll_state=aged)
        outcome = await refresh_company(client, await reload(acme))
    assert outcome.unchanged is False
    assert site.requests[2:] == [("listing", None), ("board", None)]


@db
async def test_the_same_bytes_are_unchanged_without_an_etag() -> None:
    """Ashby sends no ETag and a `Last-Modified` that is only its cache's age, so every poll
    downloads the board; the digest of the body is what says nothing changed."""
    linear = await company(slug="linear", ats_slug="linear", source_type="ashby")
    answers = {"board": LINEAR}
    served: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        served.append(str(request.url))
        return httpx2.Response(
            200,
            json=answers["board"],
            headers={"last-modified": f"Mon, 09 Nov 2026 20:{len(served):02d}:00 GMT"},
        )

    async with mock_client(handler) as client:
        first = await refresh_company(client, linear)
        second = await refresh_company(client, await reload(linear))
        changed = copy.deepcopy(LINEAR)
        changed["jobs"][0]["title"] = "A different title"
        answers["board"] = changed
        third = await refresh_company(client, await reload(linear))

    assert served == [ashby.board_url("linear")] * 3  # one request each: the board is its own probe
    assert (first.unchanged, second.unchanged, third.unchanged) == (False, True, False)
    assert third.ingest is not None and third.ingest.updated == 1


async def test_a_state_made_for_another_url_or_version_is_not_trusted() -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(dict(request.headers))
        return httpx2.Response(200, json=LINEAR, headers={"etag": '"v2"'})

    stored = {
        "version": VERSION,
        "url": "https://elsewhere.example/board",
        "etag": '"v1"',
        "digest": "0" * 40,
        "readAt": timezone.now().isoformat(),
    }
    assert PollState.read({**stored, "version": VERSION + 1}) == PollState()
    assert PollState.read("nonsense") == PollState()
    assert PollState.read({**stored, "readAt": "yesterday"}) == PollState()
    state = PollState.read(stored)
    assert state.etag == '"v1"'
    async with mock_client(handler) as client:
        polled = await poll_board(
            client,
            BOARD_ADAPTERS["ashby"],
            "linear",
            state,
            company_id=make_company_id(),
            now=timezone.now(),
        )
    assert "if-none-match" not in seen[0]
    assert polled.jobs is not None and polled.state.etag == '"v2"'
    assert polled.state.url == ashby.board_url("linear")


def make_company_id() -> uuid.UUID:
    return uuid.UUID(int=0)


# --- failures back a board off -------------------------------------------------------------------


@db
async def test_each_failure_doubles_the_wait_and_a_success_clears_it(
    no_backoff: list[float],
) -> None:
    acme = await company(slug="acme", ats_slug="acme")
    answers = {"status": 503}
    site = Greenhouse("acme", BOARD)

    def handler(request: httpx2.Request) -> httpx2.Response:
        if answers["status"] != 200:
            return httpx2.Response(answers["status"], text="busy")
        return site.handler(request)

    now = timezone.now()
    failing = acme
    async with mock_client(handler) as client:
        waits = (COLD_INTERVAL * 2, COLD_INTERVAL * 4, MAX_BACKOFF)  # 2 h, 4 h, then the cap
        for failures, wait in enumerate(waits, start=1):
            outcome = await refresh_company(client, await reload(acme))
            assert outcome.status == "error"
            failing = await reload(acme)
            assert failing.fetch_failures == failures
            assert poll_interval(failing, now) == wait
        assert failing.last_ok_at is None
        answers["status"] = 200
        await refresh_company(client, failing)
    healthy = await reload(acme)
    assert (healthy.fetch_failures, healthy.last_fetch_status) == (0, "ok")
    assert healthy.last_ok_at is not None


# --- who is due ----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_boards_that_matter_are_asked_every_quarter_hour_and_the_rest_hourly() -> None:
    now = timezone.now()
    ago = lambda **delta: now - timedelta(**delta)  # noqa: E731
    make_company(slug="never")
    make_company(slug="hot-due", last_fetched_at=ago(minutes=15), last_relevant_at=ago(days=3))
    make_company(slug="hot-fresh", last_fetched_at=ago(minutes=5), last_relevant_at=ago(days=3))
    make_company(slug="cold-due", last_fetched_at=ago(minutes=61))
    make_company(slug="cold-fresh", last_fetched_at=ago(minutes=40))
    make_company(slug="cooled", last_fetched_at=ago(minutes=20), last_relevant_at=ago(days=200))
    make_company(slug="failing", last_fetched_at=ago(minutes=90), fetch_failures=2)
    make_company(slug="page-due", source_type="careers_page", last_fetched_at=ago(hours=7))
    make_company(slug="page-fresh", source_type="careers_page", last_fetched_at=ago(hours=5))
    make_company(slug="paused", status="paused")

    # Never fetched first, then the longest waiting.
    assert [company.slug for company in due_companies(now)] == [
        "never",
        "page-due",
        "cold-due",
        "hot-due",
    ]
    by_slug = {company.slug: company for company in Company.objects.all()}
    assert poll_interval(by_slug["hot-due"], now) == HOT_INTERVAL
    assert poll_interval(by_slug["cooled"], now) == COLD_INTERVAL
    assert poll_interval(by_slug["failing"], now) == COLD_INTERVAL * 4
    by_slug["failing"].fetch_failures = 12
    assert poll_interval(by_slug["failing"], now) == MAX_BACKOFF

    boards = radar.polling(now)
    assert (boards.hot, boards.cold, boards.careers_pages) == (2, 5, 2)


@pytest.mark.django_db
def test_a_tick_a_few_seconds_early_still_finds_the_board_due() -> None:
    now = timezone.now()
    make_company(
        slug="hot",
        last_fetched_at=now - timedelta(minutes=14, seconds=20),
        last_relevant_at=now,
    )
    assert [company.slug for company in due_companies(now)] == ["hot"]


@pytest.mark.django_db
def test_a_board_becomes_hot_when_it_lists_an_early_career_role_or_gives_a_match() -> None:
    lists_one = make_company(slug="lists-one", ats_slug="lists-one")
    gives_one = make_company(slug="gives-one", ats_slug="gives-one")
    neither = make_company(slug="neither", ats_slug="neither")

    record_fetch(lists_one.id, "ok", 4, relevant=True)
    job = make_job(company=gives_one)
    assert mark_relevant([job.id]) == 1
    assert mark_relevant([job.id]) == 0  # marked moments ago: not rewritten
    record_fetch(neither.id, "ok", 4)

    marks = dict(Company.objects.values_list("slug", "last_relevant_at"))
    assert marks["lists-one"] is not None and marks["gives-one"] is not None
    assert marks["neither"] is None
    # A failed fetch never marks a board, and never moves its last good check.
    record_fetch(neither.id, "error", None, relevant=True)
    neither.refresh_from_db()
    assert (neither.last_relevant_at, neither.last_ok_at is not None) == (None, True)
    assert neither.fetch_failures == 1


@pytest.mark.django_db
def test_a_board_that_is_gone_forgets_its_poll_state() -> None:
    gone = make_company(slug="gone", poll_state={"version": VERSION, "url": "x"})
    record_fetch(gone.id, "not-found", 0, mark_invalid=True)
    gone.refresh_from_db()
    assert (gone.status, gone.poll_state) == ("invalid", {})


# --- ingest --------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_an_unchanged_posting_is_rewritten_at_most_hourly() -> None:
    acme = make_company(slug="acme", ats_slug="acme")
    ingest_jobs(acme.id, [posting(acme, 0)])
    before = _xmins(acme.id)
    result = ingest_jobs(acme.id, [posting(acme, 0)])
    assert result.unchanged == 1
    assert _xmins(acme.id) == before  # seen moments ago: left alone

    Job.objects.filter(company_id=acme.id).update(is_active=False)
    ingest_jobs(acme.id, [posting(acme, 0)])
    assert Job.objects.get(company_id=acme.id).is_active  # but a closed one reopens at once


@pytest.mark.django_db
def test_seen_lag_is_set_once_and_only_with_a_previous_check() -> None:
    acme = make_company(slug="acme", ats_slug="acme")
    ingest_jobs(acme.id, [posting(acme, 0)])  # a first fetch: nothing bounds it
    checked = database_now() - timedelta(minutes=30)
    ingest_jobs(acme.id, [posting(acme, 1)], previous_ok=checked)
    lags = dict(Job.objects.values_list("external_id", "seen_lag_s"))
    assert lags["0"] is None
    assert lags["1"] is not None and 1795 <= lags["1"] <= 1810  # posted in September: the gap

    # A later edit of the posting does not re-time it.
    ingest_jobs(
        acme.id,
        [posting(acme, 1, description="Build other things.")],
        previous_ok=database_now() - timedelta(minutes=1),
    )
    assert Job.objects.get(external_id="1").seen_lag_s == lags["1"]


# --- the report ----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_lag_report_is_the_week_s_median_and_p90_by_source() -> None:
    assert radar.lag_report() == radar.LagReport(0, None, None, [])

    board = make_company(slug="acme", ats_slug="acme")
    other = make_company(slug="linear", ats_slug="linear", source_type="ashby")
    for lag in (60, 120, 180, 240, 600):
        make_job(company=board, seen_lag_s=lag)
    make_job(company=other, seen_lag_s=900)
    make_job(company=board)  # seeded: not measured
    make_job(
        company=board, seen_lag_s=5, first_seen_at=timezone.now() - timedelta(days=9)
    )  # before the window

    report = radar.lag_report()
    assert (report.postings, report.median_s, report.p90_s) == (6, 210, 750)
    assert report.by_source == [
        radar.SourceLag("greenhouse", 5, 180, 456),
        radar.SourceLag("ashby", 1, 900, 900),
    ]


# --- when the watches start ----------------------------------------------------------------------


def test_watches_start_when_a_pass_brought_postings_in_or_on_a_sixth_hour() -> None:
    quarter_past = 1_793_491_200 + 900  # a tick that is on no sixth hour
    assert 1_793_491_200 % (6 * 3600) == 0
    assert watches_due(3, quarter_past)
    assert not watches_due(0, quarter_past)
    assert watches_due(0, 1_793_491_200)
    assert watches_due(0, None) and watches_due(0, 0)  # deferred by hand: no tick
