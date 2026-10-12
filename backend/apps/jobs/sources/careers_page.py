"""The fallback for companies with no readable API (design §10.5; `map/worker.md` §17.6).

Plenty of real employers hand-roll their careers page or sit on an ATS with no public API, so
their pages are read with a model (purpose `extract`). The most error-prone source by
construction, so also the most constrained:

- It runs only where no API exists, never as a shortcut past an adapter.
- Posting URLs are always real links taken from the page, never constructed, so a hallucinated
  job cannot produce a working link to apply to.
- Unknown fields stay null. Everything is capped: 5 listing pages, 50 postings, 30,000
  characters of listing text to the model and 20,000 of posting text stored.
- A page that yields nothing is flagged `needs_browser` rather than retried: it is almost always
  JavaScript-rendered.
- Page text reaches the model inside `<untrusted source="careers page">`: it is third-party
  data, never instructions (§7.2).

**The listing cache** (new). The crawl is a BFS: each listing page's extraction returns
`postings` and `more_listing_urls`, later pages come from the latter, and each posting's own page
is then fetched for its description. `companies.listing_cache[listing_url]` stores the whole
extraction of a page with `EXTRACTOR_VERSION` and the sha1 of the listing text as sent to the
model. A hit replays it as if the model had returned it (no call, no ledger row); a miss calls
the model and stores the result. Posting pages are always fetched (descriptions change while
listings do not), and `needs_browser` comes from the posting count as before, so a hit cannot
flip it. Entries for listing pages the crawl no longer reaches are dropped when the cache is
written back. Bump `EXTRACTOR_VERSION` whenever the instructions or the schema change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ada_url import URL, join_url
from pydantic import BaseModel

from apps.core.http import HTML_ACCEPT, FetchError, HttpClient
from apps.jobs.schemas import ExtractedPosting, ListingCache, ListingCacheEntry
from apps.jobs.sources.clean import clean_description, html_to_text_with_links
from apps.jobs.sources.dedupe import content_hash, dedupe_key, sha1_hex, web_job_id
from apps.jobs.sources.normalize import detect_remote, parse_salary, primary_location
from apps.jobs.sources.types import NormalizedJob
from apps.llm.structured import structured

MAX_LISTING_PAGES = 5  # listing pages read per company
MAX_POSTINGS = 50  # posting pages opened per company
MAX_LISTING_CHARS = 30_000  # listing text sent to the model
MAX_POSTING_CHARS = 20_000  # posting text stored as the description
FETCH_TIMEOUT_S = 15
FETCH_RETRIES = 1

# Changes whenever LISTING_INSTRUCTIONS, listing_prompt() or ExtractedListing changes, so cached
# extractions made under the old ones are re-extracted.
EXTRACTOR_VERSION = 1

LISTING_INSTRUCTIONS = """You read a company's careers page and list the job postings on it.

Rules:
- Only postings actually present on this page. Never invent a job, and never
  extrapolate from "we're hiring across engineering" into specific roles.
- Copy each posting's link exactly as it appears. If a posting has no link on
  this page, set url to null \u2014 do not construct one.
- Copy titles and locations verbatim. Leave location null if the page does not
  say.
- Ignore navigation, benefits, culture copy, and "life at" sections. Only
  actual open roles.
- more_listing_urls is for links to further pages of the SAME job list \u2014
  pagination, or per-team indexes. Not individual postings.
- If the page shows no job postings at all, return empty arrays. That is a
  valid answer and much better than guessing."""


class ExtractedListing(BaseModel):
    # Every field required, absence expressed as null or []: OpenAI's strict structured output
    # rejects optional properties outright.
    postings: list[ExtractedPosting]
    more_listing_urls: list[str]


def listing_prompt(company_name: str, url: str, text: str) -> str:
    """The per-page half of the extraction prompt; `text` is already capped."""
    return (
        f"Careers page for {company_name} at {url}.\n\n"
        f'<untrusted source="careers page">\n{text}\n</untrusted>'
    )


async def extract_listing(company_name: str, url: str, text: str) -> ExtractedListing:
    return await structured(
        "extract",
        ExtractedListing,
        instructions=LISTING_INSTRUCTIONS,
        prompt=listing_prompt(company_name, url, text),
    )


@dataclass(slots=True)
class _Page:
    url: str
    html: str


@dataclass(slots=True)
class CareersPageResult:
    jobs: list[NormalizedJob]
    # True when pages were fetched but no postings could be read from them.
    needs_browser: bool
    pages_fetched: int
    postings_fetched: int
    # The cache entries of the listing pages this crawl reached, to be written back.
    listing_cache: ListingCache = field(default_factory=dict)
    model_calls: int = 0


async def _fetch_page(client: HttpClient, url: str) -> _Page | None:
    try:
        html = await client.fetch_text(
            url, accept=HTML_ACCEPT, retries=FETCH_RETRIES, timeout=FETCH_TIMEOUT_S
        )
    except FetchError:
        return None
    return _Page(url, html)


def resolve_url(candidate: str, base: str) -> str | None:
    """Absolute, http(s) only: a model may echo a relative path."""
    try:
        resolved = join_url(base, candidate)
        if URL(resolved).protocol not in ("https:", "http:"):
            return None
    except ValueError:
        return None
    return resolved


async def fetch_careers_page(
    client: HttpClient,
    *,
    name: str,
    slug: str,
    careers_urls: list[str],
    cache: ListingCache | None = None,
    reached: ListingCache | None = None,
    max_postings: int = MAX_POSTINGS,
) -> CareersPageResult:
    """Reads a company's careers pages (`name`, `slug`, `careers_urls`) into normalised
    postings.

    `cache` is the company's stored listing cache. `reached` (filled in place, and returned as
    `listing_cache`) collects the entries of every listing page this crawl reached, so a caller
    whose crawl fails half-way still keeps the extractions it paid for.
    """
    cache = cache or {}
    reached = {} if reached is None else reached
    seen_listings: set[str] = set()
    queue = list(careers_urls)
    postings: dict[str, ExtractedPosting] = {}
    pages_fetched = 0
    model_calls = 0

    while queue and pages_fetched < MAX_LISTING_PAGES:
        url = queue.pop(0)
        if url in seen_listings:
            continue
        seen_listings.add(url)
        if url in cache:
            reached[url] = cache[url]  # still reached, even if this fetch fails

        page = await _fetch_page(client, url)
        if page is None:
            continue
        pages_fetched += 1

        # Links are preserved: the extractor must copy real posting URLs.
        sent = html_to_text_with_links(page.html, url)[:MAX_LISTING_CHARS]
        digest = sha1_hex(sent)
        entry = cache.get(url)
        if entry is None or entry.v != EXTRACTOR_VERSION or entry.sha1 != digest:
            listing = await extract_listing(name, url, sent)
            model_calls += 1
            entry = ListingCacheEntry(
                v=EXTRACTOR_VERSION,
                sha1=digest,
                postings=listing.postings,
                more_listing_urls=listing.more_listing_urls,
            )
        reached[url] = entry

        for posting in entry.postings:
            resolved = resolve_url(posting.url, url) if posting.url else None
            # A posting with no link cannot be applied to or refetched, so it is not a job we
            # can honestly offer.
            if not resolved or resolved in postings:
                continue
            if len(postings) >= max_postings:
                break
            postings[resolved] = posting

        for following in entry.more_listing_urls:
            resolved = resolve_url(following, url)
            if resolved and resolved not in seen_listings:
                queue.append(resolved)

    jobs: list[NormalizedJob] = []
    postings_fetched = 0
    for url, posting in postings.items():
        jid = web_job_id(slug, url)
        page = await _fetch_page(client, url)
        if page is not None:
            postings_fetched += 1

        cleaned = clean_description(page.html if page else None)
        text = cleaned.text[:MAX_POSTING_CHARS]
        locations = [posting.location] if posting.location else []
        location = primary_location(locations)
        salary = parse_salary(text)
        listed: dict[str, Any] = {
            "title": posting.title,
            "location": posting.location,
            "team": posting.team,
        }

        jobs.append(
            NormalizedJob(
                id=jid,
                source="web",
                external_id=jid.split(":")[-1],
                company_slug=slug,
                company_name=name,
                title=posting.title,
                locations=locations,
                location_norm=location,
                remote=detect_remote(
                    location=posting.location, title=posting.title, description=text
                ),
                employment_type=None,
                salary=salary,
                # The page text is the rawest form kept (design §14 #72): storing whole HTML
                # pages would bloat the table for no reader.
                description_raw=text or None,
                description_text=text or None,
                url=url,
                apply_url=None,
                # Careers pages rarely date their postings, and a guessed date would wrongly
                # age a job out of the search window.
                posted_at=None,
                raw={"source": "careers-page", "team": posting.team, "listing": listed},
                content_hash=content_hash(posting.title, locations, text, salary.min, salary.max),
                dedupe_key=dedupe_key(
                    name,
                    posting.title,
                    (location.city or location.raw) if location else None,
                ),
            )
        )

    return CareersPageResult(
        jobs=jobs,
        # Pages loaded but nothing readable in them is the signature of a JavaScript-rendered
        # board.
        needs_browser=pages_fetched > 0 and not postings,
        pages_fetched=pages_fetched,
        postings_fetched=postings_fetched,
        listing_cache=reached,
        model_calls=model_calls,
    )
