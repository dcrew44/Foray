"""Turning "Stripe" into a registered, verified job board (design §10.5; `map/worker.md` §18.3).

The governing rule: a company is registered only after a live API call returns postings.
Everything below produces *candidates*; verification is the only thing that produces a
registration. The order is cheapest-and-most-certain first, and every step lands in `trail`, so
a miss is explicable:

    pasted ATS link -> careers paths -> homepage -> slug guesses -> Brave search
    -> careers-page fallback

A web search answers loosely, so a search result is used only when it is about the company: its
address holds the name's slug, or its page names the company. Otherwise a made-up name would be
registered with strangers' careers pages, or with a board their page links to.

Every fetch goes through the guarded client (§10.1): a URL a user typed that points inside the
network is refused, and the refusal is recorded in the trail as "That address is not reachable
from Foray."
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from ada_url import URL, join_url
from django.conf import settings

from apps.core.db import db
from apps.core.http import BLOCKED_MESSAGE, HTML_ACCEPT, FetchError, HttpClient, HttpError
from apps.jobs.models import Company, DiscoveredVia, SourceType
from apps.jobs.registry import (
    CompanyInput,
    company_slug,
    get_company_by_board,
    upsert_company,
)
from apps.jobs.sources.js import IA, JS_WS, js_trim
from apps.jobs.sources.jsearch import BOARD_LINK_PATTERNS, DiscoveredBoard
from apps.jobs.sources.types import BoardSource
from apps.jobs.verify import VerifyOutcome, find_board, slug_candidates, verify_board

log = logging.getLogger("foray.jobs")

# ATS board URLs, and which adapter handles each.
ATS_PATTERNS: list[tuple[BoardSource, re.Pattern[str]]] = [
    (
        "greenhouse",
        re.compile(
            r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)", IA
        ),
    ),
    ("lever", re.compile(r"jobs\.lever\.co/([a-z0-9_-]+)", IA)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_-]+)", IA)),
    *BOARD_LINK_PATTERNS,
]

# ATS platforms we recognise but cannot read through an API. These become careers-page
# companies for the extractor rather than being discarded: a Workday board is still a board.
UNSUPPORTED_ATS = re.compile(
    r"(?:myworkdayjobs\.com|icims\.com|taleo\.net|successfactors\.com|smartrecruiters\.com"
    r"|breezy\.hr|jazzhr\.com|bamboohr\.com|jobvite\.com"
    r"|teamtailor\.com|paylocity\.com|dover\.com|rippling(?:ats)?\.com"
    r"|pinpointhq\.com|applytojob\.com)",
    IA,
)

# Where careers pages usually live.
CAREERS_PATHS = ("/careers", "/jobs", "/careers/jobs", "/company/careers")

# Anchors on a homepage that lead to a careers page.
CAREERS_LINK = re.compile(
    rf"href[{JS_WS}]*=[{JS_WS}]*[\"']([^\"']*(?:career|jobs|join-us|work-with-us)[^\"']*)[\"']",
    IA,
)
CAREERS_LINKS_MAX = 5

FETCH_TIMEOUT_S = 12
FETCH_RETRIES = 1
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
BRAVE_TIMEOUT_S = 12
SEARCH_RESULTS_TRIED = 3

_LOOKS_LIKE_URL = re.compile(r"^(?:https?://|www\.)|\.[a-z]{2,}(?:/|\Z)", IA)
_WWW = re.compile(r"^www\.")
_WHITESPACE = re.compile(f"[{JS_WS}]+")
_SLUG_SEPARATORS = re.compile(r"[-_]+")


@dataclass(frozen=True, slots=True)
class AtsLink:
    source: BoardSource
    slug: str


def find_ats_links(html: str) -> list[AtsLink]:
    """ATS board links in a page's HTML, in pattern order, de-duplicated."""
    found: list[AtsLink] = []
    for source, pattern in ATS_PATTERNS:
        for match in pattern.finditer(html):
            slug = match.group(1).lower()
            # Greenhouse embeds sometimes link to their own docs.
            if not slug or slug in ("embed", "job_board"):
                continue
            link = AtsLink(source, slug)
            if link not in found:
                found.append(link)
    return found


def has_unsupported_ats(html: str) -> bool:
    return UNSUPPORTED_ATS.search(html) is not None


def normalize_domain(value: str) -> str | None:
    """The bare host: no scheme, no `www.`, no path (WHATWG parsing, as `new URL`)."""
    trimmed = js_trim(value).lower()
    if not trimmed:
        return None
    try:
        host = URL(trimmed if "://" in trimmed else f"https://{trimmed}").hostname
    except ValueError:
        return None
    return _WWW.sub("", host, count=1) or None


def find_careers_links(html: str, base_url: str) -> list[str]:
    """Careers links on a homepage, absolutised against it: plenty of companies put careers
    under `/about/careers` or a subdomain, and always link to it from the homepage."""
    links: dict[str, None] = {}
    for match in CAREERS_LINK.finditer(html):
        try:
            resolved = join_url(base_url, match.group(1))
            if URL(resolved).protocol not in ("https:", "http:"):
                continue
        except ValueError:
            continue  # a malformed href is not worth failing discovery over
        links[resolved] = None
    return list(links)[:CAREERS_LINKS_MAX]


def title_case(value: str) -> str:
    return " ".join(word[:1].upper() + word[1:] for word in _WHITESPACE.split(value))


def _registered_name(source: str, ats_slug: str | None, ours: str) -> str | None:
    """The name of the company a registration would update (`upsert_company`'s identity: the
    board, or a careers page by its slug), if one exists."""
    if ats_slug is not None:
        company = get_company_by_board(source, ats_slug)
    else:
        company = Company.objects.filter(
            source_type=SourceType.CAREERS_PAGE, slug=company_slug(ours)
        ).first()
    return company.name if company is not None else None


@dataclass(slots=True)
class DiscoveryResult:
    name: str
    # None when nothing could be verified: the caller decides what to do.
    registered: Company | None
    source: str | None
    slug: str | None
    careers_urls: list[str]
    needs_browser: bool
    # What was tried, in order, as `{step, detail}`; shown to the user.
    trail: list[dict[str, str]]
    # True when this call created the company (False when it updated an existing one).
    created: bool = False


@dataclass(slots=True)
class _Discovery:
    client: HttpClient
    trail: list[dict[str, str]] = field(default_factory=list)
    blocked: bool = False

    def step(self, step: str, detail: str) -> None:
        self.trail.append({"step": step, "detail": detail})

    async def fetch(self, url: str) -> str | None:
        """A page's HTML, or None. A 404 on a guessed path is expected, not worth reporting; a
        blocked address is recorded once."""
        try:
            return await self.client.fetch_text(
                url, accept=HTML_ACCEPT, retries=FETCH_RETRIES, timeout=FETCH_TIMEOUT_S
            )
        except HttpError as error:
            if error.blocked and not self.blocked:
                self.blocked = True
                self.step("blocked", BLOCKED_MESSAGE)
            return None
        except FetchError:
            return None

    async def usable(self, link: AtsLink) -> VerifyOutcome | None:
        outcome = await verify_board(self.client, link.source, link.slug)
        return outcome if outcome.status in ("ok", "empty") else None


async def search_for_careers_page(client: HttpClient, name: str) -> list[str]:
    """Brave web search for a careers page; only with `SEARCH_API_KEY`, and the only step here
    that costs anything. Any failure is no results."""
    key = settings.ENV.search_api_key
    if key is None:
        return []
    url = f"{BRAVE_URL}?{urlencode({'q': f'{name} careers jobs', 'count': '5'})}"
    try:
        body: Any = await client.fetch_json(
            url,
            timeout=BRAVE_TIMEOUT_S,
            retries=0,
            headers={"X-Subscription-Token": key.get_secret_value(), "Accept": "application/json"},
        )
    except FetchError, ValueError:
        return []
    results = ((body or {}).get("web") or {}).get("results") or [] if isinstance(body, dict) else []
    return [result["url"] for result in results if isinstance(result, dict) and result.get("url")]


def is_about(name: str, url: str, page: str) -> bool:
    """Whether a web search result is about the company `name`: its address holds one of the
    name's first slug guesses (the joined name, or a guess of four letters or more), or its page
    names the company, case and spacing aside."""
    candidates = slug_candidates(name)[:3]
    # The joined name always; a shorter first word ("the", "box") would match too much.
    keys = [slug for index, slug in enumerate(candidates) if index == 0 or len(slug) >= 4]
    address = url.lower()
    if any(key in address for key in keys):
        return True
    wanted = " ".join(name.lower().split())
    return bool(wanted) and wanted in " ".join(html.unescape(page).lower().split())


async def discover_company(
    client: HttpClient,
    name_or_url: str,
    *,
    added_by_id: str | None = None,
    discovered_via: str = DiscoveredVia.AGENT,
    register: bool = True,
    tags: Sequence[str] = (),
) -> DiscoveryResult:
    """Resolves a company name or URL to a registered, verified company. `register=False` runs
    the whole pipeline without writing."""
    run = _Discovery(client)
    raw = js_trim(name_or_url)
    domain = normalize_domain(raw) if _LOOKS_LIKE_URL.search(raw) else None
    name = raw
    # True once `name` is only a guess (the slug of a pasted board link): it is shown title-cased
    # with its separators as spaces, and never replaces the name a company already has.
    guessed = False
    careers_urls: list[str] = []

    async def finish(source: str, slug: str | None) -> DiscoveryResult:
        ours = slug
        if ours is None:
            normalized = normalize_domain(domain if domain is not None else name)
            ours = normalized.split(".")[0] if normalized is not None else name
        result = DiscoveryResult(
            name=name,
            registered=None,
            source=source,
            slug=slug,
            careers_urls=careers_urls,
            # A careers-page company may need a browser; refresh decides for certain.
            needs_browser=source == SourceType.CAREERS_PAGE,
            trail=run.trail,
        )
        if not register:
            return result
        display = title_case(_SLUG_SEPARATORS.sub(" ", name) if guessed else name)
        if guessed:
            display = await db(_registered_name, source, slug, ours) or display
        result.registered, result.created = await db(
            upsert_company,
            CompanyInput(
                name=display,
                slug=ours,
                source_type=source,
                ats_slug=slug,
                domain=domain,
                careers_urls=careers_urls,
                tags=list(tags),
                status="active",
                needs_browser=source == SourceType.CAREERS_PAGE,
                discovered_via=discovered_via,
                added_by_id=added_by_id,
            ),
        )
        return result

    # A board URL pasted directly is the strongest possible signal. It names a board, not the
    # company: its host is the ATS's (never the company's domain, nor a site to probe for careers
    # pages) and the URL is no name, so the board's slug stands in for the name.
    direct = find_ats_links(raw)
    if direct:
        link = direct[0]
        run.step("ats-link", f"pasted {link.source}:{link.slug}")
        domain, name, guessed = None, link.slug, True
        if await run.usable(link):
            return await finish(link.source, link.slug)

    if domain:
        name = domain.split(".")[0]
        run.step("domain", domain)

    # 1. The careers page, which usually links straight at the real board.
    for path in CAREERS_PATHS if domain else ():
        url = f"https://{domain}{path}"
        page = await run.fetch(url)
        if page is None:
            continue
        careers_urls.append(url)
        links = find_ats_links(page)
        described = ", ".join(f"{link.source}:{link.slug}" for link in links) or "no ATS link"
        run.step("careers-page", f"{url} → {described}")
        for link in links:
            if await run.usable(link):
                return await finish(link.source, link.slug)
        if has_unsupported_ats(page):
            run.step(
                "unsupported-ats", "Workday/iCIMS-style board \u2014 registering as a careers page"
            )
            return await finish(SourceType.CAREERS_PAGE, None)
        break  # only the first page that loads is examined

    # 1b. No careers page at a guessed path? Follow the homepage's own link.
    if domain and not careers_urls:
        home = await run.fetch(f"https://{domain}")
        if home is not None:
            linked = find_careers_links(home, f"https://{domain}")
            run.step("homepage", f"{len(linked)} careers links")
            for url in linked:
                page = await run.fetch(url)
                if page is None:
                    continue
                careers_urls.append(url)
                for link in find_ats_links(page):
                    if await run.usable(link):
                        run.step("homepage-ats-link", f"{url} → {link.source}:{link.slug}")
                        return await finish(link.source, link.slug)
                if has_unsupported_ats(page):
                    run.step("unsupported-ats", url)
                    return await finish(SourceType.CAREERS_PAGE, None)
                break

    # 2. Slug guesses: free, and right more often than not for tech companies.
    candidates = slug_candidates(name)
    for candidate in candidates:
        outcome = await find_board(client, candidate)
        if outcome is not None and outcome.status == "ok":
            run.step("slug-guess", f"{outcome.source}:{candidate}")
            return await finish(outcome.source, candidate)
    run.step("slug-guess", f"no board for {', '.join(candidates[:3])}")

    # 3. Search, to find a careers page we could not guess.
    if not domain:
        results = await search_for_careers_page(client, name)
        run.step("search", f"{len(results)} results")
        for url in results[:SEARCH_RESULTS_TRIED]:
            page = await run.fetch(url)
            if page is None:
                continue
            if not is_about(name, url, page):
                run.step("search-skip", url)
                continue
            careers_urls.append(url)
            for link in find_ats_links(page):
                if await run.usable(link):
                    run.step("search-ats-link", f"{url} → {link.source}:{link.slug}")
                    domain = domain or normalize_domain(url)
                    return await finish(link.source, link.slug)
            if has_unsupported_ats(page):
                domain = domain or normalize_domain(url)
                run.step("unsupported-ats", url)
                return await finish(SourceType.CAREERS_PAGE, None)

    # A careers page whose ATS we could not identify is still worth registering: the page is
    # readable, which is exactly what the careers-page extractor is for.
    if careers_urls:
        run.step(
            "careers-page-fallback",
            "no recognised ATS, registering the careers page for extraction",
        )
        return await finish(SourceType.CAREERS_PAGE, None)

    return DiscoveryResult(
        name=name,
        registered=None,
        source=None,
        slug=None,
        careers_urls=careers_urls,
        needs_browser=False,
        trail=run.trail,
    )


async def register_boards_from_apply_links(
    client: HttpClient, boards: Sequence[DiscoveredBoard], *, added_by_id: str | None = None
) -> list[Company]:
    """Registers boards found in an aggregator's apply links. The aggregator costs a request
    per page and truncates descriptions; the company's own board is free and complete. Each is
    still verified (`ok` only) before being written down."""
    registered: list[Company] = []
    for board in boards:
        if await db(get_company_by_board, board.source, board.slug) is not None:
            continue
        outcome = await verify_board(client, board.source, board.slug)
        if outcome.status != "ok":
            continue
        company, _created = await db(
            upsert_company,
            CompanyInput(
                name=board.company_name or board.slug,
                slug=board.slug,
                source_type=board.source,
                ats_slug=board.slug,
                status="active",
                discovered_via=DiscoveredVia.JSEARCH,
                added_by_id=added_by_id,
            ),
        )
        registered.append(company)
    return registered
