"""The company registry (design §10.5, §12.3): slug normalisation and `-2` suffixes, board
identity, union merges, the status rules, careers-page matching and `find_companies_by_names`."""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from django.db import connections
from django.utils import timezone

from apps.jobs.models import Company
from apps.jobs.registry import (
    CompanyInput,
    company_slug,
    count_by_status,
    find_companies_by_names,
    record_fetch,
    stale_companies,
    upsert_company,
)
from tests.factories import make_company

pytestmark = pytest.mark.django_db


def board(slug: str, **fields) -> CompanyInput:
    fields.setdefault("name", slug.title())
    fields.setdefault("source_type", "greenhouse")
    fields.setdefault("ats_slug", slug)
    return CompanyInput(slug=slug, **fields)


def careers(slug: str, **fields) -> CompanyInput:
    fields.setdefault("name", slug.title())
    fields.setdefault("careers_urls", [f"https://{slug}.example.com/careers"])
    return CompanyInput(slug=slug, source_type="careers_page", **fields)


# --- slugs ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "slug"),
    [
        ("acme_corp", "acme-corp"),
        ("Acme Labs", "acme-labs"),
        ("--", "company"),
        ("!!! ???", "company"),
        ("1password", "1password"),
        ("-Linear-", "linear"),
        ("Café Numérique", "caf-num-rique"),
    ],
)
def test_slug_normalised(raw: str, slug: str) -> None:
    assert company_slug(raw) == slug


def test_a_normalised_slug_is_written_and_the_ats_slug_kept() -> None:
    company, created = upsert_company(board("acme_corp"))
    assert created
    assert company.slug == "acme-corp"
    assert company.ats_slug == "acme_corp"


def test_a_taken_slug_gets_a_suffix_for_a_new_company() -> None:
    first, _ = upsert_company(board("linear", source_type="ashby"))
    second, created = upsert_company(board("linear", source_type="lever"))
    third, _ = upsert_company(careers("linear"))
    assert created
    assert (first.slug, second.slug, third.slug) == ("linear", "linear-2", "linear-3")
    # The other company is untouched (the hijack of map/worker.md §22.23 is gone).
    first.refresh_from_db()
    assert (first.source_type, first.ats_slug) == ("ashby", "linear")


def test_an_existing_slug_never_changes() -> None:
    make_company(slug="stripe", ats_slug="stripe")
    company, created = upsert_company(board("stripe", name="Stripe, Inc."))
    assert not created
    assert company.slug == "stripe"
    assert company.name == "Stripe, Inc."


# --- board identity ------------------------------------------------------------------------------


def test_a_board_is_identified_by_source_and_ats_slug() -> None:
    a, created_a = upsert_company(board("ramp"))
    b, created_b = upsert_company(board("ramp", name="Ramp"))
    assert created_a and not created_b
    assert a.id == b.id
    assert Company.objects.count() == 1


def test_the_same_slug_on_another_ats_is_another_company() -> None:
    a, _ = upsert_company(board("ramp", source_type="greenhouse"))
    b, created = upsert_company(board("ramp", source_type="ashby"))
    assert created and a.id != b.id


@pytest.mark.django_db(transaction=True)
def test_concurrent_upserts_of_one_board_make_one_row() -> None:
    results: list[tuple[str, bool]] = []
    barrier = threading.Barrier(6)

    def add() -> None:
        try:
            barrier.wait()
            company, created = upsert_company(board("figma", tags=["design"]))
            results.append((str(company.id), created))
        finally:
            connections.close_all()

    threads = [threading.Thread(target=add) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len({company_id for company_id, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    assert Company.objects.filter(ats_slug="figma").count() == 1


# --- merges and status ---------------------------------------------------------------------------


def test_updates_union_tags_and_careers_urls() -> None:
    upsert_company(board("vercel", tags=["devtools"], careers_urls=["https://vercel.com/careers"]))
    company, _ = upsert_company(
        board(
            "vercel",
            tags=["infra", "devtools"],
            careers_urls=["https://vercel.com/jobs", "https://vercel.com/careers"],
        )
    )
    assert company.tags == ["devtools", "infra"]
    assert company.careers_urls == ["https://vercel.com/careers", "https://vercel.com/jobs"]


def test_an_empty_list_no_longer_wipes_the_urls() -> None:
    upsert_company(careers("retool", domain="retool.com"))
    company, _ = upsert_company(careers("retool", domain="retool.com", careers_urls=[], tags=[]))
    assert company.careers_urls == ["https://retool.example.com/careers"]


def test_a_missing_domain_keeps_the_known_one() -> None:
    upsert_company(board("notion", domain="notion.so"))
    company, _ = upsert_company(board("notion", domain=None))
    assert company.domain == "notion.so"


def test_seeding_never_changes_status() -> None:
    make_company(slug="gone", ats_slug="gone", status="invalid")
    company, _ = upsert_company(board("gone", discovered_via="seed"))
    assert company.status == "invalid"


def test_verified_discovery_reactivates_an_invalid_company() -> None:
    make_company(slug="back", ats_slug="back", status="invalid")
    company, _ = upsert_company(board("back", status="active"))
    assert company.status == "active"


def test_needs_browser_is_kept_unless_given() -> None:
    upsert_company(careers("crusoe", needs_browser=True))
    kept, _ = upsert_company(careers("crusoe"))
    assert kept.needs_browser is True
    cleared, _ = upsert_company(careers("crusoe", needs_browser=False))
    assert cleared.needs_browser is False


def test_discovered_via_and_added_by_never_change() -> None:
    first, _ = upsert_company(board("plaid", discovered_via="seed"))
    again, _ = upsert_company(board("plaid", discovered_via="agent"))
    assert first.id == again.id
    assert again.discovered_via == "seed"


# --- careers pages -------------------------------------------------------------------------------


def test_careers_pages_match_by_domain_then_slug() -> None:
    by_domain, _ = upsert_company(careers("acme", domain="acme.com"))
    same, created = upsert_company(
        careers("acme-inc", domain="acme.com", careers_urls=["https://acme.com/jobs"])
    )
    assert not created and same.id == by_domain.id
    assert same.careers_urls == ["https://acme.example.com/careers", "https://acme.com/jobs"]

    by_slug, _ = upsert_company(careers("beta"))
    again, created = upsert_company(careers("beta", domain="beta.io"))
    assert not created and again.id == by_slug.id
    assert again.domain == "beta.io"


def test_a_careers_page_never_takes_over_a_board() -> None:
    board_company, _ = upsert_company(board("gem"))
    page, created = upsert_company(careers("gem", domain="gem.com"))
    assert created
    assert page.id != board_company.id
    assert page.slug == "gem-2"


# --- reads and fetch records ---------------------------------------------------------------------


def test_stale_companies_puts_never_fetched_first() -> None:
    now = timezone.now()
    old = make_company(slug="old", last_fetched_at=now - timedelta(hours=5))
    never = make_company(slug="never")
    make_company(slug="fresh", last_fetched_at=now - timedelta(minutes=5))
    make_company(slug="paused", status="paused")
    stale = stale_companies(now - timedelta(hours=2))
    assert [company.slug for company in stale] == [never.slug, old.slug]


def test_record_fetch_keeps_the_count_when_none() -> None:
    company = make_company(slug="counted", last_fetch_job_count=12)
    record_fetch(company.id, "error", None)
    company.refresh_from_db()
    assert (company.last_fetch_status, company.last_fetch_job_count) == ("error", 12)
    record_fetch(company.id, "not-found", 0, mark_invalid=True)
    company.refresh_from_db()
    assert (company.status, company.last_fetch_job_count) == ("invalid", 0)
    assert company.last_fetched_at is not None


def test_count_by_status() -> None:
    make_company(slug="a1")
    make_company(slug="a2")
    make_company(slug="p1", status="paused")
    assert count_by_status() == {"active": 2, "paused": 1}


# --- find_companies_by_names ---------------------------------------------------------------------


def test_finds_by_name_slug_ats_slug_or_domain() -> None:
    anthropic = make_company(name="Anthropic", slug="anthropic", domain="anthropic.com")
    scale = make_company(name="Scale AI", slug="scale", ats_slug="scaleai")
    stripe = make_company(name="Stripe", slug="stripe-co", domain="stripe.com")
    result = find_companies_by_names(
        ["anthropic", "Scale-AI", "SCALEAI", "https://www.stripe.com/jobs"]
    )
    assert [(name, company.id) for name, company in result.found] == [
        ("anthropic", anthropic.id),
        ("Scale-AI", scale.id),
        ("SCALEAI", scale.id),
        ("https://www.stripe.com/jobs", stripe.id),
    ]
    assert result.missing == []


def test_prefers_a_board_over_a_careers_page() -> None:
    make_company(name="Retool", slug="retool-web", source_type="careers_page")
    board_company = make_company(name="Retool", slug="retool", ats_slug="retool")
    found = find_companies_by_names(["Retool"]).found
    assert found[0][1].id == board_company.id


def test_inactive_and_unknown_names_are_missing() -> None:
    make_company(name="Paused Co", slug="paused-co", status="paused")
    result = find_companies_by_names(["Paused Co", "Nobody"])
    assert result.found == []
    assert result.missing == ["Paused Co", "Nobody"]


def test_a_name_with_no_alphanumerics_is_reported_missing() -> None:
    assert find_companies_by_names(["!!!", "  "]).missing == ["!!!", "  "]
