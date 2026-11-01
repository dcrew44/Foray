"""A run the chat agent asks for waits for the user's go-ahead (#31).

`lifecycle.start_run(gate=...)` inserts the run `awaiting_approval` behind a pending `run_start`
approval and defers nothing. The user's yes defers the run's first step, exactly as an ungated
start would have; a no or a lapse cancels the run before it ever ran, with `run.cancelled` as its
one terminal event, last. Autopilot never answers one.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.db.models.functions import Now

from apps.chat.follow_up import Skip, load
from apps.runs import approvals, lifecycle, reaper
from apps.runs.approvals import decide
from apps.runs.models import Approval, Event, Run
from tests.factories import make_conversation, make_user
from tests.runs.support import age_run, event_kinds, jobs, refresh

pytestmark = pytest.mark.django_db

TITLE = "Look up job boards for Linear and Vercel"


def _gated(user: Any, *, mode: str = "approve_each", **fields: Any) -> tuple[Run, Approval]:
    run, reused = lifecycle.start_run(
        user.pk,
        "company_discovery",
        input={"names": ["Linear", "Vercel"], "refreshAfter": False, "prompt": "Find boards"},
        mode=mode,
        dedupe_key="add:names:test",
        task="company.add",
        gate=lifecycle.StartGate(TITLE, {"detail": "Looks for each board."}),
        **fields,
    )
    assert reused is False
    return run, Approval.objects.for_user(user.pk).get(run=run)


def _events(run: Run) -> list[Event]:
    return list(Event.objects.unscoped("tests").filter(run_id=run.id).order_by("id"))


def test_a_gated_start_waits_and_defers_nothing() -> None:
    user = make_user()
    run, gate = _gated(user)
    assert refresh(run).status == "awaiting_approval"
    assert (gate.kind, gate.status, gate.next_step) == ("run_start", "pending", "company.add")
    assert gate.payload == {"detail": "Looks for each board."}
    assert gate.expires_at is not None
    [event] = _events(run)
    assert (event.kind, event.approval_id, event.message) == (
        "approval.requested",
        gate.id,
        f"Waiting for your go-ahead: {TITLE}.",
    )
    assert jobs() == []  # nothing runs until the user answers
    assert gate.id in [a.id for a in approvals.list_pending(user.pk)]
    # Asking again finds the same run waiting, and writes nothing.
    again, reused = lifecycle.start_run(
        user.pk,
        "company_discovery",
        input={},
        dedupe_key="add:names:test",
        task="company.add",
        gate=lifecycle.StartGate(TITLE, {"detail": "x"}),
    )
    assert (again.id, reused) == (run.id, True)
    assert Approval.objects.for_user(user.pk).filter(run=run).count() == 1


def test_autopilot_still_waits_for_the_user() -> None:
    _, gate = _gated(make_user(), mode="autopilot")
    assert gate.status == "pending"
    assert jobs() == []


def test_a_gate_must_continue_into_its_runs_first_step() -> None:
    user = make_user()
    with pytest.raises(ValueError):
        lifecycle.start_run(
            user.pk,
            "company_discovery",
            input={},
            dedupe_key=None,
            task="search.run",
            gate=lifecycle.StartGate(TITLE, {"detail": "x"}),
        )
    with pytest.raises(ValueError):
        lifecycle.start_run(
            user.pk,
            "resume_render",  # chat never starts one, so it has no gate to wait behind
            input={},
            dedupe_key=None,
            task="resume.render",
            gate=lifecycle.StartGate(TITLE, {"detail": "x"}),
        )
    assert Run.objects.for_user(user.pk).count() == 0


def test_a_yes_defers_the_first_step_as_an_ungated_start_would() -> None:
    user = make_user()
    run, gate = _gated(user)
    result = decide(user.pk, gate.id, {"approved": True}, "ui")
    assert result.ok
    assert refresh(run).status == "running"
    [job] = jobs(lock=f"run:{run.id}")
    assert (job["task_name"], job["status"], job["args"]) == (
        "company.add",
        "todo",
        {"run_id": str(run.id)},  # no approval_id: the first step never reads the gate
    )
    assert job["queueing_lock"] == f"approval:{gate.id}"
    decided = _events(run)[-1]
    assert (decided.kind, decided.message) == ("approval.decided", f"Approved: {TITLE}.")
    claimed = lifecycle.claim(run.id)  # the worker's first move succeeds
    assert claimed is not None and claimed.started_at is not None


def test_a_no_cancels_the_run_before_it_ever_ran() -> None:
    user = make_user()
    run, gate = _gated(user)
    assert decide(user.pk, gate.id, {"approved": False}, "ui").ok
    after = refresh(run)
    assert (after.status, after.started_at) == ("cancelled", None)
    assert after.finished_at is not None
    current = Approval.objects.for_user(user.pk).get(id=gate.id)
    assert (current.status, current.decision) == ("rejected", {"approved": False})
    assert event_kinds(run.id) == ["approval.requested", "run.cancelled"]  # R4: one, last
    cancelled = _events(run)[-1]
    assert (cancelled.message, cancelled.level) == (f"Declined: {TITLE}.", "info")
    assert jobs() == []
    assert approvals.decision_reply(current) == "Not started."
    assert decide(user.pk, gate.id, {"approved": True}, "ui").outcome == "already_decided"


def test_a_lapsed_go_ahead_cancels_the_run() -> None:
    user = make_user()
    run, gate = _gated(user)
    Approval.objects.for_user(user.pk).filter(id=gate.id).update(
        expires_at=Now() - timedelta(seconds=1)
    )
    assert approvals.expire_lapsed() == 1
    assert refresh(run).status == "cancelled"
    assert event_kinds(run.id) == ["approval.requested", "run.cancelled"]
    assert _events(run)[-1].message == f"No answer within a week, so this lapsed: {TITLE}."
    assert jobs() == []


def test_stop_and_the_reaper_treat_a_waiting_start_like_any_paused_run() -> None:
    user = make_user()
    run, gate = _gated(user)
    age_run(run.id, 3600)
    assert reaper.reap_runs() == reaper.ReapResult(0, 0)
    assert refresh(run).status == "awaiting_approval"  # its pending approval holds it
    assert lifecycle.cancel(user.pk, run.id)
    assert Approval.objects.for_user(user.pk).get(id=gate.id).status == "expired"


def test_the_chat_that_asked_gets_no_follow_up_for_the_gate() -> None:
    """The turn that started the run already shows its go-ahead card."""
    user = make_user()
    chat = make_conversation(user)
    run, gate = _gated(user, conversation_id=chat.id)
    [event] = _events(run)
    skipped = load(event.id)
    assert isinstance(skipped, Skip) and skipped.reason == "asked"
