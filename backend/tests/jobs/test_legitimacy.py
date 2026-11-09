"""Legitimacy signals (#7): each rule on both sides of its threshold, where an apply link leads,
the one query behind the repost count, and the order `signals` answers in."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, get_args
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from apps.jobs.legitimacy import (
    LONG_OPEN_DAYS,
    REPOST_MIN,
    REPOST_WINDOW_DAYS,
    Context,
    Signal,
    SignalKind,
    aggregator_link,
    closed,
    is_board_link,
    is_employer_host,
    long_open,
    read_context,
    reposted,
    signals,
    stated_domain,
)
from apps.jobs.models import Job
from apps.jobs.sources.types import BoardSource
from tests.factories import make_company, make_job

NOW = datetime(2026, 11, 9, 18, 0, tzinfo=UTC)


def test_the_thresholds_are_the_ones_decided() -> None:
    assert (REPOST_MIN, REPOST_WINDOW_DAYS, LONG_OPEN_DAYS) == (3, 180, 60)


# --- closed --------------------------------------------------------------------------------------


def test_an_open_posting_is_not_closed() -> None:
    assert closed(is_active=True, last_seen_at=NOW, now=NOW, zone=UTC) is None


def test_a_closed_posting_says_when_it_was_last_seen() -> None:
    seen = datetime(2026, 11, 3, 9, 30, tzinfo=UTC)
    assert closed(is_active=False, last_seen_at=seen, now=NOW, zone=UTC) == Signal(
        "closed", "Closed", "Last seen on the employer's board on November 3."
    )


def test_a_closed_posting_names_the_year_when_it_is_not_this_one() -> None:
    seen = datetime(2025, 12, 28, 9, 30, tzinfo=UTC)
    found = closed(is_active=False, last_seen_at=seen, now=NOW, zone=UTC)
    assert found is not None
    assert found.detail == "Last seen on the employer's board on December 28, 2025."


def test_a_closed_posting_is_dated_in_the_readers_zone() -> None:
    seen = datetime(2026, 11, 3, 2, 0, tzinfo=UTC)  # still the 2nd in California
    found = closed(
        is_active=False, last_seen_at=seen, now=NOW, zone=ZoneInfo("America/Los_Angeles")
    )
    assert found is not None
    assert found.detail == "Last seen on the employer's board on November 2."


# --- long open -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("age", "detail"),
    [
        (timedelta(days=2), None),
        (timedelta(days=60), None),
        (timedelta(days=60, hours=23), None),  # the 61st day is not over
        (timedelta(days=61), "Open for 61 days."),
        (timedelta(days=74, hours=5), "Open for 74 days."),
        (timedelta(days=1204), "Open for 1,204 days."),
        (timedelta(days=-3), None),  # a source's date in the future
    ],
)
def test_long_open_starts_after_sixty_days(age: timedelta, detail: str | None) -> None:
    found = long_open(is_active=True, posted_at=NOW - age, first_seen_at=NOW, now=NOW)
    assert (found.detail if found else None) == detail
    if found:
        assert (found.kind, found.label) == ("long_open", "Open a long time")


def test_long_open_counts_from_first_seen_without_a_posting_date() -> None:
    def seen(days: int) -> Signal | None:
        first = NOW - timedelta(days=days)
        return long_open(is_active=True, posted_at=None, first_seen_at=first, now=NOW)

    assert seen(60) is None
    found = seen(61)
    assert found is not None
    assert found.detail == "Open for 61 days."


def test_long_open_counts_from_the_posting_date_when_there_is_one() -> None:
    # Seen for a week, posted three months ago: the source's date is the one that counts.
    found = long_open(
        is_active=True,
        posted_at=NOW - timedelta(days=90),
        first_seen_at=NOW - timedelta(days=7),
        now=NOW,
    )
    assert found is not None
    assert found.detail == "Open for 90 days."


def test_a_closed_posting_is_not_long_open() -> None:
    old = NOW - timedelta(days=200)
    assert long_open(is_active=False, posted_at=old, first_seen_at=old, now=NOW) is None


# --- reposted ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("postings", "detail"),
    [
        (0, None),
        (1, None),
        (2, None),
        (3, "Posted 3 times since May."),
        (4, "Posted 4 times since May."),
    ],
)
def test_reposted_starts_at_the_third_posting(postings: int, detail: str | None) -> None:
    since = datetime(2026, 5, 20, tzinfo=UTC)
    found = reposted(postings, since, now=NOW, zone=UTC)
    assert (found.detail if found else None) == detail
    if found:
        assert (found.kind, found.label) == ("reposted", "Reposted")


def test_reposted_names_the_year_when_it_is_not_this_one() -> None:
    now = datetime(2027, 1, 12, tzinfo=UTC)
    found = reposted(3, datetime(2026, 9, 2, tzinfo=UTC), now=now, zone=UTC)
    assert found is not None
    assert found.detail == "Posted 3 times since September 2026."


def test_reposted_needs_a_date() -> None:
    assert reposted(5, None, now=NOW, zone=UTC) is None


# --- where an apply link leads -------------------------------------------------------------------

# One job link at each board Foray reads, as an aggregator gives them. A career site on the
# employer's own domain (`jibe`) has no host of its own to know.
BOARD_LINKS: dict[str, str] = {
    "greenhouse": "https://job-boards.greenhouse.io/acme/jobs/4012345",
    "lever": "https://jobs.lever.co/acme/2f1c5d0e-7c1b-4b8e-9d55-0a1b2c3d4e5f/apply",
    "ashby": "https://jobs.ashbyhq.com/acme/8a0e7b3c-1d2f-4a5b-8c9d-0e1f2a3b4c5d",
    "workable": "https://apply.workable.com/acme/j/1A2B3C4D5E/",
    "recruitee": "https://acme.recruitee.com/o/software-engineer",
    "gem": "https://jobs.gem.com/acme/am9icG9zdDo0MjQy",
    "workday": "https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Austin-TX/Engineer_R-1042",
    "smartrecruiters": "https://jobs.smartrecruiters.com/Acme/744000012345678-software-engineer",
    "icims": "https://careers-acme.icims.com/jobs/1042/software-engineer/job",
}


def test_every_board_foray_reads_has_a_link_here() -> None:
    """A new adapter needs a link above, and its pattern in `legitimacy._BOARD_PATTERNS` when
    `discovery.ATS_PATTERNS` does not already carry it."""
    assert set(BOARD_LINKS) == set(get_args(BoardSource.__value__)) - {"jibe"}


@pytest.mark.parametrize("source", BOARD_LINKS)
def test_a_link_to_a_board_foray_reads_is_known(source: str) -> None:
    assert is_board_link(BOARD_LINKS[source])


@pytest.mark.parametrize(
    "url",
    [
        "https://boards.greenhouse.io/embed/job_app?for=acme&token=4012345",
        "https://apply.workable.com/j/1A2B3C4D5E",  # names no account
        "https://jobs.smartrecruiters.com/oneclick-ui/company/Acme/publication/abc",
        "https://wd5.myworkdaysite.com/en-US/recruiting/acme/Careers/job/Austin/Engineer_R-1042",
        "https://acme.taleo.net/careersection/2/jobdetail.ftl?job=1042",  # recognised, not read
        "https://career4.successfactors.com/career?company=acme",
        "https://acme.bamboohr.com/careers/42",
        "https://ats.rippling.com/acme/jobs/42",
        "HTTPS://JOBS.LEVER.CO/acme/42",
    ],
)
def test_other_links_to_known_boards(url: str) -> None:
    assert is_board_link(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/jobs/view/software-engineer-at-acme-4468875017",
        "https://unlockthegenius.netlify.app/?jobs=en-job-entry-level-software-developer",
        "https://example-jobs.com/apply?next=https://jobs.lever.co/acme/42",  # in the query
        "https://example-jobs.com/jobs.lever.co/acme/42",  # in the path
        "https://jobs.lever.co.example-jobs.com/acme/42",  # in a subdomain
        "https://acme.workable.com.example-jobs.com/j/42",
        "https://acme.icims.com.example-jobs.com/jobs/42",
        "https://nottaleo.net/careersection/2",  # ends like a board's domain, is not one
        "https://taleo.net.example-jobs.com/careersection/2",
        "mailto:jobs@acme.com",
        "not a link",
        "",
    ],
)
def test_links_that_are_not_to_a_known_board(url: str) -> None:
    assert not is_board_link(url)


@pytest.mark.parametrize(
    ("host", "employer", "domain", "own"),
    [
        ("stripe.com", "Stripe", "stripe.com", True),
        ("www.stripe.com", "Stripe", "stripe.com", True),
        ("jobs.stripe.com", "Stripe, Inc.", "https://www.stripe.com/", True),  # as JSearch says it
        ("stripe.com.example-jobs.com", "Stripe", "stripe.com", False),
        ("notstripe.com", "Stripe", "stripe.com", False),
        # Nothing says what the domain is: a domain named exactly for the employer is its own.
        ("careers.acme.com", "Acme", None, True),
        ("careers.acme.com", "Acme Corp.", None, True),
        ("www.jackandjill.ai", "Jack & Jill", None, True),
        ("jobs.acme-robotics.com", "Acme Robotics", None, True),
        ("amazon.jobs", "Amazon", "amazon.com", True),  # another domain, the same name
        ("acme-careers.com", "Acme", None, False),  # anyone can register a longer name
        ("acme.example-jobs.com", "Acme", None, False),
        ("www.linkedin.com", "Acme", None, False),
        ("www.linkedin.com", "LinkedIn", None, True),
        ("example-jobs.com", "", None, False),
    ],
)
def test_whose_host_it_is(host: str, employer: str, domain: str | None, own: bool) -> None:
    assert is_employer_host(host, employer=employer, domain=domain) is own


def test_the_domain_an_aggregator_states() -> None:
    assert stated_domain({"employer_website": "https://www.stripe.com"}) == "stripe.com"
    assert stated_domain({"employer_website": "http://www.technovasolutions.com"}) == (
        "technovasolutions.com"
    )
    assert stated_domain({"employer_website": None}) is None
    assert stated_domain({"employer_website": 7}) is None
    assert stated_domain({"id": "4012345"}) is None  # a board's payload
    assert stated_domain(None) is None


def link(
    apply_url: str | None,
    *,
    source: str = "jsearch",
    employer: str = "Acme",
    domain: str | None = "acme.com",
) -> Signal | None:
    return aggregator_link(source=source, apply_url=apply_url, employer=employer, domain=domain)


def test_an_aggregator_link_to_the_employers_own_domain_is_fine() -> None:
    assert link("https://acme.com/careers/42") is None
    assert link("https://careers.acme.com/jobs/42?utm_source=google_jobs_apply") is None


def test_an_aggregator_link_to_a_known_board_is_fine() -> None:
    assert link("https://boards.greenhouse.io/acme/jobs/4012345") is None
    assert link("https://acme.taleo.net/careersection/2/jobdetail.ftl?job=1042") is None


def test_an_aggregator_link_to_an_unknown_host_names_it() -> None:
    assert link("https://www.example-jobs.com/apply/42") == Signal(
        "aggregator_link",
        "Apply link goes elsewhere",
        "The apply link goes to example-jobs.com, not the employer's site.",
    )
    found = link("https://www.linkedin.com/jobs/view/software-engineer-at-acme-4468875017")
    assert found is not None
    assert found.detail == "The apply link goes to linkedin.com, not the employer's site."
    # The employer's name in a host that is someone else's.
    found = link("https://acme.com.example-jobs.com/apply/42")
    assert found is not None
    assert found.detail == (
        "The apply link goes to acme.com.example-jobs.com, not the employer's site."
    )


def test_an_unknown_host_is_flagged_without_a_domain_too() -> None:
    found = link("https://unlockthegenius.netlify.app/?jobs=en", employer="TechNova", domain=None)
    assert found is not None
    assert found.detail == (
        "The apply link goes to unlockthegenius.netlify.app, not the employer's site."
    )


def test_only_an_aggregators_posting_is_judged_by_its_link() -> None:
    # A board's own posting applies wherever the board says.
    assert link("https://www.example-jobs.com/apply/42", source="greenhouse") is None
    assert link("https://www.example-jobs.com/apply/42", source="web") is None


def test_a_posting_without_an_apply_link_says_nothing() -> None:
    assert link(None) is None
    assert link("") is None
    assert link("mailto:jobs@example-jobs.com") is None


# --- the query -----------------------------------------------------------------------------------


def repost(first: Job, **fields: Any) -> Job:
    """An earlier posting of `first`'s role that has closed: the same company, title and place,
    another id."""
    assert first.company is not None
    fields.setdefault("is_active", False)
    return make_job(company=first.company, title=first.title, **fields)


@pytest.mark.django_db
def test_context_counts_the_roles_postings() -> None:
    now = timezone.now()
    first = make_job(title="Field Engineer", posted_at=now - timedelta(days=100))
    context = read_context(first)
    assert (context.postings, context.since) == (1, first.posted_at)
    assert abs(context.now - now) < timedelta(minutes=1)  # the database's present

    second = repost(first)
    assert second.dedupe_key == first.dedupe_key
    context = read_context(first)
    assert context.postings == 2
    # Twice is not "again and again".
    assert [signal.kind for signal in signals(first, context)] == ["long_open"]

    repost(first, posted_at=None, first_seen_at=now - timedelta(days=120))
    context = read_context(first)
    assert context.postings == 3
    # The earliest: the undated one, by when it was first seen.
    assert context.since == now - timedelta(days=120)
    assert [signal.kind for signal in signals(first, context)] == ["reposted", "long_open"]


@pytest.mark.django_db
def test_context_leaves_out_what_is_not_a_repost() -> None:
    now = timezone.now()
    first = make_job(title="Field Engineer")
    repost(first)
    assert read_context(first).postings == 2

    # Before the window.
    repost(first, first_seen_at=now - timedelta(days=REPOST_WINDOW_DAYS + 1))
    # Another opening of the same title that is up right now is not a repost.
    repost(first, is_active=True)
    # A different role that shares the loose key (every "Member of Technical Staff (…)" at one
    # company does).
    assert first.company is not None
    make_job(
        company=first.company,
        title="Field Engineer (Robotics)",
        dedupe_key=first.dedupe_key,
        is_active=False,
    )
    # Another role, another place, another company.
    make_job(company=first.company, title="Field Engineer II")
    make_job(company=first.company, title=first.title, locations=["Austin, TX"])
    make_job(title=first.title)
    # An aggregator's copy of the role shares its key, and is a copy.
    make_job(source="jsearch", title=first.title, dedupe_key=first.dedupe_key, is_active=False)
    make_job(source="jsearch", title=first.title, dedupe_key=first.dedupe_key, is_active=False)
    assert read_context(first).postings == 2

    # The same posting under a second id (its company re-registered under another slug).
    again = make_job(
        company=make_company(name="Other slug", source_type=first.company.source_type),
        title=first.title,
        external_id=first.external_id,
        dedupe_key=first.dedupe_key,
    )
    assert again.id != first.id
    assert read_context(first).postings == 2

    # Just inside the window.
    repost(first, first_seen_at=now - timedelta(days=REPOST_WINDOW_DAYS - 1))
    assert read_context(first).postings == 3


@pytest.mark.django_db
def test_context_counts_an_aggregators_postings_among_themselves() -> None:
    board = make_job(title="Field Engineer")
    listed = [
        make_job(source="jsearch", title=board.title, dedupe_key=board.dedupe_key, is_active=n == 0)
        for n in range(3)
    ]
    assert read_context(listed[0]).postings == 3
    assert read_context(board).postings == 1


@pytest.mark.django_db
def test_context_reads_the_companys_domain(django_assert_num_queries: Any) -> None:
    job = make_job(company=make_company(name="Acme", domain="acme.com"))
    assert read_context(job).domain == "acme.com"
    assert read_context(make_job()).domain is None  # a company with no domain on record
    Job.objects.filter(id=job.id).update(company=None)  # as an aggregator's postings are
    orphan = Job.objects.get(id=job.id)
    with django_assert_num_queries(1):
        assert read_context(orphan).domain is None


# --- together ------------------------------------------------------------------------------------


def a_job(**fields: Any) -> Job:
    """An unsaved posting: `signals` reads the row it is given and asks nothing."""
    values: dict[str, Any] = {
        "id": "greenhouse:acme:1",
        "source": "greenhouse",
        "external_id": "1",
        "company_name": "Acme",
        "title": "Field Engineer",
        "url": "https://boards.greenhouse.io/acme/jobs/1",
        "apply_url": None,
        "is_active": True,
        "posted_at": NOW - timedelta(days=2),
        "first_seen_at": NOW - timedelta(days=2),
        "last_seen_at": NOW,
        "raw": {"id": 1},
    }
    return Job(**{**values, **fields})


def context(**fields: Any) -> Context:
    return replace(Context(postings=1, since=NOW, domain=None, now=NOW), **fields)


def test_an_ordinary_posting_has_no_signals() -> None:
    assert signals(a_job(), context()) == []
    # An aggregator's posting that applies at the employer's own site.
    listed = a_job(
        source="jsearch",
        apply_url="https://careers.acme.com/jobs/1",
        raw={"employer_website": "https://www.acme.com"},
    )
    assert signals(listed, context()) == []


def test_signals_come_in_the_order_the_page_shows_them() -> None:
    old = NOW - timedelta(days=74)
    job = a_job(
        source="jsearch",
        apply_url="https://www.example-jobs.com/apply/1",
        raw={"employer_website": "https://www.acme.com"},
        posted_at=old,
        first_seen_at=old,
    )
    found = signals(job, context(postings=4, since=datetime(2026, 5, 2, tzinfo=UTC)))
    assert found == [
        Signal(
            "aggregator_link",
            "Apply link goes elsewhere",
            "The apply link goes to example-jobs.com, not the employer's site.",
        ),
        Signal("reposted", "Reposted", "Posted 4 times since May."),
        Signal("long_open", "Open a long time", "Open for 74 days."),
    ]

    job.is_active = False
    job.last_seen_at = datetime(2026, 11, 3, 2, 0, tzinfo=UTC)
    found = signals(job, context(postings=4, since=old), time_zone="America/Los_Angeles")
    # Closed first, and no longer "open".
    assert [signal.kind for signal in found] == ["closed", "aggregator_link", "reposted"]
    assert found[0].detail == "Last seen on the employer's board on November 2."
    assert found[2].detail == "Posted 4 times since August."


def test_the_registered_companys_domain_comes_before_the_stated_one() -> None:
    job = a_job(
        source="jsearch",
        apply_url="https://jobs.acme.io/1",
        raw={"employer_website": "https://www.acme-holdings.com"},
        company_name="Acme Holdings",
    )
    assert [signal.kind for signal in signals(job, context())] == ["aggregator_link"]
    assert signals(job, context(domain="acme.io")) == []


def test_an_unknown_time_zone_is_read_as_utc() -> None:
    job = a_job(is_active=False, last_seen_at=datetime(2026, 11, 3, 2, 0, tzinfo=UTC))
    for zone in ("Mars/Olympus", "", None):
        (found,) = signals(job, context(), time_zone=zone)
        assert found.detail == "Last seen on the employer's board on November 3."


def test_the_page_sends_the_kinds_in_the_order_they_are_declared() -> None:
    from apps.pages.schemas import JobSignal

    kinds = get_args(JobSignal.model_fields["kind"].annotation)
    assert kinds == get_args(SignalKind) == ("closed", "aggregator_link", "reposted", "long_open")
