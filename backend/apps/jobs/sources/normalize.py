"""Turning what boards say into something we can filter on (design §10.3; `map/worker.md` §17.2).

A literal port of `normalize.ts`, pinned by goldens. Every function keeps the source's own words
in `raw` and treats its output as an interpretation: a location that cannot be recognised stays
null rather than guessed, because a wrong metro silently drops a job out of a search.
"""

from __future__ import annotations

import math
import re
from typing import Any

from apps.jobs.schemas import NormalizedLocation
from apps.jobs.sources.js import IA, S, js_trim
from apps.jobs.sources.types import NormalizedSalary, RemoteKind, SalaryPeriod

US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}  # fmt: skip

# Cities we group into a metro. Only the Bay Area for now: a gazetteer, not a geocoder, so that
# "Mountain View" and "SF" answer the same filter. It grows by adding rows.
METROS: dict[str, tuple[str, ...]] = {
    "bay-area": (
        "san francisco", "sf", "south san francisco", "oakland", "berkeley",
        "emeryville", "alameda", "san mateo", "burlingame", "redwood city",
        "menlo park", "palo alto", "east palo alto", "mountain view",
        "sunnyvale", "santa clara", "san jose", "cupertino", "milpitas",
        "fremont", "hayward", "san bruno", "daly city", "foster city",
        "brisbane", "los altos", "campbell", "los gatos", "pleasanton",
        "walnut creek", "san rafael", "novato", "sausalito", "richmond",
        "bay area", "sf bay area", "san francisco bay area", "silicon valley",
        "peninsula", "south bay", "east bay", "north bay",
    ),
}  # fmt: skip

CITY_TO_METRO = {city: metro for metro, cities in METROS.items() for city in cities}

REMOTE_WORDS = re.compile(
    rf"\b(?:fully{S}+remote|remote(?:{S}*-?{S}*(?:first|friendly|ok|eligible|us|usa|global|anywhere))?"
    rf"|work{S}+from{S}+home|wfh|distributed|virtual)\b",
    IA,
)
HYBRID_WORDS = re.compile(rf"\b(?:hybrid|flexible{S}+(?:work|location)|partially{S}+remote)\b", IA)
ONSITE_WORDS = re.compile(r"\b(?:on-?site|in-?office|in-?person)\b", IA)

# How much of a description may vote on remote status, and only when nothing stronger did.
DESCRIPTION_HEAD = 600


def detect_remote(
    *,
    location: str | None = None,
    title: str | None = None,
    is_remote_flag: bool | None = None,
    description: str | None = None,
) -> RemoteKind:
    """Remote status from whatever signals exist: an explicit flag, then the location and the
    title, and only then the head of the description (which mentions "remote" in passing far
    too often to be trusted on its own)."""
    if is_remote_flag is True:
        return "remote"

    strong = f"{location or ''} {title or ''}"
    if HYBRID_WORDS.search(strong):
        return "hybrid"
    if REMOTE_WORDS.search(strong):
        return "remote"
    if ONSITE_WORDS.search(strong):
        return "onsite"

    # A named city with no remote wording is an onsite role in practice.
    if location and parse_location(location).city:
        return "onsite"

    if description:
        head = description[:DESCRIPTION_HEAD]
        if HYBRID_WORDS.search(head):
            return "hybrid"
        if REMOTE_WORDS.search(head):
            return "remote"

    return "unknown"


# Trailing qualifiers boards append to a city, which are not part of it.
LOCATION_NOISE = re.compile(
    rf"{S}*[-\u2013\u2014(]{S}*(?:remote|hybrid|on-?site|in-?office|flexible|multiple locations"
    rf"|various)[^)]*\)?{S}*\Z",
    IA,
)
_LOCATION_SPLIT = re.compile(rf"{S}*[,|/]{S}*")
_TWO_CAPITALS = re.compile(r"[A-Z]{2}\Z")
_USA = re.compile(r"(?:usa|u\.s\.a?\.?|united states)\Z", IA)
_UK = re.compile(r"(?:uk|united kingdom|england)\Z", IA)


def parse_location(raw: str) -> NormalizedLocation:
    cleaned = js_trim(LOCATION_NOISE.sub("", raw, count=1))
    base = NormalizedLocation(raw=raw, city=None, region=None, country=None, metro=None)
    if not cleaned:
        return base

    lower = cleaned.lower()
    # A metro named directly ("SF Bay Area") has no city to extract.
    direct = CITY_TO_METRO.get(lower)
    if direct:
        return base.model_copy(update={"metro": direct})

    parts = [part for part in (js_trim(p) for p in _LOCATION_SPLIT.split(cleaned)) if part]
    if not parts:
        return base

    city, rest = parts[0], parts[1:]
    region: str | None = None
    country: str | None = None
    for part in rest:
        key = part.lower()
        if key in US_STATES:
            region = US_STATES[key]
            country = country or "US"
        elif _TWO_CAPITALS.match(part) and part != "UK":
            region = part
            country = country or "US"
        elif _USA.match(part):
            country = "US"
        elif _UK.match(part):
            country = "GB"
        elif not region:
            region = part
        else:
            country = part

    metro = CITY_TO_METRO.get(city.lower()) or CITY_TO_METRO.get(lower)
    return NormalizedLocation(raw=raw, city=city, region=region, country=country, metro=metro)


def primary_location(locations: list[str]) -> NormalizedLocation | None:
    """The first location we can make sense of, else the first one at all."""
    if not locations:
        return None
    parsed = [parse_location(location) for location in locations]
    return next((location for location in parsed if location.metro), parsed[0])


CURRENCIES = {
    "$": "USD", "us$": "USD", "usd": "USD",
    "£": "GBP", "gbp": "GBP",
    "€": "EUR", "eur": "EUR",
    "c$": "CAD", "cad": "CAD",
}  # fmt: skip

_AMOUNT = re.compile(rf"([0-9,.]+){S}*([km])?\Z", IA)


def _js_number(text: str) -> float:
    """`Number(text)` for a string of digits and dots: `""` is 0 and anything else unreadable
    is NaN."""
    if text == "":
        return 0.0
    try:
        return float(text)
    except ValueError:
        return math.nan


def parse_amount(text: str) -> float | None:
    """`"180,000"`, `"180k"`, `"180"` (in a k-scaled range) as a number."""
    match = _AMOUNT.match(js_trim(text))
    if not match:
        return None
    value = _js_number(match.group(1).replace(",", ""))
    if not math.isfinite(value):
        return None
    suffix = (match.group(2) or "").lower()
    if suffix == "k":
        return value * 1_000
    if suffix == "m":
        return value * 1_000_000
    return value


PERIOD_WORDS: list[tuple[re.Pattern[str], SalaryPeriod]] = [
    (re.compile(rf"\b(?:per{S}+hour|hourly|/{S}*(?:hr|hour)|an{S}+hour)\b", IA), "hour"),
    (re.compile(rf"\b(?:per{S}+month|monthly|/{S}*(?:mo|month)|a{S}+month)\b", IA), "month"),
    (re.compile(rf"\b(?:per{S}+year|annually|annual|/{S}*(?:yr|year)|a{S}+year)\b", IA), "year"),
]

_RANGE = re.compile(
    rf"(?P<currency>US\$|C\$|[$£€])?{S}*(?P<min>[0-9][0-9,.]*{S}*[km]?){S}*"
    rf"(?:-|\u2013|\u2014|to){S}*(?P<currency2>US\$|C\$|[$£€])?{S}*(?P<max>[0-9][0-9,.]*{S}*[km]?)",
    IA,
)
_KM = re.compile("[km]", IA)
_CURRENCY_CODE = re.compile(r"\b(USD|GBP|EUR|CAD)\b", IA)
_MONEY_WORD = re.compile(
    r"\b(?:salary|salaries|compensation|comp|pay|paid|wage|base|ote|remuneration|stipend)\b", IA
)


def parse_salary(text: str | None) -> NormalizedSalary:
    """A salary out of free text, for the majority of boards that only put the range in the
    description. Only the first range is read, and only when something nearby says it is
    money: a currency, a period or a pay word. Job posts are full of year ranges, headcounts
    and version numbers, and a salary invented from one of those would filter real jobs out."""
    if not text:
        return NormalizedSalary()

    found = _RANGE.search(text)
    if not found:
        return NormalizedSalary()

    low = parse_amount(found.group("min"))
    high = parse_amount(found.group("max"))
    if low is None or high is None:
        return NormalizedSalary()

    # "180-220k" means 180k, not 180: the suffix carries across the range.
    if high >= 1000 and low < 1000 and _KM.search(found.group("max")):
        low *= 1_000_000 if high >= 1_000_000 else 1_000

    if low <= 0 or low > high:
        return NormalizedSalary()

    symbol = (found.group("currency") or found.group("currency2") or "").lower()
    index = found.start()
    nearby = text[max(0, index - 40) : index + 120]
    code_match = _CURRENCY_CODE.search(nearby)
    code = code_match.group(1).lower() if code_match else ""
    currency = CURRENCIES.get(symbol) or CURRENCIES.get(code)

    period_word: SalaryPeriod | None = next(
        (period for rule, period in PERIOD_WORDS if rule.search(nearby)), None
    )
    if not currency and not period_word and not _MONEY_WORD.search(nearby):
        return NormalizedSalary()

    # An hourly rate is the only way a genuine pay range lands this low.
    period: SalaryPeriod = period_word or "year"
    if period_word is None and high < 1000:
        period = "hour"
    salary = NormalizedSalary(min=low, max=high, currency=currency, period=period)
    return salary if is_plausible_salary(salary) else NormalizedSalary()


def is_plausible_salary(salary: NormalizedSalary) -> bool:
    """Does this range plausibly describe pay for the period it claims? Applied to structured
    fields too: Ashby serves `minValue: 90` for "$90K", and a $90 floor is worse than none."""
    low, high = salary.min, salary.max
    if low is None or high is None:
        return False
    if low <= 0 or low > high:
        return False
    match salary.period:
        case "hour":
            return low >= 5 and high <= 2_000
        case "month":
            return low >= 500 and high <= 200_000
        case _:
            # "year", or no period: accept only magnitudes that could be an annual figure.
            return low >= 15_000 and high <= 10_000_000


def number_or_none(value: Any) -> float | None:
    """A JSON number from a payload (`value ?? null` where TypeScript trusted the type)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value
