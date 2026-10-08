"""R8: per-user rows are reachable only through `for_user(uid)` or an explicit
`unscoped(reason)` (design §2.1).

`Model.objects` refuses every unscoped query for every `UserOwned` model, while Django's own
machinery (related managers, cascades, `refresh_from_db`) keeps working through the plain
default manager.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from django.apps import apps as django_apps
from django.db import models

from apps.accounts.models import User
from apps.chat.models import Conversation, Message
from apps.core.scoping import ScopedManager, UnscopedQueryError, UserOwned
from apps.jobs.models import Company
from apps.llm.models import LlmCall
from apps.matching.models import JobMatch
from apps.resumes.models import Document, Resume
from apps.runs.models import Approval, Event, Run
from tests import factories as f

USER_OWNED: list[type[models.Model]] = sorted(
    (m for m in django_apps.get_models() if issubclass(m, UserOwned)), key=lambda m: m.__name__
)


def test_every_per_user_table_is_user_owned() -> None:
    assert {m.__name__ for m in USER_OWNED} == {
        "Approval",
        "Conversation",
        "Document",
        "Event",
        "JobMatch",
        "LlmCall",
        "Message",
        "Resume",
        "Run",
    }
    for owned in USER_OWNED:
        model: Any = owned
        assert isinstance(model.objects, ScopedManager)
        # Django's internals use the plain manager, so they never hit the refusal.
        assert model._default_manager.name == "_plain"
        assert model._base_manager.name == "_plain"
        assert not isinstance(model._default_manager, ScopedManager)
        assert model._meta.get_field("user").is_relation


@pytest.mark.parametrize("model", USER_OWNED, ids=lambda m: m.__name__)
def test_scoped_manager_refuses_unscoped(model: type[models.Model]) -> None:
    objects: Any = model.objects
    attempts = {
        "filter": lambda: objects.filter(pk=1),
        "all": lambda: objects.all(),
        "get": lambda: objects.get(pk=1),
        "first": lambda: objects.first(),
        "count": lambda: objects.count(),
        "exists": lambda: objects.exists(),
        "create": lambda: objects.create(),
        "update": lambda: objects.update(),
        "values": lambda: objects.values("pk"),
        "order_by": lambda: objects.order_by("pk"),
        "iterate": lambda: list(objects.get_queryset()),
    }
    for name, attempt in attempts.items():
        with pytest.raises(UnscopedQueryError, match=r"needs \.for_user\(user_id\)"):
            attempt()
            pytest.fail(f"{model.__name__}.objects.{name} did not refuse")
    with pytest.raises(UnscopedQueryError):
        objects.for_user("")
    with pytest.raises(UnscopedQueryError):
        objects.for_user(None)
    with pytest.raises(ValueError, match="reason"):
        objects.unscoped("  ")


@dataclass
class Graph:
    user: User
    match: JobMatch
    document: Document
    resume: Resume
    tailored: Resume
    run: Run
    approval: Approval
    event: Event
    conversation: Conversation
    message: Message
    llm_call: LlmCall
    company: Company

    def rows(self) -> dict[type[models.Model], models.Model]:
        return {
            JobMatch: self.match,
            Document: self.document,
            Resume: self.resume,
            Run: self.run,
            Approval: self.approval,
            Event: self.event,
            Conversation: self.conversation,
            Message: self.message,
            LlmCall: self.llm_call,
        }


def make_graph() -> Graph:
    user = f.make_user()
    company = f.make_company(added_by=user)
    match = f.make_match(user, job=f.make_job(company=company), fast_score=70)
    tailored = f.make_resume(user, kind="tailored", match=match)
    match.tailored_resume = tailored
    match.save(update_fields=["tailored_resume"])
    conversation = f.make_conversation(user)
    run = f.make_run(user, status="awaiting_approval", conversation=conversation)
    approval = f.make_approval(user, run=run)
    return Graph(
        user=user,
        match=match,
        document=f.make_document(user),
        resume=f.make_resume(user),
        tailored=tailored,
        run=run,
        approval=approval,
        event=f.make_event(user, kind="approval.requested", run=run, approval=approval),
        conversation=conversation,
        message=f.make_message(conversation),
        llm_call=f.make_llm_call(user, run=run, conversation=conversation),
        company=company,
    )


@pytest.mark.django_db
def test_for_user_sees_only_that_users_rows() -> None:
    a, b = make_graph(), make_graph()
    for model, row in a.rows().items():
        objects: Any = model.objects
        mine = set(objects.for_user(a.user.pk).values_list("pk", flat=True))
        theirs = set(objects.for_user(b.user.pk).values_list("pk", flat=True))
        assert row.pk in mine and b.rows()[model].pk in theirs, model
        assert mine.isdisjoint(theirs), model
        assert objects.unscoped("tests: both users").filter(pk__in=mine | theirs).count() == len(
            mine | theirs
        )
    resumes = Resume.objects.for_user(a.user.pk)
    assert set(resumes.values_list("pk", flat=True)) == {a.resume.pk, a.tailored.pk}


@pytest.mark.django_db
def test_related_managers_work() -> None:
    g = make_graph()
    user: Any = g.user
    assert list(user.job_matches.all()) == [g.match]
    assert list(user.runs.all()) == [g.run]
    assert set(user.resumes.all()) == {g.resume, g.tailored}
    assert list(g.run.approvals.all()) == [g.approval]  # pyright: ignore[reportAttributeAccessIssue]
    assert list(g.run.events.all()) == [g.event]  # pyright: ignore[reportAttributeAccessIssue]
    assert list(g.conversation.messages.all()) == [g.message]  # pyright: ignore[reportAttributeAccessIssue]
    assert list(g.match.resumes.all()) == [g.tailored]  # pyright: ignore[reportAttributeAccessIssue]
    # Forward relations and refresh_from_db go through the plain base manager.
    assert g.match.tailored_resume == g.tailored
    assert g.event.approval == g.approval and g.event.run == g.run
    fresh = JobMatch.objects.for_user(g.user.pk).select_related("job").get(pk=g.match.pk)
    assert fresh.job == g.match.job
    g.match.refresh_from_db()


@pytest.mark.django_db
def test_create_for_and_bulk_create_for_set_the_user() -> None:
    user = f.make_user()
    conversation = Conversation.objects.create_for(user.pk, id=uuid.uuid4())
    assert conversation.user_id == user.pk
    rows = Event.objects.bulk_create_for(
        user.pk,
        [
            Event(kind="worker.ping", message="Worker is alive."),
            Event(kind="run.started", message="Go"),
        ],
    )
    assert [row.user_id for row in rows] == [user.pk, user.pk]
    assert Event.objects.for_user(user.pk).count() == 2
    with pytest.raises(UnscopedQueryError):
        Event.objects.create_for("", kind="x", message="y")
    with pytest.raises(UnscopedQueryError):
        Event.objects.bulk_create_for("", [])


@pytest.mark.django_db
def test_user_delete_cascades() -> None:
    a, b = make_graph(), make_graph()
    ledger_row = a.llm_call.pk
    a.user.delete()

    for model, row in a.rows().items():
        objects: Any = model.objects
        remaining = objects.unscoped("tests: after a delete").filter(pk=row.pk)
        if model is LlmCall:
            # The ledger outlives its user: the row stays, unattributed.
            kept = remaining.get()
            assert (kept.user_id, kept.run_id, kept.conversation_id) == (None, None, None)
        else:
            assert not remaining.exists(), model
    assert LlmCall.objects.unscoped("tests").filter(pk=ledger_row).exists()

    # The shared mirror stays; it only forgets who added the company.
    company = Company.objects.get(pk=a.company.pk)
    assert company.added_by_id is None
    assert company.jobs.count() == 1  # pyright: ignore[reportAttributeAccessIssue]

    # The other user's graph is untouched.
    for model, row in b.rows().items():
        objects = model.objects
        assert objects.for_user(b.user.pk).filter(pk=row.pk).exists(), model  # pyright: ignore[reportAttributeAccessIssue]


@pytest.mark.django_db
def test_deleting_a_conversation_or_run_keeps_what_should_survive() -> None:
    g = make_graph()
    g.conversation.delete()
    run = Run.objects.for_user(g.user.pk).get(pk=g.run.pk)
    assert run.conversation_id is None  # FK SET NULL: a pending follow-up skips
    assert not Message.objects.for_user(g.user.pk).exists()
    g.run.delete()
    assert not Approval.objects.for_user(g.user.pk).exists()
    assert not Event.objects.for_user(g.user.pk).filter(run__isnull=False).exists()
    call = LlmCall.objects.for_user(g.user.pk).get(pk=g.llm_call.pk)
    assert (call.run_id, call.conversation_id) == (None, None)
