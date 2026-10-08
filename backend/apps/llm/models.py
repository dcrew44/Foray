"""The model-call ledger (design §2.2, §8.5, R10).

One row per model request, whatever happened: success, vendor error, timeout, client cancel,
stream. `cost_usd` is frozen at call time (rates change, rows should not). Rows outlive their
user (`SET NULL`), and system rows from the cron have no user. `input_tokens` keeps today's
meaning: uncached plus cache-write tokens; `cached_input_tokens` are cache reads.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from django.db import models
from django.db.models import F
from django.utils import timezone

from apps.accounts.models import User
from apps.chat.models import Conversation
from apps.core.choices import choices_checks
from apps.core.ids import new_id
from apps.core.scoping import UserOwned
from apps.runs.models import Run
from foray.env import PURPOSES


class Purpose(models.TextChoices):
    """The LLM purposes of `foray.env.PURPOSES` (§8.1)."""

    CHAT = "chat"
    EXTRACT = "extract"
    SCORE = "score"
    EVALUATE = "evaluate"
    TAILOR = "tailor"
    VERIFY = "verify"
    MERGE = "merge"


if tuple(Purpose.values) != PURPOSES:  # pragma: no cover - a guard for whoever adds a purpose
    raise RuntimeError("apps.llm.models.Purpose must list foray.env.PURPOSES in order")


class Label(models.TextChoices):
    """Marks calls that are not product work."""

    FAKE = "fake"
    SMOKE = "smoke"
    BAKEOFF = "bakeoff"


class LlmCall(UserOwned):
    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    # Indexed by the (user, created_at) index.
    user = models.ForeignKey(
        User,
        models.SET_NULL,
        null=True,
        related_name="llm_calls",
        db_index=False,
    )
    run = models.ForeignKey(Run, models.SET_NULL, null=True, related_name="llm_calls")
    # New: chat cost per chat.
    conversation = models.ForeignKey(
        Conversation, models.SET_NULL, null=True, related_name="llm_calls"
    )
    purpose = models.TextField(choices=Purpose.choices)
    provider = models.TextField()  # the first segment of the `provider:model` ref
    model = models.TextField()  # the rest of the ref
    input_tokens = models.IntegerField(default=0)  # uncached + cache-write
    output_tokens = models.IntegerField(default=0)
    cached_input_tokens = models.IntegerField(default=0)  # cache reads
    reasoning_tokens = models.IntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=12, decimal_places=6, default=Decimal(0))
    latency_ms = models.IntegerField(null=True)
    error = models.TextField(null=True)  # noqa: DJ001 (failures are costed and counted too)
    label = models.TextField(choices=Label.choices, null=True)  # noqa: DJ001 (new)
    created_at = models.DateTimeField(default=timezone.now)

    user_id: str | None
    run_id: uuid.UUID | None
    conversation_id: uuid.UUID | None

    class Meta(UserOwned.Meta):
        db_table = "llm_calls"
        constraints = [*choices_checks("llm_calls", purpose=Purpose, label=Label)]
        indexes = [
            # `run` and `conversation` are indexed by their foreign keys.
            models.Index(F("user"), F("created_at").desc(), name="llm_calls_user_created"),
            models.Index(fields=["created_at"], name="llm_calls_created"),
            models.Index(fields=["purpose", "model"], name="llm_calls_purpose_model"),
        ]

    def __str__(self) -> str:
        return f"{self.purpose} {self.provider}:{self.model} ${self.cost_usd}"
