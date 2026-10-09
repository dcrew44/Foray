"""The page view models (design §5.3; `map/next-frontend.md` §3, `map/_gaps.md` §3.7).

Each Next page makes one call, and each function here answers it from as few reads as the page
needs (the query budgets in `tests/pages/test_pages.py`). What the TypeScript pages derived on the
server moves here with today's wording: Home's headline and suggestions (`describeState`), the
chat page's starting prompts (`suggestionsFor`) and the activity page's run targets and costs
(`describeRuns`). Every read is scoped to the signed-in user, except the company registry, which
is the shared mirror.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from django.db.models import Avg, Count, F, Q, Sum
from django.db.models.functions import Now, TruncDate

from apps.accounts.api import SettingsOut
from apps.accounts.models import User
from apps.accounts.profile import SLOTS, OnboardingState, onboarding_state
from apps.accounts.schemas import Settings
from apps.core.jsonio import js_locale
from apps.jobs.models import Company, CompanyStatus
from apps.jobs.pipeline import CompanyRow, company_row
from apps.jobs.sources.jsearch import jsearch_budget
from apps.llm import registry
from apps.llm.models import LlmCall
from apps.llm.pricing import rate_for
from apps.llm.reasoning import effort_for
from apps.matching import queries as match_queries
from apps.matching.deep_eval import estimate_eval_cost
from apps.matching.models import JobMatch
from apps.matching.queries import ALL_STAGES, OPEN_STAGES, MatchRow
from apps.matching.schemas import DeepEval, FastScore
from apps.pages.schemas import (
    ActivityPage,
    AggregatorUsage,
    BaseVersion,
    ChatPage,
    ChatSuggestion,
    CompaniesPage,
    DaySpend,
    HomeHeadline,
    HomePage,
    HomeSpend,
    HomeUser,
    HomeWaiting,
    JobDetail,
    LatestRender,
    MatchDetail,
    MatchDetailPage,
    MatchesPage,
    ModelSpend,
    MonthSpend,
    ProfilePage,
    ProviderKeys,
    PurposeModel,
    PurposeSpend,
    ReviewCost,
    RunSummary,
    SettingsPage,
    SettingsUser,
    StageCounts,
    TailoredView,
    TokenRate,
)
from apps.resumes.models import Resume, ResumeKind
from apps.runs.catalogue import ApprovalStatus, RunKind, RunStatus
from apps.runs.live import to_live_run
from apps.runs.models import Approval, Run

__all__ = [
    "ACTIVITY_LIMIT",
    "BEST_LIMIT",
    "COMPANIES_LIMIT",
    "MATCHES_LIMIT",
    "activity_page",
    "chat_page",
    "chat_suggestions",
    "companies_page",
    "describe_runs",
    "describe_state",
    "home_page",
    "home_suggestions",
    "match_page",
    "matches_page",
    "month_start",
    "plural",
    "profile_page",
    "run_target",
    "settings_page",
]

BEST_LIMIT = 6  # Home's "Best matches"
MATCHES_LIMIT = 1000  # the board and the list
ACTIVITY_LIMIT = 30  # the activity page's runs
COMPANIES_LIMIT = 2000  # the registry table

QUEUE_STAGES: tuple[str, ...] = ("found", "shortlisted")  # what is waiting for a look
STRONG_SCORE = 80
ACTIVE_RUN_STATUSES = (RunStatus.QUEUED, RunStatus.RUNNING)  # Home's "searching right now"


def plural(count: int, one: str, many: str | None = None) -> str:
    """`plural()` from `src/lib/format.ts`: `1 role`, `1,204 roles`."""
    return f"{js_locale(count)} {one if count == 1 else many or f'{one}s'}"


def month_start(now: datetime | None = None) -> datetime:
    """The first of the current month, UTC: where spend is counted from."""
    at = (now or datetime.now(UTC)).astimezone(UTC)
    return datetime(at.year, at.month, 1, tzinfo=UTC)


def _first_name(user: User) -> str:
    return (user.name or "").split(" ")[0]


def _stage_counts(user_id: str) -> StageCounts:
    return StageCounts(**match_queries.count_by_stage(user_id))


# --- /pages/home ---------------------------------------------------------------------------------


def _blocking_labels(onboarding: OnboardingState) -> str:
    return ", ".join(slot.label.lower() for slot in onboarding.progress.blocking)


def describe_state(
    onboarding: OnboardingState,
    counts: StageCounts,
    best: Sequence[MatchRow],
    waiting: HomeWaiting,
    first_name: str,
) -> HomeHeadline:
    """Home's headline: a status report, not a greeting (`describeState`, verbatim)."""
    hi = f"{first_name}, " if first_name else ""
    if not onboarding.has_resume:
        return HomeHeadline(
            headline="Let's set up your search.",
            detail=(
                "Start with your resume. Foray builds your profile from it, asks a few questions "
                "about what you want, then goes looking."
            ),
        )
    if not onboarding.ready:
        return HomeHeadline(
            headline="A few answers and Foray can start searching.",
            detail=(
                f"Still needed: {_blocking_labels(onboarding)}. Answer in chat, or fill them in "
                "on your profile."
            ),
        )
    if waiting.approvals > 0:
        return HomeHeadline(
            headline=(
                f"{hi}one decision is waiting on you."
                if waiting.approvals == 1
                else f"{hi}{waiting.approvals} decisions are waiting on you."
            ),
            detail=None,
        )
    if waiting.running > 0:
        return HomeHeadline(
            headline="Foray is searching right now.",
            detail="Results land below as they come in.",
        )
    strong = sum(1 for row in best if (row.score or 0) >= STRONG_SCORE)
    queue = counts.found + counts.shortlisted
    if queue > 0:
        return HomeHeadline(
            headline=(
                f"{plural(strong, 'strong match', 'strong matches')} waiting for a look."
                if strong > 0
                else f"{plural(queue, 'role')} in your queue."
            ),
            detail=(
                f"{counts.shortlisted} shortlisted, {counts.applied} applied so far."
                if counts.shortlisted > 0
                else None
            ),
        )
    return HomeHeadline(
        headline="Ready when you are.",
        detail="Ask for what you want below, or run a search with your saved preferences.",
    )


def home_suggestions(onboarding: OnboardingState, best: Sequence[MatchRow]) -> list[str]:
    """The ask box's starting prompts: tailoring is suggested for the best match that has no
    tailored resume yet."""
    if onboarding.ready:
        roles = onboarding.preferences.target_roles or []
        untailored = next((row for row in best if row.tailored_resume_id is None), None)
        return [
            f"Find new {roles[0].lower()} roles" if roles and roles[0] else "Find new roles for me",
            "Which of my matches fit me best, and why?",
            f"Tailor my resume for the {untailored.company} role"
            if untailored is not None
            else "What should I improve on my resume?",
        ]
    if onboarding.has_resume:
        return ["Help me set up what I'm looking for", "What does my resume say about me?"]
    return []


def _month_spend_brief(user_id: str, since: datetime) -> HomeSpend:
    totals = (
        LlmCall.objects.for_user(user_id)
        .filter(created_at__gte=since)
        .aggregate(usd=Sum("cost_usd"), calls=Count("id"))
    )
    return HomeSpend(usd=float(totals["usd"] or 0), calls=totals["calls"])


def _waiting(user_id: str) -> HomeWaiting:
    approvals = (
        Approval.objects.for_user(user_id)
        .filter(status=ApprovalStatus.PENDING)
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=Now()))
        .count()
    )
    running = Run.objects.for_user(user_id).filter(status__in=ACTIVE_RUN_STATUSES).count()
    return HomeWaiting(approvals=approvals, running=running)


def home_page(user: User) -> HomePage:
    onboarding = onboarding_state(user)
    counts = _stage_counts(user.pk)
    best = match_queries.list_matches(user.pk, stages=QUEUE_STAGES, limit=BEST_LIMIT)
    waiting = _waiting(user.pk)
    return HomePage(
        user=HomeUser(name=user.name),
        onboarding=onboarding,
        counts=counts,
        best=best,
        spend=_month_spend_brief(user.pk, month_start()),
        waiting=waiting,
        suggestions=home_suggestions(onboarding, best),
        headline=describe_state(onboarding, counts, best, waiting, _first_name(user)),
    )


# --- /pages/matches ------------------------------------------------------------------------------


def matches_page(
    user: User, *, view: str | None, stage: str | None, closed: str | None
) -> MatchesPage:
    """The board shows open matches unless closed ones are asked for; a stage link from Home
    lands on the list, filtered, rather than the board."""
    stage_param = stage if stage in ALL_STAGES else None
    view_name = "list" if view == "list" or stage_param else "board"
    show_closed = closed == "1"
    stages = OPEN_STAGES if view_name == "board" and not show_closed else ALL_STAGES
    return MatchesPage(
        counts=_stage_counts(user.pk),
        matches=match_queries.list_matches(user.pk, stages=stages, limit=MATCHES_LIMIT),
        ready=onboarding_state(user).ready,
        view=view_name,
        stage=cast(Any, stage_param),
        show_closed=show_closed,
    )


# --- /pages/matches/{id} -------------------------------------------------------------------------


def _tailored_view(row: Resume) -> TailoredView:
    changes = row.changes if isinstance(row.changes, list) else []
    return TailoredView(
        id=row.id,
        document_id=row.rendered_document_id,
        created_at=row.created_at,
        changes=[change for change in changes if isinstance(change, dict)],
        verification=row.verification if isinstance(row.verification, dict) else None,
    )


def match_page(user: User, match_id: uuid.UUID) -> MatchDetailPage | None:
    """One match with its posting, its tailored resume and whether a base resume exists; None
    when the match is not this user's."""
    match = JobMatch.objects.for_user(user.pk).select_related("job").filter(id=match_id).first()
    if match is None:
        return None
    job = match.job
    tailored = None
    if match.tailored_resume_id is not None:
        tailored = Resume.objects.for_user(user.pk).filter(id=match.tailored_resume_id).first()
    has_base = Resume.objects.for_user(user.pk).filter(kind=ResumeKind.BASE).exists()
    return MatchDetailPage(
        match=MatchDetail(
            id=match.id,
            stage=cast(Any, match.stage),
            deep_eval=DeepEval.read(match.deep_eval),
            fast_eval=FastScore.read(match.fast_eval),
            tailored_resume_id=match.tailored_resume_id,
            notes=match.notes,
            created_at=match.created_at,
            stage_changed_at=match.stage_changed_at,
            fast_score=match.fast_score,
            deep_score=match.deep_score,
        ),
        job=JobDetail(
            title=job.title,
            company_name=job.company_name,
            locations=list(job.locations),
            remote=cast(Any, job.remote),
            salary_min=job.salary_min,
            salary_max=job.salary_max,
            salary_currency=job.salary_currency,
            salary_period=job.salary_period,
            posted_at=job.posted_at,
            is_active=job.is_active,
            url=job.url,
            apply_url=job.apply_url,
            description_text=job.description_text,
            source=cast(Any, job.source),
        ),
        tailored=_tailored_view(tailored) if tailored is not None else None,
        has_base_resume=has_base,
    )


# --- /pages/activity -----------------------------------------------------------------------------


def _as_input(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def run_target(
    run: Run,
    summary: str | None,
    role_by_id: Mapping[str, str],
    resume_by_id: Mapping[str, Mapping[str, Any]],
) -> str | None:
    """What a run worked on, in words (`target()`). `summary` is the live run's search summary.
    The run-based kinds read the inputs their start functions store; a missing key is None."""
    value = _as_input(run.input)
    match run.kind:
        case RunKind.SEARCH:
            if summary is not None:
                return summary
            prompt = value.get("prompt")
            return prompt if isinstance(prompt, str) else None
        case RunKind.RANK_DEEP:
            ids = value.get("matchIds")
            ids = ids if isinstance(ids, list) else []
            if not ids:
                return None
            first = role_by_id.get(ids[0]) if isinstance(ids[0], str) else None
            if len(ids) == 1:
                return first or "1 role"
            return f"{first} and {len(ids) - 1} more" if first else f"{len(ids)} roles"
        case RunKind.TAILOR:
            match_id = value.get("matchId")
            return role_by_id.get(match_id) if isinstance(match_id, str) else None
        case RunKind.RESUME_RENDER:
            resume_id = value.get("resumeId")
            resume = resume_by_id.get(resume_id) if isinstance(resume_id, str) else None
            if resume is None:
                return None
            if resume["kind"] == ResumeKind.BASE:
                return "Your base resume"
            match_id = resume["match_id"]
            role = role_by_id.get(str(match_id)) if match_id is not None else None
            return f"Tailored for {role}" if role else "A tailored resume"
        case RunKind.PROFILE_IMPORT:
            filename = value.get("filename")
            return filename if isinstance(filename, str) else None
        case RunKind.COMPANY_DISCOVERY:
            query = value.get("query")
            if isinstance(query, str):
                return query
            names = value.get("names")
            if isinstance(names, list):
                return ", ".join(name for name in names if isinstance(name, str))
            return None
        case RunKind.COMPANY_REFRESH:
            if value.get("slug"):
                name = value.get("name")
                return name if isinstance(name, str) else None
            return "Boards not fetched lately" if value.get("stale") else None
        case _:
            return None


def describe_runs(user_id: str, runs: Sequence[Run]) -> list[RunSummary]:
    """Each run with what it was about and what it cost. Runs store ids, not titles, so the
    resumes and matches they point at are read in batches: at most three reads for any number
    of runs."""
    if not runs:
        return []
    inputs = [_as_input(run.input) for run in runs]

    resume_ids = match_queries.valid_uuids(
        value["resumeId"] for value in inputs if isinstance(value.get("resumeId"), str)
    )
    resume_by_id: dict[str, dict[str, Any]] = {}
    if resume_ids:
        rows = Resume.objects.for_user(user_id).filter(id__in=resume_ids)
        for row in rows.values("id", "kind", "match_id"):
            resume_by_id[str(row["id"])] = row

    costs = (
        LlmCall.objects.for_user(user_id)
        .filter(run_id__in=[run.id for run in runs])
        .values("run_id")
        .annotate(usd=Sum("cost_usd"))
        .order_by()
    )
    cost_by_run = {row["run_id"]: float(row["usd"] or 0) for row in costs}

    match_ids: dict[str, None] = {}
    for value in inputs:
        if isinstance(value.get("matchId"), str):
            match_ids[value["matchId"]] = None
        listed = value.get("matchIds")
        if isinstance(listed, list) and listed and isinstance(listed[0], str):
            match_ids[listed[0]] = None
    for row in resume_by_id.values():
        if row["match_id"] is not None:
            match_ids[str(row["match_id"])] = None
    role_by_id = {
        str(row.match_id): f"{row.title} at {row.company}"
        for row in match_queries.get_match_rows(user_id, list(match_ids))
    }

    summaries = []
    for run in runs:
        live = to_live_run(run)
        summaries.append(
            RunSummary(
                run=live,
                target=run_target(run, live.summary, role_by_id, resume_by_id),
                cost_usd=cost_by_run.get(run.id, 0.0),
                conversation_id=run.conversation_id,
            )
        )
    return summaries


def activity_page(user: User) -> ActivityPage:
    runs = list(Run.objects.for_user(user.pk).order_by("-created_at", "-id")[:ACTIVITY_LIMIT])
    return ActivityPage(runs=describe_runs(user.pk, runs))


# --- /pages/chat ---------------------------------------------------------------------------------


def chat_suggestions(user: User) -> list[ChatSuggestion]:
    """Starting points that fit where this user is (`suggestionsFor`)."""
    onboarding = onboarding_state(user)
    if not onboarding.has_resume:
        return [
            ChatSuggestion(
                title="How does this work?",
                prompt="What can you do, and what do you need from me to get started?",
                prefill=False,
            )
        ]
    if not onboarding.ready:
        return [
            ChatSuggestion(
                title="Set up my search",
                prompt="Let's set up my job search. Ask me what you need to know.",
                prefill=False,
            ),
            ChatSuggestion(
                title="What's on my resume?",
                prompt="Summarize my resume and tell me what kinds of roles it fits.",
                prefill=False,
            ),
        ]
    top = next(iter(match_queries.list_matches(user.pk, stages=QUEUE_STAGES, limit=1)), None)
    roles = onboarding.preferences.target_roles or []
    role = roles[0].lower() if roles else "roles"
    places = onboarding.preferences.locations or []
    where = f" in {places[0]}" if places and places[0] else ""
    return [
        ChatSuggestion(
            title="Find new roles", prompt=f"Find new {role}{where} for me.", prefill=False
        ),
        ChatSuggestion(
            title="Compare my best matches",
            prompt="Which of my matches fit me best, and why? Be specific.",
            prefill=False,
        ),
        ChatSuggestion(
            title="Tailor my resume",
            prompt=f"Tailor my resume for the {top.title} role at {top.company}.",
            prefill=False,
        )
        if top is not None
        else ChatSuggestion(
            title="Improve my resume",
            prompt="What would make my resume stronger for the roles I want?",
            prefill=False,
        ),
        ChatSuggestion(
            title="Add a company", prompt="Start tracking this company for me: ", prefill=True
        ),
    ]


def chat_page(user: User) -> ChatPage:
    return ChatPage(suggestions=chat_suggestions(user))


# --- /pages/companies ----------------------------------------------------------------------------

# What `company_row` reads: everything but the careers-page listing cache, which can be large.
_COMPANY_FIELDS = (
    "id",
    "slug",
    "name",
    "domain",
    "source_type",
    "ats_slug",
    "careers_urls",
    "status",
    "needs_browser",
    "tags",
    "last_fetched_at",
    "last_fetch_status",
    "last_fetch_job_count",
    "discovered_via",
)


def companies_page() -> CompaniesPage:
    """The shared registry with each company's open postings, in one read (the page needs a
    signed-in user but is not scoped by one; bug #94)."""
    companies = (
        Company.objects.only(*_COMPANY_FIELDS)
        .annotate(open_jobs=Count("jobs", filter=Q(jobs__is_active=True)))
        .order_by("name", "id")[:COMPANIES_LIMIT]
    )
    rows: list[CompanyRow] = []
    active = 0
    for company in companies:
        open_jobs = int(company.open_jobs)  # pyright: ignore[reportAttributeAccessIssue]
        rows.append(CompanyRow.model_validate(company_row(company, open_jobs)))
        active += company.status == CompanyStatus.ACTIVE
    return CompaniesPage(
        companies=rows, active=active, open_postings=sum(row.active_jobs for row in rows)
    )


# --- /pages/profile ------------------------------------------------------------------------------


def profile_page(user: User) -> ProfilePage:
    onboarding = onboarding_state(user)
    bases = Resume.objects.for_user(user.pk).filter(kind=ResumeKind.BASE).order_by("-version")
    base = bases.values("version", "created_at").first()
    render = (
        bases.filter(rendered_document__isnull=False, rendered_document__user_id=user.pk)
        .values("rendered_document_id", "version")
        .first()
    )
    return ProfilePage(
        has_resume=onboarding.has_resume,
        ready=onboarding.ready,
        resume=onboarding.resume,
        preferences=onboarding.preferences,
        progress=onboarding.progress,
        slots=list(SLOTS),
        base=BaseVersion(version=base["version"], created_at=base["created_at"]) if base else None,
        render=LatestRender(document_id=render["rendered_document_id"], version=render["version"])
        if render
        else None,
    )


# --- /pages/settings -----------------------------------------------------------------------------


def _usd(value: Decimal | None) -> float:
    return float(value or 0)


def settings_page(user: User) -> SettingsPage:
    """Behaviour, this month's spend, the models in use and the aggregator quota. Keys never
    leave the server: only which providers have one."""
    since = month_start()
    calls = LlmCall.objects.for_user(user.pk).filter(created_at__gte=since)
    by_purpose = list(
        calls.values("purpose")
        .annotate(
            usd=Sum("cost_usd"),
            calls=Count("id"),
            input_tokens=Sum(F("input_tokens") + F("cached_input_tokens")),
            output_tokens=Sum("output_tokens"),
        )
        .order_by("-usd", "purpose")
    )
    by_model = (
        calls.values("provider", "model")
        .annotate(
            usd=Sum("cost_usd"),
            calls=Count("id"),
            failures=Count("id", filter=Q(error__isnull=False)),
            avg_latency_ms=Avg("latency_ms"),
        )
        .order_by("-usd", "provider", "model")
    )
    by_day = (
        calls.annotate(day=TruncDate("created_at", tzinfo=UTC))
        .values("day")
        .annotate(usd=Sum("cost_usd"))
        .order_by("day")
    )
    # The month's totals are the purposes' sums (exact: numeric in, float out once).
    total_usd = sum((row["usd"] or Decimal(0) for row in by_purpose), Decimal(0))
    review = estimate_eval_cost(1)
    budget = jsearch_budget()
    purposes = []
    for row in registry.configured_purposes():
        rate = rate_for(row.ref) if row.ref else None
        purposes.append(
            PurposeModel(
                purpose=row.purpose,
                ref=row.ref,
                error=row.error,
                reasoning=effort_for(row.purpose),
                rate=TokenRate(input=rate.input, output=rate.output) if rate else None,
            )
        )
    return SettingsPage(
        user=SettingsUser(name=user.name, email=user.email),
        settings=SettingsOut(**Settings.read(user.settings).model_dump()),
        review=ReviewCost(per_item_usd=review.per_item_usd, model=review.model),
        spend=MonthSpend(
            usd=float(total_usd),
            calls=sum(row["calls"] for row in by_purpose),
            input_tokens=sum(row["input_tokens"] or 0 for row in by_purpose),
            output_tokens=sum(row["output_tokens"] or 0 for row in by_purpose),
        ),
        by_purpose=[
            PurposeSpend(purpose=row["purpose"], usd=_usd(row["usd"]), calls=row["calls"])
            for row in by_purpose
        ],
        by_model=[
            ModelSpend(
                provider=row["provider"],
                model=row["model"],
                usd=_usd(row["usd"]),
                calls=row["calls"],
                failures=row["failures"],
                avg_latency_ms=float(row["avg_latency_ms"] or 0),
            )
            for row in by_model
        ],
        by_day=[DaySpend(day=row["day"], usd=_usd(row["usd"])) for row in by_day],
        month_start=since,
        aggregator=AggregatorUsage(
            used=budget["used"], budget=budget["budget"], enabled=budget["enabled"]
        ),
        purposes=purposes,
        providers=ProviderKeys(present=cast(Any, registry.configured_providers())),
    )
