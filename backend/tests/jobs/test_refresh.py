"""Board refreshes (design §10.5): only a 404 retires a company, errors keep the last count, the
empty-board guard across two refreshes, the pool keeps input order; and the three management
commands (seed, fetch_board, verify_boards) without the network."""

from __future__ import annotations

import io
from datetime import timedelta
from typing import Any

import httpx2
import pytest
from asgiref.sync import sync_to_async
from django.core.management import call_command
from django.utils import timezone

from apps.jobs.management.commands import fetch_board as fetch_board_command
from apps.jobs.management.commands import seed_companies as seed_command
from apps.jobs.models import Company, Job
from apps.jobs.refresh import (
    BOARD_STALE_AFTER,
    MULTI_BOARD_GATE,
    RefreshOutcome,
    refresh_companies,
    refresh_company,
    stale_cutoff,
)
from apps.jobs.sources import greenhouse
from tests.factories import make_company
from tests.jobs.conftest import FIXTURES, fixture_json, mock_client, routes

BOARD = fixture_json("greenhouse/anthropic.json")
db = pytest.mark.django_db(transaction=True)


async def company(**fields: Any) -> Company:
    return await sync_to_async(make_company)(**fields)


@db
async def test_an_ok_refresh_ingests_and_records() -> None:
    acme = await company(slug="acme", ats_slug="acme")
    handler = routes({greenhouse.board_url("acme"): httpx2.Response(200, json=BOARD)})
    async with mock_client(handler) as client:
        outcome = await refresh_company(client, acme)
    assert (outcome.status, outcome.jobs) == ("ok", len(BOARD["jobs"]))
    assert outcome.ingest is not None and outcome.ingest.inserted == len(BOARD["jobs"])
    acme = await Company.objects.aget(id=acme.id)
    assert (acme.last_fetch_status, acme.last_fetch_job_count) == ("ok", len(BOARD["jobs"]))
    assert acme.last_fetched_at is not None
    assert await Job.objects.filter(company_id=acme.id, is_active=True).acount() == len(
        BOARD["jobs"]
    )


@db
async def test_only_a_404_marks_a_company_invalid(no_backoff: list[float]) -> None:
    gone = await company(slug="gone", ats_slug="gone", last_fetch_job_count=4)
    flaky = await company(slug="flaky", ats_slug="flaky", last_fetch_job_count=9)
    handler = routes({greenhouse.board_url("flaky"): httpx2.Response(503, text="busy")})
    async with mock_client(handler) as client:
        outcomes = await refresh_companies(client, [gone, flaky])

    assert [(o.slug, o.status) for o in outcomes] == [("gone", "not-found"), ("flaky", "error")]
    gone = await Company.objects.aget(id=gone.id)
    flaky = await Company.objects.aget(id=flaky.id)
    assert (gone.status, gone.last_fetch_status, gone.last_fetch_job_count) == (
        "invalid",
        "not-found",
        0,
    )
    # A timeout or a 500 does not retire a company, and says nothing about its count.
    assert (flaky.status, flaky.last_fetch_status, flaky.last_fetch_job_count) == (
        "active",
        "error",
        9,
    )


@db
async def test_one_empty_answer_closes_nothing_and_a_second_does() -> None:
    acme = await company(slug="acme", ats_slug="acme")
    answers = {"board": BOARD}
    handler = routes(
        {greenhouse.board_url("acme"): lambda: httpx2.Response(200, json=answers["board"])}
    )
    async with mock_client(handler) as client:
        await refresh_company(client, acme)
        answers["board"] = {"jobs": [], "meta": {"total": 0}}
        first = await refresh_company(client, await Company.objects.aget(id=acme.id))
        still_open = await Job.objects.filter(company_id=acme.id, is_active=True).acount()
        second = await refresh_company(client, await Company.objects.aget(id=acme.id))

    assert first.ingest is not None and first.ingest.deactivated == 0
    assert still_open == len(BOARD["jobs"])
    assert second.ingest is not None and second.ingest.deactivated == len(BOARD["jobs"])
    assert await Job.objects.filter(company_id=acme.id, is_active=True).acount() == 0


@db
async def test_a_company_without_a_board_or_pages_is_unsupported() -> None:
    page = await company(slug="page", source_type="careers_page", careers_urls=[])
    async with mock_client(routes({})) as client:
        outcome = await refresh_company(client, page)
    assert outcome.status == "unsupported"


@db
async def test_the_pool_keeps_input_order() -> None:
    companies = [await company(slug=f"c{n}", ats_slug=f"c{n}") for n in range(9)]
    handler = routes(
        {greenhouse.board_url(c.slug): httpx2.Response(200, json=BOARD) for c in companies[::2]}
    )
    async with mock_client(handler) as client:
        outcomes = await refresh_companies(client, companies, concurrency=3)
    assert [o.slug for o in outcomes] == [c.slug for c in companies]
    assert [o.status for o in outcomes] == ["ok", "not-found"] * 4 + ["ok"]


def test_staleness_is_two_hours() -> None:
    now = timezone.now()
    assert timedelta(hours=2) == BOARD_STALE_AFTER
    assert stale_cutoff(now) == now - timedelta(hours=2)
    assert not MULTI_BOARD_GATE.locked()


# --- commands ------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_seed_companies_registers_the_seed_list_idempotently() -> None:
    out = io.StringIO()
    call_command("seed_companies", stdout=out)
    assert "seeding 225 companies\u2026" in out.getvalue()
    assert "registered 225" in out.getvalue()
    assert Company.objects.count() == 225
    assert set(Company.objects.values_list("discovered_via", flat=True)) == {"seed"}

    Company.objects.filter(slug="1password").update(status="invalid")
    call_command("seed_companies", stdout=io.StringIO())
    assert Company.objects.count() == 225
    assert Company.objects.get(slug="1password").status == "invalid"


@pytest.mark.django_db
def test_seed_companies_reads_a_careers_page_file() -> None:
    call_command(
        "seed_companies", file=str(FIXTURES / "careers_company.json"), stdout=io.StringIO()
    )
    retool = Company.objects.get(slug="retool")
    assert (retool.source_type, retool.ats_slug, retool.domain) == (
        "careers_page",
        None,
        "retool.com",
    )
    assert retool.careers_urls == ["https://retool.com/careers"]


@pytest.mark.django_db
def test_seed_fetch_refreshes_only_the_loaded_active_companies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_company(slug="unrelated", ats_slug="unrelated")
    refreshed: list[str] = []

    async def fake_refresh(companies: list[Company]) -> list[RefreshOutcome]:
        refreshed.extend(c.slug for c in companies)
        return [RefreshOutcome(c.name, c.slug, "ok", 0) for c in companies]

    monkeypatch.setattr(seed_command, "_refresh", fake_refresh)
    out = io.StringIO()
    call_command(
        "seed_companies", file=str(FIXTURES / "careers_company.json"), fetch=True, stdout=out
    )
    assert refreshed == ["retool"]
    assert "1 boards ok \u00b7 0 gone \u00b7 0 errored" in out.getvalue()
    assert "0 postings seen → 0 inserted, 0 updated, 0 unchanged" in out.getvalue()


def test_fetch_board_prints_postings(monkeypatch: pytest.MonkeyPatch) -> None:
    jobs = [greenhouse.normalize_job("anthropic", job) for job in BOARD["jobs"]]

    async def fake_fetch(source: str, slug: str) -> list:
        assert (source, slug) == ("greenhouse", "anthropic")
        return jobs

    monkeypatch.setattr(fetch_board_command, "_fetch", fake_fetch)
    out = io.StringIO()
    call_command("fetch_board", "greenhouse", "anthropic", limit=2, stdout=out)
    text = out.getvalue()
    assert text.startswith(f"greenhouse:anthropic \u2014 {len(jobs)} postings in ")
    assert f"  id        {jobs[0].id}" in text
    assert "  pay       USD180,000\u2013180,000/year" in text
    assert jobs[2].id not in text
    assert text.rstrip().splitlines()[-1].startswith(f"totals: {len(jobs)} postings \u00b7 ")


def test_fetch_board_usage_errors_exit_2() -> None:
    with pytest.raises(SystemExit) as raised:
        call_command("fetch_board", "workday", "acme", stderr=io.StringIO())
    assert raised.value.code == 2
