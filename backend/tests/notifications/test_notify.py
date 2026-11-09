"""Phone notifications end to end (#59): a watch's strong match reaches the user's ntfy topic
exactly once, quiet hours hold it and later release it, a failed send is recorded and retried a
bounded number of times without ever failing the watch, a user with no topic sees no change,
and Settings' test button has the worker send one now.

ntfy is the fake of `conftest.py`; the watch's models are the matching tests' fakes. The watch
runs on a real in-process Worker (`run_deferred(["background"])`) and `notify.send` on another
(`run_deferred(["system"])`), as the lanes do in production. The sender's `db()` threads see
only committed rows, so these tests are transactional.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest
from django.db import IntegrityError, transaction
from django.db.models.functions import Now
from django.test import Client
from django.utils import timezone

from apps.accounts.models import User
from apps.core.clock import database_now
from apps.core.queue import defer_in_tx
from apps.jobs.models import Company
from apps.matching import watch
from apps.matching.models import JobMatch
from apps.notifications import outbox
from apps.notifications.models import Notification
from apps.notifications.send import Outcome, send_due
from apps.notifications.tasks import notify_send
from apps.runs import periodic
from apps.runs.models import Run
from apps.runs.step import TASKS
from tests.accounts.helpers import Api
from tests.factories import (
    make_company,
    make_job,
    make_match,
    make_notification,
    make_run,
    make_user,
)
from tests.matching.conftest import FakeLLM
from tests.notifications.conftest import TOPIC, FakeNtfy, phone_user
from tests.queue import deferred, run_deferred
from tests.runs.support import jobs

pytestmark = pytest.mark.django_db(transaction=True)

STRONG = "Software Engineer, Payments"  # fast 92, deep 92: strong
APPLY = "Backend Engineer, Platform"  # fast 78, deep 78: apply
MAYBE = "Software Engineer, Tools"  # fast 60, deep 60: maybe, not announced
ORIGIN = "http://localhost:3000"


@pytest.fixture
def company() -> Company:
    return make_company(name="Acme", last_fetched_at=timezone.now())


@pytest.fixture
def scores(llm: FakeLLM) -> FakeLLM:
    llm.title_include = ["software engineer", "backend engineer"]
    llm.fast.update({STRONG: 92, APPLY: 78, MAYBE: 60})
    return llm


def post(company: Company, *titles: str) -> None:
    """Postings first seen now, put up 40 minutes ago."""
    now = timezone.now()
    for title in titles:
        make_job(
            company=company,
            title=title,
            first_seen_at=now,
            posted_at=now - timedelta(minutes=40, seconds=20),
        )


def watch_now(user: User) -> Run:
    run, reused = watch.start_watch(user.pk)
    assert not reused
    run_deferred(["background"])
    return Run.objects.unscoped("tests").get(id=run.id)


def announced(run: Run) -> list[str]:
    assert isinstance(run.output, dict), run.output
    return run.output["announcedMatchIds"]


def notes(user: User) -> list[Notification]:
    return list(
        Notification.objects.for_user(user.pk)
        .select_related("match__job")
        .order_by("created_at", "id")
    )


def by_title(user: User) -> dict[str, Notification]:
    return {row.match.job.title: row for row in notes(user)}


def reload(row: Notification) -> Notification:
    return Notification.objects.for_user(row.user_id).get(id=row.id)


def send(user: User | None = None) -> Outcome:
    """What `notify.send` does: one user's due notifications, or (the sweep) everyone's."""
    return asyncio.run(send_due(user.pk if user is not None else None))


def time_passes(user: User) -> None:
    """Whatever the user's pending notifications were waiting for has come."""
    Notification.objects.for_user(user.pk).filter(status="pending").update(send_after=Now())


def strong_match(user: User, title: str = STRONG, score: int = 90) -> JobMatch:
    job = make_job(company=make_company(name="Acme"), title=title)
    return make_match(user, job=job, fast_score=score, deep_score=score)


# --- a watch, to a phone -------------------------------------------------------------------------


def test_a_watch_s_strong_match_reaches_the_phone_exactly_once(
    company: Company, scores: FakeLLM, ntfy_server: FakeNtfy
) -> None:
    user = phone_user()
    post(company, STRONG, APPLY, MAYBE)

    run = watch_now(user)

    # The watch committed one row per match it announced and the job that sends them.
    assert run.status == "completed" and len(announced(run)) == 2
    queued = by_title(user)
    assert {title: row.status for title, row in queued.items()} == {
        STRONG: "pending",
        APPLY: "pending",
    }
    [job] = deferred("notify.send")
    assert (job.queue, job.args, job.lock) == ("system", {"user_id": user.pk}, None)
    assert job.queueing_lock == f"notify:{user.pk}"
    assert ntfy_server.requests == []  # nothing is sent inside the watch

    run_deferred(["system"])

    assert ntfy_server.published == [
        {
            "topic": TOPIC,
            "title": f"Acme: {title}",
            "message": f"{verdict}. Posted 40 minutes ago.",
            "click": f"{ORIGIN}/matches/{queued[title].match_id}",
            "tags": ["briefcase"],
        }
        for title, verdict in ((STRONG, "Strong fit"), (APPLY, "Worth applying"))  # best first
    ]
    for row in notes(user):
        assert (row.status, row.attempts, row.error) == ("sent", 1, None)
        assert row.sent_at is not None and row.sent_at <= database_now()

    # The same job delivered again, and the minute's sweep: nothing is due, nothing goes out.
    defer_in_tx(notify_send, user_id=user.pk)
    defer_in_tx(notify_send, timestamp=1_794_000_060)
    run_deferred(["system"])
    assert len(ntfy_server.requests) == 2
    assert [job["status"] for job in jobs(task_name="notify.send")] == ["succeeded"] * 3


def test_a_second_watch_sends_only_what_is_new(
    company: Company, scores: FakeLLM, ntfy_server: FakeNtfy
) -> None:
    """R20 carried to the phone: the first watch's match is not sent again by the second."""
    user = phone_user()
    post(company, STRONG)
    watch_now(user)
    run_deferred(["system"])
    assert [message["title"] for message in ntfy_server.published] == [f"Acme: {STRONG}"]

    # Inside the overlap, so the second watch reads the first posting again.
    post(company, APPLY)
    second = watch_now(user)
    run_deferred(["system"])

    assert len(announced(second)) == 1
    assert [message["title"] for message in ntfy_server.published] == [
        f"Acme: {STRONG}",
        f"Acme: {APPLY}",
    ]
    assert {title: row.status for title, row in by_title(user).items()} == {
        STRONG: "sent",
        APPLY: "sent",
    }

    # A watch with nothing new queues nothing and defers nothing.
    third = watch_now(user)
    assert announced(third) == []
    assert deferred("notify.send") == [] and len(notes(user)) == 2


def test_a_user_with_no_topic_sees_no_change(
    company: Company, scores: FakeLLM, ntfy_server: FakeNtfy
) -> None:
    user = make_user(with_resume=True, settings={"deepEvalTopN": 15})
    post(company, STRONG)

    run = watch_now(user)

    assert run.status == "completed" and len(announced(run)) == 1
    assert run.conversation_id is not None  # announced in the chat, as before
    assert notes(user) == []
    assert jobs(task_name="notify.send") == []
    assert send().sent == 0 and ntfy_server.requests == []


def test_a_send_that_fails_is_recorded_and_never_fails_the_watch(
    company: Company, scores: FakeLLM, ntfy_server: FakeNtfy
) -> None:
    ntfy_server.refuse(500, "internal server error")
    user = phone_user()
    post(company, STRONG)

    run = watch_now(user)
    run_deferred(["system"])

    assert run.status == "completed" and run.error is None
    [row] = notes(user)
    assert (row.status, row.attempts) == ("pending", 1)
    assert row.error == "ntfy.sh answered 500: internal server error."
    assert len(ntfy_server.requests) == 1
    # The job itself succeeded: the failure is the row's, to be tried again by the sweep.
    assert [job["status"] for job in jobs(task_name="notify.send")] == ["succeeded"]


def test_a_notification_that_cannot_be_queued_does_not_fail_the_watch(
    company: Company, scores: FakeLLM, ntfy_server: FakeNtfy, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(user_id: str, match_ids: list[str]) -> int:
        Notification.objects.for_user(user_id).update(status="no such status")  # a database error
        return 0

    monkeypatch.setattr(outbox, "_queue", broken)
    user = phone_user()
    make_notification(user)  # so the broken update has a row to violate the CHECK with
    post(company, STRONG)

    run = watch_now(user)

    # The savepoint took the error; the watch's own transaction completed around it.
    assert run.status == "completed" and len(announced(run)) == 1
    assert run.conversation_id is not None
    assert [row.status for row in notes(user)] == ["pending"]


def test_a_stopped_watch_queues_nothing() -> None:
    user = phone_user()
    match = strong_match(user)
    run = make_run(user, kind="watch", status="cancelled")
    output = watch.WatchOutput(
        candidates=1,
        new_matches=1,
        scored=1,
        auto_archived=0,
        evaluated=1,
        strong=1,
        apply=0,
        not_reviewed=0,
        announced_match_ids=[str(match.id)],
        message="1 role looks worth applying to, from 1 new posting.",
    )
    assert watch.finish_watch(run.id, user.pk, output) is False
    assert notes(user) == [] and jobs(task_name="notify.send") == []


def test_the_sweep_runs_every_minute_on_the_system_lane() -> None:
    """What releases a held notification and retries a failed one: the same task, with no
    user, from the worker's schedule."""
    [entry] = [entry for entry in periodic.SCHEDULE if entry.task == "notify.send"]
    assert (entry.cron, entry.queueing_lock) == ("* * * * *", "notify:sweep")
    assert (notify_send.queue, notify_send.lock, notify_send.retry_strategy) == (
        "system",
        None,
        None,
    )
    assert TASKS["notify.send"].timeout_s < outbox.LEASE.total_seconds()


# --- once per match ------------------------------------------------------------------------------


def test_queueing_a_match_twice_adds_nothing() -> None:
    user, other = phone_user(), phone_user()
    mine, theirs = strong_match(user), strong_match(other)

    assert outbox.queue_matches(user.pk, [str(mine.id), str(theirs.id), "not-an-id"]) == 1
    assert outbox.queue_matches(user.pk, [str(mine.id)]) == 0
    assert outbox.queue_matches(user.pk, []) == 0

    [row] = notes(user)
    assert (row.match_id, row.channel, row.status, row.attempts) == (mine.id, "ntfy", "pending", 0)
    assert notes(other) == []  # another user's match id is not a way to notify about it

    # A second match while the first job still waits: a row, and no second job for the user.
    another = strong_match(user, title=APPLY)
    assert outbox.queue_matches(user.pk, [str(another.id), str(mine.id)]) == 1
    assert len(notes(user)) == 2
    [job] = deferred("notify.send")
    assert job.args == {"user_id": user.pk}


def test_the_database_keeps_one_row_per_match_and_channel() -> None:
    user = phone_user()
    row = make_notification(user)
    with pytest.raises(IntegrityError) as refused, transaction.atomic():
        make_notification(user, match=row.match)
    assert "notifications_once_per_match" in str(refused.value)


def test_two_jobs_never_send_the_same_notification() -> None:
    user = phone_user()
    row = make_notification(user)

    first = outbox.claim(user.pk)
    second = outbox.claim(user.pk)

    assert first is not None and [delivery.ids for delivery in first.deliveries] == [(row.id,)]
    assert second is None  # leased: not due again until the first job could have finished
    leased = reload(row)
    assert (leased.status, leased.attempts) == ("pending", 1)
    wait = leased.send_after - database_now()
    assert timedelta(minutes=9) < wait <= outbox.LEASE
    assert outbox.due_user_ids() == []


def test_a_worker_that_dies_mid_send_is_given_up_on_after_three_attempts(
    ntfy_server: FakeNtfy,
) -> None:
    user = phone_user()
    row = make_notification(user)
    for attempt in (1, 2, 3):
        assert outbox.claim(user.pk) is not None  # claimed, and the job never reports back
        assert reload(row).attempts == attempt
        time_passes(user)  # the lease runs out

    assert outbox.claim(user.pk) is None
    spent = reload(row)
    assert (spent.status, spent.attempts, spent.error) == ("failed", 3, outbox.GAVE_UP)
    assert send().sent == 0 and ntfy_server.requests == []


# --- failures ------------------------------------------------------------------------------------


def test_a_failed_send_is_retried_twice_and_then_given_up(ntfy_server: FakeNtfy) -> None:
    ntfy_server.refuse(503, "service unavailable")
    user = phone_user()
    row = make_notification(user)

    assert send(user).failed == 1
    first = reload(row)
    assert (first.status, first.attempts) == ("pending", 1)
    assert first.error == "ntfy.sh answered 503: service unavailable."
    assert timedelta(seconds=50) < first.send_after - database_now() <= timedelta(minutes=1)
    # Not due yet: the sweep leaves it alone.
    assert send().failed == 0 and len(ntfy_server.requests) == 1

    time_passes(user)
    assert send().failed == 1
    second = reload(row)
    assert (second.status, second.attempts) == ("pending", 2)
    assert timedelta(minutes=9) < second.send_after - database_now() <= timedelta(minutes=10)

    time_passes(user)
    assert send().failed == 1
    last = reload(row)
    assert (last.status, last.attempts, last.sent_at) == ("failed", 3, None)
    assert last.error == "ntfy.sh answered 503: service unavailable."

    # Failed is final, even once ntfy is back.
    ntfy_server.accept()
    Notification.objects.for_user(user.pk).update(send_after=Now())
    assert send() == Outcome(sent=0, failed=0)
    assert len(ntfy_server.requests) == 3 and ntfy_server.published == []


def test_a_retry_that_gets_through_is_sent_once(ntfy_server: FakeNtfy) -> None:
    ntfy_server.refuse(429, "limit reached: too many requests")
    user = phone_user()
    row = make_notification(user)
    assert send(user).failed == 1

    ntfy_server.accept()
    time_passes(user)
    assert send() == Outcome(sent=1, failed=0)
    assert send() == Outcome(sent=0, failed=0)

    sent = reload(row)
    assert (sent.status, sent.attempts, sent.error) == ("sent", 2, None)
    assert len(ntfy_server.published) == 1


def test_one_users_trouble_does_not_hold_up_another(
    ntfy_server: FakeNtfy, monkeypatch: pytest.MonkeyPatch
) -> None:
    unlucky, lucky = phone_user(), phone_user()
    make_notification(unlucky)
    make_notification(lucky)
    claim = outbox.claim

    def flaky(user_id: str) -> outbox.Batch | None:
        if user_id == unlucky.pk:
            raise RuntimeError("boom")
        return claim(user_id)

    monkeypatch.setattr(outbox, "claim", flaky)
    assert send() == Outcome(sent=1, failed=0)
    assert [row.status for row in notes(lucky)] == ["sent"]
    assert [row.status for row in notes(unlucky)] == ["pending"]


# --- quiet hours ---------------------------------------------------------------------------------


def test_quiet_hours_hold_a_notification_and_release_it_when_they_end(
    ntfy_server: FakeNtfy, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = phone_user(
        time_zone="America/Los_Angeles", quietHoursStart="22:00", quietHoursEnd="08:00"
    )
    row = make_notification(user)

    # 23:30 on 14 January in Los Angeles. (A year the database's own clock has not reached, so
    # "is it due yet?" below is the same answer whenever this test runs.)
    monkeypatch.setattr(outbox, "database_now", lambda: datetime(2099, 1, 15, 7, 30, tzinfo=UTC))
    assert send(user) == Outcome(sent=0, failed=0)

    held = reload(row)
    assert (held.status, held.attempts, held.error) == ("pending", 0, None)
    assert held.send_after == datetime(2099, 1, 15, 16, 0, tzinfo=UTC)  # 08:00 in Los Angeles
    assert outbox.due_user_ids() == []  # until then the sweep has nothing to do
    assert ntfy_server.requests == []

    # 08:00 has come: the sweep, not the watch's own job, delivers it.
    monkeypatch.setattr(outbox, "database_now", lambda: datetime(2099, 1, 15, 16, 0, tzinfo=UTC))
    time_passes(user)
    assert send() == Outcome(sent=1, failed=0)
    assert reload(row).status == "sent"
    assert [message["topic"] for message in ntfy_server.published] == [TOPIC]


def test_a_notification_that_comes_due_in_quiet_hours_waits_again(
    ntfy_server: FakeNtfy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry is as unwelcome at 3 a.m. as a first attempt."""
    ntfy_server.refuse(500)
    user = phone_user(time_zone="Asia/Tokyo", quietHoursStart="23:00", quietHoursEnd="07:00")
    row = make_notification(user)
    monkeypatch.setattr(outbox, "database_now", lambda: datetime(2099, 1, 15, 3, 0, tzinfo=UTC))
    assert send(user).failed == 1  # noon in Tokyo: tried, and refused

    monkeypatch.setattr(outbox, "database_now", lambda: datetime(2099, 1, 15, 18, 0, tzinfo=UTC))
    time_passes(user)
    assert send() == Outcome(sent=0, failed=0)  # 03:00 in Tokyo: held, not retried
    held = reload(row)
    assert (held.attempts, held.send_after) == (1, datetime(2099, 1, 15, 22, 0, tzinfo=UTC))
    assert len(ntfy_server.requests) == 1


# --- what is sent, and what is not ---------------------------------------------------------------


def test_more_than_five_at_once_are_five_and_one_line_for_the_rest(
    ntfy_server: FakeNtfy,
) -> None:
    user = phone_user()
    scores = [71, 95, 83, 79, 90, 88, 75]
    for score in scores:
        make_notification(user, match=strong_match(user, title=f"Engineer {score}", score=score))

    assert send(user) == Outcome(sent=6, failed=0)

    assert [message["title"] for message in ntfy_server.published] == [
        "Acme: Engineer 95",
        "Acme: Engineer 90",
        "Acme: Engineer 88",
        "Acme: Engineer 83",
        "Acme: Engineer 79",
        "And 2 more new matches",
    ]
    assert ntfy_server.published[-1]["click"] == f"{ORIGIN}/matches"
    # Every match is settled, the two the summary stood for included.
    assert [(row.status, row.attempts) for row in notes(user)] == [("sent", 1)] * 7
    assert send() == Outcome(sent=0, failed=0)


def test_a_summary_that_fails_leaves_its_matches_to_be_sent_later(ntfy_server: FakeNtfy) -> None:
    user = phone_user()
    for score in range(80, 87):
        make_notification(user, match=strong_match(user, title=f"Engineer {score}", score=score))

    accepting = FakeNtfy()

    def only_matches(request: httpx2.Request) -> httpx2.Response:
        if b"more new matches" in request.content:
            return httpx2.Response(500, json={"error": "busy"})
        return accepting.handler(request)

    ntfy_server.answer = only_matches
    assert send(user) == Outcome(sent=5, failed=1)
    assert sorted(row.status for row in notes(user)) == ["pending"] * 2 + ["sent"] * 5

    # Later the two that were left go out as themselves: nothing is lost, nothing repeats.
    ntfy_server.accept()
    time_passes(user)
    assert send() == Outcome(sent=2, failed=0)
    assert [message["title"] for message in ntfy_server.published] == [
        "Acme: Engineer 81",
        "Acme: Engineer 80",
    ]
    assert len(accepting.published) == 5


def test_a_notification_is_dropped_when_it_no_longer_makes_sense(ntfy_server: FakeNtfy) -> None:
    # The topic was removed while the notification waited.
    gone = phone_user()
    waiting = make_notification(gone)
    User.objects.filter(pk=gone.pk).update(settings={"ntfyTopic": ""})
    # The user shortlisted the match in the app before the notification was due.
    busy = phone_user()
    seen = make_notification(busy, match=strong_match(busy))
    JobMatch.objects.for_user(busy.pk).filter(id=seen.match_id).update(stage="shortlisted")
    fresh = make_notification(busy, match=strong_match(busy, title=APPLY))

    assert send() == Outcome(sent=1, failed=0)

    assert (reload(waiting).status, reload(waiting).error) == ("skipped", outbox.SKIPPED_NO_TOPIC)
    assert (reload(seen).status, reload(seen).error) == ("skipped", outbox.SKIPPED_HANDLED)
    assert reload(fresh).status == "sent"
    assert [message["title"] for message in ntfy_server.published] == [f"Acme: {APPLY}"]
    assert reload(waiting).attempts == 0 and reload(seen).attempts == 0


def test_a_match_that_is_deleted_takes_its_notification_with_it() -> None:
    user = phone_user()
    row = make_notification(user)
    JobMatch.objects.for_user(user.pk).filter(id=row.match_id).delete()
    assert notes(user) == []


# --- Settings' test button -----------------------------------------------------------------------


def signed_in(user: User) -> Api:
    client = Client()
    client.force_login(user)
    return Api(client)


def answers(api: Api) -> list[dict[str, Any]]:
    """The `notification.test` events the user's live feed carries, oldest first."""
    events = api.get("/api/live").json()["events"]
    return [event for event in events if event["kind"] == "notification.test"]


def test_the_test_button_queues_a_job_and_the_worker_sends_one_notification(
    ntfy_server: FakeNtfy,
) -> None:
    # In the middle of the user's quiet hours, whatever the hour: a test they asked for is sent.
    user = phone_user(quietHoursStart="00:00", quietHoursEnd="23:59")
    api = signed_in(user)

    response = api.post("/api/notifications/test")

    # The request only queues the job: no request waits on ntfy.
    assert response.status_code == 202, response.content
    body = response.json()
    assert (body["ok"], body["message"]) == (True, "Sending a test notification.")
    [job] = jobs(task_name="notify.test")
    assert (job["id"], job["queue"], job["lock"], job["args"]) == (
        body["jobId"],
        "system",
        None,
        {"user_id": user.pk},
    )
    assert ntfy_server.requests == [] and answers(api) == []

    run_deferred(["system"])

    assert ntfy_server.published == [
        {
            "topic": TOPIC,
            "title": "Foray can reach your phone",
            "message": "New matches will arrive here, outside your quiet hours.",
            "click": f"{ORIGIN}/settings",
            "tags": ["white_check_mark"],
        }
    ]
    [answer] = answers(api)
    assert (answer["message"], answer["level"], answer["runId"]) == (
        "Sent. It should reach your phone in a few seconds.",
        "info",
        None,
    )
    assert answer["data"] == {"jobId": body["jobId"], "sent": True}
    assert notes(user) == []  # a test is not a match: nothing is queued or recorded


def test_the_test_button_says_what_went_wrong(ntfy_server: FakeNtfy) -> None:
    api = signed_in(phone_user())
    ntfy_server.refuse(429, "limit reached: too many requests")

    job_id = api.post("/api/notifications/test").json()["jobId"]
    run_deferred(["system"])

    [answer] = answers(api)
    assert answer["message"] == (
        "ntfy.sh is limiting how many notifications it accepts. Try again in a while."
    )
    assert (answer["level"], answer["data"]) == ("warn", {"jobId": job_id, "sent": False})
    assert len(ntfy_server.requests) == 1  # one attempt: a test is not retried
    assert [job["status"] for job in jobs(task_name="notify.test")] == ["succeeded"]


def test_the_test_button_needs_a_topic_and_a_session(ntfy_server: FakeNtfy) -> None:
    no_topic = signed_in(make_user()).post("/api/notifications/test")
    assert no_topic.status_code == 400
    assert no_topic.json() == {"ok": False, "message": "Save a topic first, then send a test."}

    signed_out = Api(Client()).post("/api/notifications/test")
    assert signed_out.status_code == 401
    assert signed_out.json() == {"ok": False, "message": "Not signed in."}
    assert jobs(task_name="notify.test") == []

    # The topic removed between the click and the job: the answer says so, and nothing is sent.
    user = phone_user()
    api = signed_in(user)
    api.post("/api/notifications/test")
    User.objects.filter(pk=user.pk).update(settings={})
    run_deferred(["system"])
    [answer] = answers(api)
    assert (answer["message"], answer["data"]["sent"]) == (
        "Save a topic first, then send a test.",
        False,
    )
    assert ntfy_server.requests == []


def test_the_test_button_is_throttled(ntfy_server: FakeNtfy) -> None:
    user, other = phone_user(), phone_user()
    api = signed_in(user)
    for _ in range(5):
        assert api.post("/api/notifications/test").status_code == 202
    sixth = api.post("/api/notifications/test")
    assert sixth.status_code == 429
    assert sixth.json() == {
        "ok": False,
        "message": "Too many attempts. Try again in a few minutes.",
    }
    assert len(jobs(task_name="notify.test")) == 5
    # Per user: someone else can still test theirs.
    assert signed_in(other).post("/api/notifications/test").status_code == 202
