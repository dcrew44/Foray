"""Conversations and their messages (design §2.2, §7.4, §7.5).

- A conversation's id is chosen by the client (`crypto.randomUUID`) and claimed by the server.
  `turn_started_at` is set while a user turn streams, so a follow-up waits (§7.7).
- A message is one AI SDK `UIMessage`, stored in that format only: `parts` and `metadata` are
  written by our accumulator. `seq` orders them (no `created_at` ties), and `message_id` is the
  SDK's id, unique within its conversation, which ends the cross-tenant overwrite of the old
  text primary key. A follow-up notice links its event through `run_update_event`, and at most
  one notice exists per event and conversation (R7).
"""

from __future__ import annotations

import uuid

from django.db import models
from django.db.models import F

from apps.accounts.models import User
from apps.core.choices import choices_checks
from apps.core.models import Timestamped
from apps.core.scoping import UserOwned
from apps.runs.models import Event


class Role(models.TextChoices):
    USER = "user"
    ASSISTANT = "assistant"


class Conversation(UserOwned, Timestamped):
    id = models.UUIDField(primary_key=True)  # client-chosen, claimed by the server
    # Indexed by the (user, updated_at) index.
    user = models.ForeignKey(User, models.CASCADE, related_name="conversations", db_index=False)
    title = models.TextField(null=True)  # noqa: DJ001 (the first 80 characters, set once)
    turn_started_at = models.DateTimeField(null=True)  # set while a user turn streams
    # updated_at bumps on every turn start and finish, and on every follow-up post.

    user_id: str

    class Meta(UserOwned.Meta):
        db_table = "conversations"
        indexes = [
            models.Index(F("user"), F("updated_at").desc(), name="conversations_user_updated"),
        ]

    def __str__(self) -> str:
        return self.title or str(self.id)


class Message(UserOwned, Timestamped):
    seq = models.BigAutoField(primary_key=True)  # the ordering key
    # Indexed by the (conversation, message_id) constraint.
    conversation = models.ForeignKey(
        Conversation, models.CASCADE, related_name="messages", db_index=False
    )
    user = models.ForeignKey(User, models.CASCADE, related_name="messages")
    message_id = models.TextField()  # the AI SDK UIMessage.id
    role = models.TextField(choices=Role.choices)
    parts = models.JSONField()
    # RunUpdateMeta {kind: "run_update", runId, eventId, label} | FollowUpMeta {kind: "follow_up",
    # runId, eventId} | null.
    metadata = models.JSONField(null=True)
    # Set on follow-up notices.
    run_update_event = models.ForeignKey(Event, models.SET_NULL, null=True, related_name="+")

    user_id: str
    conversation_id: uuid.UUID
    run_update_event_id: int | None

    class Meta(UserOwned.Meta):
        db_table = "messages"
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "message_id"], name="messages_conv_msg"
            ),
            models.UniqueConstraint(
                fields=["conversation", "run_update_event"], name="messages_one_notice_per_event"
            ),
            *choices_checks("messages", role=Role),
        ]

    def __str__(self) -> str:
        return f"{self.role} {self.message_id}"
