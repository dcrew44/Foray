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
from apps.applications.models import AnswerKind
from apps.applications.outcomes import OutcomeFunnel, OutcomeSpeed, OutcomeTimings
from apps.contacts.outreach import MatchPeople
from apps.core.router import ApiModel
from apps.jobs.legitimacy import SignalKind
from apps.jobs.pipeline import CompanyRow
from apps.matching.lanes import LaneName
from apps.matching.queries import MatchRow, Recommendation, RemoteName, StageName
from apps.matching.schemas import DeepEval, FastScore
from apps.resumes.schema import Resume
from apps.resumes.variants import VariantSpec
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
    "JobFacts",
    "JobDetail",
    "JobSignal",
    "LatestRender",
    "MatchDetail",
    "MatchDetailPage",
    "MatchesPage",
    "ModelSpend",
    "MonthSpend",
    "OutcomesPage",
    "ProfilePage",
    "ProfileVariant",
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
    "VariantPdf",
]

ProviderId = Literal["openai", "google", "anthropic", "deepseek", "compat"]
JobSourceName = Literal[
    "greenhouse",
    "lever",
    "ashby",
    "workable",
    "recruitee",
    "gem",
    "jibe",
    "workday",
    "smartrecruiters",
    "icims",
    "jsearch",
    "web",
]
JobLevelName = Literal["intern", "new_grad", "entry", "mid", "senior", "staff_plus", "unknown"]
WorkAuthName = Literal["citizen_only", "us_person", "no_sponsorship", "sponsors", "unknown"]
ClearanceName = Literal["required", "eligible", "none"]
ApplicationStatusName = Literal[
    "preparing", "needs_answers", "ready", "submitted", "dismissed", "failed"
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
    # Matched in the last 7 days and reviewed strong or apply, still waiting: at most 6 (#5).
    new_this_week: list[MatchRow]
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
    lane: LaneName | None  # the apply lane `matches` was narrowed to, when the link named one


# --- /pages/matches/{id} -------------------------------------------------------------------------


class SentAnswer(ApiModel):
    label: str
    value: str


class MatchApplication(ApiModel):
    """The match's application, when one has been prepared or is being (#60)."""

    id: uuid.UUID
    status: ApplicationStatusName
    missing: int  # required questions with no answer yet
    # Proof of arrival (#23), once it was sent: when, who pressed submit, what the employer's
    # page said, whether a picture of it is on file, and what went into the form.
    submitted_at: datetime | None
    auto_submitted: bool
    confirmation_text: str
    confirmation_url: str
    has_picture: bool
    document_id: uuid.UUID | None  # the resume PDF that was attached
    sent: list[SentAnswer]


class MatchDetail(ApiModel):
    id: uuid.UUID
    stage: StageName
    deep_eval: DeepEval | None
    fast_eval: FastScore | None
    # The line the page leads with, as the match's row has it (`matching/verdict.py`, #38).
    reason: str | None
    blocker: str | None
    # The apply lane (`matching/lanes.py`, #39): the one the match is in, the one the user chose
    # (null while it follows the review) and the one the review alone gives.
    lane: LaneName
    lane_override: LaneName | None
    review_lane: LaneName
    tailored_resume_id: uuid.UUID | None
    notes: str | None
    created_at: datetime
    stage_changed_at: datetime
    fast_score: int | None
    deep_score: int | None
    application: MatchApplication | None


class JobFacts(ApiModel):
    """What the posting asks of whoever applies (`jobs/facts.py`, #56), with the sentence each
    was read from. `read` is false until the posting's facts have been read."""

    read: bool
    level: JobLevelName
    level_from: Literal["title", "text", "years"] | None
    years_min: int | None
    years_preferred: int | None
    work_auth: WorkAuthName
    clearance: ClearanceName
    years_evidence: str | None
    work_auth_evidence: str | None
    clearance_evidence: str | None


# A reason to look twice before applying (`jobs/legitimacy.py`, #7): what kind, a few words,
# and one sentence for the user.
class JobSignal(ApiModel):
    kind: SignalKind
    label: str
    detail: str


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
    facts: JobFacts
    signals: list[JobSignal]  # closed first; empty for an ordinary posting


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
    # Who the user knows at the posting's company (#64), each with the note drafted to them
    # (#25); empty for nobody.
    knows: MatchPeople


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


class RadarLag(ApiModel):
    source: str
    postings: int
    median_s: int
    p90_s: int


class Radar(ApiModel):
    """How soon new postings are seen and how often boards are asked (`jobs/radar.py`, #55)."""

    days: int  # the window the lag was measured over
    postings: int  # new postings measured in it
    median_s: int | None  # posted-to-seen lag; null with nothing measured yet
    p90_s: int | None
    by_source: list[RadarLag]  # most postings first
    hot_boards: int  # asked every 15 minutes
    cold_boards: int  # asked hourly
    careers_pages: int  # read every six hours
    last_checked_at: datetime | None  # the newest successful check of any board


class CompaniesPage(ApiModel):
    companies: list[CompanyRow]  # by name, at most 10,000
    active: int  # companies whose board is active
    open_postings: int  # open postings across the listed companies
    radar: Radar


# --- /pages/profile ------------------------------------------------------------------------------


class BaseVersion(ApiModel):
    version: int
    created_at: datetime


class LatestRender(ApiModel):
    document_id: uuid.UUID  # the newest base version that has a PDF
    version: int


class AnswerOption(ApiModel):
    value: str  # what is stored
    label: str


class SavedAnswer(ApiModel):
    """One of the questions application forms share (#21), with the user's answer if any."""

    kind: AnswerKind
    question: str
    input: Literal["yes_no", "choice", "text"]
    options: list[AnswerOption]  # for a choice; empty otherwise
    group: Literal["work", "logistics", "links", "disclosures", "custom"]
    hint: str
    answer: str | None


class VariantPdf(ApiModel):
    document_id: uuid.UUID  # the newest PDF made of the variant
    current: bool  # a render now would print the same resume under the same file name


class ProfileVariant(ApiModel):
    id: uuid.UUID
    name: str
    keywords: list[str]  # the title words that choose it
    is_default: bool
    spec: VariantSpec  # as stored: it may name items the newest base no longer has
    pdf: VariantPdf | None
    updated_at: datetime


class ProfilePage(ApiModel):
    has_resume: bool
    ready: bool
    resume: Resume | None  # the newest base resume, when it reads
    preferences: Preferences
    progress: InterviewProgress
    slots: list[Slot]  # the interview's slots, so the frontend keeps no copy
    base: BaseVersion | None  # the newest base version, whether or not it reads
    render: LatestRender | None
    # The shared application questions in the order the page asks them, then the user's own.
    answers: list[SavedAnswer]
    variants: list[ProfileVariant]  # the resume variants (#61), oldest first
    variant_limit: int  # how many variants a user may keep, so the frontend keeps no copy


# --- /pages/apply --------------------------------------------------------------------------------


class ApplyAnswer(ApiModel):
    """One field of an application's form with the answer planned for it (`prepare.Planned`)."""

    key: str
    label: str
    type: str
    required: bool
    options: list[str]
    kind: str
    value: str | None
    source: Literal["profile", "file", "saved", "declined", "skipped", "missing"]


class ApplyResume(ApiModel):
    """Which resume an application sends, and why (`applications/resume.py`, #61)."""

    variant_id: uuid.UUID | None  # null: the full resume
    name: str  # the variant's name, or "Full resume"
    reason: str
    choice: str  # the user's own: "" (automatic), "base", or a variant's id


class ApplyVariant(ApiModel):
    id: uuid.UUID
    name: str


class ApplyItem(ApiModel):
    id: uuid.UUID
    match_id: uuid.UUID
    status: ApplicationStatusName
    company: str
    title: str
    locations: list[str]
    remote: RemoteName
    posted_at: datetime | None
    first_seen_at: datetime
    form_source: str  # whose form it is; "" when Foray does not know it
    form_read: bool  # false: only the usual fields are planned, and the page has more
    apply_url: str
    answers: list[ApplyAnswer]
    error: str
    fast_score: int | None
    deep_score: int | None
    recommendation: Recommendation | None
    prepared_at: datetime | None
    resume: ApplyResume


class ApplySent(ApiModel):
    id: uuid.UUID
    match_id: uuid.UUID
    company: str
    title: str
    submitted_at: datetime
    # Seconds from the posting going up (or first being seen) to the application being sent.
    posted_to_applied_s: int | None


class ApplyPage(ApiModel):
    # Oldest posting first: the one that has been up longest is the one to send first.
    ready: list[ApplyItem]
    needs_answers: list[ApplyItem]
    failed: list[ApplyItem]
    preparing: int
    sent: list[ApplySent]  # newest first, at most 20
    variants: list[ApplyVariant]  # the user's resume variants, to switch an application to


# --- /pages/outcomes -----------------------------------------------------------------------------


class OutcomesPage(ApiModel):
    """How long applications took to go out and what came of them (`applications/outcomes.py`,
    #65, #6). Every number comes with the count it rests on; a rate is null on too few."""

    matches: int  # every match the user has
    applications: int  # the ones known to have been applied to
    timing: OutcomeTimings
    speed: OutcomeSpeed
    funnel: OutcomeFunnel


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
    ntfy_server: str  # `NTFY_SERVER`: where the user's phone subscribes to their topic (#59)
