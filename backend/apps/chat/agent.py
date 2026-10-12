"""The chat agent, built per turn around one user (design §7.1; `src/lib/agents/chat-agent.ts`).

- **Model:** `model("chat")`, the wrapped stack of §8.3, so chat now gets the purpose's reasoning
  setting, the rate limiter and the budget guard (the TS agent bypassed both).
- **Instructions:** `prompt.system_prompt(...)`, built by the caller from the user's stored state.
- **Tools:** the 16 of `tools.CHAT_TOOLSET`, registered once at import. The user comes from
  `RunContext.deps` (`ChatDeps`), so the model has no way to name another user.
- **Read-only follow-ups:** `FollowUpTools` offers only `FOLLOW_UP_TOOLS` when `deps.follow_up`.
  Every tool stays registered, so history that used the others still converts.
- **Step cap:** `StepCap(n)` hides every tool on the last allowed request, which forces a final
  text answer instead of ending on a bare tool result; `UsageLimits(request_limit=n)` is the hard
  backstop. `n` is 8 for chat and 4 for follow-ups (§7.8).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Final

from pydantic_ai import Agent, RunContext, UsageLimits
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset

from apps.chat.tools import CHAT_TOOLSET, FOLLOW_UP_TOOLS, TOOL_RETRIES
from apps.llm.registry import model

__all__ = [
    "CHAT_STEPS",
    "FOLLOW_UP_STEPS",
    "ChatDeps",
    "FollowUpTools",
    "StepCap",
    "build_agent",
    "usage_limits",
]

CHAT_STEPS: Final = 8
FOLLOW_UP_STEPS: Final = 4


@dataclass(frozen=True, slots=True)
class ChatDeps:
    """Who the turn is for. Tools read the user from here, never from the model."""

    user_id: str
    conversation_id: uuid.UUID | None = None
    # A follow-up turn: the agent reporting back on a run, with no user message to answer. It may
    # only read (FOLLOW_UP_TOOLS): it must not start, approve or change anything the user has not
    # asked for in their own words.
    follow_up: bool = False


@dataclass
class StepCap(AbstractCapability[ChatDeps]):
    """The graceful step cap (research/llm-library.md §7): on the last allowed request no tool is
    offered, so the model answers in text. pydantic-ai would otherwise raise
    `UsageLimitExceeded`, and the AI SDK's `isStepCount` could end on a tool result with no
    text."""

    steps: int

    async def prepare_tools(
        self, ctx: RunContext[ChatDeps], tool_defs: list[ToolDefinition]
    ) -> list[ToolDefinition]:
        return [] if ctx.usage.requests >= self.steps - 1 else tool_defs


@dataclass
class FollowUpTools(AbstractCapability[ChatDeps]):
    """A follow-up reports and asks: it is offered the read-only tools only. Filtering here also
    blocks execution, so a hidden tool cannot be called either."""

    async def prepare_tools(
        self, ctx: RunContext[ChatDeps], tool_defs: list[ToolDefinition]
    ) -> list[ToolDefinition]:
        if not ctx.deps.follow_up:
            return tool_defs
        return [tool for tool in tool_defs if tool.name in FOLLOW_UP_TOOLS]


def steps_for(follow_up: bool) -> int:
    return FOLLOW_UP_STEPS if follow_up else CHAT_STEPS


def usage_limits(follow_up: bool) -> UsageLimits:
    """The hard backstop behind `StepCap`."""
    return UsageLimits(request_limit=steps_for(follow_up))


def build_agent(
    instructions: str,
    *,
    follow_up: bool = False,
    toolset: AbstractToolset[Any] = CHAT_TOOLSET,
) -> Agent[ChatDeps, str]:
    """One turn's agent. The model stack is cached per process (`registry.model`); the agent is
    cheap to build and carries this turn's instructions. `toolset` is for tests that replay a
    scripted turn against stand-in tools."""
    agent: Agent[ChatDeps, Any] = Agent(
        model("chat"),
        instructions=instructions,
        deps_type=ChatDeps,
        toolsets=[toolset],
        capabilities=[FollowUpTools(), StepCap(steps_for(follow_up))],
        retries={"tools": TOOL_RETRIES},
    )
    return agent
