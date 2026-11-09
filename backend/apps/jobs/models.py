"""The shared mirror: companies, their postings, and third-party quotas (design §2.2).

Not per-user: one fetch serves everyone, and a vanished posting is kept with
`is_active = false`. `companies.added_by` records who asked, not an owner.

JSON columns: `jobs.location_norm` is `jobs.schemas.NormalizedLocation`;
`companies.listing_cache` maps listing URLs to `jobs.schemas.ListingCacheEntry` (§10.5).
"""

from __future__ import annotations

import uuid

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Now

from apps.accounts.models import User
from apps.core.choices import choices_checks
from apps.core.ids import new_id
from apps.core.models import Timestamped


class SourceType(models.TextChoices):
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    WORKABLE = "workable"
    RECRUITEE = "recruitee"
    GEM = "gem"
    JIBE = "jibe"  # iCIMS career sites on the employer's own domain
    WORKDAY = "workday"
    SMARTRECRUITERS = "smartrecruiters"
    ICIMS = "icims"  # classic iCIMS portals, <name>.icims.com
    CAREERS_PAGE = "careers_page"


class CompanyStatus(models.TextChoices):
    ACTIVE = "active"
    PAUSED = "paused"
    INVALID = "invalid"


class FetchStatus(models.TextChoices):
    OK = "ok"
    NOT_FOUND = "not-found"
    ERROR = "error"
    NEEDS_BROWSER = "needs-browser"
    UNSUPPORTED = "unsupported"


class DiscoveredVia(models.TextChoices):
    SEED = "seed"
    AGENT = "agent"
    JSEARCH = "jsearch"
    USER = "user"


class JobSource(models.TextChoices):
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    WORKABLE = "workable"
    RECRUITEE = "recruitee"
    GEM = "gem"
    JIBE = "jibe"
    WORKDAY = "workday"
    SMARTRECRUITERS = "smartrecruiters"
    ICIMS = "icims"
    JSEARCH = "jsearch"
    WEB = "web"


class Remote(models.TextChoices):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


class Period(models.TextChoices):
    """A salary's period, as the source states it; never converted."""

    YEAR = "year"
    MONTH = "month"
    HOUR = "hour"


class JobLevel(models.TextChoices):
    """The level a posting is at (`jobs/facts.py`, #56). The profile's seniority plus `unknown`,
    which is the honest answer for most titles that carry no level word."""

    INTERN = "intern"
    NEW_GRAD = "new_grad"
    ENTRY = "entry"
    MID = "mid"
    SENIOR = "senior"
    STAFF_PLUS = "staff_plus"
    UNKNOWN = "unknown"


class WorkAuth(models.TextChoices):
    """What a posting says about who may work there, strictest first when it says several."""

    CITIZEN_ONLY = "citizen_only"  # US citizenship required
    US_PERSON = "us_person"  # citizen or permanent resident (ITAR, export control)
    NO_SPONSORSHIP = "no_sponsorship"  # will not sponsor a visa
    SPONSORS = "sponsors"  # says it sponsors
    UNKNOWN = "unknown"  # says nothing


class Clearance(models.TextChoices):
    REQUIRED = "required"  # one already held, or required without "able to obtain"
    ELIGIBLE = "eligible"  # must be able to obtain one
    NONE = "none"  # not mentioned


# The shape of every job id: `source:company_slug:external_id`, or `web:<slug>:<sha1(url)>`.
JOB_ID_PATTERN = (
    r"^(greenhouse|lever|ashby|workable|recruitee|gem|jibe|workday|smartrecruiters|icims|jsearch"
    r"|web):[^:]+:.+$"
)
COMPANY_SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]*$"


class Company(Timestamped):
    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    name = models.TextField()
    domain = models.TextField(null=True)  # noqa: DJ001 (a bare registrable domain, e.g. stripe.com)
    # New rows: `company_slug()` normalises to [a-z0-9-] (§10.5); imported slugs are kept as they
    # are. The slug is also the middle segment of every job id.
    slug = models.TextField(unique=True)
    source_type = models.TextField(choices=SourceType.choices)
    ats_slug = models.TextField(null=True)  # noqa: DJ001 (the board's id at the ATS, stored unchanged)
    careers_urls = ArrayField(models.TextField(), default=list)
    tags = ArrayField(models.TextField(), default=list)
    status = models.TextField(choices=CompanyStatus.choices, default=CompanyStatus.ACTIVE)
    last_fetched_at = models.DateTimeField(null=True)
    last_fetch_status = models.TextField(choices=FetchStatus.choices, null=True)  # noqa: DJ001
    # Also drives the empty-board guard (§10.4).
    last_fetch_job_count = models.IntegerField(null=True)
    needs_browser = models.BooleanField(default=False)
    # Careers pages: {listing_url: ListingCacheEntry} (§10.5).
    listing_cache = models.JSONField(default=dict)
    discovered_via = models.TextField(choices=DiscoveredVia.choices)
    added_by = models.ForeignKey(User, models.SET_NULL, null=True, related_name="+")
    # Polling (#55). `last_fetched_at` is the last attempt; `last_ok_at` the last check that
    # succeeded, whether it read the board or found it unchanged (the database's clock: a new
    # posting's `seen_lag_s` is measured from it).
    last_ok_at = models.DateTimeField(null=True)
    # Consecutive failed fetches. Each doubles the wait before the next (`registry.due_companies`).
    fetch_failures = models.SmallIntegerField(db_default=0)
    # What the last read left to ask "has this changed?" with (`sources.poll.PollState`).
    poll_state = models.JSONField(default=dict)
    # When this board last listed an early-career role or gave someone a match. With one in the
    # last 180 days it is polled every 15 minutes, otherwise hourly.
    last_relevant_at = models.DateTimeField(null=True)

    added_by_id: str | None

    class Meta:
        db_table = "companies"
        constraints = [
            # A board company is identified by its board, through one race-free upsert:
            # INSERT ... ON CONFLICT (source_type, ats_slug) WHERE ats_slug IS NOT NULL.
            models.UniqueConstraint(
                fields=["source_type", "ats_slug"],
                condition=Q(ats_slug__isnull=False),
                name="companies_board_unique",
            ),
            models.CheckConstraint(
                condition=Q(slug__regex=COMPANY_SLUG_PATTERN), name="companies_slug_shape"
            ),
            models.CheckConstraint(
                condition=Q(source_type=SourceType.CAREERS_PAGE, ats_slug__isnull=True)
                | (~Q(source_type=SourceType.CAREERS_PAGE) & Q(ats_slug__isnull=False)),
                name="companies_ats_slug_matches_source",
            ),
            *choices_checks(
                "companies",
                source_type=SourceType,
                status=CompanyStatus,
                last_fetch_status=FetchStatus,
                discovered_via=DiscoveredVia,
            ),
        ]
        indexes = [
            models.Index(fields=["status"], name="companies_status_idx"),
            models.Index(fields=["last_fetched_at"], name="companies_last_fetched_idx"),
        ]

    def __str__(self) -> str:
        return self.slug


class Job(models.Model):
    # source:company_slug:external_id; careers pages web:<slug>:<sha1(url)> (§2.5, §10.3).
    id = models.TextField(primary_key=True)
    source = models.TextField(choices=JobSource.choices)
    external_id = models.TextField()
    # Null for aggregator rows, which arrive before their company is registered.
    company = models.ForeignKey(Company, models.SET_NULL, null=True, related_name="jobs")
    company_name = models.TextField()
    title = models.TextField()
    locations = ArrayField(models.TextField(), default=list)
    location_norm = models.JSONField(null=True)  # NormalizedLocation
    remote = models.TextField(choices=Remote.choices, default=Remote.UNKNOWN)
    employment_type = models.TextField(null=True)  # noqa: DJ001 (free text: FullTime, Intern, ...)
    # float8: lossless for every live value (8 real hourly salaries have 3 decimals).
    salary_min = models.FloatField(null=True)
    salary_max = models.FloatField(null=True)
    salary_currency = models.TextField(null=True)  # noqa: DJ001
    salary_period = models.TextField(choices=Period.choices, null=True)  # noqa: DJ001
    description_raw = models.TextField(null=True)  # noqa: DJ001 (never rewritten: invariant 4)
    description_text = models.TextField(null=True)  # noqa: DJ001 (the cleaned copy; disposable)
    url = models.TextField()
    apply_url = models.TextField(null=True)  # noqa: DJ001
    posted_at = models.DateTimeField(null=True)  # null for careers-page postings
    raw = models.JSONField(null=True)  # the source payload; never returned by the API
    content_hash = models.CharField(max_length=40)  # sha1 hex (§10.3)
    dedupe_key = models.CharField(max_length=40)  # sha1 of company | title | primary location
    is_active = models.BooleanField(default=True)
    first_seen_at = models.DateTimeField()  # never updated after insert
    last_seen_at = models.DateTimeField()
    fetched_at = models.DateTimeField()  # cache metadata (invariant 6)
    # How long this posting had been up when we first saw it, in seconds: the smaller of
    # `first_seen_at - posted_at` and the time since its board's previous successful check (it
    # was not listed then, whatever date the source gives it). Null when nothing bounds it: a
    # board's first fetch, the aggregator, careers pages (#55).
    seen_lag_s = models.IntegerField(null=True)
    # What the posting asks of whoever applies, read once at ingest (`jobs/facts.py`, #56) and
    # again only when the posting changes or the reading does (`facts_version`, null: not read
    # yet). `facts` keeps the sentence each one was read from.
    level = models.TextField(choices=JobLevel.choices, db_default=JobLevel.UNKNOWN)
    years_min = models.SmallIntegerField(null=True)  # years of experience required; null: unsaid
    # The least a "preferred" figure asks for, when the posting requires none.
    years_preferred = models.SmallIntegerField(null=True)
    work_auth = models.TextField(choices=WorkAuth.choices, db_default=WorkAuth.UNKNOWN)
    clearance = models.TextField(choices=Clearance.choices, db_default=Clearance.NONE)
    facts = models.JSONField(null=True)
    facts_version = models.SmallIntegerField(null=True)
    # For sources read a posting at a time (`sources.types.ListedAdapter`, #57): what the
    # source's listing said of this posting when it was last read. A different value in the
    # listing means it has to be read again.
    listing_version = models.TextField(null=True)  # noqa: DJ001 (null: not from a listed source)

    company_id: uuid.UUID | None

    class Meta:
        db_table = "jobs"
        constraints = [
            models.CheckConstraint(condition=Q(id__regex=JOB_ID_PATTERN), name="jobs_id_shape"),
            *choices_checks(
                "jobs",
                source=JobSource,
                remote=Remote,
                salary_period=Period,
                level=JobLevel,
                work_auth=WorkAuth,
                clearance=Clearance,
            ),
        ]
        indexes = [
            # `company` is indexed by its foreign key.
            models.Index(fields=["dedupe_key"], name="jobs_dedupe_key_idx"),
            # The search prefilter's shape.
            models.Index(
                F("posted_at").desc(nulls_last=True),
                condition=Q(is_active=True),
                name="jobs_active_posted",
            ),
            # A watch's window: the postings first seen since its last pass (#5).
            models.Index(
                F("first_seen_at").desc(),
                condition=Q(is_active=True),
                name="jobs_active_first_seen",
            ),
            # The radar's lag report: the week's measured postings, closed ones included.
            models.Index(
                F("first_seen_at").desc(),
                condition=Q(seen_lag_s__isnull=False),
                name="jobs_seen_lag",
            ),
        ]

    def __str__(self) -> str:
        return self.id


class ApiUsage(models.Model):
    """Third-party request counters by UTC calendar month (JSearch's free tier, §10.6)."""

    provider = models.TextField()
    period = models.CharField(max_length=7)  # UTC "YYYY-MM"
    requests = models.IntegerField(default=0)
    updated_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "api_usage"
        constraints = [
            models.UniqueConstraint(fields=["provider", "period"], name="api_usage_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.provider}:{self.period}: {self.requests}"
