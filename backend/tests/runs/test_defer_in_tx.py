"""Every defer joins its transaction, in every process (design §9.1; R1, R6, R7).

The worker swaps the app's connector for a `PsycopgConnector`, whose plain `defer()` would commit
the job on its own connection before the caller's transaction does, and keep it after a
rollback. Each producer below runs under that worker connector, against the real test database:
a rollback leaves no job; a commit leaves exactly one, which another session could not see
before the commit.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from django.db import transaction
from django.db.models.functions import Now

from apps.runs import approvals, events, lifecycle
from apps.runs.models import Approval, Run
from tests.factories import make_approval, make_conversation, make_run, make_user
from tests.runs import support
from tests.runs.support import jobs

pytestmark = pytest.mark.django_db(transaction=True)


class Rollback(Exception):
    pass


Act = Callable[[], Any]


def _start_run() -> tuple[Act, Callable[[Any], dict[str, Any]], Callable[[], Any]]:
    user = make_user()

    def act() -> Run:
        run, _ = lifecycle.start_run(
            user.pk, "search", input={"prompt": "x"}, dedupe_key="search", task=support.step
        )
        return run

    def expected(run: Run) -> dict[str, Any]:
        return {
            "task_name": "tests.step",
            "lock": f"run:{run.id}",
            "queueing_lock": f"run:{run.id}",
            "args": {"run_id": str(run.id)},
        }

    def unchanged() -> bool:
        return not Run.objects.for_user(user.pk).exists()

    return act, expected, unchanged


def _emit() -> tuple[Act, Callable[[Any], dict[str, Any]], Callable[[], Any]]:
    user = make_user()
    run = make_run(user, status="running", conversation=make_conversation(user))

    def act() -> Any:
        return events.emit(user.pk, "run.completed", "Done.", run_id=run.id)

    def expected(event: Any) -> dict[str, Any]:
        return {
            "task_name": "chat.follow_up",
            "queue": "interactive",
            "lock": None,
            "queueing_lock": f"follow-up:{event.id}",
            "args": {"event_id": event.id},
        }

    return act, expected, lambda: not support.event_kinds(run.id)


def _request_approval() -> tuple[Act, Callable[[Any], dict[str, Any]], Callable[[], Any]]:
    user = make_user()
    run = make_run(user, status="running", mode="autopilot")

    def act() -> Approval:
        return approvals.request_approval(
            run.id, "deep_eval_batch", "Deep-evaluate 3 roles", support.batch_payload()
        )

    def expected(approval: Approval) -> dict[str, Any]:
        return {
            "task_name": "search.evaluate",
            "queue": "background",
            "lock": f"run:{run.id}",
            "queueing_lock": f"approval:{approval.id}",
            "args": {"run_id": str(run.id), "approval_id": str(approval.id)},
        }

    return act, expected, lambda: not Approval.objects.for_user(user.pk).exists()


def _decide() -> tuple[Act, Callable[[Any], dict[str, Any]], Callable[[], Any]]:
    user = make_user()
    run = make_run(user, status="awaiting_approval")
    approval = make_approval(user, run=run, kind="shortlist_proposal")

    def act() -> Approval:
        result = approvals.decide(user.pk, approval.id, {"approved": True}, "ui")
        assert result.ok and result.approval is not None
        return result.approval

    def expected(decided: Approval) -> dict[str, Any]:
        return {
            "task_name": "search.shortlist",
            "lock": f"run:{run.id}",
            "queueing_lock": f"approval:{decided.id}",
            "args": {"run_id": str(run.id), "approval_id": str(decided.id)},
        }

    def unchanged() -> bool:
        current = Approval.objects.for_user(user.pk).get(id=approval.id)
        return (
            current.status == "pending"
            and Run.objects.for_user(user.pk).get(id=run.id).status == "awaiting_approval"
        )

    return act, expected, unchanged


def _expire_lapsed() -> tuple[Act, Callable[[Any], dict[str, Any]], Callable[[], Any]]:
    user = make_user()
    run = make_run(user, status="awaiting_approval")
    approval = make_approval(user, run=run, expires_at=Now() - timedelta(minutes=1))

    def act() -> Approval:
        assert approvals.expire_lapsed() == 1
        return Approval.objects.for_user(user.pk).get(id=approval.id)

    def expected(expired: Approval) -> dict[str, Any]:
        assert expired.status == "expired" and expired.decided_via == "expiry"
        return {
            "task_name": "search.evaluate",
            "lock": f"run:{run.id}",
            "queueing_lock": f"approval:{approval.id}",
            "args": {"run_id": str(run.id), "approval_id": str(approval.id)},
        }

    def unchanged() -> bool:
        return Approval.objects.for_user(user.pk).get(id=approval.id).status == "pending"

    return act, expected, unchanged


PRODUCERS = {
    "start_run": _start_run,
    "emit": _emit,
    "request_approval": _request_approval,
    "decide": _decide,
    "expire_lapsed": _expire_lapsed,
}


@pytest.mark.parametrize("producer", list(PRODUCERS))
def test_rollback_leaves_no_job_under_worker_connector(
    producer: str, under_worker_connector: None
) -> None:
    act, expected, unchanged = PRODUCERS[producer]()

    with pytest.raises(Rollback), transaction.atomic():
        result = act()
        [job] = jobs()  # this session sees it ...
        assert not support.job_seen_elsewhere(job["id"])  # ... nobody else does
        raise Rollback
    assert jobs() == []
    assert unchanged()

    with transaction.atomic():
        result = act()
        [job] = jobs()
        assert not support.job_seen_elsewhere(job["id"])  # invisible until the commit
    assert support.job_seen_elsewhere(job["id"])
    assert [job] == jobs()  # exactly one
    want = expected(result)
    assert {key: job[key] for key in want} == want
    assert job["status"] == "todo"
