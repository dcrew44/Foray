"""Ingest into the shared mirror (design §10.4, §12.3): exact counts, duplicate ids, reactivation,
deactivation scope, the empty-board guard, and what is never rewritten."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest
from django.utils import timezone

from apps.jobs.ingest import BATCH_SIZE, IngestResult, ingest_jobs
from apps.jobs.models import Company, Job
from apps.jobs.sources.dedupe import content_hash, dedupe_key, job_id
from apps.jobs.sources.normalize import parse_location
from apps.jobs.sources.types import NormalizedJob, NormalizedSalary
from tests.factories import make_company

pytestmark = pytest.mark.django_db


def posting(
    company: Company, n: int, *, description: str = "Build things.", **changes
) -> NormalizedJob:
    title = changes.pop("title", f"Engineer {n}")
    locations = changes.pop("locations", ["San Francisco, CA"])
    salary = changes.pop("salary", NormalizedSalary(150000.0, 200000.0, "USD", "year"))
    job = NormalizedJob(
        id=job_id(company.source_type, company.slug, str(n)),
        source=company.source_type,  # type: ignore[arg-type]
        external_id=str(n),
        company_slug=company.slug,
        company_name=company.name,
        title=title,
        locations=locations,
        location_norm=parse_location(locations[0]) if locations else None,
        remote="onsite",
        employment_type=None,
        salary=salary,
        description_raw=f"<p>{description}</p>",
        description_text=description,
        url=f"https://example.com/jobs/{n}",
        apply_url=None,
        posted_at=datetime(2026, 9, 1, tzinfo=UTC),
        raw={"id": n},
        content_hash=content_hash(
            title, locations, f"<p>{description}</p>", salary.min, salary.max
        ),
        dedupe_key=dedupe_key(company.name, title, "San Francisco"),
    )
    return dataclasses.replace(job, **changes)


@pytest.fixture
def company() -> Company:
    return make_company(slug="acme", ats_slug="acme")


def test_first_ingest_inserts_everything(company: Company) -> None:
    result = ingest_jobs(company.id, [posting(company, n) for n in range(3)])
    assert result == IngestResult(inserted=3)
    job = Job.objects.get(id="greenhouse:acme:0")
    assert job.company_id == company.id
    assert job.is_active
    assert (job.salary_min, job.salary_max, job.salary_period) == (150000.0, 200000.0, "year")
    assert job.location_norm == {
        "raw": "San Francisco, CA",
        "city": "San Francisco",
        "region": "CA",
        "country": "US",
        "metro": "bay-area",
    }
    assert job.raw == {"id": 0}
    assert job.first_seen_at == job.last_seen_at == job.fetched_at


def test_exact_counts_for_new_changed_and_unchanged(company: Company) -> None:
    ingest_jobs(company.id, [posting(company, n) for n in range(3)])
    result = ingest_jobs(
        company.id,
        [
            posting(company, 0),
            posting(company, 1, description="Build other things."),
            posting(company, 2),
            posting(company, 3),
        ],
    )
    assert result == IngestResult(inserted=1, updated=1, unchanged=2)


def test_unchanged_postings_are_not_rewritten(company: Company) -> None:
    ingest_jobs(company.id, [posting(company, 0)])
    Job.objects.filter(id="greenhouse:acme:0").update(description_text="hand-edited")
    ingest_jobs(company.id, [posting(company, 0)])
    assert Job.objects.get(id="greenhouse:acme:0").description_text == "hand-edited"


def test_a_change_rewrites_the_source_columns_but_not_first_seen(company: Company) -> None:
    ingest_jobs(company.id, [posting(company, 0)])
    first = Job.objects.get(id="greenhouse:acme:0")
    Job.objects.filter(id=first.id).update(first_seen_at=datetime(2026, 1, 1, tzinfo=UTC))
    ingest_jobs(company.id, [posting(company, 0, description="New text.")])
    job = Job.objects.get(id=first.id)
    assert job.description_raw == "<p>New text.</p>"
    assert job.first_seen_at == datetime(2026, 1, 1, tzinfo=UTC)


def test_duplicate_ids_in_one_batch_keep_the_last_copy(company: Company) -> None:
    result = ingest_jobs(
        company.id,
        [posting(company, 7, description="first"), posting(company, 7, description="last")],
    )
    assert result == IngestResult(inserted=1)
    assert Job.objects.get(id="greenhouse:acme:7").description_text == "last"


def test_batches_larger_than_the_batch_size(company: Company) -> None:
    jobs = [posting(company, n) for n in range(BATCH_SIZE + 37)]
    assert ingest_jobs(company.id, jobs) == IngestResult(inserted=BATCH_SIZE + 37)
    assert ingest_jobs(company.id, jobs) == IngestResult(unchanged=BATCH_SIZE + 37)


# --- reactivation and deactivation ---------------------------------------------------------------


def test_an_unchanged_posting_that_reappears_is_active_again(company: Company) -> None:
    ingest_jobs(company.id, [posting(company, 0)])
    Job.objects.filter(id="greenhouse:acme:0").update(is_active=False)
    result = ingest_jobs(company.id, [posting(company, 0)])
    assert result.unchanged == 1
    assert Job.objects.get(id="greenhouse:acme:0").is_active


def test_a_changed_posting_that_reappears_is_active_again(company: Company) -> None:
    ingest_jobs(company.id, [posting(company, 0)])
    Job.objects.filter(id="greenhouse:acme:0").update(is_active=False)
    ingest_jobs(company.id, [posting(company, 0, description="changed")])
    assert Job.objects.get(id="greenhouse:acme:0").is_active


def test_a_full_board_fetch_deactivates_what_it_did_not_see(company: Company) -> None:
    other = make_company(slug="other", ats_slug="other")
    ingest_jobs(company.id, [posting(company, n) for n in range(3)])
    ingest_jobs(other.id, [posting(other, 0)])

    result = ingest_jobs(company.id, [posting(company, 0)], deactivate_missing=True)

    assert result.deactivated == 2
    active = set(Job.objects.filter(is_active=True).values_list("id", flat=True))
    # Deactivation is scoped to the company that was fetched.
    assert active == {"greenhouse:acme:0", "greenhouse:other:0"}
    # Kept, never deleted.
    assert Job.objects.count() == 4


def test_without_deactivate_missing_nothing_closes(company: Company) -> None:
    ingest_jobs(company.id, [posting(company, n) for n in range(3)])
    result = ingest_jobs(company.id, [posting(company, 0)])
    assert result.deactivated == 0
    assert Job.objects.filter(is_active=True).count() == 3


def test_aggregator_rows_have_no_company_and_never_deactivate(company: Company) -> None:
    ingest_jobs(company.id, [posting(company, 0)])
    aggregated = dataclasses.replace(posting(company, 1), id="jsearch:acme:1", source="jsearch")
    result = ingest_jobs(None, [aggregated], deactivate_missing=True)
    assert result == IngestResult(inserted=1)
    assert Job.objects.get(id="jsearch:acme:1").company_id is None
    assert Job.objects.get(id="greenhouse:acme:0").is_active


def test_the_empty_board_guard(company: Company) -> None:
    ingest_jobs(company.id, [posting(company, n) for n in range(3)])
    Company.objects.filter(id=company.id).update(last_fetch_job_count=3)

    # One empty answer after a non-empty one closes nothing ...
    first = ingest_jobs(company.id, [], deactivate_missing=True)
    assert first.deactivated == 0
    assert Job.objects.filter(is_active=True).count() == 3

    # ... the refresh then records a count of 0, and a second empty answer closes everything.
    Company.objects.filter(id=company.id).update(last_fetch_job_count=0)
    second = ingest_jobs(company.id, [], deactivate_missing=True)
    assert second.deactivated == 3
    assert Job.objects.filter(is_active=True).count() == 0


def test_an_empty_first_fetch_has_nothing_to_guard(company: Company) -> None:
    assert ingest_jobs(company.id, [], deactivate_missing=True) == IngestResult()


# --- what gets written ---------------------------------------------------------------------------


def test_lone_surrogates_are_replaced_before_storing(company: Company) -> None:
    job = posting(company, 0, title="Engineer \ud800", raw={"note": "bad \udfff"})
    job = dataclasses.replace(job, description_raw="<p>x\ud800</p>", description_text="x\ud800")
    ingest_jobs(company.id, [job])
    stored = Job.objects.get(id=job.id)
    assert stored.title == "Engineer \ufffd"
    assert stored.description_raw == "<p>x\ufffd</p>"
    assert stored.raw == {"note": "bad \ufffd"}


def test_last_seen_moves_forward_on_an_unchanged_fetch(company: Company) -> None:
    ingest_jobs(company.id, [posting(company, 0)])
    long_ago = datetime(2026, 1, 1, tzinfo=UTC)
    Job.objects.filter(id="greenhouse:acme:0").update(last_seen_at=long_ago, fetched_at=long_ago)
    ingest_jobs(company.id, [posting(company, 0)])
    job = Job.objects.get(id="greenhouse:acme:0")
    assert job.last_seen_at > long_ago
    assert job.fetched_at > long_ago
    assert job.last_seen_at <= timezone.now()
