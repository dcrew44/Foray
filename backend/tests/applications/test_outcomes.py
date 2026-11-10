"""The outcome numbers (#65, #6): the spans of posted-to-applied time, the interview rate by how
soon an application went out, the funnel and its splits. Constructed data on both sides of each
threshold: 49 and 50 applications, the minimum denominator, the bucket boundaries, a posting
with no date; and another user's rows are never counted."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.applications import outcomes
from apps.applications.outcomes import (
    MIN_DENOMINATOR,
    P90_MIN,
    RATES_AFTER,
    STEPS,
    OutcomeGroup,
    reached,
)
from apps.jobs.models import Company
from apps.matching.models import JobMatch
from tests.factories import (
    make_application,
    make_company,
    make_job,
    make_match,
    make_resume,
    make_user,
)
from tests.pages.conftest import get_page, signed_in

pytestmark = pytest.mark.django_db

ZERO = timedelta(0)
M = timedelta(minutes=1)
H = timedelta(hours=1)
D = timedelta(days=1)


def send(
    user: User,
    *,
    posted: timedelta | None = 2 * H,
    ready: timedelta | None = H,
    sent: timedelta = H,
    ago: timedelta = ZERO,
    stage: str = "applied",
    by_hand: bool = False,
    company: Company | None = None,
    **fields: Any,
) -> JobMatch:
    """An application that went out `ago` ago. Its posting had been up `posted` when Foray saw
    it (None: the posting gives no date), the application was ready `ready` after that (None:
    it never was) and sent `sent` after it was ready. `by_hand` leaves no application row: the
    user moved the match themselves, and only the match's own clock says when."""
    at = timezone.now() - ago
    seen = at - sent - (ready or ZERO)
    job = make_job(
        company=company,
        first_seen_at=seen,
        posted_at=None if posted is None else seen - posted,
    )
    match = make_match(user, job=job, stage=stage, stage_changed_at=at, **fields)
    if not by_hand:
        make_application(
            user,
            match,
            status="submitted",
            prepared_at=seen,
            ready_at=None if ready is None else seen + ready,
            submitted_at=at,
        )
    return match


def seconds(value: timedelta) -> int:
    return round(value.total_seconds())


def span(found: Any) -> tuple[int, int | None, int | None]:
    return (found.applications, found.median_s, found.p90_s)


# --- what the present proves ---------------------------------------------------------------------


def test_reached_reads_only_what_the_present_proves() -> None:
    at = {stage: reached(stage, False) for stage in (*STEPS, "rejected", "archived")}
    assert at == {
        "found": 0,
        "shortlisted": 1,
        "applied": 2,
        "interviewing": 3,
        "offer": 4,
        # A closed match says nothing about how far it got.
        "rejected": 0,
        "archived": 0,
    }
    # A recorded application proves "applied" wherever the match was put afterwards, and never
    # an interview.
    assert [reached(stage, True) for stage in ("rejected", "archived", "found", "shortlisted")] == [
        2,
        2,
        2,
        2,
    ]
    assert [reached(stage, True) for stage in ("applied", "interviewing", "offer")] == [2, 3, 4]


# --- timing --------------------------------------------------------------------------------------


def test_timing_is_the_median_of_each_span() -> None:
    user = make_user()
    send(user, posted=1 * H, ready=10 * M, sent=5 * M)
    send(user, posted=2 * H, ready=20 * M, sent=30 * M)
    send(user, posted=6 * H, ready=60 * M, sent=2 * H, stage="interviewing")
    found = outcomes.timing(user.pk)
    everything = found.all_time
    assert (everything.applications, everything.by_hand, found.untimed) == (3, 0, 0)
    # Under ten applications there is no 90th percentile to give.
    assert span(everything.posted_to_seen) == (3, seconds(2 * H), None)
    assert span(everything.seen_to_ready) == (3, seconds(20 * M), None)
    assert span(everything.ready_to_applied) == (3, seconds(30 * M), None)
    # The whole is each application's own three spans, end to end.
    assert span(everything.posted_to_applied) == (3, seconds(2 * H + 20 * M + 30 * M), None)
    assert found.days == 30
    assert found.recent == everything  # all three went out just now


def test_timing_with_no_applications_says_nothing() -> None:
    user = make_user()
    make_match(user)  # found, never applied to
    make_application(user, make_match(user, stage="shortlisted"), status="ready")
    found = outcomes.timing(user.pk)
    for window in (found.all_time, found.recent):
        assert (window.applications, window.by_hand) == (0, 0)
        for name in ("posted_to_seen", "seen_to_ready", "ready_to_applied", "posted_to_applied"):
            assert span(getattr(window, name)) == (0, None, None)
    assert found.untimed == 0


def test_the_ninetieth_percentile_needs_ten() -> None:
    user = make_user()
    for hours in range(1, P90_MIN):
        send(user, posted=hours * H)
    nine = outcomes.timing(user.pk).all_time.posted_to_seen
    assert span(nine) == (P90_MIN - 1, seconds(5 * H), None)

    send(user, posted=P90_MIN * H)
    ten = outcomes.timing(user.pk).all_time.posted_to_seen
    # One to ten hours: the median sits between the fifth and sixth, the 90th percentile a
    # tenth of the way from the ninth to the tenth.
    assert span(ten) == (P90_MIN, seconds(5.5 * H), seconds(9.1 * H))


def test_a_posting_with_no_date_has_no_posted_spans() -> None:
    user = make_user()
    send(user, posted=None, ready=10 * M, sent=5 * M)
    send(user, posted=3 * H, ready=30 * M, sent=15 * M)
    found = outcomes.timing(user.pk).all_time
    assert found.applications == 2
    assert span(found.posted_to_seen) == (1, seconds(3 * H), None)
    assert span(found.posted_to_applied) == (1, seconds(3 * H + 45 * M), None)
    # What does not need the posting's date still counts both.
    assert span(found.seen_to_ready) == (2, seconds(20 * M), None)
    assert span(found.ready_to_applied) == (2, seconds(10 * M), None)
    # And it is in no bucket: nothing says how soon after posting it went out.
    assert outcomes.by_speed(user.pk).applications == 1


def test_an_application_sent_before_it_was_ready_has_no_ready_spans() -> None:
    user = make_user()
    send(user, posted=2 * H, ready=None, sent=40 * M)  # marked sent while a question waited
    found = outcomes.timing(user.pk).all_time
    assert (found.applications, found.by_hand) == (1, 0)
    assert span(found.seen_to_ready) == (0, None, None)
    assert span(found.ready_to_applied) == (0, None, None)
    assert span(found.posted_to_applied) == (1, seconds(2 * H + 40 * M), None)


def test_a_span_is_never_negative() -> None:
    """A source that dates a posting after Foray first saw it (a repost, another clock) took
    no time to be seen, not less than none."""
    user = make_user()
    match = send(user, posted=H, ready=H, sent=H)
    job = match.job
    job.posted_at = job.first_seen_at + 30 * M
    job.save(update_fields=["posted_at"])
    found = outcomes.timing(user.pk).all_time
    assert span(found.posted_to_seen) == (1, 0, None)
    assert span(found.posted_to_applied) == (1, seconds(90 * M), None)


def test_the_recent_window_is_the_last_thirty_days() -> None:
    user = make_user()
    send(user, posted=1 * H, ago=29 * D)
    send(user, posted=9 * H, ago=31 * D)
    found = outcomes.timing(user.pk)
    assert span(found.all_time.posted_to_seen) == (2, seconds(5 * H), None)
    assert found.recent.applications == 1
    assert span(found.recent.posted_to_seen) == (1, seconds(1 * H), None)


def test_an_application_made_by_hand_has_only_its_posted_to_applied_time() -> None:
    user = make_user()
    # Moved to Applied on the board: the match's clock is when it was sent.
    send(user, posted=2 * H, ready=None, sent=3 * H, by_hand=True)
    found = outcomes.timing(user.pk)
    assert (found.all_time.applications, found.all_time.by_hand, found.untimed) == (1, 1, 0)
    assert span(found.all_time.posted_to_seen) == (1, seconds(2 * H), None)
    assert span(found.all_time.posted_to_applied) == (1, seconds(5 * H), None)
    assert span(found.all_time.seen_to_ready) == (0, None, None)
    assert span(found.all_time.ready_to_applied) == (0, None, None)

    # An application prepared for it and never marked sent lends it no ready time either.
    prepared = send(user, posted=2 * H, ready=None, sent=3 * H, by_hand=True)
    make_application(user, prepared, status="ready", ready_at=timezone.now() - D)
    found = outcomes.timing(user.pk)
    assert (found.all_time.applications, found.all_time.by_hand) == (2, 2)
    assert span(found.all_time.seen_to_ready) == (0, None, None)

    # Once one moves on, the time it was sent is gone: it is counted, and not timed.
    send(user, posted=2 * H, sent=3 * H, by_hand=True, stage="interviewing")
    send(user, posted=2 * H, sent=3 * H, by_hand=True, stage="offer")
    found = outcomes.timing(user.pk)
    assert (found.all_time.applications, found.all_time.by_hand, found.untimed) == (2, 2, 2)
    assert span(found.all_time.posted_to_applied) == (2, seconds(5 * H), None)
    # None of the four is in a bucket.
    assert outcomes.by_speed(user.pk).applications == 0


def test_a_recorded_application_keeps_its_times_when_the_match_moves_on() -> None:
    user = make_user()
    for stage in ("interviewing", "offer", "rejected", "archived", "shortlisted"):
        send(user, posted=2 * H, ready=H, sent=H, stage=stage)
    found = outcomes.timing(user.pk)
    assert (found.all_time.applications, found.all_time.by_hand, found.untimed) == (5, 0, 0)
    assert span(found.all_time.posted_to_applied) == (5, seconds(4 * H), None)


# --- interview rate by how soon it went out ------------------------------------------------------


def buckets(user: User) -> dict[str, tuple[int, int, float | None]]:
    return {
        item.bucket: (item.applications, item.interviews, item.rate)
        for item in outcomes.by_speed(user.pk).buckets
    }


def test_bucket_boundaries() -> None:
    """The same day is under 24 hours from posting, within three days is under 72."""
    user = make_user()
    second = timedelta(seconds=1)
    for whole in (D - second, D, 3 * D - second, 3 * D, 40 * D):
        # Seen as it was posted and ready at once, so `sent` is the whole posted-to-applied time.
        send(user, posted=ZERO, ready=ZERO, sent=whole)
    assert buckets(user) == {
        "same_day": (1, 0, None),
        "three_days": (2, 0, None),
        "later": (2, 0, None),
    }


def test_the_buckets_are_always_the_three_in_order() -> None:
    found = outcomes.by_speed(make_user().pk)
    assert [item.bucket for item in found.buckets] == ["same_day", "three_days", "later"]
    assert (found.applications, found.needed, found.enough) == (0, RATES_AFTER, False)


def test_rates_by_speed_need_fifty_recorded_applications() -> None:
    user = make_user()

    def batch(count: int, interviews: int, whole: timedelta) -> None:
        for index in range(count):
            stage = "interviewing" if index < interviews else "applied"
            send(user, posted=ZERO, ready=ZERO, sent=whole, stage=stage)

    batch(30, 6, 2 * H)
    batch(15, 3, 2 * D)
    batch(4, 2, 9 * D)
    # Applications made by hand are not in the buckets and do not count towards the fifty.
    for _ in range(5):
        send(user, posted=ZERO, ready=None, sent=2 * H, by_hand=True)

    found = outcomes.by_speed(user.pk)
    assert (found.applications, found.needed, found.enough) == (RATES_AFTER - 1, 50, False)
    # Under fifty: the counts, and no rate anywhere.
    assert buckets(user) == {
        "same_day": (30, 6, None),
        "three_days": (15, 3, None),
        "later": (4, 2, None),
    }

    send(user, posted=ZERO, ready=ZERO, sent=2 * H, stage="offer")
    found = outcomes.by_speed(user.pk)
    assert (found.applications, found.enough) == (RATES_AFTER, True)
    assert buckets(user) == {
        "same_day": (31, 7, round(7 / 31, 4)),  # an offer was an interview first
        "three_days": (15, 3, 0.2),
        "later": (4, 2, None),  # still too few in this bucket to say
    }


def test_an_interview_that_ended_in_a_rejection_is_not_counted() -> None:
    """The floor the page admits to: only the current stage is kept."""
    user = make_user()
    send(user, posted=ZERO, ready=ZERO, sent=H, stage="rejected")
    send(user, posted=ZERO, ready=ZERO, sent=H, stage="interviewing")
    assert buckets(user)["same_day"] == (2, 1, None)


# --- the funnel ----------------------------------------------------------------------------------


def steps(user: User) -> dict[str, tuple[int, float | None]]:
    return {step.stage: (step.matches, step.rate) for step in outcomes.funnel(user.pk).steps}


def test_the_funnel_counts_a_match_at_every_step_it_is_known_to_have_reached() -> None:
    user = make_user()
    for stage, count in (("found", 3), ("shortlisted", 2), ("rejected", 2), ("archived", 4)):
        for _ in range(count):
            make_match(user, stage=stage)
    send(user, by_hand=True)  # applied, by hand
    send(user)  # applied, recorded
    send(user, by_hand=True, stage="interviewing")
    send(user, stage="offer")
    send(user, stage="rejected")  # recorded, then rejected: it was applied to
    send(user, stage="archived")
    send(user, stage="shortlisted")  # recorded, then dragged back: it was still sent

    found = outcomes.funnel(user.pk)
    assert [step.stage for step in found.steps] == list(STEPS)
    assert [step.matches for step in found.steps] == [
        18,  # every match
        9,  # 2 shortlisted and the 7 applied to
        7,  # 2 applied, 1 interviewing, 1 offer, and 3 recorded ones now elsewhere
        2,  # interviewing and offer
        1,
    ]
    # Rejected and archived are outcomes, apart from the steps; the plain ones count only as
    # found, since nothing says how far they got.
    assert [(item.stage, item.matches, item.applied) for item in found.closed] == [
        ("rejected", 3, 1),
        ("archived", 5, 1),
    ]
    assert found.min_denominator == MIN_DENOMINATOR


def test_a_conversion_needs_the_minimum_denominator() -> None:
    user = make_user()
    for index in range(MIN_DENOMINATOR - 1):
        send(user, stage="interviewing" if index < 3 else "applied")
    nine = steps(user)
    assert nine["applied"] == (9, None)  # of 9 shortlisted-or-further
    assert nine["interviewing"] == (3, None)  # 3 of 9: counts, no rate
    assert nine["offer"] == (0, None)

    send(user)
    ten = steps(user)
    assert ten["found"] == (10, None)  # the first step converts from nothing
    assert ten["shortlisted"] == (10, 1.0)
    assert ten["applied"] == (10, 1.0)
    assert ten["interviewing"] == (3, 0.3)
    assert ten["offer"] == (0, None)  # of 3 interviews: too few to say


def test_a_new_users_funnel_is_all_zeros() -> None:
    found = outcomes.funnel(make_user().pk)
    assert [(step.matches, step.rate) for step in found.steps] == [(0, None)] * 5
    assert [(item.matches, item.applied) for item in found.closed] == [(0, 0), (0, 0)]
    assert found.by_source == []
    assert [group.key for group in found.by_recommendation] == [
        "strong",
        "apply",
        "maybe",
        "skip",
        "none",
    ]
    assert [group.key for group in found.by_lane] == ["coached", "automated", "none"]
    assert [group.key for group in found.by_resume] == ["tailored", "untailored"]
    assert all(
        (group.applications, group.interviews, group.rate) == (0, 0, None)
        for group in (*found.by_recommendation, *found.by_lane, *found.by_resume)
    )


# --- the splits ----------------------------------------------------------------------------------


def split(groups: list[OutcomeGroup]) -> dict[str, tuple[int, int, float | None]]:
    return {group.key: (group.applications, group.interviews, group.rate) for group in groups}


def test_interview_rate_by_source() -> None:
    user = make_user()
    greenhouse = make_company()
    ashby = make_company(source_type="ashby")
    for index in range(MIN_DENOMINATOR):
        send(user, company=greenhouse, stage="interviewing" if index < 4 else "applied")
    for index in range(MIN_DENOMINATOR - 1):
        send(user, company=ashby, stage="offer" if index < 2 else "applied")
    make_match(user, job=make_job(company=make_company(source_type="lever")))  # never applied to
    found = outcomes.funnel(user.pk)
    # Most applications first; a source nobody applied through is not listed.
    assert [group.key for group in found.by_source] == ["greenhouse", "ashby"]
    assert split(found.by_source) == {
        "greenhouse": (10, 4, 0.4),
        "ashby": (9, 2, None),  # one short of the minimum: the counts, not a rate
    }


def test_interview_rate_by_recommendation() -> None:
    user = make_user()
    for score, interviews in ((90, 5), (70, 2)):  # strong, apply
        for index in range(MIN_DENOMINATOR):
            stage = "interviewing" if index < interviews else "applied"
            send(user, stage=stage, deep_score=score)
    send(user, deep_score=50)  # maybe
    send(user, deep_score=20, stage="interviewing")  # skip, and it got an interview
    send(user, fast_score=80)  # triage only
    send(user, deep_score=60, deep_eval={"recommendation": "unreadable"})
    make_match(user, deep_score=95)  # strong, never applied to
    assert split(outcomes.funnel(user.pk).by_recommendation) == {
        "strong": (10, 5, 0.5),
        "apply": (10, 2, 0.2),
        "maybe": (1, 0, None),
        "skip": (1, 1, None),
        "none": (2, 0, None),  # no review, or one whose verdict does not read
    }


def test_interview_rate_by_lane_follows_the_users_override() -> None:
    user = make_user()
    for index in range(MIN_DENOMINATOR):  # strong: the review's lane is coached
        send(user, deep_score=90, stage="interviewing" if index < 3 else "applied")
    send(user, deep_score=70)  # apply: automated
    send(user, deep_score=90, lane_override="automated", stage="offer")  # the user's own choice
    send(user, deep_score=90, lane_override="none")
    send(user, fast_score=80)  # not reviewed: no lane
    assert split(outcomes.funnel(user.pk).by_lane) == {
        "coached": (10, 3, 0.3),
        "automated": (2, 1, None),
        "none": (2, 0, None),
    }


def test_interview_rate_by_resume() -> None:
    user = make_user()
    for index in range(MIN_DENOMINATOR + 1):
        match = send(user, stage="interviewing" if index < 6 else "applied")
        match.tailored_resume = make_resume(user, kind="tailored", match=match)
        match.save(update_fields=["tailored_resume"])
    for index in range(MIN_DENOMINATOR - 1):
        send(user, stage="interviewing" if index < 1 else "applied")
    assert split(outcomes.funnel(user.pk).by_resume) == {
        "tailored": (11, 6, round(6 / 11, 4)),
        "untailored": (9, 1, None),
    }


# --- one definition of an application ------------------------------------------------------------


def test_the_three_reports_count_the_same_applications(django_assert_num_queries: Any) -> None:
    """The spans and the buckets read applications in SQL, the funnel through `reached()`:
    they agree on which matches were applied to, and each is one query."""
    user = make_user()
    for stage in ("found", "shortlisted", "rejected", "archived"):
        make_match(user, stage=stage)
        make_application(user, make_match(user, stage=stage), status="ready")  # never sent
    for stage in ("applied", "interviewing", "offer"):
        send(user, stage=stage, by_hand=True)
    for stage in ("found", "shortlisted", "applied", "interviewing", "offer", "rejected"):
        send(user, stage=stage)
    send(user, stage="archived", posted=None)

    with django_assert_num_queries(1):
        timed = outcomes.timing(user.pk)
    with django_assert_num_queries(1):
        speed = outcomes.by_speed(user.pk)
    with django_assert_num_queries(1):
        found = outcomes.funnel(user.pk)

    applied = found.steps[STEPS.index("applied")].matches
    assert applied == 3 + 7
    # Every application is either timed or one made by hand that has moved on.
    assert (timed.all_time.applications, timed.all_time.by_hand, timed.untimed) == (8, 1, 2)
    assert timed.all_time.applications + timed.untimed == applied
    # The buckets hold the recorded ones whose posting says when it went up.
    assert speed.applications == 6
    for groups in (found.by_source, found.by_recommendation, found.by_lane, found.by_resume):
        assert sum(group.applications for group in groups) == applied


# --- whose rows ----------------------------------------------------------------------------------


def test_another_users_rows_are_never_counted() -> None:
    mine, theirs = make_user(), make_user()
    send(mine, posted=H, ready=H, sent=H)
    make_match(mine, stage="shortlisted")
    before = (outcomes.timing(mine.pk), outcomes.by_speed(mine.pk), outcomes.funnel(mine.pk))

    for index in range(RATES_AFTER + MIN_DENOMINATOR):
        stage = "offer" if index % 2 else "applied"
        send(theirs, posted=9 * H, ready=9 * H, sent=9 * H, stage=stage)
    send(theirs, by_hand=True, stage="interviewing")
    make_match(theirs, stage="rejected")

    assert (
        outcomes.timing(mine.pk),
        outcomes.by_speed(mine.pk),
        outcomes.funnel(mine.pk),
    ) == before
    assert outcomes.by_speed(theirs.pk).enough
    assert outcomes.timing(mine.pk).all_time.applications == 1
    assert steps(mine)["applied"] == (1, None)


def test_an_application_filed_under_another_user_does_not_make_a_match_sent() -> None:
    """The join is to the match's own user's application: a row that says otherwise (nothing
    writes one) changes nobody's numbers."""
    mine, theirs = make_user(), make_user()
    match = make_match(mine, stage="shortlisted")
    make_application(theirs, match, status="submitted", submitted_at=timezone.now())
    assert steps(mine)["applied"] == (0, None)
    assert outcomes.timing(mine.pk).all_time.applications == 0
    assert outcomes.timing(theirs.pk).all_time.applications == 0


# --- the page ------------------------------------------------------------------------------------


def test_outcomes_page_reports_all_three_parts(django_assert_max_num_queries: Any) -> None:
    user = make_user()
    make_match(user)
    make_match(user, stage="shortlisted")
    send(user, posted=H, ready=20 * M, sent=10 * M)
    send(user, posted=3 * H, ready=40 * M, sent=30 * M, stage="interviewing")
    client = signed_in(user)
    with django_assert_max_num_queries(3 + 2):  # its budget, and the session and user rows
        response = get_page(client, "/api/pages/outcomes")
    assert response.status_code == 200, response.content
    body = response.json()
    assert (body["matches"], body["applications"]) == (4, 2)
    timing = body["timing"]
    assert (timing["days"], timing["untimed"]) == (30, 0)
    assert timing["allTime"]["postedToApplied"] == {
        "applications": 2,
        "medianS": seconds((H + 30 * M + 3 * H + 70 * M) / 2),
        "p90S": None,
    }
    assert timing["allTime"]["postedToSeen"]["medianS"] == seconds(2 * H)
    assert timing["allTime"]["seenToReady"]["medianS"] == seconds(30 * M)
    assert timing["allTime"]["readyToApplied"]["medianS"] == seconds(20 * M)
    assert body["speed"] == {
        "applications": 2,
        "needed": 50,
        "enough": False,
        "buckets": [
            {"bucket": "same_day", "applications": 2, "interviews": 1, "rate": None},
            {"bucket": "three_days", "applications": 0, "interviews": 0, "rate": None},
            {"bucket": "later", "applications": 0, "interviews": 0, "rate": None},
        ],
    }
    assert [(step["stage"], step["matches"]) for step in body["funnel"]["steps"]] == [
        ("found", 4),
        ("shortlisted", 3),
        ("applied", 2),
        ("interviewing", 1),
        ("offer", 0),
    ]
    assert body["funnel"]["bySource"] == [
        {"key": "greenhouse", "applications": 2, "interviews": 1, "rate": None}
    ]
    assert body["funnel"]["minDenominator"] == 10


def test_outcomes_page_for_someone_with_nothing_yet() -> None:
    body = get_page(signed_in(make_user()), "/api/pages/outcomes").json()
    assert (body["matches"], body["applications"]) == (0, 0)
    assert body["timing"]["allTime"]["postedToApplied"] == {
        "applications": 0,
        "medianS": None,
        "p90S": None,
    }
    assert body["speed"]["applications"] == 0
    assert [step["matches"] for step in body["funnel"]["steps"]] == [0, 0, 0, 0, 0]
