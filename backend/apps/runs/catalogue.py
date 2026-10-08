"""The kind catalogue: every run kind and event kind, and the JSON each run stores (design §2.3,
§7.7, §9.2, §9.4; `map/worker.md` §6, §9.10, §11.3, §19; `map/_gaps.md` §3.2, §3.3).

Parallel lanes import from here and never redefine any of it. A lane that needs a change records
it for the next serial step.

**Runs.** A run is the only unit of background work a user sees, and every kind ends with
`run.completed`, `run.failed` or `run.cancelled` (R17). Per kind this module gives:

- `KIND_TITLE`: the kind's name in the UI ("Tailored resume", "Resume PDF", ...);
- `FAILURE_PREFIX`: what `run_step` puts before an unexpected error (§9.2);
- `FOLLOW_UP_KIND_NAME` / `FOLLOW_UP_KIND_TITLE`: the follow-up notice's "<name> has finished"
  and its "<title> finished" label (§7.7);
- `PHASES`: the values of `stats.phase`, in order;
- `STATS_KEYS`: the documented keys of the open `stats` dict, only ever merged with
  `stats || patch::jsonb`;
- `INPUT_MODELS` / `OUTPUT_MODELS`: the shapes of `runs.input` and `runs.output`, with today's
  camelCase keys. Imported rows keep whatever JSON they had; `read_input` / `read_output` read
  leniently.

Dedupe keys (`runs.dedupe_key`, one active run per key, R2): `search`; `evaluate:<sha1 of the
sorted match ids>`; `tailor:<matchId>`; `render:<resumeId>`; `import:<documentId>`;
`add:<normalised query>`; `refresh:<slug>` or `refresh:stale`.

**Approvals.** `PAYLOAD_MODELS` gives each approval kind's payload; `StoredDecision` is
`approvals.decision`; `APPROVAL_NEXT_STEP` names the continuation a decision defers.

**Events.** `EventKind` lists every kind `map/worker.md` §19 catalogues, each with its
`DEFAULT_LEVEL` (callers pass `level=` where a kind's level varies). `events.kind` has no CHECK,
so imported history may hold any of them; `LEGACY_EVENT_KINDS` are the ones only imported
history holds, because the uniform run finals replaced them. `FOLLOW_UP_KINDS` are the kinds
after which a run started from chat reports back there (§7.7); `FOLLOW_UP_EVENT` is the event a
follow-up emits.
"""

from __future__ import annotations

from typing import Any, Literal

from django.db import models
from pydantic import Field

from apps.core.choices import CAMEL, DefaultedJsonModel, JsNumber, JsonModel

# --- Enums ---------------------------------------------------------------------------------------


class RunKind(models.TextChoices):
    SEARCH = "search"
    RANK_DEEP = "rank_deep"
    TAILOR = "tailor"
    RESUME_RENDER = "resume_render"
    PROFILE_IMPORT = "profile_import"
    COMPANY_DISCOVERY = "company_discovery"
    COMPANY_REFRESH = "company_refresh"


class RunStatus(models.TextChoices):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Plain strings, so querysets and migrations see the values themselves.
ACTIVE: tuple[str, ...] = ("queued", "running", "awaiting_approval")
TERMINAL: tuple[str, ...] = ("completed", "failed", "cancelled")


class Mode(models.TextChoices):
    APPROVE_EACH = "approve_each"
    AUTOPILOT = "autopilot"


class ApprovalKind(models.TextChoices):
    DEEP_EVAL_BATCH = "deep_eval_batch"
    SHORTLIST_PROPOSAL = "shortlist_proposal"


class ApprovalStatus(models.TextChoices):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    AUTO_APPROVED = "auto_approved"


class DecidedVia(models.TextChoices):
    UI = "ui"
    CHAT = "chat"
    AUTOPILOT = "autopilot"
    EXPIRY = "expiry"


class NextStep(models.TextChoices):
    """The task a decided approval defers (was `continuation.queue`)."""

    SEARCH_EVALUATE = "search.evaluate"
    SEARCH_SHORTLIST = "search.shortlist"


class Level(models.TextChoices):
    DEBUG = "debug"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


class EventKind(models.TextChoices):
    # Run lifecycle (every kind ends with one of the three finals).
    RUN_STARTED = "run.started"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    # Approvals.
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_AUTO = "approval.auto"
    APPROVAL_DECIDED = "approval.decided"
    APPROVAL_EXPIRED = "approval.expired"
    APPROVAL_REOPENED = "approval.reopened"
    # Search progress.
    SEARCH_CRITERIA = "search.criteria"
    SEARCH_COMPANIES_ADDED = "search.companies.added"
    SEARCH_COMPANIES_MISSING = "search.companies.missing"
    SOURCE_REFRESH_START = "source.refresh.start"
    SOURCE_REFRESH_DONE = "source.refresh.done"
    SOURCE_AGGREGATOR_DONE = "source.aggregator.done"
    SOURCE_AGGREGATOR_SKIPPED = "source.aggregator.skipped"
    PREFILTER_DONE = "prefilter.done"
    SCORING_START = "scoring.start"
    SCORING_PROGRESS = "scoring.progress"
    SCORING_DONE = "scoring.done"
    EVALUATION_SKIPPED = "evaluation.skipped"
    EVALUATION_START = "evaluation.start"
    EVALUATION_PROGRESS = "evaluation.progress"
    EVALUATION_FAILED = "evaluation.failed"
    EVALUATION_DONE = "evaluation.done"
    SHORTLIST_SKIPPED = "shortlist.skipped"
    SHORTLIST_DONE = "shortlist.done"
    # Tailoring and rendering.
    TAILOR_STARTED = "tailor.started"
    TAILOR_VERIFIED = "tailor.verified"
    TAILOR_DONE = "tailor.done"
    TAILOR_FAILED = "tailor.failed"
    RENDER_DONE = "render.done"
    RENDER_FAILED = "render.failed"
    # Companies.
    COMPANY_DISCOVERED = "company.discovered"
    COMPANY_NOT_FOUND = "company.not-found"
    COMPANY_REFRESH_DONE = "company.refresh.done"
    # Jobs that are not runs.
    WORKER_PING = "worker.ping"
    CHAT_FOLLOWUP = "chat.followup"


# Each kind's level when the emitter does not pass one. Where a kind's level varies (`map/worker.md`
# §19), the emitter passes it: `evaluation.skipped` is info when the user declined, warn when
# something was missing; `source.aggregator.skipped` is warn on an unexpected error;
# `tailor.verified` is warn when lines need review; `render.failed` is warn inside a tailor run.
DEFAULT_LEVEL: dict[str, Level] = {
    EventKind.RUN_STARTED: Level.INFO,
    EventKind.RUN_COMPLETED: Level.INFO,
    EventKind.RUN_FAILED: Level.ERROR,
    EventKind.RUN_CANCELLED: Level.WARN,
    EventKind.APPROVAL_REQUESTED: Level.INFO,
    EventKind.APPROVAL_AUTO: Level.INFO,
    EventKind.APPROVAL_DECIDED: Level.INFO,
    EventKind.APPROVAL_EXPIRED: Level.WARN,
    EventKind.APPROVAL_REOPENED: Level.WARN,
    EventKind.SEARCH_CRITERIA: Level.INFO,
    EventKind.SEARCH_COMPANIES_ADDED: Level.INFO,
    EventKind.SEARCH_COMPANIES_MISSING: Level.WARN,
    EventKind.SOURCE_REFRESH_START: Level.INFO,
    EventKind.SOURCE_REFRESH_DONE: Level.INFO,
    EventKind.SOURCE_AGGREGATOR_DONE: Level.INFO,
    EventKind.SOURCE_AGGREGATOR_SKIPPED: Level.INFO,
    EventKind.PREFILTER_DONE: Level.INFO,
    EventKind.SCORING_START: Level.INFO,
    EventKind.SCORING_PROGRESS: Level.DEBUG,
    EventKind.SCORING_DONE: Level.INFO,
    EventKind.EVALUATION_SKIPPED: Level.WARN,
    EventKind.EVALUATION_START: Level.INFO,
    EventKind.EVALUATION_PROGRESS: Level.DEBUG,
    EventKind.EVALUATION_FAILED: Level.WARN,
    EventKind.EVALUATION_DONE: Level.INFO,
    EventKind.SHORTLIST_SKIPPED: Level.INFO,
    EventKind.SHORTLIST_DONE: Level.INFO,
    EventKind.TAILOR_STARTED: Level.INFO,
    EventKind.TAILOR_VERIFIED: Level.INFO,
    EventKind.TAILOR_DONE: Level.INFO,
    EventKind.TAILOR_FAILED: Level.ERROR,
    EventKind.RENDER_DONE: Level.INFO,
    EventKind.RENDER_FAILED: Level.WARN,
    EventKind.COMPANY_DISCOVERED: Level.INFO,
    EventKind.COMPANY_NOT_FOUND: Level.WARN,
    EventKind.COMPANY_REFRESH_DONE: Level.INFO,
    EventKind.WORKER_PING: Level.INFO,
    EventKind.CHAT_FOLLOWUP: Level.DEBUG,  # stays out of the feeds unless debug is on
}

# Only imported history holds these: tailor and render runs now end with `run.completed` /
# `run.failed`, and a failed continuation defer rolls the decision back instead of reopening it.
LEGACY_EVENT_KINDS: frozenset[str] = frozenset(
    {
        EventKind.APPROVAL_REOPENED,
        EventKind.TAILOR_DONE,
        EventKind.TAILOR_FAILED,
        EventKind.RENDER_DONE,
    }
)

# The same set as `src/lib/chat/follow-up-kinds.ts` (R17, checked by the chat tests).
FOLLOW_UP_KINDS: frozenset[str] = frozenset(
    {EventKind.APPROVAL_REQUESTED, EventKind.RUN_COMPLETED, EventKind.RUN_FAILED}
)
FOLLOW_UP_EVENT: str = EventKind.CHAT_FOLLOWUP

# --- Per-kind names ------------------------------------------------------------------------------

KIND_TITLE: dict[str, str] = {
    RunKind.SEARCH: "Search",
    RunKind.RANK_DEEP: "Deep review",
    RunKind.TAILOR: "Tailored resume",
    RunKind.RESUME_RENDER: "Resume PDF",
    RunKind.PROFILE_IMPORT: "Profile import",
    RunKind.COMPANY_DISCOVERY: "Company discovery",
    RunKind.COMPANY_REFRESH: "Board refresh",
}

# `run_step` fails a run with `FAILURE_PREFIX[kind] + <short error>` (§9.2). Search and deep
# review show the message alone, as today.
FAILURE_PREFIX: dict[str, str] = {
    RunKind.SEARCH: "",
    RunKind.RANK_DEEP: "",
    RunKind.TAILOR: "Tailoring failed: ",
    RunKind.RESUME_RENDER: "Could not render the PDF: ",
    RunKind.PROFILE_IMPORT: "Could not read that resume: ",
    RunKind.COMPANY_DISCOVERY: "Could not add that company: ",
    RunKind.COMPANY_REFRESH: "The refresh failed: ",
}

# The follow-up notice: "<name> has finished" (§7.7).
FOLLOW_UP_KIND_NAME: dict[str, str] = {
    RunKind.SEARCH: "a search",
    RunKind.RANK_DEEP: "a deep review",
    RunKind.TAILOR: "tailoring a resume",
    RunKind.RESUME_RENDER: "rendering a resume",
    RunKind.PROFILE_IMPORT: "reading a resume",
    RunKind.COMPANY_DISCOVERY: "company discovery",
    RunKind.COMPANY_REFRESH: "a board refresh",
}

# The follow-up label: "<title> finished" / "<title> stopped" (§7.7).
FOLLOW_UP_KIND_TITLE: dict[str, str] = {
    RunKind.SEARCH: "Search",
    RunKind.RANK_DEEP: "Deep review",
    RunKind.TAILOR: "Tailoring",
    RunKind.RESUME_RENDER: "Rendering",
    RunKind.PROFILE_IMPORT: "Resume import",
    RunKind.COMPANY_DISCOVERY: "Discovery",
    RunKind.COMPANY_REFRESH: "Board refresh",
}

# `stats.phase`, in order (§9.4). `shortlisting` (search) and `starting` (tailor) are now written.
PHASES: dict[str, tuple[str, ...]] = {
    RunKind.SEARCH: (
        "starting",
        "refreshing",
        "aggregating",
        "filtering",
        "scoring",
        "awaiting_evaluation",
        "evaluating",
        "awaiting_shortlist",
        "shortlisting",
        "done",
    ),
    RunKind.RANK_DEEP: ("evaluating", "done"),
    RunKind.TAILOR: ("starting", "tailoring", "verifying", "revising", "rendering", "done"),
    RunKind.RESUME_RENDER: ("rendering", "done"),
    RunKind.PROFILE_IMPORT: ("reading", "extracting", "saving", "done"),
    RunKind.COMPANY_DISCOVERY: ("discovering", "refreshing", "done"),
    RunKind.COMPANY_REFRESH: ("waiting", "refreshing", "done"),
}

# The documented keys of `runs.stats` (`map/worker.md` §9.10, §11.3).
STATS_KEYS: dict[str, tuple[str, ...]] = {
    RunKind.SEARCH: (
        "phase",
        "companies",
        "companiesMissing",
        "boardsRefreshed",
        "boardsFailed",
        "aggregatorJobs",
        "aggregatorRequests",
        "companiesDiscovered",
        "activeInMirror",
        "candidates",
        "matches",
        "newMatches",
        "scored",
        "autoArchived",
        "scoringFailed",
        "toEvaluate",
        "evaluated",
        "evaluationFailed",
        "recommended",
        "proposedShortlist",
        "shortlisted",
        "resumedFrom",  # the approval a continuation resumed from: makes a redelivery a no-op
    ),
    RunKind.RANK_DEEP: ("phase", "toEvaluate", "evaluated", "evaluationFailed"),
    RunKind.TAILOR: (
        "phase",
        "matchId",
        "title",
        "company",
        "baseVersion",
        "resumeId",
        "checked",
        "revisedCount",
        "unsupported",
        "changes",
        "documentId",
        "pages",
        "density",
    ),
    RunKind.RESUME_RENDER: ("phase", "documentId", "pages", "density"),
    RunKind.PROFILE_IMPORT: ("phase",),
    RunKind.COMPANY_DISCOVERY: ("phase",),
    RunKind.COMPANY_REFRESH: ("phase",),
}

# --- runs.input ----------------------------------------------------------------------------------


class SearchInput(DefaultedJsonModel):
    model_config = CAMEL

    prompt: str | None = None  # what the user asked for
    # Brief fields the chat agent resolved from the conversation (`Partial<SearchBrief>`). They
    # win over what is read from the prompt. Read with `matching.brief.parse_brief`.
    hints: dict[str, Any] | None = None
    brief: dict[str, Any] | None = None  # `SearchBrief`, derived once and cached
    criteria: dict[str, Any] | None = None  # `DerivedCriteria`, cached with the brief
    # The postings this run's prefilter chose: what the continuations mean by "this run's
    # matches".
    candidate_job_ids: list[str] | None = None


class EvaluateInput(JsonModel):
    model_config = CAMEL

    match_ids: list[str]  # sorted


class TailorInput(JsonModel):
    model_config = CAMEL

    match_id: str
    prompt: str  # "Tailor your resume for <title> at <company>"


class RenderInput(JsonModel):
    model_config = CAMEL

    resume_id: str
    prompt: str  # "Render your resume to PDF" / "Render your tailored resume to PDF"


class ImportInput(JsonModel):
    model_config = CAMEL

    document_id: str
    filename: str
    prompt: str  # "Read your resume"


class CompanyAddInput(JsonModel):
    model_config = CAMEL

    query: str | None = None  # one company: a name, a website or a job-board link
    names: list[str] | None = None  # several names (chat's discover_companies)
    refresh_after: bool  # fetch an API board once it is registered
    prompt: str


class CompanyRefreshInput(JsonModel):
    model_config = CAMEL

    slug: str | None = None  # one company
    name: str | None = None  # its name, stored at start for the activity page
    stale: bool = False  # every active board not fetched in 2 h
    prompt: str


# --- runs.output ---------------------------------------------------------------------------------


class SearchOutput(JsonModel):
    model_config = CAMEL

    candidates: int
    new_matches: int
    scored: int
    auto_archived: int
    evaluated: int
    shortlisted: int
    strong: int = 0  # new, so "N strong" shows; absent on imported runs
    top_match_ids: list[str]


class EvaluateOutput(JsonModel):
    model_config = CAMEL

    evaluated: int
    match_ids: list[str]


Density = Literal["normal", "compact"]


class TailorOutput(JsonModel):
    model_config = CAMEL

    resume_id: str
    document_id: str | None  # null when the render failed
    pages: int | None
    changes: int
    unsupported: int
    match_id: str
    density: Density | None = None  # the density the fitting chose (§11.6)


class RenderOutput(JsonModel):
    model_config = CAMEL

    resume_id: str
    document_id: str
    pages: int
    density: Density | None = None


class GarbleReport(JsonModel):
    model_config = CAMEL

    garbled: bool
    reasons: list[str]  # which signals tripped
    # chars, charsPerPage, spaceRatio, alphaRatio, replacementRatio, singleCharWordRatio
    metrics: dict[str, JsNumber] = Field(default_factory=dict)


class ImportCounts(JsonModel):
    work: int
    education: int
    skills: int
    projects: int


class ImportOutput(JsonModel):
    model_config = CAMEL

    document_id: str
    resume_id: str
    used_vision: bool
    garble: GarbleReport
    counts: ImportCounts


class DiscoveryStep(JsonModel):
    step: str
    detail: str


class CompanyAddOutput(JsonModel):
    # One query (`POST /api/companies`, chat's add_company).
    model_config = CAMEL

    registered: bool
    existed: bool
    company: dict[str, Any] | None  # the companies page's `CompanyRow`
    trail: list[DiscoveryStep]  # what was tried, in order
    message: str


class CompanyNamesOutput(JsonModel):
    # A list of names (chat's discover_companies).
    model_config = CAMEL

    added: list[str]
    not_found: list[str]
    message: str


class CompanyRefreshOutput(JsonModel):
    model_config = CAMEL

    boards: int  # boards attempted
    ok: int  # boards refreshed successfully
    not_found: int
    errored: int
    jobs: int
    inserted: int
    closed: int
    message: str


INPUT_MODELS: dict[str, type[JsonModel]] = {
    RunKind.SEARCH: SearchInput,
    RunKind.RANK_DEEP: EvaluateInput,
    RunKind.TAILOR: TailorInput,
    RunKind.RESUME_RENDER: RenderInput,
    RunKind.PROFILE_IMPORT: ImportInput,
    RunKind.COMPANY_DISCOVERY: CompanyAddInput,
    RunKind.COMPANY_REFRESH: CompanyRefreshInput,
}

# A kind may have more than one output shape; `read_output` tries them in order.
OUTPUT_MODELS: dict[str, tuple[type[JsonModel], ...]] = {
    RunKind.SEARCH: (SearchOutput,),
    RunKind.RANK_DEEP: (EvaluateOutput,),
    RunKind.TAILOR: (TailorOutput,),
    RunKind.RESUME_RENDER: (RenderOutput,),
    RunKind.PROFILE_IMPORT: (ImportOutput,),
    RunKind.COMPANY_DISCOVERY: (CompanyAddOutput, CompanyNamesOutput),
    RunKind.COMPANY_REFRESH: (CompanyRefreshOutput,),
}


def read_input(kind: str, value: Any) -> JsonModel | None:
    """A run's input through its kind's model, or None when unreadable. Never raises."""
    model = INPUT_MODELS.get(kind)
    return model.read(value) if model else None


def read_output(kind: str, value: Any) -> JsonModel | None:
    """A run's output through its kind's model(s), or None when absent or unreadable."""
    for model in OUTPUT_MODELS.get(kind, ()):
        parsed = model.read(value)
        if parsed is not None:
            return parsed
    return None


# --- approvals -----------------------------------------------------------------------------------


class CostEstimate(JsonModel):
    model_config = CAMEL

    usd: JsNumber | None  # every item, in USD; null when the model has no price on file
    per_item_usd: JsNumber | None
    model: str  # `provider:model`, or "unconfigured"


class DeepEvalBatchItem(JsonModel):
    model_config = CAMEL

    match_id: str
    job_id: str
    title: str
    company: str
    fast_score: int | None


class DeepEvalBatchPayload(JsonModel):
    # `deep_eval_batch`: which matches to spend the strong model on, and the cost.
    items: list[DeepEvalBatchItem]
    estimate: CostEstimate
    note: str | None


class ShortlistItem(JsonModel):
    model_config = CAMEL

    match_id: str
    job_id: str
    title: str
    company: str
    deep_score: int | None
    recommendation: Literal["strong", "apply", "maybe", "skip"]
    summary: str


class ShortlistPayload(JsonModel):
    # `shortlist_proposal`: evaluated matches worth moving to the shortlist.
    items: list[ShortlistItem]


PAYLOAD_MODELS: dict[str, type[JsonModel]] = {
    ApprovalKind.DEEP_EVAL_BATCH: DeepEvalBatchPayload,
    ApprovalKind.SHORTLIST_PROPOSAL: ShortlistPayload,
}

APPROVAL_NEXT_STEP: dict[str, NextStep] = {
    ApprovalKind.DEEP_EVAL_BATCH: NextStep.SEARCH_EVALUATE,
    ApprovalKind.SHORTLIST_PROPOSAL: NextStep.SEARCH_SHORTLIST,
}


class StoredDecision(JsonModel):
    # `approvals.decision`. `itemIds` is present whenever the payload has items: the approved
    # subset in payload order, or [] for a rejection; absent otherwise.
    model_config = CAMEL

    approved: bool
    item_ids: list[str] | None = None

    def dump(self, **kwargs: Any) -> dict[str, Any]:
        return super().dump(exclude_none=True, **kwargs)


def read_payload(kind: str, value: Any) -> JsonModel | None:
    """An approval's payload through its kind's model, or None when unreadable."""
    model = PAYLOAD_MODELS.get(kind)
    return model.read(value) if model else None
