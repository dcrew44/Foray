"""Recognising the same job twice: dedupe keys, content hashes and ids (design §10.3, §2.5;
`map/worker.md` §17.3, §22.12-13).

The legacy import recomputes `content_hash` and `dedupe_key` with these functions (§3.3), so the
first refresh after the move sees unchanged postings as unchanged. Both hash a JSON or `|` string
exactly as the TypeScript did: `JSON.stringify` is `json.dumps(..., separators=(",", ":"),
ensure_ascii=False)`, an integral salary is written `150000` (not `150000.0`), locations sort by
UTF-16 code units as JS `sort()` does, and lone surrogates become U+FFFD first (psycopg refuses
them, so they are replaced before hashing and before storing).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable
from typing import Any

from apps.jobs.sources.js import IA, JS_WS, NOT_LT, S

_SURROGATE = re.compile("[\ud800-\udfff]")


def scrub(value: Any) -> Any:
    """`value` with every lone surrogate replaced by U+FFFD, recursively through JSON."""
    if isinstance(value, str):
        return _SURROGATE.sub("\ufffd", value)
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, dict):
        return {scrub(key): scrub(item) for key, item in value.items()}
    return value


def sha1_hex(text: str) -> str:
    return hashlib.sha1(scrub(text).encode("utf-8")).hexdigest()


# Suffixes ATS boards append to a company name but which are not part of it.
COMPANY_SUFFIX = re.compile(
    r"\b(?:inc|inc\.|llc|l\.l\.c\.|ltd|ltd\.|limited|corp|corp\.|corporation|co|co\.|gmbh|sa|s\.a\."
    r"|plc|pty|ag|nv|bv)\b",
    IA,
)

# Bracketed and trailing qualifiers on a title: "(Remote)", "- San Francisco", "[Contract]" and
# requisition numbers vary between the same job's two listings. Applied in order.
TITLE_NOISE = [
    re.compile(r"\([^)]*\)"),
    re.compile(r"\[[^\]]*\]"),
    re.compile(rf"\bREQ[-{JS_WS}#]?[0-9]+\b", IA),
    re.compile(rf"\b(?:#{S}*)?[0-9]{{4,}}\b", re.ASCII),
    re.compile(
        rf"{S}+[-\u2013\u2014]{S}+(?:remote|hybrid|on-?site|contract|full[-{JS_WS}]?time"
        rf"|part[-{JS_WS}]?time)\b{NOT_LT}*\Z",
        IA,
    ),
]

_NOT_ALNUM = re.compile(r"[^a-z0-9]+")
_NOT_TITLE_CHAR = re.compile(r"[^a-z0-9+#]+")


def normalize_company(name: str) -> str:
    lowered = name.lower().replace("&", " and ")
    return _NOT_ALNUM.sub(" ", COMPANY_SUFFIX.sub(" ", lowered)).strip(" ")


def normalize_title(title: str) -> str:
    cleaned = title.lower()
    for pattern in TITLE_NOISE:
        cleaned = pattern.sub(" ", cleaned)
    # `+` and `#` survive so "C++" and "C#" stay distinguishable; seniority numerals (II, III)
    # are kept, since Engineer II and Engineer III are genuinely different jobs.
    return _NOT_TITLE_CHAR.sub(" ", cleaned).strip(" ")


def normalize_location_for_key(location: str | None) -> str:
    if not location:
        return ""
    return _NOT_ALNUM.sub(" ", location.lower()).strip(" ")


def dedupe_key(company_name: str, title: str, primary_location: str | None = None) -> str:
    """`sha1(company | title | primary location)`: a deliberately coarse bucketing key, so an
    aggregator's copy and the company board's copy of one posting collapse."""
    parts = [
        normalize_company(company_name),
        normalize_title(title),
        normalize_location_for_key(primary_location),
    ]
    return sha1_hex("|".join(parts))


def _utf16_order(value: str) -> bytes:
    # JS `sort()` compares UTF-16 code units; big-endian UTF-16 bytes compare the same way.
    return value.encode("utf-16-be", "surrogatepass")


def _json_number(value: float | None) -> int | float | None:
    if value is None or not math.isfinite(value):
        return None  # JSON.stringify writes NaN and Infinity as null
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e21:
        return int(value)  # JS prints 150000, not 150000.0
    return value


def content_hash(
    title: str,
    locations: Iterable[str],
    description_raw: str | None = None,
    salary_min: float | None = None,
    salary_max: float | None = None,
) -> str:
    """Whether a posting's substance changed since we last saw it: keyed on what a user would
    notice (title, places, description, pay), never on `fetched_at`."""
    payload = [
        scrub(title),
        sorted((scrub(location) for location in locations), key=_utf16_order),
        scrub(description_raw) if description_raw is not None else "",
        _json_number(salary_min),
        _json_number(salary_max),
    ]
    return sha1_hex(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))


def job_id(source: str, company_slug: str, external_id: str) -> str:
    """`source:company_slug:external_id` (invariant 2)."""
    return f"{source}:{company_slug}:{external_id}"


def web_job_id(company_slug: str, url: str) -> str:
    """Careers-page postings have no id of their own, so the WHATWG-serialised URL is the
    identity."""
    return job_id("web", company_slug, sha1_hex(url))
