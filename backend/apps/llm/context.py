"""Who a model call is for (design §8.8): the ledger's attribution.

Agent loops call the model several frames below our code, so attribution cannot be threaded
through parameters. A `ContextVar` carries it instead, the Python twin of the AsyncLocalStorage
in `src/lib/llm/context.ts`:

    with llm_context(user_id=run.user_id, run_id=run.id, conversation_id=run.conversation_id):
        await step(run)

Setters: the chat generator (inside the generator, never in the view body), `run_step`,
follow-ups, and the scripts (`label="smoke"`/`"bakeoff"`). The cron refresh sets nothing: its
rows are system rows with no user. Fakes are labelled by the model stack itself (§8.9).

pydantic-ai copies the context into tool tasks and worker threads, asyncio copies it into new
tasks, and `db()` threads inherit it through `sync_to_async`. A block replaces the context; it
does not merge with an outer one.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

__all__ = ["LlmContext", "current_llm_context", "llm_context"]

type Id = str | uuid.UUID

# `apps.llm.models.Label`: the column has a CHECK constraint, so a bad label would lose the row.
LABELS = ("fake", "smoke", "bakeoff")


@dataclass(frozen=True, slots=True)
class LlmContext:
    user_id: str | None = None
    run_id: Id | None = None
    conversation_id: Id | None = None
    label: str | None = None  # "smoke", "bakeoff" or "fake" (apps.llm.models.Label)


_NONE = LlmContext()  # frozen: safe to share
_CONTEXT: ContextVar[LlmContext] = ContextVar("foray_llm_context", default=_NONE)


@contextmanager
def llm_context(
    *,
    user_id: str | None = None,
    run_id: Id | None = None,
    conversation_id: Id | None = None,
    label: str | None = None,
) -> Iterator[LlmContext]:
    """Attribute every model request made inside the block."""
    if label and label not in LABELS:
        raise ValueError(f"Not a ledger label: {label!r}. Use one of {', '.join(LABELS)}.")
    context = LlmContext(
        user_id=user_id or None,
        run_id=run_id or None,
        conversation_id=conversation_id or None,
        label=label or None,
    )
    token = _CONTEXT.set(context)
    try:
        yield context
    finally:
        _CONTEXT.reset(token)


def current_llm_context() -> LlmContext:
    return _CONTEXT.get()
