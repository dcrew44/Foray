"""Discovery (ported from `discovery.test.ts`, 20 cases), golden equality with the TypeScript
helpers, the whole discovery order against a fake web, and the SSRF refusals (design §10.1,
§10.5, §12.2)."""

from __future__ import annotations

import httpx2
import pytest
from django.conf import settings
from pydantic import SecretStr

from apps.jobs.discovery import (
    AtsLink,
    discover_company,
    find_ats_links,
    find_careers_links,
    has_unsupported_ats,
    normalize_domain,
    register_boards_from_apply_links,
)
from apps.jobs.models import Company
from apps.jobs.sources import ashby, greenhouse, lever
from apps.jobs.sources.jsearch import DiscoveredBoard
from apps.jobs.verify import find_board, slug_candidates, verify_board
from tests.jobs.conftest import fixture_json, fixture_text, golden, html, mock_client, routes

CASES = golden("discovery/cases.json")
GREENHOUSE_BOARD = fixture_json("greenhouse/anthropic.json")
EMPTY_BOARD = {"jobs": [], "meta": {"total": 0}}


def board_ok(source: str = "greenhouse") -> httpx2.Response:
    payloads = {
        "greenhouse": GREENHOUSE_BOARD,
        "lever": fixture_json("lever/spotify.json"),
        "ashby": fixture_json("ashby/linear.json"),
    }
    return httpx2.Response(200, json=payloads[source])


# --- goldens -------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES["careersPages"])
def test_careers_page_helpers_match_golden(case: dict) -> None:
    page = fixture_text(case["fixture"])
    assert [{"source": link.source, "slug": link.slug} for link in find_ats_links(page)] == case[
        "findAtsLinks"
    ]
    assert has_unsupported_ats(page) is case["hasUnsupportedAts"]
    assert find_careers_links(page, case["baseUrl"]) == case["findCareersLinks"]


@pytest.mark.parametrize("case", CASES["normalizeDomain"])
def test_normalize_domain_matches_golden(case: dict) -> None:
    assert normalize_domain(case["input"]) == case["output"]


@pytest.mark.parametrize("case", CASES["slugCandidates"])
def test_slug_candidates_match_golden(case: dict) -> None:
    assert slug_candidates(case["input"]) == case["output"]


# --- findAtsLinks --------------------------------------------------------------------------------


def test_finds_a_greenhouse_board_link() -> None:
    page = '<a href="https://job-boards.greenhouse.io/stripe">Jobs</a>'
    assert find_ats_links(page) == [AtsLink("greenhouse", "stripe")]


def test_finds_the_older_greenhouse_host() -> None:
    page = '<a href="https://boards.greenhouse.io/figma/jobs/123">x</a>'
    assert find_ats_links(page) == [AtsLink("greenhouse", "figma")]


def test_reads_the_slug_out_of_a_greenhouse_embed() -> None:
    page = '<script src="https://boards.greenhouse.io/embed/job_board?for=airtable"></script>'
    assert find_ats_links(page) == [AtsLink("greenhouse", "airtable")]


def test_finds_lever_and_ashby_boards() -> None:
    assert find_ats_links('href="https://jobs.lever.co/spotify"') == [AtsLink("lever", "spotify")]
    assert find_ats_links('href="https://jobs.ashbyhq.com/linear/abc"') == [
        AtsLink("ashby", "linear")
    ]


def test_dedupes_a_board_linked_many_times() -> None:
    page = "".join(
        f'<a href="https://job-boards.greenhouse.io/ramp/jobs/{n}">Role {n}</a>' for n in range(20)
    )
    assert find_ats_links(page) == [AtsLink("greenhouse", "ramp")]


def test_finds_several_distinct_boards() -> None:
    page = 'a href="https://jobs.lever.co/alpha" b href="https://jobs.ashbyhq.com/beta"'
    assert len(find_ats_links(page)) == 2


def test_repeated_calls_find_the_same_links() -> None:
    page = 'href="https://jobs.lever.co/plaid"'
    assert find_ats_links(page) == find_ats_links(page)
    assert len(find_ats_links(page)) == 1


def test_finds_nothing_without_a_board() -> None:
    assert find_ats_links("<html><body>We are not hiring.</body></html>") == []


# --- hasUnsupportedAts, findCareersLinks, normalizeDomain, slugCandidates ------------------------


def test_recognises_boards_without_an_api() -> None:
    assert has_unsupported_ats('href="https://acme.wd1.myworkdayjobs.com/careers"')
    assert has_unsupported_ats('href="https://jobs.smartrecruiters.com/acme"')


def test_does_not_flag_a_readable_board() -> None:
    assert not has_unsupported_ats('href="https://jobs.lever.co/spotify"')


def test_absolutises_a_relative_careers_link() -> None:
    links = find_careers_links('<a href="/about/careers">Join us</a>', "https://acme.com")
    assert links == ["https://acme.com/about/careers"]


def test_picks_up_the_usual_wordings() -> None:
    page = '<a href="/jobs">Jobs</a><a href="/join-us">Join</a><a href="/work-with-us">Work</a>'
    assert len(find_careers_links(page, "https://acme.com")) == 3


def test_ignores_javascript_and_mailto_hrefs() -> None:
    page = '<a href="javascript:openCareers()">Careers</a><a href="mailto:jobs@acme.com">Jobs</a>'
    assert find_careers_links(page, "https://acme.com") == []


def test_ignores_unrelated_links() -> None:
    assert find_careers_links('<a href="/pricing">Pricing</a>', "https://acme.com") == []


def test_normalize_domain_strips_scheme_www_and_path() -> None:
    for value in (
        "https://www.stripe.com/careers",
        "stripe.com",
        "HTTPS://STRIPE.COM",
        "www.stripe.com",
    ):
        assert normalize_domain(value) == "stripe.com"


def test_normalize_domain_keeps_a_meaningful_subdomain() -> None:
    assert normalize_domain("https://jobs.acme.co.uk/list") == "jobs.acme.co.uk"


def test_normalize_domain_is_none_for_nothing() -> None:
    assert normalize_domain("   ") is None


def test_slug_candidates_offer_the_obvious_forms_first() -> None:
    candidates = slug_candidates("Match Group")
    assert candidates[0] == "matchgroup"
    assert "match-group" in candidates
    assert "match" in candidates


def test_slug_candidates_drop_legal_suffixes_and_punctuation() -> None:
    assert slug_candidates("Stripe, Inc.")[0] == "stripe"
    assert slug_candidates("Ben & Jerry's")[0] == "benandjerrys"


def test_slug_candidates_are_empty_without_letters() -> None:
    assert slug_candidates("   ") == []


# --- verification --------------------------------------------------------------------------------


async def test_verify_board_outcomes(no_backoff: list[float]) -> None:
    handler = routes(
        {
            greenhouse.board_url("real"): board_ok(),
            greenhouse.board_url("quiet"): httpx2.Response(200, json=EMPTY_BOARD),
            greenhouse.board_url("broken"): httpx2.Response(500, text="oops"),
        }
    )
    async with mock_client(handler) as client:
        ok = await verify_board(client, "greenhouse", "real")
        empty = await verify_board(client, "greenhouse", "quiet")
        missing = await verify_board(client, "greenhouse", "nobody")
        error = await verify_board(client, "greenhouse", "broken")
    assert (ok.status, ok.job_count) == ("ok", len(GREENHOUSE_BOARD["jobs"]))
    assert empty.status == "empty"
    assert missing.status == "not-found"
    assert error.status == "error" and error.error and "500" in error.error


async def test_find_board_prefers_a_board_with_jobs() -> None:
    handler = routes(
        {
            greenhouse.board_url("acme"): httpx2.Response(200, json=EMPTY_BOARD),
            ashby.board_url("acme"): board_ok("ashby"),
        }
    )
    async with mock_client(handler) as client:
        found = await find_board(client, "acme")
        nothing = await find_board(client, "nobody")
    assert found is not None and (found.source, found.status) == ("ashby", "ok")
    assert nothing is None


# --- the discovery order -------------------------------------------------------------------------

pytestmark_db = pytest.mark.django_db(transaction=True)


@pytestmark_db
async def test_a_pasted_board_link_is_verified_and_registered() -> None:
    handler = routes({lever.board_url("acme_corp"): board_ok("lever")})
    async with mock_client(handler) as client:
        result = await discover_company(client, "https://jobs.lever.co/acme_corp/123")
    assert result.trail[0] == {"step": "ats-link", "detail": "pasted lever:acme_corp"}
    assert result.registered is not None and result.created
    assert (result.registered.source_type, result.registered.ats_slug) == ("lever", "acme_corp")
    assert result.registered.slug == "acme-corp"  # normalised; the ATS slug is kept


@pytestmark_db
async def test_a_domain_follows_its_careers_page_to_the_board() -> None:
    page = '<a href="https://job-boards.greenhouse.io/acme">Open roles</a>'
    handler = routes(
        {
            "https://acme.com/careers": html(page),
            greenhouse.board_url("acme"): board_ok(),
        }
    )
    async with mock_client(handler) as client:
        result = await discover_company(client, "acme.com", added_by_id=None, tags=["ai"])
    assert [step["step"] for step in result.trail] == ["domain", "careers-page"]
    assert result.trail[1]["detail"] == "https://acme.com/careers → greenhouse:acme"
    company = result.registered
    assert company is not None
    assert (company.name, company.domain, company.slug, company.tags) == (
        "Acme",
        "acme.com",
        "acme",
        ["ai"],
    )
    assert company.careers_urls == ["https://acme.com/careers"]
    assert company.discovered_via == "agent"


@pytestmark_db
async def test_an_unsupported_ats_registers_a_careers_page() -> None:
    page = '<a href="https://acme.wd1.myworkdayjobs.com/External">Jobs</a>'
    handler = routes({"https://acme.com/jobs": html(page)})
    async with mock_client(handler) as client:
        result = await discover_company(client, "https://www.acme.com")
    assert result.trail[-1]["step"] == "unsupported-ats"
    company = result.registered
    assert company is not None
    assert (company.source_type, company.ats_slug, company.needs_browser) == (
        "careers_page",
        None,
        True,
    )
    assert company.careers_urls == ["https://acme.com/jobs"]


@pytestmark_db
async def test_the_homepage_link_is_followed_when_no_path_loads() -> None:
    handler = routes(
        {
            "https://acme.com/": html('<a href="/about/join-us">We are hiring</a>'),
            "https://acme.com/about/join-us": html('<a href="https://jobs.ashbyhq.com/acme">x</a>'),
            ashby.board_url("acme"): board_ok("ashby"),
        }
    )
    async with mock_client(handler) as client:
        result = await discover_company(client, "acme.com")
    steps = [step["step"] for step in result.trail]
    assert steps == ["domain", "homepage", "homepage-ats-link"]
    assert result.source == "ashby"


@pytestmark_db
async def test_slug_guesses_find_a_board() -> None:
    handler = routes({greenhouse.board_url("matchgroup"): board_ok()})
    async with mock_client(handler) as client:
        result = await discover_company(client, "Match Group")
    assert result.trail == [{"step": "slug-guess", "detail": "greenhouse:matchgroup"}]
    assert result.registered is not None
    assert result.registered.name == "Match Group"


@pytestmark_db
async def test_search_finds_a_careers_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.ENV, "search_api_key", SecretStr("brave-test"))
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if request.url.host == "api.search.brave.com":
            assert request.headers["x-subscription-token"] == "brave-test"
            assert request.url.params["q"] == "Obscure Co careers jobs"
            return httpx2.Response(
                200, json={"web": {"results": [{"url": "https://obscure.io/work"}, {}]}}
            )
        if str(request.url) == "https://obscure.io/work":
            return html("<h1>Join us</h1><p>We hire engineers.</p>")
        return httpx2.Response(404, text="Not Found")

    async with mock_client(handler) as client:
        result = await discover_company(client, "Obscure Co")
    steps = [step["step"] for step in result.trail]
    assert steps == ["slug-guess", "search", "careers-page-fallback"]
    assert result.trail[1]["detail"] == "1 results"
    company = result.registered
    assert company is not None
    assert (company.source_type, company.careers_urls) == (
        "careers_page",
        ["https://obscure.io/work"],
    )
    assert company.slug == "obscure-co"


@pytestmark_db
async def test_nothing_found_is_not_registered() -> None:
    async with mock_client(routes({})) as client:
        result = await discover_company(client, "Nobody At All")
    assert result.registered is None
    assert result.trail == [
        {"step": "slug-guess", "detail": "no board for nobodyatall, nobody-at-all, nobody"},
        {"step": "search", "detail": "0 results"},
    ]
    assert await Company.objects.acount() == 0


@pytestmark_db
async def test_register_false_writes_nothing() -> None:
    handler = routes({greenhouse.board_url("stripe"): board_ok()})
    async with mock_client(handler) as client:
        result = await discover_company(client, "Stripe", register=False)
    assert (result.source, result.slug, result.registered) == ("greenhouse", "stripe", None)
    assert await Company.objects.acount() == 0


@pytestmark_db
async def test_an_internal_url_is_refused_and_recorded() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.host != "169.254.169.254", "the guard must refuse it before sending"
        return httpx2.Response(404, text="Not Found")

    async with mock_client(handler) as client:
        result = await discover_company(client, "http://169.254.169.254/latest/meta-data")
    assert result.registered is None
    assert {"step": "blocked", "detail": "That address is not reachable from Foray."} in (
        result.trail
    )
    assert [step["step"] for step in result.trail].count("blocked") == 1


@pytestmark_db
async def test_a_name_resolving_inside_the_network_is_refused() -> None:
    async with mock_client(routes({}), addresses={"corp.internal.example": ["10.0.0.7"]}) as client:
        result = await discover_company(client, "corp.internal.example")
    assert {"step": "blocked", "detail": "That address is not reachable from Foray."} in (
        result.trail
    )


@pytestmark_db
async def test_rediscovery_reactivates_an_invalid_company() -> None:
    await Company.objects.acreate(
        name="Gone",
        slug="gone",
        source_type="greenhouse",
        ats_slug="gone",
        status="invalid",
        discovered_via="seed",
    )
    handler = routes({greenhouse.board_url("gone"): board_ok()})
    async with mock_client(handler) as client:
        result = await discover_company(client, "Gone")
    assert result.registered is not None and not result.created
    assert result.registered.status == "active"


# --- boards from apply links ---------------------------------------------------------------------


@pytestmark_db
async def test_apply_link_boards_are_registered_only_when_verified_ok() -> None:
    await Company.objects.acreate(
        name="Known", slug="known", source_type="lever", ats_slug="known", discovered_via="seed"
    )
    handler = routes(
        {
            greenhouse.board_url("ramp"): board_ok(),
            greenhouse.board_url("quiet"): httpx2.Response(200, json=EMPTY_BOARD),
        }
    )
    boards = [
        DiscoveredBoard("greenhouse", "ramp", "Ramp"),
        DiscoveredBoard("greenhouse", "quiet", "Quiet"),
        DiscoveredBoard("greenhouse", "gone", "Gone"),
        DiscoveredBoard("lever", "known", "Known"),
    ]
    async with mock_client(handler) as client:
        registered = await register_boards_from_apply_links(client, boards)
    assert [(c.name, c.slug, c.discovered_via) for c in registered] == [("Ramp", "ramp", "jsearch")]
    assert await Company.objects.acount() == 2
