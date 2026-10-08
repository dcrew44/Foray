"""The JSON columns of the shared mirror (design §2.3, §10.5; `map/worker.md` §13.1, §17.6).

- `jobs.location_norm` is `NormalizedLocation` (keys exactly `raw, city, region, country,
  metro`; the only metro in use is `bay-area`).
- `companies.listing_cache` maps a careers-page listing URL to a `ListingCacheEntry`: the whole
  extraction of that page (`postings` and `moreListingUrls`), keyed by `EXTRACTOR_VERSION` (`v`)
  and the sha1 of the listing text as sent to the model (`sha1`). A hit replays the extraction
  without a model call (§10.5).
- `ExtractedPosting` is one posting as the careers-page model returns it. Every field is
  required, absence is null: OpenAI's strict structured output rejects optional properties.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from apps.core.choices import CAMEL, JsonModel


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
