"""Run lifecycle (design §9.2, §9.4; R1, R2, R4, R17).

- Starting is atomic: a run and its first job commit together or not at all (R1).
- One active run per `(user, dedupe_key)`, even under concurrency (R2).
- A run finishes only through `complete`, `fail` or `cancel`, each conditional on an active
  status, each emitting exactly one terminal event, the run's last (R4); a late finish never
  overwrites a Stop.
- `run_step` claims, attributes, times out, turns failures into feed sentences, and fails a run a
  handler forgot to finish (R4).
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import connections, transaction

from apps.core.queue import app
from apps.runs import approvals, events, lifecycle
from apps.runs.catalogue import (
    FAILURE_PREFIX,
    FOLLOW_UP_KINDS,
    ApprovalStatus,
    EventKind,
    RunKind,
    RunStatus,
)
from apps.runs.models import Approval, Event, Run
from tests.factories import make_approval, make_run, make_user
from tests.queue import run_deferred
from tests.runs import support
from tests.runs.support import event_kinds, jobs, refresh

TERMINAL_KINDS = {EventKind.RUN_COMPLETED, EventKind.RUN_FAILED, EventKind.RUN_CANCELLED}


class Rollback(Exception):
    pass


def _start(user, *, kind: str = RunKind.SEARCH, dedupe_key: str | None = "search", **task_kwargs):
    return lifecycle.start_run(
        user.pk,
        kind,
        input={"prompt": "Find me a role"},
        dedupe_key=dedupe_key,
        task=support.step,
        task_kwargs=task_kwargs,
    )


def _terminal_events(run_id) -> list[Event]:
    return list(
        Event.objects.unscoped("tests")
        .filter(run_id=run_id, kind__in=TERMINAL_KINDS)
        .order_by("id")
    )


def _last_event(run_id) -> Event:
    event = Event.objects.unscoped("tests").filter(run_id=run_id).order_by("-id").first()
    assert event is not None
    return event


# --- starting ------------------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_start_is_atomic() -> None:
    user = make_user()
    with pytest.raises(Rollback), transaction.atomic():
        run, reused = _start(user, action="complete")
        assert not reused
        [job] = jobs(lock=f"run:{run.id}")
        assert job["task_name"] == "tests.step" and job["queueing_lock"] == f"run:{run.id}"
        assert not support.job_seen_elsewhere(job["id"])
        raise Rollback
    assert not Run.objects.for_user(user.pk).exists()
    assert jobs() == []

    with transaction.atomic():
        run, reused = _start(user, action="complete")
        [job] = jobs(lock=f"run:{run.id}")
        assert not support.job_seen_elsewhere(job["id"])  # not before the commit
    assert support.job_seen_elsewhere(job["id"])
    assert (job["queue"], job["status"], job["args"]) == (
        "background",
        "todo",
        {"run_id": str(run.id), "action": "complete"},
    )
    assert refresh(run).status == RunStatus.QUEUED


@pytest.mark.django_db
def test_start_validates_kind_and_mode() -> None:
    user = make_user()
    with pytest.raises(ValueError):
        lifecycle.start_run(user.pk, "nope", input={}, dedupe_key=None, task=support.step)
    with pytest.raises(ValueError):
        lifecycle.start_run(
            user.pk, RunKind.SEARCH, input={}, mode="yolo", dedupe_key=None, task=support.step
        )


@pytest.mark.django_db(transaction=True)
def test_one_active_run_per_key_under_concurrency() -> None:
    user = make_user()
    barrier = threading.Barrier(8)

    def start() -> tuple[uuid.UUID, bool]:
        try:
            barrier.wait(10)
            run, reused = _start(user, dedupe_key="evaluate:abc")
            return run.id, reused
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: start(), range(8)))

    assert len({run_id for run_id, _ in results}) == 1
    assert sorted(reused for _, reused in results) == [False] + [True] * 7
    assert Run.objects.for_user(user.pk).count() == 1
    assert len(jobs()) == 1


@pytest.mark.django_db
def test_a_finished_run_frees_its_key() -> None:
    user = make_user()
    first, _ = _start(user)
    again, reused = _start(user)
    assert reused and again.id == first.id
    assert lifecycle.fail(first.id, "It broke.")
    second, reused = _start(user)
    assert not reused and second.id != first.id
    # A run without a key never dedupes.
    third, reused = _start(user, dedupe_key=None)
    fourth, reused_too = _start(user, dedupe_key=None)
    assert not reused and not reused_too and third.id != fourth.id


@pytest.mark.django_db
def test_start_inside_a_caller_transaction_survives_a_reuse() -> None:
    user = make_user()
    with transaction.atomic():
        first, _ = _start(user)
        again, reused = _start(user)  # the conflict rolls back a savepoint only
        assert reused and again.id == first.id
        assert Run.objects.for_user(user.pk).count() == 1


# --- claim and progress --------------------------------------------------------------------------


@pytest.mark.django_db
def test_claim_takes_only_queued_or_running_runs() -> None:
    user = make_user()
    queued = make_run(user, status="queued")
    claimed = lifecycle.claim(queued.id)
    assert claimed is not None and claimed.status == RunStatus.RUNNING
    started = claimed.started_at
    again = lifecycle.claim(queued.id)  # a redelivery, or the continuation after a gate
    assert again is not None and again.started_at == started  # coalesce keeps the first start
    for status in ("awaiting_approval", "completed", "failed", "cancelled"):
        assert lifecycle.claim(make_run(user, status=status).id) is None
    assert lifecycle.claim(uuid.uuid4()) is None


@pytest.mark.django_db
def test_merge_stats_and_input_are_shallow_merges() -> None:
    user = make_user()
    run = make_run(user, status="running", stats={"phase": "scoring", "scored": 3})
    lifecycle.merge_stats(run.id, phase="evaluating", toEvaluate=5)
    lifecycle.merge_stats(run.id, {"evaluated": 1})
    lifecycle.merge_input(run.id, brief={"roles": ["Engineer"]})
    run = refresh(run)
    assert run.stats == {"phase": "evaluating", "scored": 3, "toEvaluate": 5, "evaluated": 1}
    assert run.input["brief"] == {"roles": ["Engineer"]}
    assert run.input["prompt"] == "Search with my saved preferences"


# --- finishing -----------------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("finish", ["complete", "fail", "cancel"])
def test_one_terminal_event_per_run(finish: str) -> None:
    user = make_user()
    run = make_run(user, status="running")
    events.emit(user.pk, EventKind.SCORING_START, "Scoring 3 roles.", run_id=run.id)

    def finishers():
        return [
            lambda: lifecycle.complete(run.id, {"evaluated": 1}, "Evaluated 1 role."),
            lambda: lifecycle.fail(run.id, "It broke."),
            lambda: lifecycle.cancel(user.pk, run.id),
        ]

    first = {"complete": 0, "fail": 1, "cancel": 2}[finish]
    assert finishers()[first]() is True
    assert [f() for f in finishers()] == [False, False, False]  # every later finish is refused

    [terminal] = _terminal_events(run.id)
    assert terminal.kind == {"complete": "run.completed", "fail": "run.failed"}.get(
        finish, "run.cancelled"
    )
    assert _last_event(run.id).id == terminal.id
    run = refresh(run)
    assert run.status == {"complete": "completed", "fail": "failed"}.get(finish, "cancelled")
    assert run.finished_at is not None


@pytest.mark.django_db
def test_complete_and_fail_write_what_the_ui_reads() -> None:
    user = make_user()
    done = make_run(user, status="running", stats={"phase": "evaluating", "evaluated": 2})
    assert lifecycle.complete(done.id, {"evaluated": 2, "matchIds": []}, "Evaluated 2 roles.")
    done = refresh(done)
    assert done.output == {"evaluated": 2, "matchIds": []}
    assert done.stats == {"phase": "done", "evaluated": 2}
    completed = _last_event(done.id)
    assert (completed.kind, completed.level, completed.message, completed.data) == (
        "run.completed",
        "info",
        "Evaluated 2 roles.",
        {"evaluated": 2, "matchIds": []},
    )
    warned = make_run(user, status="running")
    lifecycle.complete(warned.id, None, "Your resume is ready.", level="warn")
    assert refresh(warned).output is None
    assert _last_event(warned.id).level == "warn"

    failed = make_run(user, status="running")
    lifecycle.fail(failed.id, "Tailoring failed: boom")
    failed = refresh(failed)
    assert (failed.status, failed.error) == ("failed", "Tailoring failed: boom")
    event = _last_event(failed.id)
    assert (event.kind, event.level, event.message) == ("run.failed", "error", failed.error)


@pytest.mark.django_db
def test_cancel_beats_late_complete() -> None:
    user = make_user()
    run, _ = _start(user, action="complete")
    approval = make_approval(user, run=run)
    assert lifecycle.cancel(user.pk, run.id)
    # The step finishes late: it must not overwrite the Stop.
    assert lifecycle.complete(run.id, {"evaluated": 3}, "Evaluated 3 roles.") is False
    assert lifecycle.fail(run.id, "late") is False
    run = refresh(run)
    assert (run.status, run.output, run.error) == ("cancelled", None, None)
    assert event_kinds(run.id) == ["run.cancelled"]
    assert _last_event(run.id).message == "Stopped. Everything found so far was kept."
    assert Approval.objects.for_user(user.pk).get(id=approval.id).status == "expired"
    # Its queued job is cancelled outright.
    [job] = jobs(lock=f"run:{run.id}")
    assert job["status"] == "cancelled"


@pytest.mark.django_db
def test_cancel_asks_a_running_job_to_abort() -> None:
    user = make_user()
    run = make_run(user, status="running")
    job_id = support.doing_job("tests.step", worker_id=None, lock=f"run:{run.id}")
    assert lifecycle.cancel(user.pk, run.id)
    [job] = jobs(lock=f"run:{run.id}")
    assert (job["id"], job["status"], job["abort_requested"]) == (job_id, "doing", True)


@pytest.mark.django_db
def test_cancel_is_scoped_to_the_user() -> None:
    owner, other = make_user(), make_user()
    run = make_run(owner, status="running")
    assert lifecycle.cancel(other.pk, run.id) is False
    assert lifecycle.cancel(owner.pk, "not-a-uuid") is False
    assert lifecycle.cancel(owner.pk, uuid.uuid4()) is False
    assert refresh(run).status == "running"
    assert lifecycle.is_cancelled(run.id) is False
    assert lifecycle.cancel(owner.pk, run.id)
    assert lifecycle.is_cancelled(run.id) is True


# --- run_step ------------------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_handler_that_forgets_to_finish_fails() -> None:
    user = make_user()
    run, _ = _start(user, action="forget")
    run_deferred()
    run = refresh(run)
    assert (run.status, run.error) == ("failed", lifecycle.ABANDONED)
    assert event_kinds(run.id) == ["run.failed"]
    # A step that raised a gate is paused, not abandoned.
    gated, _ = _start(user, dedupe_key=None, action="gate")
    run_deferred(["background"])
    gated = refresh(gated)
    assert gated.status == "awaiting_approval"
    assert gated.stats == {"phase": "awaiting_evaluation", "toEvaluate": 3}
    assert event_kinds(gated.id) == ["approval.requested"]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("action", "status", "error"),
    [
        ("complete", "completed", None),
        ("user_error", "failed", "That board is gone."),
        ("raise", "failed", "boom"),  # search shows the error alone; first line only
        (
            "budget",
            "failed",
            "This month's model budget of $90 is spent. Raise LLM_MONTHLY_BUDGET_USD to continue.",
        ),
        ("timeout", "failed", "This step took longer than 2 minutes and was stopped."),
    ],
)
def test_run_step_turns_outcomes_into_sentences(action: str, status: str, error: str) -> None:
    user = make_user()
    run, _ = _start(user, action=action)
    run_deferred()
    run = refresh(run)
    assert (run.status, run.error) == (status, error)
    assert run.started_at is not None
    [terminal] = _terminal_events(run.id)
    assert terminal.kind == f"run.{status}"
    assert _last_event(run.id).id == terminal.id
    [job] = jobs(lock=f"run:{run.id}")
    assert job["status"] == "succeeded"  # the wrapper handled it: no procrastinate retry


@pytest.mark.django_db(transaction=True)
def test_failure_prefix_names_the_kind() -> None:
    user = make_user()
    run, _ = _start(user, kind=RunKind.TAILOR, dedupe_key="tailor:m1", action="raise")
    run_deferred()
    assert refresh(run).error == "Tailoring failed: boom"


@pytest.mark.django_db(transaction=True)
def test_a_cancelled_run_is_not_claimed() -> None:
    user = make_user()
    run, _ = _start(user, action="complete")
    with transaction.atomic():
        # Cancel after the job is queued but before a worker runs it: the job is cancelled.
        assert lifecycle.cancel(user.pk, run.id)
    run_deferred()
    assert support.CALLS == []
    assert refresh(run).status == "cancelled"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("kind", list(RunKind))
def test_every_kind_finishes_with_run_event(kind: RunKind) -> None:
    """R17: whatever happens to a run of any kind, its last event is its one `run.*` final."""
    assert kind in FAILURE_PREFIX
    assert {EventKind.RUN_COMPLETED, EventKind.RUN_FAILED} <= FOLLOW_UP_KINDS
    user = make_user()
    outcomes = {
        "complete": "run.completed",
        "raise": "run.failed",
        "forget": "run.failed",
        "timeout": "run.failed",
    }
    started = {
        action: _start(user, kind=kind, dedupe_key=f"{kind}:{action}", action=action)[0]
        for action in outcomes
    }
    cancelled, _ = _start(user, kind=kind, dedupe_key=f"{kind}:cancel", action="complete")
    lifecycle.cancel(user.pk, cancelled.id)
    run_deferred()
    for action, run in [*started.items(), ("cancel", cancelled)]:
        last = _last_event(run.id)
        assert last.kind == outcomes.get(action, "run.cancelled"), (kind, action)
        assert len(_terminal_events(run.id)) == 1
        assert refresh(run).status != RunStatus.RUNNING


@pytest.mark.django_db
def test_gate_on_a_cancelled_run_is_expired_at_once() -> None:
    user = make_user()
    run = make_run(user, status="cancelled")
    approval = approvals.request_approval(
        run.id, "deep_eval_batch", "Too late", support.batch_payload()
    )
    assert approval.status == ApprovalStatus.EXPIRED
    assert event_kinds(run.id) == []
    assert jobs() == []
    assert app.tasks.get("tests.step") is not None
