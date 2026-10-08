"""The JSON columns on `users` (design §2.3; `map/llm.md` §21, `map/database.md` §3.3).

- `users.preferences` is `Preferences`: snake_case keys, every field nullable and defaulted to
  None. Nullable because "not asked yet" and "asked, no answer" differ; defaulted because a row
  written by an older version must still read, or every answer the user gave would be lost.
  The field order is the stored and prompted order.
- `users.interview_state` is `InterviewState`: `{version, skipped}`.
- `users.settings` is `Settings`: camelCase keys, partial values stored, defaults applied on read.

All three read leniently (`DefaultedJsonModel.read`): an unreadable value reads as the defaults,
exactly as `safeParse` with a fallback did. The interview's `SLOTS` and progress live in
`accounts/profile.py`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from apps.core.choices import CAMEL, DefaultedJsonModel, JsNumber

INTERVIEW_VERSION = 1

# The profile's seniority. The ranking one (`matching.schemas.RankingSeniority`) adds `unclear`.
Seniority = Literal["intern", "new_grad", "entry", "mid", "senior", "staff_plus"]
RemotePreference = Literal["remote", "hybrid", "onsite", "any"]
AutonomyMode = Literal["approve_each", "autopilot"]


class SalaryFloor(BaseModel):
    model_config = ConfigDict(extra="ignore")

    amount: JsNumber
    currency: str
    period: Literal["year", "hour"]


class Preferences(DefaultedJsonModel):
    target_roles: list[str] | None = None
    seniority: Seniority | None = None
    locations: list[str] | None = None
    remote_pref: RemotePreference | None = None
    salary_floor: SalaryFloor | None = None
    work_auth: str | None = None
    start_date: str | None = None
    must_haves: list[str] | None = None
    dealbreakers: list[str] | None = None


class InterviewState(DefaultedJsonModel):
    version: JsNumber = INTERVIEW_VERSION
    # Slots the user explicitly declined to answer, so the agent stops asking.
    skipped: list[str] = Field(default_factory=list)


class Settings(DefaultedJsonModel):
    model_config = CAMEL

    # What a search does when the user does not say: `approve_each` stops before the deep
    # evaluation and before shortlisting; `autopilot` does both.
    default_mode: AutonomyMode = "approve_each"
    # Fast scores below this are archived with a note rather than shown.
    score_threshold: int = Field(default=55, ge=0, le=100)
    # How many of the best fast-scored matches get the strong-model review.
    deep_eval_top_n: int = Field(default=15, ge=0, le=60)


# Complete preferences for throwaway users: `create_test_user --with-resume` and
# `tests/factories.py`. Every slot is filled, so a search can start at once.
SAMPLE_PREFERENCES = Preferences(
    target_roles=["Software Engineer", "Backend Engineer"],
    seniority="mid",
    locations=["San Francisco Bay Area"],
    remote_pref="hybrid",
    salary_floor=SalaryFloor(amount=150000, currency="USD", period="year"),
    work_auth="US citizen; no sponsorship needed",
    start_date="Within a month",
    must_haves=["A team that reviews code"],
    dealbreakers=["More than one week of on-call a month"],
)
