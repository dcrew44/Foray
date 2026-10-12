"""The JSON columns of the shared mirror (design §2.3, §10.5; `map/worker.md` §13.1, §17.6).

- `jobs.location_norm` is `NormalizedLocation` (keys exactly `raw, city, region, country,
  metro`; the only metro in use is `bay-area`).
- `companies.listing_cache` maps a careers-page listing URL to a `ListingCacheEntry`: the whole
  extraction of that page (`postings` and `moreListingUrls`), keyed by `EXTRACTOR_VERSION` (`v`)
  and the sha1 of the listing text as sent to the model (`sha1`). A hit replays the extraction
  without a model call (§10.5).
- `ExtractedPosting` is one posting as the careers-page model returns it. Every field is
  required, absence is null: OpenAI's strict structured output rejects optional properties.
- `CompanyRow` is one company as the companies page shows it. It is also what a
  `company_discovery` run stores as `output.company` (`runs.catalogue.CompanyAddOutput`), so it
  lives here, where the catalogue can import it without importing the pipeline.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import Field, PlainSerializer, WithJsonSchema

from apps.core.choices import CAMEL, JsonModel
from apps.core.router import ApiModel
from apps.jobs.models import CompanyStatus, DiscoveredVia, FetchStatus, SourceType


class NormalizedLocation(JsonModel):
    raw: str
    city: str | None
    region: str | None
    country: str | None
    metro: str | None


class ExtractedPosting(JsonModel):
    title: str
    location: str | None
    url: str | None
    team: str | None


class ListingCacheEntry(JsonModel):
    model_config = CAMEL

    v: int
    sha1: str
    postings: list[ExtractedPosting] = Field(default_factory=list)
    more_listing_urls: list[str] = Field(default_factory=list)


type ListingCache = dict[str, ListingCacheEntry]


def read_listing_cache(value: Any) -> ListingCache:
    """`companies.listing_cache`, keeping the readable entries. Never raises."""
    if not isinstance(value, dict):
        return {}
    entries = {url: ListingCacheEntry.read(entry) for url, entry in value.items()}
    return {url: entry for url, entry in entries.items() if entry is not None}


def dump_listing_cache(cache: ListingCache) -> dict[str, Any]:
    return {url: entry.dump() for url, entry in cache.items()}


def _iso_string(value: datetime | None) -> str | None:
    from apps.jobs.sources.types import js_iso  # that module imports this one

    return js_iso(value)


# A datetime that a stored JSON column keeps as `toISOString()` (milliseconds, `Z`), as
# `jobs.pipeline.company_row` writes it and as the API's own encoder sends it. Its schema stays a
# date-time string.
IsoDatetime = Annotated[
    datetime | None,
    PlainSerializer(_iso_string, return_type=str | None, when_used="json"),
    WithJsonSchema(
        {"anyOf": [{"format": "date-time", "type": "string"}, {"type": "null"}]},
        mode="serialization",
    ),
]


class CompanyRow(ApiModel):
    """One company as the companies page shows it (`company-row.ts`'s `CompanyRow`)."""

    slug: str
    name: str
    domain: str | None
    source_type: SourceType
    ats_slug: str | None
    careers_url: str | None  # the first careers page, for companies read without an ATS API
    status: CompanyStatus
    needs_browser: bool
    tags: list[str]
    last_fetched_at: IsoDatetime
    last_fetch_status: FetchStatus | None
    last_fetch_job_count: int | None
    discovered_via: DiscoveredVia
    active_jobs: int  # open postings in the mirror right now
