"""Conversation endpoints (design §5.1, §5.3; `map/chat-agent.md` §1.2–§1.4). The chat stream
itself is `chat.views`.

| Operation | Answer |
|---|---|
| `GET /api/conversations` | `{conversations: [{id, title, updatedAt}]}`, newest first, at most 30 |
| `GET /api/conversations/{id}` | `{conversation: {id, title}, messages: [UIMessage]}`, or 404 |
| `DELETE /api/conversations/{id}` | `{ok: true}`, or 404 |

Deleting a chat leaves its runs with `conversation = NULL` (a pending follow-up then skips).

Messages are the stored AI SDK `UIMessage`s, exactly what the client renders (§7.4); `metadata` is
present only when the row has one. A malformed, unknown or another user's id is the same 404.

The chat tools' input and output models are registered here for the generated TypeScript (§5.4,
§6.6). The inputs keep today's snake_case argument names (§7.2), so `chat-types.ts` names both.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from django.http import HttpRequest
from pydantic import Field

from apps.accounts.models import User
from apps.chat.models import Conversation
from apps.chat.tools import INPUT_MODELS, OUTPUT_MODELS, SalaryFloorInput
from apps.chat.views import load_history
from apps.core.errors import ApiError, messages
from apps.core.openapi import register_schema
from apps.core.router import ApiModel, router

conversations = router(tags=["conversations"])

ROUTERS = {"/conversations": conversations}

LIST_LIMIT = 30
UNTITLED = "Untitled chat"
NO_SUCH_CHAT = "That chat does not exist."

for _model in dict.fromkeys([*INPUT_MODELS.values(), SalaryFloorInput, *OUTPUT_MODELS.values()]):
    register_schema(_model)


def _user_id(request: HttpRequest) -> str:
    return cast(User, request.user).pk


# --- schemas -------------------------------------------------------------------------------------


class ConversationItem(ApiModel):
    id: uuid.UUID
    title: str
    updated_at: datetime


class ConversationListOut(ApiModel):
    conversations: list[ConversationItem]


class ConversationRef(ApiModel):
    id: uuid.UUID
    title: str | None


def _absent(value: Any) -> bool:
    return value is None


class ChatMessageOut(ApiModel):
    # One stored AI SDK UIMessage. `metadata` is left out when there is none, as before.
    id: str
    role: str
    parts: list[dict[str, Any]]
    metadata: dict[str, Any] | None = Field(default=None, exclude_if=_absent)


class ConversationOut(ApiModel):
    conversation: ConversationRef
    messages: list[ChatMessageOut]


class ConversationDeletedOut(ApiModel):
    ok: bool


# --- operations ----------------------------------------------------------------------------------


@conversations.get("", response=ConversationListOut, summary="Recent chats")
def list_conversations(request: HttpRequest) -> dict[str, Any]:
    rows = Conversation.objects.for_user(_user_id(request)).order_by("-updated_at")[:LIST_LIMIT]
    return {
        "conversations": [
            {"id": row.id, "title": row.title or UNTITLED, "updated_at": row.updated_at}
            for row in rows
        ]
    }


@conversations.get("/{id}", response=ConversationOut, summary="One chat")
@messages(not_found=NO_SUCH_CHAT)
def get_conversation(request: HttpRequest, id: uuid.UUID) -> dict[str, Any]:
    user_id = _user_id(request)
    row = Conversation.objects.for_user(user_id).filter(id=id).first()
    if row is None:
        raise ApiError(404, NO_SUCH_CHAT)
    return {
        "conversation": {"id": row.id, "title": row.title},
        "messages": load_history(user_id, row.id),
    }


@conversations.delete("/{id}", response=ConversationDeletedOut, summary="Delete a chat")
@messages(not_found=NO_SUCH_CHAT)
def delete_conversation(request: HttpRequest, id: uuid.UUID) -> dict[str, bool]:
    deleted, _ = Conversation.objects.for_user(_user_id(request)).filter(id=id).delete()
    if not deleted:
        raise ApiError(404, NO_SUCH_CHAT)
    return {"ok": True}
