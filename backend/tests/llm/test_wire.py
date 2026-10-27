"""Every cell of the reasoning map (design §8.2), as it reaches the wire.

The real pydantic-ai model classes run against a mock httpx2 transport that records each request
body (`research/llm-library.md` §3). The DeepSeek row matters most: pydantic-ai silently drops the
unified `thinking` setting for DeepSeek, which would leave Flash reasoning on.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from pydantic_ai import Agent
from pydantic_ai.direct import model_request
from pydantic_ai.messages import ModelRequest

from apps.llm import reasoning, registry
from apps.llm.registry import PurposeSettings
from apps.llm.structured import structured
from foray.env import Effort
from tests.llm.vendors import Answer, Vendor, patch_base_models

EFFORTS: list[Effort] = ["none", "minimal", "low", "medium", "high", "xhigh"]


async def send(ref: str, effort: Effort, **call_settings: Any) -> dict[str, Any]:
    """One structured agent call through `PurposeSettings`; the request body the vendor got."""
    vendor = Vendor(ref)
    agent = Agent(
        PurposeSettings(vendor.base_model(), ref=ref, effort=effort),
        output_type=Answer,
        instructions="Answer factual questions.",
    )
    result = await agent.run("Which city?", model_settings=cast(Any, call_settings) or None)
    assert result.output == Answer(city="San Francisco", confident=True)
    return vendor.body


# --- OpenAI (Responses API) ----------------------------------------------------------------------

OPENAI = {"none": "none", "minimal": "low", "low": "low", "medium": "medium", "high": "high"}
OPENAI["xhigh"] = "xhigh"


@pytest.mark.parametrize("effort", EFFORTS)
@pytest.mark.parametrize(
    "ref", ["openai:gpt-5.6-luna", "openai:gpt-6-luna", "openai:gpt-6.1-terra"]
)
async def test_openai(ref: str, effort: Effort) -> None:
    body = await send(ref, effort)
    assert body["reasoning"]["effort"] == OPENAI[effort]


async def test_an_openai_model_pydantic_ai_does_not_know_borrows_a_profile() -> None:
    """pydantic-ai 2.54 has no profile for gpt-6.1-terra and would treat it as a non-reasoning
    model, sending no `reasoning` at all; it borrows gpt-6-luna's reasoning switches instead."""
    from pydantic_ai.models.openai import OpenAIResponsesModel
    from pydantic_ai.providers.openai import OpenAIProvider

    bare = OpenAIResponsesModel("gpt-6.1-terra", provider=OpenAIProvider(api_key="x"))
    assert not bare.profile.get("openai_supports_reasoning")
    model = registry.build_base_model("openai:gpt-6.1-terra")
    assert model.profile.get("openai_supports_reasoning")
    assert model.profile.get("openai_supports_reasoning_effort_none")
    assert reasoning.openai_profile("gpt-6-luna") is None  # known ones keep their own


async def test_openai_minimal_sends_low() -> None:
    """GPT-5.6 has no `minimal`; pydantic-ai deliberately sends `low`."""
    body = await send("openai:gpt-5.6-luna", "minimal")
    assert body["reasoning"]["effort"] == "low"


async def test_openai_uses_the_responses_api() -> None:
    vendor = Vendor("openai:gpt-5.6-luna")
    agent = Agent(
        PurposeSettings(vendor.base_model(), ref=vendor.ref, effort="low"), output_type=Answer
    )
    await agent.run("Which city?")
    assert vendor.urls == ["https://api.openai.com/v1/responses"]


# --- DeepSeek (the critical row) -----------------------------------------------------------------

DEEPSEEK = {"minimal": "low", "low": "low", "medium": "high", "high": "high", "xhigh": "max"}


@pytest.mark.parametrize("effort", EFFORTS)
async def test_deepseek(effort: Effort) -> None:
    body = await send("deepseek:deepseek-flash", effort)
    if effort == "none":
        assert body["thinking"] == {"type": "disabled"}
        assert "reasoning_effort" not in body
        assert body["tool_choice"] == "required"  # forcing the output tool keeps a score reliable
    else:
        assert body["thinking"] == {"type": "enabled"}
        assert body["reasoning_effort"] == DEEPSEEK[effort]
        # "Thinking mode does not support this tool_choice": while it thinks, DeepSeek takes the
        # output tool only when offered, which pydantic-ai knows for `deepseek-v4-*` ids alone.
        assert body["tool_choice"] == "auto"


async def test_deepseek_none_produces_thinking_disabled() -> None:
    body = await send("deepseek:deepseek-flash", "none")
    assert body["thinking"] == {"type": "disabled"}


async def test_deepseek_without_the_map_sends_nothing() -> None:
    """Why the map exists: the unified setting alone never reaches DeepSeek."""
    vendor = Vendor("deepseek:deepseek-flash")
    agent = Agent(vendor.base_model(), output_type=Answer)
    await agent.run("Which city?", model_settings={"thinking": False})
    assert "thinking" not in vendor.body
    assert "reasoning_effort" not in vendor.body


async def test_deepseek_keeps_thinking_when_the_caller_adds_extra_body() -> None:
    body = await send("deepseek:deepseek-flash", "none", extra_body={"user": "x"})
    assert body["thinking"] == {"type": "disabled"}
    assert body["user"] == "x"


async def test_direct_requests_get_the_purpose_settings() -> None:
    """`pydantic_ai.direct` ignores `Model.settings`, so `PurposeSettings` merges in request()."""
    vendor = Vendor("deepseek:deepseek-flash")
    model = PurposeSettings(vendor.base_model(), ref=vendor.ref, effort="none")
    await model_request(model, [ModelRequest.user_text_prompt("Which city?")])
    assert vendor.body["thinking"] == {"type": "disabled"}


# --- Anthropic -----------------------------------------------------------------------------------

HAIKU_BUDGET = {"minimal": 1024, "low": 2048, "medium": 10000, "high": 16384, "xhigh": 32768}


@pytest.mark.parametrize("effort", EFFORTS)
async def test_anthropic_budget_model(effort: Effort) -> None:
    body = await send("anthropic:claude-haiku-4-5", effort)
    if effort == "none":
        assert "thinking" not in body  # Haiku's default is off
    else:
        assert body["thinking"] == {"type": "enabled", "budget_tokens": HAIKU_BUDGET[effort]}


@pytest.mark.parametrize("effort", EFFORTS)
async def test_anthropic_adaptive_model(effort: Effort) -> None:
    body = await send("anthropic:claude-sonnet-5", effort)
    if effort == "none":
        assert body["thinking"] == {"type": "disabled"}
    else:
        assert body["thinking"]["type"] == "adaptive"
        expected = "low" if effort == "minimal" else effort
        assert body["output_config"]["effort"] == expected


async def test_anthropic_haiku_drops_temperature_when_thinking() -> None:
    """pydantic-ai still sends `temperature` to Haiku 4.5 with thinking on; the API rejects it."""
    assert "temperature" not in await send("anthropic:claude-haiku-4-5", "low", temperature=0.2)
    assert (await send("anthropic:claude-haiku-4-5", "none", temperature=0.2))["temperature"] == 0.2
    assert reasoning.quirks_for("anthropic:claude-haiku-4-5").drops_temperature_when_thinking
    assert not reasoning.quirks_for("openai:gpt-5.6-luna").drops_temperature_when_thinking


# --- Google --------------------------------------------------------------------------------------

GEMINI_LEVEL = {
    "none": "LOW",  # the model's minimum level
    "minimal": "LOW",
    "low": "LOW",
    "medium": "MEDIUM",
    "high": "HIGH",
    "xhigh": "HIGH",
}


@pytest.mark.parametrize("effort", EFFORTS)
async def test_google(effort: Effort) -> None:
    body = await send("google:gemini-3.8-flash", effort)
    config = body["generationConfig"]["thinkingConfig"]
    assert config["thinking_level"] == GEMINI_LEVEL[effort]
    if effort != "none":
        assert config["include_thoughts"] is True


def test_google_retries_three_times() -> None:
    """google-genai retries nothing by default; the AI SDK gave Gemini two retries."""
    model = Vendor("google:gemini-3.8-flash").base_model()
    options = model.client._api_client._http_options.retry_options
    assert options is not None and options.attempts == 3


# --- compat --------------------------------------------------------------------------------------


@pytest.mark.parametrize("effort", EFFORTS)
async def test_compat_sends_no_reasoning(effort: Effort, env: Any) -> None:
    env(llm_compat_base_url="http://compat.test/v1")
    vendor = Vendor("compat:some-model")
    agent = Agent(
        PurposeSettings(vendor.base_model(), ref=vendor.ref, effort=effort), output_type=Answer
    )
    await agent.run("Which city?")
    assert vendor.urls == ["http://compat.test/v1/chat/completions"]
    for key in ("reasoning_effort", "thinking", "reasoning"):
        assert key not in vendor.body


# --- the whole stack -----------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
async def test_stack_uses_the_purpose_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """score defaults to `minimal`, which reaches GPT-5.6 as `low`; evaluate is `medium`."""
    vendors = {ref: Vendor(ref) for ref in ("openai:gpt-5.6-luna", "openai:gpt-5.6-terra")}
    patch_base_models(monkeypatch, vendors)
    await structured("score", Answer, instructions="Answer.", prompt="Which city?")
    await structured("evaluate", Answer, instructions="Answer.", prompt="Which city?")
    assert vendors["openai:gpt-5.6-luna"].body["reasoning"]["effort"] == "low"
    assert vendors["openai:gpt-5.6-terra"].body["reasoning"]["effort"] == "medium"


@pytest.mark.django_db(transaction=True)
async def test_stack_honours_the_env_override(monkeypatch: pytest.MonkeyPatch, env: Any) -> None:
    env(llm_reasoning_score="none", llm_score="deepseek:deepseek-flash")
    vendors = {"deepseek:deepseek-flash": Vendor("deepseek:deepseek-flash")}
    patch_base_models(monkeypatch, vendors)
    assert registry.model_ref("score") == "deepseek:deepseek-flash"
    await structured("score", Answer, instructions="Answer.", prompt="Which city?")
    assert vendors["deepseek:deepseek-flash"].body["thinking"] == {"type": "disabled"}


def test_settings_for_every_cell() -> None:
    """The map itself, cell by cell (the tests above prove each cell on the wire)."""
    assert reasoning.settings_for("openai:x", "none") == {"thinking": False}
    assert reasoning.settings_for("anthropic:x", "minimal") == {"thinking": "minimal"}
    assert reasoning.settings_for("google:x", "xhigh") == {"thinking": "xhigh"}
    assert reasoning.settings_for("deepseek:x", "none") == {
        "extra_body": {"thinking": {"type": "disabled"}}
    }
    assert reasoning.settings_for("deepseek:x", "xhigh") == {
        "extra_body": {"thinking": {"type": "enabled"}},
        "openai_reasoning_effort": "max",
    }
    assert reasoning.settings_for("compat:x", "high") == {}
    assert reasoning.settings_for("fake:score", "high") == {}


def test_purpose_settings_are_exposed_on_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agents read `Model.settings`; every wrapper of the stack forwards it."""
    ref = "deepseek:deepseek-flash"
    patch_base_models(monkeypatch, {ref: Vendor(ref)})
    stack = registry.build_stack("score", ref, effort="none")
    assert stack.settings == {"extra_body": {"thinking": {"type": "disabled"}}}


def test_effort_defaults() -> None:
    assert reasoning.DEFAULT_EFFORT == {
        "chat": "low",
        "extract": "low",
        "score": "minimal",
        "evaluate": "medium",
        "tailor": "high",
        "verify": "low",
        "merge": "low",
    }
