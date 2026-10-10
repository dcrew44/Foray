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


# The shape of every job id: `source:company_slug:external_id`, or `web:<slug>:<sha1(url)>`.
JOB_ID_PATTERN = r"^(greenhouse|lever|ashby|jsearch|web):[^:]+:.+$"
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

    company_id: uuid.UUID | None

    class Meta:
        db_table = "jobs"
        constraints = [
            models.CheckConstraint(condition=Q(id__regex=JOB_ID_PATTERN), name="jobs_id_shape"),
            *choices_checks("jobs", source=JobSource, remote=Remote, salary_period=Period),
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
