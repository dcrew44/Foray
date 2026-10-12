"""Everything after a search's first gate, and evaluation on demand (design §9.4, §9.6;
`map/worker.md` §9.5–§10; a port of `src/lib/runs/evaluate-run.ts`).

A search pauses twice in `approve_each`: before paying for deep evaluation, and before moving
anything onto the shortlist. Each pause ends the step; the decision defers one of the
continuations here (`search.evaluate`, `search.shortlist`), which reads the run and the approval
back and carries on. Nothing is held in memory across a gate. Anything other than `approved` or
`auto_approved` is a "no" that completes the run with today's message. `resume_from` records the
approval a continuation resumed from, so a redelivery is a no-op.

`rank_deep` runs evaluate specific matches on demand (the job page's "Evaluate", chat's
`evaluate_job`): no gate, because the user asked for exactly this, and a second request for the
same matches while one runs returns that run (`dedupe_key = evaluate:<sha1 of the ids>`).

Every path ends the run: completed here, failed by `run_step` (a `UserFacingError`'s text, the
budget's sentence, or the error), or left alone because the user stopped it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from django.db import connection

from apps.core.db import db
from apps.core.errors import UserFacingError
from apps.llm.budget import BudgetExceeded
from apps.matching import queries
from apps.matching.brief import parse_brief
from apps.matching.deep_eval import (
    EvalCandidate,
    MatchEvaluation,
    NoResumeError,
    evaluate_match,
    load_eval_candidate,
)
from apps.matching.queries import MatchRow
from apps.matching.search import complete_search_run, note, roles
from apps.resumes.queries import latest_base_resume
from apps.runs import catalogue, lifecycle
from apps.runs.approvals import approved_item_ids, is_approved, request_approval
from apps.runs.catalogue import (
    ApprovalKind,
    ApprovalStatus,
    EvaluateInput,
    EvaluateOutput,
    EventKind,
    Level,
    Mode,
    RunKind,
    SearchInput,
)
from apps.runs.models import Approval, Run

__all__ = [
    "EVAL_CONCURRENCY",
    "MATCH_EVALUATE_TASK",
    "PROGRESS_EVERY",
    "BatchOutcome",
    "UnknownMatchError",
    "continue_search_evaluate",
    "continue_search_shortlist",
    "describe_evaluations",
    "describe_verdicts",
    "evaluate_batch",
    "resume_from",
    "run_match_evaluation",
    "start_match_evaluation",
]

log = logging.getLogger("foray.matching")

MATCH_EVALUATE_TASK = "match.evaluate"
# Concurrent evaluations: each takes tens of seconds on a strong model; the per-provider limiter
# is the real ceiling above this.
EVAL_CONCURRENCY = 5
# A progress event every this many evaluations.
PROGRESS_EVERY = 3

APPROVAL_GONE = "The approval this search was waiting on no longer exists."
NO_RESUME_SKIP = "Skipped the deep evaluation: there is no resume on file to judge against."


# --- shared --------------------------------------------------------------------------------------


def resume_from(run_id: uuid.UUID | str, approval_id: uuid.UUID | str, **stats: Any) -> bool:
    """Resumes a paused run from one particular approval, at most once: the approval is recorded
    in the same conditional write that merges `stats`, so a redelivered continuation does
    nothing, and a Stop that landed first still wins. Sync."""
    patch = {**stats, "resumedFrom": str(approval_id)}
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE runs SET stats = stats || %s::jsonb, updated_at = now() "
            "WHERE id = %s AND status = 'running' "
            "AND (stats ->> 'resumedFrom') IS DISTINCT FROM %s RETURNING id",
            [lifecycle.jsonb(patch), str(run_id), str(approval_id)],
        )
        return cursor.fetchone() is not None


def approval_for(run: Run, approval_id: str) -> Approval:
    """The approval a continuation was deferred for. Sync."""
    parsed = queries.as_uuid(approval_id)
    approval = Approval.objects.for_user(run.user_id).filter(id=parsed).first() if parsed else None
    if approval is None or approval.run_id != run.id:
        raise UserFacingError(APPROVAL_GONE)
    return approval


@dataclass(frozen=True, slots=True)
class Failure:
    match_id: str
    error: str


@dataclass(slots=True)
class BatchOutcome:
    evaluated: list[MatchEvaluation] = field(default_factory=list)
    failed: list[Failure] = field(default_factory=list)
    cancelled: bool = False


async def evaluate_batch(
    run: Run, match_ids: Sequence[str], candidate: EvalCandidate
) -> BatchOutcome:
    """Evaluates matches five at a time, checking for a Stop before each one. A failed
    evaluation leaves its match unevaluated and the batch carries on; results already written
    are kept whatever happens next. A budget refusal ends the batch and is raised, so the run
    fails with the budget's sentence. Progress (stats and a debug event) every 3 evaluations and
    at the end, written in order."""
    outcome = BatchOutcome()
    total = len(match_ids)
    gate = asyncio.Semaphore(EVAL_CONCURRENCY)
    writes = asyncio.Lock()
    refused: list[BudgetExceeded] = []
    reported = 0

    async def report(done: int, snapshot: dict[str, int]) -> None:
        async with writes:  # FIFO: progress lands in the order it was counted
            try:
                await db(
                    note,
                    run.id,
                    EventKind.EVALUATION_PROGRESS,
                    f"Evaluated {done} of {total}",
                    level=Level.DEBUG,
                    stats=snapshot,
                )
            except Exception:
                log.exception("run %s: a progress write failed", run.id)

    async def one(match_id: str) -> None:
        nonlocal reported
        async with gate:
            if outcome.cancelled or refused:
                return
            if await db(lifecycle.is_cancelled, run.id):
                outcome.cancelled = True
                return
            try:
                outcome.evaluated.append(await evaluate_match(run.user_id, match_id, candidate))
            except BudgetExceeded as error:
                refused.append(error)
                return
            except Exception as error:
                log.warning("run %s: evaluating match %s failed: %s", run.id, match_id, error)
                outcome.failed.append(Failure(match_id, str(error) or type(error).__name__))
        done = len(outcome.evaluated) + len(outcome.failed)
        if done - reported < PROGRESS_EVERY and done < total:
            return
        reported = done
        await report(
            done, {"evaluated": len(outcome.evaluated), "evaluationFailed": len(outcome.failed)}
        )

    async with asyncio.TaskGroup() as group:
        for match_id in match_ids:
            group.create_task(one(match_id))
    if refused:
        raise refused[0]
    return outcome


# --- gate 1's continuation: deep evaluation ------------------------------------------------------


async def continue_search_evaluate(run: Run, approval_id: str) -> None:
    """`search.evaluate`: the user (or autopilot) answered the deep-evaluation gate. A yes
    evaluates the approved matches and raises the shortlist gate; a no, or a lapsed approval,
    completes the run without evaluating."""
    approval = await db(approval_for, run, approval_id)
    if approval.status == ApprovalStatus.PENDING:
        log.warning("run %s: approval %s is still pending; ignoring", run.id, approval.id)
        return
    if not is_approved(approval):
        await db(
            note,
            run.id,
            EventKind.EVALUATION_SKIPPED,
            "The deep evaluation was never answered, so it was skipped."
            if approval.status == ApprovalStatus.EXPIRED
            else "Skipped the deep evaluation, as you asked.",
            level=Level.INFO,
            approval_id=approval.id,
            stats={"evaluated": 0},
        )
        await db(complete_search_run, run.user_id, run.id)
        return

    if not await db(resume_from, run.id, approval.id, phase="evaluating"):
        return
    batch = approved_item_ids(approval)
    brief = parse_brief(SearchInput.read(run.input).brief)
    candidate = await db(load_eval_candidate, run.user_id, brief)
    if candidate is None:
        await db(note, run.id, EventKind.EVALUATION_SKIPPED, NO_RESUME_SKIP, level=Level.WARN)
        await db(complete_search_run, run.user_id, run.id)
        return

    # Anything evaluated since the gate was raised (on demand, from the job page) is not paid
    # for twice, and nothing the user has since rejected is paid for at all.
    to_evaluate = await db(queries.matches_needing_evaluation, run.user_id, batch)
    await db(
        lifecycle.merge_stats,
        run.id,
        toEvaluate=len(to_evaluate),
        evaluated=0,
        evaluationFailed=0,
    )
    if to_evaluate:
        await db(
            note,
            run.id,
            EventKind.EVALUATION_START,
            f"Deep-evaluating {roles(len(to_evaluate))} against your resume",
        )
        outcome = await evaluate_batch(run, to_evaluate, candidate)
        await db(
            lifecycle.merge_stats,
            run.id,
            evaluated=len(outcome.evaluated),
            evaluationFailed=len(outcome.failed),
        )
        if outcome.cancelled:
            return
        if not outcome.evaluated:
            first = outcome.failed[0].error if outcome.failed else "unknown error"
            raise UserFacingError(f"Deep evaluation failed for every role: {first}")
        if outcome.failed:
            n = len(outcome.failed)
            await db(
                note,
                run.id,
                EventKind.EVALUATION_FAILED,
                f"{roles(n)} could not be evaluated and {'was' if n == 1 else 'were'} left as "
                f"{'it was' if n == 1 else 'they were'}: {outcome.failed[0].error}",
                level=Level.WARN,
                data={
                    "failed": [{"matchId": f.match_id, "error": f.error} for f in outcome.failed]
                },
            )

    # The whole approved batch, including anything evaluated earlier: that is what the
    # shortlist is proposed from.
    rows = await db(queries.get_match_rows, run.user_id, batch)
    recommended = sum(1 for row in rows if row.recommendation in ("strong", "apply"))
    await db(
        note,
        run.id,
        EventKind.EVALUATION_DONE,
        describe_verdicts(rows),
        stats={"recommended": recommended},
    )
    if await db(lifecycle.is_cancelled, run.id):
        return
    await propose_shortlist(run, rows)


async def propose_shortlist(run: Run, rows: Sequence[MatchRow]) -> None:
    """Gate 2: evaluated matches worth applying to that are still in `found`. In `approve_each`
    this pauses the run; on autopilot it shortlists them at once (phase `shortlisting`). Nothing
    qualifying completes the run."""
    picks = [row for row in rows if queries.is_shortlist_candidate(row)]  # best first already
    if not picks:
        await db(complete_search_run, run.user_id, run.id)
        return
    n = len(picks)
    payload = catalogue.ShortlistPayload(
        items=[
            catalogue.ShortlistItem(
                match_id=str(row.match_id),
                job_id=row.job_id,
                title=row.title,
                company=row.company,
                deep_score=row.deep_score,
                recommendation=row.recommendation or "apply",
                summary=row.headline or "",
            )
            for row in picks
        ]
    )
    autopilot = run.mode == Mode.AUTOPILOT
    await db(
        request_approval,
        run.id,
        ApprovalKind.SHORTLIST_PROPOSAL,
        f"Shortlist {roles(n)}",
        payload,
        stats=(
            {"phase": "shortlisting", "proposedShortlist": n}
            if autopilot
            else {"phase": "awaiting_shortlist", "proposedShortlist": n}
        ),
        data={"count": n},
        messages={
            "requested": f"{'1 role looks' if n == 1 else f'{n} roles look'} worth applying to. "
            f"Shortlist {'it' if n == 1 else 'them'}?",
            "auto": f"Autopilot: shortlisting {roles(n)} that {'looks' if n == 1 else 'look'} "
            "worth applying to.",
        },
    )


# --- gate 2's continuation: shortlisting ---------------------------------------------------------


async def continue_search_shortlist(run: Run, approval_id: str) -> None:
    """`search.shortlist`: moves the approved matches that are still in `found` to `shortlisted`
    (one conditional write, so a match the user moved meanwhile stays where they put it) and
    completes the run."""
    approval = await db(approval_for, run, approval_id)
    if approval.status == ApprovalStatus.PENDING:
        log.warning("run %s: approval %s is still pending; ignoring", run.id, approval.id)
        return
    if not is_approved(approval):
        await db(
            note,
            run.id,
            EventKind.SHORTLIST_SKIPPED,
            "The shortlist proposal was never answered, so nothing was moved."
            if approval.status == ApprovalStatus.EXPIRED
            else "Left the shortlist as it was, as you asked.",
            approval_id=approval.id,
            stats={"shortlisted": 0},
        )
        await db(complete_search_run, run.user_id, run.id)
        return

    if not await db(resume_from, run.id, approval.id, phase="shortlisting"):
        return
    moved = await db(
        queries.move_matches_from_stage,
        run.user_id,
        approved_item_ids(approval),
        "found",
        "shortlisted",
    )
    await db(
        note,
        run.id,
        EventKind.SHORTLIST_DONE,
        f"Shortlisted {roles(len(moved))}."
        if moved
        else "Nothing to shortlist: those roles had already been moved.",
        stats={"shortlisted": len(moved)},
        data={"matchIds": moved},
    )
    await db(complete_search_run, run.user_id, run.id)


# --- on demand -----------------------------------------------------------------------------------


class UnknownMatchError(Exception):
    def __init__(self) -> None:
        super().__init__("There is no such match to evaluate.")


@dataclass(frozen=True, slots=True)
class EvaluationStart:
    run: Run
    reused: bool
    matches: list[MatchRow]


def start_match_evaluation(
    user_id: str, match_ids: Sequence[Any], *, conversation_id: uuid.UUID | str | None = None
) -> EvaluationStart:
    """Starts a `rank_deep` run for specific matches of this user (others are ignored). Asking
    for matches already being evaluated returns that run rather than paying twice. Raises
    `UnknownMatchError` when none of the ids is one of the user's matches, and `NoResumeError`
    when there is no resume to evaluate against. Sync."""
    matches = queries.get_match_rows(user_id, match_ids)
    if not matches:
        raise UnknownMatchError()
    if latest_base_resume(user_id) is None:
        raise NoResumeError()
    ids = sorted(str(match.match_id) for match in matches)
    key = hashlib.sha1(",".join(ids).encode()).hexdigest()
    run, reused = lifecycle.start_run(
        user_id,
        RunKind.RANK_DEEP,
        input=EvaluateInput(match_ids=ids),
        conversation_id=conversation_id,
        dedupe_key=f"evaluate:{key}",
        task=MATCH_EVALUATE_TASK,
    )
    return EvaluationStart(run=run, reused=reused, matches=matches)


async def run_match_evaluation(run: Run) -> None:
    """`match.evaluate`: the worker half of `start_match_evaluation`. Re-evaluates a match that
    already has an evaluation: that is what "Review again" asks for (bug #45, kept)."""
    value = EvaluateInput.read(run.input)
    rows = await db(queries.get_match_rows, run.user_id, value.match_ids if value else [])
    if not rows:
        raise UserFacingError("There is nothing to evaluate: those matches do not exist.")
    started = (
        f"Evaluating {rows[0].title} at {rows[0].company}"
        if len(rows) == 1
        else f"Deep-evaluating {roles(len(rows))}"
    )
    await db(
        note,
        run.id,
        EventKind.RUN_STARTED,
        started,
        stats={
            "phase": "evaluating",
            "toEvaluate": len(rows),
            "evaluated": 0,
            "evaluationFailed": 0,
        },
    )
    candidate = await db(load_eval_candidate, run.user_id)
    if candidate is None:
        raise NoResumeError()

    outcome = await evaluate_batch(run, [str(row.match_id) for row in rows], candidate)
    await db(
        lifecycle.merge_stats,
        run.id,
        evaluated=len(outcome.evaluated),
        evaluationFailed=len(outcome.failed),
    )
    if outcome.cancelled:
        return
    if not outcome.evaluated:
        raise UserFacingError(
            outcome.failed[0].error if outcome.failed else "Nothing was evaluated."
        )
    output = EvaluateOutput(
        evaluated=len(outcome.evaluated),
        match_ids=[item.match_id for item in outcome.evaluated],
    )
    await db(
        lifecycle.complete,
        run.id,
        output,
        describe_evaluations(outcome.evaluated, len(outcome.failed)),
    )


# --- wording -------------------------------------------------------------------------------------

VERDICT: dict[str, str] = {
    "strong": "a strong match",
    "apply": "worth applying",
    "maybe": "a maybe",
    "skip": "probably not worth applying",
}


class _Verdict(Protocol):
    @property
    def score(self) -> int: ...
    @property
    def recommendation(self) -> str: ...


class _Evaluated(Protocol):
    @property
    def title(self) -> str: ...
    @property
    def company(self) -> str: ...
    @property
    def evaluation(self) -> _Verdict: ...


def describe_evaluations(evaluations: Sequence[_Evaluated], failed: int = 0) -> str:
    """The closing line of an on-demand evaluation, naming the role(s): "Evaluated Senior Backend
    Engineer at Stripe: 82, worth applying."."""

    def label(item: _Evaluated) -> str:
        return f"{item.title} at {item.company}"

    def verdict(item: _Evaluated) -> str:
        return f"{item.evaluation.score}, {VERDICT[item.evaluation.recommendation]}"

    if len(evaluations) == 1:
        only = evaluations[0]
        message = f"Evaluated {label(only)}: {verdict(only)}."
    else:
        best = sorted(evaluations, key=lambda item: -item.evaluation.score)
        shown = "; ".join(f"{label(item)} ({verdict(item)})" for item in best[:3])
        more = f"; and {len(best) - 3} more" if len(best) > 3 else ""
        message = f"Evaluated {roles(len(evaluations))}: {shown}{more}."
    if failed > 0:
        message += f" {failed} could not be evaluated."
    return message


def describe_verdicts(rows: Sequence[MatchRow]) -> str:
    """ "Deep evaluation done: 3 strong, 2 worth applying, 5 maybes, 2 to skip."."""
    counts = dict.fromkeys(("strong", "apply", "maybe", "skip"), 0)
    for row in rows:
        if row.recommendation:
            counts[row.recommendation] += 1
    parts = []
    if counts["strong"]:
        parts.append(f"{counts['strong']} strong")
    if counts["apply"]:
        parts.append(f"{counts['apply']} worth applying")
    if counts["maybe"]:
        parts.append(f"{counts['maybe']} {'maybe' if counts['maybe'] == 1 else 'maybes'}")
    if counts["skip"]:
        parts.append(f"{counts['skip']} to skip")
    return f"Deep evaluation done: {', '.join(parts)}." if parts else "Deep evaluation done."
