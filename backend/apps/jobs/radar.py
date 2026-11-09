"""The radar's own numbers (#55): how soon after it goes up a posting is seen, and how often
each board is asked.

Timeliness is what the product is judged by, so it is measured rather than assumed. Every new
posting that arrives on a board checked before carries `seen_lag_s` (`ingest.py`): the smaller of
"since the source says it was posted" and "since we last looked and it was not there". The first
alone would blame the radar for a posting the employer took down and put back, or for a source
whose date is when the role was drafted (Lever's `createdAt`); the second alone is only as good
as the polling interval. The report is the median and 90th percentile of that, by source, over
the last week. A board's first fetch measures nothing: every posting on it predates the looking.

Sync; one query each.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import connection
from django.db.models import Count, Max, Q

from apps.jobs.models import Company, CompanyStatus, SourceType
from apps.jobs.registry import RELEVANT_FOR

__all__ = ["WINDOW", "LagReport", "Polling", "SourceLag", "lag_report", "polling"]

WINDOW = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class SourceLag:
    source: str
    postings: int
    median_s: int
    p90_s: int


@dataclass(frozen=True, slots=True)
class LagReport:
    postings: int  # new postings measured in the window
    median_s: int | None  # None with nothing measured
    p90_s: int | None
    by_source: list[SourceLag]  # most postings first


def lag_report(window: timedelta = WINDOW) -> LagReport:
    """Posted-to-seen lag over the window, overall and by source."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT source, count(*),
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY seen_lag_s),
                   percentile_cont(0.9) WITHIN GROUP (ORDER BY seen_lag_s)
            FROM jobs
            WHERE seen_lag_s IS NOT NULL AND first_seen_at > now() - make_interval(secs => %s)
            GROUP BY GROUPING SETS ((source), ())
            """,
            [window.total_seconds()],
        )
        rows = cursor.fetchall()
    overall = next((row for row in rows if row[0] is None), None)
    by_source = sorted(
        (SourceLag(row[0], int(row[1]), round(row[2]), round(row[3])) for row in rows if row[0]),
        key=lambda lag: (-lag.postings, lag.source),
    )
    if overall is None or not overall[1]:
        return LagReport(0, None, None, [])
    return LagReport(int(overall[1]), round(overall[2]), round(overall[3]), by_source)


@dataclass(frozen=True, slots=True)
class Polling:
    hot: int  # active boards asked every 15 minutes
    cold: int  # active boards asked hourly
    careers_pages: int  # active careers pages, read every six hours
    last_ok_at: datetime | None  # the newest successful check of any company


def polling(now: datetime) -> Polling:
    """How many active companies are on each cadence, and when one last answered."""
    counted = Company.objects.filter(status=CompanyStatus.ACTIVE).aggregate(
        total=Count("id"),
        careers=Count("id", filter=Q(source_type=SourceType.CAREERS_PAGE)),
        hot=Count(
            "id",
            filter=~Q(source_type=SourceType.CAREERS_PAGE)
            & Q(last_relevant_at__gt=now - RELEVANT_FOR),
        ),
        last_ok=Max("last_ok_at"),
    )
    hot, careers = int(counted["hot"]), int(counted["careers"])
    return Polling(
        hot=hot,
        cold=int(counted["total"]) - careers - hot,
        careers_pages=careers,
        last_ok_at=counted["last_ok"],
    )
