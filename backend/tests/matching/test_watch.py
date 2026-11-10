"""Watch end to end: the postings each scheduled refresh brings in, scored, reviewed within the
daily cap and announced in the "New matches" chat (#5).

The models are fakes (`FakeLLM`, by posting title) below the real budget, limiter and ledger.
`run_deferred(["background"])` runs the watch on a real in-process Worker and leaves the chat
follow-up (interactive lane) queued, so a test can see that it was deferred without calling a
chat model.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command
from django.db import connections
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.schemas import SAMPLE_PREFERENCES
from apps.chat import follow_up
from apps.chat.follow_up import KnownRole
from apps.chat.models import Conversation
from apps.core.queue import defer_in_tx
from apps.jobs.models import Company, Job
from apps.llm.models import LlmCall
from apps.matching import tasks as matching_tasks
from apps.matching import watch
from apps.matching.models import JobMatch
from apps.pages import build
from apps.runs import lifecycle
from apps.runs.models import Approval, Event, Run
from tests.factories import (
    make_company,
    make_contact,
    make_job,
    make_match,
    make_run,
    make_user,
)
from tests.matching.conftest import FakeLLM
from tests.queue import deferred, run_deferred

pytestmark = pytest.mark.django_db(transaction=True)

STRONG = "Software Engineer, Payments"  # fast 92, deep 92: strong
APPLY = "Backend Engineer, Platform"  # fast 78, deep 78: apply
MAYBE = "Software Engineer, Tools"  # fast 60, deep 60: maybe
LOW = "Software Engineer, Legacy"  # fast 40: archived by the fast score
SALES = "Account Executive"  # not a title the criteria want


@pytest.fixture
def company() -> Company:
    return make_company(name="Acme", last_fetched_at=timezone.now())


@pytest.fixture
def scores(llm: FakeLLM) -> FakeLLM:
    llm.title_include = ["software engineer", "backend engineer"]
    llm.fast.update({STRONG: 92, APPLY: 78, MAYBE: 60, LOW: 40})
    return llm


def post(company: Company, *titles: str, seen: timedelta = timedelta(0)) -> list[Job]:
    """Postings first seen `seen` ago."""
    at = timezone.now() - seen
    return [make_job(company=company, title=title, first_seen_at=at) for title in titles]


def watcher(**settings: Any) -> User:
    return make_user(with_resume=True, settings={"deepEvalTopN": 15, **settings})


def watch_now(user: User) -> Run:
    run, reused = watch.start_watch(user.pk)
    assert not reused
    run_deferred(["background"])
    return Run.objects.unscoped("tests").get(id=run.id)


def output(run: Run) -> dict[str, Any]:
    assert isinstance(run.output, dict), run.output
    return run.output


def matches(user: User) -> dict[str, JobMatch]:
    rows = JobMatch.objects.for_user(user.pk).select_related("job")
    return {row.job.title: row for row in rows}


def titles(user: User, match_ids: list[str]) -> list[str]:
    rows = JobMatch.objects.for_user(user.pk).filter(id__in=match_ids).select_related("job")
    by_id = {str(row.id): row.job.title for row in rows}
    return [by_id[match_id] for match_id in match_ids]


def follow_ups() -> list[Any]:
    return deferred("chat.follow_up")


# --- one watch -----------------------------------------------------------------------------------


def test_a_watch_reviews_new_postings_and_announces_them_in_a_new_matches_chat(
    company: Company, scores: FakeLLM
) -> None:
    user = watcher()
    post(company, STRONG, APPLY, MAYBE, LOW, SALES)

    run = watch_now(user)

    assert run.status == "completed"
    assert run.mode == "autopilot"
    result = output(run)
    assert titles(user, result["announcedMatchIds"]) == [STRONG, APPLY]
    assert {key: result[key] for key in result if key != "announcedMatchIds"} == {
        "candidates": 4,
        "newMatches": 4,
        "scored": 4,
        "autoArchived": 1,
        "evaluated": 3,
        "strong": 1,
        "apply": 1,
        "notReviewed": 0,
        "message": "2 roles look worth applying to, from 4 new postings.",
    }
    assert run.stats["announced"] == 2
    # Announced matches stay in `found` for the user; watch never shortlists or asks.
    found = matches(user)
    assert {title: row.stage for title, row in found.items()} == {
        STRONG: "found",
        APPLY: "found",
        MAYBE: "found",
        LOW: "archived",
    }
    assert not Approval.objects.unscoped("tests").filter(run_id=run.id).exists()
    # Linked to a new "New matches" chat, and the usual follow-up is on its way there.
    conversation = Conversation.objects.for_user(user.pk).get()
    assert conversation.title == "New matches"
    assert run.conversation_id == conversation.id
    assert len(follow_ups()) == 1
    completed = Event.objects.unscoped("tests").get(run_id=run.id, kind="run.completed")
    assert completed.level == "info"
    # Progress stays out of the feeds: only the outcome is above debug.
    levels = Event.objects.unscoped("tests").filter(run_id=run.id).values_list("level", flat=True)
    assert sorted(set(levels)) == ["debug", "info"]
    assert [call.output for call in scores.calls].count("derive") == 1


def test_a_quiet_watch_posts_nothing_and_stays_out_of_activity(
    company: Company, scores: FakeLLM
) -> None:
    user = watcher()
    post(company, MAYBE, LOW)

    run = watch_now(user)

    assert run.status == "completed"
    assert run.conversation_id is None
    assert output(run)["announcedMatchIds"] == []
    assert output(run)["message"] == "Nothing strong among 2 new postings."
    assert run.stats["announced"] == 0
    assert not Conversation.objects.for_user(user.pk).exists()
    assert follow_ups() == []
    completed = Event.objects.unscoped("tests").get(run_id=run.id, kind="run.completed")
    assert completed.level == "debug"
    assert [row.run.id for row in build.activity_page(user).runs] == []


def test_nothing_new_means_nothing_to_score(company: Company, scores: FakeLLM) -> None:
    user = watcher()
    run = watch_now(user)
    assert output(run)["message"] == "No new postings match your criteria."
    assert output(run)["candidates"] == 0
    assert [call.output for call in scores.calls] == ["derive"]


# --- across watches ------------------------------------------------------------------------------


def test_a_second_watch_reads_only_what_arrived_since_and_never_announces_twice(
    company: Company, scores: FakeLLM
) -> None:
    """R20: a match is announced by the one watch that reviewed it."""
    user = watcher()
    post(company, STRONG)
    first = watch_now(user)
    assert titles(user, output(first)["announcedMatchIds"]) == [STRONG]

    # Inside the overlap, so the prefilter sees the first posting again; it is not new to watch.
    post(company, APPLY)
    second = watch_now(user)

    assert titles(user, output(second)["announcedMatchIds"]) == [APPLY]
    assert output(second)["newMatches"] == 1
    fast = [call.title for call in scores.of("fast")]
    deep = [call.title for call in scores.of("deep")]
    assert fast == [STRONG, APPLY]
    assert deep == [STRONG, APPLY]
    # The criteria were derived once and reused while the preferences held.
    assert len(scores.of("derive")) == 1
    # Both announcements went to the same chat.
    assert Conversation.objects.for_user(user.pk).count() == 1
    assert second.conversation_id == first.conversation_id


def test_the_window_starts_at_the_last_watch_and_reaches_back_a_week_at_most(
    company: Company, scores: FakeLLM
) -> None:
    user = watcher()
    post(company, MAYBE, seen=timedelta(days=8))  # older than any window
    post(company, APPLY, seen=timedelta(days=2))
    first = watch_now(user)
    assert titles(user, output(first)["announcedMatchIds"]) == [APPLY]

    # First seen before the last watch's end, less the ten-minute overlap: already looked at.
    post(company, STRONG, seen=timedelta(hours=1))
    second = watch_now(user)
    assert output(second)["candidates"] == 0
    assert set(matches(user)) == {APPLY}


def test_a_search_s_matches_are_the_search_s(company: Company, scores: FakeLLM) -> None:
    user = watcher()
    [job] = post(company, STRONG)
    make_match(user, job=job, fast_score=92)

    run = watch_now(user)

    assert output(run)["newMatches"] == 0
    assert output(run)["announcedMatchIds"] == []
    assert scores.of("deep") == []


def test_the_daily_cap_holds_reviews_back_until_it_frees(company: Company, scores: FakeLLM) -> None:
    user = watcher(watchDailyReviews=1)
    post(company, STRONG, APPLY)

    first = watch_now(user)
    assert titles(user, output(first)["announcedMatchIds"]) == [STRONG]
    assert output(first)["notReviewed"] == 1
    assert output(first)["message"] == (
        "1 role looks worth applying to, from 2 new postings. 1 more scored well; the daily "
        "review cap holds them for now."
    )

    # Still inside the 24 hours: nothing more is reviewed.
    second = watch_now(user)
    assert output(second)["evaluated"] == 0
    assert output(second)["notReviewed"] == 1

    # A day later the cap has room, and the backlog gets its review.
    LlmCall.objects.unscoped("tests").filter(purpose="evaluate").update(
        created_at=timezone.now() - timedelta(hours=25)
    )
    third = watch_now(user)
    assert titles(user, output(third)["announcedMatchIds"]) == [APPLY]
    assert output(third)["notReviewed"] == 0


def test_no_deep_reviews_when_the_limit_is_zero(company: Company, scores: FakeLLM) -> None:
    user = watcher(deepEvalTopN=0)
    post(company, STRONG)
    run = watch_now(user)
    assert output(run)["evaluated"] == 0
    assert output(run)["notReviewed"] == 1
    assert scores.of("deep") == []


def test_a_posting_that_failed_to_score_is_picked_up_by_the_next_watch(
    company: Company, scores: FakeLLM
) -> None:
    user = watcher()
    post(company, STRONG)
    scores.fail_fast.add(STRONG)
    first = watch_now(user)
    assert first.status == "completed"
    assert output(first)["scored"] == 0

    scores.fail_fast.clear()
    second = watch_now(user)
    assert output(second)["newMatches"] == 0  # nothing new arrived
    assert titles(user, output(second)["announcedMatchIds"]) == [STRONG]


def test_a_deleted_chat_is_made_again(company: Company, scores: FakeLLM) -> None:
    user = watcher()
    post(company, STRONG)
    first = watch_now(user)
    Conversation.objects.for_user(user.pk).filter(id=first.conversation_id).delete()

    post(company, APPLY)
    second = watch_now(user)

    assert second.conversation_id is not None
    assert second.conversation_id != first.conversation_id
    assert Conversation.objects.for_user(user.pk).get().title == "New matches"


def test_stop_mid_scoring_leaves_no_chat(company: Company, scores: FakeLLM) -> None:
    user = watcher()
    post(company, STRONG, APPLY)
    run, _ = watch.start_watch(user.pk)

    def stop() -> None:  # on the model's thread, as the user would, mid-scoring
        lifecycle.cancel(user.pk, run.id)
        connections.close_all()

    scores.after_fast = stop
    run_deferred(["background"])

    run = Run.objects.unscoped("tests").get(id=run.id)
    assert run.status == "cancelled"
    assert scores.of("deep") == []
    assert not Conversation.objects.for_user(user.pk).exists()
    assert follow_ups() == []


def test_finishing_a_stopped_watch_writes_nothing(company: Company) -> None:
    user = watcher()
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
        announced_match_ids=["x"],
        message="1 role looks worth applying to, from 1 new posting.",
    )
    assert watch.finish_watch(run.id, user.pk, output) is False
    assert not Conversation.objects.for_user(user.pk).exists()


# --- the fan-out ---------------------------------------------------------------------------------


def test_the_fan_out_starts_a_watch_for_each_user_who_is_watching(
    company: Company, scores: FakeLLM
) -> None:
    eligible = watcher()
    make_user(with_resume=True, settings={"watchEnabled": False})
    make_user(preferences=SAMPLE_PREFERENCES.dump())  # no resume
    incomplete = make_user(with_resume=True)
    User.objects.filter(pk=incomplete.pk).update(preferences={})
    searching = watcher()
    make_run(searching, kind="search", status="running")
    parked = watcher()  # a search waiting at a gate does not hold the watch back
    make_run(parked, kind="search", status="awaiting_approval")
    watching = watcher()
    make_run(watching, kind="watch", status="running", dedupe_key="watch")

    assert set(watch.eligible_user_ids()) == {eligible.pk, parked.pk, watching.pk}
    assert watch.start_watches() == 2  # the running watch is left alone
    started = Run.objects.unscoped("tests").filter(kind="watch", status="queued")
    assert set(started.values_list("user_id", flat=True)) == {eligible.pk, parked.pk}


def test_the_fan_out_starts_nothing_once_the_month_s_budget_is_spent(
    settings: Any, scores: FakeLLM
) -> None:
    watcher()
    settings.ENV = settings.ENV.model_copy(update={"llm_monthly_budget_usd": Decimal(0)})
    assert watch.eligible_user_ids() == []


def test_the_fan_out_task_runs_on_the_system_lane(company: Company, scores: FakeLLM) -> None:
    user = watcher()
    defer_in_tx(matching_tasks.watch_fan_out, queueing_lock="watch:fan-out")
    run_deferred(["system"])
    [run] = Run.objects.for_user(user.pk).filter(kind="watch")
    assert run.status == "queued"
    assert [job.task_name for job in deferred()] == ["watch.run"]


def test_home_does_not_call_a_watch_searching() -> None:
    user = watcher()
    make_run(user, kind="watch", status="running")
    assert build.home_page(user).waiting.running == 0


def test_watch_now_starts_one_watch_or_the_fan_out(settings: Any, company: Company) -> None:
    settings.DEBUG = True
    user = watcher()
    out = StringIO()
    call_command("watch_now", email=user.email, days=30, stdout=out)
    [run] = Run.objects.for_user(user.pk).filter(kind="watch")
    assert out.getvalue().strip() == f"run: {run.id}"
    assert stored_input(run)["since"] is not None
    again = StringIO()
    call_command("watch_now", email=user.email, stdout=again)
    assert again.getvalue().strip() == f"already running: {run.id}"
    fan_out = StringIO()
    call_command("watch_now", fan_out=True, stdout=fan_out)
    assert fan_out.getvalue().startswith("job: ")
    assert [job.queueing_lock for job in deferred("watch.fan_out")] == ["watch:fan-out"]


def stored_input(run: Run) -> dict[str, Any]:
    assert isinstance(run.input, dict), run.input
    return run.input


def test_a_watch_starts_preparing_an_application_for_each_match_it_keeps(
    company: Company, scores: FakeLLM
) -> None:
    """#60: what is worth a review is worth applying to, and its application is started beside
    the reviews. What the fast score archived gets none."""
    from apps.applications.models import Application

    user = watcher()
    post(company, STRONG, APPLY, MAYBE, LOW)
    watch_now(user)

    prepared = {
        row.match.job.title: row.status
        for row in Application.objects.for_user(user.pk).select_related("match__job")
    }
    assert prepared == {STRONG: "preparing", APPLY: "preparing", MAYBE: "preparing"}
    assert len(deferred("application.prepare")) == 3

    # A second watch over the same matches starts nothing twice.
    post(company, "Software Engineer, Search")
    scores.fast["Software Engineer, Search"] = 90
    watch_now(user)
    assert Application.objects.for_user(user.pk).count() == 4
    assert len(deferred("application.prepare")) == 4


# --- where the user knows someone (#64) ----------------------------------------------------------


def test_a_watch_says_when_a_match_is_at_a_company_where_the_user_knows_someone(
    company: Company, scores: FakeLLM
) -> None:
    user = watcher()
    elsewhere = make_company(name="Globex", last_fetched_at=timezone.now())
    post(company, STRONG)
    post(elsewhere, APPLY)
    make_contact(user, name="Priya Rao", company=company, position="Staff Engineer")
    make_contact(user, name="Marcus Bell", company_name="Acme, Inc.")  # linked by name alone
    make_contact(user, name="Someone Else", company_name="Hooli")

    run = watch_now(user)

    result = output(run)
    assert titles(user, result["announcedMatchIds"]) == [STRONG, APPLY]
    assert result["message"] == (
        "2 roles look worth applying to, from 2 new postings. You know someone at one of them."
    )
    completed = Event.objects.unscoped("tests").get(run_id=run.id, kind="run.completed")
    assert completed.message == result["message"]
    # What the chat follow-up will be told: the role, and who the user knows there.
    [known] = follow_up.known_roles(user.pk, run)
    assert (known.title, known.company, known.count) == (STRONG, "Acme", 2)
    assert sorted(known.people) == [("Marcus Bell", ""), ("Priya Rao", "Staff Engineer")]
    assert len(follow_ups()) == 1
    # Everything else about the watch is what it was.
    assert {key: result[key] for key in result if key not in ("announcedMatchIds", "message")} == {
        "candidates": 2,
        "newMatches": 2,
        "scored": 2,
        "autoArchived": 0,
        "evaluated": 2,
        "strong": 1,
        "apply": 1,
        "notReviewed": 0,
    }


def test_a_watch_where_the_user_knows_nobody_is_announced_as_before(
    company: Company, scores: FakeLLM
) -> None:
    """The other half of #64: contacts at other companies, or another user's contacts at this
    one, change nothing about what a watch says."""
    user, other = watcher(), make_user()
    post(company, STRONG, APPLY)
    make_contact(user, name="Someone Else", company_name="Hooli")
    make_contact(other, name="Not Mine", company=company)

    run = watch_now(user)

    assert output(run)["message"] == "2 roles look worth applying to, from 2 new postings."
    assert follow_up.known_roles(user.pk, run) == []
    assert set(output(run)) == {
        "candidates",
        "newMatches",
        "scored",
        "autoArchived",
        "evaluated",
        "strong",
        "apply",
        "notReviewed",
        "announcedMatchIds",
        "message",
    }


def test_the_watch_message_counts_the_matches_where_the_user_knows_someone() -> None:
    assert watch.watch_message(9, 2, 0) == "2 roles look worth applying to, from 9 new postings."
    assert watch.watch_message(9, 2, 0, 0) == watch.watch_message(9, 2, 0)
    assert watch.watch_message(9, 1, 0, 1) == (
        "1 role looks worth applying to, from 9 new postings. You know someone there."
    )
    assert watch.watch_message(9, 3, 0, 1) == (
        "3 roles look worth applying to, from 9 new postings. You know someone at one of them."
    )
    assert watch.watch_message(9, 3, 2, 2) == (
        "3 roles look worth applying to, from 9 new postings. You know someone at 2 of them. "
        "2 more scored well; the daily review cap holds them for now."
    )
    # Nothing announced: nothing to say about contacts.
    assert watch.watch_message(9, 0, 0, 0) == "Nothing strong among 9 new postings."


def test_a_contact_lookup_that_fails_never_fails_the_watch(
    company: Company, scores: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(user_id: str, match_ids: list[str]) -> int:
        raise RuntimeError("the contacts table is on fire")

    monkeypatch.setattr(watch, "count_matches_with_contacts", broken)
    user = watcher()
    post(company, STRONG)
    make_contact(user, company=company)
    run = watch_now(user)
    assert run.status == "completed"
    assert output(run)["message"] == "1 role looks worth applying to, from 1 new posting."


def test_known_roles_are_only_a_watch_s(company: Company) -> None:
    user = make_user()
    make_contact(user, name="Priya Rao", company=company)
    match = make_match(user, job=make_job(company=company), fast_score=90, deep_score=90)
    announced = {"announcedMatchIds": [str(match.id), "not-a-uuid"]}
    search = make_run(user, kind="search", status="completed", output=announced)
    assert follow_up.known_roles(user.pk, search) == []
    watched = make_run(user, kind="watch", status="completed", output=announced)
    assert follow_up.known_roles(user.pk, watched) == [
        KnownRole(title="Software Engineer", company="Acme", people=(("Priya Rao", ""),), count=1)
    ]
    assert follow_up.known_roles(user.pk, make_run(user, kind="watch", output=None)) == []


def test_the_fan_out_links_contacts_to_companies_registered_since_they_were_imported(
    scores: FakeLLM,
) -> None:
    """#63: a contact imported before their company's board was registered is linked before
    the watches start, for every user and whether or not that user is watching."""
    from apps.contacts.models import Contact

    watching, idle = watcher(), make_user()
    for user in (watching, idle):
        make_contact(user, name="Lena Fischer", company_name="Globex Analytics")
    globex = make_company(name="Globex Analytics", last_fetched_at=timezone.now())

    assert watch.start_watches() == 1
    for user in (watching, idle):
        assert Contact.objects.for_user(user.pk).get().company_id == globex.id


def test_the_fan_out_starts_its_watches_even_when_contacts_cannot_be_linked(
    scores: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> int:
        raise RuntimeError("no linking today")

    monkeypatch.setattr(watch, "link_contacts", broken)
    watcher()
    assert watch.start_watches() == 1
