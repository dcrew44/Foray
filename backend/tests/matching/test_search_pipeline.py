"""A search run end to end, against the test database with factory jobs (design §9.4, §12.2; a
port of `src/test/unit/search-gates.test.ts`, plus the whole `search.run` step).

The models are fakes (`FakeLLM`, by posting title), installed below the real budget, limiter
and ledger wrappers. The continuations run on a real procrastinate Worker in-process
(`run_deferred()`), exactly as `manage.py worker` would run them, so every test here is
transactional. What is pinned: the exact event-kind sequences and wording (with the uniform
`run.*` finals), `strong` in the output, the phases, and that a search follows its brief (R15).
"""

from __future__ import annotations

import asyncio
import itertools
from typing import Any

import pytest
from django.db import connections
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.schemas import SAMPLE_PREFERENCES, Settings
from apps.core.queue import app, defer_in_tx
from apps.jobs import refresh as refresh_module
from apps.jobs.models import Company
from apps.jobs.refresh import RefreshOutcome
from apps.llm.models import LlmCall
from apps.matching import evaluate, fast_score, search
from apps.matching.brief import EMPTY_BRIEF
from apps.matching.deep_eval import NoResumeError
from apps.matching.evaluate import UnknownMatchError, start_match_evaluation
from apps.matching.models import JobMatch
from apps.matching.queries import (
    get_match_rows,
    is_shortlist_candidate,
    list_shortlist_candidates,
    move_matches_from_stage,
    select_for_deep_eval,
    top_match_ids_for_jobs,
)
from apps.matching.schemas import DeepEval
from apps.matching.search import gate_deep_evaluation
from apps.runs import approvals, lifecycle, reaper
from apps.runs.models import Approval, Event, Run
from tests.factories import make_company, make_job, make_match, make_resume, make_run, make_user
from tests.matching.conftest import FakeLLM
from tests.queue import deferred, run_deferred

pytestmark = pytest.mark.django_db(transaction=True)

_redelivery = itertools.count(1)


@pytest.fixture(autouse=True)
def _fresh_gate(monkeypatch: pytest.MonkeyPatch) -> asyncio.Lock:
    """`MULTI_BOARD_GATE` binds to the event loop that first waits on it, and every test runs
    its jobs on a loop of its own, so each gets a fresh gate. The search reads it from
    `apps.jobs.refresh` at call time, so it holds this one."""
    gate = asyncio.Lock()
    monkeypatch.setattr(refresh_module, "MULTI_BOARD_GATE", gate)
    return gate


# --- helpers -------------------------------------------------------------------------------------


def settings(**patch: Any) -> Settings:
    return Settings(score_threshold=55, deep_eval_top_n=3).model_copy(update=patch)


def deep(score: int, recommendation: str, summary: str = "A fit.") -> dict[str, Any]:
    return DeepEval(
        matched_requirements=[],
        gaps=[],
        seniority_fit="match",
        location_fit="match",
        salary_fit="unstated",
        risk_flags=[],
        talking_points=[],
        summary=summary,
        score=score,
        recommendation=recommendation,  # pyright: ignore[reportArgumentType]
    ).dump()


def fresh_company(**fields: Any) -> Company:
    fields.setdefault("last_fetched_at", timezone.now())
    return make_company(**fields)


def seed(user: User, seeds: list[dict[str, Any]], llm: FakeLLM | None = None) -> list[JobMatch]:
    """One match per seed, each on its own posting titled "Role <n>"; the fake models score a
    posting by its title."""
    company = fresh_company()
    matches = []
    for seed_ in seeds:
        title = f"Role {next(_redelivery)}"
        job = make_job(company=company, title=title)
        fields: dict[str, Any] = {}
        if seed_.get("deep") is not None:
            fields["deep_score"] = seed_["deep"]["score"]
            fields["deep_eval"] = seed_["deep"]
        matches.append(
            make_match(
                user,
                job=job,
                stage=seed_.get("stage", "found"),
                fast_score=seed_.get("fast"),
                **fields,
            )
        )
        if llm is not None and seed_.get("fast") is not None:
            llm.fast[title] = seed_["fast"]
    return matches


def ids(matches: list[JobMatch]) -> list[str]:
    return [str(match.id) for match in matches]


def job_ids(matches: list[JobMatch]) -> list[str]:
    return [match.job_id for match in matches]


def search_run(user: User, mode: str, jobs: list[str], **stats: Any) -> Run:
    return make_run(
        user,
        kind="search",
        status="running",
        mode=mode,
        input={"prompt": "Search with my saved preferences", "candidateJobIds": jobs},
        stats={"phase": "scoring", **stats},
    )


def gate(run: Run, chosen: Settings, jobs: list[str]) -> None:
    asyncio.run(gate_deep_evaluation(run, chosen, jobs, EMPTY_BRIEF))


def refreshed(run: Run) -> Run:
    return Run.objects.unscoped("tests").get(id=run.id)


def approvals_for(run: Run) -> list[Approval]:
    return list(Approval.objects.unscoped("tests").filter(run_id=run.id).order_by("created_at"))


def events_for(run: Run) -> list[Event]:
    return list(Event.objects.unscoped("tests").filter(run_id=run.id).order_by("id"))


def kinds(run: Run) -> list[str]:
    return [event.kind for event in events_for(run)]


def stages(matches: list[JobMatch]) -> list[str]:
    found = dict(
        JobMatch.objects.unscoped("tests")
        .filter(id__in=[m.id for m in matches])
        .values_list("id", "stage")
    )
    return [found[match.id] for match in matches]


def decide(user: User, approval: Approval, via: str = "ui", **decision: Any) -> None:
    result = approvals.decide(user.pk, approval.id, decision, via)
    assert result.ok, result.outcome


def redeliver(task: str, run: Run, **kwargs: Any) -> None:
    """A second delivery of a job that already ran (what pg-boss could do)."""
    defer_in_tx(
        app.tasks[task],
        lock=f"run:{run.id}",
        queueing_lock=f"redelivery:{next(_redelivery)}",
        run_id=str(run.id),
        **kwargs,
    )


def last_event(run: Run) -> Event:
    return events_for(run)[-1]


# --- selecting what to evaluate, and the shortlist rule ------------------------------------------


def test_selects_this_runs_open_unevaluated_matches_at_or_above_the_threshold() -> None:
    user = make_user()
    matches = seed(
        user,
        [
            {"fast": 90},  # 0: selected
            {"stage": "shortlisted", "fast": 70},  # 1: selected
            {"fast": 40},  # 2: below the threshold
            {"fast": 95, "deep": deep(90, "strong")},  # 3: already evaluated
            {"stage": "archived", "fast": 99},  # 4: closed
            {"fast": 60},  # 5: selected
            {},  # 6: unscored
            {"fast": 98},  # 7: not this run's: from an earlier search
        ],
    )
    run_jobs = job_ids(matches[:7])
    every = select_for_deep_eval(user.pk, job_ids=run_jobs, min_fast_score=55, limit=10)
    assert [str(row.match_id) for row in every] == ids([matches[0], matches[1], matches[5]])
    assert every[0].description_chars > 0
    top = select_for_deep_eval(user.pk, job_ids=run_jobs, min_fast_score=55, limit=1)
    assert [str(row.match_id) for row in top] == ids(matches[:1])
    assert select_for_deep_eval(user.pk, job_ids=run_jobs, min_fast_score=55, limit=0) == []
    assert select_for_deep_eval(user.pk, job_ids=[], min_fast_score=55, limit=10) == []
    # Never another user's.
    assert select_for_deep_eval(make_user().pk, job_ids=run_jobs, min_fast_score=0, limit=9) == []


def test_the_shortlist_rule_reads_the_same_from_the_database() -> None:
    user = make_user()
    matches = seed(
        user,
        [
            {"deep": deep(75, "apply", "a")},
            {"deep": deep(91, "strong", "b")},
            {"deep": deep(60, "maybe", "c")},
            {"stage": "shortlisted", "deep": deep(95, "strong", "d")},
            {"fast": 88},
        ],
    )
    rows = get_match_rows(user.pk, ids(matches))
    assert [(row.stage, row.recommendation, is_shortlist_candidate(row)) for row in rows] == [
        ("shortlisted", "strong", False),
        ("found", "strong", True),
        ("found", None, False),
        ("found", "apply", True),
        ("found", "maybe", False),
    ]
    picks = list_shortlist_candidates(user.pk, ids(matches))
    assert [str(row.match_id) for row in picks] == ids([matches[1], matches[0]])
    assert picks[0].headline == "b"
    # Moving is conditional on the stage, so a match moved meanwhile stays put.
    JobMatch.objects.for_user(user.pk).filter(id=matches[0].id).update(stage="applied")
    moved = move_matches_from_stage(user.pk, ids(matches[:2]), "found", "shortlisted")
    assert moved == ids(matches[1:2])
    assert stages(matches[:2]) == ["applied", "shortlisted"]


def test_top_matches_rank_open_matches_by_deep_score_else_fast() -> None:
    user = make_user()
    matches = seed(
        user,
        [
            {"fast": 70},
            {"fast": 60, "deep": deep(95, "strong")},
            {"stage": "rejected", "fast": 99},
            {},
        ],
    )
    assert top_match_ids_for_jobs(user.pk, job_ids(matches), 5) == ids([matches[1], matches[0]])
    assert top_match_ids_for_jobs(user.pk, job_ids(matches), 1) == ids(matches[1:2])
    assert top_match_ids_for_jobs(user.pk, [], 5) == []


# --- the gates, approve_each ---------------------------------------------------------------------


def test_approve_each_asks_evaluates_proposes_shortlists_and_completes(llm: FakeLLM) -> None:
    user = make_user(with_resume=True)
    matches = seed(
        user,
        [
            {"fast": 92},  # 0 → strong
            {"fast": 78},  # 1 → apply
            {"fast": 64},  # 2 → not in the top 3 once 4 is counted
            {"stage": "archived", "fast": 40},  # 3
            {"stage": "shortlisted", "fast": 88},  # 4 → selected, then left out by the user
        ],
        llm,
    )
    jobs = job_ids(matches)
    run = search_run(
        user, "approve_each", jobs, candidates=5, newMatches=5, scored=5, autoArchived=1
    )

    gate(run, settings(), jobs)
    [gate1] = approvals_for(run)
    assert (gate1.kind, gate1.status) == ("deep_eval_batch", "pending")
    assert gate1.title == "Deep-evaluate 3 roles (under a cent)"  # the fake model costs nothing
    payload = gate1.payload
    assert [item["matchId"] for item in payload["items"]] == ids(
        [matches[0], matches[4], matches[1]]
    )
    assert payload["items"][0] == {
        "matchId": str(matches[0].id),
        "jobId": matches[0].job_id,
        "title": matches[0].job.title,
        "company": matches[0].job.company_name,
        "fastScore": 92,
    }
    assert payload["estimate"] == {"usd": 0.0, "perItemUsd": 0.0, "model": "fake:evaluate"}
    assert payload["note"] is None
    paused = refreshed(run)
    assert paused.status == "awaiting_approval"
    assert paused.stats["phase"] == "awaiting_evaluation" and paused.stats["toEvaluate"] == 3
    assert deferred() == []
    requested = events_for(run)[-1]
    assert requested.kind == "approval.requested" and requested.approval_id == gate1.id
    assert requested.message == "Waiting for your go-ahead to deep-evaluate 3 roles (under a cent)."

    # The user leaves out the one they had already shortlisted.
    decide(user, gate1, approved=True, itemIds=ids([matches[0], matches[1]]))
    [job] = deferred("search.evaluate")
    assert job.args == {"run_id": str(run.id), "approval_id": str(gate1.id)}
    assert (job.lock, job.queueing_lock) == (f"run:{run.id}", f"approval:{gate1.id}")
    run_deferred()
    assert sorted(call.title for call in llm.of("deep")) == sorted(
        [matches[0].job.title, matches[1].job.title]
    )

    _, gate2 = approvals_for(run)
    assert (gate2.kind, gate2.status, gate2.title) == (
        "shortlist_proposal",
        "pending",
        "Shortlist 2 roles",
    )
    items = gate2.payload["items"]
    assert [(i["matchId"], i["recommendation"], i["deepScore"]) for i in items] == [
        (str(matches[0].id), "strong", 92),
        (str(matches[1].id), "apply", 78),
    ]
    assert items[0]["summary"].startswith("Why ")
    paused = refreshed(run)
    assert paused.status == "awaiting_approval"
    assert {k: paused.stats[k] for k in ("phase", "evaluated", "evaluationFailed")} == {
        "phase": "awaiting_shortlist",
        "evaluated": 2,
        "evaluationFailed": 0,
    }
    assert (paused.stats["recommended"], paused.stats["proposedShortlist"]) == (2, 2)
    assert paused.stats["resumedFrom"] == str(gate1.id)

    # A repeat delivery of the first continuation changes nothing.
    redeliver("search.evaluate", run, approval_id=str(gate1.id))
    run_deferred()
    assert len(approvals_for(run)) == 2 and len(llm.of("deep")) == 2

    decide(user, gate2, via="chat", approved=True, itemIds=ids(matches[:1]))
    run_deferred()
    assert stages([matches[0], matches[1], matches[4]]) == ["shortlisted", "found", "shortlisted"]

    done = refreshed(run)
    assert done.status == "completed" and done.finished_at is not None
    assert (done.stats["phase"], done.stats["shortlisted"]) == ("done", 1)
    assert done.output == {
        "candidates": 5,
        "newMatches": 5,
        "scored": 5,
        "autoArchived": 1,
        "evaluated": 2,
        "shortlisted": 1,
        "strong": 2,  # 92 (deep) and 88 (fast): open matches scoring 80 or more
        # Deep score where there is one, else fast; closed matches excluded.
        "topMatchIds": ids([matches[0], matches[4], matches[1], matches[2]]),
    }
    assert kinds(run) == [
        "approval.requested",
        "approval.decided",
        "evaluation.start",
        "evaluation.progress",
        "evaluation.done",
        "approval.requested",
        "approval.decided",
        "shortlist.done",
        "run.completed",
    ]
    messages = {event.kind: event.message for event in events_for(run)}
    assert messages["evaluation.start"] == "Deep-evaluating 2 roles against your resume"
    assert messages["evaluation.progress"] == "Evaluated 2 of 2"
    assert messages["evaluation.done"] == "Deep evaluation done: 1 strong, 1 worth applying."
    assert messages["shortlist.done"] == "Shortlisted 1 role."
    completed = last_event(run)
    assert completed.message == (
        "Found 5 roles, scored 5, deep-evaluated 2. 2 look worth applying to; 1 shortlisted."
    )
    assert completed.data == done.output
    # Every model call of the run is ledgered against it.
    rows = LlmCall.objects.unscoped("tests").filter(run_id=run.id)
    assert sorted(rows.values_list("purpose", flat=True)) == ["evaluate", "evaluate"]
    assert set(rows.values_list("user_id", flat=True)) == {user.pk}


def test_completes_with_nothing_evaluated_when_the_gate_is_declined(llm: FakeLLM) -> None:
    user = make_user(with_resume=True)
    matches = seed(user, [{"fast": 90}, {"fast": 80}], llm)
    run = search_run(user, "approve_each", job_ids(matches), candidates=2, newMatches=2, scored=2)

    gate(run, settings(), job_ids(matches))
    [gate1] = approvals_for(run)
    decide(user, gate1, approved=False)
    run_deferred()

    assert llm.of("deep") == []
    done = refreshed(run)
    assert done.status == "completed"
    assert (done.stats["phase"], done.stats["evaluated"]) == ("done", 0)
    assert done.output is not None
    assert (done.output["evaluated"], done.output["shortlisted"]) == (0, 0)
    assert kinds(run) == [
        "approval.requested",
        "approval.decided",
        "evaluation.skipped",
        "run.completed",
    ]
    skipped = events_for(run)[2]
    assert (skipped.message, skipped.level) == (
        "Skipped the deep evaluation, as you asked.",
        "info",
    )
    assert skipped.approval_id == gate1.id


def test_does_nothing_for_a_run_stopped_while_it_waited(llm: FakeLLM) -> None:
    user = make_user(with_resume=True)
    matches = seed(user, [{"fast": 90}], llm)
    run = search_run(user, "approve_each", job_ids(matches))
    gate(run, settings(), job_ids(matches))
    [gate1] = approvals_for(run)

    assert lifecycle.cancel(user.pk, run.id)
    assert approvals.decide(user.pk, gate1.id, {"approved": True}, "ui").outcome == "expired"
    redeliver("search.evaluate", run, approval_id=str(gate1.id))  # a continuation in flight
    run_deferred()

    assert llm.of("deep") == []
    assert refreshed(run).status == "cancelled"
    assert approvals_for(run)[0].status == "expired"
    assert kinds(run) == ["approval.requested", "run.cancelled"]


def test_keeps_going_past_a_failed_evaluation_and_fails_when_every_one_fails(llm: FakeLLM) -> None:
    user = make_user(with_resume=True)
    partial = seed(user, [{"fast": 90}, {"fast": 86}], llm)
    llm.fail_deep.add(partial[1].job.title)
    run = search_run(user, "approve_each", job_ids(partial))
    gate(run, settings(), job_ids(partial))
    decide(user, approvals_for(run)[0], approved=True)
    run_deferred()

    stats = refreshed(run).stats
    assert (stats["evaluated"], stats["evaluationFailed"]) == (1, 1)
    failed = next(event for event in events_for(run) if event.kind == "evaluation.failed")
    assert failed.message == (
        "1 role could not be evaluated and was left as it was: model timed out"
    )
    assert failed.data == {"failed": [{"matchId": str(partial[1].id), "error": "model timed out"}]}
    # Still reaches the shortlist gate with the one that worked.
    _, proposal = approvals_for(run)
    assert [item["matchId"] for item in proposal.payload["items"]] == ids(partial[:1])

    doomed = seed(user, [{"fast": 90}], llm)
    llm.fail_deep.add(doomed[0].job.title)
    failing = search_run(user, "approve_each", job_ids(doomed))
    gate(failing, settings(), job_ids(doomed))
    decide(user, approvals_for(failing)[0], approved=True)
    run_deferred()

    failed_run = refreshed(failing)
    assert failed_run.status == "failed"
    assert failed_run.error == "Deep evaluation failed for every role: model timed out"
    assert kinds(failing)[-1] == "run.failed"
    assert last_event(failing).message == failed_run.error


def test_skips_the_evaluation_without_a_resume(llm: FakeLLM) -> None:
    user = make_user()
    matches = seed(user, [{"fast": 90}], llm)
    run = search_run(user, "approve_each", job_ids(matches), candidates=1, newMatches=1, scored=1)
    gate(run, settings(), job_ids(matches))

    assert approvals_for(run) == []
    assert refreshed(run).status == "completed"
    skipped = events_for(run)[0]
    assert (skipped.kind, skipped.level) == ("evaluation.skipped", "warn")
    assert skipped.message == (
        "Skipped the deep evaluation: there is no resume on file to judge against."
    )
    assert last_event(run).message == "Found 1 role, scored 1. 1 looks strong."


# --- the gates, autopilot ------------------------------------------------------------------------


def test_autopilot_evaluates_and_shortlists_without_stopping(llm: FakeLLM) -> None:
    user = make_user(with_resume=True)
    matches = seed(user, [{"fast": 91}, {"fast": 72}, {"fast": 58}], llm)
    run = search_run(user, "autopilot", job_ids(matches), candidates=3, newMatches=3, scored=3)

    gate(run, settings(), job_ids(matches))
    [gate1] = approvals_for(run)
    assert (gate1.status, gate1.decided_via) == ("auto_approved", "autopilot")
    running = refreshed(run)
    assert running.status == "running"
    assert (running.stats["phase"], running.stats["toEvaluate"]) == ("evaluating", 3)
    assert [job.task_name for job in deferred()] == ["search.evaluate"]

    run_deferred()

    _, gate2 = approvals_for(run)
    assert (gate2.kind, gate2.status) == ("shortlist_proposal", "auto_approved")
    assert stages(matches) == ["shortlisted", "shortlisted", "found"]
    done = refreshed(run)
    assert done.status == "completed"
    assert done.output is not None
    assert (done.output["evaluated"], done.output["shortlisted"]) == (3, 2)
    assert done.output["strong"] == 1
    assert done.stats["resumedFrom"] == str(gate2.id)
    assert kinds(run) == [
        "approval.auto",
        "evaluation.start",
        "evaluation.progress",
        "evaluation.done",
        "approval.auto",
        "shortlist.done",
        "run.completed",
    ]
    assert events_for(run)[0].message == "Autopilot: deep-evaluating 3 roles (under a cent)."
    assert events_for(run)[4].message == (
        "Autopilot: shortlisting 2 roles that look worth applying to."
    )


def test_autopilot_gate_two_writes_the_shortlisting_phase(llm: FakeLLM) -> None:
    user = make_user(with_resume=True)
    matches = seed(user, [{"deep": deep(90, "strong")}], llm)
    rows = get_match_rows(user.pk, ids(matches))
    for mode, phase in (("autopilot", "shortlisting"), ("approve_each", "awaiting_shortlist")):
        run = search_run(user, mode, job_ids(matches))
        asyncio.run(evaluate.propose_shortlist(run, rows))
        assert refreshed(run).stats["phase"] == phase
        assert refreshed(run).stats["proposedShortlist"] == 1


def test_completes_straight_away_when_there_is_nothing_to_evaluate(llm: FakeLLM) -> None:
    user = make_user(with_resume=True)
    matches = seed(user, [{"fast": 30}, {"stage": "archived", "fast": 20}], llm)
    run = search_run(
        user, "autopilot", job_ids(matches), candidates=2, newMatches=2, scored=2, autoArchived=1
    )
    gate(run, settings(), job_ids(matches))

    assert approvals_for(run) == []
    assert deferred() == []
    done = refreshed(run)
    assert done.status == "completed"
    assert done.output is not None and done.output["evaluated"] == 0
    assert isinstance(done.output["topMatchIds"], list)
    assert last_event(run).message == "Found 2 roles, scored 2. 1 was archived as too weak."


def test_skips_deep_evaluation_entirely_when_it_is_turned_off(llm: FakeLLM) -> None:
    user = make_user(with_resume=True)
    matches = seed(user, [{"fast": 95}], llm)
    run = search_run(user, "autopilot", job_ids(matches), candidates=1, newMatches=1, scored=1)
    gate(run, settings(deep_eval_top_n=0), job_ids(matches))
    assert approvals_for(run) == []
    assert refreshed(run).status == "completed"


# --- evaluation on demand ------------------------------------------------------------------------


def test_on_demand_refuses_without_a_resume_then_evaluates_and_reuses(llm: FakeLLM) -> None:
    user = make_user()
    matches = seed(user, [{"fast": 92}], llm)
    with pytest.raises(NoResumeError):
        start_match_evaluation(user.pk, ids(matches))

    make_resume(user)
    first = start_match_evaluation(user.pk, ids(matches))
    assert not first.reused
    assert first.run.kind == "rank_deep"
    assert first.run.input == {"matchIds": ids(matches)}
    # Asking again while it is queued returns the same run rather than paying twice.
    again = start_match_evaluation(user.pk, ids(matches) + ["junk"])
    assert again.reused and again.run.id == first.run.id
    [job] = deferred("match.evaluate")
    assert job.args == {"run_id": str(first.run.id)}
    assert (job.queue, job.lock) == ("interactive", f"run:{first.run.id}")

    run_deferred()
    done = refreshed(first.run)
    assert done.status == "completed"
    assert done.output == {"evaluated": 1, "matchIds": ids(matches)}
    assert (done.stats["phase"], done.stats["evaluated"], done.stats["evaluationFailed"]) == (
        "done",
        1,
        0,
    )
    [row] = get_match_rows(user.pk, ids(matches))
    assert (row.deep_score, row.recommendation) == (92, "strong")
    assert kinds(first.run) == ["run.started", "evaluation.progress", "run.completed"]
    assert events_for(first.run)[0].message == f"Evaluating {row.title} at {row.company}"
    assert last_event(first.run).message == (
        f"Evaluated {row.title} at {row.company}: 92, a strong match."
    )

    # A repeat delivery does not run it twice.
    redeliver("match.evaluate", first.run)
    run_deferred()
    assert len(llm.of("deep")) == 1


def test_on_demand_ignores_matches_that_are_not_the_users() -> None:
    user = make_user(with_resume=True)
    theirs = seed(make_user(), [{"fast": 70}])
    with pytest.raises(UnknownMatchError, match="There is no such match to evaluate."):
        start_match_evaluation(user.pk, [*ids(theirs), "junk"])


def test_a_skip_verdict_archives_an_untouched_match(llm: FakeLLM) -> None:
    user = make_user(with_resume=True)
    matches = seed(user, [{"fast": 40}, {"stage": "applied", "fast": 41}], llm)
    JobMatch.objects.for_user(user.pk).filter(id=matches[0].id).update(notes="Mine.")
    start_match_evaluation(user.pk, ids(matches))
    run_deferred()
    found = {m.id: m for m in JobMatch.objects.for_user(user.pk)}
    assert found[matches[0].id].stage == "archived"
    assert found[matches[0].id].notes == (
        "Mine.\n\nArchived: the deep review recommended skipping this one."
    )
    assert found[matches[1].id].stage == "applied"  # the user's move stands


# --- the whole search ----------------------------------------------------------------------------


@pytest.fixture
def mirror(llm: FakeLLM) -> dict[str, Any]:
    """A fresh board with postings the sample profile (Bay Area, hybrid) will and won't match."""
    llm.title_include = ["software engineer", "backend engineer"]
    company = fresh_company(name="Acme")
    austin = {
        "locations": ["Austin, TX"],
        "location_norm": {"raw": "Austin, TX", "city": "Austin", "region": "TX", "country": "US"},
        "remote": "onsite",
    }
    jobs = {
        "payments": make_job(company=company, title="Software Engineer, Payments"),
        "backend": make_job(company=company, title="Backend Engineer, Platform"),
        "tools": make_job(company=company, title="Software Engineer, Tools"),
        "legacy": make_job(company=company, title="Software Engineer, Legacy"),
        "sales": make_job(company=company, title="Account Executive"),
        "austin": make_job(company=company, title="Software Engineer, Austin", **austin),
    }
    llm.fast.update(
        {
            jobs["payments"].title: 92,
            jobs["backend"].title: 78,
            jobs["tools"].title: 60,
            jobs["legacy"].title: 40,
        }
    )
    return jobs


def test_a_whole_search_through_the_api_approve_each(
    mirror: dict[str, Any], llm: FakeLLM, signed_in: Any
) -> None:
    api, user = signed_in(with_resume=True)
    started = api.post("/api/searches", {})
    assert started.status_code == 202
    run_id = started.json()["runId"]
    assert started.json() == {
        "ok": True,
        "runId": run_id,
        "message": "Search started. Foray will check with you before the deep evaluation.",
    }
    run = Run.objects.for_user(user.pk).get(id=run_id)
    run_deferred()

    paused = refreshed(run)
    assert paused.status == "awaiting_approval"
    assert kinds(run) == [
        "run.started",
        "search.criteria",
        "source.aggregator.skipped",
        "prefilter.done",
        "scoring.start",
        "scoring.progress",
        "scoring.done",
        "approval.requested",
    ]
    said = [(event.message, event.level) for event in events_for(run)]
    assert said == [
        ("Searching: Search with my saved preferences", "info"),
        (
            "Looking for roles that fit your saved preferences. "
            "Titles like: software engineer, backend engineer.",
            "info",
        ),
        ("JSearch unavailable: JSEARCH_ENABLED is false", "info"),
        ("4 of 6 open postings match your criteria", "info"),
        ("Scoring 4 roles against your profile", "info"),
        ("Scored 4 of 4", "debug"),
        ("Scored 4 roles; 1 came in under 55 and was archived.", "info"),
        ("Waiting for your go-ahead to deep-evaluate 3 roles (under a cent).", "info"),
    ]
    assert events_for(run)[3].data == {"activeInMirror": 6, "selected": 4, "cappedAt": None}
    assert {
        k: paused.stats[k] for k in ("phase", "toEvaluate", "candidates", "activeInMirror")
    } == {
        "phase": "awaiting_evaluation",
        "toEvaluate": 3,
        "candidates": 4,
        "activeInMirror": 6,
    }
    assert {k: paused.stats[k] for k in ("matches", "newMatches", "scored", "autoArchived")} == {
        "matches": 4,
        "newMatches": 4,
        "scored": 4,
        "autoArchived": 1,
    }
    assert paused.input["brief"] == EMPTY_BRIEF.dump()
    assert paused.input["criteria"]["title_include"] == ["software engineer", "backend engineer"]
    assert sorted(paused.input["candidateJobIds"]) == sorted(
        mirror[key].id for key in ("payments", "backend", "tools", "legacy")
    )
    legacy = JobMatch.objects.for_user(user.pk).get(job=mirror["legacy"])
    assert (legacy.stage, legacy.fast_score) == ("archived", 40)
    assert legacy.notes == "Auto-archived: scored 40, below the 55 threshold."

    [gate1] = approvals_for(run)
    answer = api.post(f"/api/approvals/{gate1.id}/decision", {"approved": True})
    assert answer.json() == {
        "ok": True,
        "message": "Approved. Deep-evaluating 3 roles in the background.",
    }
    assert refreshed(run).status == "running"
    run_deferred()

    _, gate2 = approvals_for(run)
    assert gate2.status == "pending"
    assert [e.message for e in events_for(run)[-4:]] == [
        "Deep-evaluating 3 roles against your resume",
        "Evaluated 3 of 3",
        "Deep evaluation done: 1 strong, 1 worth applying, 1 maybe.",
        "2 roles look worth applying to. Shortlist them?",
    ]
    api.post(f"/api/approvals/{gate2.id}/decision", {"approved": True})
    run_deferred()

    done = refreshed(run)
    assert done.status == "completed"
    assert kinds(run)[8:] == [
        "approval.decided",
        "evaluation.start",
        "evaluation.progress",
        "evaluation.done",
        "approval.requested",
        "approval.decided",
        "shortlist.done",
        "run.completed",
    ]
    assert last_event(run).message == (
        "Found 4 roles, scored 4, deep-evaluated 3. 2 look worth applying to; 2 shortlisted."
    )
    shortlisted = JobMatch.objects.for_user(user.pk).filter(stage="shortlisted")
    assert sorted(shortlisted.values_list("job_id", flat=True)) == sorted(
        [mirror["payments"].id, mirror["backend"].id]
    )
    assert done.output is not None
    assert done.output["strong"] == 1
    assert len(done.output["topMatchIds"]) == 3
    # One derive call, four fast scores, three reviews: every one ledgered against the run.
    calls = LlmCall.objects.unscoped("tests").filter(run_id=run.id)
    assert sorted(calls.values_list("purpose", flat=True)) == ["evaluate"] * 3 + ["score"] * 5
    assert set(calls.values_list("user_id", flat=True)) == {user.pk}


def test_a_whole_search_on_autopilot_and_a_repeat_that_finds_nothing_new(
    mirror: dict[str, Any], llm: FakeLLM
) -> None:
    user = make_user(with_resume=True)
    first = search.start_search(user.pk, prompt="backend roles", mode="autopilot")
    assert not first.reused
    run_deferred()
    assert refreshed(first.run).status == "completed"
    assert kinds(first.run) == [
        "run.started",
        "search.criteria",
        "source.aggregator.skipped",
        "prefilter.done",
        "scoring.start",
        "scoring.progress",
        "scoring.done",
        "approval.auto",
        "evaluation.start",
        "evaluation.progress",
        "evaluation.done",
        "approval.auto",
        "shortlist.done",
        "run.completed",
    ]
    assert events_for(first.run)[0].message == "Searching: backend roles"
    derive = llm.of("derive")[0]
    assert "This search request:\nbackend roles" in derive.prompt

    again = search.start_search(user.pk, prompt="backend roles", mode="autopilot")
    run_deferred()
    repeat = refreshed(again.run)
    assert repeat.status == "completed"
    # Nothing new, nothing re-scored (already scored), nothing left to evaluate.
    assert len(llm.of("fast")) == 4
    # The archived role stays dismissed; the rest are known and already scored.
    assert last_event(again.run).message == (
        "Found 3 roles (none new). 1 looks strong, 1 is worth a look."
    )
    assert repeat.output is not None
    assert (repeat.output["newMatches"], repeat.output["scored"]) == (0, 0)
    assert repeat.stats["phase"] == "done"
    scoring_done = next(e for e in events_for(again.run) if e.kind == "scoring.done")
    assert scoring_done.message == "Nothing new to score: every one of these was scored before."


def test_brief_overrides_saved_preferences(llm: FakeLLM) -> None:
    """R15: a request for entry-level roles at one company is filtered, scored and judged as
    one, whatever the saved preferences say, and the saved preferences are untouched."""
    saved = SAMPLE_PREFERENCES.model_copy(
        update={"target_roles": ["Senior Backend Engineer"], "seniority": "senior"}
    )
    user = make_user(with_resume=True, preferences=saved.dump())
    acme = fresh_company(name="Acme")
    other = fresh_company(name="Globex")
    new_grad = make_job(company=acme, title="Software Engineer, New Grad")
    make_job(company=acme, title="Senior Software Engineer")
    make_job(company=other, title="Senior Backend Engineer")
    make_job(company=other, title="Software Engineer, New Grad")
    llm.title_include = ["software engineer"]
    llm.fast[new_grad.title] = 90

    hints = {"roles": ["software engineer"], "seniority": "entry", "companies": ["Acme"]}
    run = search.start_search(user.pk, prompt="entry-level roles at Acme", hints=hints).run
    run_deferred()

    paused = refreshed(run)
    assert paused.input["brief"] == {
        "roles": ["software engineer"],
        "seniority": "entry",
        "locations": None,
        "remote": None,
        "companies": ["Acme"],
        "focus": None,
    }
    assert paused.input["candidateJobIds"] == [new_grad.id]
    assert paused.stats["companies"] == ["Acme"] and paused.stats["companiesMissing"] == []
    derive = llm.of("derive")[0]
    assert '"seniority": "entry"' in derive.prompt.split("Already resolved")[1]
    [fast] = llm.of("fast")
    assert '"target_roles":["software engineer"],"seniority":"entry"' in fast.instructions
    criteria = next(e for e in events_for(run) if e.kind == "search.criteria")
    assert criteria.message.startswith(
        "Looking for entry-level software engineer roles at Acme. Titles like: software engineer"
    )
    # A search limited to companies whose boards are all known skips the aggregator.
    assert "source.aggregator.skipped" not in kinds(run)

    decide(user, approvals_for(run)[0], approved=True)
    run_deferred()
    [review] = llm.of("deep")
    assert '- seniority: "entry"' in review.instructions
    assert '- target_roles: ["software engineer"]' in review.instructions
    # Saved preferences are never changed by a search.
    assert User.objects.get(pk=user.pk).preferences == saved.dump()


def test_named_companies_are_looked_up_refreshed_and_reported(
    llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = make_user(with_resume=True)
    stale = make_company(name="Acme", last_fetched_at=None)
    make_job(company=stale, title="Software Engineer, Acme")
    discovered: list[str] = []
    refreshed_slugs: list[str] = []

    async def discover(client: Any, name: str, **kwargs: Any) -> Any:
        discovered.append(name)
        return type("Result", (), {"registered": None})()

    async def refresh(client: Any, companies: list[Company]) -> list[RefreshOutcome]:
        refreshed_slugs.extend(company.slug for company in companies)
        assert not refresh_module.MULTI_BOARD_GATE.locked()  # not a stale-set refresh
        return [RefreshOutcome(company.name, company.slug, "ok", 1) for company in companies]

    monkeypatch.setattr(search, "discover_company", discover)
    monkeypatch.setattr(search, "refresh_companies", refresh)
    hints = {"companies": ["Acme", "Initech"]}
    run = search.start_search(user.pk, prompt="roles at Acme or Initech", hints=hints).run
    run_deferred()

    assert discovered == ["Initech"] and refreshed_slugs == [stale.slug]
    said = {event.kind: (event.message, event.level) for event in events_for(run)}
    assert said["search.companies.missing"] == (
        "Couldn't find a job board for Initech, so its roles only come from the job aggregator. "
        "Ask Foray to add the company with its careers page link to search it directly.",
        "warn",
    )
    assert said["source.refresh.start"] == ("Refreshing Acme", "info")
    assert said["source.refresh.done"] == ("Refreshed 1/1 board", "info")
    stats = refreshed(run).stats
    assert (stats["companies"], stats["companiesMissing"]) == (["Acme"], ["Initech"])
    assert (stats["boardsRefreshed"], stats["boardsFailed"]) == (1, 0)
    # Initech has no board, so the aggregator still runs (and is skipped: JSearch is off).
    assert "source.aggregator.skipped" in said


def test_an_unlimited_search_refreshes_the_stale_set_under_the_gate(
    llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = make_user(with_resume=True)
    make_company(name="Never fetched", last_fetched_at=None)
    fresh_company(name="Fresh")
    held: list[bool] = []

    async def refresh(client: Any, companies: list[Company]) -> list[RefreshOutcome]:
        held.append(refresh_module.MULTI_BOARD_GATE.locked())
        return [
            RefreshOutcome(c.name, c.slug, "ok" if i == 0 else "error", 0)
            for i, c in enumerate(companies)
        ]

    monkeypatch.setattr(search, "refresh_companies", refresh)
    run = search.start_search(user.pk, prompt="anything").run
    run_deferred()
    assert held == [True]
    said = {event.kind: event.message for event in events_for(run)}
    assert said["source.refresh.start"] == "Refreshing 1 stale job boards"
    assert said["source.refresh.done"] == "Refreshed 1/1 board"


def test_a_search_without_required_preferences_fails_with_the_reason(llm: FakeLLM) -> None:
    user = make_user()
    run = search.start_search(user.pk, prompt="anything").run
    run_deferred()
    failed = refreshed(run)
    assert failed.status == "failed"
    assert failed.error == (
        "Cannot search yet: Target roles, Seniority, Locations, Remote preference, "
        "Work authorisation not known"
    )
    assert kinds(run) == ["run.started", "run.failed"]
    assert llm.calls == []


def test_a_run_failed_during_scoring_stops_spending(
    mirror: dict[str, Any], llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step 13: the reaper failed a search whose worker had only been asleep; its task woke up
    and went on scoring for 45 minutes, paying for each call and writing scores to a run that
    had already ended. Any terminal status stops a pipeline, not only `cancelled`. One posting
    a batch, so the stop check runs between each."""
    monkeypatch.setattr(fast_score, "BATCH", 1)
    user = make_user(with_resume=True)
    run = search.start_search(user.pk, prompt="anything").run

    def reaped() -> None:  # on the model's thread, after the first score is answered
        if lifecycle.fail(run.id, reaper.INTERRUPTED):
            connections.close_all()

    llm.after_fast = reaped
    run_deferred()

    failed = refreshed(run)
    assert (failed.status, failed.error) == ("failed", reaper.INTERRUPTED)
    assert len(llm.of("fast")) == 1  # the other three postings were never sent
    assert kinds(run)[-1] == "run.failed"
    assert approvals_for(run) == []
    scored = JobMatch.objects.unscoped("tests").filter(user_id=user.pk, fast_score__isnull=False)
    assert scored.count() == 1


def test_a_stop_during_scoring_keeps_the_stop_last(mirror: dict[str, Any], llm: FakeLLM) -> None:
    user = make_user(with_resume=True)
    run = search.start_search(user.pk, prompt="anything").run

    def press_stop() -> None:  # on the model's thread, as the user would, mid-scoring
        lifecycle.cancel(user.pk, run.id)
        connections.close_all()

    llm.after_fast = press_stop
    run_deferred()

    stopped = refreshed(run)
    assert stopped.status == "cancelled"
    assert kinds(run) == [
        "run.started",
        "search.criteria",
        "source.aggregator.skipped",
        "prefilter.done",
        "scoring.start",
        "run.cancelled",
    ]
    assert last_event(run).message == "Stopped. Everything found so far was kept."
    assert approvals_for(run) == []
