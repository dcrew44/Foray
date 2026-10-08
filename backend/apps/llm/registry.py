"""Purpose in, model out (design §8.1, §8.3). Stub: step 3B builds the providers and wrappers.

Call sites ask for what they are doing, `model("score")`, never for a vendor:

    model(purpose) = BudgetGuard(RateLimited(LedgerModel(PurposeSettings(base_model(ref)))))

`model()` is cached per process in `_MODELS`. `tests/fakes.py` swaps `model_ref`/`base_model`
and gives the cache a fresh dict for the duration of a test.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.conf import settings

from foray.env import PURPOSES, Purpose

if TYPE_CHECKING:
    from pydantic_ai.models import Model

__all__ = ["PURPOSES", "Purpose", "base_model", "model", "model_ref"]

_MODELS: dict[Purpose, Model] = {}


def model_ref(purpose: Purpose) -> str:
    """`LLM_<PURPOSE>`, else `LLM_DEFAULT`, as `provider:model`. Validated at boot (§1.3)."""
    return settings.ENV.model_ref(purpose)


def base_model(ref: str) -> Model:
    """The bare provider model for `provider:model`, built with explicit keys from `ENV`."""
    raise NotImplementedError(f"apps.llm.registry.base_model({ref!r}) is built in step 3B")


def model(purpose: Purpose) -> Model:
    """The full wrapped stack for a purpose, cached per process."""
    cached = _MODELS.get(purpose)
    if cached is None:
        raise NotImplementedError(f"apps.llm.registry.model({purpose!r}) is built in step 3B")
    return cached
