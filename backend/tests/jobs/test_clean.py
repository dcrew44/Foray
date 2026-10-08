"""Cleaning, dedupe keys and content hashes (ported from `clean.test.ts`, 20 cases), plus golden
equality with the TypeScript `clean.ts` and `dedupe.ts` (design §12.2). The hash tests are
Python-native: stable, order-insensitive, change-sensitive, and equal to the TS goldens.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from apps.jobs.sources import ashby, greenhouse, jsearch, lever
from apps.jobs.sources.clean import CleanResult, clean_description, decode_entities, html_to_text
from apps.jobs.sources.dedupe import (
    content_hash,
    dedupe_key,
    job_id,
    normalize_company,
    normalize_location_for_key,
    normalize_title,
    scrub,
    web_job_id,
)
from tests.jobs.conftest import fixture_json, golden

COMPACT = {"separators": (",", ":"), "ensure_ascii": False}
CLEAN = golden("clean/cases.json")
DEDUPE = golden("dedupe/cases.json")


# --- goldens -------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", CLEAN["cleanDescription"])
def test_clean_description_matches_golden(case: dict) -> None:
    result = clean_description(case["input"])
    assert {"text": result.text, "removed": result.removed} == case["output"]


@pytest.mark.parametrize("case", CLEAN["htmlToText"])
def test_html_to_text_matches_golden(case: dict) -> None:
    assert html_to_text(case["input"]) == case["output"]


@pytest.mark.parametrize("case", CLEAN["decodeEntities"])
def test_decode_entities_matches_golden(case: dict) -> None:
    assert decode_entities(case["input"]) == case["output"]


def test_boilerplate_removed_from_every_fixture_matches_golden() -> None:
    expected: dict[str, list[str]] = golden("clean/fixture_removed.json")
    boards = {
        "greenhouse": (greenhouse.normalize_job, ["anthropic", "stripe"]),
        "lever": (lever.normalize_job, ["matchgroup", "spotify"]),
        "ashby": (ashby.normalize_job, ["linear", "vanta"]),
    }
    seen: dict[str, list[str]] = {}
    for source, (normalize, slugs) in boards.items():
        for slug in slugs:
            payload = fixture_json(f"{source}/{slug}.json")
            postings = payload if source == "lever" else payload["jobs"]
            for posting in postings:
                job = normalize(slug, posting)
                seen[job.id] = clean_description(job.description_raw).removed
    for posting in fixture_json("jsearch/entry-level-sf.json")["data"]["jobs"]:
        job = jsearch.normalize_job(posting)
        seen[job.id] = clean_description(job.description_raw).removed
    assert seen == expected


@pytest.mark.parametrize("case", DEDUPE["normalizeCompany"])
def test_normalize_company_matches_golden(case: dict) -> None:
    assert normalize_company(case["input"]) == case["output"]


@pytest.mark.parametrize("case", DEDUPE["normalizeTitle"])
def test_normalize_title_matches_golden(case: dict) -> None:
    assert normalize_title(case["input"]) == case["output"]


@pytest.mark.parametrize("case", DEDUPE["normalizeLocationForKey"])
def test_normalize_location_for_key_matches_golden(case: dict) -> None:
    assert normalize_location_for_key(case["input"]) == case["output"]


@pytest.mark.parametrize("case", DEDUPE["dedupeKey"])
def test_dedupe_key_matches_golden(case: dict) -> None:
    given = case["input"]
    assert (
        dedupe_key(given["companyName"], given["title"], given.get("primaryLocation"))
        == case["output"]
    )


@pytest.mark.parametrize("case", DEDUPE["contentHash"])
def test_content_hash_matches_golden(case: dict) -> None:
    given = case["input"]
    assert (
        content_hash(
            given["title"],
            given["locations"],
            given.get("descriptionRaw"),
            given.get("salaryMin"),
            given.get("salaryMax"),
        )
        == case["output"]
    )


@pytest.mark.parametrize("case", DEDUPE["jobId"])
def test_job_id_matches_golden(case: dict) -> None:
    assert job_id(*case["input"]) == case["output"]


@pytest.mark.parametrize("case", DEDUPE["webJobId"])
def test_web_job_id_matches_golden(case: dict) -> None:
    assert web_job_id(case["input"]["companySlug"], case["input"]["url"]) == case["output"]


# --- htmlToText ----------------------------------------------------------------------------------


def test_html_to_text_keeps_list_structure_as_bullets() -> None:
    text = html_to_text("<ul><li>First thing</li><li>Second thing</li></ul>")
    assert "\u2022 First thing" in text
    assert "\u2022 Second thing" in text


def test_html_to_text_decodes_entities() -> None:
    assert html_to_text("<p>Ben &amp; Jerry&rsquo;s</p>") == "Ben & Jerry\u2019s"


def test_html_to_text_drops_script_and_style() -> None:
    html = "<p>Real text</p><script>var x = 'not text';</script><style>.a{}</style>"
    assert html_to_text(html) == "Real text"


def test_html_to_text_drops_uppercase_script_with_a_lowercase_close() -> None:
    assert html_to_text("<p>Kept</p><SCRIPT>gone()</script>") == "Kept"


# --- cleanDescription ----------------------------------------------------------------------------


def test_clean_handles_entity_escaped_html() -> None:
    raw = "&lt;p&gt;We are hiring an engineer.&lt;/p&gt;"
    assert clean_description(raw).text == "We are hiring an engineer."


def test_clean_strips_a_trailing_equal_opportunity_block() -> None:
    body = "We are hiring.\n\n" * 30
    result = clean_description(
        f"{body}Acme Corp is an equal opportunity employer and does not discriminate."
    )
    assert "equal opportunity" in result.removed
    assert "equal opportunity" not in result.text
    assert "We are hiring." in result.text


def test_clean_strips_e_verify_and_accommodation_from_the_tail() -> None:
    body = "Responsibilities include shipping code.\n\n" * 20
    result = clean_description(
        f"{body}E-Verify is used by this employer.\n\nIf you need an accommodation, contact us."
    )
    assert {"E-Verify", "accommodation"} <= set(result.removed)


def test_clean_leaves_the_same_wording_in_the_body() -> None:
    tail = "You will ship features.\n\n" * 40
    result = clean_description(
        f"We are an equal opportunity employer and you will own that programme.\n\n{tail}"
    )
    assert result.removed == []
    assert "equal opportunity" in result.text


def test_clean_never_summarises() -> None:
    paragraphs = [
        "Build and operate the billing service.",
        "Work with a team of six engineers.",
        "Requirements: three years of TypeScript.",
    ]
    text = clean_description("\n\n".join(paragraphs)).text
    for paragraph in paragraphs:
        assert paragraph in text


def test_clean_accepts_plain_text() -> None:
    assert clean_description("Just plain text.").text == "Just plain text."


def test_clean_returns_empty_for_nothing() -> None:
    assert clean_description(None) == CleanResult("", [])


def test_clean_uses_the_javascript_whitespace_class() -> None:
    # U+00A0 and U+FEFF are trimmed as JS does; U+0085 and U+001C are not JS whitespace.
    assert clean_description("\u00a0\ufeffText\u2028").text == "Text"
    assert clean_description("\u0085Text\u001c").text == "\u0085Text\u001c"


# --- dedupe --------------------------------------------------------------------------------------


def test_dedupe_ignores_legal_suffixes() -> None:
    assert normalize_company("Stripe, Inc.") == normalize_company("Stripe")
    assert normalize_company("Acme Corp") == normalize_company("ACME")


def test_dedupe_strips_bracketed_and_trailing_title_qualifiers() -> None:
    assert normalize_title("Software Engineer (Remote)") == normalize_title("Software Engineer")
    assert normalize_title("Backend Engineer - Remote") == normalize_title("Backend Engineer")
    assert normalize_title("Data Engineer [REQ-1234]") == normalize_title("Data Engineer")


def test_dedupe_keeps_a_seniority_numeral() -> None:
    assert normalize_title("Engineer II") != normalize_title("Engineer III")


def test_dedupe_keeps_language_symbols() -> None:
    assert normalize_title("C++ Engineer") != normalize_title("C# Engineer")


def test_dedupe_collapses_one_job_seen_through_two_sources() -> None:
    aggregator = dedupe_key("Stripe, Inc.", "Software Engineer, Payments (Remote)", "San Francisco")
    board = dedupe_key("Stripe", "Software Engineer, Payments", "san francisco")
    assert aggregator == board


def test_dedupe_does_not_collapse_different_roles() -> None:
    assert dedupe_key("Stripe", "Backend Engineer", "San Francisco") != dedupe_key(
        "Stripe", "Frontend Engineer", "San Francisco"
    )


def test_dedupe_does_not_collapse_one_role_in_two_cities() -> None:
    assert dedupe_key("Stripe", "Backend Engineer", "San Francisco") != dedupe_key(
        "Stripe", "Backend Engineer", "New York"
    )


# --- contentHash (Python-native) -----------------------------------------------------------------

BASE = {
    "title": "Engineer",
    "locations": ["San Francisco, CA"],
    "description_raw": "Build things.",
    "salary_min": 100.0,
    "salary_max": 200.0,
}


def test_content_hash_is_stable() -> None:
    assert content_hash(**BASE) == content_hash(**dict(BASE))


def test_content_hash_ignores_location_order() -> None:
    assert content_hash(**{**BASE, "locations": ["A", "B"]}) == content_hash(
        **{**BASE, "locations": ["B", "A"]}
    )


def test_content_hash_changes_with_the_description_or_pay() -> None:
    assert content_hash(**{**BASE, "description_raw": "Build other things."}) != content_hash(
        **BASE
    )
    assert content_hash(**{**BASE, "salary_max": 250.0}) != content_hash(**BASE)


def test_content_hash_writes_integral_floats_as_javascript_does() -> None:
    # float8 reads give 150000.0; JSON.stringify wrote 150000.
    integral = content_hash("E", [], "x", 150000.0, 200000.0)
    assert integral == content_hash("E", [], "x", 150000, 200000)


def test_content_hash_sorts_by_utf16_code_units() -> None:
    # JS sorts by UTF-16 code units: U+1F600 (D83D DE00) comes before U+FF5E, the reverse of
    # code-point order.
    emoji, fullwidth = "\U0001f600", "\uff5e"
    javascript_order = json.dumps(["E", [emoji, fullwidth], "", None, None], **COMPACT)
    expected = hashlib.sha1(javascript_order.encode()).hexdigest()
    assert content_hash("E", [fullwidth, emoji]) == expected


def test_lone_surrogates_are_replaced_before_hashing() -> None:
    assert scrub("a\ud800b") == "a\ufffdb"
    assert content_hash("E", [], "a\ud800b") == content_hash("E", [], "a\ufffdb")
    assert scrub({"k\udfff": ["\ud800"]}) == {"k\ufffd": ["\ufffd"]}
