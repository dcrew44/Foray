"""Purpose in, model out (design §8.1, §8.3).

Call sites ask for what they are doing, `model("score")`, never for a vendor. Changing which
model scores postings is an `.env` edit; vendor quirks live in `reasoning.py`.

    model(purpose) = BudgetGuard(RateLimited(LedgerModel(PurposeSettings(base_model(ref)))))

- All four wrappers are `WrapperModel`s on the model, so agent-internal calls, nested agents and
  streams all pass through them (invariant 10).
- `PurposeSettings` carries the purpose's reasoning settings both as `Model.settings` (which
  agents honour) and merged inside `request()`/`request_stream()` (which `pydantic_ai.direct`
  calls need).
- The ledger sits inside the limiter, so `latency_ms` measures the provider, not the queue.
- A purpose never falls back to another vendor: `model_ref` is `LLM_<PURPOSE>`, else
  `LLM_DEFAULT`, validated at boot (§1.3). With `LLM_FAKE` every purpose resolves to
  `fake:<purpose>` (§8.9) and its ledger rows are labelled `fake`.

`model()` is cached per process in `_MODELS`. `model_ref`, `base_model` and `_MODELS` are
module globals looked up at call time: `tests/fakes.py` swaps them for a test.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

from django.conf import settings
from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings

from apps.llm import reasoning
from apps.llm.budget import BudgetGuard
from apps.llm.ledger import LedgerModel
from apps.llm.limiter import RateLimited
from apps.llm.pricing import FAKE_PROVIDER
from foray.env import PROVIDER_CREDENTIAL, PURPOSES, Effort, Purpose

__all__ = [
    "PROVIDERS",
    "PURPOSES",
    "ModelNotConfiguredError",
    "Purpose",
    "PurposeSettings",
    "base_model",
    "build_base_model",
    "build_stack",
    "configured_providers",
    "configured_purposes",
    "model",
    "model_ref",
]

# The order the settings page and `llm_smoke` list them in (registry.ts).
PROVIDERS = ("openai", "google", "anthropic", "deepseek", "compat")

_MODELS: dict[Purpose, Model] = {}


class ModelNotConfiguredError(Exception):
    """A ref names an unknown provider or one whose credential is not set."""


def model_ref(purpose: Purpose) -> str:
    """`LLM_<PURPOSE>`, else `LLM_DEFAULT`, as `provider:model`; `fake:<purpose>` under
    `LLM_FAKE`."""
    env = settings.ENV
    if env.llm_fake:
        return f"{FAKE_PROVIDER}:{purpose}"
    return env.model_ref(purpose)


def split_ref(ref: str) -> tuple[str, str]:
    provider, sep, name = ref.partition(":")
    if not sep or not provider or not name:
        raise ModelNotConfiguredError(f'"{ref}" is not a "provider:model" reference.')
    return provider, name


def provider_of(ref: str) -> str:
    return split_ref(ref)[0]


def _key(provider: str) -> str:
    env = settings.ENV
    attr = PROVIDER_CREDENTIAL[provider].lower()
    value = getattr(env, attr)
    if value is None:
        raise ModelNotConfiguredError(
            f"{provider} needs {PROVIDER_CREDENTIAL[provider]}, which is not set. Falling back to "
            "another vendor would spend money somewhere you did not choose."
        )
    return value if isinstance(value, str) else value.get_secret_value()


def build_base_model(ref: str, *, http_client: Any = None) -> Model:
    """The bare provider model for `provider:model`, built with explicit keys from `ENV`
    (pydantic-ai's own env lookup would miss `GOOGLE_GENERATIVE_AI_API_KEY`). `http_client` is
    for tests: an `httpx2.AsyncClient` over a mock transport."""
    provider, name = split_ref(ref)
    match provider:
        case "openai":
            from pydantic_ai.models.openai import OpenAIResponsesModel
            from pydantic_ai.providers.openai import OpenAIProvider

            # `openai:` is the Responses API, as the AI SDK uses.
            return OpenAIResponsesModel(
                name,
                provider=OpenAIProvider(api_key=_key("openai"), http_client=http_client),
                profile=reasoning.openai_profile(name),
            )
        case "deepseek":
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.deepseek import DeepSeekProvider

            return OpenAIChatModel(
                name,
                provider=DeepSeekProvider(api_key=_key("deepseek"), http_client=http_client),
                profile=reasoning.deepseek_profile(name),
            )
        case "anthropic":
            from pydantic_ai.models.anthropic import AnthropicModel
            from pydantic_ai.providers.anthropic import AnthropicProvider

            return AnthropicModel(
                name,
                provider=AnthropicProvider(api_key=_key("anthropic"), http_client=http_client),
                profile=reasoning.anthropic_profile(name),
            )
        case "google":
            from google.genai.types import HttpRetryOptions
            from pydantic_ai.models.google import GoogleModel
            from pydantic_ai.providers.google import GoogleProvider

            # google-genai retries nothing by default; the AI SDK gave Gemini two retries.
            return GoogleModel(
                name,
                provider=GoogleProvider(
                    api_key=_key("google"),
                    http_client=http_client,
                    retry_options=HttpRetryOptions(attempts=3),
                ),
            )
        case "compat":
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.openai import OpenAIProvider

            base_url = _key("compat")  # LLM_COMPAT_BASE_URL makes the provider usable
            api_key = settings.ENV.llm_compat_api_key
            return OpenAIChatModel(
                name,
                provider=OpenAIProvider(
                    base_url=base_url,
                    # A local server may need no key; never let the SDK read OPENAI_API_KEY.
                    api_key=api_key.get_secret_value() if api_key else "not-set",
                    http_client=http_client,
                ),
            )
        case "fake" if settings.ENV.debug:  # LLM_FAKE requires DEBUG (§1.3)
            from apps.llm import fake

            return fake.fake_model(cast(Purpose, name))
        case _:
            raise ModelNotConfiguredError(
                f'"{ref}" names the unknown provider "{provider}". '
                f"Use one of {', '.join(PROVIDERS)}."
            )


def base_model(ref: str) -> Model:
    """The bare provider model for `provider:model`."""
    return build_base_model(ref)


class PurposeSettings(WrapperModel):
    """The purpose's reasoning settings (§8.2), plus the actual ref's quirks."""

    def __init__(self, wrapped: Model, *, ref: str, effort: Effort):
        super().__init__(wrapped)
        self.ref = ref
        self.effort: Effort = effort
        self.purpose_settings: dict[str, Any] = reasoning.settings_for(ref, effort)

    @property
    def settings(self) -> ModelSettings | None:
        merged = reasoning.merge_settings(dict(self.wrapped.settings or {}), self.purpose_settings)
        return cast(ModelSettings, merged) if merged else None

    def merged(self, model_settings: ModelSettings | None) -> ModelSettings | None:
        merged = reasoning.merge_settings(self.purpose_settings, cast(Any, model_settings))
        merged = reasoning.apply_quirks(self.ref, merged)
        return cast(ModelSettings, merged) if merged else None

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        return await self.wrapped.request(
            messages, self.merged(model_settings), model_request_parameters
        )

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        async with self.wrapped.request_stream(
            messages, self.merged(model_settings), model_request_parameters, run_context
        ) as stream:
            yield stream


def build_stack(
    purpose: Purpose, ref: str, *, effort: Effort | None = None, label: str | None = None
) -> Model:
    """The full wrapped stack over `base_model(ref)`. `llm_bakeoff` uses it for explicit refs.
    Under `LLM_FAKE` a fake ref's rows are labelled `fake`, whatever the context says."""
    if label is None and settings.ENV.llm_fake and provider_of(ref) == FAKE_PROVIDER:
        label = "fake"
    base = base_model(ref)  # a module global: tests/fakes.py swaps it
    settings_layer = PurposeSettings(base, ref=ref, effort=effort or reasoning.effort_for(purpose))
    ledger = LedgerModel(settings_layer, purpose=purpose, ref=ref, label=label)
    return BudgetGuard(RateLimited(ledger, provider=provider_of(ref)))


def model(purpose: Purpose) -> Model:
    """The full wrapped stack for a purpose, cached per process."""
    if purpose not in PURPOSES:
        raise ValueError(f"Not an LLM purpose: {purpose!r}")
    cached = _MODELS.get(purpose)
    if cached is None:
        cached = _MODELS[purpose] = build_stack(purpose, model_ref(purpose))
    return cached


def configured_providers() -> list[str]:
    """Providers whose credential is set, in the fixed order."""
    return [p for p in PROVIDERS if settings.ENV.provider_configured(p)]


@dataclass(frozen=True, slots=True)
class ConfiguredPurpose:
    purpose: Purpose
    ref: str | None
    error: str | None


def configured_purposes() -> list[ConfiguredPurpose]:
    """Which purposes are usable right now, for `llm_smoke` and the settings page."""
    found: list[ConfiguredPurpose] = []
    for purpose in PURPOSES:
        try:
            ref = model_ref(purpose)
            provider = provider_of(ref)
            if provider != FAKE_PROVIDER:
                if provider not in PROVIDER_CREDENTIAL:
                    raise ModelNotConfiguredError(
                        f'"{ref}" names the unknown provider "{provider}".'
                    )
                _key(provider)
            found.append(ConfiguredPurpose(purpose, ref, None))
        except ModelNotConfiguredError as exc:
            found.append(
                ConfiguredPurpose(
                    purpose, None, f'No model configured for purpose "{purpose}": {exc}'
                )
            )
    return found
