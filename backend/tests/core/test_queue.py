"""`defer_in_tx` joins the caller's transaction, even under the worker connector (design §9.1).

Under the worker connector a plain `task.defer()` commits on its own pooled connection before
the caller's transaction does. `defer_in_tx` must not: its job is invisible to other sessions
until commit, and gone after a rollback. `run_deferred()` then runs the committed job with a real
Worker (§15.4: `run_worker_async(wait=False)` returns once the queue is drained).
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from django.db import connections, transaction
from procrastinate.exceptions import AlreadyEnqueued

from apps.core.queue import app, defer_in_tx
from tests.queue import deferred, run_deferred, worker_connector

pytestmark = pytest.mark.django_db(transaction=True)

CALLS: list[dict[str, Any]] = []


@app.task(name="tests.noop", queue="system")
def noop(**kwargs: Any) -> None:
    CALLS.append(kwargs)


class Rollback(Exception):
    pass


def _seen_by_another_session(job_id: int) -> bool:
    """Look for the job from a different thread, so on a different pooled connection."""

    def look() -> bool:
        try:
            with connections["default"].cursor() as cursor:
                cursor.execute("SELECT 1 FROM procrastinate_jobs WHERE id = %s", [job_id])
                return cursor.fetchone() is not None
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(look).result()


@pytest.fixture
def under_worker_connector() -> Iterator[None]:
    with app.replace_connector(worker_connector()):
        yield


@pytest.fixture(autouse=True)
def _reset_calls() -> None:
    CALLS.clear()


def test_rollback_leaves_no_job_under_worker_connector(under_worker_connector: None) -> None:
    job_ids: list[int] = []
    with pytest.raises(Rollback), transaction.atomic():
        job_ids.append(defer_in_tx(noop, lock="run:test", value=1))
        assert [j.id for j in deferred("tests.noop")] == job_ids  # this session sees it
        assert not _seen_by_another_session(job_ids[0])  # nobody else does yet
        raise Rollback
    assert deferred("tests.noop") == []
    assert not _seen_by_another_session(job_ids[0])


def test_commit_leaves_exactly_one_job(under_worker_connector: None) -> None:
    with transaction.atomic():
        job_id = defer_in_tx(noop, lock="run:test", queueing_lock="noop:test", value=2)
        assert not _seen_by_another_session(job_id)
    assert _seen_by_another_session(job_id)
    [job] = deferred("tests.noop")
    assert (job.id, job.queue, job.args) == (job_id, "system", {"value": 2})
    assert (job.lock, job.queueing_lock) == ("run:test", "noop:test")


def test_queueing_lock_conflict_keeps_the_transaction_usable(
    under_worker_connector: None,
) -> None:
    with transaction.atomic():
        defer_in_tx(noop, queueing_lock="noop:once", value=1)
        with pytest.raises(AlreadyEnqueued):
            defer_in_tx(noop, queueing_lock="noop:once", value=2)
        # The savepoint rolled back only the failed insert; the transaction still works.
        assert len(deferred("tests.noop")) == 1
    assert len(deferred("tests.noop")) == 1


def test_run_deferred_runs_committed_jobs() -> None:
    with transaction.atomic():
        defer_in_tx(noop, lock="run:a", value=3)
        defer_in_tx(noop, lock="run:a", value=4)
    run_deferred()
    assert CALLS == [{"value": 3}, {"value": 4}]
    assert deferred("tests.noop") == []
    with connections["default"].cursor() as cursor:
        cursor.execute(
            "SELECT status, count(*) FROM procrastinate_jobs WHERE task_name = 'tests.noop' "
            "GROUP BY status"
        )
        assert cursor.fetchall() == [("succeeded", 2)]


def test_a_delay_is_measured_from_the_database_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """`schedule_in` counts from the database's clock, the one `fetch_job` compares
    `scheduled_at` with, however far Python's clock has drifted (deviations/step-8E.md §7)."""
    from datetime import timedelta

    from procrastinate import utils as procrastinate_utils

    real = procrastinate_utils.utcnow
    monkeypatch.setattr(procrastinate_utils, "utcnow", lambda: real() - timedelta(minutes=5))
    job_id = defer_in_tx(noop, schedule_in={"seconds": 4})
    with connections["default"].cursor() as cursor:
        cursor.execute(
            "SELECT scheduled_at - clock_timestamp() FROM procrastinate_jobs WHERE id = %s",
            [job_id],
        )
        [(wait,)] = cursor.fetchall()
    assert timedelta(seconds=3) < wait <= timedelta(seconds=4)
