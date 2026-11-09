"""Which apply lane a match is in (design "Apply lanes"; #19, #39).

The deep review decides. `strong` goes to the coached lane (Foray coaches, the user writes and
sends), `apply` to the automated lane (Foray fills the form, the user presses submit), and
`maybe`, `skip` or no review at all to neither: a fast score alone never puts a match in a lane,
because triage is least reliable in the band where a cutoff would sit (#35).

The user can put any match in any lane, or in none. That choice is `job_matches.lane_override`,
and it wins until they hand the match back to the review (NULL). Only the user's own change
writes the column, so a re-review moves the lane a review gives and never the user's.

`effective_lane` is the rule, and the one place it is written: the row query, the match page
and whatever acts on a lane later all call it (`lane_of(match)` for a loaded match).
`lane_expression` says the same thing in SQL, from the same table, for a query that has to
narrow by lane before its limit; `tests/matching/test_lanes.py` holds the two to each other
over every stored shape.
"""

from __future__ import annotations

from typing import Any, Final, Literal

from django.db.models import Case, F, TextField, Value, When
from django.db.models.expressions import Combinable
from django.db.models.functions import Coalesce

from apps.matching.models import JobMatch, Lane

__all__ = [
    "LANES",
    "REVIEW_LANES",
    "Lane",
    "LaneName",
    "as_lane",
    "effective_lane",
    "lane_expression",
    "lane_of",
    "review_lane",
]

LaneName = Literal["coached", "automated", "none"]
LANES: Final[tuple[str, ...]] = tuple(Lane.values)

# The lane each recommendation of a deep review leads to; any other verdict leads to none.
REVIEW_LANES: Final[dict[str, Lane]] = {"strong": Lane.COACHED, "apply": Lane.AUTOMATED}


def as_lane(value: Any) -> Lane | None:
    """A lane from a caller or a column, or None when it does not name one."""
    return Lane(value) if isinstance(value, str) and value in LANES else None


def review_lane(deep_eval: Any) -> Lane:
    """The lane the deep review alone gives a match; none without one. The evaluation is read as
    stored JSON, leniently, as a row's verdict is (`matching/verdict.py`)."""
    recommendation = deep_eval.get("recommendation") if isinstance(deep_eval, dict) else None
    if not isinstance(recommendation, str):
        return Lane.NONE
    return REVIEW_LANES.get(recommendation, Lane.NONE)


def effective_lane(override: Any, deep_eval: Any) -> Lane:
    """The lane a match is in: the user's override when they set one, else the review's."""
    return as_lane(override) or review_lane(deep_eval)


def lane_of(match: JobMatch) -> Lane:
    """`effective_lane` for a loaded match: what code that acts on a lane asks."""
    return effective_lane(match.lane_override, match.deep_eval)


def lane_expression(override: str = "lane_override", deep_eval: str = "deep_eval") -> Combinable:
    """`effective_lane` as an expression over a match's columns, to filter or group by:
    `matches.alias(lane=lane_expression()).filter(lane=Lane.COACHED)`. The CHECK on the override
    column keeps anything that is not a lane out of it, so it needs no reading here."""
    by_review = Case(
        *(
            When(**{f"{deep_eval}__recommendation": recommendation}, then=Value(lane.value))
            for recommendation, lane in REVIEW_LANES.items()
        ),
        default=Value(Lane.NONE.value),
    )
    return Coalesce(F(override), by_review, output_field=TextField())
