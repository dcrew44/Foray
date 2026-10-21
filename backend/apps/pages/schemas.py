"""What each page endpoint answers (design §5.3; `map/next-frontend.md` §3).

One model per Next page, camelCase on the wire like every `ApiModel`. Two kinds of field keep
other spellings on purpose (§2.1): `preferences` is the stored `Preferences` (snake_case keys, as
the Preferences form reads them), and a match's `fastEval` / `deepEval` are the stored
`FastScore` / `DeepEval`. A tailored resume's `changes` and `verification` are passed through as
stored, as the TypeScript page did; the tailor panel reads them as plain objects.

Component names are page-prefixed or specific enough not to clash with another app's models
(`tests/core/test_openapi_names.py`). The models carry no class docstrings, which pydantic would
publish as the components' descriptions.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from apps.accounts.api import SettingsOut
from apps.accounts.profile import InterviewProgress, OnboardingState, Slot
from apps.accounts.schemas import Preferences
from apps.core.router import ApiModel
from apps.jobs.pipeline import CompanyRow
from apps.matching.queries import MatchRow, RemoteName, StageName
from apps.matching.schemas import DeepEval, FastScore
from apps.resumes.schema import Resume
from apps.runs.live import LiveRun
from foray.env import Effort, Purpose

__all__ = [
    "ActivityPage",
    "AggregatorUsage",
    "BaseVersion",
    "ChatPage",
    "ChatSuggestion",
    "CompaniesPage",
    "DaySpend",
    "HomeHeadline",
    "HomePage",
    "HomeSpend",
    "HomeUser",
    "HomeWaiting",
    "JobDetail",
    "LatestRender",
    "MatchDetail",
    "MatchDetailPage",
    "MatchesPage",
    "ModelSpend",
    "MonthSpend",
    "ProfilePage",
    "ProviderId",
    "ProviderKeys",
    "PurposeModel",
    "PurposeSpend",
    "ReviewCost",
    "RunSummary",
    "SettingsPage",
    "SettingsUser",
    "StageCounts",
    "TailoredView",
    "TokenRate",
]

ProviderId = Literal["openai", "google", "anthropic", "deepseek", "compat"]
JobSourceName = Literal[
    "greenhouse", "lever", "ashby", "workable", "recruitee", "gem", "jsearch", "web"
]


# Every stage with its count, zeros included (`countByStage`).
class StageCounts(ApiModel):
    found: int
    shortlisted: int
    applied: int
    interviewing: int
    offer: int
    rejected: int
    archived: int


# --- /pages/home ---------------------------------------------------------------------------------


class HomeUser(ApiModel):
    name: str


class HomeSpend(ApiModel):
    usd: float  # model spend this UTC month
    calls: int


class HomeWaiting(ApiModel):
    approvals: int  # pending, unexpired approvals
    running: int  # queued or running runs


# The page's headline is a status report, not a greeting (`describeState`).
class HomeHeadline(ApiModel):
    headline: str
    detail: str | None


class HomePage(ApiModel):
    user: HomeUser
    onboarding: OnboardingState
    counts: StageCounts
    best: list[MatchRow]  # found and shortlisted, best first, at most 6
    spend: HomeSpend
    waiting: HomeWaiting
    suggestions: list[str]  # the ask box's starting prompts
    headline: HomeHeadline


# --- /pages/matches ------------------------------------------------------------------------------


class MatchesPage(ApiModel):
    counts: StageCounts
    matches: list[MatchRow]  # best first, at most 1,000
    ready: bool  # a search can start
    view: Literal["board", "list"]
    stage: StageName | None  # the list's stage filter, when the link named a real stage
    show_closed: bool


# --- /pages/matches/{id} -------------------------------------------------------------------------


class MatchDetail(ApiModel):
    id: uuid.UUID
    stage: StageName
    deep_eval: DeepEval | None
    fast_eval: FastScore | None
    tailored_resume_id: uuid.UUID | None
    notes: str | None
    created_at: datetime
    stage_changed_at: datetime
    fast_score: int | None
    deep_score: int | None


class JobDetail(ApiModel):
    title: str
    company_name: str
    locations: list[str]
    remote: RemoteName
    salary_min: float | None
    salary_max: float | None
    salary_currency: str | None
    salary_period: str | None
    posted_at: datetime | None
    is_active: bool
    url: str
    apply_url: str | None
    description_text: str | None
    source: JobSourceName


class TailoredView(ApiModel):
    id: uuid.UUID
    document_id: uuid.UUID | None  # the rendered PDF, when the render succeeded
    created_at: datetime
    changes: list[dict[str, Any]]  # the stored `TailorChange` objects
    verification: dict[str, Any] | None  # the stored `Verification`


class MatchDetailPage(ApiModel):
    match: MatchDetail
    job: JobDetail
    tailored: TailoredView | None
    has_base_resume: bool


# --- /pages/activity -----------------------------------------------------------------------------


class RunSummary(ApiModel):
    run: LiveRun
    target: str | None  # what the run worked on, in words: "Senior Engineer at Life360"
    cost_usd: float | None  # model spend attributed to the run
    conversation_id: uuid.UUID | None


class ActivityPage(ApiModel):
    runs: list[RunSummary]  # newest first, at most 30


# --- /pages/chat ---------------------------------------------------------------------------------


class ChatSuggestion(ApiModel):
    title: str
    prompt: str
    prefill: bool  # put the prompt in the composer instead of sending it


class ChatPage(ApiModel):
    suggestions: list[ChatSuggestion]


# --- /pages/companies ----------------------------------------------------------------------------


class CompaniesPage(ApiModel):
    companies: list[CompanyRow]  # by name, at most 2,000
    active: int  # companies whose board is active
    open_postings: int  # open postings across the listed companies


# --- /pages/profile ------------------------------------------------------------------------------


class BaseVersion(ApiModel):
    version: int
    created_at: datetime


class LatestRender(ApiModel):
    document_id: uuid.UUID  # the newest base version that has a PDF
    version: int


class ProfilePage(ApiModel):
    has_resume: bool
    ready: bool
    resume: Resume | None  # the newest base resume, when it reads
    preferences: Preferences
    progress: InterviewProgress
    slots: list[Slot]  # the interview's slots, so the frontend keeps no copy
    base: BaseVersion | None  # the newest base version, whether or not it reads
    render: LatestRender | None


# --- /pages/settings -----------------------------------------------------------------------------


class SettingsUser(ApiModel):
    name: str
    email: str


class ReviewCost(ApiModel):
    per_item_usd: float | None  # one deep review at list price; null when unpriced
    model: str  # `provider:model`, or "unconfigured"


class MonthSpend(ApiModel):
    usd: float
    calls: int
    input_tokens: int  # uncached and cached input
    output_tokens: int


class PurposeSpend(ApiModel):
    purpose: Purpose
    usd: float
    calls: int


class ModelSpend(ApiModel):
    provider: str
    model: str
    usd: float
    calls: int
    failures: int
    avg_latency_ms: float


class DaySpend(ApiModel):
    day: date  # a UTC day that had calls
    usd: float


class AggregatorUsage(ApiModel):
    used: int  # JSearch requests this month
    budget: int
    enabled: bool


class TokenRate(ApiModel):
    input: float  # USD per million tokens
    output: float


class PurposeModel(ApiModel):
    purpose: Purpose
    ref: str | None  # `provider:model`; null when the purpose has no usable model
    error: str | None  # why not
    reasoning: Effort
    rate: TokenRate | None  # null when the model is not priced


class ProviderKeys(ApiModel):
    present: list[ProviderId]  # providers whose key is set; keys never leave the server


class SettingsPage(ApiModel):
    user: SettingsUser
    settings: SettingsOut  # with the defaults applied
    review: ReviewCost
    spend: MonthSpend
    by_purpose: list[PurposeSpend]  # most spent first
    by_model: list[ModelSpend]  # most spent first
    by_day: list[DaySpend]
    month_start: datetime  # the first of this month, UTC
    aggregator: AggregatorUsage
    purposes: list[PurposeModel]
    providers: ProviderKeys
