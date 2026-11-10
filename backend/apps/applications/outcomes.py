"""What came of the applications (#65, #6): how long each took to go out, and how far it got.

The product is judged by posted-to-applied time (DESIGN.md, Direction), so that is measured
first, in the three spans it is made of, and the claim it rests on (applying early helps) is
tested by the interview rate of applications sent the same day, within three days and later.
Then the funnel, and the interview rate by source, recommendation, lane and resume.

**What counts as an application.** A match is known to have been applied to when either

- its application was marked sent on the Apply page (`applications.status = 'submitted'`, which
  keeps `submitted_at` whatever happens to the match afterwards): a *recorded* application; or
- it is in `applied`, `interviewing` or `offer` now with no such record: one the user moved
  there *by hand*. It has no ready time, so no seen-to-ready or ready-to-applied span, and its
  sent time is `job_matches.stage_changed_at`, which is when it was sent only while the match
  is still in `applied`. Once it moves on, that time is gone: it stays an application, with no
  timing (`untimed`).

**What "reached a stage" means.** `job_matches` keeps where a match is now and nothing of where
it has been, and no event records a move. So `reached()` says only what the present proves: a
match in `offer` was interviewed and applied to, one in `interviewing` was applied to, and a
match in `rejected` or `archived` is known to have got as far as `applied` only when its
application was recorded, and no further. Three things follow, and the page says them:

- every count past `found` is a floor: a closed match is not counted at the steps it passed;
- an interview that ended in a rejection is not counted as an interview, so an interview rate
  can read too low;
- an application made by hand and then closed is not counted as an application at all, so for
  someone who tracks by hand a rate can read too high. Recorded applications do not have this
  problem, which is why the rates by how soon an application went out use only those.

Recording when a match first entered each stage would settle all three; nothing here guesses.

**Small numbers.** A rate is given only on `MIN_DENOMINATOR` or more (under it the caller gets
the counts and `None`), a 90th percentile only on `P90_MIN` or more, and the rates by how soon
an application went out only once `RATES_AFTER` applications are in the buckets (#65).

"Posted" is the posting's own `posted_at`; a posting without one has no posted-to-seen and no
posted-to-applied time, and is in no bucket. "Tailored" is a match that has a tailored resume,
whenever it was written.

Sync; one query each. The spans and the buckets are raw SQL over `_SENT` (percentiles, as
`jobs/radar.py` reads its lag), with the user in its `WHERE` and in its join; the funnel is one
grouped count through `for_user`, so the lane is `lanes.lane_expression()` and nothing else.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Final, Literal

from django.db import connection
from django.db.models import BooleanField, Case, Count, F, Q, Value, When
from django.db.models.fields.json import KT

from apps.applications.models import ApplicationStatus
from apps.core.router import ApiModel
from apps.matching.lanes import LANES, lane_expression
from apps.matching.models import JobMatch
from apps.matching.queries import RECOMMENDATIONS

__all__ = [
    "MIN_DENOMINATOR",
    "P90_MIN",
    "RATES_AFTER",
    "RECENT",
    "SAME_DAY",
    "STEPS",
    "THREE_DAYS",
    "OutcomeBucket",
    "OutcomeClosed",
    "OutcomeFunnel",
    "OutcomeGroup",
    "OutcomeSpan",
    "OutcomeSpeed",
    "OutcomeStep",
    "OutcomeTiming",
    "OutcomeTimings",
    "by_speed",
    "funnel",
    "reached",
    "timing",
]

RECENT: Final = timedelta(days=30)  # the window beside "all time"
# The buckets of posted-to-applied time, as the Apply page already words a sent application
# (`afterPosted`): the same day is under 24 hours, within three days is under 72.
SAME_DAY: Final = timedelta(days=1)
THREE_DAYS: Final = timedelta(days=3)
# Applications in the buckets before their interview rates are shown (#65).
RATES_AFTER: Final = 50
# The fewest a rate may rest on. Under it the counts are given and the rate is not.
MIN_DENOMINATOR: Final = 10
# The fewest spans a 90th percentile is given for: under ten it is only the longest one.
P90_MIN: Final = 10

# The funnel, in order. `rejected` and `archived` are outcomes, not steps.
STEPS: Final[tuple[str, ...]] = ("found", "shortlisted", "applied", "interviewing", "offer")
_APPLIED: Final = STEPS.index("applied")
_INTERVIEWING: Final = STEPS.index("interviewing")
_CLOSED: Final[tuple[str, ...]] = ("rejected", "archived")

StepName = Literal["found", "shortlisted", "applied", "interviewing", "offer"]
BucketName = Literal["same_day", "three_days", "later"]
_BUCKETS: Final[tuple[BucketName, ...]] = ("same_day", "three_days", "later")
NOT_REVIEWED: Final = "none"  # a match with no deep review, or one whose verdict does not read


def reached(stage: str, recorded: bool) -> int:
    """How far along `STEPS` a match is known to have got, as an index: from where it is now,
    and whether its application was recorded as sent. A closed match proves nothing by its
    stage (module docstring), and a recorded application proves `applied` wherever the match
    was put afterwards."""
    here = STEPS.index(stage) if stage in STEPS else 0
    return max(here, _APPLIED) if recorded else here


def _rate(part: int, whole: int) -> float | None:
    """`part / whole`, or None when `whole` is too few to say."""
    return round(part / whole, 4) if whole >= MIN_DENOMINATOR else None


# --- the user's applications ---------------------------------------------------------------------

# One row for each match known to have been applied to: every one `reached()` puts at
# `applied` or past it (`test_the_three_reports_count_the_same_applications`). The join brings
# only this user's application, and only one that was marked sent, so `ready_at` is a recorded
# application's or NULL: one prepared after a match was moved by hand says nothing about how
# that application was made. `sent_at` is NULL for one made by hand that has left `applied`.
_SENT: Final = """
    SELECT m.stage,
           a.id IS NOT NULL AS recorded,
           CASE WHEN a.id IS NOT NULL THEN a.submitted_at
                WHEN m.stage = 'applied' THEN m.stage_changed_at END AS sent_at,
           a.ready_at,
           j.posted_at,
           j.first_seen_at
    FROM job_matches m
    JOIN jobs j ON j.id = m.job_id
    LEFT JOIN applications a
           ON a.match_id = m.id AND a.user_id = m.user_id
          AND a.status = 'submitted' AND a.submitted_at IS NOT NULL
    WHERE m.user_id = %(user)s
      AND (a.id IS NOT NULL OR m.stage IN ('applied', 'interviewing', 'offer'))
"""


# --- timing (#65) --------------------------------------------------------------------------------

# Each span, as (the later moment, the earlier one) among `_SENT`'s columns.
_SPANS: Final[dict[str, tuple[str, str]]] = {
    "posted_to_seen": ("first_seen_at", "posted_at"),
    "seen_to_ready": ("ready_at", "first_seen_at"),
    "ready_to_applied": ("sent_at", "ready_at"),
    "posted_to_applied": ("sent_at", "posted_at"),
}


def _span_sql(later: str, earlier: str) -> str:
    """The time from one moment to a later one: zero when the two are the other way round (two
    clocks, or a date a source moved), NULL when either is missing."""
    return f"CASE WHEN {later} < {earlier} THEN interval '0' ELSE {later} - {earlier} END"


_SPAN_COLUMNS: Final = ", ".join(
    f"{_span_sql(later, earlier)} AS {name}" for name, (later, earlier) in _SPANS.items()
)
# For each span: how many applications have it, its median and its 90th percentile.
_SPAN_NUMBERS: Final = ", ".join(
    f"count(s.{name}), "
    f"percentile_cont(0.5) WITHIN GROUP (ORDER BY s.{name}), "
    f"percentile_cont(0.9) WITHIN GROUP (ORDER BY s.{name})"
    for name in _SPANS
)
# Two rows, always: every application with a sent time, and the ones sent in the last
# `%(recent)s`. Each also carries the count of applications with no sent time.
_TIMING: Final = f"""
    WITH sent AS ({_SENT}), spans AS (
        SELECT sent_at, recorded, {_SPAN_COLUMNS}
        FROM sent
        WHERE sent_at IS NOT NULL
    )
    SELECT w.name,
           (SELECT count(*) FROM sent WHERE sent_at IS NULL),
           count(s.sent_at),
           count(*) FILTER (WHERE NOT s.recorded),
           {_SPAN_NUMBERS}
    FROM (
        VALUES ('all_time', NULL::interval), ('recent', make_interval(secs => %(recent)s))
    ) AS w (name, last)
    LEFT JOIN spans s ON w.last IS NULL OR s.sent_at > now() - w.last
    GROUP BY w.name
"""


class OutcomeSpan(ApiModel):
    applications: int  # how many applications the two numbers rest on
    median_s: int | None  # null with none
    p90_s: int | None  # null under `P90_MIN`


class OutcomeTiming(ApiModel):
    applications: int  # sent in the window, with the time each was sent
    by_hand: int  # of those, moved to Applied by hand: no ready time
    posted_to_seen: OutcomeSpan
    seen_to_ready: OutcomeSpan
    ready_to_applied: OutcomeSpan
    posted_to_applied: OutcomeSpan


class OutcomeTimings(ApiModel):
    days: int  # the recent window
    p90_after: int  # `P90_MIN`
    all_time: OutcomeTiming
    recent: OutcomeTiming
    # Made by hand and since moved past Applied: the time they were sent was not kept.
    untimed: int


def _seconds(value: timedelta | None) -> int | None:
    return None if value is None else round(value.total_seconds())


def _window(row: tuple[Any, ...]) -> OutcomeTiming:
    """One row of `_TIMING`, after its name and the untimed count."""
    spans: dict[str, OutcomeSpan] = {}
    for index, name in enumerate(_SPANS):
        counted, median, p90 = row[4 + 3 * index : 7 + 3 * index]
        spans[name] = OutcomeSpan(
            applications=int(counted),
            median_s=_seconds(median),
            p90_s=_seconds(p90) if counted >= P90_MIN else None,
        )
    return OutcomeTiming(applications=int(row[2]), by_hand=int(row[3]), **spans)


def timing(user_id: str) -> OutcomeTimings:
    """The median and 90th percentile of each span, over every sent application and over the
    ones sent in the last `RECENT`."""
    with connection.cursor() as cursor:
        cursor.execute(_TIMING, {"user": user_id, "recent": RECENT.total_seconds()})
        rows = {row[0]: row for row in cursor.fetchall()}
    return OutcomeTimings(
        days=RECENT.days,
        p90_after=P90_MIN,
        all_time=_window(rows["all_time"]),
        recent=_window(rows["recent"]),
        untimed=int(rows["all_time"][1]),
    )


# --- interview rate by how soon it went out (#65) ------------------------------------------------

# Recorded applications to dated postings, by the seconds from posted to sent (less than none
# falls in the first bucket), with how many of each are in an interview stage now.
_BY_SPEED: Final = f"""
    WITH sent AS ({_SENT})
    SELECT CASE WHEN waited < %(same_day)s THEN 'same_day'
                WHEN waited < %(three_days)s THEN 'three_days'
                ELSE 'later' END,
           count(*),
           count(*) FILTER (WHERE stage IN ('interviewing', 'offer'))
    FROM (
        SELECT stage, extract(epoch FROM sent_at - posted_at) AS waited
        FROM sent
        WHERE recorded AND posted_at IS NOT NULL
    ) AS timed
    GROUP BY 1
"""


class OutcomeBucket(ApiModel):
    bucket: BucketName
    applications: int
    interviews: int  # of those, in Interviewing or Offer now
    rate: float | None  # null until `enough`, and under `MIN_DENOMINATOR` in this bucket


class OutcomeSpeed(ApiModel):
    applications: int  # recorded applications to postings that say when they went up
    needed: int  # `RATES_AFTER`
    enough: bool
    buckets: list[OutcomeBucket]  # always the three, soonest first


def by_speed(user_id: str) -> OutcomeSpeed:
    """Recorded applications by posted-to-applied time, with how many of each reached an
    interview. Only recorded ones: an application made by hand keeps its sent time only until
    it moves on, so it would sit in a bucket exactly as long as it had no interview."""
    with connection.cursor() as cursor:
        cursor.execute(
            _BY_SPEED,
            {
                "user": user_id,
                "same_day": SAME_DAY.total_seconds(),
                "three_days": THREE_DAYS.total_seconds(),
            },
        )
        counted = {row[0]: (int(row[1]), int(row[2])) for row in cursor.fetchall()}
    total = sum(applications for applications, _ in counted.values())
    enough = total >= RATES_AFTER
    buckets: list[OutcomeBucket] = []
    for bucket in _BUCKETS:
        applications, interviews = counted.get(bucket, (0, 0))
        buckets.append(
            OutcomeBucket(
                bucket=bucket,
                applications=applications,
                interviews=interviews,
                rate=_rate(interviews, applications) if enough else None,
            )
        )
    return OutcomeSpeed(applications=total, needed=RATES_AFTER, enough=enough, buckets=buckets)


# --- the funnel and its splits (#6) --------------------------------------------------------------


class OutcomeStep(ApiModel):
    stage: StepName
    matches: int  # known to have reached this step or gone past it
    rate: float | None  # of the step before; null for the first and under `MIN_DENOMINATOR`


class OutcomeClosed(ApiModel):
    stage: Literal["rejected", "archived"]
    matches: int
    applied: int  # of those, the ones whose application was recorded as sent


class OutcomeGroup(ApiModel):
    key: str  # a source, a recommendation (or "none"), a lane, or "tailored" / "untailored"
    applications: int
    interviews: int
    rate: float | None  # null under `MIN_DENOMINATOR`


class OutcomeFunnel(ApiModel):
    steps: list[OutcomeStep]  # the five of `STEPS`, in order
    closed: list[OutcomeClosed]  # rejected, then archived
    by_source: list[OutcomeGroup]  # most applications first; only sources applied through
    by_recommendation: list[OutcomeGroup]  # strong, apply, maybe, skip, then "none"
    by_lane: list[OutcomeGroup]  # coached, automated, none
    by_resume: list[OutcomeGroup]  # tailored, untailored
    min_denominator: int  # `MIN_DENOMINATOR`


def _groups(counted: dict[str, list[int]], keys: tuple[str, ...]) -> list[OutcomeGroup]:
    groups: list[OutcomeGroup] = []
    for key in keys:
        applications, interviews = counted.get(key, (0, 0))
        groups.append(
            OutcomeGroup(
                key=key,
                applications=applications,
                interviews=interviews,
                rate=_rate(interviews, applications),
            )
        )
    return groups


def funnel(user_id: str) -> OutcomeFunnel:
    """How many matches reached each step, what became of the closed ones, and the interview
    rate of the applications by source, recommendation, lane and resume. One grouped count of
    the user's matches; `reached()` reads each group."""
    # `_SENT`'s join, said to the ORM: this user's application, marked sent.
    recorded = Q(
        application__status=ApplicationStatus.SUBMITTED,
        application__submitted_at__isnull=False,
        application__user=F("user"),
    )
    rows = (
        JobMatch.objects.for_user(user_id)
        .annotate(
            recorded=Case(
                When(recorded, then=Value(True)), default=Value(False), output_field=BooleanField()
            ),
            verdict=KT("deep_eval__recommendation"),
            lane=lane_expression(),
            tailored=Case(
                When(tailored_resume__isnull=False, then=Value(True)),
                default=Value(False),
                output_field=BooleanField(),
            ),
        )
        .values("stage", "recorded", "job__source", "verdict", "lane", "tailored")
        .annotate(matches=Count("id"))
        .order_by()
    )
    steps = [0] * len(STEPS)
    closed = {stage: [0, 0] for stage in _CLOSED}
    splits: dict[str, dict[str, list[int]]] = {
        "source": {},
        "recommendation": {},
        "lane": {},
        "resume": {},
    }
    for row in rows:
        matches, stage = int(row["matches"]), row["stage"]
        far = reached(stage, row["recorded"])
        for step in range(far + 1):
            steps[step] += matches
        if stage in closed:
            closed[stage][0] += matches
            closed[stage][1] += matches if far >= _APPLIED else 0
        if far < _APPLIED:
            continue
        keys = {
            "source": row["job__source"],
            "recommendation": row["verdict"] if row["verdict"] in RECOMMENDATIONS else NOT_REVIEWED,
            "lane": row["lane"],
            "resume": "tailored" if row["tailored"] else "untailored",
        }
        for split, key in keys.items():
            counted = splits[split].setdefault(key, [0, 0])
            counted[0] += matches
            counted[1] += matches if far >= _INTERVIEWING else 0
    sources = sorted(splits["source"], key=lambda key: (-splits["source"][key][0], key))
    return OutcomeFunnel(
        steps=[
            OutcomeStep(
                stage=stage,  # pyright: ignore[reportArgumentType] (one of `STEPS`)
                matches=steps[index],
                rate=_rate(steps[index], steps[index - 1]) if index else None,
            )
            for index, stage in enumerate(STEPS)
        ],
        closed=[
            OutcomeClosed(
                stage=stage,  # pyright: ignore[reportArgumentType] (one of `_CLOSED`)
                matches=closed[stage][0],
                applied=closed[stage][1],
            )
            for stage in _CLOSED
        ],
        by_source=_groups(splits["source"], tuple(sources)),
        by_recommendation=_groups(splits["recommendation"], (*RECOMMENDATIONS, NOT_REVIEWED)),
        by_lane=_groups(splits["lane"], LANES),
        by_resume=_groups(splits["resume"], ("tailored", "untailored")),
        min_denominator=MIN_DENOMINATOR,
    )
