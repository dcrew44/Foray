"""Follow-ups for runs started from chat, against the real database and queue (design §7.7;
R7, R17; the port of `follow-up-db.test.ts`).

- `events.emit()` defers `chat.follow_up(event_id)` with the queueing lock `follow-up:<id>`, only
  for a run that reports back to a chat, only for `FOLLOW_UP_KINDS`;
- a follow-up posts its notice and reply at most once per event (R7);
- it never interleaves with a streaming user turn: it re-defers while the marker is set, and a
  generation the user overtook is thrown away and re-deferred (R7);
- it skips what is no longer news, writes nothing for an empty reply, offers read-only tools
  only, stops at 4 steps, and is attributed to its run;
- `FOLLOW_UP_KINDS` is the same set in Python and TypeScript (R17).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from django.utils import timezone
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from apps.chat import follow_up
from apps.chat.follow_up import run_follow_up
from apps.chat.models import Conversation, Message
from apps.chat.tools import FOLLOW_UP_TOOLS
from apps.core.db import db
from apps.core.queue import app
from apps.llm import fake
from apps.llm.models import LlmCall
from apps.runs import events
from apps.runs.catalogue import FOLLOW_UP_EVENT, FOLLOW_UP_KINDS
from apps.runs.models import Event, Run
from tests.factories import (
    make_approval,
    make_conversation,
    make_job,
    make_match,
    make_message,
    make_run,
    make_user,
)
from tests.fakes import fake_models
from tests.queue import deferred, run_deferred
from tests.runs.support import sql

pytestmark = pytest.mark.django_db(transaction=True)

REPO_ROOT = Path(__file__).resolve().parents[3]
TS_KINDS = REPO_ROOT / "src" / "lib" / "chat" / "follow-up-kinds.ts"


# --- helpers -------------------------------------------------------------------------------------


type Script = Callable[[list[ModelMessage], AgentInfo], AsyncIterator[Any]]


def install(monkeypatch: pytest.MonkeyPatch, stream: Script) -> None:
    fake_models(monkeypatch, {"chat": FunctionModel(stream_function=stream)})


def says(text: str, seen: list[AgentInfo] | None = None) -> Script:
    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        if seen is not None:
            seen.append(info)
        yield text

    return stream


def chat_run(**fields: Any) -> tuple[Any, Conversation, Run]:
    user = make_user()
    conversation = make_conversation(user)
    run = make_run(
        user, status=fields.pop("status", "completed"), conversation=conversation, **fields
    )
    return user, conversation, run


def emitted(user_id: str, kind: str, message: str, run: Run, **kw: Any) -> Event:
    return events.emit(user_id, kind, message, run_id=run.id, **kw)


def messages_of(conversation: Conversation) -> list[Message]:
    return list(
        Message.objects.for_user(conversation.user_id)
        .filter(conversation_id=conversation.id)
        .order_by("seq")
    )


def followup_events(user_id: str) -> list[Event]:
    return list(Event.objects.for_user(user_id).filter(kind=FOLLOW_UP_EVENT).order_by("id"))


def run_now(event_id: int) -> str:
    return asyncio.run(run_follow_up(event_id))


# --- R17 -----------------------------------------------------------------------------------------


def test_follow_up_kinds_match_frontend() -> None:
    """`src/lib/chat/follow-up-kinds.ts` holds the same set as the catalogue, and the same
    event name (R17)."""
    source = TS_KINDS.read_text()
    block = re.search(r"FOLLOW_UP_KINDS[^=]*=\s*new Set\(\[(.*?)\]\)", source, re.DOTALL)
    assert block is not None
    kinds = set(re.findall(r'"([^"]+)"', block.group(1)))
    assert kinds == set(FOLLOW_UP_KINDS) == {"approval.requested", "run.completed", "run.failed"}
    name = re.search(r'FOLLOW_UP_EVENT\s*=\s*"([^"]+)"', source)
    assert name is not None and name.group(1) == FOLLOW_UP_EVENT == "chat.followup"


# --- the trigger ---------------------------------------------------------------------------------


def test_emit_defers_one_follow_up_per_event_for_chat_runs_only() -> None:
    user, _, run = chat_run(status="running")
    event = emitted(user.pk, "run.completed", "Done.", run)
    [job] = deferred("chat.follow_up")
    assert job.args == {"event_id": event.id}
    assert (job.queue, job.lock, job.queueing_lock) == (
        "interactive",
        None,
        f"follow-up:{event.id}",
    )

    # Not a follow-up kind, a run with no chat, an event with no run: nothing more.
    emitted(user.pk, "run.started", "Started.", run)
    plain = make_run(user, status="running")
    emitted(user.pk, "run.completed", "Done.", plain)
    events.emit(user.pk, FOLLOW_UP_EVENT, "Foray followed up in chat.", data={"x": 1})
    assert [j.id for j in deferred("chat.follow_up")] == [job.id]
    assert app.tasks["chat.follow_up"].queue == "interactive"


# --- R7: once per event ---------------------------------------------------------------------------


def test_follow_up_once(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, says("Your search found three roles worth a look."))
    user, conversation, run = chat_run(
        status="running", input={"prompt": "Find backend roles", "hints": None}
    )
    make_message(conversation, text="Find backend roles")
    before = Conversation.objects.for_user(user.pk).get(id=conversation.id).updated_at
    event = emitted(user.pk, "run.completed", "Found 3 roles.", run)

    run_deferred(["interactive"])

    notice, reply = messages_of(conversation)[1:]
    assert notice.role == "user" and reply.role == "assistant"
    assert notice.run_update_event_id == event.id
    assert notice.metadata == {
        "kind": "run_update",
        "runId": str(run.id),
        "eventId": event.id,
        "label": "Search finished",
    }
    assert notice.parts[0]["text"].startswith(f"[Automatic update about run {run.id}")
    assert reply.metadata == {"kind": "follow_up", "runId": str(run.id), "eventId": event.id}
    assert [p["type"] for p in reply.parts] == ["step-start", "text"]
    assert reply.parts[1]["text"] == "Your search found three roles worth a look."
    after = Conversation.objects.for_user(user.pk).get(id=conversation.id).updated_at
    assert after > before

    [announced] = followup_events(user.pk)
    assert announced.run_id is None and announced.level == "debug"
    assert announced.message == "Foray followed up in chat."
    assert announced.data == {
        "conversationId": str(conversation.id),
        "runId": str(run.id),
        "messageId": reply.message_id,
        "label": "Search finished",
    }

    # A redelivery finds the notice and posts nothing more.
    assert run_now(event.id) == "skipped (duplicate)"
    assert len(messages_of(conversation)) == 3
    assert (followup_events(user.pk)[-1].data or {})["reason"] == "duplicate"
    assert deferred("chat.follow_up") == []  # the skip triggers nothing


def test_a_concurrent_post_for_the_same_event_loses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two generations for one event (a shutdown retry racing a re-defer, say): the unique
    notice per event lets exactly one post."""
    install(monkeypatch, says("Done."))
    user, conversation, run = chat_run()
    event = emitted(user.pk, "run.completed", "Done.", run)
    loaded = follow_up.load(event.id)
    assert isinstance(loaded, follow_up.Loaded)

    def post(reply_id: str, read_at: Any) -> str:
        return follow_up.post_follow_up(
            follow_up.Post(
                user_id=user.pk,
                conversation_id=conversation.id,
                run_id=run.id,
                event_id=event.id,
                history_read_at=read_at,
                notice_id=f"notice-{reply_id}",
                notice_text="update",
                label="Search finished",
                reply_id=reply_id,
                reply_parts=[{"type": "text", "text": "hi", "state": "done"}],
            )
        )

    assert post("r1", loaded.history_read_at) == "posted"
    # Read before the first post: the conversation has moved on since.
    assert post("r2", loaded.history_read_at) == "retry"
    # Read after it: the unique notice per event refuses the second.
    fresh = Conversation.objects.for_user(user.pk).get(id=conversation.id).updated_at
    assert post("r3", fresh) == "duplicate"
    assert len(messages_of(conversation)) == 2


# --- R7: the user's turn wins ---------------------------------------------------------------------


def test_follow_up_yields_to_user_turn(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    install(monkeypatch, says("Here is the update."))
    user, conversation, run = chat_run()
    Conversation.objects.for_user(user.pk).filter(id=conversation.id).update(
        turn_started_at=timezone.now()
    )
    event = emitted(user.pk, "run.completed", "Found 2 roles.", run)
    [first] = deferred("chat.follow_up")

    with caplog.at_level(logging.INFO, logger="foray.chat"):
        assert run_now(event.id) == "deferred"
    assert "a turn is streaming; trying again in 4s" in caplog.text
    assert messages_of(conversation) == []
    # The successor waits for its own slot; the job that was already queued holds the lock.
    assert [j.id for j in deferred("chat.follow_up")] == [first.id]

    # The turn ends: the next attempt posts.
    Conversation.objects.for_user(user.pk).filter(id=conversation.id).update(turn_started_at=None)
    assert run_now(event.id) == "posted"
    assert [m.role for m in messages_of(conversation)] == ["user", "assistant"]


def test_a_follow_up_re_defers_itself_while_a_turn_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run as the worker runs it: the job re-defers itself (4 s later, same queueing lock) and
    ends; no worker slot waits."""
    install(monkeypatch, says("Here is the update."))
    user, conversation, run = chat_run()
    Conversation.objects.for_user(user.pk).filter(id=conversation.id).update(
        turn_started_at=timezone.now()
    )
    event = emitted(user.pk, "run.completed", "Done.", run)
    run_deferred(["interactive"])
    [later] = deferred("chat.follow_up")
    assert later.queueing_lock == f"follow-up:{event.id}" and later.args == {"event_id": event.id}
    [(scheduled_at,)] = sql("SELECT scheduled_at FROM procrastinate_jobs WHERE id = %s", [later.id])
    assert scheduled_at > timezone.now()
    assert messages_of(conversation) == []


def test_a_dead_turns_marker_is_ignored_and_stale_news_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, says("Update."))
    user, conversation, run = chat_run()
    long_ago = timezone.now() - timedelta(seconds=250)  # older than the 180 s turn timeout
    Conversation.objects.for_user(user.pk).filter(id=conversation.id).update(
        turn_started_at=long_ago
    )
    event = emitted(user.pk, "run.completed", "Done.", run)
    assert run_now(event.id) == "posted"

    _, other, other_run = chat_run()
    Conversation.objects.for_user(other.user_id).filter(id=other.id).update(
        turn_started_at=timezone.now()
    )
    old = emitted(other.user_id, "run.completed", "Done.", other_run)
    Event.objects.unscoped("tests").filter(id=old.id).update(
        created_at=timezone.now() - timedelta(minutes=6)
    )
    assert run_now(old.id) == "skipped (stale)"
    assert messages_of(other) == []


def test_follow_up_retries_when_the_conversation_moved(monkeypatch: pytest.MonkeyPatch) -> None:
    """The user spoke while the reply was being generated: the generation is thrown away (a few
    cents, ledgered) and the follow-up re-defers, so nothing is written above their turn."""
    holder: dict[str, Any] = {}

    def user_speaks() -> None:
        Conversation.objects.for_user(holder["user"]).filter(id=holder["conversation"]).update(
            updated_at=timezone.now()
        )

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        await db(user_speaks)
        yield "Stale by the time it lands."

    install(monkeypatch, stream)
    user, conversation, run = chat_run()
    holder.update(user=user.pk, conversation=conversation.id)
    event = emitted(user.pk, "run.completed", "Done.", run)
    [queued] = deferred("chat.follow_up")
    assert run_now(event.id) == "deferred"
    assert messages_of(conversation) == []
    assert [j.id for j in deferred("chat.follow_up")] == [queued.id]
    assert LlmCall.objects.for_user(user.pk).filter(purpose="chat").count() == 1


# --- what is not news -----------------------------------------------------------------------------


def test_skips_what_is_no_longer_news(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, says("Should not be posted."))

    user, conversation, run = chat_run(status="running")
    event = emitted(user.pk, "run.completed", "Done.", run)
    Run.objects.for_user(user.pk).filter(id=run.id).update(status="cancelled")
    assert run_now(event.id) == "skipped (cancelled)"

    user, conversation, run = chat_run(status="awaiting_approval")
    approval = make_approval(user, run=run, status="approved")
    gate = emitted(user.pk, "approval.requested", "Waiting.", run, approval_id=approval.id)
    assert run_now(gate.id) == "skipped (decided)"
    assert followup_events(user.pk)[-1].data == {
        "conversationId": str(conversation.id),
        "runId": str(run.id),
        "skipped": True,
        "reason": "decided",
    }
    assert messages_of(conversation) == []

    # A deleted chat leaves its runs (conversation NULL): there is nobody to tell.
    user, conversation, run = chat_run()
    event = emitted(user.pk, "run.completed", "Done.", run)
    Conversation.objects.for_user(user.pk).filter(id=conversation.id).delete()
    assert Run.objects.for_user(user.pk).get(id=run.id).conversation_id is None
    assert run_now(event.id) == "nobody to tell"
    assert followup_events(user.pk) == []


def test_an_empty_reply_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, says("   "))
    user, conversation, run = chat_run()
    event = emitted(user.pk, "run.failed", "Boom.", run)
    assert run_now(event.id) == "failed"
    assert messages_of(conversation) == []
    [announced] = followup_events(user.pk)
    assert (announced.data or {})["failed"] is True
    assert announced.message == "No chat follow-up was needed."


def test_a_model_error_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        raise RuntimeError("provider overloaded")
        yield ""  # pragma: no cover

    install(monkeypatch, stream)
    user, conversation, run = chat_run()
    event = emitted(user.pk, "run.completed", "Done.", run)
    assert run_now(event.id) == "failed"
    assert messages_of(conversation) == []


# --- read only, capped, attributed ----------------------------------------------------------------


def test_follow_up_is_read_only_capped_and_attributed(monkeypatch: pytest.MonkeyPatch) -> None:
    offered: list[set[str]] = []

    async def stream(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
        names = {tool.name for tool in info.function_tools}
        offered.append(names)
        if "get_run_status" in names:  # a greedy model: it calls a tool whenever it can
            yield {
                0: DeltaToolCall(
                    name="get_run_status",
                    json_args='{"run_id":null}',
                    tool_call_id=f"c{len(offered)}",
                )
            }
        else:
            yield "That is the status."

    install(monkeypatch, stream)
    user, conversation, run = chat_run()
    event = emitted(user.pk, "run.completed", "Done.", run)
    assert run_now(event.id) == "posted"

    assert offered[0] == set(FOLLOW_UP_TOOLS)
    assert offered == [set(FOLLOW_UP_TOOLS)] * 3 + [set()]  # 4 steps; the last offers nothing
    reply = messages_of(conversation)[1]
    kinds = [part["type"] for part in reply.parts]
    assert kinds.count("tool-get_run_status") == 3 and kinds[-1] == "text"
    output = next(p for p in reply.parts if p["type"] == "tool-get_run_status")["output"]
    assert output["ok"] is True and output["runId"] == str(run.id)

    rows = list(LlmCall.objects.for_user(user.pk).filter(purpose="chat"))
    assert len(rows) == 4
    assert {(row.run_id, row.conversation_id) for row in rows} == {(run.id, conversation.id)}


def test_a_gate_follow_up_names_the_proposed_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts: list[str] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        prompts.append(fake.latest_user_text(messages))
        yield "Both look strong. Shall I go ahead with the deep review?"

    install(monkeypatch, stream)
    user, conversation, run = chat_run(
        status="awaiting_approval",
        input={"prompt": "entry-level roles", "brief": {"seniority": "entry"}},
    )
    first = make_match(user, job=make_job(title="  Data Engineer "), fast_score=81)
    second = make_match(user, job=make_job(title="Platform Engineer"), fast_score=77)
    payload = {
        "items": [
            {"matchId": str(second.id), "jobId": second.job_id, "title": "x", "company": "y"},
            {"matchId": str(first.id), "jobId": first.job_id, "title": "x", "company": "y"},
        ],
        "estimate": None,
        "note": None,
    }
    approval = make_approval(user, run=run, payload=payload, title="Deep-evaluate 2 roles")
    gate = emitted(user.pk, "approval.requested", "Waiting for you.", run, approval_id=approval.id)
    assert run_now(gate.id) == "posted"
    [notice_text] = prompts
    lines = notice_text.splitlines()
    roles = [line for line in lines if line.startswith("- ")]
    assert roles[0].startswith("- Platform Engineer at ") and "(77)" in roles[0]
    assert roles[1].startswith("- Data Engineer at ") and "(81)" in roles[1]  # trimmed title
    label = (messages_of(conversation)[0].metadata or {})["label"]
    assert label == "Search paused for your go-ahead"


def test_the_chat_fake_reports_in_text_with_read_only_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`LLM_FAKE`'s chat model (apps/chat/fakes.py): a notice gets `get_run_status`, then two
    sentences; it never calls a tool the follow-up does not offer."""
    fake_models(monkeypatch, {"chat": fake.fake_model("chat")})
    user, conversation, run = chat_run(input={"prompt": "Find roles", "hints": None})
    event = emitted(user.pk, "run.completed", "Found 2 roles.", run)
    assert run_now(event.id) == "posted"
    reply = messages_of(conversation)[1]
    assert [p["type"] for p in reply.parts] == [
        "step-start",
        "tool-get_run_status",
        "step-start",
        "text",
    ]
    assert reply.parts[3]["text"] == (
        "Foray checked the run: it is completed. Tell me what you would like to do next."
    )
