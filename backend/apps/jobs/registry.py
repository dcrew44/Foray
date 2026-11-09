"""The shared company registry (design §10.5; `map/worker.md` §18.1, §22.23).

Unlike per-user data, `companies` is part of the shared mirror: one person adding Stripe means
everyone benefits from the fetch, and `added_by` records who asked, not who owns it. Every
function here is sync; async callers use `db()`.

**Identity** (fixes the slug hijack of `map/worker.md` §22.23):
- A board company is identified only by `(source_type, ats_slug)`, through one race-free
  `INSERT ... ON CONFLICT (source_type, ats_slug) WHERE ats_slug IS NOT NULL DO UPDATE`.
- A careers-page company matches by domain, then by slug, among careers-page companies.
- A new company's slug is `company_slug()` of the desired one, with `-2`, `-3` ... when that is
  taken by another company. Existing slugs (imported or not) never change.

**Updates** keep what is known: `tags` and `careers_urls` are unioned (an empty list no longer
wipes the URLs), a missing domain keeps the old one, and status changes only when the caller
passes one. Seeding never does; verified discovery passes `active`, which is the only path that
re-activates an `invalid` company.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from django.db import IntegrityError, connection, transaction
from django.db.models import Count, F, Q
from django.db.models.functions import Least, Now
from django.utils import timezone

from apps.core.ids import new_id
from apps.jobs.models import Company, CompanyStatus, FetchStatus, SourceType

LIST_LIMIT = 500
STALE_LIMIT = 200

# How often a board is asked whether it changed (#55). A board that listed an early-career role
# or gave someone a match in the last `RELEVANT_FOR` is asked every tick of the cron; the rest
# hourly; a careers page (which can cost a model call) as often as before. A tick that arrives a
# few seconds early still counts (`DUE_SLACK`).
HOT_INTERVAL = timedelta(minutes=15)
COLD_INTERVAL = timedelta(hours=1)
CAREERS_PAGE_INTERVAL = timedelta(hours=6)
RELEVANT_FOR = timedelta(days=180)
DUE_SLACK = timedelta(seconds=90)
# A failing board waits twice as long after each failure, up to this.
MAX_BACKOFF = timedelta(hours=6)
MAX_FAILURES = 15
DUE_LIMIT = 5000
# `mark_relevant` rewrites a company's mark at most this often.
RELEVANT_TOUCH_EVERY = timedelta(hours=12)
_SLUG_RUN = re.compile(r"[^a-z0-9]+")
_SLUG_ATTEMPTS = 5


def company_slug(raw: str) -> str:
    """Our slug for a new company: lowercased, every run outside `[a-z0-9]` one `-`, no `-` at
    either end, and `company` when nothing is left. The ATS slug itself is stored unchanged in
    `ats_slug`; this one is also the middle of every job id, so it must fit
    `companies_slug_shape`."""
    return _SLUG_RUN.sub("-", raw.lower()).strip("-") or "company"


@dataclass(slots=True)
class CompanyInput:
    name: str
    slug: str
    source_type: str
    ats_slug: str | None = None
    domain: str | None = None
    careers_urls: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    # None keeps an existing company's status (and makes a new one active).
    status: str | None = None
    # None keeps an existing company's flag (and makes a new one False).
    needs_browser: bool | None = None
    discovered_via: str = "agent"
    added_by_id: str | None = None
    # Where the board was learned from (`companies.found_in`); `discovered_via` when not given.
    found_in: list[str] = field(default_factory=list)


def _union(existing: Iterable[str], extra: Iterable[str]) -> list[str]:
    return list(dict.fromkeys([*existing, *extra]))


def _free_slug(desired: str) -> str:
    taken = set(
        Company.objects.filter(Q(slug=desired) | Q(slug__startswith=f"{desired}-")).values_list(
            "slug", flat=True
        )
    )
    if desired not in taken:
        return desired
    n = 2
    while f"{desired}-{n}" in taken:
        n += 1
    return f"{desired}-{n}"


def _is_slug_conflict(error: IntegrityError) -> bool:
    cause = error.__cause__
    diag = getattr(cause, "diag", None)
    return getattr(diag, "constraint_name", None) == "companies_slug_key"


def upsert_company(data: CompanyInput) -> tuple[Company, bool]:
    """Registers a company or updates the one already holding that board (or careers page).
    Returns `(company, created)`."""
    desired = company_slug(data.slug)
    careers_urls = _union([], data.careers_urls)
    tags = _union([], data.tags)
    found_in = _union([], data.found_in or [str(data.discovered_via)])
    if data.source_type == SourceType.CAREERS_PAGE:
        return _upsert_careers_page(data, desired, careers_urls, tags, found_in)
    if not data.ats_slug:
        raise ValueError(f"A {data.source_type} company needs its board slug")

    for _attempt in range(_SLUG_ATTEMPTS):
        slug = _free_slug(desired)
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO companies (
                        id, name, domain, slug, source_type, ats_slug, careers_urls, tags, status,
                        needs_browser, listing_cache, poll_state, discovered_via, added_by_id,
                        found_in, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::text[], %s::text[], %s, %s, '{}'::jsonb,
                            '{}'::jsonb, %s, %s, %s::text[], now(), now())
                    ON CONFLICT (source_type, ats_slug) WHERE ats_slug IS NOT NULL DO UPDATE SET
                        name = EXCLUDED.name,
                        domain = coalesce(EXCLUDED.domain, companies.domain),
                        careers_urls = companies.careers_urls || ARRAY(
                            SELECT u FROM unnest(EXCLUDED.careers_urls) WITH ORDINALITY AS t(u, n)
                            WHERE NOT (u = ANY (companies.careers_urls)) ORDER BY n),
                        tags = companies.tags || ARRAY(
                            SELECT u FROM unnest(EXCLUDED.tags) WITH ORDINALITY AS t(u, n)
                            WHERE NOT (u = ANY (companies.tags)) ORDER BY n),
                        found_in = companies.found_in || ARRAY(
                            SELECT u FROM unnest(EXCLUDED.found_in) WITH ORDINALITY AS t(u, n)
                            WHERE NOT (u = ANY (companies.found_in)) ORDER BY n),
                        status = CASE WHEN %s THEN EXCLUDED.status ELSE companies.status END,
                        needs_browser = CASE WHEN %s THEN EXCLUDED.needs_browser
                                             ELSE companies.needs_browser END,
                        updated_at = now()
                    RETURNING id, (xmax = 0) AS inserted
                    """,
                    [
                        new_id(),
                        data.name,
                        data.domain,
                        slug,
                        str(data.source_type),
                        data.ats_slug,
                        careers_urls,
                        tags,
                        str(data.status or CompanyStatus.ACTIVE),
                        bool(data.needs_browser),
                        str(data.discovered_via),
                        data.added_by_id,
                        found_in,
                        data.status is not None,
                        data.needs_browser is not None,
                    ],
                )
                row = cursor.fetchone()
        except IntegrityError as error:
            if _is_slug_conflict(error):
                continue  # another company took that slug between the check and the insert
            raise
        assert row is not None
        return Company.objects.get(id=row[0]), bool(row[1])
    raise RuntimeError(f"Could not find a free slug for {desired!r}")


def _upsert_careers_page(
    data: CompanyInput, desired: str, careers_urls: list[str], tags: list[str], found_in: list[str]
) -> tuple[Company, bool]:
    for _attempt in range(_SLUG_ATTEMPTS):
        try:
            with transaction.atomic():
                pages = Company.objects.select_for_update().filter(
                    source_type=SourceType.CAREERS_PAGE
                )
                existing = (
                    pages.filter(domain=data.domain).order_by("created_at").first()
                    if data.domain
                    else None
                ) or pages.filter(slug=desired).first()

                if existing is not None:
                    existing.name = data.name
                    existing.domain = data.domain if data.domain is not None else existing.domain
                    existing.careers_urls = _union(existing.careers_urls, careers_urls)
                    existing.tags = _union(existing.tags, tags)
                    existing.found_in = _union(existing.found_in, found_in)
                    if data.status is not None:
                        existing.status = data.status
                    if data.needs_browser is not None:
                        existing.needs_browser = data.needs_browser
                    existing.save()
                    return existing, False

                company = Company.objects.create(
                    name=data.name,
                    domain=data.domain,
                    slug=_free_slug(desired),
                    source_type=SourceType.CAREERS_PAGE,
                    ats_slug=None,
                    careers_urls=careers_urls,
                    tags=tags,
                    status=data.status or CompanyStatus.ACTIVE,
                    needs_browser=bool(data.needs_browser),
                    discovered_via=data.discovered_via,
                    added_by_id=data.added_by_id,
                    found_in=found_in,
                )
                return company, True
        except IntegrityError as error:
            if _is_slug_conflict(error):
                continue
            raise
    raise RuntimeError(f"Could not find a free slug for {desired!r}")


# --- reads ---------------------------------------------------------------------------------------


def get_company_by_slug(slug: str) -> Company | None:
    return Company.objects.filter(slug=slug).first()


def get_company_by_board(source_type: str, ats_slug: str) -> Company | None:
    return Company.objects.filter(source_type=source_type, ats_slug=ats_slug).first()


def list_companies(status: str | None = None, limit: int = LIST_LIMIT) -> list[Company]:
    rows = Company.objects.all() if status is None else Company.objects.filter(status=status)
    return list(rows.order_by("name")[:limit])


def stale_companies(stale_before: datetime, limit: int = STALE_LIMIT) -> list[Company]:
    """Active companies not fetched since `stale_before`, never-fetched first."""
    rows = Company.objects.filter(status=CompanyStatus.ACTIVE).filter(
        Q(last_fetched_at__isnull=True) | Q(last_fetched_at__lt=stale_before)
    )
    return list(rows.order_by(F("last_fetched_at").asc(nulls_first=True))[:limit])


def poll_interval(company: Company, now: datetime) -> timedelta:
    """How long after its last attempt this company is due again."""
    if company.source_type == SourceType.CAREERS_PAGE:
        base = CAREERS_PAGE_INTERVAL
    elif company.last_relevant_at is not None and now - company.last_relevant_at < RELEVANT_FOR:
        base = HOT_INTERVAL
    elif isinstance(company.poll_state, dict) and company.poll_state.get("pending") is True:
        # A listed board still being read (#57) goes on at the next tick, whatever it lists.
        base = HOT_INTERVAL
    else:
        base = COLD_INTERVAL
    failures = min(int(company.fetch_failures or 0), MAX_FAILURES)
    return max(base, min(base * 2**failures, MAX_BACKOFF)) if failures else base


def due_companies(now: datetime, limit: int = DUE_LIMIT) -> list[Company]:
    """Active companies whose next check has come, never-fetched first, then the longest
    waiting. What the scheduled refresh reads every fifteen minutes."""
    # Nothing is due sooner than the hot interval, so the database narrows first.
    soonest = now - (HOT_INTERVAL - DUE_SLACK)
    rows = Company.objects.filter(status=CompanyStatus.ACTIVE).filter(
        Q(last_fetched_at__isnull=True) | Q(last_fetched_at__lte=soonest)
    )
    due = [
        company
        for company in rows.order_by(F("last_fetched_at").asc(nulls_first=True))
        if company.last_fetched_at is None
        or now - company.last_fetched_at >= poll_interval(company, now) - DUE_SLACK
    ]
    return due[:limit]


def count_by_status() -> dict[str, int]:
    rows = Company.objects.values("status").annotate(count=Count("id")).order_by("status")
    return {row["status"]: row["count"] for row in rows}


def known_slugs(slugs: Sequence[str]) -> set[str]:
    if not slugs:
        return set()
    return set(Company.objects.filter(slug__in=slugs).values_list("slug", flat=True))


# --- writes --------------------------------------------------------------------------------------


def record_fetch(
    company_id: uuid.UUID,
    status: str,
    job_count: int | None,
    *,
    mark_invalid: bool = False,
    poll_state: dict[str, Any] | None = None,
    relevant: bool = False,
) -> None:
    """Writes `last_fetched_at` and `last_fetch_status`; `job_count=None` keeps the previous
    count (an errored fetch says nothing about how many postings there are, and the count is
    what the empty-board guard reads).

    A check that succeeded (`ok`, `needs-browser`) also moves `last_ok_at` and clears the
    failure count; one that failed adds to it, which is what backs a broken board off. `ok`
    with a `poll_state` remembers it for the next poll, and a board that is gone forgets its
    own. `relevant` marks the board as one worth polling often (`last_relevant_at`)."""
    fields: dict[str, object] = {
        "last_fetched_at": timezone.now(),
        "last_fetch_status": status,
        "updated_at": Now(),
    }
    if job_count is not None:
        fields["last_fetch_job_count"] = job_count
    if status in (FetchStatus.OK, FetchStatus.NEEDS_BROWSER):
        fields["last_ok_at"] = Now()
        fields["fetch_failures"] = 0
        if poll_state is not None:
            fields["poll_state"] = poll_state
        if relevant:
            fields["last_relevant_at"] = Now()
    else:
        fields["fetch_failures"] = Least(F("fetch_failures") + 1, MAX_FAILURES)
    if mark_invalid:
        fields["status"] = CompanyStatus.INVALID
        fields["poll_state"] = {}
    Company.objects.filter(id=company_id).update(**fields)


def mark_relevant(job_ids: Sequence[str]) -> int:
    """The companies behind these postings just gave someone a match: keep their boards on the
    fast cadence. Rewrites a mark only when it is half a day old, so a busy watch does not
    update the same rows every pass. Returns how many companies were marked."""
    if not job_ids:
        return 0
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE companies SET last_relevant_at = now() "
            "WHERE id IN (SELECT company_id FROM jobs WHERE id = ANY(%s) "
            "AND company_id IS NOT NULL) "
            "AND (last_relevant_at IS NULL "
            "OR last_relevant_at < now() - make_interval(secs => %s))",
            [list(job_ids), RELEVANT_TOUCH_EVERY.total_seconds()],
        )
        return cursor.rowcount


def set_company_status(company_id: uuid.UUID, status: str) -> Company | None:
    updated = Company.objects.filter(id=company_id).update(status=status, updated_at=Now())
    return Company.objects.filter(id=company_id).first() if updated else None


# --- names ---------------------------------------------------------------------------------------

_NAME_KEY = re.compile(r"[^a-z0-9]+")
_SCHEME = re.compile(r"^https?://")
_WWW = re.compile(r"^www\.")


@dataclass(slots=True)
class FoundCompanies:
    found: list[tuple[str, Company]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def find_companies_by_names(names: Sequence[str]) -> FoundCompanies:
    """Registered active companies for names a user typed ("Anthropic", "stripe.com"): the name
    ignoring case and punctuation, our slug, the board slug, or the domain. A board wins over a
    careers page. A name with no letters or digits is reported missing (it used to vanish)."""
    result = FoundCompanies()
    for name in names:
        key = _NAME_KEY.sub("", name.lower())
        domain = _WWW.sub("", _SCHEME.sub("", name.strip().lower())).split("/")[0]
        if not key:
            result.missing.append(name)
            continue
        rows = list(
            Company.objects.raw(
                """
                SELECT * FROM companies
                WHERE status = 'active'
                  AND (regexp_replace(lower(name), '[^a-z0-9]+', '', 'g') = %s
                       OR regexp_replace(slug, '[^a-z0-9]+', '', 'g') = %s
                       OR regexp_replace(lower(coalesce(ats_slug, '')), '[^a-z0-9]+', '', 'g') = %s
                       OR (%s AND domain = %s))
                ORDER BY
                    CASE WHEN source_type <> 'careers_page' THEN 0 ELSE 1 END,
                    created_at
                LIMIT 1
                """,
                [key, key, key, "." in domain, domain],
            )
        )
        if rows:
            result.found.append((name, rows[0]))
        else:
            result.missing.append(name)
    return result
