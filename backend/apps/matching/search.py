"""One search run: refresh, aggregate, filter, record, score, then the first gate (design §9.4;
`map/worker.md` §9; a port of `src/lib/runs/search-run.ts` and the start and finish of
`evaluate-run.ts`).

The shape that matters is a *pipeline with checkpoints*, not an agent loop. Each stage writes its
result and emits an event before the next begins, so a run that is stopped or crashes halfway
has still done, and kept, everything up to that point. The stages are named in `stats.phase`:
starting → refreshing → aggregating → filtering → scoring → awaiting_evaluation | evaluating →
awaiting_shortlist | shortlisting → done.

The `search.run` step ends at the first gate. Deep evaluation costs real money, so in
`approve_each` the run pauses there; the decision defers `search.evaluate` (`evaluate.py`),
which evaluates, raises the shortlist gate and eventually completes the run. On autopilot both
gates approve themselves and the continuations wait on the run's lock.

A search follows its request (R15): everything after the brief reads `apply_brief(saved,
brief)`, never the saved preferences. Stop is checked between stages; the stop itself is
`lifecycle.cancel` (which emits the run's one `run.cancelled`), so the pipeline only returns.
Progress events are written only while the run is running (`note`), so nothing follows a
terminal event (R4).
"""

from __future__ import annotations

import logging
import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from django.db import connection, transaction

from apps.accounts.models import User
from apps.accounts.profile import interview_progress
from apps.accounts.schemas import InterviewState, Preferences, Settings
from apps.core.db import db
from apps.core.errors import UserFacingError
from apps.core.http import HttpClient
from apps.jobs import refresh as refresh_module
from apps.jobs.discovery import discover_company, register_boards_from_apply_links
from apps.jobs.ingest import ingest_jobs
from apps.jobs.models import Company, DiscoveredVia
from apps.jobs.refresh import refresh_companies, stale_cutoff
from apps.jobs.registry import find_companies_by_names, stale_companies
from apps.jobs.sources.jsearch import JSearchDisabledError, JSearchQuotaError, search_jsearch
from apps.matching import queries
from apps.matching.brief import (
    SAVED_PREFERENCES_PROMPT,
    SearchBrief,
    apply_brief,
    describe_brief,
    parse_brief,
    request_text,
)
from apps.matching.deep_eval import cost_phrase, estimate_eval_cost_for, load_eval_candidate
from apps.matching.fast_score import score_matches
from apps.matching.prefilter import CompanyFilter, DerivedCriteria, derive_search, prefilter_jobs
from apps.resumes.queries import latest_base_resume
from apps.resumes.text import js_trim
from apps.runs import catalogue, events, lifecycle
from apps.runs.approvals import request_approval
from apps.runs.catalogue import (
    ApprovalKind,
    EventKind,
    Level,
    Mode,
    RunKind,
    SearchInput,
    SearchOutput,
)
from apps.runs.models import Run

__all__ = [
    "MAX_JSEARCH_REQUESTS",
    "SEARCH_TASK",
    "PreferencesIncompleteError",
    "SearchStart",
    "assert_can_search",
    "complete_search_run",
    "gate_deep_evaluation",
    "join_names",
    "lower_first",
    "note",
    "roles",
    "run_search",
    "search_inputs",
    "start_search",
    "summarize_search_run",
]

log = logging.getLogger("foray.matching")

SEARCH_TASK = "search.run"
DEDUPE_KEY = "search"
# Aggregator pages per run. Each is one request against a small monthly budget: a handful of
# pages is enough to surface companies the registry does not track.
MAX_JSEARCH_REQUESTS = 3


# --- small wording helpers -----------------------------------------------------------------------


def roles(n: int) -> str:
    return f"{n} {'role' if n == 1 else 'roles'}"


def join_names(names: Sequence[str]) -> str:
    """`a`, `a and b`, `a, b and c`."""
    if len(names) <= 1:
        return names[0] if names else ""
    return f"{', '.join(names[:-1])} and {names[-1]}"


def lower_first(text: str) -> str:
    """ "Entry-level roles" reads as "entry-level roles" mid-sentence, but a company or an acronym
    at the start ("iOS roles") keeps its case."""
    if len(text) >= 2 and "A" <= text[0] <= "Z" and "a" <= text[1] <= "z":
        return text[0].lower() + text[1:]
    return text


def capitalize(text: str) -> str:
    return text[:1].upper() + text[1:]


# --- progress ------------------------------------------------------------------------------------

_NOTE = (
    "UPDATE runs SET stats = stats || %s::jsonb, updated_at = now() "
    "WHERE id = %s AND status = 'running' RETURNING user_id"
)


def note(
    run_id: uuid.UUID | str,
    kind: str,
    message: str,
    *,
    stats: Mapping[str, Any] | None = None,
    level: str | None = None,
    data: Any = None,
    approval_id: uuid.UUID | str | None = None,
) -> bool:
    """Progress on a running run, in one transaction: merge `stats`, then emit one event. Only
    while the run is `running`: the run row's lock orders it against a Stop, so no progress
    event ever follows the run's terminal event (R4). Sync; False when the run is not running."""
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(_NOTE, [lifecycle.jsonb(dict(stats or {})), str(run_id)])
            row = cursor.fetchone()
        if row is None:
            return False
        events.emit(
            row[0], kind, message, run_id=run_id, level=level, data=data, approval_id=approval_id
        )
    return True


async def _cancelled(run: Run) -> bool:
    return await db(lifecycle.is_cancelled, run.id)


# --- starting ------------------------------------------------------------------------------------


class PreferencesIncompleteError(UserFacingError):
    """A required preference is not known yet. Searching without, say, work authorisation or a
    location produces a list that looks plausible and is mostly unusable."""

    def __init__(self, missing: Sequence[str]) -> None:
        self.missing = list(missing)
        super().__init__(f"Cannot search yet: {', '.join(self.missing)} not known")


@dataclass(frozen=True, slots=True)
class SearchProfile:
    preferences: Preferences
    settings: Settings
    missing: list[str]  # the labels of required slots still unfilled


def search_profile(user_id: str) -> SearchProfile:
    """The saved preferences and settings, and what still blocks a search. Sync."""
    row = User.objects.only("preferences", "interview_state", "settings").get(pk=user_id)
    preferences = Preferences.read(row.preferences)
    progress = interview_progress(preferences, InterviewState.read(row.interview_state))
    return SearchProfile(
        preferences=preferences,
        settings=Settings.read(row.settings),
        missing=[slot.label for slot in progress.blocking],
    )


def assert_can_search(user_id: str) -> SearchProfile:
    """The gate on a search: every required preference is known. Sync."""
    profile = search_profile(user_id)
    if profile.missing:
        raise PreferencesIncompleteError(profile.missing)
    return profile


@dataclass(frozen=True, slots=True)
class SearchStart:
    run: Run
    reused: bool  # an active search already held the user's one slot: that run came back


def search_inputs(
    user_id: str,
    *,
    prompt: str | None,
    mode: str | None,
    again_from: str | None,
    settings: Settings,
) -> tuple[str, Any, str]:
    """`POST /api/searches`'s inputs (`map/http-surface.md` §4.14): `(prompt, hints, mode)`.

    `again_from` repeats one of the user's earlier searches (anything else is ignored): its
    request and hints, not its brief, which is derived again so a search repeated after a
    profile edit reads the new profile. Sync."""
    earlier: Run | None = None
    run_id = queries.as_uuid(again_from) if again_from else None
    if run_id is not None:
        earlier = Run.objects.for_user(user_id).filter(id=run_id, kind=RunKind.SEARCH).first()
    previous = earlier.input if earlier is not None and isinstance(earlier.input, dict) else {}
    previous_prompt = previous.get("prompt")
    text = js_trim(prompt) if prompt else ""
    chosen_prompt = (
        text
        or (previous_prompt if isinstance(previous_prompt, str) and previous_prompt else "")
        or SAVED_PREFERENCES_PROMPT
    )
    hints = previous.get("hints")
    chosen_mode = mode or (earlier.mode if earlier is not None else None) or settings.default_mode
    return chosen_prompt, hints, chosen_mode


def start_search(
    user_id: str,
    *,
    prompt: str,
    hints: Any = None,
    mode: str = Mode.APPROVE_EACH,
    conversation_id: uuid.UUID | str | None = None,
) -> SearchStart:
    """Insert a queued search and defer `search.run` in one transaction (R1). One active search
    per user (R2): while one is active, that run comes back with `reused=True`. Sync. The
    caller has checked `assert_can_search`."""
    run, reused = lifecycle.start_run(
        user_id,
        RunKind.SEARCH,
        input={"prompt": prompt, "hints": hints},
        mode=mode,
        conversation_id=conversation_id,
        dedupe_key=DEDUPE_KEY,
        task=SEARCH_TASK,
    )
    return SearchStart(run=run, reused=reused)


# --- the step ------------------------------------------------------------------------------------


async def run_search(run: Run) -> None:
    """`search.run`: everything up to the first gate."""
    value = SearchInput.read(run.input)
    prompt = value.prompt
    await db(
        note,
        run.id,
        EventKind.RUN_STARTED,
        f"Searching: {prompt}" if prompt else "Searching for matching roles",
        stats={"phase": "starting"},
    )
    profile = await db(assert_can_search, run.user_id)
    saved = profile.preferences

    # 1. What this search is for, and the title terms that find it. Cached on the run.
    cached = DerivedCriteria.read(value.criteria) if isinstance(value.brief, dict) else None
    if cached is not None:
        brief, criteria = parse_brief(value.brief), cached
    else:
        brief, criteria = await derive_search(saved, request_text(prompt), parse_brief(value.hints))
    await db(lifecycle.merge_input, run.id, brief=brief.dump(), criteria=criteria.dump())

    # Everything downstream reads these, not the saved preferences: a search for entry-level
    # roles must not be filtered or judged as a senior one.
    preferences = apply_brief(saved, brief)
    described = describe_brief(brief)
    looking = (
        f"Looking for {lower_first(described)}."
        if described
        else "Looking for roles that fit your saved preferences."
    )
    await db(
        note,
        run.id,
        EventKind.SEARCH_CRITERIA,
        f"{looking} Titles like: {', '.join(criteria.title_include[:6])}.",
        data={"brief": brief.dump(), "criteria": criteria.dump()},
    )
    if await _cancelled(run):
        return

    async with HttpClient() as client:
        # 2. The companies the request names, registered if they are not tracked yet.
        targets = await resolve_targets(run, brief.companies, client) if brief.companies else None
        company_filter = (
            CompanyFilter(ids=[c.id for c in targets.found], names=list(brief.companies or []))
            if targets is not None
            else None
        )
        if await _cancelled(run):
            return

        # 3. Refresh stale boards, so the search is over current postings.
        await db(lifecycle.merge_stats, run.id, phase="refreshing")
        await refresh_boards(run, targets, client)
        if await _cancelled(run):
            return

        # 4. The aggregator, for companies the registry does not track. A search whose named
        #    companies all have boards has nothing to gain from it.
        if targets is None or targets.missing:
            await db(lifecycle.merge_stats, run.id, phase="aggregating")
            await run_aggregator(run, preferences, criteria, brief, client)
    if await _cancelled(run):
        return

    # 5. Narrow the mirror to what is worth scoring.
    await db(lifecycle.merge_stats, run.id, phase="filtering")
    found = await db(prefilter_jobs, run.user_id, preferences, criteria, company_filter)
    await db(
        note,
        run.id,
        EventKind.PREFILTER_DONE,
        f"{found.selected} of {found.active_in_mirror:,} open postings match "
        f"{'this search' if described else 'your criteria'}"
        + (" (capped)" if found.capped_at else ""),
        stats={"activeInMirror": found.active_in_mirror, "candidates": found.selected},
        data=found.funnel(),
    )

    # 6. Record them as matches, and remember which postings they were.
    await db(record_matches, run.id, run.user_id, found.job_ids)
    if await _cancelled(run):
        return

    # 7. Score them. Cheap tier, one call per posting, triage not judgement.
    await db(lifecycle.merge_stats, run.id, phase="scoring")
    base = await db(latest_base_resume, run.user_id)
    await db(
        note,
        run.id,
        EventKind.SCORING_START,
        f"Scoring {len(found.job_ids)} roles against your profile",
    )

    async def progress(done: int, total: int) -> None:
        await db(
            note,
            run.id,
            EventKind.SCORING_PROGRESS,
            f"Scored {done} of {total}",
            level=Level.DEBUG,
            stats={"scored": done},
        )

    threshold = profile.settings.score_threshold
    scoring = await score_matches(
        run.user_id,
        preferences=preferences,
        focus=brief.focus,
        resume=base.resume if base is not None else None,
        job_ids=found.job_ids,
        threshold=threshold,
        should_stop=lambda: _cancelled(run),
        on_progress=progress,
    )
    await db(
        lifecycle.merge_stats,
        run.id,
        scored=scoring.scored,
        autoArchived=scoring.archived,
        scoringFailed=scoring.failed,
    )
    if await _cancelled(run):
        return
    await db(
        note,
        run.id,
        EventKind.SCORING_DONE,
        scoring_message(
            len(found.job_ids), scoring.scored, scoring.archived, scoring.failed, threshold
        ),
    )

    # 8. The first gate: which matches earn the strong model's time.
    await gate_deep_evaluation(run, profile.settings, found.job_ids, brief)


def scoring_message(
    candidates: int, scored: int, archived: int, failed: int, threshold: int
) -> str:
    if candidates == 0:
        return "Nothing to score."
    if scored == 0 and failed == 0:
        return "Nothing new to score: every one of these was scored before."
    message = f"Scored {scored} {'role' if scored == 1 else 'roles'}"
    if archived > 0:
        message += (
            f"; {archived} came in under {threshold} and {'was' if archived == 1 else 'were'} "
            "archived."
        )
    else:
        message += "."
    if failed > 0:
        message += f" {failed} could not be scored."
    return message


def record_matches(run_id: uuid.UUID, user_id: str, job_ids: list[str]) -> int:
    """Upserts the prefilter's postings as `found` matches and remembers them on the run as "this
    run's matches" (the continuations read them long after this step). Sync."""
    with transaction.atomic():
        created = queries.upsert_matches(user_id, job_ids)
        lifecycle.merge_input(run_id, candidateJobIds=job_ids)
        lifecycle.merge_stats(run_id, matches=len(job_ids), newMatches=created)
    return created


# --- companies, refresh, aggregator --------------------------------------------------------------


@dataclass(slots=True)
class Targets:
    found: list[Company] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


async def resolve_targets(run: Run, names: Sequence[str], client: HttpClient) -> Targets:
    """The registered companies a search is limited to. A name the registry does not track is
    looked up and registered the way "add a company" does, so "roles at X" works the first time
    X is mentioned; one that cannot be found is reported and searched for by name in aggregator
    postings only."""
    lookup = await db(find_companies_by_names, list(names))
    targets = Targets(found=[company for _, company in lookup.found])
    added: list[str] = []
    for name in lookup.missing:
        try:
            result = await discover_company(
                client, name, added_by_id=run.user_id, discovered_via=DiscoveredVia.USER
            )
        except Exception:
            log.exception("search %s: could not look up %s", run.id, name)
            targets.missing.append(name)
            continue
        if result.registered is not None:
            targets.found.append(result.registered)
            added.append(result.registered.name)
        else:
            targets.missing.append(name)

    await db(
        lifecycle.merge_stats,
        run.id,
        companies=[company.name for company in targets.found],
        companiesMissing=targets.missing,
    )
    if added:
        await db(
            note, run.id, EventKind.SEARCH_COMPANIES_ADDED, f"Started tracking {join_names(added)}."
        )
    if targets.missing:
        count = len(targets.missing)
        await db(
            note,
            run.id,
            EventKind.SEARCH_COMPANIES_MISSING,
            f"Couldn't find a job board for {join_names(targets.missing)}, so "
            f"{'its' if count == 1 else 'their'} roles only come from the job aggregator. "
            "Ask Foray to add the company with its careers page link to search it directly.",
            level=Level.WARN,
        )
    return targets


async def refresh_boards(run: Run, targets: Targets | None, client: HttpClient) -> None:
    """Refreshes stale boards before the search reads the mirror. A search limited to named
    companies refreshes only theirs (ungated: one or a few boards). An unlimited search
    refreshes the stale set under `MULTI_BOARD_GATE`, so it never overlaps the six-hourly cron or
    another stale-set refresh; the set is read once the gate is held."""
    if targets is not None:
        cutoff = stale_cutoff()
        stale = [
            c for c in targets.found if c.last_fetched_at is None or c.last_fetched_at < cutoff
        ]
        await _refresh(run, stale, client, named=True)
        return
    # Read at call time, as `jobs.pipeline` does: the gate is an `asyncio.Lock`, bound to the
    # loop that first waits on it, and tests give each loop a fresh one.
    async with refresh_module.MULTI_BOARD_GATE:
        stale = await db(stale_companies, stale_cutoff())
        await _refresh(run, stale, client, named=False)


async def _refresh(run: Run, stale: list[Company], client: HttpClient, *, named: bool) -> None:
    if not stale:
        return
    count = len(stale)
    await db(
        note,
        run.id,
        EventKind.SOURCE_REFRESH_START,
        f"Refreshing {join_names([c.name for c in stale])}"
        if named
        else f"Refreshing {count} stale job boards",
    )
    outcomes = await refresh_companies(client, stale)
    ok = sum(1 for outcome in outcomes if outcome.status == "ok")
    await db(
        note,
        run.id,
        EventKind.SOURCE_REFRESH_DONE,
        f"Refreshed {ok}/{count} {'board' if count == 1 else 'boards'}",
        stats={"boardsRefreshed": ok, "boardsFailed": len(outcomes) - ok},
    )


def aggregator_query(
    preferences: Preferences, criteria: DerivedCriteria, brief: SearchBrief
) -> str:
    role = (
        (preferences.target_roles or [None])[0]
        or (criteria.title_include[0] if criteria.title_include else None)
        or "software engineer"
    )
    if brief.seniority in ("entry", "new_grad"):
        level = "entry level "
    elif brief.seniority == "intern":
        level = "intern "
    else:
        level = ""
    company = f" at {brief.companies[0]}" if brief.companies else ""
    where = f" in {preferences.locations[0]}" if preferences.locations else ""
    return f"{level}{role}{company}{where}"


async def run_aggregator(
    run: Run,
    preferences: Preferences,
    criteria: DerivedCriteria,
    brief: SearchBrief,
    client: HttpClient,
) -> None:
    """The aggregator pass. Failing here never fails the run: the registry is the primary source,
    and a quota that has run out is a normal state rather than an error."""
    query = aggregator_query(preferences, criteria, brief)
    try:
        result = await search_jsearch(
            client,
            query,
            max_requests=MAX_JSEARCH_REQUESTS,
            remote_only=preferences.remote_pref == "remote",
        )
        await db(ingest_jobs, None, result.jobs)
        registered = await register_boards_from_apply_links(
            client, result.discovered_boards, added_by_id=run.user_id
        )
    except Exception as error:
        expected = isinstance(error, JSearchQuotaError | JSearchDisabledError)
        await db(
            note,
            run.id,
            EventKind.SOURCE_AGGREGATOR_SKIPPED,
            str(error) or type(error).__name__,
            level=Level.INFO if expected else Level.WARN,
        )
        return
    await db(
        note,
        run.id,
        EventKind.SOURCE_AGGREGATOR_DONE,
        f'Aggregator: {len(result.jobs)} postings for "{query}"'
        + (f", {len(registered)} new job boards registered" if registered else ""),
        stats={
            "aggregatorJobs": len(result.jobs),
            "aggregatorRequests": result.requests_used,
            "companiesDiscovered": len(registered),
        },
        data={"requestsThisMonth": result.requests_this_month},
    )


# --- gate 1 --------------------------------------------------------------------------------------


async def gate_deep_evaluation(
    run: Run, settings: Settings, candidate_job_ids: Sequence[str], brief: SearchBrief
) -> None:
    """Picks the best unevaluated matches among this run's postings and asks before paying to
    evaluate them, or, on autopilot, approves that itself. Completes the run when there is
    nothing to evaluate, deep evaluation is turned off, or it cannot run at all."""
    if settings.deep_eval_top_n <= 0:
        await db(complete_search_run, run.user_id, run.id)
        return
    # Only this run's own postings: topping the batch up with leftovers from earlier searches put
    # unrelated roles into a targeted search's review.
    selected = await db(
        queries.select_for_deep_eval,
        run.user_id,
        job_ids=list(candidate_job_ids),
        min_fast_score=settings.score_threshold,
        limit=settings.deep_eval_top_n,
    )
    if not selected:
        await db(complete_search_run, run.user_id, run.id)
        return

    candidate = await db(load_eval_candidate, run.user_id, brief)
    if candidate is None:
        await _skip_evaluation(
            run, "Skipped the deep evaluation: there is no resume on file to judge against."
        )
        return

    # The quote is priced from the prompts that would actually be sent.
    estimate = estimate_eval_cost_for(candidate, [row.description_chars for row in selected])
    if estimate.model == "unconfigured":
        await _skip_evaluation(
            run,
            "Skipped the deep evaluation: no model is configured for it (LLM_EVALUATE or "
            "LLM_DEFAULT).",
        )
        return

    n = len(selected)
    cost = cost_phrase(estimate.usd)
    priced = f" ({cost})" if cost else ""
    what = roles(n)
    quote = catalogue.CostEstimate(
        usd=estimate.usd, per_item_usd=estimate.per_item_usd, model=estimate.model
    )
    payload = catalogue.DeepEvalBatchPayload(
        items=[
            catalogue.DeepEvalBatchItem(
                match_id=str(row.match_id),
                job_id=row.job_id,
                title=row.title,
                company=row.company,
                fast_score=row.fast_score,
            )
            for row in selected
        ],
        estimate=quote,
        note=(
            f"{estimate.model} has no price on file, so there is no cost estimate."
            if estimate.usd is None
            else None
        ),
    )
    autopilot = run.mode == Mode.AUTOPILOT
    await db(
        request_approval,
        run.id,
        ApprovalKind.DEEP_EVAL_BATCH,
        f"Deep-evaluate {what}{priced}",
        payload,
        stats={"phase": "evaluating" if autopilot else "awaiting_evaluation", "toEvaluate": n},
        data={"count": n, "estimate": quote},
        messages={
            "requested": f"Waiting for your go-ahead to deep-evaluate {what}{priced}.",
            "auto": f"Autopilot: deep-evaluating {what}{priced}.",
        },
    )


async def _skip_evaluation(run: Run, message: str) -> None:
    await db(note, run.id, EventKind.EVALUATION_SKIPPED, message, level=Level.WARN)
    await db(complete_search_run, run.user_id, run.id)


# --- finishing -----------------------------------------------------------------------------------


def _count(stats: Mapping[str, Any], key: str) -> int:
    value = stats.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return 0
    return int(value)


def complete_search_run(user_id: str, run_id: uuid.UUID | str) -> bool:
    """Completes a search run from whatever it recorded along the way. Counts are read back from
    `stats`, because by the time a run completes the step that scored it is long gone. False when
    the run had already finished (a Stop landed first). Sync."""
    run = Run.objects.for_user(user_id).filter(id=run_id).first()
    if run is None:
        return False
    stats = run.stats if isinstance(run.stats, dict) else {}
    job_ids = SearchInput.read(run.input).candidate_job_ids or []
    strong, good = queries.score_bands_for_jobs(user_id, job_ids)
    output = SearchOutput(
        candidates=_count(stats, "candidates"),
        new_matches=_count(stats, "newMatches"),
        scored=_count(stats, "scored"),
        auto_archived=_count(stats, "autoArchived"),
        evaluated=_count(stats, "evaluated"),
        shortlisted=_count(stats, "shortlisted"),
        strong=strong,
        top_match_ids=queries.top_match_ids_for_jobs(user_id, job_ids, 5),
    )
    evaluated = output.evaluated > 0
    message = summarize_search_run(
        candidates=output.candidates,
        new_matches=output.new_matches,
        scored=output.scored,
        auto_archived=output.auto_archived,
        evaluated=output.evaluated,
        shortlisted=output.shortlisted,
        recommended=_count(stats, "recommended"),
        # Fast-score bands, for the message only when nothing was deep-evaluated.
        strong=0 if evaluated else strong,
        good=0 if evaluated else good,
    )
    return lifecycle.complete(run.id, output, message)


def summarize_search_run(
    *,
    candidates: int,
    new_matches: int,
    scored: int,
    auto_archived: int,
    evaluated: int,
    shortlisted: int,
    recommended: int,
    strong: int,
    good: int,
) -> str:
    """The run's closing line, e.g. "Found 60 roles, scored 60, deep-evaluated 12. 5 look worth
    applying to; 4 shortlisted." `recommended` counts evaluated matches recommended strong or
    apply; `strong` and `good` are fast-score bands, used when nothing was deep-evaluated."""
    if candidates == 0:
        return "No open roles matched your criteria this time."
    # A repeat search mostly finds roles it has seen and scored before, so "0 new" and "scored 0"
    # are said plainly or left out rather than recited.
    if new_matches >= candidates:
        fresh = ""
    elif new_matches == 0:
        fresh = " (none new)"
    else:
        fresh = f" ({new_matches} new)"
    parts = [f"Found {roles(candidates)}{fresh}"]
    if scored > 0:
        parts.append(f"scored {scored}")
    if evaluated > 0:
        parts.append(f"deep-evaluated {evaluated}")
    done = ", ".join(parts)

    if evaluated > 0:
        if recommended == 0:
            verdict = "None looked worth applying to."
        else:
            verdict = f"{recommended} {'looks' if recommended == 1 else 'look'} worth applying to"
            verdict += f"; {shortlisted} shortlisted." if shortlisted > 0 else "."
        return f"{done}. {verdict}"

    bands = []
    if strong > 0:
        bands.append(f"{strong} {'looks' if strong == 1 else 'look'} strong")
    if good > 0:
        bands.append(f"{good} {'is' if good == 1 else 'are'} worth a look")
    if auto_archived > 0:
        bands.append(
            f"{auto_archived} {'was' if auto_archived == 1 else 'were'} archived as too weak"
        )
    return f"{done}." + (f" {capitalize(', '.join(bands))}." if bands else "")
