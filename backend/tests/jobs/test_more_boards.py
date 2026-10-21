"""Workable, Recruitee and Gem against real captured payloads.

These adapters postdate the TypeScript app, so there are no goldens: each test pins one quirk the
captured data actually has (hidden Workable locations, Recruitee's stringly amounts and split
description, Gem's opaque ids and explicit workplace).
"""

from __future__ import annotations

import re
from collections.abc import Callable

import httpx2
import pytest

from apps.jobs.discovery import AtsLink, find_ats_links, has_unsupported_ats
from apps.jobs.sources import BOARD_ADAPTERS, BOARD_SOURCES, gem, recruitee, workable
from apps.jobs.sources.jsearch import DiscoveredBoard, ats_from_apply_options
from apps.jobs.sources.types import BoardNotFound
from tests.jobs.conftest import fixture_json, fixture_text, mock_client, routes

huggingface = fixture_json("workable/huggingface.json")
blueground = fixture_json("workable/blueground.json")
channable = fixture_json("recruitee/channable.json")
retool = fixture_json("gem/retool.json")

hf_jobs = [
    workable.normalize_job("huggingface", job, huggingface["name"]) for job in huggingface["jobs"]
]
blue_jobs = [
    workable.normalize_job("blueground", job, blueground["name"]) for job in blueground["jobs"]
]
channable_jobs = [recruitee.normalize_job("channable", offer) for offer in channable["offers"]]
retool_jobs = [gem.normalize_job("retool", job) for job in retool]

FETCHES = [
    ("workable", "huggingface", workable.board_url, huggingface, len(huggingface["jobs"])),
    ("recruitee", "channable", recruitee.board_url, channable, len(channable["offers"])),
    ("gem", "retool", gem.board_url, retool, len(retool)),
]


@pytest.mark.parametrize(("source", "slug", "url", "payload", "count"), FETCHES)
async def test_fetch_board_through_the_client(
    source: str, slug: str, url: Callable[[str], str], payload: object, count: int
) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=payload)

    async with mock_client(handler) as client:
        jobs = await BOARD_ADAPTERS[source].fetch_board(client, slug)

    assert [str(request.url) for request in seen] == [url(slug)]
    assert len(jobs) == count
    assert all(job.id.startswith(f"{source}:{slug}:") for job in jobs)


@pytest.mark.parametrize(
    ("source", "url", "fixture"),
    [
        ("workable", workable.board_url, "workable/_not-found.txt"),
        ("recruitee", recruitee.board_url, "recruitee/_not-found.txt"),
        ("gem", gem.board_url, "gem/_not-found.txt"),
    ],
)
async def test_a_404_is_board_not_found(
    source: str, url: Callable[[str], str], fixture: str
) -> None:
    slug = "foray-no-such-company-xyz123"
    handler = routes({url(slug): httpx2.Response(404, text=fixture_text(fixture))})
    async with mock_client(handler) as client:
        with pytest.raises(BoardNotFound) as raised:
            await BOARD_ADAPTERS[source].fetch_board(client, slug)
    assert str(raised.value) == f'No {source} board for slug "{slug}"'


@pytest.mark.parametrize(
    ("source", "url", "empty"),
    [
        ("workable", workable.board_url, {"name": "Quiet", "description": "", "jobs": []}),
        ("recruitee", recruitee.board_url, {"offers": []}),
        ("gem", gem.board_url, []),
    ],
)
async def test_an_empty_board_is_not_a_404(
    source: str, url: Callable[[str], str], empty: object
) -> None:
    handler = routes({url("quiet"): httpx2.Response(200, json=empty)})
    async with mock_client(handler) as client:
        assert await BOARD_ADAPTERS[source].fetch_board(client, "quiet") == []


def test_the_new_boards_are_tried_after_the_original_three() -> None:
    assert BOARD_SOURCES[:3] == ("greenhouse", "lever", "ashby")
    assert set(BOARD_SOURCES) == set(BOARD_ADAPTERS)


# --- workable ------------------------------------------------------------------------------------


def test_workable_takes_the_company_name_from_the_board() -> None:
    assert {job.company_name for job in hf_jobs} == {"Hugging Face"}


def test_workable_keeps_a_shown_location_as_city_region_country() -> None:
    first = hf_jobs[1]
    assert first.locations == ["New York, New York, United States"]
    assert first.location_norm is not None
    assert (first.location_norm.city, first.location_norm.region) == ("New York", "NY")


def test_workable_does_not_offer_a_hidden_office_as_the_place() -> None:
    hidden = next(
        job for job in huggingface["jobs"] if all(loc["hidden"] for loc in job["locations"])
    )
    assert hidden["locations"][0]["city"] == "Paris"
    job = workable.normalize_job("huggingface", hidden, "Hugging Face")
    assert job.locations == ["France"]


def test_workable_telecommuting_is_remote_and_its_absence_reads_the_place() -> None:
    assert {job.remote for job in hf_jobs} == {"remote"}
    ios = next(job for job in blue_jobs if "iOS" in job.title)
    assert ios.remote == "onsite"


def test_workable_reads_pay_from_the_description_and_dates_from_published_on() -> None:
    manager = next(job for job in blue_jobs if job.title == "Warehouse Manager")
    assert (manager.salary.min, manager.salary.max, manager.salary.currency) == (
        60_000,
        70_000,
        "USD",
    )
    assert manager.posted_at is not None and manager.posted_at.isoformat().startswith("2026-10-09")


def test_workable_apply_url_is_its_own_page() -> None:
    job = hf_jobs[0]
    assert job.url == "https://apply.workable.com/j/81B46579FE"
    assert job.apply_url == "https://apply.workable.com/j/81B46579FE/apply"


# --- recruitee -----------------------------------------------------------------------------------


def test_recruitee_stitches_the_requirements_onto_the_description() -> None:
    offer = channable["offers"][0]
    job = channable_jobs[0]
    assert job.description_raw == f"{offer['description']}\n{offer['requirements']}"
    assert job.description_text and "<" not in job.description_text[:200]


def test_recruitee_reads_string_amounts_as_a_salary() -> None:
    haskell = channable_jobs[0]
    assert (haskell.salary.min, haskell.salary.max) == (4000, 5700)
    assert (haskell.salary.currency, haskell.salary.period) == ("EUR", "month")


def test_recruitee_treats_a_zero_salary_as_none() -> None:
    open_application = channable_jobs[3]
    assert channable["offers"][3]["salary"]["min"] == "0"
    assert open_application.salary.min is None


def test_recruitee_workplace_booleans_win() -> None:
    assert [job.remote for job in channable_jobs] == ["hybrid", "remote", "hybrid", "hybrid"]


def test_recruitee_reads_its_utc_dates() -> None:
    assert channable_jobs[0].posted_at is not None
    assert channable_jobs[0].posted_at.isoformat() == "2026-10-20T09:26:03+00:00"


def test_recruitee_uses_the_company_name_and_the_careers_urls() -> None:
    job = channable_jobs[0]
    assert job.company_name == "Channable"
    assert job.url.startswith("https://") and job.apply_url and job.apply_url.endswith("/c/new")


async def test_recruitee_refuses_a_slug_that_cannot_be_a_subdomain() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError(f"fetched {request.url}")

    async with mock_client(handler) as client:
        with pytest.raises(BoardNotFound):
            await recruitee.fetch_board(client, "acme_corp")


async def test_recruitee_skips_an_unpublished_offer() -> None:
    draft = dict(channable["offers"][0], status="draft")
    handler = routes(
        {recruitee.board_url("channable"): httpx2.Response(200, json={"offers": [draft]})}
    )
    async with mock_client(handler) as client:
        assert await recruitee.fetch_board(client, "channable") == []


# --- gem -----------------------------------------------------------------------------------------


def test_gem_states_the_workplace_outright() -> None:
    assert [job.remote for job in retool_jobs] == ["remote", "onsite", "hybrid", "hybrid"]


def test_gem_lists_every_office() -> None:
    apps = retool_jobs[2]
    assert apps.locations == ["San Francisco, United States", "New York, United States"]
    assert apps.location_norm is not None and apps.location_norm.metro == "bay-area"


def test_gem_reads_pay_from_the_description() -> None:
    assert all(job.salary.currency == "USD" and job.salary.min for job in retool_jobs)


def test_gem_keeps_opaque_and_numeric_ids() -> None:
    assert retool_jobs[1].external_id == "4618972005"
    assert not retool_jobs[0].external_id.isdigit()


def test_gem_posting_page_is_the_application() -> None:
    assert all(job.apply_url is None for job in retool_jobs)
    assert all(re.match(r"https://jobs\.gem\.com/retool/", job.url) for job in retool_jobs)


# --- links ---------------------------------------------------------------------------------------


def test_discovery_finds_the_new_boards() -> None:
    page = (
        '<a href="https://apply.workable.com/huggingface/">a</a>'
        '<a href="https://channable.recruitee.com/o/haskell">b</a>'
        '<a href="https://jobs.gem.com/retool/4003629005">c</a>'
        '<a href="https://blueground.workable.com/">d</a>'
    )
    assert find_ats_links(page) == [
        AtsLink("workable", "huggingface"),
        AtsLink("workable", "blueground"),
        AtsLink("recruitee", "channable"),
        AtsLink("gem", "retool"),
    ]


def test_a_workable_job_link_names_no_account() -> None:
    page = (
        '<a href="https://apply.workable.com/j/81B46579FE">x</a>'
        '<a href="https://www.workable.com/">y</a>'
    )
    assert find_ats_links(page) == []


def test_the_new_boards_are_no_longer_unsupported() -> None:
    assert not has_unsupported_ats('href="https://apply.workable.com/huggingface/"')
    assert not has_unsupported_ats('href="https://channable.recruitee.com/"')
    assert not has_unsupported_ats('href="https://jobs.gem.com/retool"')


def test_a_jsearch_apply_link_can_point_at_a_new_board() -> None:
    job = {"job_id": "1", "job_apply_link": "https://jobs.gem.com/retool/4003629005"}
    assert ats_from_apply_options(job) == DiscoveredBoard("gem", "retool")
