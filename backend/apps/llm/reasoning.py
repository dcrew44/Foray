"""How hard the model thinks, per purpose, and every other vendor quirk (design §8.2).

This is the only module that knows a vendor's oddities. Call sites ask for a purpose; changing
how much a purpose reasons is an `.env` edit (`LLM_REASONING_<PURPOSE>`), and working around a
provider is a change here.

Effort defaults (`map/llm.md` §3): tailoring gets the most because a fabricated bullet is the
worst failure in the product; the bulk passes get the least because they run hundreds of times.
Reasoning is the single largest cost lever: on DeepSeek Flash, 92% of a scoring call's output
tokens were reasoning (2,085 of 2,255).

| Effort | openai / anthropic / google | deepseek | compat |
|---|---|---|---|
| none | `thinking=False` (OpenAI: `effort: "none"`) | `thinking: {type: disabled}` | nothing |
| minimal | `thinking="minimal"` (GPT-5.6: `low`) | enabled, effort `low` | nothing |
| low ... xhigh | `thinking=<effort>` | enabled, `low`/`high`/`high`/`max` | nothing |

GPT-5.6 and GPT-6 have no `minimal`, so pydantic-ai deliberately sends `low` for them.
DeepSeek is the critical row: pydantic-ai silently drops the unified `thinking` setting for it,
which would leave Flash reasoning on. So it gets the raw `thinking` body field plus
`openai_reasoning_effort`, mirroring the AI SDK's map. `llm/test_wire.py` pins every cell on the
wire.

pydantic-ai also has to know a model by name. An OpenAI model missing from its profiles is
treated as a non-reasoning model, and the `thinking` setting and prompt caching are dropped
without a word; `openai_profile()` fills that in. DeepSeek rejects a forced tool call while it
thinks, and pydantic-ai only knows that for ids starting `deepseek-v4-`, which the current
`deepseek-flash` does not; `deepseek_profile()` says so for every DeepSeek model.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from django.conf import settings
from pydantic_ai.profiles import ModelProfile

from foray.env import Effort, Purpose

__all__ = [
    "DEFAULT_EFFORT",
    "Quirks",
    "apply_quirks",
    "deepseek_profile",
    "effort_for",
    "openai_profile",
    "quirks_for",
    "settings_for",
]

DEFAULT_EFFORT: dict[Purpose, Effort] = {
    "chat": "low",
    "extract": "low",
    "score": "minimal",
    "evaluate": "medium",
    "tailor": "high",
    "verify": "low",
    "merge": "low",
}

UNIFIED_THINKING = frozenset({"openai", "anthropic", "google"})

# The AI SDK's DeepSeek map (deepseek dist/index.js:840-868).
DEEPSEEK_EFFORT: dict[Effort, str] = {
    "minimal": "low",
    "low": "low",
    "medium": "high",
    "high": "high",
    "xhigh": "max",
}


def effort_for(purpose: Purpose) -> Effort:
    """`LLM_REASONING_<PURPOSE>`, else the default. Validated at boot (§1.3)."""
    return settings.ENV.reasoning_override(purpose) or DEFAULT_EFFORT[purpose]


def settings_for(ref: str, effort: Effort) -> dict[str, Any]:
    """The model settings that carry `effort` to the vendor of `ref`."""
    provider = ref.split(":", 1)[0]
    if provider in UNIFIED_THINKING:
        return {"thinking": False if effort == "none" else effort}
    if provider == "deepseek":
        if effort == "none":
            return {"extra_body": {"thinking": {"type": "disabled"}}}
        return {
            "extra_body": {"thinking": {"type": "enabled"}},
            "openai_reasoning_effort": DEEPSEEK_EFFORT[effort],
        }
    return {}  # compat (a flag could send openai_reasoning_effort), and the fakes


# OpenAI models pydantic-ai 2.54 has no profile for, each with a sibling that takes the same
# reasoning efforts (gpt-6.1-terra: none, low ... max, default medium, no minimal; see
# developers.openai.com/api/docs/models/gpt-6.1-terra).
OPENAI_PROFILE_OF: dict[str, str] = {"gpt-6.1-terra": "gpt-6-luna"}

# What a model borrows from its sibling: the reasoning switches and prompt caching, nothing that
# changes what is sent beyond those (`reasoning.context` and `phase` are left at the defaults).
_BORROWED = (
    "supports_thinking",
    "thinking_enabled_by_default",
    "openai_supports_reasoning",
    "openai_supports_reasoning_effort_none",
    "openai_supports_minimal_reasoning_effort",
    "openai_supports_encrypted_reasoning_content",
    "openai_supports_prompt_cache_breakpoints",
    "default_cache_retention",
)


def openai_profile(name: str) -> ModelProfile | None:
    """The profile keys an OpenAI model needs and pydantic-ai lacks; None for those it knows."""
    sibling = OPENAI_PROFILE_OF.get(name)
    if sibling is None:
        return None
    from pydantic_ai.providers.openai import OpenAIProvider

    known: dict[str, Any] = dict(OpenAIProvider.model_profile(sibling) or {})
    return cast(ModelProfile, {key: known[key] for key in _BORROWED if key in known})


def deepseek_profile(name: str) -> ModelProfile:
    """DeepSeek answers a forced tool choice with "Thinking mode does not support this
    tool_choice" whenever thinking is on, and structured output is a forced call to the output
    tool. With this, pydantic-ai offers the tool without forcing it while the model thinks (our
    explicit `openai_reasoning_effort` is how it can tell), and still forces it when thinking is
    off, where forcing is what keeps a fast score reliable."""
    return cast(ModelProfile, {"supports_forced_tool_choice_with_thinking": False})


@dataclass(frozen=True, slots=True)
class Quirks:
    # Anthropic's reasoning-capable models reject `temperature` alongside extended thinking
    # rather than ignoring it. pydantic-ai drops it for Sonnet 5 but still sends it for
    # Haiku 4.5 (`research/llm-library.md` §3).
    drops_temperature_when_thinking: bool = False


_ANTHROPIC_REASONERS = re.compile(r"sonnet-5|opus-5|haiku-4-5")


def quirks_for(ref: str) -> Quirks:
    """The quirks of the ref actually called (TS took them from the configured ref even when a
    call overrode the model)."""
    provider, _, name = ref.partition(":")
    return Quirks(
        drops_temperature_when_thinking=(
            provider == "anthropic" and _ANTHROPIC_REASONERS.search(name) is not None
        )
    )


def _thinking_on(model_settings: Mapping[str, Any]) -> bool:
    thinking = model_settings.get("thinking")
    return thinking is not None and thinking is not False


def apply_quirks(ref: str, model_settings: dict[str, Any]) -> dict[str, Any]:
    quirks = quirks_for(ref)
    if quirks.drops_temperature_when_thinking and _thinking_on(model_settings):
        model_settings.pop("temperature", None)
    return model_settings


def merge_settings(
    purpose_settings: Mapping[str, Any], call_settings: Mapping[str, Any] | None
) -> dict[str, Any]:
    """`{**purpose, **call}`, except that `extra_body` merges key by key, so a caller's extra body
    fields never drop DeepSeek's `thinking` switch."""
    merged: dict[str, Any] = {**purpose_settings, **(call_settings or {})}
    purpose_body = purpose_settings.get("extra_body")
    call_body = (call_settings or {}).get("extra_body")
    if isinstance(purpose_body, Mapping) and isinstance(call_body, Mapping):
        merged["extra_body"] = {**purpose_body, **call_body}
    return merged
