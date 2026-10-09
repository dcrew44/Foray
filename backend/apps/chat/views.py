"""`POST /api/chat`, the one async endpoint (design §7.3, §7.5, R13).

A plain Django async view, mounted before the ninja API. The request thread never touches the
database: every query runs through `db()`, and anything middleware might have opened is released
before streaming. So an open chat stream holds no pooled connection between its `db()` calls.

1. **`prepare_turn`** (sync, one transaction after the throttle): who is asking, the `chat:<user>`
   throttle, the conversation claimed (or refused), the message validated, a regenerate detected,
   the user message written with the turn marker, and everything the agent needs loaded. Its
   refusals are plain text with a status, because `useChat` shows the body as the error.
2. **`pump_turn`** owns the agent run and the 180 s turn timeout. It never yields to Django, so
   a timeout can only cancel the agent, never Django's `send()`.
3. **`stream_turn`** is the response body. It reads the pump's queue, sends `: ping` every 15 s
   while the model is quiet, tees every chunk into a `UIMessageAccumulator`, saves a checkpoint
   at every finished step, and in `finally` (a normal end, an error, or the client's Stop) stops
   the pump and saves the reply under `asyncio.shield`. `[DONE]` is sent only once the reply is
   saved.

Ids: the conversation id is the client's, claimed or refused; the user message keeps the client's
id; the reply id is `generate_message_id()`, sent in `start.messageId`. "Try again" re-sends the
latest user message's id: the stale tail is deleted and nothing is inserted twice.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from importlib import import_module
from types import SimpleNamespace
from typing import Any, Final, NamedTuple, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib import auth
from django.core.exceptions import RequestDataTooBig
from django.db import connection, transaction
from django.http import HttpRequest, HttpResponse, StreamingHttpResponse
from django.http.response import HttpResponseBase
from django.utils import timezone
from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic_ai.messages import ModelMessage

from apps.accounts.models import User
from apps.accounts.schemas import InterviewState, Preferences, Settings
from apps.chat import convert
from apps.chat.agent import ChatDeps, build_agent, usage_limits
from apps.chat.models import Conversation, Message, Role
from apps.chat.prompt import system_prompt
from apps.chat.uistream import (
    DONE,
    GENERIC_ERROR,
    PING,
    Chunk,
    ForayVercelStream,
    UIMessageAccumulator,
    frame,
    ui_headers,
)
from apps.core import throttle
from apps.core.db import db
from apps.core.errors import NOT_SIGNED_IN, UNEXPECTED, ApiError
from apps.core.ids import generate_message_id
from apps.core.jsonio import js_json
from apps.llm.context import llm_context
from apps.resumes.queries import latest_base_resume

__all__ = [
    "CHAT_TURN_TIMEOUT_S",
    "INVALID_TOOL_INPUT",
    "PING_EVERY_S",
    "ChatRequest",
    "Turn",
    "agent_chunks",
    "chat_view",
    "checkpoint_reply",
    "finish_turn",
    "load_history",
    "prepare_turn",
    "pump_turn",
    "stored_message",
    "stream_turn",
]

log = logging.getLogger("foray.chat")

# Read at call time, so tests can shorten them.
CHAT_TURN_TIMEOUT_S: float = 180
PING_EVERY_S: float = 15

MESSAGE_MAX: Final = 20_000  # characters of text in one user message
TITLE_MAX: Final = 80  # a conversation's title: the first message's first 80 characters
MESSAGE_ID_MAX: Final = 200
TIME_ZONE_MAX: Final = 64  # users.timezone

UNREADABLE: Final = "That request could not be read."
NO_MESSAGE: Final = "No message provided."
UNREADABLE_MESSAGE: Final = "That message could not be read."
NO_TEXT: Final = "That message has no text."
TOO_LONG: Final = "That message is too long (20,000 characters at most)."
BAD_CHAT_ID: Final = "That chat id is not valid."
NO_SUCH_CHAT: Final = "That chat does not exist."
ALREADY_SENT: Final = "That message was already sent."
TIMED_OUT: Final = "That reply took too long and was stopped."
# What a call the model got wrong shows under its tool line. pydantic-ai's own text (the
# validation report, or "Unknown tool name") goes back to the model as its retry prompt; the
# user gets one plain sentence, as the AI SDK's "An error occurred." was.
INVALID_TOOL_INPUT: Final = "Its input was not valid, so it did not run."


# --- the request ---------------------------------------------------------------------------------


class ChatRequest(BaseModel):
    """Tolerant: the app sends `{id, message, tz}`; the SDK's default `{id, messages, trigger,
    messageId}` works too (the last message is the one sent)."""

    model_config = ConfigDict(extra="allow")

    id: str
    message: Any = None
    messages: list[Any] | None = None
    tz: Any = None


@dataclass
class Turn:
    """Everything one agent turn needs, loaded before streaming starts."""

    user_id: str
    conversation_id: uuid.UUID
    reply_id: str
    instructions: str
    user_prompt: str | None
    message_history: list[ModelMessage] = field(default_factory=list[ModelMessage])
    follow_up: bool = False
    run_id: uuid.UUID | None = None  # follow-ups: the run reported on (attribution)

    @property
    def deps(self) -> ChatDeps:
        return ChatDeps(self.user_id, self.conversation_id, self.follow_up)

    @property
    def agent_args(self) -> dict[str, Any]:
        return {
            "user_prompt": self.user_prompt,
            "message_history": self.message_history,
            "deps": self.deps,
            "usage_limits": usage_limits(self.follow_up),
        }


# --- conversations and messages (sync; through db()) ---------------------------------------------


def stored_message(row: Message) -> dict[str, Any]:
    """A stored row as the `UIMessage` the client renders: `{id, role, parts, metadata?}`."""
    message: dict[str, Any] = {"id": row.message_id, "role": row.role, "parts": row.parts}
    if row.metadata is not None:
        message["metadata"] = row.metadata
    return message


def load_history(user_id: str, conversation_id: uuid.UUID) -> list[dict[str, Any]]:
    """The conversation's messages in order (`seq`, so there are no `created_at` ties)."""
    rows = Message.objects.for_user(user_id).filter(conversation_id=conversation_id).order_by("seq")
    return [stored_message(row) for row in rows]


_UPSERT_REPLY = """
INSERT INTO messages (conversation_id, user_id, message_id, role, parts, metadata,
                      created_at, updated_at)
VALUES (%(conversation)s, %(user)s, %(message)s, 'assistant', %(parts)s::jsonb, %(metadata)s::jsonb,
        now(), now())
ON CONFLICT (conversation_id, message_id) DO UPDATE
   SET parts = EXCLUDED.parts, metadata = EXCLUDED.metadata, updated_at = now()
 WHERE messages.user_id = EXCLUDED.user_id AND messages.role = 'assistant'
"""


def _lock_conversation(user_id: str, conversation_id: uuid.UUID) -> Conversation | None:
    """Inside a transaction: the user's conversation, locked (a delete waits for the save)."""
    return (
        Conversation.objects.for_user(user_id)
        .select_for_update()
        .filter(id=conversation_id)
        .first()
    )


def _save_reply(turn: Turn, message: Mapping[str, Any]) -> None:
    parts = message.get("parts")
    if not parts:
        return  # a reply with no parts is not saved
    metadata = message.get("metadata")
    with connection.cursor() as cursor:
        cursor.execute(
            _UPSERT_REPLY,
            {
                "conversation": str(turn.conversation_id),
                "user": turn.user_id,
                "message": turn.reply_id,
                "parts": js_json(parts),
                "metadata": None if metadata is None else js_json(metadata),
            },
        )


def checkpoint_reply(turn: Turn, message: Mapping[str, Any]) -> None:
    """Save the reply so far (after each finished step): a killed process loses at most a step."""
    with transaction.atomic():
        if _lock_conversation(turn.user_id, turn.conversation_id) is not None:
            _save_reply(turn, message)


def finish_turn(turn: Turn, message: Mapping[str, Any]) -> None:
    """Save the reply and clear the turn marker (`updated_at` moves, so a follow-up generated
    meanwhile sees that the conversation moved on)."""
    with transaction.atomic():
        if _lock_conversation(turn.user_id, turn.conversation_id) is None:
            return  # deleted while the reply streamed
        _save_reply(turn, message)
        Conversation.objects.for_user(turn.user_id).filter(id=turn.conversation_id).update(
            turn_started_at=None, updated_at=timezone.now()
        )


# --- prepare_turn --------------------------------------------------------------------------------


def _signed_in(session_key: str | None) -> User:
    """The session's user, read on this db thread (the request thread never touches the
    database)."""
    if not session_key:
        raise ApiError(401, NOT_SIGNED_IN)
    engine: Any = import_module(settings.SESSION_ENGINE)
    request: Any = SimpleNamespace(session=engine.SessionStore(session_key=session_key))
    user = auth.get_user(request)
    if not user.is_authenticated:
        raise ApiError(401, NOT_SIGNED_IN)
    return cast(User, user)


def _conversation_id(raw: str) -> uuid.UUID:
    try:
        parsed = uuid.UUID(raw)
    except ValueError, AttributeError, TypeError:
        raise ApiError(400, BAD_CHAT_ID) from None
    if str(parsed) != raw.lower():  # a canonical UUID only, as the TS regex required
        raise ApiError(400, BAD_CHAT_ID)
    return parsed


def _claim(user_id: str, conversation_id: uuid.UUID) -> Conversation:
    """Insert the client's id for this user, or find it; an id another user holds is refused
    (today the server silently created a different conversation the client never learnt of)."""
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO conversations (id, user_id, title, turn_started_at, created_at, "
            "updated_at) VALUES (%s, %s, NULL, NULL, now(), now()) "
            "ON CONFLICT (id) DO NOTHING RETURNING id",
            [str(conversation_id), user_id],
        )
    found = _lock_conversation(user_id, conversation_id)
    if found is None:
        raise ApiError(404, NO_SUCH_CHAT)
    return found


@dataclass(frozen=True, slots=True)
class Incoming:
    message_id: str
    texts: list[str]

    @property
    def parts(self) -> list[dict[str, str]]:
        return [{"type": "text", "text": text} for text in self.texts]


def _incoming(body: ChatRequest) -> Incoming:
    """The user's message, validated: role `user`, text parts only (metadata and anything else
    dropped), at most 20,000 characters. Nothing is written before this passes."""
    raw = body.message
    if raw is None and body.messages:
        raw = body.messages[-1]
    if not isinstance(raw, dict):
        raise ApiError(400, NO_MESSAGE)
    message_id = raw.get("id")
    parts = raw.get("parts")
    if (
        not isinstance(message_id, str)
        or not 0 < len(message_id) <= MESSAGE_ID_MAX
        or raw.get("role") != Role.USER
        or not isinstance(parts, list)
    ):
        raise ApiError(400, UNREADABLE_MESSAGE)
    texts = [
        part["text"]
        for part in parts
        if isinstance(part, dict)
        and part.get("type") == "text"
        and isinstance(part.get("text"), str)
    ]
    if not "".join(texts).strip():
        raise ApiError(400, NO_TEXT)
    if sum(len(text) for text in texts) > MESSAGE_MAX:
        raise ApiError(400, TOO_LONG)
    return Incoming(message_id, texts)


def _valid_zone(tz: Any) -> str | None:
    if not isinstance(tz, str) or not 0 < len(tz) <= TIME_ZONE_MAX:
        return None
    try:
        ZoneInfo(tz)
    except ZoneInfoNotFoundError, ValueError:
        return None
    return tz


def _write_user_message(user_id: str, conversation: Conversation, incoming: Incoming) -> None:
    """Insert the message, or, for "Try again" on the latest user message, drop the stale tail
    so the model never sees the message twice."""
    rows = Message.objects.for_user(user_id).filter(conversation_id=conversation.id)
    existing = rows.filter(message_id=incoming.message_id).first()
    if existing is None:
        Message.objects.create_for(
            user_id,
            conversation_id=conversation.id,
            message_id=incoming.message_id,
            role=Role.USER,
            parts=incoming.parts,
            metadata=None,
        )
        return
    latest = rows.filter(role=Role.USER).order_by("-seq").first()
    if latest is None or latest.seq != existing.seq:
        raise ApiError(409, ALREADY_SENT)
    rows.filter(seq__gt=existing.seq).delete()


@dataclass(frozen=True, slots=True)
class _Snapshot:
    has_resume: bool
    preferences: Preferences
    interview_state: InterviewState
    settings: Settings
    name: str
    time_zone: str


def _snapshot(user_id: str) -> _Snapshot:
    user = User.objects.only("name", "preferences", "interview_state", "settings", "timezone").get(
        pk=user_id
    )
    return _Snapshot(
        has_resume=latest_base_resume(user_id) is not None,
        preferences=Preferences.read(user.preferences),
        interview_state=InterviewState.read(user.interview_state),
        settings=Settings.read(user.settings),
        name=user.name,
        time_zone=user.timezone,
    )


def instructions_for(user_id: str, now: datetime | None = None) -> str:
    """The system prompt from the user's stored state (sync; follow-ups use it too)."""
    snap = _snapshot(user_id)
    return system_prompt(
        has_resume=snap.has_resume,
        preferences=snap.preferences,
        interview_state=snap.interview_state,
        settings=snap.settings,
        user_name=snap.name or None,
        now=now or timezone.now(),
        time_zone=snap.time_zone,
    )


def prepare_turn(session_key: str | None, body: ChatRequest) -> Turn:
    """Everything before the first token (§7.3). Raises `ApiError` with the client's text."""
    user = _signed_in(session_key)
    limit = throttle.CHAT
    throttle.throttle(f"chat:{user.pk}", limit.limit, limit.window_s)  # autocommit: it counts
    conversation_id = _conversation_id(body.id)
    now = timezone.now()
    with transaction.atomic():
        conversation = _claim(user.pk, conversation_id)
        incoming = _incoming(body)
        _write_user_message(user.pk, conversation, incoming)
        changes: dict[str, Any] = {"turn_started_at": now, "updated_at": now}
        if not conversation.title:
            changes["title"] = incoming.texts[0][:TITLE_MAX]
        Conversation.objects.for_user(user.pk).filter(id=conversation.id).update(**changes)
        zone = _valid_zone(body.tz)
        if zone is not None and zone != user.timezone:
            User.objects.filter(pk=user.pk).update(timezone=zone, updated_at=now)
        history = load_history(user.pk, conversation.id)
    turn_input = convert.turn_input(history)
    return Turn(
        user_id=user.pk,
        conversation_id=conversation.id,
        reply_id=generate_message_id(),
        instructions=instructions_for(user.pk, now),
        user_prompt=turn_input.user_prompt,
        message_history=turn_input.message_history,
    )


# --- the stream ----------------------------------------------------------------------------------


def ui_chunk(chunk: Chunk) -> Chunk:
    """A chunk as the user sees it and the row stores it (see `INVALID_TOOL_INPUT`)."""
    if chunk.get("type") == "tool-input-error":
        return {**chunk, "errorText": INVALID_TOOL_INPUT}
    return chunk


async def agent_chunks(turn: Turn) -> AsyncIterator[Chunk]:
    """The turn's chunks, from `start` to `finish` (no `[DONE]`). Agent errors arrive as an
    `error` chunk with the user's text (`ForayVercelStream.on_error`), never as an exception."""
    agent = build_agent(turn.instructions, follow_up=turn.follow_up)
    async with agent.run_stream_events(**turn.agent_args) as events:
        async for chunk in ForayVercelStream(turn).chunks(events):
            yield ui_chunk(chunk)


END: Final = object()


class Abort(NamedTuple):
    text: str


async def pump_turn(turn: Turn, q: asyncio.Queue[Any]) -> None:
    """Owns the agent run and the turn timeout. Never yields to Django, so a timeout here can
    only cancel the agent, never Django's send()."""
    try:
        async with asyncio.timeout(CHAT_TURN_TIMEOUT_S):
            async for chunk in agent_chunks(turn):
                await q.put(chunk)
    except TimeoutError:
        await q.put(Abort(TIMED_OUT))
    except Exception:  # a bug outside the stream class
        log.exception("chat turn %s failed", turn.reply_id)
        await q.put(Abort(GENERIC_ERROR))
    finally:
        await q.put(END)


async def stream_turn(turn: Turn) -> AsyncIterator[str]:
    """The response body (see the module docstring)."""
    acc = UIMessageAccumulator(turn.reply_id)
    q: asyncio.Queue[Any] = asyncio.Queue()
    # Set inside the generator (the view body has returned by the time this runs); the task
    # copies the context.
    with llm_context(user_id=turn.user_id, conversation_id=turn.conversation_id):
        pump = asyncio.create_task(pump_turn(turn, q))
    started = False
    try:
        while True:
            try:
                item = await asyncio.wait_for(q.get(), PING_EVERY_S)  # losing a get() loses nothing
            except TimeoutError:
                yield PING  # a heartbeat; the agent is untouched
                continue
            if item is END:
                break
            if isinstance(item, Abort):
                if not started:  # the client must learn the reply's id even then
                    start: Chunk = {"type": "start", "messageId": turn.reply_id}
                    acc.apply(start)
                    started = True
                    yield frame(start)
                for chunk in acc.close_with_error(item.text):
                    yield frame(chunk)
                continue
            chunk = cast(Chunk, item)
            acc.apply(chunk)
            started = True
            if chunk["type"] == "finish-step":
                await db(checkpoint_reply, turn, acc.message)  # a killed process loses <= 1 step
            yield frame(chunk)
    finally:  # normal end, error, and the client's Stop (CancelledError)
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await pump  # the agent is stopped before the save
        try:
            await asyncio.shield(db(finish_turn, turn, acc.message))
        except Exception:
            log.exception("chat turn %s: could not save the reply", turn.reply_id)
    yield DONE  # only after the row is saved (map/chat-agent.md §3.6)


def _release_request_connections() -> None:
    """Thread-sensitive: closes this request's connection, resolved inside that thread (an
    unopened one is a no-op). It goes back to the pool."""
    from django.db import connections

    connections["default"].close()


def _plain(text: str, status: int) -> HttpResponse:
    return HttpResponse(text, status=status, content_type="text/plain; charset=utf-8")


async def chat_view(request: HttpRequest) -> HttpResponseBase:
    if request.method != "POST":
        response = _plain("Use POST.", 405)
        response["Allow"] = "POST"
        return response
    try:
        body = ChatRequest.model_validate_json(request.body)
    except RequestDataTooBig:
        return _plain(TOO_LONG, 413)
    except ValidationError:
        return _plain(UNREADABLE, 400)
    try:
        turn = await db(prepare_turn, request.COOKIES.get(settings.SESSION_COOKIE_NAME), body)
    except ApiError as error:  # 401 / 400 / 404 / 409 / 429, plain text for useChat
        return _plain(error.message, error.status)
    except Exception:
        log.exception("chat: could not start a turn")
        return _plain(UNEXPECTED, 500)
    await sync_to_async(_release_request_connections)()
    # Django encodes str chunks itself; its stubs only name bytes. The generator is handed over
    # as is: a wrapper would not run its `finally` (the save) when the client stops.
    body: Any = stream_turn(turn)
    return StreamingHttpResponse(body, headers=ui_headers(turn.conversation_id))
