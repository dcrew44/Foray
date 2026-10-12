"""Careers pages (ported from `careers-page.test.ts`, 7 cases) plus the listing cache of design
§10.5: a second refresh of an unchanged two-page site makes 0 model calls and still follows page
2 and fetches every posting page; a changed page 2 re-extracts only page 2; a changed posting
page updates that job; a bumped `EXTRACTOR_VERSION` re-extracts; `needs_browser` is unchanged by
a hit.

The extraction model is replaced by `structured_stub`, which answers from a scripted listing per
page and records every call: step 3B's `structured()` is a stub in this lane.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import httpx2
import pytest
from asgiref.sync import sync_to_async
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from apps.jobs import fakes
from apps.jobs.models import Company, Job
from apps.jobs.refresh import refresh_company
from apps.jobs.schemas import ExtractedPosting, ListingCacheEntry, read_listing_cache
from apps.jobs.sources import careers_page
from apps.jobs.sources.careers_page import (
    EXTRACTOR_VERSION,
    LISTING_INSTRUCTIONS,
    MAX_LISTING_CHARS,
    ExtractedListing,
    fetch_careers_page,
    listing_prompt,
)
from apps.jobs.sources.clean import html_to_text, html_to_text_with_links
from apps.jobs.sources.dedupe import sha1_hex, web_job_id
from tests.factories import make_company
from tests.jobs.conftest import FIXTURES, GOLDENS, fixture_text, golden, html, mock_client

retool = fixture_text("careers-pages/retool.html")
crusoe = fixture_text("careers-pages/crusoe.html")

# --- links survive the conversion (careers-page.test.ts) -----------------------------------------


@pytest.mark.parametrize("case", golden("links/cases.json"))
def test_html_to_text_with_links_matches_golden(case: dict) -> None:
    assert html_to_text_with_links(case["html"], case["baseUrl"]) == case["output"]


@pytest.mark.parametrize(
    ("slug", "base"),
    [("retool", "https://retool.com/careers"), ("crusoe", "https://www.crusoe.ai/about/careers")],
)
def test_real_pages_match_golden(slug: str, base: str) -> None:
    page = (FIXTURES / f"careers-pages/{slug}.html").read_text()
    assert (
        html_to_text_with_links(page, base)
        == (GOLDENS / f"links/{slug}.with-links.txt").read_text()
    )
    assert html_to_text(page) == (GOLDENS / f"links/{slug}.text.txt").read_text()


def test_keeps_a_link_target_that_plain_text_drops() -> None:
    page = '<a href="/careers/swe">Software Engineer</a>'
    assert html_to_text(page) == "Software Engineer"
    assert "[Software Engineer](/careers/swe)" in html_to_text_with_links(page)


def test_absolutises_against_the_page() -> None:
    text = html_to_text_with_links('<a href="/careers/swe">SWE</a>', "https://acme.com/careers")
    assert "[SWE](https://acme.com/careers/swe)" in text


def test_keeps_the_label_but_drops_navigation_targets() -> None:
    text = html_to_text_with_links(
        '<a href="#top">Back to top</a><a href="mailto:jobs@acme.com">Email us</a>',
        "https://acme.com",
    )
    assert "Back to top" in text
    assert "mailto:" not in text


def test_strips_markup_inside_a_label() -> None:
    text = html_to_text_with_links('<a href="/x"><span>Staff</span> <b>Engineer</b></a>')
    assert "[Staff Engineer](/x)" in text


def test_drops_a_link_without_a_readable_label() -> None:
    assert "](/x)" not in html_to_text_with_links('<a href="/x"><img src="y.png"></a>')


def test_surfaces_real_posting_links_from_a_real_page() -> None:
    text = html_to_text_with_links(retool, "https://retool.com/careers")
    assert re.search(r"\[[^\]]*Engineer[^\]]*\]\(https://retool\.com/careers/", text)
    assert "](https://retool.com/careers/" not in html_to_text(retool)


def test_a_javascript_rendered_page_has_nothing_posting_shaped() -> None:
    def distinct(page: str, base: str) -> set[str]:
        links = re.findall(r"\]\((https?://[^)]+)\)", html_to_text_with_links(page, base))
        return {url for url in links if re.search(r"job|career|position|opening", url, re.I)}

    assert len(distinct(crusoe, "https://www.crusoe.ai/careers")) < 6
    assert len(distinct(retool, "https://retool.com/careers")) > 10


# --- the extraction prompt -----------------------------------------------------------------------


def test_the_prompt_wraps_page_text_as_untrusted() -> None:
    assert listing_prompt("Acme", "https://acme.com/careers", "Jobs") == (
        "Careers page for Acme at https://acme.com/careers.\n\n"
        '<untrusted source="careers page">\nJobs\n</untrusted>'
    )


# --- a fake careers site -------------------------------------------------------------------------

LISTING = "https://acme.example.com/careers"
PAGE_2 = "https://acme.example.com/careers?page=2"


def posting_url(n: int) -> str:
    return f"https://acme.example.com/careers/{n}"


@dataclass
class Site:
    """Pages served by the mock transport, the scripted extraction of each listing page, and a
    log of what was fetched and what the model was asked."""

    pages: dict[str, str] = field(default_factory=dict)
    listings: dict[str, ExtractedListing] = field(default_factory=dict)
    fetched: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    fail_on: set[str] = field(default_factory=set)

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        self.fetched.append(url)
        body = self.pages.get(url)
        return html(body) if body is not None else httpx2.Response(404, text="Not Found")

    async def structured(
        self,
        purpose: str,
        output: type[Any],
        *,
        instructions: str,
        prompt: str,
        timeout: float | None = None,  # noqa: ASYNC109 (structured()'s signature, §8.4)
    ) -> ExtractedListing:
        url = re.match(r"Careers page for .* at (\S+)\.\n", prompt)
        assert url is not None
        self.calls.append(
            {
                "purpose": purpose,
                "output": output,
                "instructions": instructions,
                "url": url.group(1),
                "prompt": prompt,
            }
        )
        if url.group(1) in self.fail_on:
            raise RuntimeError("the model is down")
        return self.listings[url.group(1)]

    def called(self) -> list[str]:
        return [call["url"] for call in self.calls]


def listing(*postings: tuple[str, str | None], more: list[str] | None = None) -> ExtractedListing:
    return ExtractedListing(
        postings=[
            ExtractedPosting(title=title, location="San Francisco, CA", url=url, team="Eng")
            for title, url in postings
        ],
        more_listing_urls=more or [],
    )


@pytest.fixture
def site(monkeypatch: pytest.MonkeyPatch) -> Site:
    site = Site(
        pages={
            LISTING: '<a href="/careers/1">Engineer</a><a href="/careers/2">Designer</a>'
            '<a href="?page=2">Next</a>',
            PAGE_2: '<a href="/careers/3">Analyst</a>',
            posting_url(1): "<p>Build things. $150,000 - $200,000 per year.</p>",
            posting_url(2): "<p>Design things.</p>",
            posting_url(3): "<p>Analyse things.</p>",
        },
        listings={
            LISTING: listing(
                ("Engineer", "/careers/1"), ("Designer", posting_url(2)), more=["?page=2"]
            ),
            PAGE_2: listing(("Analyst", "/careers/3")),
        },
    )
    monkeypatch.setattr(careers_page, "structured", site.structured)
    return site


def careers_company(**fields: Any) -> Company:
    fields.setdefault("careers_urls", [LISTING])
    return make_company(name="Acme", slug="acme", source_type="careers_page", **fields)


async def refresh(site: Site, company_id: Any) -> tuple[Any, Company]:
    company = await Company.objects.aget(id=company_id)
    site.fetched.clear()
    site.calls.clear()
    async with mock_client(site.handler) as client:
        outcome = await refresh_company(client, company)
    return outcome, await Company.objects.aget(id=company_id)


db = pytest.mark.django_db(transaction=True)


@db
async def test_first_refresh_extracts_both_pages_and_ingests_every_posting(site: Site) -> None:
    company = await _acompany()
    outcome, company = await refresh(site, company.id)

    assert outcome.status == "ok"
    assert outcome.model_calls == 2
    assert outcome.ingest is not None and outcome.ingest.inserted == 3
    assert site.called() == [LISTING, PAGE_2]
    call = site.calls[0]
    assert (call["purpose"], call["output"], call["instructions"]) == (
        "extract",
        ExtractedListing,
        LISTING_INSTRUCTIONS,
    )
    assert '<untrusted source="careers page">\n' in call["prompt"]

    jobs = {job.id: job async for job in Job.objects.all()}
    engineer = jobs[web_job_id("acme", posting_url(1))]
    assert engineer.source == "web"
    assert engineer.external_id == engineer.id.split(":")[-1]
    assert engineer.description_raw == engineer.description_text
    assert engineer.salary_min == 150000
    assert engineer.posted_at is None
    assert engineer.raw == {
        "source": "careers-page",
        "team": "Eng",
        "listing": {"title": "Engineer", "location": "San Francisco, CA", "team": "Eng"},
    }

    cache = read_listing_cache(company.listing_cache)
    assert set(cache) == {LISTING, PAGE_2}
    assert cache[LISTING].v == EXTRACTOR_VERSION
    sent = html_to_text_with_links(site.pages[LISTING], LISTING)[:MAX_LISTING_CHARS]
    assert cache[LISTING].sha1 == sha1_hex(sent)
    assert company.listing_cache[LISTING]["moreListingUrls"] == ["?page=2"]
    assert (company.last_fetch_status, company.last_fetch_job_count) == ("ok", 3)
    assert company.needs_browser is False


@db
async def test_an_unchanged_site_makes_no_model_calls(site: Site) -> None:
    company = await _acompany()
    await refresh(site, company.id)
    outcome, company = await refresh(site, company.id)

    assert site.calls == []
    assert outcome.model_calls == 0
    # Page 2 is still followed and every posting page is still fetched.
    assert site.fetched == [LISTING, PAGE_2, posting_url(1), posting_url(2), posting_url(3)]
    assert outcome.ingest is not None and outcome.ingest.unchanged == 3
    assert outcome.ingest.inserted == outcome.ingest.updated == 0


@db
async def test_a_changed_page_two_re_extracts_only_page_two(site: Site) -> None:
    company = await _acompany()
    await refresh(site, company.id)
    site.pages[PAGE_2] += '<a href="/careers/4">Writer</a>'
    site.pages[posting_url(4)] = "<p>Write things.</p>"
    site.listings[PAGE_2] = listing(("Analyst", "/careers/3"), ("Writer", "/careers/4"))

    outcome, company = await refresh(site, company.id)

    assert site.called() == [PAGE_2]
    assert outcome.ingest is not None and outcome.ingest.inserted == 1
    assert len(company.listing_cache[PAGE_2]["postings"]) == 2


@db
async def test_a_changed_posting_page_updates_that_job(site: Site) -> None:
    company = await _acompany()
    await refresh(site, company.id)
    site.pages[posting_url(2)] = "<p>Design better things.</p>"

    outcome, _ = await refresh(site, company.id)

    assert site.calls == []
    assert outcome.ingest is not None
    assert (outcome.ingest.updated, outcome.ingest.unchanged) == (1, 2)
    job = await Job.objects.aget(id=web_job_id("acme", posting_url(2)))
    assert job.description_text == "Design better things."


@db
async def test_a_bumped_extractor_version_re_extracts(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    company = await _acompany()
    await refresh(site, company.id)
    monkeypatch.setattr(careers_page, "EXTRACTOR_VERSION", EXTRACTOR_VERSION + 1)

    outcome, company = await refresh(site, company.id)

    assert site.called() == [LISTING, PAGE_2]
    assert outcome.model_calls == 2
    assert company.listing_cache[LISTING]["v"] == EXTRACTOR_VERSION + 1


@db
async def test_needs_browser_is_unchanged_by_a_hit(site: Site) -> None:
    site.pages[LISTING] = "<div id='app'>Loading\u2026</div>"
    site.listings[LISTING] = listing()
    company = await _acompany()

    first, company = await refresh(site, company.id)
    assert (first.model_calls, company.needs_browser, company.last_fetch_status) == (
        1,
        True,
        "needs-browser",
    )

    second, company = await refresh(site, company.id)
    assert second.model_calls == 0
    assert (company.needs_browser, company.last_fetch_status) == (True, "needs-browser")


@db
async def test_needs_browser_is_cleared_when_postings_appear(site: Site) -> None:
    company = await _acompany(needs_browser=True)
    _, company = await refresh(site, company.id)
    assert company.needs_browser is False


@db
async def test_entries_for_unreached_listing_pages_are_dropped(site: Site) -> None:
    stale = ListingCacheEntry(v=EXTRACTOR_VERSION, sha1="0" * 40, postings=[], more_listing_urls=[])
    company = await _acompany(listing_cache={"https://acme.example.com/old-careers": stale.dump()})
    _, company = await refresh(site, company.id)
    assert set(company.listing_cache) == {LISTING, PAGE_2}


@db
async def test_a_failed_crawl_keeps_what_it_paid_for(site: Site) -> None:
    stale = ListingCacheEntry(v=EXTRACTOR_VERSION, sha1="0" * 40, postings=[], more_listing_urls=[])
    company = await _acompany(
        listing_cache={"https://acme.example.com/old-careers": stale.dump()},
        last_fetch_job_count=7,
    )
    site.fail_on = {PAGE_2}

    outcome, company = await refresh(site, company.id)

    assert outcome.status == "error"
    assert set(company.listing_cache) == {
        LISTING,
        "https://acme.example.com/old-careers",
    }
    assert (company.last_fetch_status, company.last_fetch_job_count) == ("error", 7)
    assert company.status == "active"  # a careers page is never marked invalid


@db
async def test_careers_pages_never_deactivate(site: Site) -> None:
    company = await _acompany()
    await refresh(site, company.id)
    site.listings[PAGE_2] = listing()
    site.pages[PAGE_2] = "<p>Nothing here today.</p>"

    outcome, _ = await refresh(site, company.id)

    assert outcome.ingest is not None and outcome.ingest.deactivated == 0
    analyst = await Job.objects.aget(id=web_job_id("acme", posting_url(3)))
    assert analyst.is_active


# --- the crawl itself ----------------------------------------------------------------------------


async def test_skips_postings_without_a_usable_link(site: Site) -> None:
    site.listings[LISTING] = listing(
        ("No link", None),
        ("Script", "javascript:apply()"),
        ("Engineer", "/careers/1"),
        ("Engineer again", "https://acme.example.com/careers/1"),
    )
    async with mock_client(site.handler) as client:
        result = await fetch_careers_page(client, name="Acme", slug="acme", careers_urls=[LISTING])
    # No link, a javascript: link and a repeat of the same URL are not jobs.
    assert [job.url for job in result.jobs] == [posting_url(1)]
    assert result.jobs[0].title == "Engineer"


async def test_caps_postings_and_listing_pages(site: Site) -> None:
    pages = [f"{LISTING}?p={n}" for n in range(8)]
    for n, url in enumerate(pages):
        site.pages[url] = f"<p>page {n}</p>"
        site.listings[url] = listing(
            *[(f"Role {n}.{i}", f"/careers/{n}-{i}") for i in range(3)],
            more=[pages[n + 1]] if n + 1 < len(pages) else [],
        )
    async with mock_client(site.handler) as client:
        result = await fetch_careers_page(
            client, name="Acme", slug="acme", careers_urls=[pages[0]], max_postings=7
        )
    assert result.pages_fetched == 5
    assert len(result.jobs) == 7
    assert result.postings_fetched == 0  # those posting pages do not exist
    assert result.needs_browser is False


async def test_model_emitted_internal_urls_are_never_fetched(site: Site) -> None:
    site.listings[LISTING] = listing(
        ("Metadata", "http://169.254.169.254/latest/meta-data/"),
        more=["http://127.0.0.1:8000/admin"],
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == "acme.example.com", request.url
        return site.handler(request)

    async with mock_client(handler) as client:
        result = await fetch_careers_page(client, name="Acme", slug="acme", careers_urls=[LISTING])
    assert result.pages_fetched == 1
    assert result.postings_fetched == 0
    assert [job.url for job in result.jobs] == ["http://169.254.169.254/latest/meta-data/"]
    assert result.jobs[0].description_raw is None


async def test_a_page_that_fails_to_load_is_skipped_without_counting(site: Site) -> None:
    async with mock_client(site.handler) as client:
        result = await fetch_careers_page(
            client, name="Acme", slug="acme", careers_urls=["https://acme.example.com/missing"]
        )
    assert (result.pages_fetched, result.jobs, result.needs_browser) == (0, [], False)
    assert site.calls == []


# --- the LLM_FAKE override -----------------------------------------------------------------------


def test_the_fake_reads_the_listing_links_as_postings() -> None:
    text = html_to_text_with_links(retool, "https://retool.com/careers")[:MAX_LISTING_CHARS]
    result = fakes.fake_listing(listing_prompt("Retool", "https://retool.com/careers", text))
    assert len(result.postings) > 10
    for item in result.postings:
        assert item.url is not None and item.url.startswith("https://retool.com/careers/")
    assert result.more_listing_urls == []


async def test_the_fake_answers_through_a_real_output_tool() -> None:
    def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return fakes.careers_listing_reply(messages, info) or ModelResponse(parts=[TextPart("?")])

    agent = Agent(
        FunctionModel(reply), output_type=ExtractedListing, instructions=LISTING_INSTRUCTIONS
    )
    page = '<a href="/careers/1">Engineer</a><a href="/pricing">Pricing</a>'
    text = html_to_text_with_links(page, LISTING)
    result = await agent.run(listing_prompt("Acme", LISTING, text))
    assert result.output.postings == [
        ExtractedPosting(title="Engineer", location=None, url=posting_url(1), team=None)
    ]


async def test_the_fake_leaves_other_extract_requests_alone() -> None:
    seen: list[ModelResponse | None] = []

    def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(fakes.careers_listing_reply(messages, info))
        return ModelResponse(parts=[TextPart("plain")])

    agent = Agent(FunctionModel(reply), instructions="Read this resume.")
    await agent.run("Jane Doe, engineer")
    assert seen == [None]


# --- helpers -------------------------------------------------------------------------------------


async def _acompany(**fields: Any) -> Company:
    return await sync_to_async(careers_company)(**fields)
