"""Fake models for tests (design §12.1).

`fake_models(monkeypatch, {"score": FunctionModel(...)})` makes `registry.model("score")` build
its usual stack over the given model: the budget guard, rate limiter and ledger wrappers stay in
the path, so tests can assert ledger rows too. Purposes not named keep their configured model.

A faked purpose resolves to the ref `fake:<purpose>` (priced at 0, §8.9), and the per-process
model cache is replaced by an empty one for the test, so nothing leaks between tests.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

import pytest

from apps.llm import registry

if TYPE_CHECKING:
    from pydantic_ai.models import Model

FAKE_PROVIDER = "fake"


def fake_ref(purpose: str) -> str:
    return f"{FAKE_PROVIDER}:{purpose}"


def fake_models(monkeypatch: pytest.MonkeyPatch, models: Mapping[str, Model]) -> None:
    unknown = set(models) - set(registry.PURPOSES)
    if unknown:
        raise ValueError(f"Not LLM purposes: {', '.join(sorted(unknown))}")

    real_model_ref = registry.model_ref
    real_base_model = registry.base_model

    def model_ref(purpose: registry.Purpose) -> str:
        return fake_ref(purpose) if purpose in models else real_model_ref(purpose)

    def base_model(ref: str) -> Model:
        provider, _, purpose = ref.partition(":")
        if provider == FAKE_PROVIDER and purpose in models:
            return models[purpose]
        return real_base_model(ref)

    monkeypatch.setattr(registry, "model_ref", model_ref)
    monkeypatch.setattr(registry, "base_model", base_model)
    monkeypatch.setattr(registry, "_MODELS", {})  # restored to the real cache after the test
