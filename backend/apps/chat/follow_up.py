"""The agent reporting back on runs started from chat (design §7.7; a port of
`src/lib/chat/follow-up.ts`).

A chat turn ends when the agent starts a run; the run takes minutes. When it pauses for a
decision, finishes or fails (`FOLLOW_UP_KINDS`), `events.emit()` defers `chat.follow_up(event_id)`
in the same transaction, and this module adds two messages to that conversation: an automatic
update describing the moment (a user-role "run update" notice the chat shows as a divider) and the
agent's reply to it, written with read-only tools so it can show results but never act.

The user's own turn always has priority, and the follow-up never waits inside a worker slot:

1. **Skip** a cancelled run, a decision already answered, a deleted chat, or an event already
   reported (`messages_one_notice_per_event`). A skip emits `chat.followup {skipped, reason}`.
2. **Yield** to a streaming turn: while `turn_started_at` is younger than 200 s the task
   re-defers itself in 4 s and returns (a marker older than the 180 s turn timeout belongs to a
   dead process and is ignored). An event more than 5 minutes old is skipped as `stale`.
3. **Generate without writing:** read the history (noting `updated_at`), build the notice with
   `describe_update`, and run the agent headless (read-only tools, 4 steps, 90 s) into an
   accumulator.
4. **Post atomically:** lock the conversation; if a turn started or anything was written since
   the history was read, re-defer (step 2); otherwise insert the notice and the reply back to
   back and emit `chat.followup {conversationId, runId, messageId, label}`.
5. An **empty reply** writes nothing and emits `chat.followup {failed: true}`.

Every `chat.followup` event is level `debug` and has no run in its `run_id` column, so it never
triggers another follow-up. Model calls are attributed to `(user, run, conversation)`.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from django.db import IntegrityError, transaction
from django.utils import timezone
from procrastinate.exceptions import AlreadyEnqueued

from apps.chat import convert
from apps.chat.models import Conversation, Message, Role
from apps.chat.uistream import UIMessageAccumulator
from apps.chat.views import (
    TIMED_OUT,
    Turn,
    agent_chunks,
    instructions_for,
    load_history,
)
from apps.core.db import db
from apps.core.ids import generate_message_id
from apps.core.jsonio import js_num
from apps.core.queue import defer_in_tx
from apps.llm.context import llm_context
from apps.matching.brief import describe_brief, parse_brief
from apps.matching.queries import get_match_rows
from apps.resumes.text import js_trim
from apps.runs import step
from apps.runs.approvals import payload_item_ids
from apps.runs.catalogue import (
    FOLLOW_UP_EVENT,
    FOLLOW_UP_KIND_NAME,
    FOLLOW_UP_KIND_TITLE,
    ApprovalKind,
    ApprovalStatus,
    EventKind,
    Level,
    RunKind,
    RunStatus,
)
from apps.runs.events import FOLLOW_UP_TASK, emit
from apps.runs.models import Approval, Event, Run

__all__ = [
    "FOLLOW_UP_TIMEOUT_S",
    "MARKER_TTL",
    "REDEFER_S",
    "STALE_AFTER",
    "ApprovalView",
    "EventView",
    "ProposedRole",
    "RunView",
    "Update",
    "describe_update",
    "post_follow_up",
    "proposed_roles",
    "run_follow_up",
]

log = logging.getLogger("foray.chat")

FOLLOW_UP_TIMEOUT_S: float = 90  # the headless agent run (read at call time: tests shorten it)
MARKER_TTL: Final = timedelta(seconds=200)  # a marker older than this belongs to a dead process
REDEFER_S: Final = 4
STALE_AFTER: Final = timedelta(minutes=5)
PROPOSED_MAX: Final = 10

POSTED_MESSAGE: Final = "Foray followed up in chat."
NOT_NEEDED_MESSAGE: Final = "No chat follow-up was needed."


# --- what the agent is told ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunView:
    """What the update says about a run (a plain copy, so the text is testable without rows)."""

    id: Any
    kind: str
    input: Any
    output: Any

    @classmethod
    def of(cls, run: Run) -> RunView:
        return cls(id=run.id, kind=run.kind, input=run.input, output=run.output)


@dataclass(frozen=True, slots=True)
class EventView:
    kind: str
    message: str

    @classmethod
    def of(cls, event: Event) -> EventView:
        return cls(kind=event.kind, message=event.message)


@dataclass(frozen=True, slots=True)
class ApprovalView:
    kind: str
    title: str
    payload: Any

    @classmethod
    def of(cls, approval: Approval) -> ApprovalView:
        return cls(kind=approval.kind, title=approval.title, payload=approval.payload)


@dataclass(frozen=True, slots=True)
class ProposedRole:
    """A role a gate is asking about, as the update describes it."""

    title: str
    company: str
    score: int | float | None
    headline: str | None
    locations: str


@dataclass(frozen=True, slots=True)
class Update:
    label: str  # the divider's text, e.g. "Search finished"
    text: str


RULES: Final = (
    "Keep it short and conversational: a few sentences, no headings. Do not repeat what the "
    "conversation above already says, and do not restate numbers the run card shows unless they "
    "matter. In this reply you can only look things up: do not say you started, approved, moved "
    "or changed anything."
)


def _role_lines(roles: Sequence[ProposedRole]) -> str:
    return "\n".join(
        f"- {role.title} at {role.company}"
        + (f" ({js_num(role.score)})" if role.score is not None else "")
        + (f", {role.locations}" if role.locations else "")
        + (f": {role.headline}" if role.headline else "")
        for role in roles
    )


def _capitalize(text: str) -> str:
    return text[:1].upper() + text[1:]


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _what(run: RunView) -> str:
    value = run.input if isinstance(run.input, Mapping) else {}
    prompt = _str(value.get("prompt"))
    if run.kind == RunKind.SEARCH:
        brief = value.get("brief")
        scope = describe_brief(parse_brief(brief if brief is not None else value.get("hints")))
        if scope is not None:
            return scope
        return prompt if prompt is not None else "a search"
    if run.kind == RunKind.TAILOR:
        return (
            re.sub(r"^Tailor ", "tailoring ", prompt)
            if prompt is not None
            else ("tailoring a resume")
        )
    if prompt is not None:
        return prompt
    return FOLLOW_UP_KIND_NAME.get(run.kind) or "background work"


def describe_update(
    run: RunView,
    event: EventView,
    approval: ApprovalView | None,
    proposed: Sequence[ProposedRole] = (),
) -> Update:
    """The automatic update: what happened, in plain words, and what a useful report back looks
    like for this kind of moment. Pinned by `goldens/strings/describe_update.json`."""
    what = _what(run)
    head = f"[Automatic update about run {run.id}, not written by the user]"
    list_it = (
        f'Call list_jobs with run_id "{run.id}" to see them; the user sees those as cards, so do '
        "not retype their details."
    )
    title = FOLLOW_UP_KIND_TITLE.get(run.kind) or "Run"

    if event.kind == EventKind.APPROVAL_REQUESTED and approval is not None:
        if approval.kind == ApprovalKind.DEEP_EVAL_BATCH:
            lines = [
                head,
                f"The search ({what}) has scored what it found and is paused, waiting for the "
                f"user: {approval.title}. {event.message}",
                "The roles it proposes to review, best first (fast score in brackets):\n"
                + _role_lines(proposed)
                if proposed
                else None,
                "The approval card above already lists these roles, so do not call list_jobs. In "
                "two or three sentences, say what stands out: which look strongest and why, and "
                "anything that gives you pause. Then ask whether to go ahead with the deep "
                "review. Do not answer the approval yourself.",
                RULES,
            ]
            return Update("Search paused for your go-ahead", "\n".join(x for x in lines if x))
        if approval.kind == ApprovalKind.SHORTLIST_PROPOSAL:
            items = list(proposed) or [
                ProposedRole(
                    title=_str(item.get("title")) or "",
                    company=_str(item.get("company")) or "",
                    score=None,
                    headline=None,
                    locations="",
                )
                for item in _payload_items(approval.payload)
            ]
            noun = "role" if len(items) == 1 else "roles"
            return Update(
                "Deep review done",
                "\n".join(
                    [
                        head,
                        f"The deep review for the search ({what}) is done. It proposes "
                        f"shortlisting {len(items)} {noun}, with the review's score and "
                        f"verdict:\n{_role_lines(items)}",
                        "The approval card above already lists these roles, so do not call "
                        "list_jobs. Say which look best and why, grounded in the reviews (get_job "
                        "has the evidence if you need more), then ask whether to shortlist them. "
                        "Do not answer the approval yourself.",
                        RULES,
                    ]
                ),
            )

    if event.kind == EventKind.APPROVAL_REQUESTED:
        waiting_on = approval.title if approval is not None else event.message
        return Update(
            "Waiting for your go-ahead",
            "\n".join(
                [
                    head,
                    f"{what} is paused, waiting for the user: {waiting_on}.",
                    "Say what it needs and ask.",
                    RULES,
                ]
            ),
        )

    if event.kind in (EventKind.RUN_FAILED, EventKind.TAILOR_FAILED):
        return Update(
            f"{title} stopped",
            "\n".join(
                [
                    head,
                    f"{_capitalize(what)} stopped with an error: {event.message}",
                    "Explain what happened in a sentence, without technical detail, and offer to "
                    "try again.",
                    RULES,
                ]
            ),
        )

    if run.kind == RunKind.SEARCH:
        output = run.output if isinstance(run.output, Mapping) else {}
        candidates = output.get("candidates")
        found = candidates if candidates is not None else 0
        # `(output.candidates ?? 0) === 0`: only the number zero counts as empty.
        empty = isinstance(found, int | float) and not isinstance(found, bool) and found == 0
        return Update(
            "Search finished",
            "\n".join(
                [
                    head,
                    f"The search ({what}) has finished. Its result: {event.message}",
                    "Nothing matched. Say so plainly and suggest one or two ways to widen the "
                    "search."
                    if empty
                    else f"{list_it} Show the best few (limit 5), then say in one or two "
                    "sentences what stands out.",
                    "End with one concrete next step you could take for them.",
                    RULES,
                ]
            ),
        )

    if run.kind == RunKind.RANK_DEEP:
        return Update(
            "Deep review finished",
            "\n".join(
                [
                    head,
                    f"The deep review has finished. Its result: {event.message}",
                    "Give the verdict on each reviewed role in a sentence or two, grounded in the "
                    "review (get_job with its match id has the evidence), and suggest a next "
                    "step.",
                    RULES,
                ]
            ),
        )

    if run.kind == RunKind.TAILOR:
        return Update(
            "Tailored resume ready",
            "\n".join(
                [
                    head,
                    f"{_capitalize(what)} is done. {event.message}",
                    "Tell them it is ready (the card above has the PDF), mention anything "
                    "flagged for their review, and suggest what to do next.",
                    RULES,
                ]
            ),
        )

    return Update(
        f"{title} finished",
        "\n".join(
            [
                head,
                f"{_capitalize(what)} has finished: {event.message}",
                "Report the result briefly.",
                RULES,
            ]
        ),
    )


def _payload_items(payload: Any) -> list[Mapping[str, Any]]:
    items = payload.get("items") if isinstance(payload, Mapping) else None
    return [item for item in items if isinstance(item, Mapping)] if isinstance(items, list) else []


def proposed_roles(user_id: str, approval: Approval) -> list[ProposedRole]:
    """At a gate, the roles in question go into the update itself: the approval card already
    lists them, so the reply should talk about them rather than list them again. Sync."""
    ids = payload_item_ids(approval.payload)[:PROPOSED_MAX]
    order = {item_id: index for index, item_id in enumerate(ids)}
    rows = sorted(get_match_rows(user_id, ids), key=lambda row: order.get(str(row.match_id), 0))
    return [
        ProposedRole(
            title=js_trim(row.title),
            company=row.company,
            score=row.deep_score if row.deep_score is not None else row.fast_score,
            headline=row.headline,
            locations=", ".join(row.locations[:3]),
        )
        for row in rows
    ]


# --- the task's steps (sync, through db()) --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Skip:
    reason: str  # cancelled | decided | gone | duplicate | stale
    user_id: str
    conversation_id: uuid.UUID | None
    run_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class Wait:
    """A user turn is streaming: try again shortly."""


@dataclass(frozen=True, slots=True)
class Loaded:
    event: Event
    run: Run
    conversation_id: uuid.UUID
    approval: Approval | None
    proposed: list[ProposedRole]
    history: list[dict[str, Any]]
    history_read_at: datetime  # conversation.updated_at when the history was read
    instructions: str


def _turn_streaming(conversation: Conversation, now: datetime) -> bool:
    started = conversation.turn_started_at
    return started is not None and now - started < MARKER_TTL


def _stale(event: Event, now: datetime) -> bool:
    return now - event.created_at > STALE_AFTER


def load(event_id: int) -> Loaded | Skip | Wait | None:
    """Step 1 and 2: what to report, or why not now. None when there is nobody to tell (no run,
    or a run not started from a chat that still exists)."""
    event = (
        Event.objects.unscoped("chat.follow_up: the event it was deferred for")
        .filter(id=event_id)
        .first()
    )
    if event is None or event.run_id is None:
        return None
    user_id = event.user_id
    run = Run.objects.for_user(user_id).filter(id=event.run_id).first()
    if run is None or run.conversation_id is None:
        return None  # a deleted chat leaves runs.conversation NULL: nobody to tell

    def skip(reason: str) -> Skip:
        return Skip(reason, user_id, run.conversation_id, run.id)

    conversation = Conversation.objects.for_user(user_id).filter(id=run.conversation_id).first()
    if conversation is None:
        return skip("gone")
    if run.status == RunStatus.CANCELLED:
        return skip("cancelled")  # news that is already stale is not worth a message
    approval = (
        Approval.objects.for_user(user_id).filter(id=event.approval_id).first()
        if event.approval_id is not None
        else None
    )
    if event.kind == EventKind.APPROVAL_REQUESTED and (
        approval is None or approval.status != ApprovalStatus.PENDING
    ):
        return skip("decided")  # answered from the card, most likely
    already = Message.objects.for_user(user_id).filter(
        conversation_id=conversation.id, run_update_event_id=event.id
    )
    if already.exists():
        return skip("duplicate")
    now = timezone.now()
    if _turn_streaming(conversation, now):
        return skip("stale") if _stale(event, now) else Wait()
    return Loaded(
        event=event,
        run=run,
        conversation_id=conversation.id,
        approval=approval,
        proposed=proposed_roles(user_id, approval) if approval is not None else [],
        history=load_history(user_id, conversation.id),
        history_read_at=conversation.updated_at,
        instructions=instructions_for(user_id),
    )


def announce(
    user_id: str, conversation_id: uuid.UUID | None, run_id: uuid.UUID, **data: Any
) -> None:
    """`chat.followup`: an open chat reloads on it, or clears "looking at the results". Level
    debug, no run in the column (so it never triggers another follow-up)."""
    emit(
        user_id,
        FOLLOW_UP_EVENT,
        POSTED_MESSAGE if data.get("messageId") else NOT_NEEDED_MESSAGE,
        level=Level.DEBUG,
        data={
            "conversationId": None if conversation_id is None else str(conversation_id),
            "runId": str(run_id),
            **data,
        },
    )


@dataclass(frozen=True, slots=True)
class Post:
    user_id: str
    conversation_id: uuid.UUID
    run_id: uuid.UUID
    event_id: int
    history_read_at: datetime
    notice_id: str
    notice_text: str
    label: str
    reply_id: str
    reply_parts: list[Any]


def post_follow_up(post: Post) -> str:
    """Step 4, one transaction: `posted`, `retry` (the user spoke meanwhile, so the reply would
    be stale or interleaved), `gone` or `duplicate`."""
    with transaction.atomic():
        conversation = (
            Conversation.objects.for_user(post.user_id)
            .select_for_update()
            .filter(id=post.conversation_id)
            .first()
        )
        if conversation is None:
            return "gone"
        now = timezone.now()
        if _turn_streaming(conversation, now) or conversation.updated_at > post.history_read_at:
            return "retry"
        try:
            with transaction.atomic():  # a savepoint: the unique notice may already exist
                Message.objects.create_for(
                    post.user_id,
                    conversation_id=conversation.id,
                    message_id=post.notice_id,
                    role=Role.USER,
                    parts=[{"type": "text", "text": post.notice_text}],
                    metadata={
                        "kind": "run_update",
                        "runId": str(post.run_id),
                        "eventId": post.event_id,
                        "label": post.label,
                    },
                    run_update_event_id=post.event_id,
                )
                Message.objects.create_for(
                    post.user_id,
                    conversation_id=conversation.id,
                    message_id=post.reply_id,
                    role=Role.ASSISTANT,
                    parts=post.reply_parts,
                    metadata={
                        "kind": "follow_up",
                        "runId": str(post.run_id),
                        "eventId": post.event_id,
                    },
                )
        except IntegrityError:
            return "duplicate"
        Conversation.objects.for_user(post.user_id).filter(id=conversation.id).update(
            updated_at=now
        )
        announce(
            post.user_id,
            conversation.id,
            post.run_id,
            messageId=post.reply_id,
            label=post.label,
        )
    return "posted"


def redefer(event_id: int) -> bool:
    """Try again in a few seconds. Only `todo` jobs hold the queueing lock, so the running job
    may defer its successor; False when one is already waiting."""
    try:
        defer_in_tx(
            step.task_for(FOLLOW_UP_TASK),
            queueing_lock=f"follow-up:{event_id}",
            schedule_in={"seconds": REDEFER_S},
            event_id=event_id,
        )
    except AlreadyEnqueued:
        return False
    return True


def _later(event_id: int) -> str:
    """Re-defer, unless the event has gone stale meanwhile."""
    event = (
        Event.objects.unscoped("chat.follow_up: the event it was deferred for")
        .filter(id=event_id)
        .first()
    )
    if event is not None and _stale(event, timezone.now()):
        return "stale"
    redefer(event_id)
    return "deferred"


# --- the task ------------------------------------------------------------------------------------


def _has_content(message: Mapping[str, Any]) -> bool:
    for part in message.get("parts") or []:
        kind = str(part.get("type", ""))
        if kind == "text" and str(part.get("text") or "").strip():
            return True
        if kind.startswith("tool-"):
            return True
    return False


async def _generate(loaded: Loaded, notice: dict[str, Any]) -> dict[str, Any]:
    """Step 3: the agent's reply, headless, into an accumulator only."""
    turn_input = convert.turn_input([*loaded.history, notice])
    turn = Turn(
        user_id=loaded.event.user_id,
        conversation_id=loaded.conversation_id,
        reply_id=generate_message_id(),
        instructions=loaded.instructions,
        user_prompt=turn_input.user_prompt,
        message_history=turn_input.message_history,
        follow_up=True,
        run_id=loaded.run.id,
    )
    acc = UIMessageAccumulator(turn.reply_id)
    with llm_context(
        user_id=turn.user_id, run_id=loaded.run.id, conversation_id=loaded.conversation_id
    ):
        try:
            async with asyncio.timeout(FOLLOW_UP_TIMEOUT_S):
                async for chunk in agent_chunks(turn):
                    acc.apply(chunk)
        except TimeoutError:
            acc.close_with_error(TIMED_OUT)  # a partial reply with content is still posted
    return acc.message


async def run_follow_up(event_id: int) -> str:
    """`chat.follow_up`: report one event in its chat. Returns what happened, for the log."""
    loaded = await db(load, event_id)
    if loaded is None:
        return "nobody to tell"
    if isinstance(loaded, Skip):
        await db(
            announce,
            loaded.user_id,
            loaded.conversation_id,
            loaded.run_id,
            skipped=True,
            reason=loaded.reason,
        )
        return f"skipped ({loaded.reason})"
    if isinstance(loaded, Wait):
        # load() skips an event that is already stale; this one is not, so it waits its turn.
        await db(redefer, event_id)
        log.info(
            "chat follow-up for event %s: a turn is streaming; trying again in %ss",
            event_id,
            REDEFER_S,
        )
        return "deferred"

    run, event = loaded.run, loaded.event
    update = describe_update(
        RunView.of(run),
        EventView.of(event),
        ApprovalView.of(loaded.approval) if loaded.approval is not None else None,
        loaded.proposed,
    )
    notice = {
        "id": generate_message_id(),
        "role": Role.USER.value,
        "parts": [{"type": "text", "text": update.text}],
        "metadata": {
            "kind": "run_update",
            "runId": str(run.id),
            "eventId": event.id,
            "label": update.label,
        },
    }
    try:
        reply = await _generate(loaded, notice)
    except Exception:
        log.exception("chat follow-up for run %s, event %s failed", run.id, event.id)
        await db(announce, event.user_id, loaded.conversation_id, run.id, failed=True)
        return "failed"
    if not _has_content(reply):
        log.warning("chat follow-up for run %s, event %s came back empty", run.id, event.id)
        await db(announce, event.user_id, loaded.conversation_id, run.id, failed=True)
        return "failed"

    outcome = await db(
        post_follow_up,
        Post(
            user_id=event.user_id,
            conversation_id=loaded.conversation_id,
            run_id=run.id,
            event_id=event.id,
            history_read_at=loaded.history_read_at,
            notice_id=notice["id"],
            notice_text=update.text,
            label=update.label,
            reply_id=reply["id"],
            reply_parts=reply["parts"],
        ),
    )
    if outcome == "retry":
        # The user spoke meanwhile; the discarded generation was a few cents of read-only chat.
        later = await db(_later, event_id)
        if later == "stale":
            await db(
                announce,
                event.user_id,
                loaded.conversation_id,
                run.id,
                skipped=True,
                reason="stale",
            )
            return "skipped (stale)"
        log.info(
            "chat follow-up for event %s: the conversation moved on; trying again in %ss",
            event_id,
            REDEFER_S,
        )
        return "deferred"
    if outcome in ("gone", "duplicate"):
        await db(
            announce, event.user_id, loaded.conversation_id, run.id, skipped=True, reason=outcome
        )
        return f"skipped ({outcome})"
    return outcome
