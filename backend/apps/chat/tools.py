"""The chat agent's 16 tools (design §7.2; a port of `src/lib/agents/tools/*.ts`).

- **Who.** Every tool reads the user from `RunContext.deps` (`agent.ChatDeps`), so the model has
  no way to name another user.
- **Async, through `db()`.** pydantic-ai runs sync tools in anyio threads, outside Django's
  connection handling, so they would leak pooled connections. Every tool is `async def` and
  reaches the database only through `db()` (`tests/test_register.py` checks it).
- **Inputs** are one pydantic model per tool, "all fields required, null means absent" (OpenAI
  strict schemas), with today's snake_case names and descriptions. The model is the tool's
  JSON schema.
- **Outputs** keep exactly the keys `tool-part.tsx` reads (`map/chat-agent.md` §4.2, §5.3),
  in today's order. `OUTPUT_MODELS` describes them for the generated TypeScript (`chat.api`
  registers them); the tests validate every output against its model.
- **One error wrapper** (`@chat_tool`): an `ApiError` (a throttle, a refused start),
  `BudgetExceeded` or another `UserFacingError` returns `{ok: false, message}` with its own
  sentence; anything else is logged and returns "That step failed on our side.", which the UI
  shows as a warning line and the model can recover from.
- **Runs** a tool starts carry `conversation=deps.conversation_id`, so they report back in this
  chat (R7, R17). That now includes `add_company` and `discover_companies`, whose
  `company_discovery` runs posted nothing back before.

`FOLLOW_UP_TOOLS` are the read-only six a follow-up turn may call (`agent.FollowUpTools`).
"""

from __future__ import annotations

import functools
import logging
import math
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal, cast

from django.db import transaction
from django.utils import timezone
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from apps.accounts.models import User
from apps.accounts.profile import SlotId, interview_progress
from apps.accounts.schemas import InterviewState, Preferences, RemotePreference, Seniority
from apps.core import throttle
from apps.core.choices import JsNumber
from apps.core.db import db
from apps.core.errors import ApiError, UserFacingError
from apps.core.jsonio import js_num
from apps.core.router import ApiModel
from apps.jobs import pipeline as company_pipeline
from apps.jobs import registry as company_registry
from apps.jobs.models import Company, Job
from apps.jobs.sources.types import js_iso
from apps.llm.structured import structured
from apps.matching import queries as match_queries
from apps.matching import search
from apps.matching.brief import clean_brief, describe_brief
from apps.matching.deep_eval import NoResumeError, cost_phrase, estimate_eval_cost
from apps.matching.evaluate import UnknownMatchError, start_match_evaluation
from apps.matching.queries import MatchRow, StageName
from apps.resumes import pipeline as resume_pipeline
from apps.resumes import schema as resume_schema
from apps.resumes.queries import latest_base_resume, latest_base_resume_row
from apps.resumes.text import RESUME_SECTIONS, js_trim, resume_to_text
from apps.runs import approvals as approval_service
from apps.runs import lifecycle
from apps.runs.catalogue import ApprovalKind, CompanyAddInput, RunKind, RunStatus, StoredDecision
from apps.runs.models import Approval, Run

__all__ = [
    "CHAT_TOOLSET",
    "FAILED_ON_OUR_SIDE",
    "FOLLOW_UP_TOOLS",
    "INPUT_MODELS",
    "OUTPUT_MODELS",
    "TOOL_NAMES",
    "UNTRUSTED_JOB",
    "chat_tool",
    "failure",
    "guard",
    "format_card_salary",
    "format_job_salary",
    "job_card",
    "proposal_prompt",
]

log = logging.getLogger("foray.chat")

FAILED_ON_OUR_SIDE: Final = "That step failed on our side."
# The chat step cap (§7.8). A tool may be retried with fixed input this many times in a turn, so
# invalid input never ends a turn before the step cap does (the AI SDK had no per-tool limit).
TOOL_RETRIES: Final = 8

FOLLOW_UP_TOOLS: Final = (
    "get_profile_summary",
    "get_resume",
    "get_run_status",
    "list_jobs",
    "get_job",
    "list_companies",
)

# `RunContext[ChatDeps]` (`agent.ChatDeps`: user_id, conversation_id, follow_up); the agent
# module imports this one, so the deps are typed loosely here.
Ctx = RunContext[Any]
type Output = dict[str, Any]

CHAT_TOOLSET: FunctionToolset[Any] = FunctionToolset(max_retries=TOOL_RETRIES)


# --- the error wrapper ---------------------------------------------------------------------------


def failure(message: str, **extra: Any) -> Output:
    """`{ok: false, message}`, with any keys a tool's failure also carries (before `message`)."""
    return {"ok": False, **extra, "message": message}


type ToolFn = Callable[[Ctx, Any], Awaitable[Output]]


def guard(fn: ToolFn) -> ToolFn:
    """The one error wrapper (see the module docstring)."""

    @functools.wraps(fn)
    async def guarded(ctx: Ctx, args: Any) -> Output:
        try:
            return await fn(ctx, args)
        except ApiError as error:
            return failure(error.message)
        except UserFacingError as error:  # BudgetExceeded included: its user_message
            return failure(getattr(error, "user_message", None) or str(error))
        except Exception:
            log.exception("chat tool %s failed", fn.__name__)
            return failure(FAILED_ON_OUR_SIDE)

    return guarded


def chat_tool(
    description: str, *, toolset: FunctionToolset[Any] = CHAT_TOOLSET
) -> Callable[[ToolFn], ToolFn]:
    """Register an `async def tool(ctx, args)` on `toolset` (the agent's, unless a test brings
    its own) behind the one error wrapper."""

    def deco(fn: ToolFn) -> ToolFn:
        guarded = guard(fn)
        toolset.add_function(guarded, takes_ctx=True, name=fn.__name__, description=description)
        return guarded

    return deco


def _user(ctx: Ctx) -> str:
    return ctx.deps.user_id


def _conversation(ctx: Ctx) -> uuid.UUID | None:
    return ctx.deps.conversation_id


# --- input models --------------------------------------------------------------------------------


class _Input(BaseModel):
    # No class docstrings: pydantic would send them to the model as schema descriptions.
    model_config = ConfigDict(extra="forbid")


Section = Literal["basics", "summary", "work", "education", "projects", "skills"]
Mode = Literal["approve_each", "autopilot"]


class GetProfileSummaryInput(_Input):
    pass


class GetResumeInput(_Input):
    sections: list[Section] | None = Field(
        description="Limit to these sections. Null reads the whole resume, which is usually what "
        "you want."
    )


class SalaryFloorInput(_Input):
    amount: JsNumber  # 150000 stays an int, as JavaScript stored it
    currency: str
    period: Literal["year", "hour"]


class UpdateProfilePreferencesInput(_Input):
    # Every field nullable and none optional: null means "leave what is stored alone", so the
    # agent can record two answers from one message without clobbering the other slots.
    target_roles: list[str] | None
    seniority: Seniority | None
    locations: list[str] | None
    remote_pref: RemotePreference | None
    salary_floor: SalaryFloorInput | None
    work_auth: str | None
    start_date: str | None
    must_haves: list[str] | None
    dealbreakers: list[str] | None
    # Slots the user declined, so the agent stops asking.
    declined_slots: list[SlotId] | None


class AddCompanyInput(_Input):
    name_or_url: str = Field(description='A company name ("Stripe"), a domain, or a job-board URL.')


class DiscoverCompaniesInput(_Input):
    description: str
    count: int = Field(ge=1, le=25, description="How many companies to look for. Keep it modest.")


class ListCompaniesInput(_Input):
    limit: Annotated[int, Field(ge=1, le=50)] | None


class StartSearchRunInput(_Input):
    prompt: str = Field(description="What the user asked for, in their own words.")
    companies: list[str] | None = Field(
        description='Companies to limit the search to, when the user names any ("at '
        'Anthropic"). Unknown companies are looked up and added automatically.'
    )
    roles: list[str] | None = Field(
        description='Kinds of role for this search, as short titles ("software engineer").'
    )
    seniority: Seniority | None = Field(
        description='Level for this search when the user states one. "Entry-level", "junior" or '
        '"most entry-level" is entry.'
    )
    locations: list[str] | None = Field(
        description="Places for this search, when the user names any."
    )
    remote: RemotePreference | None = Field(
        description="Remote preference for this search, only when the user states one."
    )
    focus: str | None = Field(
        description="Anything else about this search that should shape the ranking, in one "
        'sentence ("lowest experience requirements first").'
    )
    mode: Mode | None = Field(
        description="approve_each pauses before deep evaluation and before shortlisting; "
        "autopilot does both without asking. Null uses the user's default from settings — only "
        "set it when they say which they want."
    )


class GetRunStatusInput(_Input):
    run_id: str | None


class CancelRunInput(_Input):
    run_id: str


class ListJobsInput(_Input):
    run_id: str | None = Field(description="Only the roles this search run found.")
    limit: Annotated[int, Field(ge=1, le=50)] | None
    stage: StageName | None
    min_score: Annotated[int, Field(ge=0, le=100)] | None
    query: str | None = Field(description="Text to match in title or company.")


class GetJobInput(_Input):
    id: str = Field(description="A matchId or jobId.")


class ListPendingApprovalsInput(_Input):
    pass


class AnswerApprovalInput(_Input):
    approval_id: str = Field(description="The approval's id.")
    approve: bool
    item_ids: list[str] | None = Field(
        description="With approve=true: only these items. Null means every item. Ignored when "
        "declining."
    )


class MoveJobStageInput(_Input):
    id: str = Field(description="A matchId or jobId.")
    stage: StageName
    note: str | None = Field(
        description="A short note to add to the role's notes (e.g. who they spoke to), or null."
    )


class EvaluateJobInput(_Input):
    id: str = Field(description="A matchId or jobId.")


class TailorResumeInput(_Input):
    id: str = Field(description="A matchId or jobId.")


# --- output models (documentation for the generated TypeScript; see the module docstring) -----


class _Output(ApiModel):
    model_config = ConfigDict(extra="forbid")


class ResumeCounts(_Output):
    work: int
    education: int
    skills: int
    projects: int


class GetProfileSummaryOutput(_Output):
    has_resume: bool
    counts: ResumeCounts | None = None
    note: str | None = None
    preferences: dict[str, Any]  # the stored snake_case preferences, as they are
    known_preferences: list[str]


class GetResumeOutput(_Output):
    ok: bool
    version: int | None = None
    resume: str | None = None
    message: str | None = None


class UpdateProfilePreferencesOutput(_Output):
    ok: bool
    saved: dict[str, Any] | None = None  # the merged snake_case preferences
    answered: int | None = None
    total: int | None = None
    still_needed: list[str] | None = None
    blocking_search: list[str] | None = None
    message: str | None = None


class RunStartedToolOutput(_Output):
    # add_company
    ok: bool
    run_id: str | None = None
    message: str


class DiscoverCompaniesOutput(_Output):
    ok: bool
    run_id: str | None = None
    queued: list[str] | None = None
    message: str


class CompanySample(_Output):
    name: str
    source: str
    last_fetched: str | None
    jobs: int | None


class ListCompaniesOutput(_Output):
    counts: dict[str, int]
    sample: list[CompanySample]


class StartSearchRunOutput(_Output):
    ok: bool
    run_id: str | None = None
    mode: Mode | None = None
    searching_for: str | None = None
    already_running: bool | None = None
    needs: list[str] | None = None
    message: str


class GetRunStatusOutput(_Output):
    ok: bool
    run_id: str | None = None
    kind: str | None = None
    status: str | None = None
    mode: str | None = None
    stats: dict[str, Any] | None = None
    output: Any = None
    error: str | None = None
    message: str | None = None


class CancelRunOutput(_Output):
    ok: bool
    message: str


class JobCard(_Output):
    match_id: str
    job_id: str
    title: str
    company: str
    locations: list[str]
    remote: str
    stage: str
    score: int | None
    fast_score: int | None
    deep_score: int | None
    recommendation: str | None
    headline: str | None
    salary: str | None
    posted_at: str | None
    url: str


class ListJobsOutput(_Output):
    ok: bool | None = None
    count: int | None = None
    jobs: list[JobCard] | None = None
    message: str | None = None


class JobMatchView(_Output):
    stage: str
    fast_score: int | None
    fast_eval: dict[str, Any] | None  # FastScore, snake_case keys as stored
    deep_score: int | None
    deep_eval: dict[str, Any] | None  # DeepEval, snake_case keys as stored
    notes: str | None
    tailored_resume_id: str | None


class GetJobOutput(_Output):
    ok: bool
    match_id: str | None = None
    job_id: str | None = None
    title: str | None = None
    company: str | None = None
    locations: list[str] | None = None
    remote: str | None = None
    salary: str | None = None
    posted_at: str | None = None
    is_open: bool | None = None
    url: str | None = None
    apply_url: str | None = None
    match: JobMatchView | None = None
    description: str | None = None  # wrapped in <untrusted source="job posting">
    message: str | None = None


class PendingApprovalView(_Output):
    id: str
    kind: str
    title: str
    run_id: str
    created_at: str
    items: list[dict[str, Any]]  # DeepEvalBatchItem | ShortlistItem, camelCase
    estimate: dict[str, Any] | None  # CostEstimate, camelCase


class ListPendingApprovalsOutput(_Output):
    approvals: list[PendingApprovalView]


class AnswerApprovalOutput(_Output):
    ok: bool
    message: str


class MoveJobStageOutput(_Output):
    ok: bool
    match_id: str | None
    stage: StageName | None
    message: str


class MatchRunOutput(_Output):
    # evaluate_job, tailor_resume
    ok: bool
    run_id: str | None = None
    match_id: str | None = None
    message: str


INPUT_MODELS: Final[dict[str, type[BaseModel]]] = {}
OUTPUT_MODELS: Final[dict[str, type[ApiModel]]] = {
    "get_profile_summary": GetProfileSummaryOutput,
    "get_resume": GetResumeOutput,
    "update_profile_preferences": UpdateProfilePreferencesOutput,
    "add_company": RunStartedToolOutput,
    "discover_companies": DiscoverCompaniesOutput,
    "list_companies": ListCompaniesOutput,
    "start_search_run": StartSearchRunOutput,
    "get_run_status": GetRunStatusOutput,
    "cancel_run": CancelRunOutput,
    "list_jobs": ListJobsOutput,
    "get_job": GetJobOutput,
    "list_pending_approvals": ListPendingApprovalsOutput,
    "answer_approval": AnswerApprovalOutput,
    "move_job_stage": MoveJobStageOutput,
    "evaluate_job": MatchRunOutput,
    "tailor_resume": MatchRunOutput,
}


def _id(value: Any) -> str | None:
    return None if value is None else str(value)


# --- profile -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Summary:
    counts: dict[str, int] | None
    preferences: dict[str, Any]


def _profile_summary(user_id: str) -> _Summary:
    stored = User.objects.only("preferences").get(pk=user_id).preferences
    preferences = stored if isinstance(stored, dict) else {}
    base = latest_base_resume(user_id)  # invariant 13: the latest base resume
    if base is None:
        return _Summary(None, preferences)
    resume = base.resume
    counts = {
        "work": len(resume.work),
        "education": len(resume.education),
        "skills": len(resume.skills),
        "projects": len(resume.projects),
    }
    return _Summary(counts, preferences)


@chat_tool(
    "How complete the user's profile is, and what they have said they are looking for. This "
    "reports counts, NOT resume content — to read what their resume actually says, use "
    "get_resume."
)
async def get_profile_summary(ctx: Ctx, args: GetProfileSummaryInput) -> Output:
    summary = await db(_profile_summary, _user(ctx))
    # Preferences are returned even before a resume: they can now exist first.
    if summary.counts is None:
        return {
            "hasResume": False,
            "note": "No resume yet. The user still needs to upload one on Home or Profile.",
            "preferences": summary.preferences,
            "knownPreferences": list(summary.preferences),
        }
    return {
        "hasResume": True,
        "counts": summary.counts,
        "preferences": summary.preferences,
        "knownPreferences": list(summary.preferences),
    }


def _resume(user_id: str, sections: Sequence[str] | None) -> Output:
    row = latest_base_resume_row(user_id)
    if row is None:
        return failure("No resume on file. The user needs to upload one on the profile page.")
    parsed = resume_schema.Resume.read(row.data)
    if parsed is None:
        return failure("The stored resume could not be read; it may need re-uploading.")
    wanted = sections if sections else RESUME_SECTIONS
    # Prose rather than raw JSON: the model reads this, and a resume is prose. Ids stay inline
    # so it can cite specific facts.
    return {"ok": True, "version": row.version, "resume": resume_to_text(parsed, wanted)}


@chat_tool(
    "Read the user's actual resume — their roles, bullets, education, projects and skills as "
    "written. Call this before saying anything about their background, judging whether a job "
    "fits them, or drafting anything grounded in their experience. get_profile_summary only "
    "reports how much is on file, not what it says."
)
async def get_resume(ctx: Ctx, args: GetResumeInput) -> Output:
    return await db(_resume, _user(ctx), args.sections)


_PREFERENCE_FIELDS: Final = tuple(Preferences.model_fields)


def _update_preferences(user_id: str, args: UpdateProfilePreferencesInput) -> Output:
    given = args.model_dump(mode="json")
    with transaction.atomic():
        row = (
            User.objects.select_for_update().only("preferences", "interview_state").get(pk=user_id)
        )
        current = Preferences.read(row.preferences).dump()
        state = InterviewState.read(row.interview_state)
        # null means "not answered in this turn", so stored answers survive a turn that only
        # fills one or two slots; an empty list does overwrite.
        answered = {key: given[key] for key in _PREFERENCE_FIELDS if given.get(key) is not None}
        merged = Preferences.read({**current, **answered})
        state.skipped = list(dict.fromkeys([*state.skipped, *(args.declined_slots or [])]))
        User.objects.filter(pk=user_id).update(
            preferences=merged.dump(), interview_state=state.dump(), updated_at=timezone.now()
        )
    progress = interview_progress(merged, state)
    return {
        "ok": True,
        "saved": merged.dump(),
        "answered": progress.answered,
        "total": progress.total,
        "stillNeeded": [slot.label for slot in progress.outstanding],
        "blockingSearch": [slot.label for slot in progress.blocking],
    }


@chat_tool(
    "Record what the user has told you about what they are looking for. Pass only the fields "
    "they just answered; leave the rest null. Use declined_slots for anything they explicitly "
    "do not want to answer."
)
async def update_profile_preferences(ctx: Ctx, args: UpdateProfilePreferencesInput) -> Output:
    # Works before a resume exists: preferences live on the user (§1.2).
    return await db(_update_preferences, _user(ctx), args)


# --- companies -----------------------------------------------------------------------------------


@chat_tool(
    "Register one company's job board so its postings are searched. Accepts a company name, a "
    "website, or a job-board URL. Verifies the board is real before registering it. Runs in "
    "the background: it returns a run id at once, and the chat shows a live card."
)
async def add_company(ctx: Ctx, args: AddCompanyInput) -> Output:
    # A `company_discovery` run (it used to block the reply for tens of seconds), throttled with
    # the endpoint's `company-add:<user>` count; it reports back in this chat when it finishes.
    started = await db(
        company_pipeline.start_add,
        _user(ctx),
        args.name_or_url,
        conversation_id=_conversation(ctx),
    )
    return {"ok": True, "runId": str(started.run.id), "message": started.message}


class ProposedCompany(BaseModel):
    name: str
    why: str = Field(description="One short clause on why this company fits.")


class ProposedCompanies(BaseModel):
    companies: list[ProposedCompany]


PROPOSAL_INSTRUCTIONS: Final = (
    "You name real companies matching a description. Only companies you are confident actually "
    "exist. No descriptions of what they might be called — exact names as they are commonly "
    "written."
)


def proposal_prompt(description: str, count: int) -> str:
    return f"Name up to {count} companies matching: {description}"


@chat_tool(
    "Find and register several companies matching a description, e.g. 'AI infrastructure "
    "startups in SF hiring new grads'. Runs in the background; progress appears in the activity "
    "feed."
)
async def discover_companies(ctx: Ctx, args: DiscoverCompaniesInput) -> Output:
    # The model proposes names (the agent thinking, so it stays in the turn); discovery decides
    # whether any of them are real. Nothing is registered on the strength of a suggestion.
    proposed = await structured(
        "chat",
        ProposedCompanies,
        instructions=PROPOSAL_INSTRUCTIONS,
        prompt=proposal_prompt(args.description, args.count),
    )
    names = [company.name for company in proposed.companies[: args.count]]
    if not names:
        return failure("Could not think of matching companies.")
    started = await db(
        company_pipeline.start_names, _user(ctx), names, conversation_id=_conversation(ctx)
    )
    value = CompanyAddInput.read(started.run.input)
    return {
        "ok": True,
        "runId": str(started.run.id),
        "queued": list(value.names or []) if value is not None else names,
        "message": started.message,
    }


def _companies(limit: int | None) -> Output:
    sample: list[Company] = company_registry.list_companies(status="active", limit=limit or 20)
    return {
        "counts": company_registry.count_by_status(),
        "sample": [
            {
                "name": company.name,
                "source": company.source_type,
                "lastFetched": js_iso(company.last_fetched_at),
                "jobs": company.last_fetch_job_count,
            }
            for company in sample
        ],
    }


@chat_tool("Summarise which companies are registered and searchable, with counts by status.")
async def list_companies(ctx: Ctx, args: ListCompaniesInput) -> Output:
    # The registry is shared, not per user.
    return await db(_companies, args.limit)


# --- searching -----------------------------------------------------------------------------------

ALREADY_WAITING: Final = (
    "A search is already waiting on the user's go-ahead; this is its card. Do not start another."
)
ALREADY_RUNNING: Final = "A search is already running; this is its card. Do not start another."
STARTED_AUTOPILOT: Final = (
    "Search started on autopilot. It will score, evaluate and shortlist without stopping."
)
STARTED_APPROVE_EACH: Final = (
    "Search started. It will pause for approval before the deep evaluation and before shortlisting."
)


def _start_search(
    user_id: str, conversation_id: uuid.UUID | None, args: StartSearchRunInput
) -> Output:
    try:
        profile = search.assert_can_search(user_id)
    except search.PreferencesIncompleteError as error:
        # A search on incomplete preferences looks plausible and is mostly unusable.
        return {
            "ok": False,
            "needs": error.missing,
            "message": f"Cannot search yet — still need: {', '.join(error.missing)}. "
            "Ask for those first, then start the search.",
        }
    hints = clean_brief(
        {
            "companies": args.companies,
            "roles": args.roles,
            "seniority": args.seniority,
            "locations": args.locations,
            "remote": args.remote,
            "focus": args.focus,
        }
    )
    mode = args.mode or profile.settings.default_mode
    started = search.start_search(
        user_id,
        prompt=args.prompt,
        hints=hints.dump(),
        mode=mode,
        conversation_id=conversation_id,
    )
    run = started.run
    if started.reused:  # one active search per user (R2): its card comes back
        return {
            "ok": True,
            "runId": str(run.id),
            "mode": run.mode,
            "alreadyRunning": True,
            "message": ALREADY_WAITING
            if run.status == RunStatus.AWAITING_APPROVAL
            else ALREADY_RUNNING,
        }
    return {
        "ok": True,
        "runId": str(run.id),
        "mode": mode,
        "searchingFor": describe_brief(hints),
        "message": STARTED_AUTOPILOT if mode == "autopilot" else STARTED_APPROVE_EACH,
    }


@chat_tool(
    "Start a background search: refresh job boards, filter, score every candidate against the "
    "user's resume, then deep-evaluate the best. The search follows what the user asked for "
    "this time: fill companies, roles, seniority, locations, remote and focus from their "
    "request, and leave a field null to use their saved preference. These apply to this search "
    "only and never change saved preferences (use update_profile_preferences for lasting "
    "changes). Returns a run id immediately; the chat shows a live card for it. Refuses if "
    "essential preferences are not yet known."
)
async def start_search_run(ctx: Ctx, args: StartSearchRunInput) -> Output:
    return await db(_start_search, _user(ctx), _conversation(ctx), args)


def _user_run(user_id: str, run_id: str | None) -> Run | None:
    if run_id is None:
        return Run.objects.for_user(user_id).order_by("-created_at").first()
    parsed = lifecycle.as_uuid(run_id)
    if parsed is None:
        return None
    return Run.objects.for_user(user_id).filter(id=parsed).first()


def _run_status(user_id: str, run_id: str | None) -> Output:
    # An empty id reads as "the most recent run", as JavaScript's falsy check did.
    run = _user_run(user_id, run_id or None)
    if run is None:
        return failure("No runs yet.")
    return {
        "ok": True,
        "runId": str(run.id),
        "kind": run.kind,
        "status": run.status,
        "mode": run.mode,
        "stats": run.stats,
        "output": run.output,
        "error": run.error,
    }


@chat_tool(
    "Check a background run: its status, progress counters and result. Pass null for run_id "
    "to get the most recent run."
)
async def get_run_status(ctx: Ctx, args: GetRunStatusInput) -> Output:
    return await db(_run_status, _user(ctx), args.run_id)


@chat_tool("Stop a running background run. Work already done is kept.")
async def cancel_run(ctx: Ctx, args: CancelRunInput) -> Output:
    # One way to stop anything: lifecycle.cancel emits the run's one run.cancelled (§9.9).
    if await db(lifecycle.cancel, _user(ctx), args.run_id):
        return {"ok": True, "message": "Cancelled. Everything found so far was kept."}
    return failure("That run is not running.")


# --- matches -------------------------------------------------------------------------------------


def format_card_salary(
    salary_min: float | None,
    salary_max: float | None,
    currency: str | None,
    period: str | None,
) -> str | None:
    """`formatSalary` (`search.ts`): "$150k–200k", "EUR 90k–120k", "$55–70/hour"."""

    def fmt(n: float) -> str:
        # `Math.round` rounds half up (toward +∞), unlike Python's round-half-even.
        if n >= 1000:
            return f"{int(_js_round(n / 1000))}k"
        return str(int(_js_round(n)))

    if not salary_min and not salary_max:
        return None
    if salary_min and salary_max and salary_min != salary_max:
        amount = f"{fmt(salary_min)}–{fmt(salary_max)}"
    else:
        amount = fmt(salary_min if salary_min is not None else cast(float, salary_max))
    symbol = "$" if not currency or currency == "USD" else f"{currency} "
    suffix = f"/{period}" if period and period != "year" else ""
    return f"{symbol}{amount}{suffix}"


def _js_round(value: float) -> float:
    return math.floor(value + 0.5)


def job_card(row: MatchRow) -> Output:
    """The compact shape every job-listing tool returns, so the UI renders one card."""
    return {
        "matchId": str(row.match_id),
        "jobId": row.job_id,
        "title": row.title,
        "company": row.company,
        "locations": list(row.locations),
        "remote": row.remote,
        "stage": row.stage,
        "score": row.score,
        "fastScore": row.fast_score,
        "deepScore": row.deep_score,
        "recommendation": row.recommendation,
        "headline": row.headline,
        "salary": format_card_salary(
            row.salary_min, row.salary_max, row.salary_currency, row.salary_period
        ),
        "postedAt": js_iso(row.posted_at),
        "url": row.url,
    }


def _list_jobs(user_id: str, args: ListJobsInput) -> Output:
    job_ids: list[str] | None = None
    if args.run_id:
        run = _user_run(user_id, args.run_id)
        if run is None or run.kind != RunKind.SEARCH:
            return failure("No such search run.")
        found = run.input.get("candidateJobIds") if isinstance(run.input, dict) else None
        job_ids = [i for i in found if isinstance(i, str)] if isinstance(found, list) else []
    rows = match_queries.list_matches(
        user_id,
        job_ids=job_ids,
        limit=args.limit or 10,
        stages=[args.stage] if args.stage else match_queries.OPEN_STAGES,
        min_score=args.min_score,
        query=args.query,
    )
    return {"count": len(rows), "jobs": [job_card(row) for row in rows]}


@chat_tool(
    "List the user's matched roles, best first (deep score, else fast score). Filter by "
    "pipeline stage, a minimum score, or text in the title/company. Pass run_id to list only "
    "what one search found; without it this lists matches from every search. By default "
    "excludes rejected and archived matches."
)
async def list_jobs(ctx: Ctx, args: ListJobsInput) -> Output:
    return await db(_list_jobs, _user(ctx), args)


UNTRUSTED_JOB: Final = '<untrusted source="job posting">{}</untrusted>'
DESCRIPTION_MAX: Final = 8_000  # a chat turn does not need 20,000 characters


def format_job_salary(
    salary_min: float | None,
    salary_max: float | None,
    currency: str | None,
    period: str | None,
) -> str | None:
    """`get_job`'s salary: "USD 150000–200000 per year", each bound as JavaScript printed it."""
    if salary_min is None and salary_max is None:
        return None
    low = js_num(salary_min) if salary_min is not None else "?"
    high = js_num(salary_max) if salary_max is not None else "?"
    return f"{currency or ''} {low}–{high} per {period or '?'}".strip()


def _get_job(user_id: str, wanted: str) -> Output:
    match = match_queries.resolve_match(user_id, wanted)
    job_id = match.job_id if match is not None else wanted
    job = Job.objects.filter(id=job_id).first()
    if job is None:
        return failure("No such job.")
    own = match or match_queries.get_match_by_job(user_id, job.id)
    text = job.description_text
    return {
        "ok": True,
        "matchId": _id(own.id) if own is not None else None,
        "jobId": job.id,
        "title": job.title,
        "company": job.company_name,
        "locations": list(job.locations),
        "remote": job.remote,
        "salary": format_job_salary(
            job.salary_min, job.salary_max, job.salary_currency, job.salary_period
        ),
        "postedAt": js_iso(job.posted_at),
        "isOpen": job.is_active,
        "url": job.url,
        "applyUrl": job.apply_url,
        "match": {
            "stage": own.stage,
            "fastScore": own.fast_score,
            "fastEval": own.fast_eval,
            "deepScore": own.deep_score,
            "deepEval": own.deep_eval,
            "notes": own.notes,
            "tailoredResumeId": _id(own.tailored_resume_id),
        }
        if own is not None
        else None,
        # Third-party text: wrapped, so the agent reads it as data (§7.2).
        "description": None if text is None else UNTRUSTED_JOB.format(text[:DESCRIPTION_MAX]),
    }


@chat_tool(
    "Read one job posting in full — its cleaned description, plus this user's match for it "
    "(stage, scores, and the deep evaluation's evidence and gaps when there is one). Accepts a "
    "match id or a job id."
)
async def get_job(ctx: Ctx, args: GetJobInput) -> Output:
    return await db(_get_job, _user(ctx), args.id)


# --- approvals -----------------------------------------------------------------------------------


def _payload_items(payload: Any) -> list[Any]:
    items = payload.get("items") if isinstance(payload, Mapping) else None
    return items if isinstance(items, list) else []


def _pending(user_id: str) -> Output:
    rows = approval_service.list_pending(user_id)
    return {
        "approvals": [
            {
                "id": str(row.id),
                "kind": row.kind,
                "title": row.title,
                "runId": str(row.run_id),
                "createdAt": js_iso(row.created_at),
                "items": _payload_items(row.payload),
                "estimate": row.payload.get("estimate")
                if isinstance(row.payload, Mapping)
                else None,
            }
            for row in rows
        ]
    }


@chat_tool(
    "List what is waiting on the user's answer. deep_eval_batch: a search paused before "
    "deep-evaluating its best roles with the strong model — items are the roles, estimate is "
    "the cost in USD. shortlist_proposal: roles the deep evaluation recommends, waiting to be "
    "moved onto the shortlist. Answer one with answer_approval once the user decides."
)
async def list_pending_approvals(ctx: Ctx, args: ListPendingApprovalsInput) -> Output:
    return await db(_pending, _user(ctx))


def match_items(payload: Any, ids: Sequence[str]) -> list[str]:
    """The payload item ids a set of model-supplied ids refers to, in payload order (a job id
    stands for its match id)."""
    wanted = {js_trim(i) for i in ids}
    return [
        item["matchId"]
        for item in _payload_items(payload)
        if isinstance(item, Mapping)
        and isinstance(item.get("matchId"), str)
        and (item.get("matchId") in wanted or item.get("jobId") in wanted)
    ]


def confirmation(approval: Approval) -> str:
    """What the agent is told after a decision (`approvals.ts`'s `confirmation`)."""
    decision = StoredDecision.read(approval.decision) or StoredDecision(approved=False)
    total = len(_payload_items(approval.payload))
    chosen = len(decision.item_ids) if decision.item_ids is not None else total
    count = (
        f"{chosen} {'role' if chosen == 1 else 'roles'}"
        if chosen == total
        else f"{chosen} of {total} roles"
    )
    if approval.kind == ApprovalKind.DEEP_EVAL_BATCH:
        return (
            f"Approved deep evaluation of {count}. It runs in the background; the search card "
            "shows progress, and the shortlist proposal follows."
            if decision.approved
            else "Skipped the deep evaluation. The search will wrap up without it."
        )
    if approval.kind == ApprovalKind.SHORTLIST_PROPOSAL:
        return (
            f"Approved: shortlisting {count}."
            if decision.approved
            else "Left the shortlist as it was. The search will wrap up."
        )
    return "Approved." if decision.approved else "Declined."


def _answer(user_id: str, args: AnswerApprovalInput) -> Output:
    parsed = lifecycle.as_uuid(args.approval_id)
    approval = (
        Approval.objects.for_user(user_id).filter(id=parsed).first() if parsed is not None else None
    )
    if approval is None:
        return failure(
            "No approval with that id. Call list_pending_approvals for the current ones."
        )
    decision: dict[str, Any] = {"approved": args.approve}
    if args.approve and args.item_ids is not None:
        # Ids that match nothing would count as a rejection, an irreversible answer the user
        # never gave, so that is refused instead.
        chosen = match_items(approval.payload, args.item_ids)
        if not chosen:
            return failure(
                "None of those ids are items of this approval, so nothing was answered. Use the "
                "matchId values from list_pending_approvals."
            )
        decision["itemIds"] = chosen
    try:
        result = approval_service.decide(user_id, approval.id, decision, "chat")  # R6
    except Exception:
        log.exception("chat: deciding approval %s failed", approval.id)
        return failure("Could not resume the run just now; the request is still open. Try again.")
    if not result.ok or result.approval is None:
        if result.outcome == "already_decided":
            return failure("That request was already answered, so nothing changed.")
        if result.outcome == "expired":
            return failure("That request has expired and can no longer be answered.")
        return failure("No approval with that id.")
    return {"ok": True, "message": confirmation(result.approval)}


@chat_tool(
    "Answer a pending approval (from list_pending_approvals) with the user's decision. Only "
    "call this after the user has clearly said yes or no to it in this conversation — never "
    "decide for them, and ask if it is ambiguous. approve=true with item_ids approves just "
    "those items (each item's matchId; a jobId also works); item_ids=null approves all of them. "
    "approve=false declines the whole request. Approving deep evaluation spends a few cents per "
    "role; the paused search then resumes in the background. An approval can only be answered "
    "once."
)
async def answer_approval(ctx: Ctx, args: AnswerApprovalInput) -> Output:
    return await db(_answer, _user(ctx), args)


# --- one role ------------------------------------------------------------------------------------


def _label(user_id: str, match_id: uuid.UUID) -> str | None:
    rows = match_queries.get_match_rows(user_id, [match_id])
    return f"{rows[0].title} at {rows[0].company}" if rows else None


def _move(user_id: str, args: MoveJobStageInput) -> Output:
    match = match_queries.resolve_or_create_match(user_id, js_trim(args.id))
    if match is None:
        return failure("No role with that id.", matchId=None, stage=None)
    match_queries.set_match_stage(user_id, match.id, args.stage)
    noted = bool(args.note and js_trim(args.note))
    if noted:
        match_queries.append_match_note(user_id, match.id, cast(str, args.note))
    label = _label(user_id, match.id) or "That role"
    return {
        "ok": True,
        "matchId": str(match.id),
        "stage": args.stage,
        "message": f"Moved {label} to {args.stage}." + (" Note added." if noted else ""),
    }


@chat_tool(
    "Move one of the user's roles to a pipeline stage — shortlisted, applied, interviewing, "
    "offer, rejected (they or the company said no), archived (not interested), or back to "
    "found. Use when they say they applied, got an interview, want to shortlist or drop a role. "
    "Accepts a matchId or jobId. Optionally appends a note to the role's notes."
)
async def move_job_stage(ctx: Ctx, args: MoveJobStageInput) -> Output:
    return await db(_move, _user(ctx), args)


def _evaluate(user_id: str, conversation_id: uuid.UUID | None, wanted: str) -> Output:
    match = match_queries.resolve_or_create_match(user_id, js_trim(wanted))
    if match is None:
        return failure("No role with that id.", matchId=None)
    limit = throttle.EVALUATE  # shared with POST /api/matches/{id}/evaluate (§4.5)
    throttle.throttle(f"evaluate:{user_id}", limit.limit, limit.window_s)
    try:
        started = start_match_evaluation(user_id, [match.id], conversation_id=conversation_id)
    except (NoResumeError, UnknownMatchError) as error:
        return failure(str(error), matchId=str(match.id))
    row = started.matches[0]
    label = f"{row.title} at {row.company}"
    if started.reused:
        message = f"{label} is already being evaluated; the card shows its progress."
    else:
        cost = cost_phrase(estimate_eval_cost(1).per_item_usd)
        message = (
            f"Evaluating {label} in the background{f' ({cost})' if cost else ''}. "
            "The card shows progress; the result is saved on the role."
        )
    return {"ok": True, "runId": str(started.run.id), "matchId": str(match.id), "message": message}


@chat_tool(
    "Deep-evaluate one role against the user's full resume with the strong model: a 0–100 "
    "score, a recommendation (strong / apply / maybe / skip), the requirements they meet with "
    "evidence from their resume, gaps, seniority/location/salary fit, risks and talking points. "
    "Costs a few cents and runs in the background for about half a minute; the chat shows a "
    "live card, and get_job returns the result once it is done. Accepts a matchId or jobId. If "
    "get_job already shows a deep evaluation, only re-run it when the user asks or their resume "
    "changed."
)
async def evaluate_job(ctx: Ctx, args: EvaluateJobInput) -> Output:
    return await db(_evaluate, _user(ctx), _conversation(ctx), args.id)


@chat_tool(
    "Tailor the user's resume to one job: select, reorder and reword what their resume already "
    "says for that posting, verify every line against the original, and render a PDF. Never "
    "adds skills or experience. Accepts a match id or a job id (from list_jobs or get_job). "
    "Takes about a minute; returns a run id immediately and the chat shows its progress."
)
async def tailor_resume(ctx: Ctx, args: TailorResumeInput) -> Output:
    # start_tailor_run counts `tailor:<user>` itself, shared with the endpoint.
    started = await db(
        resume_pipeline.start_tailor_run,
        _user(ctx),
        args.id,
        conversation_id=_conversation(ctx),
    )
    return started.as_dict()


TOOL_NAMES: Final = tuple(CHAT_TOOLSET.tools)
INPUT_MODELS.update(
    {
        "get_profile_summary": GetProfileSummaryInput,
        "get_resume": GetResumeInput,
        "update_profile_preferences": UpdateProfilePreferencesInput,
        "add_company": AddCompanyInput,
        "discover_companies": DiscoverCompaniesInput,
        "list_companies": ListCompaniesInput,
        "start_search_run": StartSearchRunInput,
        "get_run_status": GetRunStatusInput,
        "cancel_run": CancelRunInput,
        "list_jobs": ListJobsInput,
        "get_job": GetJobInput,
        "list_pending_approvals": ListPendingApprovalsInput,
        "answer_approval": AnswerApprovalInput,
        "move_job_stage": MoveJobStageInput,
        "evaluate_job": EvaluateJobInput,
        "tailor_resume": TailorResumeInput,
    }
)
