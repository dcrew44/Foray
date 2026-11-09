"""Watch: score the postings each scheduled refresh brings in, and announce the strong ones (#5).

A search makes matches only when the user asks; between searches, strong roles sat unscored in
the mirror for a week. A scheduled refresh that brought postings in defers `watch.fan_out`
(as does every sixth hour's tick, for what the review cap held back: `jobs.tasks.watches_due`),
which starts one `watch` run per user who is watching. A watch is a search with no request and no
gates over a narrow window:

- **The window.** Postings first seen since the previous completed watch (less ten minutes, as
  an ingest can commit late), and never more than a week back. A failed or stopped watch leaves
  the window where it was.
- **Only its own matches.** It works on the matches it creates (`insert_new_matches`), plus
  those earlier watches of the week created and could not finish (scoring failed, or the daily
  cap held them back). A match a search already made is the search's: watch never re-reviews
  it, and never announces anything twice (R20).
- **No approval.** Deep reviews run without asking, at most `watch_daily_reviews` in 24 hours
  (counted from the ledger) and `deep_eval_top_n` per watch, best fast score first.
- **Announcing.** A watch that reviewed something as `strong` or `apply` completes linked to the
  user's "New matches" chat, so `emit()` defers the usual chat follow-up. A quiet one completes
  at debug level with `announced: 0`, which the feeds leave out. Watch moves no stages beyond
  the archiving the shared scoring helpers do: what it announces stays in `found` for the user.
- **To a phone.** The same matches are queued for the user's ntfy topic as the watch completes
  (`notifications.outbox.queue_matches`, #59): one notification per match, once, and nothing for
  a user who has set no topic.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models.functions import Now

from apps.accounts.models import User
from apps.accounts.profile import interview_progress
from apps.accounts.schemas import InterviewState, Preferences, Settings
from apps.applications import prepare as prepare_application
from apps.chat.models import Conversation
from apps.core.clock import database_now
from apps.core.db import db
from apps.core.ids import new_id
from apps.llm.budget import month_to_date_spend_sync
from apps.llm.models import LlmCall
from apps.matching import queries
from apps.matching.brief import EMPTY_BRIEF
from apps.matching.deep_eval import MatchEvaluation, load_eval_candidate
from apps.matching.evaluate import evaluate_batch
from apps.matching.fast_score import score_matches
from apps.matching.prefilter import DerivedCriteria, derive_search, prefilter_jobs
from apps.matching.search import assert_can_search, note, roles, scoring_message
from apps.notifications import outbox
from apps.resumes.models import Resume, ResumeKind
from apps.resumes.queries import latest_base_resume
from apps.runs import lifecycle
from apps.runs.catalogue import (
    EventKind,
    Level,
    Mode,
    RunKind,
    RunStatus,
    WatchInput,
    WatchOutput,
)
from apps.runs.models import Run

__all__ = [
    "DEDUPE_KEY",
    "FAN_OUT_TASK",
    "WATCH_TASK",
    "eligible_user_ids",
    "finish_watch",
    "new_matches_conversation",
    "reviews_used",
    "run_watch",
    "start_watch",
    "start_watches",
    "watch_message",
    "watch_window",
]

log = logging.getLogger("foray.matching")

WATCH_TASK = "watch.run"
FAN_OUT_TASK = "watch.fan_out"
DEDUPE_KEY = "watch"
PROMPT = "Check new postings"
CONVERSATION_TITLE = "New matches"
# How far back a watch looks, and how long an unfinished match stays its business.
WINDOW = timedelta(days=7)
# The edge of the last window is read again: a posting inserted by a transaction that began
# before that watch started may have committed after it read.
OVERLAP = timedelta(minutes=10)
REVIEW_PERIOD = timedelta(hours=24)
ANNOUNCED = ("strong", "apply")


# --- starting ------------------------------------------------------------------------------------


def eligible_user_ids() -> list[str]:
    """Who the fan-out starts a watch for, oldest account first: active, watching, every
    required preference known, a base resume, and no search queued or running (a search parked
    at a gate does not count). Nobody once this month's model budget is spent. Sync."""
    if month_to_date_spend_sync() >= settings.ENV.llm_monthly_budget_usd:
        return []
    with_resume = set(
        Resume.objects.unscoped("watch: who has a base resume")
        .filter(kind=ResumeKind.BASE)
        .values_list("user_id", flat=True)
    )
    searching = set(
        Run.objects.unscoped("watch: who is searching right now")
        .filter(kind=RunKind.SEARCH, status__in=(RunStatus.QUEUED, RunStatus.RUNNING))
        .values_list("user_id", flat=True)
    )
    users = (
        User.objects.filter(is_active=True, pk__in=with_resume - searching)
        .only("id", "preferences", "interview_state", "settings")
        .order_by("created_at")
    )
    chosen: list[str] = []
    for user in users:
        if not Settings.read(user.settings).watch_enabled:
            continue
        progress = interview_progress(
            Preferences.read(user.preferences), InterviewState.read(user.interview_state)
        )
        if not progress.blocking:
            chosen.append(user.pk)
    return chosen


def start_watch(user_id: str, *, since: datetime | None = None) -> tuple[Run, bool]:
    """One user's watch (`lifecycle.start_run`, one active per user). `since` widens the window
    by hand (`watch_now --days`). Sync."""
    return lifecycle.start_run(
        user_id,
        RunKind.WATCH,
        input=WatchInput(prompt=PROMPT, since=since.isoformat() if since else None),
        mode=Mode.AUTOPILOT,
        dedupe_key=DEDUPE_KEY,
        task=WATCH_TASK,
    )


def start_watches() -> int:
    """`watch.fan_out`: a watch for every eligible user. One user's failure is logged and the
    rest still start. Returns how many started. Sync."""
    started = 0
    for user_id in eligible_user_ids():
        try:
            _, reused = start_watch(user_id)
        except Exception:
            log.exception("watch: could not start a watch for user %s", user_id)
            continue
        started += 0 if reused else 1
    return started


# --- what a watch reads --------------------------------------------------------------------------


def _watch_runs(user_id: str):
    return Run.objects.for_user(user_id).filter(kind=RunKind.WATCH)


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def watch_window(user_id: str, since: str | None) -> tuple[datetime, datetime]:
    """`(since, until)`: from the end of the previous completed watch (less the overlap), never
    more than `WINDOW` back, to the database's present. A `since` given at start wins. Sync."""
    until = database_now()
    given = _instant(since)
    if given is not None:
        return given, until
    floor = until - WINDOW
    previous = (
        _watch_runs(user_id)
        .filter(status=RunStatus.COMPLETED)
        .order_by("-created_at")
        .values_list("input", flat=True)
        .first()
    )
    last = _instant(previous.get("until")) if isinstance(previous, dict) else None
    return (max(floor, last - OVERLAP) if last is not None else floor), until


def cached_criteria(user_id: str, run_id: uuid.UUID, saved: Preferences) -> DerivedCriteria | None:
    """The title terms an earlier watch derived from these same saved preferences, so the many
    watches of a day neither pay for nor drift between derivations. Sync."""
    rows = _watch_runs(user_id).exclude(id=run_id).order_by("-created_at")
    stored = rows.values_list("input", flat=True).first()
    if not isinstance(stored, dict) or stored.get("preferences") != saved.dump():
        return None
    return DerivedCriteria.read(stored.get("criteria"))


def backlog_job_ids(user_id: str, run_id: uuid.UUID) -> list[str]:
    """The matches earlier watches of the week created, newest first. Scoring and review skip
    whatever is already done, so this only picks up what a failure or the cap left. Sync."""
    rows = (
        _watch_runs(user_id)
        .exclude(id=run_id)
        .filter(created_at__gte=Now() - WINDOW)
        .order_by("-created_at")
        .values_list("input", flat=True)
    )
    found: dict[str, None] = {}
    for stored in rows:
        ids = stored.get("newJobIds") if isinstance(stored, dict) else None
        for job_id in ids if isinstance(ids, list) else []:
            if isinstance(job_id, str):
                found[job_id] = None
    return list(found)


def reviews_used(user_id: str) -> int:
    """Deep reviews watches paid for in the last 24 hours, failed ones included. Sync."""
    return (
        LlmCall.objects.for_user(user_id)
        .filter(purpose="evaluate", run__kind=RunKind.WATCH, created_at__gte=Now() - REVIEW_PERIOD)
        .count()
    )


def record_new_matches(run_id: uuid.UUID, user_id: str, job_ids: list[str]) -> list[str]:
    """Records the window's postings as `found` matches and remembers on the run which it chose
    and which matches it created. Sync."""
    with transaction.atomic():
        created = queries.insert_new_matches(user_id, job_ids)
        lifecycle.merge_input(run_id, candidateJobIds=job_ids, newJobIds=created)
        lifecycle.merge_stats(run_id, matches=len(job_ids), newMatches=len(created))
    return created


# --- finishing -----------------------------------------------------------------------------------


def new_matches_conversation(user_id: str) -> uuid.UUID:
    """The chat watches report to: the latest one a watch reported to, or a new "New matches"
    chat when there is none (or the user deleted it). Sync."""
    existing = (
        _watch_runs(user_id)
        .filter(conversation__isnull=False)
        .order_by("-created_at")
        .values_list("conversation_id", flat=True)
        .first()
    )
    if existing is not None:
        return existing
    return Conversation.objects.create_for(user_id, id=new_id(), title=CONVERSATION_TITLE).id


def finish_watch(run_id: uuid.UUID, user_id: str, output: WatchOutput) -> bool:
    """Completes the watch. With something to announce, at info level and linked to the "New
    matches" chat (created here if need be), so `emit()` defers the chat follow-up; otherwise at
    debug level, unlinked. What it announces is queued for the user's phone in the same
    transaction (#59; before `complete()`, whose `emit()` must come last). A watch Stopped
    meanwhile leaves no chat and no notification behind. Sync."""
    announced = len(output.announced_match_ids)
    with transaction.atomic():
        conversation = new_matches_conversation(user_id) if announced else None
        if announced:
            outbox.queue_matches(user_id, output.announced_match_ids)
        done = lifecycle.complete(
            run_id,
            output,
            output.message,
            level=Level.INFO if announced else Level.DEBUG,
            stats={"announced": announced, "notReviewed": output.not_reviewed},
            conversation_id=conversation,
        )
        if not done:
            transaction.set_rollback(True)
    return done


def prepare_applications(user_id: str, match_ids: list[str]) -> int:
    """Starts preparing an application for each kept match. Never fails the watch: a posting
    whose application could not be started can still be prepared from its match page. Sync."""
    try:
        with transaction.atomic():
            return prepare_application.start_many(user_id, match_ids)
    except Exception:
        log.warning("could not start preparing applications for %s", user_id, exc_info=True)
        return 0


def watch_message(considered: int, announced: int, not_reviewed: int) -> str:
    """`considered`: the new matches this watch made, plus the week's it picked up again."""
    if considered == 0:
        return "No new postings match your criteria."
    pool = f"{considered} new {'posting' if considered == 1 else 'postings'}"
    if announced == 0:
        message = f"Nothing strong among {pool}."
    else:
        verb = "looks" if announced == 1 else "look"
        message = f"{roles(announced).capitalize()} {verb} worth applying to, from {pool}."
    if not_reviewed > 0:
        message += f" {not_reviewed} more scored well; the daily review cap holds them for now."
    return message


# --- the step ------------------------------------------------------------------------------------


async def _cancelled(run: Run) -> bool:
    return await db(lifecycle.is_cancelled, run.id)


async def run_watch(run: Run) -> None:
    """`watch.run`: filter the window, score, review within the cap, then complete. Progress is
    debug-level throughout: a watch after every refresh that found something must not fill the
    activity feed."""
    value = WatchInput.read(run.input)
    await db(
        note,
        run.id,
        EventKind.RUN_STARTED,
        "Checking new postings",
        level=Level.DEBUG,
        stats={"phase": "starting"},
    )
    profile = await db(assert_can_search, run.user_id)
    saved = profile.preferences
    since, until = await db(watch_window, run.user_id, value.since)
    criteria = await db(cached_criteria, run.user_id, run.id, saved)
    if criteria is None:
        _, criteria = await derive_search(saved, None, EMPTY_BRIEF)
    await db(
        lifecycle.merge_input,
        run.id,
        since=since.isoformat(),
        until=until.isoformat(),
        preferences=saved.dump(),
        criteria=criteria.dump(),
    )
    if await _cancelled(run):
        return

    # 1. The window's postings that fit, as new matches; plus what earlier watches left.
    await db(lifecycle.merge_stats, run.id, phase="filtering")
    found = await db(prefilter_jobs, run.user_id, saved, criteria, first_seen_after=since)
    created = await db(record_new_matches, run.id, run.user_id, found.job_ids)
    backlog = await db(backlog_job_ids, run.user_id, run.id)
    work = list(dict.fromkeys([*created, *backlog]))
    await db(
        note,
        run.id,
        EventKind.PREFILTER_DONE,
        f"{found.selected} new postings match your criteria",
        level=Level.DEBUG,
        stats={"candidates": found.selected},
        data=found.funnel(),
    )
    if await _cancelled(run):
        return

    # 2. Fast scores.
    await db(lifecycle.merge_stats, run.id, phase="scoring")
    base = await db(latest_base_resume, run.user_id)
    threshold = profile.settings.score_threshold
    scoring = await score_matches(
        run.user_id,
        preferences=saved,
        focus=None,
        resume=base.resume if base is not None else None,
        job_ids=work,
        threshold=threshold,
        should_stop=lambda: _cancelled(run),
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
        scoring_message(len(work), scoring.scored, scoring.archived, scoring.failed, threshold),
        level=Level.DEBUG,
    )

    # 3. Deep reviews for the best of them, within the day's cap, without asking.
    await db(lifecycle.merge_stats, run.id, phase="evaluating")
    worth = await db(
        queries.select_for_deep_eval,
        run.user_id,
        job_ids=work,
        min_fast_score=threshold,
        limit=len(work),
    )
    # What is worth a review is worth applying to: its application is prepared now, beside the
    # reviews and without waiting for them (#60).
    await db(prepare_applications, run.user_id, [str(item.match_id) for item in worth])
    used = await db(reviews_used, run.user_id)
    allowance = min(profile.settings.deep_eval_top_n, profile.settings.watch_daily_reviews - used)
    chosen = worth[: max(0, allowance)]
    candidate = await db(load_eval_candidate, run.user_id) if chosen else None
    evaluated: list[MatchEvaluation] = []
    if chosen and candidate is not None:
        await db(
            note,
            run.id,
            EventKind.EVALUATION_START,
            f"Reviewing {roles(len(chosen))}",
            level=Level.DEBUG,
            stats={"toEvaluate": len(chosen)},
        )
        outcome = await evaluate_batch(run, [str(c.match_id) for c in chosen], candidate)
        if outcome.cancelled:
            return
        evaluated = outcome.evaluated
        await db(
            lifecycle.merge_stats,
            run.id,
            evaluated=len(outcome.evaluated),
            evaluationFailed=len(outcome.failed),
        )
    reviewed = {str(item.match_id) for item in evaluated}
    not_reviewed = sum(1 for item in worth if str(item.match_id) not in reviewed)

    # 4. Announce what came out worth applying to, best first.
    announced = sorted(
        (item for item in evaluated if item.evaluation.recommendation in ANNOUNCED),
        key=lambda item: -item.evaluation.score,
    )
    strong = sum(1 for item in announced if item.evaluation.recommendation == "strong")
    await db(
        finish_watch,
        run.id,
        run.user_id,
        WatchOutput(
            candidates=found.selected,
            new_matches=len(created),
            scored=scoring.scored,
            auto_archived=scoring.archived,
            evaluated=len(evaluated),
            strong=strong,
            apply=len(announced) - strong,
            not_reviewed=not_reviewed,
            announced_match_ids=[item.match_id for item in announced],
            message=watch_message(len(work), len(announced), not_reviewed),
        ),
    )
