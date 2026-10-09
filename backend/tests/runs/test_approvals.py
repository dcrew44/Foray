"""Approvals and continuations (design §9.6, R6; ported from `approvals.test.ts`, 18 cases).

The continuation is a job now, not a pg-boss send: "enqueued" means a `todo` job of the
approval's `next_step` holding `run:<id>` with queueing lock `approval:<id>`. The reopen tests
went with the reopen path: a failed defer rolls the whole decision back
(`test_decide_rollback_leaves_pending`).
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any, cast

import pytest
from django.db import connections
from django.utils import timezone

from apps.runs import approvals, lifecycle, step
from apps.runs.approvals import (
    APPROVAL_TTL,
    approved_item_ids,
    decide,
    describe_decision,
    is_approved,
    list_pending,
    request_approval,
    resolve_decision,
)
from apps.runs.catalogue import StoredDecision
from apps.runs.models import Approval, Event, Run
from tests.factories import make_approval, make_run, make_user
from tests.runs import support
from tests.runs.support import batch_payload, event_kinds, jobs, refresh


def _paused_at(user: Any, mode: str = "approve_each", payload: dict[str, Any] | None = None):
    run = make_run(user, status="running", mode=mode)
    payload = payload or batch_payload()
    approval = request_approval(
        run.id,
        "deep_eval_batch",
        "Deep-evaluate 3 roles (about $0.15)",
        payload,
        stats={"phase": "awaiting_evaluation", "toEvaluate": 3},
        data={"count": 3},
    )
    return run, approval, payload


def _continuations(approval: Approval) -> list[dict[str, Any]]:
    return jobs(lock=f"run:{approval.run_id}")


def _events(run: Run) -> list[Event]:
    return list(Event.objects.unscoped("tests").filter(run_id=run.id).order_by("id"))


def _approval(approval: Approval) -> Approval:
    return Approval.objects.unscoped("tests").get(id=approval.id)


# --- request_approval ----------------------------------------------------------------------------


@pytest.mark.django_db
def test_approve_each_pauses_the_run_and_announces_it() -> None:
    user = make_user()
    run, approval, _ = _paused_at(user)

    assert approval.status == "pending"
    assert approval.decision is None
    assert approval.next_step == "search.evaluate"
    assert approval.expires_at is not None
    assert approval.expires_at - timezone.now() > APPROVAL_TTL - timedelta(minutes=1)

    run = refresh(run)
    assert run.status == "awaiting_approval"
    assert run.stats == {"phase": "awaiting_evaluation", "toEvaluate": 3}

    [event] = _events(run)
    assert (event.kind, event.approval_id, event.level) == (
        "approval.requested",
        approval.id,
        "info",
    )
    assert event.message == "Waiting for your go-ahead: Deep-evaluate 3 roles (about $0.15)."
    assert event.data == {"kind": "deep_eval_batch", "count": 3}
    assert _continuations(approval) == []  # nothing runs until the user answers


@pytest.mark.django_db
def test_autopilot_approves_every_item_and_defers_the_continuation() -> None:
    user = make_user()
    run, approval, payload = _paused_at(user, mode="autopilot")

    assert approval.status == "auto_approved"
    assert approval.decided_via == "autopilot"
    assert approval.decision == {
        "approved": True,
        "itemIds": [item["matchId"] for item in payload["items"]],
    }
    assert approval.expires_at is None
    assert is_approved(approval)

    [job] = _continuations(approval)
    assert (job["task_name"], job["queueing_lock"], job["args"]) == (
        "search.evaluate",
        f"approval:{approval.id}",
        {"run_id": str(run.id), "approval_id": str(approval.id)},
    )
    run = refresh(run)
    assert run.status == "running"  # never paused on autopilot
    assert run.stats == {"phase": "awaiting_evaluation", "toEvaluate": 3}
    assert event_kinds(run.id) == ["approval.auto"]
    assert _events(run)[0].message == "Autopilot approved: Deep-evaluate 3 roles (about $0.15)."


@pytest.mark.django_db
def test_custom_messages_and_a_payload_without_items() -> None:
    user = make_user()
    run = make_run(user, status="running", mode="autopilot")
    approval = request_approval(
        run.id,
        "shortlist_proposal",
        "Shortlist 2 roles",
        {"items": []},
        messages={"auto": "Autopilot: shortlisting 2 roles that look worth applying to."},
    )
    assert approval.next_step == "search.shortlist"
    assert approval.decision == {"approved": True, "itemIds": []}
    assert _events(run)[0].message == (
        "Autopilot: shortlisting 2 roles that look worth applying to."
    )
    other = make_run(user, status="running")
    request_approval(
        other.id,
        "shortlist_proposal",
        "Shortlist 1 role",
        {"items": []},
        messages={"requested": "1 role looks worth applying to. Shortlist it?"},
    )
    assert _events(other)[0].message == "1 role looks worth applying to. Shortlist it?"


@pytest.mark.django_db
def test_expires_the_approval_on_the_spot_if_the_run_was_cancelled_first() -> None:
    user = make_user()
    run = make_run(user, status="cancelled")
    approval = request_approval(run.id, "deep_eval_batch", "Too late", batch_payload())
    assert approval.status == "expired"
    assert _approval(approval).status == "expired"
    assert refresh(run).status == "cancelled"
    assert _events(run) == []
    assert jobs() == []


# --- decide --------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_approves_a_subset_ignoring_unknown_ids_and_defers_once() -> None:
    user = make_user()
    run, approval, payload = _paused_at(user)
    first, _, third = (item["matchId"] for item in payload["items"])

    result = decide(
        user.pk, approval.id, {"approved": True, "itemIds": [third, "not-an-item", first]}, "ui"
    )

    assert result.ok and result.approval is not None
    decided = result.approval
    assert decided.status == "approved" and decided.decided_via == "ui"
    assert decided.decision == {"approved": True, "itemIds": [first, third]}  # payload order
    assert approved_item_ids(decided) == [first, third]

    [job] = _continuations(approval)
    assert job["args"] == {"run_id": str(run.id), "approval_id": str(approval.id)}
    assert refresh(run).status == "running"  # decide moves the run, with its job (§9.6)

    decided_event = _events(run)[-1]
    assert decided_event.kind == "approval.decided"
    assert decided_event.message == "Approved deep evaluation of 2 of 3 roles."
    assert decided_event.approval_id == approval.id
    assert decided_event.data == {
        "kind": "deep_eval_batch",
        "via": "ui",
        "approved": True,
        "itemIds": [first, third],
    }


@pytest.mark.django_db
def test_approves_everything_when_no_subset_is_given() -> None:
    user = make_user()
    _, approval, payload = _paused_at(user)
    result = decide(user.pk, approval.id, {"approved": True, "itemIds": None}, "chat")
    assert result.approval is not None
    assert result.approval.decision == {
        "approved": True,
        "itemIds": [item["matchId"] for item in payload["items"]],
    }
    assert result.approval.decided_via == "chat"


@pytest.mark.django_db
def test_is_a_no_op_the_second_time() -> None:
    user = make_user()
    _, approval, _ = _paused_at(user)
    first = decide(user.pk, approval.id, {"approved": True}, "ui")
    second = decide(user.pk, approval.id, {"approved": False}, "chat")
    assert first.ok
    assert second.outcome == "already_decided"
    assert len(_continuations(approval)) == 1
    assert _approval(approval).status == "approved"


@pytest.mark.django_db(transaction=True)
def test_concurrent_decides_one_wins() -> None:
    """R6: the UI and chat (and a double click) answer at the same moment; one decision wins
    and exactly one continuation is deferred."""
    user = make_user()
    _, approval, _ = _paused_at(user)
    barrier = threading.Barrier(3)
    answers = [("ui", True), ("chat", False), ("chat", True)]

    def answer(via: str, approved: bool) -> str:
        try:
            barrier.wait(10)
            return decide(user.pk, approval.id, {"approved": approved}, via).outcome
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=3) as pool:
        outcomes = list(pool.map(lambda a: answer(*a), answers))

    assert sorted(outcomes) == ["already_decided", "already_decided", "decided"]
    assert len(_continuations(approval)) == 1
    assert event_kinds(approval.run_id).count("approval.decided") == 1


@pytest.mark.django_db
def test_decide_rollback_leaves_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed defer rolls the decision back: no "reopened" path (§9.6)."""
    user = make_user()
    run, approval, _ = _paused_at(user)

    def broken(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("the queue is down")

    monkeypatch.setattr(step, "defer_in_tx", broken)
    with pytest.raises(RuntimeError):
        decide(user.pk, approval.id, {"approved": True}, "ui")

    current = _approval(approval)
    assert (current.status, current.decision, current.decided_via, current.decided_at) == (
        "pending",
        None,
        None,
        None,
    )
    assert refresh(run).status == "awaiting_approval"
    assert event_kinds(run.id) == ["approval.requested"]
    assert jobs() == []
    monkeypatch.undo()
    assert decide(user.pk, approval.id, {"approved": True}, "ui").ok  # answering again works


@pytest.mark.django_db
def test_an_empty_subset_is_a_rejection_that_still_resumes_the_run() -> None:
    user = make_user()
    run, approval, _ = _paused_at(user)
    result = decide(user.pk, approval.id, {"approved": True, "itemIds": ["nothing-here"]}, "ui")
    assert result.ok and result.approval is not None
    assert result.approval.status == "rejected"
    assert result.approval.decision == {"approved": False, "itemIds": []}
    assert not is_approved(result.approval)
    assert len(_continuations(approval)) == 1  # the continuation is what finishes the run
    assert _events(run)[-1].message == "Skipped the deep evaluation."


@pytest.mark.django_db
def test_a_lapsed_approval_cannot_be_decided_and_resumes_the_run_as_a_no() -> None:
    user = make_user()
    run, approval, _ = _paused_at(user)
    Approval.objects.for_user(user.pk).filter(id=approval.id).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    assert approval.id not in [a.id for a in list_pending(user.pk)]

    result = decide(user.pk, approval.id, {"approved": True}, "ui")
    assert result.outcome == "expired"
    current = _approval(approval)
    assert (current.status, current.decision, current.decided_via) == ("expired", None, "expiry")
    # The expiry ran inline: the run resumes and its continuation finishes it with a "no".
    assert refresh(run).status == "running"
    [job] = _continuations(approval)
    assert job["queueing_lock"] == f"approval:{approval.id}"
    expired = _events(run)[-1]
    assert (expired.kind, expired.level, expired.message) == (
        "approval.expired",
        "warn",
        "No answer within a week, so this lapsed: Deep-evaluate 3 roles (about $0.15).",
    )


@pytest.mark.django_db
def test_the_sweep_resumes_a_lapsed_approval_exactly_once() -> None:
    user = make_user()
    run, approval, _ = _paused_at(user)
    fresh_run, fresh, _ = _paused_at(user)
    Approval.objects.for_user(user.pk).filter(id=approval.id).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    assert approvals.expire_lapsed() == 1
    assert approvals.expire_lapsed() == 0
    assert len(_continuations(approval)) == 1
    assert event_kinds(run.id).count("approval.expired") == 1
    assert _approval(fresh).status == "pending"
    assert refresh(fresh_run).status == "awaiting_approval"


@pytest.mark.django_db
def test_the_sweep_lapses_quietly_when_the_run_already_ended() -> None:
    user = make_user()
    run = make_run(user, status="failed")
    approval = make_approval(user, run=run, expires_at=timezone.now() - timedelta(seconds=1))
    assert approvals.expire_lapsed() == 1
    assert _approval(approval).status == "expired"
    assert event_kinds(run.id) == []  # the terminal event stays the run's last
    assert jobs() == []


@pytest.mark.django_db
def test_reports_unknown_and_malformed_ids_as_not_found() -> None:
    user = make_user()
    assert decide(user.pk, uuid.uuid4(), {"approved": True}, "ui").outcome == "not_found"
    assert decide(user.pk, "approval-7", {"approved": True}, "chat").outcome == "not_found"
    with pytest.raises(ValueError):
        decide(user.pk, uuid.uuid4(), {"approved": True}, "autopilot-ish")


@pytest.mark.django_db
def test_will_not_let_another_user_decide() -> None:
    user, other = make_user(), make_user()
    _, approval, _ = _paused_at(user)
    assert decide(other.pk, approval.id, {"approved": True}, "ui").outcome == "not_found"
    assert jobs() == []


@pytest.mark.django_db
def test_an_approval_on_a_finished_run_reads_as_expired() -> None:
    user = make_user()
    run, approval, _ = _paused_at(user)
    lifecycle.fail(run.id, "It broke.")
    assert decide(user.pk, approval.id, {"approved": True}, "ui").outcome == "expired"
    assert _approval(approval).status == "expired"
    assert jobs() == []


# --- cancelling a paused run ---------------------------------------------------------------------


@pytest.mark.django_db
def test_cancelling_a_paused_run_leaves_nothing_to_answer() -> None:
    user = make_user()
    run, approval, _ = _paused_at(user)
    assert lifecycle.cancel(user.pk, run.id)
    assert _approval(approval).status == "expired"
    assert approval.id not in [a.id for a in list_pending(user.pk)]
    assert decide(user.pk, approval.id, {"approved": True}, "ui").outcome == "expired"
    assert jobs() == []
    assert event_kinds(run.id)[-1] == "run.cancelled"


@pytest.mark.django_db
def test_lists_only_approvals_that_can_still_be_answered() -> None:
    user = make_user()
    _, open_approval, _ = _paused_at(user)
    _, answered, _ = _paused_at(user)
    decide(user.pk, answered.id, {"approved": False}, "ui")
    pending = [a.id for a in list_pending(user.pk)]
    assert open_approval.id in pending
    assert answered.id not in pending
    assert list_pending(make_user().pk) == []


# --- pure helpers --------------------------------------------------------------------------------

PAYLOAD = {"items": [{"matchId": "a"}, {"matchId": "b"}, {"id": "c"}]}


def _stored(**kwargs: Any) -> dict[str, Any]:
    return StoredDecision(**kwargs).dump()


def test_resolve_keeps_payload_order_and_drops_unknown_ids() -> None:
    resolved = resolve_decision(PAYLOAD, {"approved": True, "itemIds": ["c", "zzz", "a", "a"]})
    assert resolved.dump() == {"approved": True, "itemIds": ["a", "c"]}


def test_resolve_all_without_a_subset_and_nothing_on_a_rejection() -> None:
    assert resolve_decision(PAYLOAD, {"approved": True}).dump() == _stored(
        approved=True, item_ids=["a", "b", "c"]
    )
    assert resolve_decision(PAYLOAD, {"approved": False, "itemIds": ["a"]}).dump() == {
        "approved": False,
        "itemIds": [],
    }


def test_resolve_counts_an_empty_approved_subset_as_a_rejection() -> None:
    assert resolve_decision(PAYLOAD, {"approved": True, "itemIds": []}).dump() == {
        "approved": False,
        "itemIds": [],
    }


def test_resolve_passes_a_plain_yes_or_no_for_payloads_without_items() -> None:
    assert resolve_decision({"company": "Stripe"}, {"approved": True}).dump() == {"approved": True}
    assert resolve_decision(None, {"approved": False}).dump() == {"approved": False}


@pytest.mark.django_db
def test_describe_decision_and_the_card_reply() -> None:
    user = make_user()
    run = make_run(user, status="awaiting_approval")
    batch = make_approval(user, run=run, payload=batch_payload(3))
    ids = approvals.payload_item_ids(batch.payload)
    assert describe_decision(batch, StoredDecision(approved=True, item_ids=ids)) == (
        "Approved deep evaluation of 3 roles."
    )
    assert describe_decision(batch, StoredDecision(approved=True, item_ids=ids[:1])) == (
        "Approved deep evaluation of 1 of 3 roles."
    )
    shortlist = make_approval(
        user,
        run=make_run(user, status="awaiting_approval"),
        kind="shortlist_proposal",
        payload={"items": [{"matchId": "x"}]},
    )
    assert describe_decision(shortlist, StoredDecision(approved=True)) == (
        "Approved shortlisting 1 role."
    )
    assert describe_decision(shortlist, StoredDecision(approved=False, item_ids=[])) == (
        "Left the shortlist as it was."
    )
    batch.decision = cast(Any, {"approved": True, "itemIds": ids[:1]})
    assert approvals.decision_reply(batch) == "Approved. Deep-evaluating 1 role in the background."
    batch.decision = cast(Any, {"approved": False, "itemIds": []})
    assert approvals.decision_reply(batch) == "Skipped the deep evaluation."
    shortlist.decision = cast(Any, {"approved": True, "itemIds": ["x"]})
    assert approvals.decision_reply(shortlist) == "Approved. Shortlisting 1 role."
    shortlist.decision = cast(Any, {"approved": False, "itemIds": []})
    assert approvals.decision_reply(shortlist) == "Left the shortlist as it was."


def test_payload_item_ids_take_match_id_then_id_strings_only() -> None:
    payload = {
        "items": [
            {"matchId": "a"},
            {"matchId": None, "id": "b"},
            {"matchId": 7, "id": "c"},  # a non-string matchId is not replaced by id
            {"id": "a"},  # duplicate
            "x",
            None,
        ]
    }
    assert approvals.payload_item_ids(payload) == ["a", "b"]
    assert approvals.payload_item_ids({"items": "nope"}) == []
    assert support.batch_payload(2)["items"][1]["company"] == "Beta"
